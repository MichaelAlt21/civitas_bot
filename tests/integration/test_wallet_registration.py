import asyncio

import pytest
from sqlalchemy import func, select, text

from civitas.app.citizens import CitizensService, RegistrationOutcome
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.ledger import AccountKind, LedgerError, LedgerErrorReason, TransactionKind
from civitas.infrastructure.db.ledger import PostgresLedgerUnitOfWork
from civitas.infrastructure.db.models import AuditEventRow

GUILD_ID = 101
SENDER = 201
RECIPIENT = 202
SECOND_RECIPIENT = 203

pytestmark = pytest.mark.asyncio


def _services(engine, *, grant=100, genesis=8000):
    unit_of_work = PostgresLedgerUnitOfWork(engine)
    funding = WalletFunding(starter_grant=grant, treasury_genesis=genesis)
    citizens = CitizensService(
        unit_of_work,
        guild_id=GUILD_ID,
        allowed_user_ids={SENDER, RECIPIENT, SECOND_RECIPIENT},
        fund_verified=funding.fund_registered_citizen,
    )
    wallet = WalletService(unit_of_work, guild_id=GUILD_ID, authorize=citizens.authorize, funding=funding)
    return unit_of_work, citizens, wallet


async def test_registration_funds_before_first_wallet_read_and_rejoin_does_not_mint(pg_engine):
    unit_of_work, citizens, wallet = _services(pg_engine)
    await wallet.initialize_guild()
    registration = await citizens.register(GUILD_ID, SENDER)
    citizen = registration.citizen

    async with unit_of_work() as repository:
        account = await repository.ensure_account(citizen.guild_id, AccountKind.PERSONAL, citizen.id)
        assert await repository.get_balance(account.id) == 100

    await citizens.register(GUILD_ID, RECIPIENT)
    receipt = await wallet.transfer(GUILD_ID, SENDER, RECIPIENT, 20, idempotency_key="initial-transfer")
    repeated = await wallet.transfer(GUILD_ID, SENDER, RECIPIENT, 20, idempotency_key="initial-transfer")
    assert repeated.transaction_id == receipt.transaction_id
    assert not repeated.created

    assert await citizens.mark_departed(GUILD_ID, SENDER)
    restored = await citizens.register(GUILD_ID, SENDER)
    assert restored.outcome is RegistrationOutcome.RESTORED
    assert restored.citizen.id == citizen.id
    assert await wallet.balance(GUILD_ID, SENDER) == 80
    assert await wallet.balance(GUILD_ID, RECIPIENT) == 120
    history = await wallet.history(GUILD_ID, SENDER)
    assert [(entry.kind, entry.delta) for entry in history] == [
        (TransactionKind.TRANSFER, -20),
        (TransactionKind.STARTER_GRANT, 100),
    ]


async def test_failed_funding_rolls_back_citizenship_audit_and_postings(pg_engine):
    unit_of_work, _, wallet = _services(pg_engine)
    funding = WalletFunding(starter_grant=100, treasury_genesis=8000)
    await wallet.initialize_guild()

    async def fail_after_funding(repository, citizen):
        await funding.fund_registered_citizen(repository, citizen)
        raise RuntimeError("registration failed before commit")

    citizens = CitizensService(
        unit_of_work, guild_id=GUILD_ID, allowed_user_ids={SENDER}, fund_verified=fail_after_funding
    )
    with pytest.raises(RuntimeError, match="registration failed"):
        await citizens.register(GUILD_ID, SENDER)
    assert await citizens.get_profile(GUILD_ID, SENDER) is None

    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(AuditEventRow)) == 0
        assert await connection.scalar(text("SELECT count(*) FROM ledger_entries")) == 2


async def test_wallet_funds_preexisting_citizen_once_and_keeps_historical_grant(pg_engine):
    unit_of_work, _, wallet = _services(pg_engine)
    old_citizens = CitizensService(unit_of_work, guild_id=GUILD_ID, allowed_user_ids={SENDER})
    await old_citizens.register(GUILD_ID, SENDER)

    assert await wallet.balance(GUILD_ID, SENDER) == 100
    _, _, updated_wallet = _services(pg_engine, grant=250, genesis=9000)
    await updated_wallet.initialize_guild()
    assert await updated_wallet.balance(GUILD_ID, SENDER) == 100
    history = await updated_wallet.history(GUILD_ID, SENDER)
    assert [entry.delta for entry in history] == [100]


async def test_concurrent_wallet_transfers_cannot_spend_same_balance_twice(pg_engine):
    _, citizens, wallet = _services(pg_engine)
    for user_id in (SENDER, RECIPIENT, SECOND_RECIPIENT):
        await citizens.register(GUILD_ID, user_id)

    first, second = await asyncio.wait_for(
        asyncio.gather(
            wallet.transfer(GUILD_ID, SENDER, RECIPIENT, 80, idempotency_key="spend-one"),
            wallet.transfer(GUILD_ID, SENDER, SECOND_RECIPIENT, 80, idempotency_key="spend-two"),
            return_exceptions=True,
        ),
        timeout=10,
    )
    failures = [item for item in (first, second) if isinstance(item, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], LedgerError)
    assert failures[0].reason is LedgerErrorReason.INSUFFICIENT_FUNDS
    assert await wallet.balance(GUILD_ID, SENDER) == 20
    assert sorted([await wallet.balance(GUILD_ID, user_id) for user_id in (RECIPIENT, SECOND_RECIPIENT)]) == [
        100, 180
    ]
