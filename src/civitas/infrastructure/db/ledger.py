from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.domain.citizens import Citizen
from civitas.domain.ledger import (
    MAX_AMOUNT,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    Account,
    AccountKind,
    HistoryEntry,
    LedgerError,
    LedgerErrorReason,
    PostResult,
    TransactionKind,
)

from .citizens import PostgresCitizensRepository, _to_citizen
from .court_appeal_models import CourtAppealConfirmationRow
from .court_case_models import CourtActDecisionRow, CourtCaseRow
from .jobs_models import BudgetPeriodRow
from .ledger_models import FinancialAccountRow, FinancialTransactionRow, LedgerEntryRow
from .models import CitizenRow, GuildRow

_MIN_BIGINT = -MAX_AMOUNT - 1


def active_custody_at(at: datetime) -> ColumnElement[bool]:
    overturned = select(CourtAppealConfirmationRow.case_id).where(
        CourtAppealConfirmationRow.guild_id == CourtActDecisionRow.guild_id,
        CourtAppealConfirmationRow.case_id == CourtActDecisionRow.case_id,
        CourtAppealConfirmationRow.outcome == "OVERTURNED",
        CourtAppealConfirmationRow.confirmed_at <= at,
    ).exists()
    return (
        CourtActDecisionRow.custody_hours.is_not(None)
        & (CourtActDecisionRow.decided_at <= at)
        & (CourtActDecisionRow.decided_at + CourtActDecisionRow.custody_hours * text("interval '1 hour'") > at)
        & ~overturned
    )


class PostgresLedgerRepository(PostgresCitizensRepository):
    async def require_free(self, guild_id: int, citizen_id: int, *, at: datetime) -> None:
        active = select(CourtActDecisionRow.case_id).join(
            CourtCaseRow, CourtCaseRow.id == CourtActDecisionRow.case_id,
        ).where(
            CourtActDecisionRow.guild_id == guild_id, CourtCaseRow.respondent_citizen_id == citizen_id,
            active_custody_at(at),
        ).exists()
        if await self._session.scalar(select(active)):
            raise LedgerError(LedgerErrorReason.IN_CUSTODY)

    async def ensure_guild(self, guild_id: int) -> int:
        return await self._get_or_create_guild(guild_id, datetime.now(UTC))

    async def get_citizens(
        self, guild_id: int, user_ids: tuple[int, ...], *, lock: bool = False
    ) -> tuple[Citizen, ...]:
        statement = (
            select(CitizenRow)
            .join(GuildRow, GuildRow.id == CitizenRow.guild_id)
            .where(
                GuildRow.discord_guild_id == Decimal(guild_id),
                CitizenRow.discord_user_id.in_([Decimal(user_id) for user_id in user_ids]),
            )
            .order_by(CitizenRow.id)
        )
        if lock:
            statement = statement.with_for_update(of=CitizenRow, key_share=True)
        rows = await self._session.scalars(statement)
        return tuple(_to_citizen(row) for row in rows)

    async def ensure_account(
        self, guild_id: int, kind: AccountKind, citizen_id: int | None = None
    ) -> Account:
        return await self._ensure_account(guild_id, kind, citizen_id=citizen_id)

    async def ensure_company_account(self, guild_id: int, company_id: int) -> Account:
        return await self._ensure_account(guild_id, AccountKind.COMPANY, company_id=company_id)

    async def ensure_escrow_account(self, guild_id: int, order_id: int) -> Account:
        return await self._ensure_account(guild_id, AccountKind.ESCROW, order_id=order_id)

    async def ensure_private_job_escrow(self, guild_id: int, job_id: int) -> Account:
        return await self._ensure_account(guild_id, AccountKind.ESCROW, private_job_id=job_id)

    async def hold_court_fine(self, guild_id: int, case_id: int) -> PostResult:
        record = await self._court_fine(guild_id, case_id)
        if record is None:
            raise LedgerError(LedgerErrorReason.INVALID_POSTING)
        decision, respondent_id = record
        overturned = await self._session.scalar(select(CourtAppealConfirmationRow.case_id).where(
            CourtAppealConfirmationRow.guild_id == guild_id, CourtAppealConfirmationRow.case_id == case_id,
            CourtAppealConfirmationRow.outcome == "OVERTURNED",
        ))
        if overturned is not None:
            raise LedgerError(LedgerErrorReason.INVALID_POSTING)
        personal = await self.ensure_account(guild_id, AccountKind.PERSONAL, respondent_id)
        escrow = await self._ensure_account(guild_id, AccountKind.ESCROW, court_case_id=case_id)
        return await self.post(
            guild_id, TransactionKind.COURT_FINE_HOLD, f"court_fine_hold:{case_id}",
            {personal.id: -decision.fine_amount, escrow.id: decision.fine_amount},
            actor_citizen_id=decision.judge_citizen_id, reason=decision.reason,
        )

    async def _court_fine(self, guild_id: int, case_id: int):
        return (await self._session.execute(
            select(CourtActDecisionRow, CourtCaseRow.respondent_citizen_id)
            .join(CourtCaseRow, CourtCaseRow.id == CourtActDecisionRow.case_id)
            .where(
                CourtActDecisionRow.guild_id == guild_id, CourtActDecisionRow.case_id == case_id,
                CourtActDecisionRow.fine_amount.is_not(None),
            )
            .with_for_update(of=CourtActDecisionRow, key_share=True),
        )).one_or_none()

    async def close_court_fine(
        self, guild_id: int, case_id: int, *, refund: bool, confirmed_by: int | None, settled_at: datetime | None = None,
    ) -> PostResult | None:
        record = await self._court_fine(guild_id, case_id)
        if record is None or await self.get_post(
            guild_id, TransactionKind.COURT_FINE_HOLD, f"court_fine_hold:{case_id}",
        ) is None:
            return None
        decision, respondent_id = record
        escrow = await self._ensure_account(guild_id, AccountKind.ESCROW, court_case_id=case_id)
        if refund:
            destination = await self.ensure_account(guild_id, AccountKind.PERSONAL, respondent_id)
            kind = TransactionKind.COURT_FINE_REFUND
        else:
            destination = await self.ensure_account(guild_id, AccountKind.TREASURY)
            kind = TransactionKind.COURT_FINE_SETTLE
        return await self.post(
            guild_id, kind, f"{kind.value.lower()}:{case_id}",
            {escrow.id: -decision.fine_amount, destination.id: decision.fine_amount},
            actor_citizen_id=confirmed_by, reason=decision.reason, posted_at=settled_at,
        )

    async def _ensure_account(
        self,
        guild_id: int,
        kind: AccountKind,
        *,
        citizen_id: int | None = None,
        company_id: int | None = None,
        order_id: int | None = None,
        private_job_id: int | None = None,
        court_case_id: int | None = None,
    ) -> Account:
        row = await self._session.scalar(
            insert(FinancialAccountRow)
            .values(
                guild_id=guild_id, kind=kind.value, citizen_id=citizen_id,
                organization_id=company_id, order_id=order_id, private_job_id=private_job_id,
                court_case_id=court_case_id,
            )
            .on_conflict_do_nothing(constraint="uq_financial_accounts_owner")
            .returning(FinancialAccountRow)
        )
        if row is None:
            row = (
                await self._session.scalars(
                    select(FinancialAccountRow).where(
                        FinancialAccountRow.guild_id == guild_id,
                        FinancialAccountRow.kind == kind.value,
                        FinancialAccountRow.citizen_id == citizen_id,
                        FinancialAccountRow.organization_id == company_id,
                        FinancialAccountRow.order_id == order_id,
                        FinancialAccountRow.private_job_id == private_job_id,
                        FinancialAccountRow.court_case_id == court_case_id,
                    )
                )
            ).one()
        return Account(
            id=row.id, guild_id=row.guild_id, kind=AccountKind(row.kind),
            citizen_id=row.citizen_id, company_id=row.organization_id, order_id=row.order_id,
            private_job_id=row.private_job_id,
            court_case_id=row.court_case_id,
        )

    async def get_balance(self, account_id: int) -> int:
        balance = await self._session.scalar(
            select(func.coalesce(func.sum(LedgerEntryRow.delta), 0)).where(
                LedgerEntryRow.account_id == account_id
            )
        )
        return int(balance)

    async def get_personal_account_id(self, guild_id: int, citizen_id: int) -> int | None:
        return await self._session.scalar(select(FinancialAccountRow.id).where(
            FinancialAccountRow.guild_id == guild_id, FinancialAccountRow.citizen_id == citizen_id,
            FinancialAccountRow.kind == AccountKind.PERSONAL.value,
        ))

    async def _reserved_treasury_cash(self, guild_id: int) -> int:
        reserved = await self._session.scalar(
            select(func.coalesce(func.sum(BudgetPeriodRow.reserved_total), 0)).where(
                BudgetPeriodRow.guild_id == guild_id
            )
        )
        return int(reserved)

    async def get_history(self, account_id: int, limit: int) -> tuple[HistoryEntry, ...]:
        rows = await self._session.execute(
            select(
                FinancialTransactionRow.id,
                FinancialTransactionRow.kind,
                LedgerEntryRow.delta,
                FinancialTransactionRow.created_at,
            )
            .join(LedgerEntryRow, LedgerEntryRow.transaction_id == FinancialTransactionRow.id)
            .where(LedgerEntryRow.account_id == account_id)
            .order_by(FinancialTransactionRow.created_at.desc(), FinancialTransactionRow.id.desc())
            .limit(limit)
        )
        return tuple(
            HistoryEntry(
                transaction_id=transaction_id,
                kind=TransactionKind(kind),
                delta=delta,
                created_at=created_at.astimezone(UTC),
            )
            for transaction_id, kind, delta, created_at in rows
        )

    async def _get_transaction(
        self, guild_id: int, kind: TransactionKind, key: str
    ) -> FinancialTransactionRow | None:
        return await self._session.scalar(
            select(FinancialTransactionRow).where(
                FinancialTransactionRow.guild_id == guild_id,
                FinancialTransactionRow.kind == kind.value,
                FinancialTransactionRow.idempotency_key == key,
            )
        )

    async def get_post(
        self, guild_id: int, kind: TransactionKind, key: str
    ) -> PostResult | None:
        transaction = await self._get_transaction(guild_id, kind, key)
        return PostResult(transaction_id=transaction.id, created=False) if transaction else None

    async def _replay(
        self,
        transaction: FinancialTransactionRow,
        entries: Mapping[int, int],
        actor_citizen_id: int | None,
        reason: str,
    ) -> PostResult:
        rows = await self._session.execute(
            select(LedgerEntryRow.account_id, LedgerEntryRow.delta).where(
                LedgerEntryRow.transaction_id == transaction.id
            )
        )
        if (
            transaction.actor_citizen_id != actor_citizen_id
            or transaction.reason != reason
            or dict(rows.tuples().all()) != entries
        ):
            raise LedgerError(LedgerErrorReason.IDEMPOTENCY_CONFLICT)
        return PostResult(transaction_id=transaction.id, created=False)

    async def post(
        self,
        guild_id: int,
        kind: TransactionKind,
        key: str,
        entries: Mapping[int, int],
        *,
        actor_citizen_id: int | None = None,
        reason: str,
        posted_at: datetime | None = None,
    ) -> PostResult:
        entries = dict(entries)
        if (
            not 1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH
            or not reason.strip()
            or len(entries) < 2
            or any(
                type(delta) is not int or delta == 0 or not _MIN_BIGINT <= delta <= MAX_AMOUNT
                for delta in entries.values()
            )
            or sum(entries.values()) != 0
        ):
            raise LedgerError(LedgerErrorReason.INVALID_POSTING)

        account_ids = tuple(entries)
        accounts = (
            await self._session.scalars(
                select(FinancialAccountRow)
                .where(
                    FinancialAccountRow.guild_id == guild_id,
                    FinancialAccountRow.id.in_(account_ids),
                )
                .order_by(FinancialAccountRow.id)
                .with_for_update(key_share=True)
            )
        ).all()
        if len(accounts) != len(entries):
            raise LedgerError(LedgerErrorReason.INVALID_POSTING)
        existing = await self._get_transaction(guild_id, kind, key)
        if existing is not None:
            return await self._replay(existing, entries, actor_citizen_id, reason)

        if kind is TransactionKind.COURT_FINE_HOLD:
            if len(accounts) != 2:
                raise LedgerError(LedgerErrorReason.INVALID_POSTING)
            personal = next(account for account in accounts if entries[account.id] < 0)
            escrow = next(account for account in accounts if entries[account.id] > 0)
            if escrow.court_case_id is None or key != f"court_fine_hold:{escrow.court_case_id}":
                raise LedgerError(LedgerErrorReason.INVALID_POSTING)
            record = await self._court_fine(guild_id, escrow.court_case_id)
            if record is None:
                raise LedgerError(LedgerErrorReason.INVALID_POSTING)
            decision, respondent_id = record
            if (
                personal.kind != AccountKind.PERSONAL or personal.citizen_id != respondent_id
                or entries[escrow.id] != decision.fine_amount or actor_citizen_id != decision.judge_citizen_id
                or reason != decision.reason
            ):
                raise LedgerError(LedgerErrorReason.INVALID_POSTING)
        elif kind not in (TransactionKind.COURT_FINE_REFUND, TransactionKind.COURT_FINE_SETTLE):
            if any(account.court_case_id is not None for account in accounts):
                raise LedgerError(LedgerErrorReason.INVALID_POSTING)
            _validate_accounts(kind, key, accounts, entries)
        balances = await self._session.execute(
            select(LedgerEntryRow.account_id, func.sum(LedgerEntryRow.delta))
            .where(LedgerEntryRow.account_id.in_(account_ids))
            .group_by(LedgerEntryRow.account_id)
        )
        current = dict(balances.tuples().all())
        for account in accounts:
            balance = current.get(account.id, 0) + entries[account.id]
            if account.kind != AccountKind.ISSUER and balance < 0:
                raise LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
            if (
                account.kind == AccountKind.TREASURY
                and entries[account.id] < 0
                and balance < await self._reserved_treasury_cash(guild_id)
            ):
                raise LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
            if not _MIN_BIGINT <= balance <= MAX_AMOUNT:
                raise LedgerError(LedgerErrorReason.BALANCE_OVERFLOW)

        transaction_id = await self._session.scalar(
            insert(FinancialTransactionRow)
            .values(
                guild_id=guild_id,
                kind=kind.value,
                idempotency_key=key,
                entry_count=len(entries),
                actor_citizen_id=actor_citizen_id,
                reason=reason,
                **({"created_at": posted_at} if posted_at is not None else {}),
            )
            .on_conflict_do_nothing(constraint="uq_financial_transactions_operation")
            .returning(FinancialTransactionRow.id)
        )
        if transaction_id is None:
            # Requests with disjoint accounts can still race on the operation key.
            winner = await self._get_transaction(guild_id, kind, key)
            assert winner is not None
            return await self._replay(winner, entries, actor_citizen_id, reason)

        await self._session.execute(
            insert(LedgerEntryRow),
            [
                {
                    "transaction_id": transaction_id,
                    "account_id": account.id,
                    "guild_id": guild_id,
                    "delta": entries[account.id],
                }
                for account in accounts
            ],
        )
        return PostResult(transaction_id=transaction_id, created=True)


def _validate_accounts(
    kind: TransactionKind,
    key: str,
    accounts: list[FinancialAccountRow],
    entries: Mapping[int, int],
) -> None:
    if kind in (TransactionKind.PRIVATE_JOB_FUND, TransactionKind.PRIVATE_JOB_PAY, TransactionKind.PRIVATE_JOB_REFUND):
        if len(accounts) == 2:
            debit = next(account for account in accounts if entries[account.id] < 0)
            credit = next(account for account in accounts if entries[account.id] > 0)
            escrow, counterparty = (credit, debit) if kind == TransactionKind.PRIVATE_JOB_FUND else (debit, credit)
            expected = AccountKind.PERSONAL if kind == TransactionKind.PRIVATE_JOB_PAY else AccountKind.COMPANY
            if (
                escrow.kind == AccountKind.ESCROW and escrow.private_job_id is not None
                and counterparty.kind == expected and key == f"{kind.value.lower()}:{escrow.private_job_id}"
            ):
                return
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    if kind == TransactionKind.STATE_CONTRACT_PAYOUT:
        if (
            len(accounts) == 2
            and key.startswith("state_contract_payout:")
            and any(account.kind == AccountKind.TREASURY and entries[account.id] < 0
                    for account in accounts)
            and any(account.kind in (AccountKind.PERSONAL, AccountKind.COMPANY)
                    and entries[account.id] > 0 for account in accounts)
        ):
            return
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    if kind == TransactionKind.ASSET_PURCHASE:
        if (
            len(accounts) == 2
            and key.startswith("asset_purchase:")
            and any(account.kind in (AccountKind.PERSONAL, AccountKind.COMPANY)
                    and entries[account.id] < 0 for account in accounts)
            and any(account.kind == AccountKind.TREASURY and entries[account.id] > 0
                    for account in accounts)
        ):
            return
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    if kind == TransactionKind.ASSET_RESALE:
        prefix, _, offer_id = key.partition(":")
        debits = [account.kind for account in accounts if entries[account.id] < 0]
        credits = [account.kind for account in accounts if entries[account.id] > 0]
        valid_credits = credits == [AccountKind.PERSONAL] or (
            len(credits) == 2 and set(credits) == {AccountKind.PERSONAL, AccountKind.TREASURY}
        )
        if (
            prefix == "asset_resale"
            and offer_id.isascii() and offer_id.isdecimal() and not offer_id.startswith("0")
            and int(offer_id) <= MAX_AMOUNT
            and debits == [AccountKind.PERSONAL]
            and valid_credits
        ):
            return
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    if kind in {
        TransactionKind.COMPANY_FEE, TransactionKind.ORDER_FUND,
        TransactionKind.ORDER_SETTLE, TransactionKind.ORDER_REFUND,
    }:
        _validate_market_accounts(kind, key, accounts, entries)
        return

    if kind == TransactionKind.NPC_PAYOUT:
        if (
            len(accounts) == 2
            and key.startswith("npc_payout:")
            and any(account.kind == AccountKind.TREASURY and entries[account.id] < 0
                    for account in accounts)
            and any(account.kind == AccountKind.PERSONAL and entries[account.id] > 0
                    for account in accounts)
        ):
            return
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    issuers = [account for account in accounts if account.kind == AccountKind.ISSUER]
    if kind == TransactionKind.TRANSFER and all(
        account.kind in (AccountKind.PERSONAL, AccountKind.TREASURY) for account in accounts
    ):
        return
    if len(accounts) == 2 and len(issuers) == 1 and entries[issuers[0].id] < 0:
        recipient = next(account for account in accounts if account.id != issuers[0].id)
        if (
            kind == TransactionKind.GENESIS
            and recipient.kind == AccountKind.TREASURY
            and key == "genesis"
        ):
            return
        if (
            kind == TransactionKind.STARTER_GRANT
            and recipient.kind == AccountKind.PERSONAL
            and key == f"starter_grant:{recipient.citizen_id}"
        ):
            return
    raise LedgerError(LedgerErrorReason.INVALID_POSTING)


def _validate_market_accounts(
    kind: TransactionKind,
    key: str,
    accounts: list[FinancialAccountRow],
    entries: Mapping[int, int],
) -> None:
    if not key.startswith(f"{kind.value.lower()}:"):
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)

    debits = [AccountKind(account.kind) for account in accounts if entries[account.id] < 0]
    credits = [AccountKind(account.kind) for account in accounts if entries[account.id] > 0]
    if kind == TransactionKind.COMPANY_FEE:
        valid = debits == [AccountKind.PERSONAL] and credits == [AccountKind.TREASURY]
    elif kind == TransactionKind.ORDER_FUND:
        valid = len(debits) == 1 and debits[0] in (AccountKind.PERSONAL, AccountKind.COMPANY)
        valid = valid and credits == [AccountKind.ESCROW]
    elif kind == TransactionKind.ORDER_REFUND:
        valid = debits == [AccountKind.ESCROW] and len(credits) == 1
        valid = valid and credits[0] in (AccountKind.PERSONAL, AccountKind.COMPANY)
    else:
        valid = debits == [AccountKind.ESCROW] and 1 <= len(credits) <= 2
        valid = valid and set(credits) <= {AccountKind.COMPANY, AccountKind.TREASURY}
        valid = valid and len(credits) == len(set(credits))
    if not valid:
        raise LedgerError(LedgerErrorReason.INVALID_POSTING)


class PostgresLedgerUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresLedgerRepository]:
        async with self._sessions.begin() as session:
            yield PostgresLedgerRepository(session)
