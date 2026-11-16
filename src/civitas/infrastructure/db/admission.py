from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .admission_models import AdmissionAuditRow, AdmissionRosterRow, AdmittedUserRow


class PostgresAdmissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def initialize(self, guild_id: int, initial_user_ids: Iterable[int]) -> None:
        discord_guild_id = Decimal(guild_id)
        created_roster = await self._session.scalar(
            insert(AdmissionRosterRow)
            .values(discord_guild_id=discord_guild_id)
            .on_conflict_do_nothing()
            .returning(AdmissionRosterRow.discord_guild_id)
        )
        if created_roster is None:
            return
        rows = [
            {"discord_guild_id": discord_guild_id, "discord_user_id": Decimal(user_id)}
            for user_id in sorted(set(initial_user_ids))
        ]
        if rows:
            await self._session.execute(insert(AdmittedUserRow), rows)

    async def is_allowed(self, guild_id: int, user_id: int) -> bool:
        return await self._session.scalar(
            select(AdmittedUserRow.discord_user_id).where(
                AdmittedUserRow.discord_guild_id == Decimal(guild_id),
                AdmittedUserRow.discord_user_id == Decimal(user_id),
            )
        ) is not None

    async def list_allowed(self, guild_id: int, *, limit: int, offset: int) -> tuple[int, ...]:
        ids = await self._session.scalars(
            select(AdmittedUserRow.discord_user_id)
            .where(AdmittedUserRow.discord_guild_id == Decimal(guild_id))
            .order_by(AdmittedUserRow.discord_user_id)
            .limit(limit)
            .offset(offset)
        )
        return tuple(int(user_id) for user_id in ids)

    async def set_allowed(
        self, guild_id: int, user_id: int, *, allowed: bool, actor_user_id: int, reason: str,
    ) -> bool:
        discord_guild_id = Decimal(guild_id)
        discord_user_id = Decimal(user_id)
        # The roster row orders concurrent edits even when the individual member row is absent.
        (
            await self._session.scalars(
                select(AdmissionRosterRow)
                .where(AdmissionRosterRow.discord_guild_id == discord_guild_id)
                .with_for_update(key_share=True)
            )
        ).one()
        if allowed:
            changed_user = await self._session.scalar(
                insert(AdmittedUserRow)
                .values(discord_guild_id=discord_guild_id, discord_user_id=discord_user_id)
                .on_conflict_do_nothing()
                .returning(AdmittedUserRow.discord_user_id)
            )
        else:
            changed_user = await self._session.scalar(
                delete(AdmittedUserRow)
                .where(
                    AdmittedUserRow.discord_guild_id == discord_guild_id,
                    AdmittedUserRow.discord_user_id == discord_user_id,
                )
                .returning(AdmittedUserRow.discord_user_id)
            )
        if changed_user is None:
            return False
        self._session.add(AdmissionAuditRow(
            discord_guild_id=discord_guild_id,
            actor_discord_user_id=Decimal(actor_user_id),
            target_discord_user_id=discord_user_id,
            allowed=allowed,
            reason=reason,
            occurred_at=datetime.now(UTC),
        ))
        return True


class PostgresAdmissionUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresAdmissionRepository]:
        async with self._sessions.begin() as session:
            yield PostgresAdmissionRepository(session)
