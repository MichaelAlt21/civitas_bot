from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from civitas.app.presentation import GuildPresentation, PresentationBinding, PresentationError, PresentationErrorReason

from .presentation_models import GuildPresentationRow, PresentationAuditRow

_ROLE_FIELDS = ("citizen_role_id", "president_role_id", "parliament_role_id", "custody_role_id")


def _to_decimal(value: int | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _to_int(value: Decimal | None) -> int | None:
    return int(value) if value is not None else None


class PostgresPresentationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def initialize(self, guild_id: int, initial: GuildPresentation) -> None:
        await self._session.execute(
            insert(GuildPresentationRow)
            .values(
                discord_guild_id=Decimal(guild_id),
                citizen_role_id=_to_decimal(initial.citizen_role_id),
                president_role_id=_to_decimal(initial.president_role_id),
                parliament_role_id=_to_decimal(initial.parliament_role_id),
                report_channel_id=_to_decimal(initial.report_channel_id),
                custody_role_id=_to_decimal(initial.custody_role_id),
            )
            .on_conflict_do_nothing()
        )

    async def get_presentation(self, guild_id: int) -> GuildPresentation:
        row = await self._session.get(GuildPresentationRow, Decimal(guild_id))
        if row is None:
            return GuildPresentation()
        return GuildPresentation(
            citizen_role_id=_to_int(row.citizen_role_id),
            president_role_id=_to_int(row.president_role_id),
            parliament_role_id=_to_int(row.parliament_role_id),
            report_channel_id=_to_int(row.report_channel_id),
            custody_role_id=_to_int(row.custody_role_id),
        )

    async def set_binding(
        self, guild_id: int, binding: PresentationBinding, value: int | None, *, actor_user_id: int,
    ) -> bool:
        row = (
            await self._session.scalars(
                select(GuildPresentationRow)
                .where(GuildPresentationRow.discord_guild_id == Decimal(guild_id))
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        ).one()
        field = binding.value
        current = getattr(row, field)
        updated = _to_decimal(value)
        if current == updated:
            return False
        if updated is not None and field in _ROLE_FIELDS and any(
            updated == getattr(row, role_field) for role_field in _ROLE_FIELDS if role_field != field
        ):
            raise PresentationError(PresentationErrorReason.DUPLICATE_ROLE)
        setattr(row, field, updated)
        self._session.add(PresentationAuditRow(
            discord_guild_id=row.discord_guild_id,
            actor_discord_user_id=Decimal(actor_user_id),
            binding=field,
            previous_discord_id=current,
            new_discord_id=updated,
            occurred_at=datetime.now(UTC),
        ))
        return True


class PostgresPresentationUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresPresentationRepository]:
        async with self._sessions.begin() as session:
            yield PostgresPresentationRepository(session)
