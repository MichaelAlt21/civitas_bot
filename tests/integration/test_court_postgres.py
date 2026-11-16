import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from civitas.app.admission import AdmissionService
from civitas.app.citizens import CitizensService
from civitas.app.court import CourtError, CourtErrorReason, CourtService
from civitas.app.court_appeals import AppealOutcome, CourtAppealError, CourtAppealService
from civitas.app.court_cases import (
    AdministrativeOutcome,
    CaseKind,
    CaseSummary,
    CourtCaseError,
    CourtCaseErrorReason,
    CourtCaseService,
)
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.law import ActKind, LawError, LawErrorReason, LawService
from civitas.app.prosecutor import ProsecutorAction, ProsecutorError, ProsecutorErrorReason, ProsecutorService
from civitas.domain.government import OfficeKind
from civitas.infrastructure.db.admission import PostgresAdmissionUnitOfWork
from civitas.infrastructure.db.court import PostgresCourtUnitOfWork
from civitas.infrastructure.db.court_appeals import PostgresCourtAppealUnitOfWork
from civitas.infrastructure.db.court_cases import PostgresCourtCaseUnitOfWork
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.law import PostgresLawUnitOfWork
from civitas.infrastructure.db.prosecutor import PostgresProsecutorUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = 101
PRESIDENT, DEPUTY_1, DEPUTY_2, NOMINEE, RECORDER, CERTIFIER, OBSERVER = range(201, 208)


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 15, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest_asyncio.fixture
async def elected_offices(pg_engine):
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
        rules=ElectionRules(
            min_eligible=3, quorum_bp=4000, quorum_min=3, parliament_seats=2, term_days=28,
            voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
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
async def court_docket(pg_engine, elected_offices):
    _, clock = elected_offices
    court = CourtService(PostgresCourtUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    justice = await court.nominate(
        GUILD, PRESIDENT, candidate_user_id=NOMINEE, reason="Nominate judge", idempotency_key="docket-judge",
    )
    await court.approve(GUILD, DEPUTY_1, justice.id, reason="First judicial signature")
    await court.approve(GUILD, DEPUTY_2, justice.id, reason="Second judicial signature")

    prosecutor = ProsecutorService(PostgresProsecutorUnitOfWork(pg_engine), guild_id=GUILD, clock=clock)
    motion = await prosecutor.propose(
        GUILD, PRESIDENT, action=ProsecutorAction.APPOINT, candidate_user_id=RECORDER,
        reason="Nominate prosecutor", idempotency_key="docket-prosecutor",
    )
    await prosecutor.approve(GUILD, DEPUTY_1, motion.id, reason="First prosecutor signature")
    await prosecutor.approve(GUILD, DEPUTY_2, motion.id, reason="Second prosecutor signature")

    return CourtCaseService(PostgresCourtCaseUnitOfWork(pg_engine), guild_id=GUILD, clock=clock), clock


async def _register_law(laws, *, number, effective_at):
    return await laws.register(
        GUILD, DEPUTY_1, code="PROC-01", title=f"Notices revision {number}",
        text=f"Published registration requirements, revision {number}.",
        source_url=f"https://discord.com/channels/{GUILD}/701/{800 + number}",
        effective_at=effective_at.isoformat(), idempotency_key=f"docket-law-{number}",
    )


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
    replacement = await prosecutor.propose(
        GUILD, PRESIDENT, action=ProsecutorAction.APPOINT, candidate_user_id=OBSERVER,
        reason="Nominate replacement", idempotency_key="docket-replacement-prosecutor",
    )
    await prosecutor.approve(GUILD, DEPUTY_1, replacement.id, reason="First replacement signature")
    await prosecutor.approve(GUILD, DEPUTY_2, replacement.id, reason="Second replacement signature")
    assert await cases.case(GUILD, OBSERVER, filed.id) == (filed, True)
    assert await cases.case(GUILD, RECORDER, filed.id) == (summary, False)


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
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await decrees.show(GUILD, decree.code)
    assert await laws.show(GUILD, law.code) == law
    assert (await decrees.revision(GUILD, OBSERVER, decree.id)).text == decree.text
    assert await decrees.show(GUILD, decree.code, at=(clock.now - timedelta(microseconds=1)).isoformat()) == decree


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

    clock.now = max(poll.closes_at for poll in open_polls)
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
    assert await laws.show(GUILD, current.code) == current
    assert await laws.list_effective(GUILD) == (current,)
    assert await laws.show(GUILD, current.code, at=(clock.now - timedelta(microseconds=1)).isoformat()) == original
    assert await laws.show(GUILD, current.code, at=(first_decision.decided_at - timedelta(microseconds=1)).isoformat()) == current
    assert await laws.show(GUILD, current.code, at=clock.now.isoformat()) == current
    assert not await cases.list_returns(GUILD, ActKind.LAW)
    assert await cases.decision(GUILD, first_decision.case_id) == first_decision
    assert await cases.decision(GUILD, second_decision.case_id) == second_decision
