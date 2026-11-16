from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from civitas.app.court_appeals import AppealConfirmation, AppealOutcome, AppealTally, CourtAppeal

from .court_appeal_models import (
    CourtAppealConfirmationRow, CourtAppealOpeningRow, CourtAppealRow, CourtAppealTallyRow,
)
from .court_cases import PostgresCourtCaseRepository
from .government_models import ElectionPollSourceRow, ImpeachmentProtocolRow
from .models import GuildRow


def _appeal_query(*criteria: ColumnElement[bool]):
    first = aliased(CourtAppealConfirmationRow)
    second = aliased(CourtAppealConfirmationRow)
    return (
        select(CourtAppealRow, CourtAppealOpeningRow, first, second, CourtAppealTallyRow)
        .select_from(CourtAppealRow)
        .join(GuildRow, GuildRow.id == CourtAppealRow.guild_id)
        .outerjoin(
            CourtAppealOpeningRow,
            (CourtAppealOpeningRow.guild_id == CourtAppealRow.guild_id)
            & (CourtAppealOpeningRow.case_id == CourtAppealRow.case_id),
        )
        .outerjoin(
            first,
            (first.guild_id == CourtAppealRow.guild_id) & (first.case_id == CourtAppealRow.case_id)
            & (first.round_number == 1),
        )
        .outerjoin(
            second,
            (second.guild_id == CourtAppealRow.guild_id) & (second.case_id == CourtAppealRow.case_id)
            & (second.round_number == 2),
        )
        .outerjoin(
            CourtAppealTallyRow,
            (CourtAppealTallyRow.guild_id == CourtAppealRow.guild_id)
            & (CourtAppealTallyRow.case_id == CourtAppealRow.case_id)
            & (CourtAppealTallyRow.round_number == func.coalesce(second.round_number, first.round_number)),
        )
        .where(*criteria)
    )


def _to_appeal(
    row: CourtAppealRow,
    ballot: CourtAppealOpeningRow | None,
    first: CourtAppealConfirmationRow | None,
    second: CourtAppealConfirmationRow | None,
    tally: CourtAppealTallyRow | None,
) -> CourtAppeal:
    extension = first.extended_until if first else None
    decision = second if extension is not None else first
    eligible_user_ids = tuple(map(int, ballot.eligible_user_ids)) if ballot else ()
    closes_at = extension if extension is not None else ballot.closes_at if ballot else None
    return CourtAppeal(
        guild_id=row.guild_id,
        case_id=row.case_id,
        appellant_citizen_id=row.appellant_citizen_id,
        filed_at=row.filed_at.astimezone(UTC),
        opened_by_citizen_id=ballot.opened_by_citizen_id if ballot else None,
        opened_at=ballot.opened_at.astimezone(UTC) if ballot else None,
        source_url=ballot.source_url if ballot else None,
        eligible_user_ids=eligible_user_ids,
        eligible_count=len(eligible_user_ids),
        quorum=ballot.quorum if ballot else None,
        closes_at=closes_at.astimezone(UTC) if closes_at else None,
        round_number=2 if extension is not None else 1,
        outcome=AppealOutcome(decision.outcome) if decision else None,
        resolved_at=decision.confirmed_at.astimezone(UTC) if decision else None,
        participants=tally.participants if tally else None,
        votes_to_cancel=tally.votes_to_cancel if tally else None,
    )


def _utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(UTC) if dt is not None else None


class PostgresCourtAppealRepository(PostgresCourtCaseRepository):
    async def get_court_appeal(self, discord_guild_id: int, case_id: int) -> CourtAppeal | None:
        row = (await self._session.execute(_appeal_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), CourtAppealRow.case_id == case_id,
        ))).one_or_none()
        return _to_appeal(*row) if row is not None else None

    async def add_court_appeal(self, appeal: CourtAppeal) -> CourtAppeal:
        await self._session.execute(insert(CourtAppealRow).values(
            guild_id=appeal.guild_id, case_id=appeal.case_id,
            appellant_citizen_id=appeal.appellant_citizen_id, filed_at=appeal.filed_at,
        ))
        return await self._require_appeal(appeal.guild_id, appeal.case_id)

    async def open_court_appeal(self, appeal: CourtAppeal) -> CourtAppeal:
        await self._session.execute(insert(CourtAppealOpeningRow).values(
            guild_id=appeal.guild_id, case_id=appeal.case_id,
            opened_by_citizen_id=appeal.opened_by_citizen_id, opened_at=appeal.opened_at,
            source_url=appeal.source_url, eligible_user_ids=[Decimal(user_id) for user_id in appeal.eligible_user_ids],
            quorum=appeal.quorum, closes_at=appeal.closes_at,
        ))
        return await self._require_appeal(appeal.guild_id, appeal.case_id)

    async def get_appeal_tally(self, local_guild_id: int, case_id: int, round_number: int) -> AppealTally | None:
        row = await self._session.get(CourtAppealTallyRow, (local_guild_id, case_id, round_number))
        if row is None:
            return None
        return AppealTally(
            guild_id=row.guild_id, case_id=row.case_id, round_number=row.round_number,
            participants=row.participants, votes_to_cancel=row.votes_to_cancel,
            recorded_by_citizen_id=row.recorded_by_citizen_id, recorded_at=row.recorded_at.astimezone(UTC),
        )

    async def add_appeal_tally(self, tally: AppealTally) -> AppealTally:
        await self._session.execute(insert(CourtAppealTallyRow).values(
            guild_id=tally.guild_id, case_id=tally.case_id, round_number=tally.round_number,
            participants=tally.participants, votes_to_cancel=tally.votes_to_cancel,
            recorded_by_citizen_id=tally.recorded_by_citizen_id, recorded_at=tally.recorded_at,
        ))
        return tally

    async def get_appeal_confirmation(
        self, local_guild_id: int, case_id: int, round_number: int,
    ) -> AppealConfirmation | None:
        row = await self._session.get(CourtAppealConfirmationRow, (local_guild_id, case_id, round_number))
        if row is None:
            return None
        return AppealConfirmation(
            guild_id=row.guild_id, case_id=row.case_id, round_number=row.round_number,
            confirmed_by_citizen_id=row.confirmed_by_citizen_id, confirmed_at=row.confirmed_at.astimezone(UTC),
            outcome=AppealOutcome(row.outcome), extended_until=_utc(row.extended_until),
        )

    async def add_appeal_confirmation(self, confirmation: AppealConfirmation) -> CourtAppeal:
        await self._session.execute(insert(CourtAppealConfirmationRow).values(
            guild_id=confirmation.guild_id, case_id=confirmation.case_id, round_number=confirmation.round_number,
            confirmed_by_citizen_id=confirmation.confirmed_by_citizen_id, confirmed_at=confirmation.confirmed_at,
            outcome=confirmation.outcome.value, extended_until=confirmation.extended_until,
        ))
        return await self._require_appeal(confirmation.guild_id, confirmation.case_id)

    async def appeal_source_in_use(self, local_guild_id: int, source_url: str) -> bool:
        appeal = select(CourtAppealOpeningRow.case_id).where(
            CourtAppealOpeningRow.guild_id == local_guild_id, CourtAppealOpeningRow.source_url == source_url,
        ).exists()
        election = select(ElectionPollSourceRow.election_id).where(
            ElectionPollSourceRow.guild_id == local_guild_id, ElectionPollSourceRow.source_url == source_url,
        ).exists()
        impeachment = select(ImpeachmentProtocolRow.impeachment_id).where(
            ImpeachmentProtocolRow.guild_id == local_guild_id,
            func.civitas_election_source_key(ImpeachmentProtocolRow.source_url) == source_url,
        ).exists()
        return bool(await self._session.scalar(select(or_(appeal, election, impeachment))))

    async def _require_appeal(self, local_guild_id: int, case_id: int) -> CourtAppeal:
        row = (await self._session.execute(_appeal_query(
            CourtAppealRow.guild_id == local_guild_id, CourtAppealRow.case_id == case_id,
        ))).one()
        return _to_appeal(*row)


class PostgresCourtAppealUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresCourtAppealRepository]:
        async with self._sessions.begin() as session:
            yield PostgresCourtAppealRepository(session)
