from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import MAX_DISCORD_ID, CitizensService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.ledger import TransactionKind
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow
from civitas.infrastructure.db.receipt_models import PrivateReceiptRow
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork

pytestmark = pytest.mark.asyncio
GUILD = MAX_DISCORD_ID
SENDER, RECIPIENT = 11, MAX_DISCORD_ID - 1


async def _setup(pg_engine: AsyncEngine):
    unit_of_work = PostgresReceiptUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids={SENDER, RECIPIENT},
        fund_verified=funding.fund_registered_citizen,
    )
    sender = (await citizens.register(GUILD, SENDER)).citizen
    recipient = (await citizens.register(GUILD, RECIPIENT)).citizen
    wallet = WalletService(unit_of_work, guild_id=GUILD, authorize=citizens.authorize, funding=funding)
    return unit_of_work, wallet, sender, recipient


async def test_retry_after_restart_keeps_one_money_posting_and_own_receipt(pg_engine: AsyncEngine) -> None:
    unit_of_work, wallet, sender, recipient = await _setup(pg_engine)
    async with unit_of_work() as repository:
        await repository.set_receipt_preference(recipient.guild_id, recipient.id, "en")

    posted = await wallet.transfer(GUILD, SENDER, RECIPIENT, 100, idempotency_key="transfer")
    repeated = await wallet.transfer(GUILD, SENDER, RECIPIENT, 100, idempotency_key="transfer")
    assert posted.transaction_id == repeated.transaction_id
    assert await wallet.balance(GUILD, SENDER) == 150
    assert await wallet.balance(GUILD, RECIPIENT) == 350

    now = datetime.now(UTC) + timedelta(seconds=1)
    async with unit_of_work() as repository:
        (first,) = await repository.claim_receipts(
            GUILD, now=now, lease_until=now + timedelta(seconds=10), limit=1
        )
    assert (first.recipient_discord_user_id, first.locale, first.delta, first.kind) == (
        RECIPIENT, "en", 100, TransactionKind.TRANSFER,
    )
    assert first.attempts == 1
    async with unit_of_work() as repository:
        assert await repository.can_deliver_receipt(first.id, first.lease_token, now=now)
        assert await repository.reschedule_receipt(
            first.id, first.lease_token, retry_at=now + timedelta(seconds=20)
        )
        assert await repository.claim_receipts(
            GUILD, now=now, lease_until=now + timedelta(seconds=10), limit=1
        ) == ()

    restarted = PostgresReceiptUnitOfWork(pg_engine)
    later = now + timedelta(seconds=20)
    async with restarted() as repository:
        (retry,) = await repository.claim_receipts(
            GUILD, now=later, lease_until=later + timedelta(seconds=10), limit=1
        )
        assert retry.id == first.id and retry.attempts == 2
        assert retry.lease_token != first.lease_token
        assert not await repository.mark_receipt_sent(first.id, first.lease_token, message_id=1, at=later)
        assert await repository.mark_receipt_sent(retry.id, retry.lease_token, message_id=MAX_DISCORD_ID, at=later)
        outgoing = await repository.get_own_receipt(sender.guild_id, sender.id, posted.transaction_id)
        incoming = await repository.get_own_receipt(recipient.guild_id, recipient.id, posted.transaction_id)
        assert (outgoing.delta, incoming.delta) == (-100, 100)
        assert await repository.get_own_receipt(recipient.guild_id + 1, recipient.id, posted.transaction_id) is None
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(PrivateReceiptRow)) == 1
        assert await connection.scalar(select(func.count()).select_from(FinancialTransactionRow).where(
            FinancialTransactionRow.kind == TransactionKind.TRANSFER.value
        )) == 1


async def test_expired_lease_reclaims_and_opt_out_suppresses_receipts(pg_engine: AsyncEngine) -> None:
    unit_of_work, wallet, _, recipient = await _setup(pg_engine)
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
