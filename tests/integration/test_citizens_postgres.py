import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import (
    MAX_DISCORD_ID,
    CitizenshipAccessError,
    CitizensService,
    RegistrationOutcome,
    RegistrationResult,
)
from civitas.domain.citizens import CitizenAuditAction, CitizenStatus
from civitas.infrastructure.db import PostgresCitizenUnitOfWork
from civitas.infrastructure.db.models import AuditEventRow, CitizenRow, GuildRow

pytestmark = pytest.mark.asyncio

GUILD_ID = MAX_DISCORD_ID - 1
USER_ID = MAX_DISCORD_ID


def _service(engine: AsyncEngine) -> CitizensService:
    return CitizensService(
        PostgresCitizenUnitOfWork(engine),
        guild_id=GUILD_ID,
        allowed_user_ids={USER_ID},
    )


async def _table_counts(engine: AsyncEngine) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(
                    select(func.count()).select_from(GuildRow).scalar_subquery(),
                    select(func.count()).select_from(CitizenRow).scalar_subquery(),
                    select(func.count()).select_from(AuditEventRow).scalar_subquery(),
                )
            )
        ).tuples().one()


async def test_migration_and_registration_audit(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert {"guilds", "citizens", "audit_events", "alembic_version"} <= set(tables)

    service = _service(pg_engine)
    assert await service.get_profile(GUILD_ID, USER_ID) is None
    registration = await service.register(GUILD_ID, USER_ID)
    assert registration.outcome is RegistrationOutcome.CREATED
    citizen = registration.citizen
    assert citizen.status is CitizenStatus.VERIFIED
    assert citizen.discord_user_id == USER_ID
    assert citizen.joined_at.tzinfo is UTC
    assert await service.get_profile(GUILD_ID, USER_ID) == citizen
    async with pg_engine.connect() as connection:
        event = (await connection.execute(select(AuditEventRow.__table__))).mappings().one()
    assert event["action"] == CitizenAuditAction.REGISTERED
    assert event["citizen_id"] == citizen.id
    assert event["guild_id"] == citizen.guild_id
    assert event["actor_discord_user_id"] == USER_ID
    assert event["occurred_at"] == citizen.joined_at
    assert event["reason"] == "discord_allowlist"
    assert event["result"] == "success"


async def test_ten_retries_keep_one_registration_and_audit(pg_engine: AsyncEngine) -> None:
    service = _service(pg_engine)
    created = await service.register(GUILD_ID, USER_ID)
    for _ in range(10):
        repeated = await service.register(GUILD_ID, USER_ID)
        assert repeated.outcome is RegistrationOutcome.EXISTING
        assert repeated.citizen == created.citizen
    assert await _table_counts(pg_engine) == (1, 1, 1)


async def _register_concurrently(
    engine: AsyncEngine,
) -> tuple[RegistrationResult, RegistrationResult]:
    barrier = asyncio.Barrier(2)
    unit_of_work = PostgresCitizenUnitOfWork(engine)

    @asynccontextmanager
    async def synchronized_unit_of_work():
        async with unit_of_work() as repo:
            await barrier.wait()
            yield repo

    service = CitizensService(
        synchronized_unit_of_work, guild_id=GUILD_ID, allowed_user_ids={USER_ID}
    )
    first, second = await asyncio.wait_for(
        asyncio.gather(service.register(GUILD_ID, USER_ID), service.register(GUILD_ID, USER_ID)),
        timeout=10,
    )
    return first, second


@pytest.mark.parametrize("existing_guild", [False, True])
async def test_concurrent_registration(pg_engine: AsyncEngine, existing_guild: bool) -> None:
    if existing_guild:
        async with pg_engine.begin() as connection:
            await connection.execute(
                insert(GuildRow).values(discord_guild_id=GUILD_ID, created_at=datetime.now(UTC))
            )

    first, second = await _register_concurrently(pg_engine)
    assert {first.outcome, second.outcome} == {
        RegistrationOutcome.CREATED,
        RegistrationOutcome.EXISTING,
    }
    assert first.citizen == second.citizen
    assert await _table_counts(pg_engine) == (1, 1, 1)


async def test_audit_error_rolls_back_first_registration(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(text(
            "ALTER TABLE audit_events ADD CONSTRAINT test_reject_audit CHECK (false)"
        ))
    service = _service(pg_engine)
    with pytest.raises(IntegrityError):
        await service.register(GUILD_ID, USER_ID)
    assert await _table_counts(pg_engine) == (0, 0, 0)
    assert await service.get_profile(GUILD_ID, USER_ID) is None


async def test_readmission_preserves_existing_citizen(pg_engine: AsyncEngine) -> None:
    service = _service(pg_engine)
    created = await service.register(GUILD_ID, USER_ID)
    async with pg_engine.begin() as connection:
        await connection.execute(
            update(CitizenRow)
            .where(CitizenRow.id == created.citizen.id)
            .values(status=CitizenStatus.LEFT)
        )

    first, second = await _register_concurrently(pg_engine)
    assert {first.outcome, second.outcome} == {
        RegistrationOutcome.RESTORED,
        RegistrationOutcome.EXISTING,
    }
    assert first.citizen == second.citizen
    restored = first if first.outcome is RegistrationOutcome.RESTORED else second
    assert restored.citizen.id == created.citizen.id
    assert restored.citizen.joined_at == created.citizen.joined_at
    assert restored.citizen.status is CitizenStatus.VERIFIED
    assert (await service.register(GUILD_ID, USER_ID)).outcome is RegistrationOutcome.EXISTING
    async with pg_engine.connect() as connection:
        actions = (await connection.scalars(
            select(AuditEventRow.action).order_by(AuditEventRow.id)
        )).all()
    assert actions == [CitizenAuditAction.REGISTERED, CitizenAuditAction.RESTORED]
    assert await _table_counts(pg_engine) == (1, 1, 2)


async def test_denied_registrations_do_not_write(pg_engine: AsyncEngine) -> None:
    service = _service(pg_engine)
    for guild_id, user_id in [(None, USER_ID), (123, USER_ID), (GUILD_ID, 123)]:
        with pytest.raises(CitizenshipAccessError):
            await service.register(guild_id, user_id)
    assert await _table_counts(pg_engine) == (0, 0, 0)


async def test_role_reconciliation_reads_all_statuses_in_one_guild(
    pg_engine: AsyncEngine,
) -> None:
    unit_of_work = PostgresCitizenUnitOfWork(pg_engine)
    async with unit_of_work() as repository:
        assert await repository.list_role_members(GUILD_ID) == ()
    assert await _table_counts(pg_engine) == (0, 0, 0)

    statuses = {
        USER_ID: CitizenStatus.VERIFIED,
        USER_ID - 1: CitizenStatus.PENDING,
        USER_ID - 2: CitizenStatus.SUSPENDED,
        USER_ID - 3: CitizenStatus.LEFT,
    }
    service = CitizensService(
        unit_of_work, guild_id=GUILD_ID, allowed_user_ids=statuses.keys()
    )
    for user_id, status in statuses.items():
        citizen = (await service.register(GUILD_ID, user_id)).citizen
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(CitizenRow).where(CitizenRow.id == citizen.id).values(status=status.value)
            )

    foreign_guild = GUILD_ID - 1
    foreign_service = CitizensService(
        unit_of_work, guild_id=foreign_guild, allowed_user_ids={USER_ID}
    )
    foreign_citizen = (await foreign_service.register(foreign_guild, USER_ID)).citizen

    async with unit_of_work() as repository:
        members = await repository.list_role_members(GUILD_ID)
        assert await repository.list_role_members(foreign_guild) == (foreign_citizen,)
        assert await repository.list_role_members(GUILD_ID - 2) == ()
    assert [(member.discord_user_id, member.status) for member in members] == list(statuses.items())
    assert await _table_counts(pg_engine) == (2, 5, 5)
