import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from civitas.app.admission import AdmissionError, AdmissionErrorReason, AdmissionService
from civitas.app.citizens import (
    MAX_DISCORD_ID,
    AccessDeniedReason,
    CitizenshipAccessError,
    CitizensService,
    RegistrationOutcome,
)
from civitas.app.wallet import WalletFunding, WalletService
from civitas.infrastructure.db.admission import PostgresAdmissionUnitOfWork
from civitas.infrastructure.db.admission_models import AdmissionAuditRow
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = MAX_DISCORD_ID - 2
ADMIN = 501
PLAYER = MAX_DISCORD_ID


async def test_discord_admission_changes_persist_and_preserve_citizen_and_grant(pg_engine):
    admission_uow = PostgresAdmissionUnitOfWork(pg_engine)
    admission = AdmissionService(admission_uow, guild_id=GUILD)
    await admission.initialize(())
    uow = PostgresReceiptUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        uow, guild_id=GUILD, authorize_admission=admission.authorize, fund_verified=funding.fund_registered_citizen,
    )
    wallet = WalletService(uow, guild_id=GUILD, authorize=citizens.authorize, funding=funding)
    await wallet.initialize_guild()

    async def update_admission(allowed: bool, reason: str = "") -> bool:
        return await admission.set_allowed(
            GUILD, ADMIN, administrator=True, target_user_id=PLAYER, allowed=allowed, reason=reason,
        )

    with pytest.raises(CitizenshipAccessError, match=AccessDeniedReason.NOT_ALLOWED):
        await citizens.register(GUILD, PLAYER)
    with pytest.raises(AdmissionError, match=AdmissionErrorReason.ADMINISTRATOR_REQUIRED):
        await admission.set_allowed(GUILD, PLAYER, administrator=False, target_user_id=PLAYER, allowed=True)
    with pytest.raises(CitizenshipAccessError, match=AccessDeniedReason.WRONG_GUILD):
        await admission.list_allowed(GUILD + 1, ADMIN, administrator=True)

    assert sorted(await asyncio.gather(update_admission(True), update_admission(True))) == [False, True]
    original = await citizens.register(GUILD, PLAYER)
    assert original.outcome is RegistrationOutcome.CREATED
    assert await wallet.balance(GUILD, PLAYER) == 250
    assert await admission.list_allowed(GUILD, ADMIN, administrator=True) == (PLAYER,)

    assert sorted(await asyncio.gather(
        update_admission(False, "Removed by staff"), update_admission(False, "Removed by staff"),
    )) == [False, True]
    with pytest.raises(CitizenshipAccessError, match=AccessDeniedReason.NOT_ALLOWED):
        await wallet.balance(GUILD, PLAYER)

    after_restart = AdmissionService(PostgresAdmissionUnitOfWork(pg_engine), guild_id=GUILD)
    await after_restart.initialize((PLAYER,))
    assert await after_restart.list_allowed(GUILD, ADMIN, administrator=True) == ()
    assert await update_admission(True)
    existing = await citizens.register(GUILD, PLAYER)
    assert existing.outcome is RegistrationOutcome.EXISTING
    assert existing.citizen == original.citizen
    assert await wallet.balance(GUILD, PLAYER) == 250

    async with pg_engine.connect() as connection:
        records = (await connection.execute(
            select(
                AdmissionAuditRow.actor_discord_user_id, AdmissionAuditRow.target_discord_user_id,
                AdmissionAuditRow.allowed, AdmissionAuditRow.reason,
            ).order_by(AdmissionAuditRow.id),
        )).all()
    assert records == [
        (ADMIN, PLAYER, True, "discord_administrator"),
        (ADMIN, PLAYER, False, "Removed by staff"),
        (ADMIN, PLAYER, True, "discord_administrator"),
    ]


async def test_admission_migration_only_downgrades_before_roster_initialization(pg_engine):
    def downgrade(connection):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.downgrade(config, "0021_electorate_admission")

    def upgrade(connection):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async with pg_engine.begin() as connection:
        await connection.run_sync(downgrade)
        await connection.run_sync(upgrade)

    admission = AdmissionService(PostgresAdmissionUnitOfWork(pg_engine), guild_id=GUILD)
    await admission.initialize(())
    with pytest.raises(DBAPIError, match="Cannot downgrade while an admission roster is initialized"):
        async with pg_engine.begin() as connection:
            await connection.run_sync(downgrade)
    await admission.initialize((PLAYER,))
    assert await admission.list_allowed(GUILD, ADMIN, administrator=True) == ()
