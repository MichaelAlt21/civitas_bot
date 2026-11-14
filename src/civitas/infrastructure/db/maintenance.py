from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .maintenance_models import EconomyMaintenanceRow
from .models import GuildRow
from .receipts import PostgresReceiptRepository


class PostgresMaintenanceRepository(PostgresReceiptRepository):
    async def get_economy_read_only(self, discord_guild_id: int) -> bool:
        return bool(await self._session.scalar(
            select(EconomyMaintenanceRow.read_only)
            .join(GuildRow, GuildRow.id == EconomyMaintenanceRow.guild_id)
            .where(GuildRow.discord_guild_id == Decimal(discord_guild_id))
        ))

    async def set_economy_read_only(
        self,
        discord_guild_id: int,
        read_only: bool,
        *,
        actor_discord_user_id: int,
        reason: str,
        at: datetime,
    ) -> bool:
        guild_id = await self.ensure_guild(discord_guild_id)
        changed = await self._session.scalar(select(func.civitas_set_economy_read_only(
            guild_id, read_only, Decimal(actor_discord_user_id), reason, at,
        )))
        return bool(changed)


class PostgresMaintenanceUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresMaintenanceRepository]:
        async with self._sessions.begin() as session:
            yield PostgresMaintenanceRepository(session)
