import asyncio
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy import insert
from sqlalchemy import text
from test_postgres_restore import restore_database

from civitas.app.admission import AdmissionService
from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.court import CourtError, CourtErrorReason, CourtService
from civitas.app.court_appeals import AppealOutcome, CourtAppealError, CourtAppealService
from civitas.app.court_cases import (
    AdministrativeOutcome,
    CaseKind,
    CaseSummary,
    CourtCaseError,
    CourtCaseErrorReason,
    CourtCaseService,
    CourtFineStatus,
    MeritsDecision,
    MeritsOutcome,
)
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.law import ActKind, LawError, LawErrorReason, LawService
from civitas.app.presentation import GuildPresentation, PresentationBinding, PresentationService
from civitas.app.receipts import ReceiptService
from civitas.app.prosecutor import ProsecutorAction, ProsecutorError, ProsecutorErrorReason, ProsecutorService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.government import OfficeKind
from civitas.domain.court_sanctions import CourtSentence, SanctionTerm
from civitas.domain.ledger import AccountKind, LedgerError, LedgerErrorReason, TransactionKind
from civitas.domain.reports import ReportWindow
from civitas.infrastructure.db.admission import PostgresAdmissionUnitOfWork
from civitas.infrastructure.db.admin_audit import PostgresAdminAuditUnitOfWork
from civitas.infrastructure.db.court import PostgresCourtUnitOfWork
from civitas.infrastructure.db.court_appeals import PostgresCourtAppealUnitOfWork
from civitas.infrastructure.db.court_cases import PostgresCourtCaseUnitOfWork
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.law import PostgresLawUnitOfWork
from civitas.infrastructure.db.ledger import PostgresLedgerUnitOfWork
from civitas.infrastructure.db.ledger_models import FinancialAccountRow
from civitas.infrastructure.db.maintenance import PostgresMaintenanceUnitOfWork
from civitas.infrastructure.db.reports import PostgresReportUnitOfWork
from civitas.infrastructure.db.prosecutor import PostgresProsecutorUnitOfWork
from civitas.infrastructure.db.presentation import PostgresPresentationUnitOfWork
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = 101
PRESIDENT, DEPUTY_1, DEPUTY_2, NOMINEE, RECORDER, CERTIFIER, OBSERVER = range(201, 208)


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 15, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest_asyncio.fixture
async def elected_offices(pg_engine, request):
    clock = Clock()
    admission = AdmissionService(PostgresAdmissionUnitOfWork(pg_engine), guild_id=GUILD)
    citizens_ids = (PRESIDENT, DEPUTY_1, DEPUTY_2, NOMINEE, RECORDER, CERTIFIER, OBSERVER)
    await admission.initialize(citizens_ids)
    government_uow = PostgresGovernmentUnitOfWork(pg_engine)
    citizens = CitizensService(government_uow, guild_id=GUILD, authorize_admission=admission.authorize)
    for user_id in citizens_ids:
        await citizens.register(GUILD, user_id)
    government = GovernmentService(
        government_uow,
        guild_id=GUILD,
        authorize=citizens.authorize,
        commissioner_user_ids=(RECORDER, CERTIFIER),
        rules=replace(
            ElectionRules(
                min_eligible=3, quorum_bp=4000, quorum_min=3, parliament_seats=2,
                term_days=28, voting_hours=48, extension_hours=24,
                poll_hosts=frozenset({"poll.example.test"}),
            ),
            **getattr(request, "param", {}),
        ),
        use_persisted_admission=True,
        clock=clock,
    )
    elections = []
    for office, votes in (
        (OfficeKind.PRESIDENT, {PRESIDENT: 4}), (OfficeKind.PARLIAMENT, {DEPUTY_1: 2, DEPUTY_2: 2}),
    ):
        election = await government.open_election(
            GUILD, RECORDER, office=office, candidate_user_ids=tuple(votes),
            reason="Scheduled elections", idempotency_key=office.value,
        )
        elections.append((office, election, votes))

    clock.now = elections[0][1].closes_at
    for office, election, votes in elections:
        await government.record_protocol(
            GUILD, RECORDER, election.id, source_url=f"https://poll.example.test/{office.value.lower()}",
            participants=4, tallies=votes, reason="Public results",
        )
        await government.certify_election(GUILD, CERTIFIER, election.id, reason="Checked poll protocol")

    clock.now += timedelta(days=3)
    return government, clock


async def test_court_appointment_needs_two_distinct_deputies_and_has_own_term(pg_engine, elected_offices):
    government, clock = elected_offices
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    proposed = dict(candidate_user_id=NOMINEE, reason="Court appointment", idempotency_key="court-1")
    with pytest.raises(CourtError, match=CourtErrorReason.PRESIDENT_REQUIRED):
        await court.nominate(GUILD, NOMINEE, **proposed)
    assert await court.current(GUILD) is None

    nomination = await court.nominate(GUILD, PRESIDENT, **proposed)
    assert await court.nominate(GUILD, PRESIDENT, **proposed) == nomination
    with pytest.raises(CourtError, match=CourtErrorReason.DEPUTY_REQUIRED):
        await court.approve(GUILD, CERTIFIER, nomination.id, reason="Outside parliament")
    competing = await court.nominate(GUILD, PRESIDENT, **(proposed | {
        "candidate_user_id": DEPUTY_1, "idempotency_key": "competing-candidate",
    }))
    with pytest.raises(CourtError, match=CourtErrorReason.CONFLICT_OF_INTEREST):
        await court.approve(GUILD, DEPUTY_1, competing.id, reason="Self approval")

    first = await court.approve(GUILD, DEPUTY_1, nomination.id, reason="First approval")
    assert first.approval_count == 1 and first.starts_at is None
    assert await court.approve(GUILD, DEPUTY_1, nomination.id, reason="First approval") == first
    assert await court.current(GUILD) is None

    first_replay, second_replay = await asyncio.gather(*(
        court.approve(GUILD, DEPUTY_2, nomination.id, reason="Second approval") for _ in range(2)
    ))
    assert first_replay == second_replay
    assert first_replay.approval_count == 2
    assert first_replay.starts_at == clock.now and first_replay.ends_at == clock.now + timedelta(days=28)
    assert await court.current(GUILD) == first_replay
    with pytest.raises(CourtError, match=CourtErrorReason.OFFICE_OCCUPIED):
        await court.approve(GUILD, DEPUTY_2, competing.id, reason="Already appointed")

    clock.now += timedelta(days=26)
    assert await government.list_offices(GUILD, NOMINEE) == ()
    assert await court.current(GUILD) == first_replay
    clock.now += timedelta(days=2)
    assert await court.current(GUILD) is None
    assert await court.get_nomination(GUILD, nomination.id) == first_replay


async def test_prosecutor_appointment_removal_and_stale_proposal(pg_engine, elected_offices):
    government, clock = elected_offices
    unit_of_work = PostgresProsecutorUnitOfWork(pg_engine)
    prosecutor = ProsecutorService(unit_of_work, guild_id=GUILD, clock=clock)
    appointment = dict(action=ProsecutorAction.APPOINT, candidate_user_id=NOMINEE, reason="Appoint prosecutor")
    with pytest.raises(ProsecutorError, match=ProsecutorErrorReason.PRESIDENT_REQUIRED):
        await prosecutor.propose(GUILD, NOMINEE, **appointment, idempotency_key="unauthorized")
    motion = await prosecutor.propose(GUILD, PRESIDENT, **appointment, idempotency_key="appoint-1")
    assert await prosecutor.propose(GUILD, PRESIDENT, **appointment, idempotency_key="appoint-1") == motion
    with pytest.raises(IntegrityError, match="requires two independent signatures"):
        async with unit_of_work() as repository:
            await repository.apply_prosecutor_motion(motion.guild_id, motion.id, at=clock.now)
    with pytest.raises(ProsecutorError, match=ProsecutorErrorReason.SENATOR_REQUIRED):
        await prosecutor.approve(GUILD, CERTIFIER, motion.id, reason="No Senate seat")

    first = await prosecutor.approve(GUILD, DEPUTY_1, motion.id, reason="First signature")
    assert first.approval_count == 1 and await prosecutor.current(GUILD) is None
    assert await prosecutor.approve(GUILD, DEPUTY_1, motion.id, reason="Repeat") == first
    second, replay = await asyncio.gather(*(
        prosecutor.approve(GUILD, DEPUTY_2, motion.id, reason="Second signature") for _ in range(2)
    ))
    assert second == replay == await prosecutor.current(GUILD)

    removal = dict(action=ProsecutorAction.REMOVE, reason="Remove current prosecutor")
    stale = await prosecutor.propose(GUILD, PRESIDENT, **removal, idempotency_key="competing-removal")
    removed = await prosecutor.propose(GUILD, PRESIDENT, **removal, idempotency_key="authorized-removal")
    assert stale.appointment_id == removed.appointment_id == motion.id
    await prosecutor.approve(GUILD, DEPUTY_1, removed.id, reason="First removal signature")
    assert await prosecutor.current(GUILD) == second
    await prosecutor.approve(GUILD, DEPUTY_2, removed.id, reason="Second removal signature")
    assert await prosecutor.current(GUILD) is None

    future = await prosecutor.propose(GUILD, PRESIDENT, **appointment, idempotency_key="appoint-2")
    await prosecutor.approve(GUILD, DEPUTY_1, future.id, reason="First signature")
    active = await prosecutor.approve(GUILD, DEPUTY_2, future.id, reason="Second signature")
    with pytest.raises(ProsecutorError, match=ProsecutorErrorReason.STALE_APPOINTMENT):
        await prosecutor.approve(GUILD, DEPUTY_2, stale.id, reason="Old removal")
    assert await prosecutor.current(GUILD) == active

    clock.now += timedelta(days=60)
    assert await government.list_offices(GUILD, NOMINEE) == ()
    restarted = ProsecutorService(PostgresProsecutorUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    assert await restarted.current(GUILD) == active


@pytest_asyncio.fixture
async def court_docket(pg_engine, elected_offices, request):
    _, clock = elected_offices
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    justice = await court.nominate(
        GUILD, PRESIDENT, candidate_user_id=NOMINEE, reason="Nominate judge", idempotency_key="docket-judge",
    )
    await court.approve(GUILD, DEPUTY_1, justice.id, reason="First judicial signature")
    await court.approve(GUILD, DEPUTY_2, justice.id, reason="Second judicial signature")

    prosecutor = ProsecutorService(PostgresProsecutorUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    motion = await prosecutor.propose(
        GUILD, PRESIDENT, action=ProsecutorAction.APPOINT,
        candidate_user_id=getattr(request, "param", RECORDER),
        reason="Nominate prosecutor", idempotency_key="docket-prosecutor",
    )
    await prosecutor.approve(GUILD, DEPUTY_1, motion.id, reason="First prosecutor signature")
    await prosecutor.approve(GUILD, DEPUTY_2, motion.id, reason="Second prosecutor signature")

    return CourtCaseService(PostgresCourtCaseUnitOfWork(pg_engine), guild_id=GUILD, clock=clock), clock


async def _register_law(laws, *, number, effective_at, correction_case_id=None):
    return await laws.register(
        GUILD, DEPUTY_1, code="PROC-01", title=f"Notices revision {number}",
        text=f"Published registration requirements, revision {number}.",
        source_url=f"https://discord.com/channels/{GUILD}/701/{800 + number}",
        effective_at=effective_at.isoformat(), idempotency_key=f"docket-law-{number}",
        correction_case_id=correction_case_id,
    )


async def test_early_removal_ends_only_the_target_mandate_and_allows_successor(pg_engine, court_docket):
    cases, clock = court_docket
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    incumbent = await court.current(GUILD)
    removal = await court.propose_removal(GUILD, PRESIDENT, reason="Fixture", idempotency_key="remove")
    competing = await court.propose_removal(GUILD, PRESIDENT, reason="Second motion", idempotency_key="competing")
    assert removal.nomination_id == incumbent.id
    assert await court.removal_details(GUILD, DEPUTY_2, removal.id) == removal
    with pytest.raises(CourtError, match="^deputy_required$"):
        await court.removal_details(GUILD, OBSERVER, removal.id)
    first = await court.approve_removal(GUILD, DEPUTY_1, removal.id)
    assert first.second_signed_at is None and await court.current(GUILD) == incumbent
    assert await court.removal_details(GUILD, PRESIDENT, removal.id) == first
    assert await court.approve_removal(GUILD, DEPUTY_1, removal.id) == first
    clock.now += timedelta(minutes=1)
    final = await court.approve_removal(GUILD, DEPUTY_2, removal.id)
    assert final.second_signed_at == clock.now and await court.current(GUILD) is None
    historical = await court.get_nomination(GUILD, incumbent.id)
    assert historical.removed_at == clock.now and historical.ends_at == incumbent.ends_at
    assert await court.approve_removal(GUILD, DEPUTY_2, removal.id) == final
    with pytest.raises(IntegrityError, match="Judicial removal signature history is immutable"):
        async with pg_engine.begin() as connection:
            await connection.execute(text(
                "UPDATE court_removal_proposals SET second_senator_id = NULL, second_signed_at = NULL WHERE id = :id",
            ), {"id": removal.id})
    assert await court.current(GUILD) is None
    async with PostgresAdminAuditUnitOfWork(pg_engine)() as repository:
        audit = await repository.list_admin_audit(GUILD, limit=100, offset=0)
    assert [(event.actor_discord_user_id, event.subject_id) for event in audit
            if event.action == "court.removal.confirm"] == [(DEPUTY_2, incumbent.id)]
    case = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=CERTIFIER, statement="Filed during vacancy", idempotency_key="vacancy-case",
    )
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(
            GUILD, NOMINEE, case.id, hearing_at=(clock.now + timedelta(hours=1)).isoformat(),
            idempotency_key="formerly-judge",
        )
    successor = await court.nominate(
        GUILD, PRESIDENT, candidate_user_id=OBSERVER, reason="New appointment", idempotency_key="early-successor",
    )
    await court.approve(GUILD, DEPUTY_1, successor.id, reason="First approval")
    await court.approve(GUILD, DEPUTY_2, successor.id, reason="Second approval")
    assert (await court.current(GUILD)).id == successor.id
    with pytest.raises(CourtError, match="^removal_expired$"):
        await court.approve_removal(GUILD, DEPUTY_2, competing.id)


async def test_only_prosecutor_can_file_charge_under_confirmed_law_in_force_at_event(pg_engine, court_docket):
    cases, clock = court_docket
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    first = await _register_law(laws, number=1, effective_at=clock.now)
    await laws.confirm(GUILD, DEPUTY_2, first.id)
    event_at = clock.now + timedelta(minutes=15)
    clock.now += timedelta(hours=1)
    second = await _register_law(laws, number=2, effective_at=clock.now)
    await laws.confirm(GUILD, DEPUTY_2, second.id)
    draft = await _register_law(laws, number=3, effective_at=clock.now + timedelta(hours=1))
    assert (await laws.show(GUILD, "PROC-01")).id == second.id

    charge = dict(
        respondent_user_id=CERTIFIER, law_revision_id=first.id, occurred_at=event_at.isoformat(),
        statement="A notice required on the event date was omitted.", idempotency_key="docket-charge",
    )
    with pytest.raises(CourtCaseError, match="^prosecutor_required$"):
        await cases.file_charge(GUILD, PRESIDENT, **charge)
    with pytest.raises(CourtCaseError, match="^invalid_law$"):
        await cases.file_charge(GUILD, RECORDER, **(charge | {"law_revision_id": second.id}))
    with pytest.raises(CourtCaseError, match="^invalid_law$"):
        await cases.file_charge(GUILD, RECORDER, **(charge | {"law_revision_id": draft.id}))
    assert not await cases.list_cases(GUILD)

    filed, replay = await asyncio.gather(*[cases.file_charge(GUILD, RECORDER, **charge) for _ in range(2)])
    assert replay == filed
    assert filed.kind is CaseKind.CRIMINAL
    assert filed.for_state and not filed.against_state
    assert filed.filer_user_id == RECORDER and filed.respondent_user_id == CERTIFIER
    assert filed.law_revision_id == first.id and filed.occurred_at == event_at
    assert await cases.case(GUILD, CERTIFIER, filed.id) == (filed, True)
    assert [entry.id for entry in await cases.list_cases(GUILD)] == [filed.id]
    assert (await cases.case(GUILD, RECORDER, filed.id))[1]
    await cases.recuse(GUILD, RECORDER, filed.id, role="PROSECUTOR")
    assert not (await cases.case(GUILD, RECORDER, filed.id))[1]
    conflicted = await cases.file_charge(
        GUILD, RECORDER, **(charge | {
            "respondent_user_id": OBSERVER, "idempotency_key": "charge-against-future-prosecutor",
        }),
    )

    prosecutor = ProsecutorService(PostgresProsecutorUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    removal = await prosecutor.propose(
        GUILD, PRESIDENT, action=ProsecutorAction.REMOVE, reason="Prosecutor removal",
        idempotency_key="docket-remove-prosecutor",
    )
    await prosecutor.approve(GUILD, DEPUTY_1, removal.id, reason="First removal signature")
    await prosecutor.approve(GUILD, DEPUTY_2, removal.id, reason="Second removal signature")
    with pytest.raises(CourtCaseError, match="^prosecutor_required$"):
        await cases.file_charge(GUILD, RECORDER, **(charge | {"idempotency_key": "former-prosecutor-charge"}))
    with pytest.raises(CourtCaseError, match="^prosecutor_required$"):
        await cases.file_charge(GUILD, RECORDER, **charge)
    summary, may_read_statement = await cases.case(GUILD, RECORDER, filed.id)
    assert isinstance(summary, CaseSummary) and not may_read_statement
    assert await cases.case(GUILD, CERTIFIER, filed.id) == (filed, True)
    assert await cases.case(GUILD, NOMINEE, filed.id) == (filed, True)
    assert await cases.case(GUILD, OBSERVER, filed.id) == (summary, False)
    replacement = await prosecutor.propose(
        GUILD, PRESIDENT, action=ProsecutorAction.APPOINT, candidate_user_id=OBSERVER,
        reason="Nominate replacement", idempotency_key="docket-replacement-prosecutor",
    )
    await prosecutor.approve(GUILD, DEPUTY_1, replacement.id, reason="First replacement signature")
    assert await cases.case(GUILD, OBSERVER, filed.id) == (summary, False)
    await prosecutor.approve(GUILD, DEPUTY_2, replacement.id, reason="Second replacement signature")
    assert await cases.case(GUILD, OBSERVER, filed.id) == (filed, True)
    assert await cases.case(GUILD, RECORDER, filed.id) == (summary, False)
    proposal = await cases.propose_case_substitute(
        GUILD, PRESIDENT, conflicted.id, CERTIFIER, idempotency_key="state-substitute", role="PROSECUTOR",
    )
    assert not await cases.approve_case_substitute(GUILD, DEPUTY_1, proposal, role="PROSECUTOR")
    assert not (await cases.case(GUILD, CERTIFIER, conflicted.id))[1]
    assert await cases.approve_case_substitute(GUILD, DEPUTY_2, proposal, role="PROSECUTOR")
    assert await cases.case(GUILD, CERTIFIER, conflicted.id) == (conflicted, True)
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, conflicted.id, hearing_at=hearing.isoformat(),
        idempotency_key="hearing-with-state-substitute",
    )
    clock.now = hearing
    await cases.decide_merits(
        GUILD, NOMINEE, conflicted.id, outcome=MeritsOutcome.ACQUITTED, reason="Published decision",
        idempotency_key="decision-with-state-substitute",
    )
    appeals = CourtAppealService(
        PostgresCourtAppealUnitOfWork(pg_engine), guild_id=GUILD, clock=clock,
        commissioner_user_ids=(CERTIFIER, DEPUTY_1, DEPUTY_2), quorum_bp=4000, quorum_min=3,
        poll_hosts={"poll.example.test"},
    )
    assert (await appeals.file(GUILD, CERTIFIER, conflicted.id)).case_id == conflicted.id
    with pytest.raises(CourtAppealError, match="^conflict_of_interest$"):
        await appeals.open(GUILD, CERTIFIER, conflicted.id, source_url="https://poll.example.test/conflict")


async def test_civil_case_statement_stays_private_and_only_disinterested_judge_sets_hearing(court_docket):
    cases, clock = court_docket
    payload = dict(
        respondent_user_id=CERTIFIER, statement="Privately submitted evidence for a civil claim.",
        idempotency_key="civil-docket",
    )
    filed = await cases.file_civil(GUILD, PRESIDENT, **payload)
    assert filed.kind is CaseKind.CIVIL and filed.hearing_at is None
    assert await cases.file_civil(GUILD, PRESIDENT, **payload) == filed
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.file_civil(GUILD, PRESIDENT, **(payload | {"statement": "A different claim."}))
    for viewer in (PRESIDENT, CERTIFIER, NOMINEE):
        assert await cases.case(GUILD, viewer, filed.id) == (filed, True)
    for viewer in (DEPUTY_1, RECORDER):
        summary, may_read_statement = await cases.case(GUILD, viewer, filed.id)
        assert isinstance(summary, CaseSummary)
        assert summary.id == filed.id and not may_read_statement
        assert not hasattr(summary, "statement") and not hasattr(summary, "respondent_user_id")
    assert all(isinstance(summary, CaseSummary) for summary in await cases.list_cases(GUILD))

    hearing = dict(hearing_at=(clock.now + timedelta(hours=1)).isoformat(), idempotency_key="civil-hearing")
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(GUILD, PRESIDENT, filed.id, **hearing)
    with pytest.raises(CourtCaseError, match="^invalid_time$"):
        await cases.schedule_hearing(
            GUILD, NOMINEE, filed.id, hearing_at=clock.now.isoformat(), idempotency_key="past-civil-hearing",
        )
    conflicted = await cases.file_civil(GUILD, NOMINEE, **(payload | {"idempotency_key": "judge-as-litigant"}))
    with pytest.raises(CourtCaseError, match="^conflict_of_interest$"):
        await cases.schedule_hearing(GUILD, NOMINEE, conflicted.id, **hearing)

    scheduled, replay = await asyncio.gather(*[
        cases.schedule_hearing(GUILD, NOMINEE, filed.id, **hearing) for _ in range(2)
    ])
    assert replay == scheduled
    assert scheduled.hearing_at == datetime.fromisoformat(hearing["hearing_at"])
    later = (clock.now + timedelta(hours=2)).isoformat()
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.schedule_hearing(GUILD, NOMINEE, filed.id, **(hearing | {"hearing_at": later}))
    rescheduled = await cases.schedule_hearing(
        GUILD, NOMINEE, filed.id, hearing_at=later, idempotency_key="reschedule-civil-hearing",
    )
    assert rescheduled.hearing_at == datetime.fromisoformat(later)
    assert (await cases.case(GUILD, DEPUTY_1, filed.id))[0].hearing_at == rescheduled.hearing_at
    clock.now += timedelta(minutes=90)
    assert await cases.schedule_hearing(GUILD, NOMINEE, filed.id, **hearing) == rescheduled

    clock.now += timedelta(days=29)
    summary, may_read_statement = await cases.case(GUILD, NOMINEE, filed.id)
    assert isinstance(summary, CaseSummary) and not may_read_statement
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(
            GUILD, NOMINEE, filed.id, hearing_at=(clock.now + timedelta(hours=1)).isoformat(),
            idempotency_key="expired-judge-hearing",
        )
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.decide_merits(
            GUILD, NOMINEE, filed.id, outcome=MeritsOutcome.GRANTED,
            reason="Former judge attempts to rule", idempotency_key="expired-judge-ruling",
        )
    waiting_case = await cases.file_civil(GUILD, PRESIDENT, **(payload | {"idempotency_key": "claim-during-vacancy"}))
    assert waiting_case.hearing_at is None
    cases_by_id = {case.id: case for case in await cases.list_cases(GUILD)}
    assert cases_by_id[filed.id].hearing_at == rescheduled.hearing_at
    assert cases_by_id[filed.id].merits_outcome is None
    assert cases_by_id[waiting_case.id].hearing_at is None


@pytest.mark.parametrize("elected_offices", [{"term_days": 56}], indirect=True)
async def test_successor_can_resume_case_after_judicial_vacancy(pg_engine, court_docket):
    cases, clock = court_docket
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    filed = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=CERTIFIER, statement="Evidence submitted before the vacancy.",
        idempotency_key="successor-case",
    )
    await cases.schedule_hearing(
        GUILD, NOMINEE, filed.id, hearing_at=(clock.now + timedelta(hours=1)).isoformat(),
        idempotency_key="pre-vacancy-hearing",
    )
    clock.now += timedelta(days=29)
    nominee = await court.nominate(
        GUILD, PRESIDENT, candidate_user_id=OBSERVER, reason="Nominate successor", idempotency_key="successor",
    )
    await court.approve(GUILD, DEPUTY_1, nominee.id, reason="First independent signature")
    hearing_at = clock.now + timedelta(hours=1)
    hearing = dict(hearing_at=hearing_at.isoformat(), idempotency_key="successor-hearing")
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(GUILD, OBSERVER, filed.id, **hearing)
    await court.approve(GUILD, DEPUTY_2, nominee.id, reason="Second independent signature")
    assert (await cases.case(GUILD, OBSERVER, filed.id))[1]
    updated = await cases.schedule_hearing(GUILD, OBSERVER, filed.id, **hearing)
    assert updated.id == filed.id and updated.statement == filed.statement
    clock.now = hearing_at
    result = await cases.decide_merits(
        GUILD, OBSERVER, filed.id, outcome=MeritsOutcome.GRANTED,
        reason="Successor heard the parties", idempotency_key="successor-verdict",
    )
    assert result.case_id == filed.id and result.judge_citizen_id == updated.scheduled_by_citizen_id
    appeals = CourtAppealService(
        PostgresCourtAppealUnitOfWork(pg_engine), guild_id=GUILD, clock=clock,
        commissioner_user_ids=(NOMINEE, RECORDER, DEPUTY_1), quorum_bp=4000, quorum_min=3,
        poll_hosts={"poll.example.test"},
    )
    await appeals.file(GUILD, CERTIFIER, filed.id)
    source = "https://poll.example.test/successor-case"
    with pytest.raises(CourtAppealError, match="^conflict_of_interest$"):
        await appeals.open(GUILD, NOMINEE, filed.id, source_url=source)
    assert (await appeals.open(GUILD, RECORDER, filed.id, source_url=source)).case_id == filed.id


async def test_substitute_judge_requires_two_signatures_and_has_case_scope(pg_engine, court_docket):
    cases, clock = court_docket
    case = await cases.file_civil(
        GUILD, NOMINEE, respondent_user_id=CERTIFIER, statement="Case involving the incumbent judge.",
        idempotency_key="case-substitute",
    )
    other = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=CERTIFIER, statement="Unrelated case.",
        idempotency_key="case-with-incumbent",
    )
    with pytest.raises(CourtCaseError, match="^substitute_not_allowed$"):
        await cases.propose_case_judge(GUILD, PRESIDENT, other.id, OBSERVER, idempotency_key="no-conflict")
    proposal_id = await cases.propose_case_judge(GUILD, PRESIDENT, case.id, OBSERVER, idempotency_key="substitute")
    assert await cases.substitute_details(GUILD, DEPUTY_1, proposal_id, role="JUDGE") == (case.id, OBSERVER, 0)
    with pytest.raises(CitizenshipAccessError):
        await cases.substitute_details(GUILD, CERTIFIER, proposal_id, role="JUDGE")
    assert not await cases.approve_case_judge(GUILD, DEPUTY_1, proposal_id)
    assert not await cases.approve_case_judge(GUILD, DEPUTY_1, proposal_id)
    hearing_at = clock.now + timedelta(hours=1)
    hearing = dict(hearing_at=hearing_at.isoformat(), idempotency_key="substitute-hearing")
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(GUILD, OBSERVER, case.id, **hearing)
    assert await cases.approve_case_judge(GUILD, DEPUTY_2, proposal_id)
    assert await cases.substitute_details(GUILD, PRESIDENT, proposal_id, role="JUDGE") == (case.id, OBSERVER, 2)
    assert await cases.approve_case_judge(GUILD, DEPUTY_2, proposal_id)
    async with PostgresAdminAuditUnitOfWork(pg_engine)() as repository:
        audit = await repository.list_admin_audit(GUILD, limit=100, offset=0)
    events = [
        (entry.action.rsplit(".", 1)[-1], entry.actor_discord_user_id, entry.subject_id)
        for entry in audit if entry.action.startswith("court.substitute.judge.")
    ]
    assert sorted(events) == [
        ("confirm", DEPUTY_2, case.id), ("nominate", PRESIDENT, case.id), ("sign", DEPUTY_1, case.id),
    ]
    assert await cases.propose_case_judge(
        GUILD, PRESIDENT, case.id, OBSERVER, idempotency_key="substitute",
    ) == proposal_id
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.propose_case_judge(GUILD, PRESIDENT, case.id, RECORDER, idempotency_key="substitute")
    assert (await cases.case(GUILD, OBSERVER, case.id))[1]
    with pytest.raises(CourtCaseError, match="^judge_required$"):
        await cases.schedule_hearing(GUILD, OBSERVER, other.id, **hearing)
    updated = await cases.schedule_hearing(GUILD, OBSERVER, case.id, **hearing)
    clock.now = hearing_at
    verdict = await cases.decide_merits(
        GUILD, OBSERVER, case.id, outcome=MeritsOutcome.GRANTED,
        reason="Substitute heard both parties", idempotency_key="substitute-verdict",
    )
    assert verdict.judge_citizen_id == updated.scheduled_by_citizen_id


async def test_parallel_case_substitute_approvals_confirm_only_one_nominee(pg_engine, court_docket):
    cases, _ = court_docket
    case = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=CERTIFIER, statement="Case with a voluntary recusal.",
        idempotency_key="parallel-substitute-case",
    )
    with pytest.raises(CourtCaseError, match="^substitute_not_allowed$"):
        await cases.recuse(GUILD, OBSERVER, case.id, role="JUDGE")
    await cases.recuse(GUILD, NOMINEE, case.id, role="JUDGE")
    await cases.recuse(GUILD, NOMINEE, case.id, role="JUDGE")
    for mutation in (
        "UPDATE court_case_recusals SET recused_at = now() WHERE case_id = :case_id",
        "DELETE FROM court_case_recusals WHERE case_id = :case_id",
    ):
        with pytest.raises(IntegrityError):
            async with pg_engine.begin() as connection:
                await connection.execute(text(mutation), {"case_id": case.id})
    async with PostgresAdminAuditUnitOfWork(pg_engine)() as repository:
        audit = await repository.list_admin_audit(GUILD, limit=100, offset=0)
    recusals = [entry for entry in audit if entry.action == "court.recuse.judge"]
    assert [(entry.actor_discord_user_id, entry.subject_id) for entry in recusals] == [(NOMINEE, case.id)]
    assert not (await cases.case(GUILD, NOMINEE, case.id))[1]
    candidates = (OBSERVER, RECORDER)
    proposals = [
        await cases.propose_case_judge(GUILD, PRESIDENT, case.id, candidate, idempotency_key=f"proposal-{candidate}")
        for candidate in candidates
    ]
    for proposal in proposals:
        assert not await cases.approve_case_judge(GUILD, DEPUTY_1, proposal)
    results = await asyncio.gather(
        *(cases.approve_case_judge(GUILD, DEPUTY_2, proposal) for proposal in proposals), return_exceptions=True,
    )
    assert results.count(True) == 1
    failure = results[1 - results.index(True)]
    assert isinstance(failure, CourtCaseError) and failure.reason is CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED
    for candidate, result in zip(candidates, results, strict=True):
        assert (await cases.case(GUILD, candidate, case.id))[1] is (result is True)

    def migrate(connection):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.downgrade(config, "0043_court_removal_guard")
        command.upgrade(config, "head")

    async with pg_engine.begin() as connection:
        await connection.run_sync(migrate)

    assert not (await cases.case(GUILD, NOMINEE, case.id))[1]
    for candidate, result in zip(candidates, results, strict=True):
        assert (await cases.case(GUILD, candidate, case.id))[1] is (result is True)


async def test_substitute_judge_can_hear_case_when_incumbent_represents_state(pg_engine, court_docket):
    cases, clock = court_docket
    prosecutor = ProsecutorService(PostgresProsecutorUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    for action, candidate in ((ProsecutorAction.REMOVE, None), (ProsecutorAction.APPOINT, NOMINEE)):
        motion = await prosecutor.propose(
            GUILD, PRESIDENT, action=action, candidate_user_id=candidate, reason="Fixture motion",
            idempotency_key=f"dual-mandate-{action.value}",
        )
        await prosecutor.approve(GUILD, DEPUTY_1, motion.id, reason="First signature")
        await prosecutor.approve(GUILD, DEPUTY_2, motion.id, reason="Second signature")
    law_service = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    draft = await _register_law(law_service, number=1, effective_at=clock.now)
    law = await law_service.confirm(GUILD, DEPUTY_2, draft.id)
    case = await cases.file_challenge(
        GUILD, CERTIFIER, law_revision_id=law.id, statement="My rights are affected.",
        idempotency_key="dual-mandate-challenge",
    )
    hearing_at = clock.now + timedelta(hours=1)
    hearing = dict(hearing_at=hearing_at.isoformat(), idempotency_key="dual-mandate-hearing")
    with pytest.raises(CourtCaseError, match="^conflict_of_interest$"):
        await cases.schedule_hearing(GUILD, NOMINEE, case.id, **hearing)
    proposal = await cases.propose_case_judge(GUILD, PRESIDENT, case.id, OBSERVER, idempotency_key="dual-mandate-judge")
    assert not await cases.approve_case_judge(GUILD, DEPUTY_1, proposal)
    assert await cases.approve_case_judge(GUILD, DEPUTY_2, proposal)
    assert (await cases.schedule_hearing(GUILD, OBSERVER, case.id, **hearing)).hearing_at == hearing_at


async def test_case_substitute_storage_requires_independent_signatures(pg_engine, court_docket):
    cases, clock = court_docket
    case = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=CERTIFIER, statement="Fixture",
        idempotency_key="substitute-storage-case",
    )
    sql = text("""
        INSERT INTO court_case_substitutes (
            guild_id, case_id, case_kind, role, candidate_id, proposer_id, proposed_at,
            request_key, first_senator_id, first_signed_at, second_senator_id, second_signed_at
        )
        SELECT :guild, :case_id, 'CIVIL', 'JUDGE', candidate.id, proposer.id, :at,
            :key, first.id, :at, second.id, :at
        FROM citizens candidate, citizens proposer, citizens first, citizens second
        WHERE candidate.discord_user_id = :candidate AND proposer.discord_user_id = :proposer
            AND first.discord_user_id = :first AND second.discord_user_id = :second
            AND candidate.guild_id = :guild AND proposer.guild_id = :guild
            AND first.guild_id = :guild AND second.guild_id = :guild
        RETURNING id
    """)
    values = dict(
        guild=case.guild_id, case_id=case.id, at=clock.now, key="test-mandate",
        candidate=OBSERVER, proposer=PRESIDENT, first=DEPUTY_1, second=DEPUTY_2,
    )
    with pytest.raises(IntegrityError, match="must be proposed before signing"):
        async with pg_engine.begin() as connection:
            await connection.scalar(sql, values)

    async def record_signature(connection, proposal_id, senator, *, stage):
        columns = {
            "first": ("first_senator_id", "first_signed_at"),
            "second": ("second_senator_id", "second_signed_at"),
        }
        column, signed_at = columns[stage]
        await connection.execute(text(
            f"UPDATE court_case_substitutes SET {column} = (SELECT id FROM citizens "
            f"WHERE guild_id = :guild AND discord_user_id = :senator), {signed_at} = :at WHERE id = :proposal"
        ), {"guild": case.guild_id, "senator": senator, "at": clock.now, "proposal": proposal_id})

    draft_sql = text(str(sql).replace(":key, first.id, :at, second.id, :at", ":key, NULL, NULL, NULL, NULL"))
    async with pg_engine.begin() as connection:
        proposal = await connection.scalar(draft_sql, values)
        other = await connection.scalar(draft_sql, values | {"key": "other-proposal"})
        await record_signature(connection, proposal, DEPUTY_1, stage="first")
        await record_signature(connection, other, DEPUTY_1, stage="first")
    with pytest.raises(IntegrityError, match="ck_case_substitutes_independence"):
        async with pg_engine.begin() as connection:
            await record_signature(connection, proposal, DEPUTY_1, stage="second")
    async with pg_engine.begin() as connection:
        await record_signature(connection, proposal, DEPUTY_2, stage="second")
    with pytest.raises(IntegrityError, match="uq_case_substitutes_confirmed"):
        async with pg_engine.begin() as connection:
            await record_signature(connection, other, DEPUTY_2, stage="second")
    with pytest.raises(IntegrityError, match="signature history is immutable"):
        async with pg_engine.begin() as connection:
            await connection.execute(text(
                "UPDATE court_case_substitutes SET second_senator_id = NULL, second_signed_at = NULL WHERE id = :id",
            ), {"id": proposal})


async def test_challenge_links_confirmed_revision_and_state_representation_is_scoped(pg_engine, court_docket):
    cases, clock = court_docket
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    draft = await _register_law(laws, number=1, effective_at=clock.now)
    challenge = dict(
        law_revision_id=draft.id, statement="This requirement restricted my use of the notice register.",
        idempotency_key="law-challenge",
    )
    with pytest.raises(CourtCaseError, match="^invalid_law$"):
        await cases.file_challenge(GUILD, CERTIFIER, **challenge)
    confirmed = await laws.confirm(GUILD, DEPUTY_2, draft.id)
    case = await cases.file_challenge(GUILD, CERTIFIER, **challenge)
    assert case.kind is CaseKind.ADMINISTRATIVE
    assert case.law_revision_id == confirmed.id and case.against_state and not case.for_state
    assert await cases.case(GUILD, RECORDER, case.id) == (case, True)
    summary, may_read_statement = await cases.case(GUILD, DEPUTY_1, case.id)
    assert isinstance(summary, CaseSummary) and not may_read_statement
    review = await cases.file_challenge(
        GUILD, RECORDER, law_revision_id=confirmed.id, statement="Review for compliance with the charter.",
        idempotency_key="prosecutor-law-challenge",
    )
    assert review.kind is CaseKind.ADMINISTRATIVE and review.for_state and review.against_state

    civil_payload = dict(
        statement="Motion on behalf of the state.", respondent_user_id=CERTIFIER,
        for_state=True, idempotency_key="state-civil",
    )
    with pytest.raises(CourtCaseError, match="^prosecutor_required$"):
        await cases.file_civil(GUILD, PRESIDENT, **civil_payload)
    civil = await cases.file_civil(GUILD, RECORDER, **civil_payload)
    assert civil.kind is CaseKind.CIVIL and civil.for_state and not civil.against_state


def _decree_options(clock):
    return dict(
        code="PROC-01", title="Presidential notice", text="Published presidential decree about notices.",
        source_url=f"https://discord.com/channels/{GUILD}/701/900",
        effective_at=clock.now.isoformat(), idempotency_key="decree-1",
    )


async def test_decree_needs_president_and_senator_and_has_its_own_namespace(pg_engine, elected_offices):
    _, clock = elected_offices
    unit_of_work = PostgresLawUnitOfWork(pg_engine)
    laws = LawService(unit_of_work, guild_id=GUILD, clock=clock)
    decrees = LawService(unit_of_work, guild_id=GUILD, clock=clock, act_kind=ActKind.DECREE)

    options = _decree_options(clock)
    with pytest.raises(LawError, match=LawErrorReason.PRESIDENT_REQUIRED):
        await decrees.register(GUILD, DEPUTY_1, **options)
    with pytest.raises(LawError, match=LawErrorReason.INVALID_SOURCE):
        await decrees.register(GUILD, PRESIDENT, **(options | {"source_url": "https://discord.com/channels/102/701/900"}))

    law = await _register_law(laws, number=1, effective_at=clock.now)
    law = await laws.confirm(GUILD, DEPUTY_2, law.id)
    decree = await decrees.register(GUILD, PRESIDENT, **options)
    assert await decrees.register(GUILD, PRESIDENT, **options) == decree
    assert decree.act_kind is ActKind.DECREE and decree.confirmed_at is None
    assert await decrees.revision(GUILD, PRESIDENT, decree.id) == decree
    assert await decrees.revision(GUILD, DEPUTY_1, decree.id) == decree
    assert not await decrees.list_effective(GUILD)
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await decrees.revision(GUILD, OBSERVER, decree.id)
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await laws.confirm(GUILD, DEPUTY_1, decree.id)
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await decrees.confirm(GUILD, DEPUTY_1, law.id)
    with pytest.raises(LawError, match=LawErrorReason.DEPUTY_REQUIRED):
        await decrees.confirm(GUILD, PRESIDENT, decree.id)
    with pytest.raises(LawError, match=LawErrorReason.DEPUTY_REQUIRED):
        await decrees.confirm(GUILD, OBSERVER, decree.id)

    confirmed, replay = await asyncio.gather(
        decrees.confirm(GUILD, DEPUTY_1, decree.id),
        decrees.confirm(GUILD, DEPUTY_2, decree.id),
    )
    assert confirmed == replay and confirmed.confirmed_at == clock.now
    assert await decrees.revision(GUILD, OBSERVER, decree.id) == confirmed
    assert await laws.show(GUILD, law.code) == law
    assert await decrees.show(GUILD, law.code) == confirmed
    assert await laws.list_effective(GUILD) == (law,)
    assert await decrees.list_effective(GUILD) == (confirmed,)

    clock.now += timedelta(days=29)
    with pytest.raises(LawError, match=LawErrorReason.PRESIDENT_REQUIRED):
        await decrees.register(GUILD, PRESIDENT, **(options | {"idempotency_key": "expired-president"}))


async def test_decree_can_be_challenged_but_cannot_support_a_criminal_charge(pg_engine, court_docket):
    cases, clock = court_docket
    unit_of_work = PostgresLawUnitOfWork(pg_engine)
    laws = LawService(unit_of_work, guild_id=GUILD, clock=clock)
    decrees = LawService(unit_of_work, guild_id=GUILD, clock=clock, act_kind=ActKind.DECREE)
    law = await _register_law(laws, number=1, effective_at=clock.now)
    law = await laws.confirm(GUILD, DEPUTY_2, law.id)
    decree = await decrees.register(GUILD, PRESIDENT, **_decree_options(clock))

    challenge = dict(
        decree_revision_id=decree.id, statement="The decree restricted my use of the notice register.",
        idempotency_key="decree-challenge",
    )
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_DECREE):
        await cases.file_decree_challenge(GUILD, CERTIFIER, **challenge)
    await decrees.confirm(GUILD, DEPUTY_2, decree.id)
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_DECREE):
        await cases.file_decree_challenge(GUILD, CERTIFIER, **(challenge | {"decree_revision_id": law.id}))
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_LAW):
        await cases.file_challenge(
            GUILD, CERTIFIER, law_revision_id=decree.id, statement=challenge["statement"],
            idempotency_key="decree-as-law-challenge",
        )

    filed = await cases.file_decree_challenge(GUILD, CERTIFIER, **challenge)
    assert filed.kind is CaseKind.ADMINISTRATIVE and filed.act_kind is ActKind.DECREE
    assert filed.law_revision_id == decree.id and filed.against_state and not filed.for_state
    assert await cases.file_decree_challenge(GUILD, CERTIFIER, **challenge) == filed
    summary, may_read_statement = await cases.case(GUILD, OBSERVER, filed.id)
    assert isinstance(summary, CaseSummary) and not may_read_statement
    assert summary.act_kind is ActKind.DECREE and summary.law_revision_id == decree.id

    charge = dict(
        respondent_user_id=CERTIFIER, occurred_at=clock.now.isoformat(),
        statement="A required notice was omitted.", idempotency_key="decree-criminal-charge",
    )
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_LAW):
        await cases.file_charge(GUILD, RECORDER, **charge, law_revision_id=decree.id)
    criminal_case = await cases.file_charge(GUILD, RECORDER, **charge, law_revision_id=law.id)
    assert criminal_case.act_kind is ActKind.LAW
    invalid_case = replace(criminal_case, law_revision_id=decree.id, act_kind=ActKind.DECREE)
    with pytest.raises(IntegrityError, match="Criminal charges require a law revision"):
        async with PostgresCourtCaseUnitOfWork(pg_engine)() as repository:
            await repository.add_case(invalid_case, request_key="decree-as-criminal-ground")


async def test_only_disinterested_judge_can_deny_challenge_after_hearing_once(pg_engine, court_docket):
    cases, clock = court_docket
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    draft = await _register_law(laws, number=1, effective_at=clock.now)
    law = await laws.confirm(GUILD, DEPUTY_2, draft.id)
    challenge = dict(law_revision_id=law.id, statement="Private details supporting an administrative claim.")
    filed = await cases.file_challenge(GUILD, CERTIFIER, **challenge, idempotency_key="hearing-required-challenge")
    conflict = await cases.file_challenge(GUILD, NOMINEE, **challenge, idempotency_key="judge-own-challenge")
    civil = await cases.file_civil(
        GUILD, CERTIFIER, respondent_user_id=OBSERVER, statement="A separate civil claim.",
        idempotency_key="civil-without-administrative-decision",
    )
    proposed = dict(
        outcome=AdministrativeOutcome.DENIED, reason="The established facts do not support the claim.",
        idempotency_key="deny-challenge",
    )
    assert await cases.decision(GUILD, filed.id) is None
    for user_id, case_id, error in (
        (PRESIDENT, filed.id, CourtCaseErrorReason.JUDGE_REQUIRED),
        (NOMINEE, conflict.id, CourtCaseErrorReason.CONFLICT_OF_INTEREST),
        (NOMINEE, civil.id, CourtCaseErrorReason.ADMINISTRATIVE_REQUIRED),
        (NOMINEE, filed.id, CourtCaseErrorReason.HEARING_REQUIRED),
    ):
        with pytest.raises(CourtCaseError, match=f"^{error.value}$"):
            await cases.decide_challenge(GUILD, user_id, case_id, **proposed)

    hearing_at = clock.now + timedelta(hours=1)
    hearing = dict(hearing_at=hearing_at.isoformat(), idempotency_key="deny-challenge-hearing")
    await cases.schedule_hearing(GUILD, NOMINEE, filed.id, **hearing)
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.HEARING_REQUIRED):
        await cases.decide_challenge(GUILD, NOMINEE, filed.id, **proposed)
    clock.now = hearing_at
    first, repeated = await asyncio.gather(
        cases.decide_challenge(GUILD, NOMINEE, filed.id, **proposed),
        cases.decide_challenge(GUILD, NOMINEE, filed.id, **proposed),
    )
    assert first == repeated == await cases.decision(GUILD, filed.id)
    assert first.case_id == filed.id and first.revision_id == law.id
    assert first.outcome is AdministrativeOutcome.DENIED and first.decided_at == hearing_at
    assert first.reason == proposed["reason"]
    assert await laws.show(GUILD, law.code) == law
    assert not await cases.list_returns(GUILD, ActKind.LAW)
    assert not await cases.list_returns(GUILD, ActKind.DECREE)
    public, may_read_statement = await cases.case(GUILD, OBSERVER, filed.id)
    assert isinstance(public, CaseSummary) and not may_read_statement
    assert public.administrative_outcome is AdministrativeOutcome.DENIED

    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_REQUEST):
        await cases.decide_challenge(GUILD, NOMINEE, filed.id, **(proposed | {"reason": "Different reasoning."}))
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.ALREADY_DECIDED):
        await cases.decide_challenge(
            GUILD, NOMINEE, filed.id, **(proposed | {
                "outcome": AdministrativeOutcome.GRANTED, "idempotency_key": "reverse-without-appeal",
            }),
        )
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.ALREADY_DECIDED):
        await cases.schedule_hearing(
            GUILD, NOMINEE, filed.id, hearing_at=(clock.now + timedelta(hours=1)).isoformat(),
            idempotency_key="closed-hearing",
        )


async def test_granted_challenges_suspend_exact_law_revisions_and_restore_preceding_law(pg_engine, court_docket):
    cases, clock = court_docket
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    revisions = []
    for number in range(1, 4):
        draft = await _register_law(laws, number=number, effective_at=clock.now)
        revisions.append(await laws.confirm(GUILD, DEPUTY_2, draft.id))
        clock.now += timedelta(minutes=10)
    original, intermediate, latest = revisions
    assert await laws.show(GUILD, latest.code) == latest

    hearing_at = clock.now + timedelta(hours=1)
    challenges = []
    for act in (intermediate, latest):
        act_case = await cases.file_challenge(
            GUILD, CERTIFIER, law_revision_id=act.id, statement="The rule impaired my right to submit a notice.",
            idempotency_key=f"challenge-revision-{act.id}",
        )
        await cases.schedule_hearing(
            GUILD, NOMINEE, act_case.id, hearing_at=hearing_at.isoformat(), idempotency_key=f"hearing-{act_case.id}",
        )
        challenges.append(act_case)

    clock.now = hearing_at
    earlier_decision = await cases.decide_challenge(
        GUILD, NOMINEE, challenges[0].id, outcome=AdministrativeOutcome.GRANTED,
        reason="The intermediate version contradicts the charter.", idempotency_key="suspend-intermediate-law",
    )
    assert earlier_decision.revision_id == intermediate.id
    assert await laws.show(GUILD, latest.code) == latest
    clock.now += timedelta(minutes=1)
    later_decision = await cases.decide_challenge(
        GUILD, NOMINEE, challenges[1].id, outcome=AdministrativeOutcome.GRANTED,
        reason="The current version contradicts the charter.", idempotency_key="suspend-latest-law",
    )
    assert later_decision.revision_id == latest.id and later_decision.act_kind is ActKind.LAW
    assert await laws.show(GUILD, latest.code) == original
    assert await laws.list_effective(GUILD) == (original,)
    assert await laws.show(GUILD, latest.code, at=hearing_at.isoformat()) == latest
    assert await laws.show(GUILD, latest.code, at=clock.now.isoformat()) == original
    assert await laws.show(GUILD, latest.code, at=intermediate.effective_at.isoformat()) == intermediate
    for act in (intermediate, latest):
        preserved = await laws.revision(GUILD, OBSERVER, act.id)
        assert preserved.text == act.text and preserved.source_url == act.source_url
    assert set(await cases.list_returns(GUILD, ActKind.LAW)) == {earlier_decision, later_decision}
    assert not await cases.list_returns(GUILD, ActKind.DECREE)

    charge = dict(
        respondent_user_id=CERTIFIER, occurred_at=clock.now.isoformat(), statement="A required notice was omitted.",
        idempotency_key="post-suspension-charge",
    )
    with pytest.raises(CourtCaseError, match=CourtCaseErrorReason.INVALID_LAW):
        await cases.file_charge(GUILD, RECORDER, law_revision_id=latest.id, **charge)
    accepted = await cases.file_charge(GUILD, RECORDER, law_revision_id=original.id, **charge)
    assert accepted.law_revision_id == original.id

    suspended_at = clock.now
    effective_at = suspended_at + timedelta(hours=1)
    with pytest.raises(LawError, match=LawErrorReason.CORRECTION_CASE_REQUIRED):
        await _register_law(laws, number=4, effective_at=effective_at)
    with pytest.raises(LawError, match=LawErrorReason.INVALID_REQUEST):
        await _register_law(laws, number=4, effective_at=effective_at, correction_case_id=earlier_decision.case_id)
    correction = await _register_law(
        laws, number=4, effective_at=effective_at, correction_case_id=later_decision.case_id,
    )
    assert correction.code == latest.code
    assert await laws.show(GUILD, latest.code) == original
    corrected = await laws.confirm(GUILD, DEPUTY_2, correction.id)
    assert await laws.show(GUILD, latest.code) == original
    clock.now = effective_at
    assert await laws.show(GUILD, latest.code) == corrected
    assert await laws.show(GUILD, latest.code, at=suspended_at.isoformat()) == original
    assert set(await cases.list_returns(GUILD, ActKind.LAW)) == {earlier_decision, later_decision}


async def test_decree_returns_to_president_and_does_not_suspend_law_with_same_code(pg_engine, court_docket):
    cases, clock = court_docket
    unit_of_work = PostgresLawUnitOfWork(pg_engine)
    laws = LawService(unit_of_work, guild_id=GUILD, clock=clock)
    decrees = LawService(unit_of_work, guild_id=GUILD, clock=clock, act_kind=ActKind.DECREE)
    law_draft = await _register_law(laws, number=1, effective_at=clock.now)
    law = await laws.confirm(GUILD, DEPUTY_2, law_draft.id)
    decree_draft = await decrees.register(GUILD, PRESIDENT, **_decree_options(clock))
    decree = await decrees.confirm(GUILD, DEPUTY_2, decree_draft.id)
    assert law.code == decree.code
    old_draft = await decrees.register(
        GUILD, PRESIDENT, **(_decree_options(clock) | {
            "effective_at": (clock.now + timedelta(hours=5)).isoformat(),
            "idempotency_key": "pre-suspension-decree-draft",
        }),
    )

    filed = await cases.file_decree_challenge(
        GUILD, RECORDER, decree_revision_id=decree.id, statement="The decree contradicts the Senate law.",
        idempotency_key="return-decree-challenge",
    )
    hearing_at = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, filed.id, hearing_at=hearing_at.isoformat(), idempotency_key="return-decree-hearing",
    )
    clock.now = hearing_at
    decision = await cases.decide_challenge(
        GUILD, NOMINEE, filed.id, outcome=AdministrativeOutcome.GRANTED,
        reason="The decree exceeded authority delegated by the Senate law.", idempotency_key="suspend-decree",
    )
    assert decision == await cases.decision(GUILD, filed.id)
    assert decision.act_kind is ActKind.DECREE and decision.revision_id == decree.id
    assert await cases.list_returns(GUILD, ActKind.DECREE) == (decision,)
    assert not await cases.list_returns(GUILD, ActKind.LAW)
    assert not await decrees.list_effective(GUILD)
    with pytest.raises(LawError, match=LawErrorReason.CORRECTION_CASE_REQUIRED):
        await decrees.confirm(GUILD, DEPUTY_2, old_draft.id)
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await decrees.show(GUILD, decree.code)
    assert await laws.show(GUILD, law.code) == law
    assert (await decrees.revision(GUILD, OBSERVER, decree.id)).text == decree.text
    assert await decrees.show(GUILD, decree.code, at=(clock.now - timedelta(microseconds=1)).isoformat()) == decree

    suspended_at = clock.now
    effective_at = suspended_at + timedelta(hours=1)
    with pytest.raises(LawError, match=LawErrorReason.INVALID_REQUEST):
        await decrees.register(
            GUILD, PRESIDENT, **(_decree_options(clock) | {
                "code": "OTHER-01", "effective_at": effective_at.isoformat(),
                "idempotency_key": "wrong-correction", "correction_case_id": filed.id,
            }),
        )
    correction = await decrees.register(
        GUILD, PRESIDENT, **(_decree_options(clock) | {
            "text": "Corrected published decree within delegated authority.",
            "source_url": f"https://discord.com/channels/{GUILD}/701/901",
            "effective_at": effective_at.isoformat(),
            "idempotency_key": "decree-correction",
            "correction_case_id": filed.id,
        }),
    )
    corrected = await decrees.confirm(GUILD, DEPUTY_2, correction.id)
    assert corrected.correction_case_id == filed.id
    assert not await decrees.list_effective(GUILD)
    clock.now = effective_at
    assert await decrees.show(GUILD, decree.code) == corrected
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await decrees.show(GUILD, decree.code, at=suspended_at.isoformat())
    assert await cases.list_returns(GUILD, ActKind.DECREE) == (decision,)
    assert await laws.show(GUILD, law.code) == law


@pytest_asyncio.fixture
async def appeal_docket(pg_engine, court_docket):
    cases, clock = court_docket
    appeals = CourtAppealService(
        PostgresCourtAppealUnitOfWork(pg_engine), guild_id=GUILD, clock=clock,
        commissioner_user_ids=(RECORDER, CERTIFIER, NOMINEE, OBSERVER), quorum_bp=4000, quorum_min=3,
        poll_hosts={"poll.example.test"},
    )
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    original = await _register_law(laws, number=1, effective_at=clock.now)
    original = await laws.confirm(GUILD, DEPUTY_2, original.id)
    clock.now += timedelta(minutes=10)
    current = await _register_law(laws, number=2, effective_at=clock.now)
    current = await laws.confirm(GUILD, DEPUTY_2, current.id)
    return cases, appeals, laws, clock, original, current


@pytest.mark.parametrize("court_docket", [OBSERVER], indirect=True)
@pytest.mark.parametrize(
    "elected_offices", [{"min_eligible": 7, "quorum_min": 2, "voting_hours": 1, "extension_hours": 1}],
    indirect=True,
)
async def test_seven_player_rehearsal_can_certify_criminal_appeal(pg_engine, court_docket):
    cases, clock = court_docket
    laws = LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    draft = await _register_law(laws, number=1, effective_at=clock.now)
    law = await laws.confirm(GUILD, DEPUTY_2, draft.id)
    case = await cases.file_charge(
        GUILD, OBSERVER, respondent_user_id=PRESIDENT, law_revision_id=law.id,
        occurred_at=clock.now.isoformat(), statement="Rehearsal charge", idempotency_key="rehearsal-charge",
    )
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, case.id, hearing_at=hearing.isoformat(), idempotency_key="rehearsal-hearing",
    )
    clock.now = hearing
    await cases.decide_merits(
        GUILD, NOMINEE, case.id, outcome=MeritsOutcome.GUILTY,
        reason="Rehearsal judgment", idempotency_key="rehearsal-judgment",
    )
    appeals = CourtAppealService(
        PostgresCourtAppealUnitOfWork(pg_engine), guild_id=GUILD, clock=clock,
        commissioner_user_ids=(RECORDER, CERTIFIER), quorum_bp=4000, quorum_min=2,
        poll_hosts={"discord.com"},
    )
    await appeals.file(GUILD, PRESIDENT, case.id)
    opened = await appeals.open(GUILD, RECORDER, case.id, source_url=f"https://discord.com/channels/{GUILD}/701/900")
    clock.now = opened.closes_at
    await appeals.record(GUILD, RECORDER, case.id, round_number=1, participants=4, votes_to_cancel=3)
    result = await appeals.confirm(GUILD, CERTIFIER, case.id, round_number=1)
    assert result.outcome is AppealOutcome.OVERTURNED


@pytest.mark.parametrize("criminal", [False, True])
async def test_merits_decision_can_be_overturned_without_suspending_law(appeal_docket, criminal):
    cases, appeals, laws, clock, _, current = appeal_docket
    if criminal:
        case = await cases.file_charge(
            GUILD, RECORDER, respondent_user_id=PRESIDENT, law_revision_id=current.id,
            occurred_at=clock.now.isoformat(), statement="Filed charge", idempotency_key="merits-case",
        )
    else:
        case = await cases.file_civil(
            GUILD, PRESIDENT, respondent_user_id=DEPUTY_1, statement="Filed claim",
            idempotency_key="merits-case",
        )
    clock.now += timedelta(minutes=1)
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, case.id, hearing_at=hearing.isoformat(), idempotency_key="merits-hearing",
    )
    clock.now = hearing
    payload = dict(
        outcome=MeritsOutcome.GUILTY if criminal else MeritsOutcome.GRANTED,
        reason="Public reasons following hearing", idempotency_key="merits-decision",
    )
    decision = await cases.decide_merits(GUILD, NOMINEE, case.id, **payload)
    assert await cases.decide_merits(GUILD, NOMINEE, case.id, **payload) == decision
    assert await cases.decision(GUILD, case.id) == decision
    assert (await cases.case(GUILD, PRESIDENT, case.id))[0].merits_outcome == payload["outcome"]
    challenge = await cases.file_challenge(
        GUILD, PRESIDENT, law_revision_id=current.id, statement="Challenged the published rule",
        idempotency_key="related-challenge",
    )
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, challenge.id, hearing_at=hearing.isoformat(), idempotency_key="related-hearing",
    )
    clock.now = hearing
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.decide_challenge(
            GUILD, NOMINEE, challenge.id, outcome=AdministrativeOutcome.DENIED,
            reason="Dismiss challenge", idempotency_key=payload["idempotency_key"],
        )
    await cases.decide_challenge(
        GUILD, NOMINEE, challenge.id, outcome=AdministrativeOutcome.DENIED,
        reason="Dismiss challenge", idempotency_key="related-decision",
    )
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.decide_merits(GUILD, NOMINEE, case.id, **(payload | {"idempotency_key": "related-decision"}))
    await appeals.file(GUILD, PRESIDENT, case.id)
    opened = await appeals.open(GUILD, CERTIFIER, case.id, source_url="https://poll.example.test/merits")
    clock.now = opened.closes_at
    await appeals.record(GUILD, CERTIFIER, case.id, round_number=1, participants=4, votes_to_cancel=3)
    result = await appeals.confirm(GUILD, OBSERVER, case.id, round_number=1)
    assert result.outcome is AppealOutcome.OVERTURNED
    assert (await laws.show(GUILD, current.code)).id == current.id


@pytest.mark.parametrize(
    "funded,appeal_outcome,pause",
    [(False, AppealOutcome.OVERTURNED, False), (True, AppealOutcome.OVERTURNED, False),
     (True, AppealOutcome.UPHELD, False), (True, AppealOutcome.NO_QUORUM, False), (False, None, False),
     (True, AppealOutcome.OVERTURNED, True), (True, AppealOutcome.UPHELD, True)],
)
async def test_sentence_persists_with_guilty_verdict(pg_engine, appeal_docket, funded, appeal_outcome, pause):
    cases, appeals, _, clock, _, current = appeal_docket
    case = await cases.file_charge(
        GUILD, RECORDER, respondent_user_id=PRESIDENT, law_revision_id=current.id,
        occurred_at=clock.now.isoformat(), statement="Filed charge", idempotency_key="sentence-case",
    )
    hearing = clock.now + timedelta(hours=1)
    heard = await cases.schedule_hearing(
        GUILD, NOMINEE, case.id, hearing_at=hearing.isoformat(), idempotency_key="sentence-hearing",
    )
    clock.now = hearing
    sentence = CourtSentence("Article 7", SanctionTerm(35, 15, 125))
    decision = MeritsDecision(
        case.guild_id, case.id, CaseKind.CRIMINAL, current.id, MeritsOutcome.GUILTY,
        "Public reasons", heard.scheduled_by_citizen_id, clock.now, sentence,
    )
    unit_of_work = PostgresCourtCaseUnitOfWork(pg_engine)
    with pytest.raises(IntegrityError, match="ck_court_act_decisions_sentence"):
        async with unit_of_work() as repository:
            await repository.add_merits_decision(replace(decision, outcome=MeritsOutcome.ACQUITTED), "sentence")
    ledger = PostgresLedgerUnitOfWork(pg_engine)
    receipts = ReceiptService(PostgresReceiptUnitOfWork(pg_engine), guild_id=GUILD, authorize=lambda *_: None)

    async def set_pause(enabled):
        async with PostgresMaintenanceUnitOfWork(pg_engine)() as repository:
            await repository.set_economy_read_only(
                GUILD, enabled, actor_discord_user_id=901, reason="Test maintenance", at=clock.now,
            )

    async def credit():
        async with ledger() as repository:
            personal = await repository.ensure_account(case.guild_id, AccountKind.PERSONAL, case.respondent_citizen_id)
            issuer = await repository.ensure_account(case.guild_id, AccountKind.ISSUER)
            await repository.post(
                case.guild_id, TransactionKind.STARTER_GRANT, f"starter_grant:{case.respondent_citizen_id}",
                {issuer.id: -70, personal.id: 70}, reason="Test starter funds",
            )
            return personal

    if funded:
        personal = await credit()
        await receipts.set_preference(GUILD, PRESIDENT, locale="ru")
    if pause:
        await set_pause(True)
    payload = dict(outcome=decision.outcome, reason=decision.reason, idempotency_key="sentence")
    with pytest.raises(CourtCaseError, match="^invalid_sentence$"):
        await cases.decide_merits(
            GUILD, NOMINEE, case.id, **(payload | {"outcome": MeritsOutcome.ACQUITTED}), sentence=sentence,
        )
    assert await cases.decide_merits(GUILD, NOMINEE, case.id, **payload, sentence=sentence) == decision
    assert await cases.decide_merits(GUILD, NOMINEE, case.id, **payload, sentence=sentence) == decision
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.decide_merits(
            GUILD, NOMINEE, case.id, **payload, sentence=replace(sentence, fine=SanctionTerm(36, 15, 125)),
        )
    assert await cases.decision(GUILD, case.id) == decision
    with pytest.raises(CourtCaseError, match="^invalid_request$"):
        await cases.decide_merits(
            GUILD, NOMINEE, case.id, outcome=decision.outcome, reason=decision.reason, idempotency_key="sentence",
        )

    async with ledger() as repository:
        personal = await repository.ensure_account(case.guild_id, AccountKind.PERSONAL, case.respondent_citizen_id)
        initial_receipt = await repository.get_post(case.guild_id, TransactionKind.COURT_FINE_HOLD, f"court_fine_hold:{case.id}")
        assert (initial_receipt is not None) == (funded and not pause)
        assert await repository.get_balance(personal.id) == (70 if pause else 35 if funded else 0)
        if not funded:
            with pytest.raises(LedgerError, match=LedgerErrorReason.INSUFFICIENT_FUNDS.value):
                await repository.hold_court_fine(case.guild_id, case.id)
    assert await cases.fine_status(GUILD, PRESIDENT, case.id) is (
        CourtFineStatus.HELD if funded and not pause else CourtFineStatus.PENDING
    )
    assert await cases.fine_status(GUILD, NOMINEE, case.id) is None
    assert await cases.fine_status(GUILD, PRESIDENT, case.id + 999) is None
    if not funded:
        await credit()
        await appeals.reconcile_fines()
    if pause:
        await appeals.reconcile_fines()
        await set_pause(False)
        await appeals.reconcile_fines()

    async def hold():
        async with ledger() as repository:
            return await repository.hold_court_fine(case.guild_id, case.id)

    first, second = await asyncio.gather(hold(), hold())
    assert first.transaction_id == second.transaction_id
    assert not first.created and not second.created

    async def check_private_receipt(transaction_id, *, kind, delta):
        receipt = await receipts.get_receipt(GUILD, PRESIDENT, transaction_id)
        assert receipt is not None and (receipt.kind, receipt.delta) == (kind, delta)
        for other_user in (NOMINEE, OBSERVER):
            assert await receipts.get_receipt(GUILD, other_user, transaction_id) is None

    await check_private_receipt(first.transaction_id, kind=TransactionKind.COURT_FINE_HOLD, delta=-35)
    assert await cases.fine_status(GUILD, PRESIDENT, case.id) is CourtFineStatus.HELD
    async with ledger() as repository:
        assert await repository.get_balance(personal.id) == 35
        history = await repository.get_history(personal.id, 5)
        assert [(entry.kind, entry.delta) for entry in history][:1] == [(TransactionKind.COURT_FINE_HOLD, -35)]

    async def check_fine_report(expected, as_of):
        # The test clock controls court procedure while the ledger normally uses PostgreSQL time.
        beginning = min(datetime.now(UTC), decision.decided_at) - timedelta(days=7)
        async with PostgresReportUnitOfWork(pg_engine)() as repository:
            report = await repository.read_report(GUILD, ReportWindow(beginning.date(), beginning, as_of), as_of=as_of)
        assert (report.court_fines, report.other_income, report.total_income, report.closing_balance) == (
            expected, 0, expected, expected,
        )

    await check_fine_report(0, max(clock.now, datetime.now(UTC)) + timedelta(seconds=1))
    if appeal_outcome is None:
        clock.now = decision.decided_at + timedelta(hours=24)
        await appeals.reconcile_fines()
        async with ledger() as repository:
            treasury = await repository.ensure_account(case.guild_id, AccountKind.TREASURY)
            assert await repository.get_balance(treasury.id) == 0
        clock.now += timedelta(seconds=1)
        await asyncio.gather(appeals.reconcile_fines(), appeals.reconcile_fines())
        assert await cases.fine_status(GUILD, PRESIDENT, case.id) is CourtFineStatus.SETTLED
        await check_fine_report(0, clock.now)
        await check_fine_report(35, clock.now + timedelta(seconds=1))
        async with ledger() as repository:
            assert await repository.get_balance(treasury.id) == 35
            assert await repository.get_balance(personal.id) == 35
        return

    await appeals.file(GUILD, PRESIDENT, case.id)
    opened = await appeals.open(GUILD, CERTIFIER, case.id, source_url="https://poll.example.test/fine")
    clock.now = opened.closes_at
    await appeals.reconcile_fines()
    async with ledger() as repository:
        treasury = await repository.ensure_account(case.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(treasury.id) == 0
    overturned = appeal_outcome is AppealOutcome.OVERTURNED
    participants, votes = (2, 2) if appeal_outcome is AppealOutcome.NO_QUORUM else (4, 3 if overturned else 2)
    await appeals.record(
        GUILD, CERTIFIER, case.id, round_number=1, participants=participants, votes_to_cancel=votes,
    )
    if pause:
        await set_pause(True)
    result = await appeals.confirm(GUILD, OBSERVER, case.id, round_number=1)
    round_number = 1
    if appeal_outcome is AppealOutcome.NO_QUORUM:
        assert result.round_number == 2 and result.outcome is None
        async with ledger() as repository:
            treasury = await repository.ensure_account(case.guild_id, AccountKind.TREASURY)
            assert await repository.get_balance(treasury.id) == 0
        clock.now = result.closes_at
        round_number = 2
        await appeals.record(
            GUILD, CERTIFIER, case.id, round_number=2, participants=participants, votes_to_cancel=votes,
        )
        result = await appeals.confirm(GUILD, OBSERVER, case.id, round_number=2)
    assert result.outcome is appeal_outcome
    assert await appeals.confirm(GUILD, OBSERVER, case.id, round_number=round_number) == result
    if pause:
        await appeals.reconcile_fines()
        assert await cases.fine_status(GUILD, PRESIDENT, case.id) is (
            CourtFineStatus.REFUND_PENDING if overturned else CourtFineStatus.HELD
        )
        await check_fine_report(0, max(clock.now, datetime.now(UTC)) + timedelta(seconds=1))
        async with ledger() as repository:
            assert await repository.get_balance(personal.id) == 35
        await set_pause(False)
        await asyncio.gather(appeals.reconcile_fines(), appeals.reconcile_fines())
    await check_fine_report(0 if overturned else 35, max(clock.now, datetime.now(UTC)) + timedelta(seconds=1))
    assert await cases.fine_status(GUILD, PRESIDENT, case.id) is (
        CourtFineStatus.REFUNDED if overturned else CourtFineStatus.SETTLED
    )
    async with ledger() as repository:
        treasury = await repository.ensure_account(case.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(personal.id) == (70 if overturned else 35)
        assert await repository.get_balance(treasury.id) == (0 if overturned else 35)
        kind = TransactionKind.COURT_FINE_REFUND if overturned else TransactionKind.COURT_FINE_SETTLE
        closing = await repository.get_post(case.guild_id, kind, f"{kind.value.lower()}:{case.id}")
    assert closing is not None
    if overturned:
        await check_private_receipt(closing.transaction_id, kind=kind, delta=35)
    else:
        assert await receipts.get_receipt(GUILD, PRESIDENT, closing.transaction_id) is None
    if funded:
        delivered = []

        async def send(notification):
            delivered.append((notification.recipient_discord_user_id, notification.transaction_id, notification.locale))
            return 901 + len(delivered)

        expected = [(PRESIDENT, first.transaction_id, "ru")]
        if overturned:
            expected.append((PRESIDENT, closing.transaction_id, "ru"))
        for _ in expected:
            assert await receipts.run_due(send)
        assert not await receipts.run_due(send)
        assert delivered == expected
    if overturned:
        with pytest.raises(LedgerError, match=LedgerErrorReason.INVALID_POSTING.value):
            await hold()


async def test_overturned_uncollected_fine_cannot_be_held_after_top_up(pg_engine, appeal_docket):
    cases, appeals, _, clock, _, law = appeal_docket
    case = await cases.file_charge(
        GUILD, RECORDER, respondent_user_id=PRESIDENT, law_revision_id=law.id,
        occurred_at=clock.now.isoformat(), statement="Fixture charge", idempotency_key="cancelled-fine-case",
    )
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, case.id, hearing_at=hearing.isoformat(), idempotency_key="cancelled-fine-hearing",
    )
    clock.now = hearing
    decision = await cases.decide_merits(
        GUILD, NOMINEE, case.id, outcome=MeritsOutcome.GUILTY, reason="Fixture judgment",
        idempotency_key="cancelled-fine-judgment", sentence=CourtSentence("7", fine=SanctionTerm(25, 10, 50)),
    )
    assert await cases.fine_status(GUILD, PRESIDENT, case.id) is CourtFineStatus.PENDING
    await appeals.file(GUILD, PRESIDENT, case.id)
    poll = await appeals.open(GUILD, CERTIFIER, case.id, source_url="https://poll.example.test/cancel-fine")
    clock.now = poll.closes_at
    await appeals.record(GUILD, CERTIFIER, case.id, round_number=1, participants=4, votes_to_cancel=3)
    assert (await appeals.confirm(GUILD, OBSERVER, case.id, round_number=1)).outcome is AppealOutcome.OVERTURNED

    ledger = PostgresLedgerUnitOfWork(pg_engine)
    wallet = WalletService(
        ledger, guild_id=GUILD, authorize=lambda *_: None,
        funding=WalletFunding(starter_grant=100, treasury_genesis=0), clock=clock,
    )
    await wallet.fund_citizen(GUILD, DEPUTY_1)
    await wallet.transfer(GUILD, DEPUTY_1, PRESIDENT, 30, idempotency_key="top-up-after-overturn")
    await asyncio.gather(appeals.reconcile_fines(), appeals.reconcile_fines())
    assert await cases.fine_status(GUILD, PRESIDENT, case.id) is CourtFineStatus.CANCELLED

    async with pg_engine.begin() as connection:
        escrow_id = await connection.scalar(insert(FinancialAccountRow).values(
            guild_id=case.guild_id, kind=AccountKind.ESCROW.value, court_case_id=case.id,
        ).returning(FinancialAccountRow.id))
    with pytest.raises(IntegrityError, match="Cannot hold an overturned court fine"):
        async with ledger() as repository:
            account = await repository.ensure_account(case.guild_id, AccountKind.PERSONAL, case.respondent_citizen_id)
            await repository.post(
                case.guild_id, TransactionKind.COURT_FINE_HOLD, f"court_fine_hold:{case.id}",
                {account.id: -25, escrow_id: 25}, actor_citizen_id=decision.judge_citizen_id, reason=decision.reason,
            )
    async with ledger() as repository:
        assert await repository.get_balance(account.id) == 30
        assert await repository.get_balance(escrow_id) == 0


@pytest.mark.parametrize("overturned", [False, True])
async def test_custody_restricts_new_transfers_and_allows_appeal(pg_engine, appeal_docket, overturned):
    cases, appeals, _, clock, _, current = appeal_docket
    wallet = WalletService(
        PostgresLedgerUnitOfWork(pg_engine), guild_id=GUILD, authorize=lambda *_: None,
        funding=WalletFunding(starter_grant=100, treasury_genesis=0), clock=clock,
    )
    case = await cases.file_charge(
        GUILD, RECORDER, respondent_user_id=PRESIDENT, law_revision_id=current.id,
        occurred_at=clock.now.isoformat(), statement="Filed charge", idempotency_key="custody-case",
    )
    hearing = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(GUILD, NOMINEE, case.id, hearing_at=hearing.isoformat(), idempotency_key="custody-hearing")
    old = await wallet.transfer(GUILD, PRESIDENT, DEPUTY_1, 10, idempotency_key="before-custody")
    clock.now = hearing
    fine = SanctionTerm(25, 10, 30) if overturned else None
    decision = await cases.decide_merits(
        GUILD, NOMINEE, case.id, outcome=MeritsOutcome.GUILTY,
        reason="Public reasons", idempotency_key="custody",
        sentence=CourtSentence("7", fine=fine, custody=SanctionTerm(2, 1, 4)),
    )
    assert decision.sentence.custody.value == 2
    assert await cases.custody_role_assignments() == {PRESIDENT: True}
    with pytest.raises(LedgerError, match=LedgerErrorReason.IN_CUSTODY.value):
        await wallet.prepare_transfer(GUILD, PRESIDENT, DEPUTY_1, 10)
    with pytest.raises(LedgerError, match=LedgerErrorReason.IN_CUSTODY.value):
        await wallet.transfer(GUILD, PRESIDENT, DEPUTY_1, 10, idempotency_key="after-custody")
    replay = await wallet.transfer(GUILD, PRESIDENT, DEPUTY_1, 10, idempotency_key="before-custody")
    assert old.transaction_id == replay.transaction_id and not replay.created
    await wallet.transfer(GUILD, DEPUTY_1, PRESIDENT, 10, idempotency_key="incoming-custody")
    assert await wallet.balance(GUILD, PRESIDENT) == (75 if overturned else 100)
    if overturned:
        await appeals.file(GUILD, PRESIDENT, case.id)
        opened = await appeals.open(GUILD, CERTIFIER, case.id, source_url="https://poll.example.test/custody")
        clock.now = opened.closes_at
        await appeals.record(GUILD, CERTIFIER, case.id, round_number=1, participants=4, votes_to_cancel=3)
        result = await appeals.confirm(GUILD, OBSERVER, case.id, round_number=1)
        assert result.outcome is AppealOutcome.OVERTURNED
        assert await wallet.balance(GUILD, PRESIDENT) == 100
    else:
        clock.now = hearing + timedelta(hours=2)
    assert await cases.custody_role_assignments() == {PRESIDENT: False}
    assert (await wallet.transfer(GUILD, PRESIDENT, DEPUTY_1, 10, idempotency_key="after-custody")).created


async def test_custody_and_held_fine_remain_enforceable_after_restore(pg_engine, appeal_docket, tmp_path):
    if not os.getenv("CIVITAS_TEST_PG_BIN"):
        pytest.skip("Set CIVITAS_TEST_PG_BIN to verify backup and restore with PostgreSQL client tools")
    cases, _, _, clock, _, law = appeal_docket
    presentation = PresentationService(PostgresPresentationUnitOfWork(pg_engine), guild_id=GUILD)
    await presentation.initialize(GuildPresentation())
    assert await presentation.set_binding(
        GUILD, 901, administrator=True, binding=PresentationBinding.CUSTODY_ROLE, value=902,
    )
    funding = WalletFunding(starter_grant=100, treasury_genesis=0)
    wallet = WalletService(
        PostgresLedgerUnitOfWork(pg_engine), guild_id=GUILD, authorize=lambda *_: None, funding=funding, clock=clock,
    )
    await wallet.fund_citizen(GUILD, PRESIDENT)
    case = await cases.file_charge(
        GUILD, RECORDER, respondent_user_id=PRESIDENT, law_revision_id=law.id,
        occurred_at=clock.now.isoformat(), statement="Fixture charge", idempotency_key="restore-court-case",
    )
    hearing_at = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, case.id, hearing_at=hearing_at.isoformat(), idempotency_key="restore-court-hearing",
    )
    clock.now = hearing_at
    decision = await cases.decide_merits(
        GUILD, NOMINEE, case.id, outcome=MeritsOutcome.GUILTY, reason="Fixture judgment",
        idempotency_key="restore-court-judgment",
        sentence=CourtSentence("7", fine=SanctionTerm(25, 10, 50), custody=SanctionTerm(2, 1, 4)),
    )
    assert await wallet.balance(GUILD, PRESIDENT) == 75
    pending = await cases.file_civil(
        GUILD, PRESIDENT, respondent_user_id=DEPUTY_1, statement="Fixture evidence for the substitute judge.",
        idempotency_key="restore-substitute-case",
    )
    await cases.recuse(GUILD, NOMINEE, pending.id, role="JUDGE")
    nominee = await cases.propose_case_judge(
        GUILD, PRESIDENT, pending.id, OBSERVER, idempotency_key="restore-substitute-judge",
    )
    assert not await cases.approve_case_judge(GUILD, DEPUTY_1, nominee)
    assert await cases.approve_case_judge(GUILD, DEPUTY_2, nominee)
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    removal = await court.propose_removal(GUILD, PRESIDENT, reason="Fixture removal", idempotency_key="restore-removal")
    await court.approve_removal(GUILD, DEPUTY_1, removal.id)
    completed_removal = await court.approve_removal(GUILD, DEPUTY_2, removal.id)
    async with restore_database(pg_engine, tmp_path) as restored:
        restored_court = CourtService(PostgresCourtUnitOfWork(restored), guild_id=GUILD, clock=clock)
        assert await restored_court.current(GUILD) is None
        historical = await restored_court.get_nomination(GUILD, removal.nomination_id)
        assert historical.removed_at == completed_removal.second_signed_at == clock.now
        assert await restored_court.approve_removal(GUILD, DEPUTY_2, removal.id) == completed_removal
        successor = await restored_court.nominate(
            GUILD, PRESIDENT, candidate_user_id=OBSERVER, reason="Fixture successor",
            idempotency_key="restore-successor",
        )
        await restored_court.approve(GUILD, DEPUTY_1, successor.id, reason="First signature")
        await restored_court.approve(GUILD, DEPUTY_2, successor.id, reason="Second signature")
        assert (await restored_court.current(GUILD)).id == successor.id
        restored_cases = CourtCaseService(PostgresCourtCaseUnitOfWork(restored), guild_id=GUILD, clock=clock)
        restored_presentation = PresentationService(PostgresPresentationUnitOfWork(restored), guild_id=GUILD)
        assert await restored_presentation.current() == GuildPresentation(custody_role_id=902)
        assert await restored_cases.decision(GUILD, case.id) == decision
        assert await restored_cases.custody_role_assignments() == {PRESIDENT: True}
        await restored_cases.approve_case_judge(GUILD, DEPUTY_2, nominee)
        assert not (await restored_cases.case(GUILD, NOMINEE, pending.id))[1]
        assert await restored_cases.case(GUILD, OBSERVER, pending.id) == (pending, True)
        hearing_at = (clock.now + timedelta(hours=1)).isoformat()
        with pytest.raises(CourtCaseError, match="^judge_required$"):
            await restored_cases.schedule_hearing(
                GUILD, NOMINEE, pending.id, hearing_at=hearing_at, idempotency_key="restore-recused-hearing",
            )
        updated = await restored_cases.schedule_hearing(
            GUILD, OBSERVER, pending.id, hearing_at=hearing_at, idempotency_key="restore-substitute-hearing",
        )
        assert updated.hearing_at == datetime.fromisoformat(hearing_at)
        ledger = PostgresLedgerUnitOfWork(restored)
        async with ledger() as repository:
            receipt = await repository.hold_court_fine(case.guild_id, case.id)
            assert not receipt.created
        restored_wallet = WalletService(ledger, guild_id=GUILD, authorize=lambda *_: None, funding=funding, clock=clock)
        with pytest.raises(LedgerError, match=LedgerErrorReason.IN_CUSTODY.value):
            await restored_wallet.prepare_transfer(GUILD, PRESIDENT, DEPUTY_1, 1)
        clock.now += timedelta(hours=25)
        restored_appeals = CourtAppealService(
            PostgresCourtAppealUnitOfWork(restored), guild_id=GUILD, clock=clock,
            commissioner_user_ids=(CERTIFIER, OBSERVER), quorum_bp=4000, quorum_min=3,
            poll_hosts={"poll.example.test"},
        )
        await restored_appeals.reconcile_fines()
        await restored_appeals.reconcile_fines()
        assert await restored_cases.custody_role_assignments() == {PRESIDENT: False}
        assert await restored_wallet.balance(GUILD, PRESIDENT) == 75
        async with ledger() as repository:
            treasury = await repository.ensure_account(case.guild_id, AccountKind.TREASURY)
            assert await repository.get_balance(treasury.id) == 25


async def _grant_appealable_challenge(cases, clock, revision_id, *, key):
    challenge = await cases.file_challenge(
        GUILD, PRESIDENT, law_revision_id=revision_id, statement="The registration rule affected my rights.",
        idempotency_key=f"appeal-challenge-{key}",
    )
    hearing_at = clock.now + timedelta(hours=1)
    await cases.schedule_hearing(
        GUILD, NOMINEE, challenge.id, hearing_at=hearing_at.isoformat(), idempotency_key=f"appeal-hearing-{key}",
    )
    clock.now = hearing_at
    return await cases.decide_challenge(
        GUILD, NOMINEE, challenge.id, outcome=AdministrativeOutcome.GRANTED,
        reason="The revision imposed requirements beyond the delegated authority.",
        idempotency_key=f"appeal-decision-{key}",
    )


async def test_appeal_filing_deadline_and_frozen_electorate_with_independent_signatures(pg_engine, appeal_docket):
    cases, appeals, laws, clock, original, current = appeal_docket
    decision = await _grant_appealable_challenge(cases, clock, current.id, key="timely")
    late = await _grant_appealable_challenge(cases, clock, original.id, key="late")
    clock.now = decision.decided_at + timedelta(hours=24)
    with pytest.raises(CourtAppealError, match="^party_required$"):
        await appeals.file(GUILD, OBSERVER, decision.case_id)
    filed = await appeals.file(GUILD, PRESIDENT, decision.case_id)
    assert filed.filed_at == clock.now and filed.source_url is None
    assert await appeals.file(GUILD, PRESIDENT, decision.case_id) == filed
    with pytest.raises(CourtAppealError, match="^already_filed$"):
        await appeals.file(GUILD, RECORDER, decision.case_id)

    clock.now = late.decided_at + timedelta(hours=24, microseconds=1)
    with pytest.raises(CourtAppealError, match="^deadline_passed$"):
        await appeals.file(GUILD, PRESIDENT, late.case_id)

    source = "https://poll.example.test/appeal-timely"
    with pytest.raises(CourtAppealError, match="^invalid_source$"):
        await appeals.open(GUILD, CERTIFIER, decision.case_id, source_url="https://foreign.example.test/appeal-timely")
    for actor in (NOMINEE, RECORDER):
        with pytest.raises(CourtAppealError, match="^conflict_of_interest$"):
            await appeals.open(GUILD, actor, decision.case_id, source_url=source)
    opened = await appeals.open(GUILD, CERTIFIER, decision.case_id, source_url=source)
    assert opened.source_url == source and opened.opened_at == clock.now
    assert opened.closes_at == opened.opened_at + timedelta(hours=48)
    assert opened.quorum == 3 and opened.eligible_count == 7 and opened.round_number == 1
    original_voters = set(await appeals.voters(GUILD, OBSERVER, decision.case_id))
    assert original_voters == {PRESIDENT, DEPUTY_1, DEPUTY_2, NOMINEE, RECORDER, CERTIFIER, OBSERVER}
    assert (await appeals.show(GUILD, decision.case_id)).eligible_user_ids == ()
    with pytest.raises(CourtAppealError, match="^commissioner_required$"):
        await appeals.voters(GUILD, DEPUTY_2, decision.case_id)

    admission = AdmissionService(PostgresAdmissionUnitOfWork(pg_engine), guild_id=GUILD)
    await admission.set_allowed(
        GUILD, PRESIDENT, administrator=True, target_user_id=DEPUTY_1, allowed=False, reason="Admission revoked after opening",
    )
    new_voter = 208
    await admission.set_allowed(GUILD, PRESIDENT, administrator=True, target_user_id=new_voter, allowed=True)
    citizens = CitizensService(PostgresGovernmentUnitOfWork(pg_engine), guild_id=GUILD, authorize_admission=admission.authorize)
    await citizens.register(GUILD, new_voter)
    assert set(await appeals.voters(GUILD, OBSERVER, decision.case_id)) == original_voters
    assert await appeals.open(GUILD, CERTIFIER, decision.case_id, source_url=source) == opened

    poll = dict(round_number=1, participants=4, votes_to_cancel=2)
    with pytest.raises(CourtAppealError, match="^voting_in_progress$"):
        await appeals.record(GUILD, CERTIFIER, decision.case_id, **poll)
    clock.now = opened.closes_at
    with pytest.raises(CourtAppealError, match="^invalid_tally$"):
        await appeals.record(GUILD, CERTIFIER, decision.case_id, **(poll | {"participants": 8}))
    first_signature = await appeals.record(GUILD, CERTIFIER, decision.case_id, **poll)
    assert first_signature.participants == 4 and first_signature.votes_to_cancel == 2
    pending = await appeals.show(GUILD, decision.case_id)
    assert pending.participants is None and pending.votes_to_cancel is None and pending.outcome is None
    with pytest.raises(CourtAppealError, match="^independent_confirmation$"):
        await appeals.confirm(GUILD, CERTIFIER, decision.case_id, round_number=1)

    upheld = await appeals.confirm(GUILD, OBSERVER, decision.case_id, round_number=1)
    assert upheld.outcome is AppealOutcome.UPHELD
    assert (upheld.participants, upheld.votes_to_cancel) == (4, 2)
    public = await appeals.show(GUILD, decision.case_id)
    assert not public.eligible_user_ids and public.eligible_count == 7
    assert public.outcome is AppealOutcome.UPHELD
    assert not await laws.list_effective(GUILD)


async def test_appeal_gets_one_extension_and_keeps_suspension_after_final_no_quorum(appeal_docket):
    cases, appeals, laws, clock, original, current = appeal_docket
    decision = await _grant_appealable_challenge(cases, clock, current.id, key="low-quorum")
    await appeals.file(GUILD, PRESIDENT, decision.case_id)
    opened = await appeals.open(GUILD, CERTIFIER, decision.case_id, source_url="https://poll.example.test/appeal-low-quorum")

    clock.now = opened.closes_at
    await appeals.record(GUILD, CERTIFIER, decision.case_id, round_number=1, participants=2, votes_to_cancel=2)
    clock.now += timedelta(minutes=5)
    extended = await appeals.confirm(GUILD, OBSERVER, decision.case_id, round_number=1)
    assert extended.round_number == 2 and extended.outcome is None
    assert extended.closes_at == clock.now + timedelta(hours=24)
    assert extended.source_url == opened.source_url and extended.quorum == opened.quorum
    assert await appeals.confirm(GUILD, OBSERVER, decision.case_id, round_number=1) == extended
    assert await laws.show(GUILD, current.code) == original

    clock.now = extended.closes_at
    await appeals.record(GUILD, CERTIFIER, decision.case_id, round_number=2, participants=1, votes_to_cancel=1)
    pending = await appeals.show(GUILD, decision.case_id)
    assert (pending.participants, pending.votes_to_cancel) == (2, 2)
    final = await appeals.confirm(GUILD, OBSERVER, decision.case_id, round_number=2)
    assert final.round_number == 2 and final.closes_at == extended.closes_at
    assert final.outcome is AppealOutcome.NO_QUORUM and final.resolved_at == clock.now
    assert (final.participants, final.votes_to_cancel) == (1, 1)
    assert await laws.show(GUILD, current.code) == original
    assert await cases.list_returns(GUILD, ActKind.LAW) == (decision,)


async def test_appeal_reversal_restores_only_its_own_suspension_and_preserves_history(appeal_docket):
    cases, appeals, laws, clock, original, current = appeal_docket
    first_decision = await _grant_appealable_challenge(cases, clock, current.id, key="first-suspension")
    second_decision = await _grant_appealable_challenge(cases, clock, current.id, key="second-suspension")
    open_polls = []
    for decision in (first_decision, second_decision):
        await appeals.file(GUILD, PRESIDENT, decision.case_id)
        if open_polls:
            with pytest.raises(CourtAppealError, match="^source_in_use$"):
                await appeals.open(GUILD, CERTIFIER, decision.case_id, source_url=open_polls[0].source_url)
        open_polls.append(await appeals.open(
            GUILD, CERTIFIER, decision.case_id, source_url=f"https://poll.example.test/appeal-{decision.case_id}",
        ))
    assert await laws.show(GUILD, current.code) == original
    assert set(await cases.list_returns(GUILD, ActKind.LAW)) == {first_decision, second_decision}

    closes_at = max(poll.closes_at for poll in open_polls)
    correction_at = closes_at + timedelta(seconds=30)
    draft = await _register_law(
        laws, number=3, effective_at=correction_at, correction_case_id=second_decision.case_id,
    )
    correction = await laws.confirm(GUILD, DEPUTY_2, draft.id)
    clock.now = closes_at
    for decision in (first_decision, second_decision):
        await appeals.record(GUILD, CERTIFIER, decision.case_id, round_number=1, participants=4, votes_to_cancel=3)
    overturned, replay = await asyncio.gather(*[
        appeals.confirm(GUILD, OBSERVER, first_decision.case_id, round_number=1) for _ in range(2)
    ])
    assert overturned == replay and overturned.outcome is AppealOutcome.OVERTURNED
    assert await laws.show(GUILD, current.code) == original
    assert await cases.list_returns(GUILD, ActKind.LAW) == (second_decision,)

    clock.now += timedelta(minutes=1)
    restored = await appeals.confirm(GUILD, OBSERVER, second_decision.case_id, round_number=1)
    assert restored.outcome is AppealOutcome.OVERTURNED and restored.resolved_at == clock.now
    assert await laws.show(GUILD, current.code) == correction
    assert await laws.list_effective(GUILD) == (correction,)
    assert await laws.show(GUILD, current.code, at=(correction_at - timedelta(microseconds=1)).isoformat()) == original
    assert await laws.show(GUILD, current.code, at=(first_decision.decided_at - timedelta(microseconds=1)).isoformat()) == current
    assert await laws.show(GUILD, current.code, at=clock.now.isoformat()) == correction
    assert not await cases.list_returns(GUILD, ActKind.LAW)
    assert await cases.decision(GUILD, first_decision.case_id) == first_decision
    assert await cases.decision(GUILD, second_decision.case_id) == second_decision
