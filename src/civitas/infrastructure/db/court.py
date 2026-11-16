from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.app.court import CourtNomination

from .court_models import CourtAppointmentRow, CourtApprovalRow, CourtNominationRow
from .government import PostgresGovernmentRepository
from .models import CitizenRow, GuildRow


class PostgresCourtRepository(PostgresGovernmentRepository):
    async def _select_court_nomination(self, *criteria: ColumnElement[bool]) -> CourtNomination | None:
        approval_count = (
            select(func.count()).select_from(CourtApprovalRow)
            .where(
                CourtApprovalRow.guild_id == CourtNominationRow.guild_id,
                CourtApprovalRow.nomination_id == CourtNominationRow.id,
            )
            .correlate(CourtNominationRow)
            .scalar_subquery()
        )
        result = (
            await self._session.execute(
                select(CourtNominationRow, CitizenRow.discord_user_id, approval_count, CourtAppointmentRow)
                .select_from(CourtNominationRow)
                .join(GuildRow, GuildRow.id == CourtNominationRow.guild_id)
                .join(
                    CitizenRow,
                    (CitizenRow.guild_id == CourtNominationRow.guild_id)
                    & (CitizenRow.id == CourtNominationRow.candidate_citizen_id),
                )
                .outerjoin(
                    CourtAppointmentRow,
                    (CourtAppointmentRow.guild_id == CourtNominationRow.guild_id)
                    & (CourtAppointmentRow.nomination_id == CourtNominationRow.id),
                )
                .where(*criteria)
            )
        ).one_or_none()
        if result is None:
            return None
        row, candidate_user_id, approval_total, appointment = result
        return CourtNomination(
            id=row.id,
            guild_id=row.guild_id,
            president_term_id=row.president_term_id,
            proposer_citizen_id=row.proposer_citizen_id,
            candidate_citizen_id=row.candidate_citizen_id,
            candidate_user_id=int(candidate_user_id),
            reason=row.reason,
            proposed_at=row.proposed_at.astimezone(UTC),
            approval_count=approval_total,
            starts_at=appointment.starts_at.astimezone(UTC) if appointment is not None else None,
            ends_at=appointment.ends_at.astimezone(UTC) if appointment is not None else None,
        )

    async def get_court_nomination(self, guild_id: int, nomination_id: int) -> CourtNomination | None:
        return await self._select_court_nomination(
            CourtNominationRow.guild_id == guild_id, CourtNominationRow.id == nomination_id,
        )

    async def get_court_nomination_for_discord(
        self, discord_guild_id: int, nomination_id: int,
    ) -> CourtNomination | None:
        return await self._select_court_nomination(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), CourtNominationRow.id == nomination_id,
        )

    async def find_court_nomination(self, guild_id: int, request_key: str) -> CourtNomination | None:
        return await self._select_court_nomination(
            CourtNominationRow.guild_id == guild_id, CourtNominationRow.request_key == request_key,
        )

    async def add_court_nomination(self, nomination: CourtNomination, request_key: str) -> CourtNomination:
        nomination_id = (
            await self._session.scalars(
                insert(CourtNominationRow)
                .values(
                    guild_id=nomination.guild_id,
                    president_term_id=nomination.president_term_id,
                    proposer_citizen_id=nomination.proposer_citizen_id,
                    candidate_citizen_id=nomination.candidate_citizen_id,
                    reason=nomination.reason,
                    proposed_at=nomination.proposed_at,
                    request_key=request_key,
                )
                .returning(CourtNominationRow.id)
            )
        ).one()
        return await self._require_court_nomination(nomination.guild_id, nomination_id)

    async def add_court_approval(
        self, guild_id: int, nomination_id: int, deputy_citizen_id: int, *, reason: str, at: datetime,
    ) -> bool:
        new_signer = await self._session.scalar(
            pg_insert(CourtApprovalRow)
            .values(
                guild_id=guild_id,
                nomination_id=nomination_id,
                deputy_citizen_id=deputy_citizen_id,
                reason=reason,
                approved_at=at,
            )
            .on_conflict_do_nothing()
            .returning(CourtApprovalRow.deputy_citizen_id)
        )
        return new_signer is not None

    async def activate_court_nomination(
        self, guild_id: int, nomination_id: int, *, starts_at: datetime, ends_at: datetime,
    ) -> CourtNomination:
        await self._session.execute(
            insert(CourtAppointmentRow)
            .values(guild_id=guild_id, nomination_id=nomination_id, starts_at=starts_at, ends_at=ends_at)
        )
        return await self._require_court_nomination(guild_id, nomination_id)

    async def _require_court_nomination(self, guild_id: int, nomination_id: int) -> CourtNomination:
        nomination = await self.get_court_nomination(guild_id, nomination_id)
        if nomination is None:
            raise RuntimeError("The persisted court nomination is missing")
        return nomination

    async def get_active_court_nomination(self, discord_guild_id: int, at: datetime) -> CourtNomination | None:
        return await self._select_court_nomination(
            GuildRow.discord_guild_id == Decimal(discord_guild_id),
            CourtAppointmentRow.starts_at <= at,
            CourtAppointmentRow.ends_at > at,
        )


class PostgresCourtUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresCourtRepository]:
        async with self._sessions.begin() as session:
            yield PostgresCourtRepository(session)
