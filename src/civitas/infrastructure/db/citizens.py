from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from civitas.domain.citizens import Citizen, CitizenAuditEvent, CitizenStatus

from .models import AuditEventRow, CitizenRow, GuildRow


def _to_citizen(row: CitizenRow) -> Citizen:
    return Citizen(
        id=row.id,
        guild_id=row.guild_id,
        discord_user_id=int(row.discord_user_id),
        status=CitizenStatus(row.status),
        joined_at=row.joined_at.astimezone(UTC),
        verified_at=row.verified_at.astimezone(UTC) if row.verified_at else None,
    )


class PostgresCitizensRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_profile(self, guild_id: int, user_id: int, *, lock: bool = False) -> Citizen | None:
        statement = (
            select(CitizenRow)
            .join(GuildRow, GuildRow.id == CitizenRow.guild_id)
            .where(
                GuildRow.discord_guild_id == Decimal(guild_id),
                CitizenRow.discord_user_id == Decimal(user_id),
            )
        )
        if lock:
            statement = statement.with_for_update(of=CitizenRow, key_share=True)
        row = await self._session.scalar(statement)
        return _to_citizen(row) if row is not None else None

    async def list_role_members(self, guild_id: int) -> tuple[Citizen, ...]:
        rows = await self._session.scalars(
            select(CitizenRow)
            .join(GuildRow, GuildRow.id == CitizenRow.guild_id)
            .where(GuildRow.discord_guild_id == Decimal(guild_id))
            .order_by(CitizenRow.id)
        )
        return tuple(_to_citizen(row) for row in rows)

    async def _get_or_create_guild(self, guild_id: int, now: datetime) -> int:
        discord_guild_id = Decimal(guild_id)
        local_id = await self._session.scalar(
            insert(GuildRow)
            .values(discord_guild_id=discord_guild_id, created_at=now)
            .on_conflict_do_nothing(constraint="uq_guilds_discord_guild_id")
            .returning(GuildRow.id)
        )
        if local_id is not None:
            return local_id
        return (
            await self._session.scalars(
                select(GuildRow.id).where(GuildRow.discord_guild_id == discord_guild_id)
            )
        ).one()

    async def get_or_create_verified(
        self, guild_id: int, user_id: int, now: datetime
    ) -> tuple[Citizen, bool]:
        local_guild_id = await self._get_or_create_guild(guild_id, now)
        discord_user_id = Decimal(user_id)
        row = await self._session.scalar(
            insert(CitizenRow)
            .values(
                guild_id=local_guild_id,
                discord_user_id=discord_user_id,
                status=CitizenStatus.VERIFIED.value,
                joined_at=now,
                verified_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_citizens_guild_user")
            .returning(CitizenRow)
        )
        if row is not None:
            return _to_citizen(row), True

        # Under READ COMMITTED a new statement sees the winner of a concurrent
        # insert. Lock the existing row until the service finishes any transition.
        existing = (
            await self._session.scalars(
                select(CitizenRow)
                .where(
                    CitizenRow.guild_id == local_guild_id,
                    CitizenRow.discord_user_id == discord_user_id,
                )
                .with_for_update()
            )
        ).one()
        return _to_citizen(existing), False

    async def update_citizen_status(self, citizen: Citizen) -> None:
        await self._session.execute(
            update(CitizenRow)
            .where(CitizenRow.id == citizen.id, CitizenRow.guild_id == citizen.guild_id)
            .values(status=citizen.status.value, verified_at=citizen.verified_at)
        )

    async def append_audit(self, event: CitizenAuditEvent) -> None:
        self._session.add(
            AuditEventRow(
                guild_id=event.citizen.guild_id,
                actor_discord_user_id=Decimal(event.actor_discord_user_id or event.citizen.discord_user_id),
                action=event.action.value,
                citizen_id=event.citizen.id,
                reason=event.reason,
                occurred_at=event.occurred_at,
                result="success",
            )
        )


class PostgresCitizenUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresCitizensRepository]:
        async with self._sessions.begin() as session:
            yield PostgresCitizensRepository(session)
