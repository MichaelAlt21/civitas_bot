from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, insert, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from civitas.app.court_cases import (
    AdministrativeDecision, AdministrativeOutcome, CaseKind, CourtCase, CourtCaseError, CourtCaseErrorReason,
    CourtFineStatus, MeritsDecision, MeritsOutcome,
)
from civitas.app.law import ActKind
from civitas.domain.court_sanctions import CourtSentence, SanctionTerm
from civitas.domain.ledger import LedgerError, LedgerErrorReason, TransactionKind

from .court import PostgresCourtRepository
from .court_appeal_models import CourtAppealConfirmationRow
from .court_case_models import CourtActDecisionRow, CourtCaseRow, CourtHearingRow
from .law import PostgresLawRepository, appeal_overturned
from .law_models import LawRevisionRow
from .ledger import PostgresLedgerRepository, active_custody_at
from .ledger_models import FinancialTransactionRow
from .models import CitizenRow, GuildRow
from .prosecutor import PostgresProsecutorRepository


def _case_query(*criteria: ColumnElement[bool]):
    filer = aliased(CitizenRow)
    respondent = aliased(CitizenRow)
    latest_hearing = (
        select(CourtHearingRow.hearing_at, CourtHearingRow.judge_citizen_id)
        .where(CourtHearingRow.guild_id == CourtCaseRow.guild_id, CourtHearingRow.case_id == CourtCaseRow.id)
        .order_by(CourtHearingRow.id.desc())
        .limit(1)
        .correlate(CourtCaseRow)
        .lateral()
    )
    return (
        select(
            CourtCaseRow, filer.discord_user_id, respondent.discord_user_id,
            latest_hearing.c.hearing_at, latest_hearing.c.judge_citizen_id, LawRevisionRow.act_kind,
            CourtActDecisionRow.outcome,
        )
        .select_from(CourtCaseRow)
        .join(GuildRow, GuildRow.id == CourtCaseRow.guild_id)
        .join(filer, (filer.guild_id == CourtCaseRow.guild_id) & (filer.id == CourtCaseRow.filer_citizen_id))
        .outerjoin(
            respondent,
            (respondent.guild_id == CourtCaseRow.guild_id) & (respondent.id == CourtCaseRow.respondent_citizen_id),
        )
        .outerjoin(latest_hearing, true())
        .outerjoin(
            LawRevisionRow,
            (LawRevisionRow.guild_id == CourtCaseRow.guild_id) & (LawRevisionRow.id == CourtCaseRow.law_revision_id),
        )
        .outerjoin(
            CourtActDecisionRow,
            (CourtActDecisionRow.guild_id == CourtCaseRow.guild_id) & (CourtActDecisionRow.case_id == CourtCaseRow.id),
        )
        .where(*criteria)
    )


def _to_case(
    row: CourtCaseRow,
    filer_user_id: Decimal,
    respondent_user_id: Decimal | None,
    hearing_at: datetime | None,
    judge_citizen_id: int | None,
    act_kind: str | None,
    administrative_outcome: str | None,
) -> CourtCase:
    return CourtCase(
        id=row.id,
        guild_id=row.guild_id,
        kind=CaseKind(row.kind),
        filer_citizen_id=row.filer_citizen_id,
        filer_user_id=int(filer_user_id),
        respondent_citizen_id=row.respondent_citizen_id,
        respondent_user_id=int(respondent_user_id) if respondent_user_id is not None else None,
        for_state=row.for_state,
        against_state=row.against_state,
        law_revision_id=row.law_revision_id,
        occurred_at=row.occurred_at.astimezone(UTC) if row.occurred_at is not None else None,
        statement=row.statement,
        filed_at=row.filed_at.astimezone(UTC),
        hearing_at=hearing_at.astimezone(UTC) if hearing_at is not None else None,
        scheduled_by_citizen_id=judge_citizen_id,
        act_kind=ActKind(act_kind) if act_kind is not None else None,
        administrative_outcome=(
            AdministrativeOutcome(administrative_outcome)
            if administrative_outcome and row.kind == CaseKind.ADMINISTRATIVE.value else None
        ),
        merits_outcome=(
            MeritsOutcome(administrative_outcome)
            if administrative_outcome and row.kind != CaseKind.ADMINISTRATIVE.value else None
        ),
    )


def _decision_query(*criteria: ColumnElement[bool]):
    return (
        select(CourtActDecisionRow, LawRevisionRow.act_kind)
        .select_from(CourtActDecisionRow)
        .join(GuildRow, GuildRow.id == CourtActDecisionRow.guild_id)
        .join(
            LawRevisionRow,
            (LawRevisionRow.guild_id == CourtActDecisionRow.guild_id)
            & (LawRevisionRow.id == CourtActDecisionRow.revision_id),
        )
        .where(CourtActDecisionRow.case_kind == CaseKind.ADMINISTRATIVE.value, *criteria)
    )


def _to_decision(row: CourtActDecisionRow, act_kind: str) -> AdministrativeDecision:
    return AdministrativeDecision(
        guild_id=row.guild_id, case_id=row.case_id, revision_id=row.revision_id, act_kind=ActKind(act_kind),
        outcome=AdministrativeOutcome(row.outcome), reason=row.reason, judge_citizen_id=row.judge_citizen_id,
        decided_at=row.decided_at.astimezone(UTC),
    )


class PostgresCourtCaseRepository(PostgresCourtRepository, PostgresLawRepository, PostgresProsecutorRepository):
    async def get_fine_status(self, guild_id: int, case_id: int, respondent_id: int) -> CourtFineStatus | None:
        def posted(kind: TransactionKind) -> ColumnElement[bool]:
            return select(FinancialTransactionRow.id).where(
                FinancialTransactionRow.guild_id == guild_id,
                FinancialTransactionRow.kind == kind.value,
                FinancialTransactionRow.idempotency_key == f"{kind.value.lower()}:{case_id}",
            ).exists()

        overturned = select(CourtAppealConfirmationRow.case_id).where(
            CourtAppealConfirmationRow.guild_id == guild_id, CourtAppealConfirmationRow.case_id == case_id,
            CourtAppealConfirmationRow.outcome == "OVERTURNED",
        ).exists()
        row = (await self._session.execute(
            select(posted(TransactionKind.COURT_FINE_HOLD), posted(TransactionKind.COURT_FINE_REFUND),
                   posted(TransactionKind.COURT_FINE_SETTLE), overturned)
            .select_from(CourtActDecisionRow)
            .join(CourtCaseRow, CourtCaseRow.id == CourtActDecisionRow.case_id)
            .where(CourtActDecisionRow.guild_id == guild_id, CourtActDecisionRow.case_id == case_id,
                   CourtCaseRow.respondent_citizen_id == respondent_id, CourtActDecisionRow.fine_amount.is_not(None)),
        )).one_or_none()
        if row is None:
            return None
        held, refunded, settled, cancelled = row
        if refunded:
            return CourtFineStatus.REFUNDED
        if settled:
            return CourtFineStatus.SETTLED
        if cancelled:
            return CourtFineStatus.REFUND_PENDING if held else CourtFineStatus.CANCELLED
        return CourtFineStatus.HELD if held else CourtFineStatus.PENDING

    async def custody_role_assignments(self, guild_id: int, *, at: datetime) -> dict[int, bool]:
        rows = await self._session.execute(
            select(CitizenRow.discord_user_id, func.bool_or(active_custody_at(at)))
            .select_from(CourtActDecisionRow)
            .join(CourtCaseRow, CourtCaseRow.id == CourtActDecisionRow.case_id)
            .join(CitizenRow, CitizenRow.id == CourtCaseRow.respondent_citizen_id)
            .join(GuildRow, GuildRow.id == CourtActDecisionRow.guild_id)
            .where(GuildRow.discord_guild_id == Decimal(guild_id), CourtActDecisionRow.custody_hours.is_not(None))
            .group_by(CitizenRow.discord_user_id),
        )
        return {int(user_id): present for user_id, present in rows}
    async def find_case(self, guild_id: int, request_key: str) -> CourtCase | None:
        result = (await self._session.execute(_case_query(
            CourtCaseRow.guild_id == guild_id, CourtCaseRow.request_key == request_key,
        ))).one_or_none()
        return _to_case(*result) if result is not None else None

    async def add_case(self, case: CourtCase, request_key: str) -> CourtCase:
        case_id = (
            await self._session.scalars(
                insert(CourtCaseRow)
                .values(
                    guild_id=case.guild_id,
                    kind=case.kind.value,
                    filer_citizen_id=case.filer_citizen_id,
                    respondent_citizen_id=case.respondent_citizen_id,
                    for_state=case.for_state,
                    against_state=case.against_state,
                    law_revision_id=case.law_revision_id,
                    occurred_at=case.occurred_at,
                    statement=case.statement,
                    filed_at=case.filed_at,
                    request_key=request_key,
                )
                .returning(CourtCaseRow.id)
            )
        ).one()
        return await self._require_case(case.guild_id, case_id)

    async def get_case(self, discord_guild_id: int, case_id: int) -> CourtCase | None:
        result = (await self._session.execute(_case_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), CourtCaseRow.id == case_id,
        ))).one_or_none()
        return _to_case(*result) if result is not None else None

    async def list_cases(self, discord_guild_id: int, *, limit: int, offset: int) -> tuple[CourtCase, ...]:
        rows = await self._session.execute(_case_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id),
        ).order_by(CourtCaseRow.id.desc()).limit(limit).offset(offset))
        return tuple(_to_case(*row) for row in rows)

    async def get_act_decision(self, discord_guild_id: int, case_id: int) -> AdministrativeDecision | None:
        result = (await self._session.execute(_decision_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), CourtActDecisionRow.case_id == case_id,
        ))).one_or_none()
        return _to_decision(*result) if result is not None else None

    async def find_act_decision(self, local_guild_id: int, request_key: str) -> AdministrativeDecision | None:
        result = (await self._session.execute(_decision_query(
            CourtActDecisionRow.guild_id == local_guild_id, CourtActDecisionRow.request_key == request_key,
        ))).one_or_none()
        return _to_decision(*result) if result is not None else None

    async def add_act_decision(self, decision: AdministrativeDecision, request_key: str) -> AdministrativeDecision:
        await self._session.execute(insert(CourtActDecisionRow).values(
            guild_id=decision.guild_id, case_id=decision.case_id, revision_id=decision.revision_id,
            outcome=decision.outcome.value, reason=decision.reason, judge_citizen_id=decision.judge_citizen_id,
            decided_at=decision.decided_at, request_key=request_key,
        ))
        result = (await self._session.execute(_decision_query(
            CourtActDecisionRow.guild_id == decision.guild_id, CourtActDecisionRow.case_id == decision.case_id,
        ))).one()
        return _to_decision(*result)

    async def list_returned_acts(
        self, discord_guild_id: int, act_kind: ActKind, *, limit: int, offset: int,
    ) -> tuple[AdministrativeDecision, ...]:
        rows = await self._session.execute(_decision_query(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), LawRevisionRow.act_kind == act_kind.value,
            CourtActDecisionRow.outcome == AdministrativeOutcome.GRANTED.value,
            ~appeal_overturned(),
        ).order_by(CourtActDecisionRow.decided_at.desc(), CourtActDecisionRow.case_id.desc()).limit(limit).offset(offset))
        return tuple(_to_decision(*row) for row in rows)

    async def get_merits_decision(self, discord_guild_id: int, case_id: int) -> MeritsDecision | None:
        return await self._merits_decision(
            GuildRow.discord_guild_id == Decimal(discord_guild_id), CourtActDecisionRow.case_id == case_id,
        )

    async def find_merits_decision(self, guild_id: int, request_key: str) -> MeritsDecision | None:
        return await self._merits_decision(
            CourtActDecisionRow.guild_id == guild_id, CourtActDecisionRow.request_key == request_key,
        )

    async def _merits_decision(self, *criteria: ColumnElement[bool]) -> MeritsDecision | None:
        row = await self._session.scalar(
            select(CourtActDecisionRow).join(GuildRow, GuildRow.id == CourtActDecisionRow.guild_id)
            .where(CourtActDecisionRow.case_kind != CaseKind.ADMINISTRATIVE.value, *criteria),
        )
        if row is None:
            return None
        sentence = None
        if row.sanction_article is not None:
            fine = (
                SanctionTerm(row.fine_amount, row.fine_min, row.fine_max)
                if row.fine_amount is not None else None
            )
            custody = (
                SanctionTerm(row.custody_hours, row.custody_min, row.custody_max)
                if row.custody_hours is not None else None
            )
            sentence = CourtSentence(row.sanction_article, fine, custody)
        return MeritsDecision(
            row.guild_id, row.case_id, CaseKind(row.case_kind), row.revision_id, MeritsOutcome(row.outcome),
            row.reason, row.judge_citizen_id, row.decided_at.astimezone(UTC), sentence,
        )

    async def add_merits_decision(self, decision: MeritsDecision, request_key: str) -> MeritsDecision:
        sentence = decision.sentence
        fine = sentence.fine if sentence is not None else None
        custody = sentence.custody if sentence is not None else None
        if custody is not None:
            await self._session.scalar(
                select(CitizenRow.id)
                .join(CourtCaseRow, CourtCaseRow.respondent_citizen_id == CitizenRow.id)
                .where(CourtCaseRow.guild_id == decision.guild_id, CourtCaseRow.id == decision.case_id)
                .with_for_update(of=CitizenRow, key_share=True),
            )
        await self._session.execute(insert(CourtActDecisionRow).values(
            guild_id=decision.guild_id, case_id=decision.case_id, case_kind=decision.kind.value,
            revision_id=decision.revision_id, outcome=decision.outcome.value, reason=decision.reason,
            judge_citizen_id=decision.judge_citizen_id, decided_at=decision.decided_at, request_key=request_key,
            sanction_article=sentence.article if sentence is not None else None,
            fine_amount=fine.value if fine is not None else None,
            fine_min=fine.article_minimum if fine is not None else None,
            fine_max=fine.article_maximum if fine is not None else None,
            custody_hours=custody.value if custody is not None else None,
            custody_min=custody.article_minimum if custody is not None else None,
            custody_max=custody.article_maximum if custody is not None else None,
        ))
        if fine is not None:
            try:
                async with self._session.begin_nested():
                    await PostgresLedgerRepository(self._session).hold_court_fine(decision.guild_id, decision.case_id)
            except LedgerError as error:
                if error.reason not in (LedgerErrorReason.INSUFFICIENT_FUNDS, LedgerErrorReason.ECONOMY_READ_ONLY):
                    raise
        return decision

    async def schedule_hearing(
        self,
        case: CourtCase,
        *,
        judge_citizen_id: int,
        hearing_at: datetime,
        scheduled_at: datetime,
        request_key: str,
    ) -> CourtCase:
        recorded = await self._find_hearing(case.guild_id, request_key)
        if recorded is not None:
            return await self._repeat_hearing(case, recorded, judge_citizen_id, hearing_at)
        if case.administrative_outcome is not None or case.merits_outcome is not None:
            raise CourtCaseError(CourtCaseErrorReason.ALREADY_DECIDED)
        if scheduled_at < case.filed_at or hearing_at <= scheduled_at:
            raise CourtCaseError(CourtCaseErrorReason.INVALID_TIME)
        hearing_id = await self._session.scalar(
            pg_insert(CourtHearingRow)
            .values(
                guild_id=case.guild_id, case_id=case.id, case_filed_at=case.filed_at, judge_citizen_id=judge_citizen_id,
                hearing_at=hearing_at, scheduled_at=scheduled_at, request_key=request_key,
            )
            .on_conflict_do_nothing(constraint="uq_court_hearings_request")
            .returning(CourtHearingRow.id)
        )
        if hearing_id is None:
            recorded = await self._find_hearing(case.guild_id, request_key)
            assert recorded is not None
            return await self._repeat_hearing(case, recorded, judge_citizen_id, hearing_at)
        return await self._require_case(case.guild_id, case.id)

    async def _find_hearing(self, guild_id: int, request_key: str) -> CourtHearingRow | None:
        return await self._session.scalar(select(CourtHearingRow).where(
            CourtHearingRow.guild_id == guild_id, CourtHearingRow.request_key == request_key,
        ))

    async def _repeat_hearing(
        self, case: CourtCase, recorded: CourtHearingRow, judge_citizen_id: int, hearing_at: datetime,
    ) -> CourtCase:
        if (recorded.case_id, recorded.judge_citizen_id, recorded.hearing_at) != (case.id, judge_citizen_id, hearing_at):
            raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
        return await self._require_case(case.guild_id, case.id)

    async def _require_case(self, guild_id: int, case_id: int) -> CourtCase:
        result = (await self._session.execute(_case_query(
            CourtCaseRow.guild_id == guild_id, CourtCaseRow.id == case_id,
        ))).one()
        return _to_case(*result)


class PostgresCourtCaseUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresCourtCaseRepository]:
        async with self._sessions.begin() as session:
            yield PostgresCourtCaseRepository(session)
