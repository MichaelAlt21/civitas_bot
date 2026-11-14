from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.domain.citizens import CitizenStatus
from civitas.domain.ledger import AccountKind, TransactionKind
from civitas.domain.receipts import ReceiptDelivery, ReceiptDetails

from .government import PostgresGovernmentRepository
from .ledger_models import FinancialAccountRow, FinancialTransactionRow, LedgerEntryRow
from .models import CitizenRow, GuildRow
from .receipt_models import PrivateReceiptRow, ReceiptPreferenceRow


def _pending():
    return (PrivateReceiptRow.sent_at.is_(None), PrivateReceiptRow.suppressed_at.is_(None))


def _own_personal_account():
    return (
        (FinancialAccountRow.id == PrivateReceiptRow.account_id)
        & (FinancialAccountRow.guild_id == PrivateReceiptRow.guild_id)
        & (FinancialAccountRow.citizen_id == PrivateReceiptRow.citizen_id)
        & (FinancialAccountRow.kind == AccountKind.PERSONAL.value)
    )


class PostgresReceiptRepository(PostgresGovernmentRepository):
    async def get_own_receipt(
        self, guild_id: int, citizen_id: int, transaction_id: int,
    ) -> ReceiptDetails | None:
        row = (await self._session.execute(
            select(LedgerEntryRow.delta, FinancialTransactionRow.kind, FinancialTransactionRow.created_at)
            .join(FinancialTransactionRow, FinancialTransactionRow.id == LedgerEntryRow.transaction_id)
            .join(FinancialAccountRow, FinancialAccountRow.id == LedgerEntryRow.account_id)
            .where(
                FinancialAccountRow.guild_id == guild_id, FinancialAccountRow.citizen_id == citizen_id,
                FinancialAccountRow.kind == AccountKind.PERSONAL.value,
                LedgerEntryRow.transaction_id == transaction_id,
            )
        )).one_or_none()
        if row is None:
            return None
        delta, kind, created_at = row
        return ReceiptDetails(
            transaction_id=transaction_id, delta=delta,
            kind=TransactionKind(kind), created_at=created_at.astimezone(UTC),
        )

    async def set_receipt_preference(self, guild_id: int, citizen_id: int, locale: str | None) -> None:
        if locale is None:
            await self._session.execute(delete(ReceiptPreferenceRow).where(
                ReceiptPreferenceRow.guild_id == guild_id, ReceiptPreferenceRow.citizen_id == citizen_id,
            ))
            await self._session.execute(update(PrivateReceiptRow).where(
                PrivateReceiptRow.guild_id == guild_id, PrivateReceiptRow.citizen_id == citizen_id,
                *_pending(),
            ).values(suppressed_at=datetime.now(UTC), lease_token=None, lease_until=None))
            return
        await self._session.execute(insert(ReceiptPreferenceRow).values(
            guild_id=guild_id, citizen_id=citizen_id, locale=locale,
        ).on_conflict_do_update(
            index_elements=[ReceiptPreferenceRow.citizen_id], set_={"locale": locale},
            where=ReceiptPreferenceRow.guild_id == guild_id,
        ))

    async def claim_receipts(
        self, discord_guild_id: int, *, now: datetime, lease_until: datetime, limit: int,
    ) -> tuple[ReceiptDelivery, ...]:
        rows = (await self._session.execute(
            select(
                PrivateReceiptRow, CitizenRow.discord_user_id, LedgerEntryRow.delta,
                FinancialTransactionRow.kind, FinancialTransactionRow.created_at,
            )
            .join(GuildRow, GuildRow.id == PrivateReceiptRow.guild_id)
            .join(CitizenRow, CitizenRow.id == PrivateReceiptRow.citizen_id)
            .join(ReceiptPreferenceRow, ReceiptPreferenceRow.citizen_id == CitizenRow.id)
            .join(FinancialAccountRow, _own_personal_account())
            .join(FinancialTransactionRow, FinancialTransactionRow.id == PrivateReceiptRow.transaction_id)
            .join(LedgerEntryRow, (LedgerEntryRow.transaction_id == PrivateReceiptRow.transaction_id)
                  & (LedgerEntryRow.account_id == PrivateReceiptRow.account_id))
            .where(
                GuildRow.discord_guild_id == Decimal(discord_guild_id),
                CitizenRow.status == CitizenStatus.VERIFIED.value,
                *_pending(),
                PrivateReceiptRow.next_attempt_at <= now,
                or_(PrivateReceiptRow.lease_until.is_(None), PrivateReceiptRow.lease_until <= now),
            )
            .order_by(PrivateReceiptRow.next_attempt_at, PrivateReceiptRow.id)
            .limit(limit)
            .with_for_update(of=PrivateReceiptRow, skip_locked=True)
        )).all()
        deliveries = []
        for receipt, user_id, delta, kind, created_at in rows:
            receipt.lease_token = uuid4()
            receipt.lease_until = lease_until
            receipt.attempts += 1
            deliveries.append(ReceiptDelivery(
                id=receipt.id, recipient_discord_user_id=int(user_id), locale=receipt.locale,
                delta=delta, kind=TransactionKind(kind), transaction_id=receipt.transaction_id,
                created_at=created_at.astimezone(UTC), lease_token=receipt.lease_token,
                attempts=receipt.attempts,
            ))
        return tuple(deliveries)

    async def can_deliver_receipt(self, receipt_id: int, lease_token: UUID, *, now: datetime) -> bool:
        eligible = (
            select(PrivateReceiptRow.id)
            .join(CitizenRow, CitizenRow.id == PrivateReceiptRow.citizen_id)
            .join(ReceiptPreferenceRow, ReceiptPreferenceRow.citizen_id == CitizenRow.id)
            .join(FinancialAccountRow, _own_personal_account())
            .where(
                PrivateReceiptRow.id == receipt_id, PrivateReceiptRow.lease_token == lease_token,
                PrivateReceiptRow.lease_until > now, *_pending(),
                CitizenRow.status == CitizenStatus.VERIFIED.value,
            )
        )
        return bool(await self._session.scalar(select(exists(eligible))))

    async def _finish_receipt(self, receipt_id: int, lease_token: UUID, **values: object) -> bool:
        result = await self._session.scalar(
            update(PrivateReceiptRow).where(
                PrivateReceiptRow.id == receipt_id, PrivateReceiptRow.lease_token == lease_token,
                *_pending(),
            ).values(lease_token=None, lease_until=None, **values).returning(PrivateReceiptRow.id)
        )
        return result is not None

    async def mark_receipt_sent(
        self, receipt_id: int, lease_token: UUID, *, message_id: int, at: datetime,
    ) -> bool:
        return await self._finish_receipt(
            receipt_id, lease_token, sent_at=at, discord_message_id=Decimal(message_id)
        )

    async def reschedule_receipt(
        self, receipt_id: int, lease_token: UUID, *, retry_at: datetime,
    ) -> bool:
        return await self._finish_receipt(receipt_id, lease_token, next_attempt_at=retry_at)

    async def suppress_receipt(self, receipt_id: int, lease_token: UUID, *, at: datetime) -> bool:
        return await self._finish_receipt(receipt_id, lease_token, suppressed_at=at)


class PostgresReceiptUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresReceiptRepository]:
        async with self._sessions.begin() as session:
            yield PostgresReceiptRepository(session)
