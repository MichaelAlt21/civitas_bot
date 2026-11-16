from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.app.law import ActKind, LawRevision

from .court_appeal_models import CourtAppealConfirmationRow
from .court_case_models import CourtActDecisionRow
from .government import PostgresGovernmentRepository
from .law_models import LawConfirmationRow, LawRevisionRow
from .models import GuildRow


def _to_law(row: LawRevisionRow, confirmation: LawConfirmationRow | None) -> LawRevision:
    return LawRevision(
        id=row.id, guild_id=row.guild_id, code=row.code, title=row.title, text=row.text, source_url=row.source_url,
        effective_at=row.effective_at.astimezone(UTC), proposer_citizen_id=row.proposer_citizen_id,
        proposed_at=row.proposed_at.astimezone(UTC),
        confirmed_by_citizen_id=confirmation.confirmed_by_citizen_id if confirmation else None,
        confirmed_at=confirmation.confirmed_at.astimezone(UTC) if confirmation else None,
        act_kind=ActKind(row.act_kind),
    )


def _law_query(*criteria: ColumnElement[bool]):
    return (
        select(LawRevisionRow, LawConfirmationRow)
        .select_from(LawRevisionRow)
        .join(GuildRow, GuildRow.id == LawRevisionRow.guild_id)
        .outerjoin(
            LawConfirmationRow,
            (LawConfirmationRow.guild_id == LawRevisionRow.guild_id)
            & (LawConfirmationRow.revision_id == LawRevisionRow.id),
        )
        .where(*criteria)
    )


def appeal_overturned(at: datetime | None = None) -> ColumnElement[bool]:
    overturn = select(CourtAppealConfirmationRow.case_id).where(
        CourtAppealConfirmationRow.guild_id == CourtActDecisionRow.guild_id,
        CourtAppealConfirmationRow.case_id == CourtActDecisionRow.case_id,
        CourtAppealConfirmationRow.outcome == "OVERTURNED",
    ).correlate(CourtActDecisionRow)
    if at is not None:
        overturn = overturn.where(CourtAppealConfirmationRow.confirmed_at <= at)
    return overturn.exists()


def _revision_in_force(at: datetime) -> ColumnElement[bool]:
    suspension = select(CourtActDecisionRow.case_id).where(
        CourtActDecisionRow.guild_id == LawRevisionRow.guild_id,
        CourtActDecisionRow.revision_id == LawRevisionRow.id,
        CourtActDecisionRow.outcome == "GRANTED",
        CourtActDecisionRow.decided_at <= at,
        ~appeal_overturned(at),
    ).correlate(LawRevisionRow)
    return ~suspension.exists()


class PostgresLawRepository(PostgresGovernmentRepository):
    async def find_law_revision(self, guild_id: int, request_key: str) -> LawRevision | None:
        result = (await self._session.execute(_law_query(
            LawRevisionRow.guild_id == guild_id, LawRevisionRow.request_key == request_key,
        ))).one_or_none()
        return _to_law(*result) if result is not None else None

    async def add_law_revision(self, revision: LawRevision, request_key: str) -> LawRevision:
        row = (await self._session.scalars(
            insert(LawRevisionRow)
            .values(
                guild_id=revision.guild_id, code=revision.code, title=revision.title, text=revision.text,
                source_url=revision.source_url, effective_at=revision.effective_at,
                proposer_citizen_id=revision.proposer_citizen_id, proposed_at=revision.proposed_at,
                request_key=request_key, act_kind=revision.act_kind.value,
            )
            .returning(LawRevisionRow)
        )).one()
        return _to_law(row, None)

    async def get_law_revision(self, discord_guild_id: int, revision_id: int) -> LawRevision | None:
        result = (await self._session.execute(_law_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), LawRevisionRow.id == revision_id,
        ))).one_or_none()
        return _to_law(*result) if result is not None else None

    async def confirm_law_revision(
        self, revision: LawRevision, confirmer_citizen_id: int, *, at: datetime,
    ) -> LawRevision:
        await self._session.execute(
            insert(LawConfirmationRow).values(
                guild_id=revision.guild_id, revision_id=revision.id, code=revision.code,
                effective_at=revision.effective_at, proposer_citizen_id=revision.proposer_citizen_id,
                proposed_at=revision.proposed_at, confirmed_by_citizen_id=confirmer_citizen_id, confirmed_at=at,
                act_kind=revision.act_kind.value,
            )
        )
        return replace(revision, confirmed_by_citizen_id=confirmer_citizen_id, confirmed_at=at.astimezone(UTC))

    async def has_confirmed_law_slot(
        self, guild_id: int, code: str, effective_at: datetime, *, act_kind: ActKind = ActKind.LAW,
    ) -> bool:
        slot = select(LawConfirmationRow.revision_id).where(
            LawConfirmationRow.guild_id == guild_id, LawConfirmationRow.code == code,
            LawConfirmationRow.act_kind == act_kind.value, LawConfirmationRow.effective_at == effective_at,
        )
        return bool(await self._session.scalar(select(slot.exists())))

    async def get_effective_law(
        self, discord_guild_id: int, code: str, at: datetime, *, act_kind: ActKind = ActKind.LAW,
    ) -> LawRevision | None:
        result = (await self._session.execute(_law_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id),
            LawConfirmationRow.code == code, LawConfirmationRow.act_kind == act_kind.value,
            LawConfirmationRow.effective_at <= at, _revision_in_force(at),
        ).order_by(LawConfirmationRow.effective_at.desc()).limit(1))).one_or_none()
        return _to_law(*result) if result is not None else None

    async def list_effective_laws(
        self, discord_guild_id: int, at: datetime, *, limit: int, offset: int, act_kind: ActKind = ActKind.LAW,
    ) -> tuple[LawRevision, ...]:
        rows = await self._session.execute(_law_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), LawConfirmationRow.act_kind == act_kind.value,
            LawConfirmationRow.effective_at <= at, _revision_in_force(at),
        ).distinct(LawConfirmationRow.code)
            .order_by(LawConfirmationRow.code, LawConfirmationRow.effective_at.desc())
            .limit(limit).offset(offset))
        return tuple(_to_law(row, confirmation) for row, confirmation in rows)


class PostgresLawUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresLawRepository]:
        async with self._sessions.begin() as session:
            yield PostgresLawRepository(session)
