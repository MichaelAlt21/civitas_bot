from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError, MAX_DISCORD_ID, CitizensService
from civitas.app.receipts import ReceiptService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.ledger import TransactionKind
from civitas.domain.receipts import ReceiptDelivery
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow
from civitas.infrastructure.db.receipt_models import PrivateReceiptRow
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork

pytestmark = pytest.mark.asyncio
GUILD = MAX_DISCORD_ID
SENDER, RECIPIENT, OTHER_CITIZEN = 11, MAX_DISCORD_ID - 1, 12


async def _setup(pg_engine: AsyncEngine):
    unit_of_work = PostgresReceiptUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids={SENDER, RECIPIENT, OTHER_CITIZEN},
        fund_verified=funding.fund_registered_citizen,
    )
    sender = (await citizens.register(GUILD, SENDER)).citizen
    recipient = (await citizens.register(GUILD, RECIPIENT)).citizen
    wallet = WalletService(unit_of_work, guild_id=GUILD, authorize=citizens.authorize, funding=funding)
    return unit_of_work, wallet, citizens, sender, recipient


async def test_retry_after_restart_keeps_one_money_posting_and_own_receipt(pg_engine: AsyncEngine) -> None:
    unit_of_work, wallet, citizens, _, _ = await _setup(pg_engine)
    await citizens.register(GUILD, OTHER_CITIZEN)
    now = datetime.now(UTC)
    service = ReceiptService(unit_of_work, guild_id=GUILD, authorize=citizens.authorize, clock=lambda: now)
    await service.set_preference(GUILD, RECIPIENT, locale="en")

    posted = await wallet.transfer(GUILD, SENDER, RECIPIENT, 100, idempotency_key="transfer")
    repeated = await wallet.transfer(GUILD, SENDER, RECIPIENT, 100, idempotency_key="transfer")
    assert posted.transaction_id == repeated.transaction_id
    deliveries: list[ReceiptDelivery] = []
    observed_balances: list[tuple[int, int]] = []

    async def send(receipt: ReceiptDelivery) -> int:
        deliveries.append(receipt)
        observed_balances.append((await wallet.balance(GUILD, SENDER), await wallet.balance(GUILD, RECIPIENT)))
        if len(deliveries) == 1:
            raise OSError("Simulated Discord outage")
        return MAX_DISCORD_ID

    now = datetime.now(UTC) + timedelta(seconds=1)
    assert not await service.run_due(send)
    assert len(deliveries) == 1
    restarted = ReceiptService(
        PostgresReceiptUnitOfWork(pg_engine), guild_id=GUILD, authorize=citizens.authorize, clock=lambda: now,
    )
    now += timedelta(seconds=29)
    assert not await restarted.run_due(send)
    assert len(deliveries) == 1
    now += timedelta(seconds=1)
    assert await restarted.run_due(send)
    assert not await restarted.run_due(send)

    first, retry = deliveries
    assert (first.recipient_discord_user_id, first.locale, first.delta, first.kind) == (
        RECIPIENT, "en", 100, TransactionKind.TRANSFER,
    )
    assert first.id == retry.id and first.transaction_id == retry.transaction_id == posted.transaction_id
    assert (first.attempts, retry.attempts) == (1, 2)
    assert first.lease_token != retry.lease_token
    assert observed_balances == [(150, 350), (150, 350)]

    outgoing = await restarted.get_receipt(GUILD, SENDER, posted.transaction_id)
    incoming = await restarted.get_receipt(GUILD, RECIPIENT, posted.transaction_id)
    assert outgoing is not None and incoming is not None
    assert (outgoing.delta, incoming.delta) == (-100, 100)
    assert await restarted.get_receipt(GUILD, OTHER_CITIZEN, posted.transaction_id) is None
    with pytest.raises(CitizenshipAccessError, match=AccessDeniedReason.WRONG_GUILD):
        await restarted.get_receipt(GUILD - 1, RECIPIENT, posted.transaction_id)
    async with pg_engine.connect() as connection:
        assert (await connection.execute(
            select(PrivateReceiptRow.id, PrivateReceiptRow.discord_message_id, PrivateReceiptRow.sent_at)
        )).all() == [(first.id, MAX_DISCORD_ID, now)]
        assert await connection.scalar(select(func.count()).select_from(FinancialTransactionRow).where(
            FinancialTransactionRow.kind == TransactionKind.TRANSFER.value
        )) == 1


async def test_expired_lease_reclaims_and_opt_out_suppresses_receipts(pg_engine: AsyncEngine) -> None:
    unit_of_work, wallet, _, _, recipient = await _setup(pg_engine)
    async with unit_of_work() as repository:
        await repository.set_receipt_preference(recipient.guild_id, recipient.id, "ru")
    await wallet.transfer(GUILD, SENDER, RECIPIENT, 10, idempotency_key="transfer-1")

    now = datetime.now(UTC) + timedelta(seconds=1)
    expiry = now + timedelta(seconds=3)
    async with unit_of_work() as repository:
        (first,) = await repository.claim_receipts(GUILD, now=now, lease_until=expiry, limit=1)
        assert await repository.claim_receipts(GUILD, now=now, lease_until=expiry, limit=1) == ()
    async with unit_of_work() as repository:
        (retry,) = await repository.claim_receipts(
            GUILD, now=expiry, lease_until=expiry + timedelta(seconds=3), limit=1
        )
        assert retry.id == first.id and retry.attempts == 2
        assert not await repository.can_deliver_receipt(first.id, first.lease_token, now=expiry)
    async with unit_of_work() as repository:
        await repository.set_receipt_preference(recipient.guild_id, recipient.id, None)
        assert not await repository.can_deliver_receipt(retry.id, retry.lease_token, now=expiry)
    await wallet.transfer(GUILD, SENDER, RECIPIENT, 10, idempotency_key="transfer-2")
    async with unit_of_work() as repository:
        assert not await repository.mark_receipt_sent(retry.id, retry.lease_token, message_id=1, at=expiry)
        await repository.set_receipt_preference(recipient.guild_id, recipient.id, "en")
        assert await repository.claim_receipts(
            GUILD, now=expiry, lease_until=expiry + timedelta(seconds=3), limit=1
        ) == ()
    async with pg_engine.connect() as connection:
        (row,) = (await connection.execute(select(PrivateReceiptRow.suppressed_at))).all()
        assert row.suppressed_at is not None
