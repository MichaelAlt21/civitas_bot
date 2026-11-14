import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.admin_operators import AdminOperatorPolicy
from civitas.app.citizens import MAX_DISCORD_ID, CitizensService
from civitas.app.maintenance import MaintenanceError, MaintenanceErrorReason, MaintenanceService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.ledger import LedgerError, LedgerErrorReason
from civitas.infrastructure.db.jobs_models import BudgetPeriodRow
from civitas.infrastructure.db.maintenance import PostgresMaintenanceUnitOfWork
from civitas.infrastructure.db.maintenance_models import EconomyMaintenanceAuditRow

pytestmark = pytest.mark.asyncio
GUILD, OPERATOR = MAX_DISCORD_ID, MAX_DISCORD_ID - 1
SENDER, RECIPIENT = 11, 12
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


async def _wallet(engine: AsyncEngine) -> WalletService:
    unit_of_work = PostgresMaintenanceUnitOfWork(engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids={SENDER, RECIPIENT},
        fund_verified=funding.fund_registered_citizen,
    )
    for user_id in (SENDER, RECIPIENT):
        await citizens.register(GUILD, user_id)
    return WalletService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, funding=funding,
    )


async def _toggle(engine: AsyncEngine, read_only: bool) -> bool:
    async with PostgresMaintenanceUnitOfWork(engine)() as repository:
        return await repository.set_economy_read_only(
            GUILD, read_only, actor_discord_user_id=OPERATOR, reason="Maintenance exercise", at=NOW,
        )


async def test_persistent_pause_preserves_reads_and_existing_posting_replay(pg_engine: AsyncEngine) -> None:
    wallet = await _wallet(pg_engine)
    initial = await wallet.transfer(GUILD, SENDER, RECIPIENT, 50, idempotency_key="first")
    assert await _toggle(pg_engine, True)
    assert not await _toggle(pg_engine, True)
    async with PostgresMaintenanceUnitOfWork(pg_engine)() as restarted:
        assert await restarted.get_economy_read_only(GUILD)
        assert not await restarted.get_economy_read_only(GUILD - 2)

    assert await wallet.balance(GUILD, SENDER) == 200
    assert await wallet.balance(GUILD, RECIPIENT) == 300
    replay = await wallet.transfer(GUILD, SENDER, RECIPIENT, 50, idempotency_key="first")
    assert replay.transaction_id == initial.transaction_id and not replay.created
    with pytest.raises(LedgerError) as rejected:
        await wallet.transfer(GUILD, SENDER, RECIPIENT, 10, idempotency_key="paused")
    assert rejected.value.reason is LedgerErrorReason.ECONOMY_READ_ONLY
    assert await wallet.balance(GUILD, SENDER) == 200

    assert await _toggle(pg_engine, False)
    await wallet.transfer(GUILD, SENDER, RECIPIENT, 10, idempotency_key="paused")
    assert await wallet.balance(GUILD, SENDER) == 190
    async with pg_engine.connect() as connection:
        audit = (await connection.execute(select(
            EconomyMaintenanceAuditRow.actor_discord_user_id,
            EconomyMaintenanceAuditRow.previous_read_only,
            EconomyMaintenanceAuditRow.read_only,
        ).order_by(EconomyMaintenanceAuditRow.id))).all()
    assert audit == [(OPERATOR, False, True), (OPERATOR, True, False)]
    with pytest.raises(IntegrityError, match="append-only"):
        async with pg_engine.begin() as connection:
            await connection.execute(update(EconomyMaintenanceAuditRow).values(reason="Changed"))


async def test_pause_wins_against_uncommitted_financial_posting(pg_engine: AsyncEngine) -> None:
    wallet = await _wallet(pg_engine)
    unit_of_work = PostgresMaintenanceUnitOfWork(pg_engine)
    financial_rows_ready = asyncio.Event()
    release_commit = asyncio.Event()

    @asynccontextmanager
    async def delayed_commit():
        async with unit_of_work() as repository:
            yield repository
            financial_rows_ready.set()
            await release_commit.wait()

    delayed = WalletService(
        delayed_commit, guild_id=GUILD, authorize=lambda guild_id, user_id: None,
        funding=WalletFunding(starter_grant=250, treasury_genesis=8000),
    )
    posting = asyncio.create_task(delayed.transfer(GUILD, SENDER, RECIPIENT, 50, idempotency_key="race"))
    try:
        await asyncio.wait_for(financial_rows_ready.wait(), timeout=10)
        assert await asyncio.wait_for(_toggle(pg_engine, True), timeout=10)
    finally:
        release_commit.set()
    with pytest.raises(LedgerError) as rejected:
        await asyncio.wait_for(posting, timeout=10)
    assert rejected.value.reason is LedgerErrorReason.ECONOMY_READ_ONLY
    assert await wallet.balance(GUILD, SENDER) == 250
    assert await wallet.balance(GUILD, RECIPIENT) == 250


async def test_pause_blocks_new_reservations(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresMaintenanceUnitOfWork(pg_engine)
    async with unit_of_work() as repository:
        guild_id = await repository.ensure_guild(GUILD)
    # This fixture tests the pause trigger independently of the separate
    # treasury accounting guards, which run on a real reservation in its suite.
    async with pg_engine.begin() as connection:
        period_id = await connection.scalar(insert(BudgetPeriodRow).values(
            guild_id=guild_id, week_start_date=NOW.date() - timedelta(days=NOW.weekday()),
            start_at=NOW - timedelta(days=1), end_at=NOW + timedelta(days=1),
            overall_cap=500, spent_total=0, reserved_total=0,
        ).returning(BudgetPeriodRow.id))
    assert await _toggle(pg_engine, True)
    with pytest.raises(LedgerError) as rejected:
        async with pg_engine.begin() as connection:
            await connection.execute(update(BudgetPeriodRow).where(
                BudgetPeriodRow.id == period_id
            ).values(reserved_total=100))
    assert rejected.value.reason is LedgerErrorReason.ECONOMY_READ_ONLY
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(BudgetPeriodRow.reserved_total)) == 0


async def test_technical_operator_without_citizenship_can_pause_and_legacy_wallet_reads_remain_available(
    pg_engine: AsyncEngine,
) -> None:
    unit_of_work = PostgresMaintenanceUnitOfWork(pg_engine)
    citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids={SENDER})
    await citizens.register(GUILD, SENDER)
    wallet = WalletService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize,
        funding=WalletFunding(starter_grant=250, treasury_genesis=8000),
    )
    maintenance = MaintenanceService(
        unit_of_work, guild_id=GUILD,
        operator_policy=AdminOperatorPolicy(discord_guild_id=GUILD, operator_user_ids={OPERATOR}),
    )
    with pytest.raises(MaintenanceError) as denied:
        await maintenance.set_read_only(GUILD, SENDER, enabled=True, reason="Not an operator")
    assert denied.value.reason == MaintenanceErrorReason.OPERATOR_REQUIRED
    with pytest.raises(MaintenanceError) as invalid:
        await maintenance.set_read_only(GUILD, OPERATOR, enabled=True, reason=" ")
    assert invalid.value.reason == MaintenanceErrorReason.INVALID_REASON
    assert not await maintenance.status(GUILD, OPERATOR)

    assert (await maintenance.set_read_only(GUILD, OPERATOR, enabled=True, reason=" Incident 12 ")).changed
    assert await wallet.balance(GUILD, SENDER) == 0
    assert await wallet.history(GUILD, SENDER) == ()
    with pytest.raises(LedgerError) as denied_funding:
        await wallet.fund_citizen(GUILD, SENDER)
    assert denied_funding.value.reason == LedgerErrorReason.ECONOMY_READ_ONLY
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(EconomyMaintenanceAuditRow.reason)) == "Incident 12"

    assert (await maintenance.set_read_only(GUILD, OPERATOR, enabled=False, reason="Reconciled")).changed
    assert await wallet.balance(GUILD, SENDER) == 250
