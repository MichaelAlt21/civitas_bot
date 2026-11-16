from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.app.prosecutor import ProsecutorAction, ProsecutorMotion

from .government import PostgresGovernmentRepository
from .models import CitizenRow, GuildRow
from .prosecutor_models import ProsecutorApprovalRow, ProsecutorMotionRow, ProsecutorResolutionRow


class PostgresProsecutorRepository(PostgresGovernmentRepository):
    async def _select_motion(self, *criteria: ColumnElement[bool]) -> ProsecutorMotion | None:
        approval_count = (
            select(func.count()).select_from(ProsecutorApprovalRow)
            .where(
                ProsecutorApprovalRow.guild_id == ProsecutorMotionRow.guild_id,
                ProsecutorApprovalRow.motion_id == ProsecutorMotionRow.id,
            )
            .correlate(ProsecutorMotionRow)
            .scalar_subquery()
        )
        result = (
            await self._session.execute(
                select(ProsecutorMotionRow, CitizenRow.discord_user_id, approval_count, ProsecutorResolutionRow.applied_at)
                .select_from(ProsecutorMotionRow)
                .join(GuildRow, GuildRow.id == ProsecutorMotionRow.guild_id)
                .join(
                    CitizenRow,
                    (CitizenRow.guild_id == ProsecutorMotionRow.guild_id)
                    & (CitizenRow.id == ProsecutorMotionRow.subject_citizen_id),
                )
                .outerjoin(
                    ProsecutorResolutionRow,
                    (ProsecutorResolutionRow.guild_id == ProsecutorMotionRow.guild_id)
                    & (ProsecutorResolutionRow.motion_id == ProsecutorMotionRow.id),
                )
                .where(*criteria)
            )
        ).one_or_none()
        if result is None:
            return None
        row, subject_user_id, approval_total, applied_at = result
        return ProsecutorMotion(
            id=row.id,
            guild_id=row.guild_id,
            action=ProsecutorAction(row.action),
            president_term_id=row.president_term_id,
            proposer_citizen_id=row.proposer_citizen_id,
            subject_citizen_id=row.subject_citizen_id,
            subject_user_id=int(subject_user_id),
            appointment_id=row.appointment_id,
            reason=row.reason,
            proposed_at=row.proposed_at.astimezone(UTC),
            approval_count=approval_total,
            applied_at=applied_at.astimezone(UTC) if applied_at is not None else None,
        )

    async def find_prosecutor_motion(self, guild_id: int, key: str) -> ProsecutorMotion | None:
        return await self._select_motion(ProsecutorMotionRow.guild_id == guild_id, ProsecutorMotionRow.request_key == key)

    async def add_prosecutor_motion(self, motion: ProsecutorMotion, key: str) -> ProsecutorMotion:
        motion_id = (
            await self._session.scalars(
                insert(ProsecutorMotionRow)
                .values(
                    guild_id=motion.guild_id,
                    action=motion.action.value,
                    president_term_id=motion.president_term_id,
                    proposer_citizen_id=motion.proposer_citizen_id,
                    subject_citizen_id=motion.subject_citizen_id,
                    appointment_id=motion.appointment_id,
                    reason=motion.reason,
                    proposed_at=motion.proposed_at,
                    request_key=key,
                )
                .returning(ProsecutorMotionRow.id)
            )
        ).one()
        return await self._require_motion(motion.guild_id, motion_id)

    async def get_prosecutor_motion(self, discord_guild_id: int, motion_id: int) -> ProsecutorMotion | None:
        return await self._select_motion(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), ProsecutorMotionRow.id == motion_id,
        )

    async def get_active_prosecutor(self, discord_guild_id: int) -> ProsecutorMotion | None:
        latest_motion = (
            select(ProsecutorResolutionRow.motion_id)
            .where(ProsecutorResolutionRow.guild_id == ProsecutorMotionRow.guild_id)
            .order_by(ProsecutorResolutionRow.id.desc())
            .limit(1)
            .correlate(ProsecutorMotionRow)
            .scalar_subquery()
        )
        return await self._select_motion(
            GuildRow.discord_guild_id == Decimal(discord_guild_id),
            ProsecutorMotionRow.id == latest_motion,
            ProsecutorMotionRow.action == ProsecutorAction.APPOINT.value,
        )

    async def add_prosecutor_approval(
        self, guild_id: int, motion_id: int, senator_citizen_id: int, *, reason: str, at: datetime,
    ) -> bool:
        approval = await self._session.scalar(
            pg_insert(ProsecutorApprovalRow)
            .values(
                guild_id=guild_id, motion_id=motion_id, senator_citizen_id=senator_citizen_id,
                reason=reason, approved_at=at,
            )
            .on_conflict_do_nothing(constraint="uq_prosecutor_approvals_signer")
            .returning(ProsecutorApprovalRow.id)
        )
        return approval is not None

    async def apply_prosecutor_motion(self, guild_id: int, motion_id: int, *, at: datetime) -> ProsecutorMotion:
        await self._session.execute(
            insert(ProsecutorResolutionRow).values(guild_id=guild_id, motion_id=motion_id, applied_at=at)
        )
        return await self._require_motion(guild_id, motion_id)

    async def _require_motion(self, guild_id: int, motion_id: int) -> ProsecutorMotion:
        motion = await self._select_motion(ProsecutorMotionRow.guild_id == guild_id, ProsecutorMotionRow.id == motion_id)
        if motion is None:
            raise RuntimeError("The persisted prosecutor motion is missing")
        return motion


class PostgresProsecutorUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresProsecutorRepository]:
        async with self._sessions.begin() as session:
            yield PostgresProsecutorRepository(session)
