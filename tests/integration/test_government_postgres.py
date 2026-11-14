import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.admin_audit import AdminAuditService
from civitas.app.admin_operators import AdminOperatorPolicy
from civitas.app.citizens import MAX_DISCORD_ID, CitizensService
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding
from civitas.domain.citizens import CitizenStatus
from civitas.domain.government import Election, ElectionStatus, OfficeKind
from civitas.domain.jobs import BudgetCaps
from civitas.domain.treasury import ContractStatus
from civitas.infrastructure.db.admin_audit import PostgresAdminAuditUnitOfWork
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.government_models import (
    ElectionCertificationRow,
    ElectionCrisisConfirmationRow,
    ElectionCrisisProposalRow,
    ElectionPollSourceRow,
    ElectionProtocolRow,
    ElectionRow,
    PublicOfficeRow,
)
from civitas.infrastructure.db.models import CitizenRow

pytestmark = pytest.mark.asyncio

GUILD = MAX_DISCORD_ID - 1
WINNER, RUNNER_UP = MAX_DISCORD_ID, MAX_DISCORD_ID - 2
RECORDER, REVIEWER, SECOND_REVIEWER, ORDINARY = 11, 12, 13, 14
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
RULES = ElectionRules(
    min_eligible=4, quorum_bp=4000, quorum_min=3, parliament_seats=2, term_days=28,
    voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
)
SOURCE = "https://poll.example.test/president"


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


async def _setup(engine: AsyncEngine) -> tuple[PostgresGovernmentUnitOfWork, GovernmentService, Clock]:
    unit_of_work = PostgresGovernmentUnitOfWork(engine)
    participants = (RECORDER, REVIEWER, SECOND_REVIEWER, ORDINARY, WINNER, RUNNER_UP)
    citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids=participants)
    for user_id in participants:
        await citizens.register(GUILD, user_id)
    clock = Clock()
    service = GovernmentService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize,
        commissioner_user_ids={RECORDER, REVIEWER, SECOND_REVIEWER}, rules=RULES, clock=clock,
    )
    return unit_of_work, service, clock


async def _open(
    service: GovernmentService, *, key: str, actor: int = RECORDER, runoff_of_id: int | None = None,
) -> Election:
    return await service.open_election(
        GUILD, actor, office=OfficeKind.PRESIDENT, candidate_user_ids=(WINNER, RUNNER_UP),
        reason="Open presidential poll", idempotency_key=key, runoff_of_id=runoff_of_id,
    )


async def _record(
    service: GovernmentService, election: Election, tallies: dict[int, int], *, source_url: str = SOURCE,
) -> Election:
    return await service.record_protocol(
        GUILD, RECORDER, election.id, source_url=source_url, participants=sum(tallies.values()),
        tallies=tallies, reason="Manual poll transcription",
    )


async def _certify(service: GovernmentService, election: Election, actor: int = REVIEWER) -> Election:
    return await service.certify_election(GUILD, actor, election.id, reason="Independent poll verification")


async def test_certified_mandates_gate_treasury_and_expiry(pg_engine: AsyncEngine) -> None:
    uow, government, clock = await _setup(pg_engine)
    president = await _open(government, key="treasury-president")
    parliament = await government.open_election(
        GUILD, RECORDER, office=OfficeKind.PARLIAMENT, candidate_user_ids=(RUNNER_UP, ORDINARY),
        reason="Elect parliamentary reviewers", idempotency_key="treasury-parliament",
    )
    clock.now = president.closes_at
    await _record(government, president, {WINNER: 2, RUNNER_UP: 1})
    await _certify(government, president)
    await government.record_protocol(
        GUILD, RECORDER, parliament.id, source_url="https://poll.example.test/parliament", participants=3,
        tallies={RUNNER_UP: 2, ORDINARY: 1}, reason="Manual parliamentary protocol",
    )
    await _certify(government, parliament)
    citizens = CitizensService(uow, guild_id=GUILD, allowed_user_ids={WINNER, RUNNER_UP, ORDINARY, RECORDER})
    async with uow() as repository:
        await WalletFunding(starter_grant=0, treasury_genesis=1000).initialize_guild(repository, president.guild_id)
    treasury = TreasuryService(
        uow, guild_id=GUILD, authorize=citizens.authorize, initiator_user_ids={RECORDER}, approver_user_ids={RECORDER},
        rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
        elected_authority=True, clock=clock,
    )
    proposal = dict(recipient_user_id=ORDINARY, title="Survey", terms="Survey the fictional square", amount=50,
                    deadline=clock.now + timedelta(days=1), reason="Public maintenance", idempotency_key="survey")
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.INITIATOR_REQUIRED):
        await treasury.propose_contract(GUILD, RECORDER, **proposal)
    contract = await treasury.propose_contract(GUILD, WINNER, **proposal)
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await treasury.activate_budget(GUILD, WINNER, reason="President cannot approve")
    await treasury.activate_budget(GUILD, RUNNER_UP, reason="Approve state contracts")
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.CONFLICT_OF_INTEREST):
        await treasury.reserve_contract(GUILD, ORDINARY, contract.id, reason="Beneficiary conflict")
    await treasury.reserve_contract(GUILD, RUNNER_UP, contract.id, reason="Reserve approved")
    await treasury.submit_contract(GUILD, ORDINARY, contract.id, evidence="Survey completed")
    paid = await treasury.pay_contract(GUILD, RUNNER_UP, contract.id, reason="Work accepted")
    assert paid.status is ContractStatus.PAID

    clock.now += timedelta(days=RULES.term_days)
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await treasury.activate_budget(GUILD, RUNNER_UP, reason="Expired mandate")


async def test_ordinary_citizen_cannot_open_or_certify_presidential_election(
    pg_engine: AsyncEngine,
) -> None:
    _, service, clock = await _setup(pg_engine)
    with pytest.raises(GovernmentError) as denied:
        await _open(service, key="unauthorized", actor=ORDINARY)
    assert denied.value.reason is GovernmentErrorReason.COMMISSIONER_REQUIRED
    election = await _open(service, key="president")
    assert await service.list_unfinished_elections(GUILD, REVIEWER) == (election,)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await service.list_unfinished_elections(GUILD, ORDINARY)
    clock.now = election.closes_at
    recorded = await _record(service, election, {WINNER: 2, RUNNER_UP: 1})
    assert recorded.status is ElectionStatus.PENDING_CERTIFICATION
    (pending,) = await service.list_unfinished_elections(GUILD, REVIEWER)
    assert pending.id == recorded.id and pending.status is ElectionStatus.PENDING_CERTIFICATION
    assert pending.protocol is None
    with pytest.raises(GovernmentError) as denied:
        await _certify(service, election, ORDINARY)
    assert denied.value.reason is GovernmentErrorReason.COMMISSIONER_REQUIRED
    with pytest.raises(GovernmentError) as denied:
        await _certify(service, election, RECORDER)
    assert denied.value.reason is GovernmentErrorReason.INDEPENDENT_CERTIFICATION_REQUIRED
    assert await service.list_offices(GUILD, ORDINARY) == ()
    assert await service.list_results(GUILD, ORDINARY) == ()


async def test_concurrent_independent_attestations_apply_one_term_and_replay(
    pg_engine: AsyncEngine,
) -> None:
    unit_of_work, service, clock = await _setup(pg_engine)
    election = await _open(service, key="president")
    clock.now = election.closes_at
    await _record(service, election, {WINNER: 2, RUNNER_UP: 1})
    first, second = await asyncio.wait_for(
        asyncio.gather(_certify(service, election), _certify(service, election, SECOND_REVIEWER)),
        timeout=10,
    )
    assert first == second
    assert first.status is ElectionStatus.APPLIED
    assert await _certify(service, election, SECOND_REVIEWER) == first
    assert await service.list_results(GUILD, ORDINARY) == (first,)
    assert await service.list_unfinished_elections(GUILD, REVIEWER) == ()
    (term,) = await service.list_offices(GUILD, ORDINARY)
    assert term.holder_discord_user_id == WINNER
    assert term.starts_at == first.certified_at
    assert term.ends_at == term.starts_at + timedelta(days=28)
    async with unit_of_work() as repository:
        assert await repository.has_active_office(election.guild_id, term.holder_citizen_id, term.office, term.starts_at)
        assert not await repository.has_active_office(election.guild_id, term.holder_citizen_id, term.office, term.ends_at)
    async with pg_engine.connect() as connection:
        assert len((await connection.execute(select(ElectionCertificationRow))).all()) == 1
        assert len((await connection.execute(select(PublicOfficeRow))).all()) == 1


async def test_successor_starts_at_predecessor_expiry_and_role_sync_removes_expired(
    pg_engine: AsyncEngine,
) -> None:
    unit_of_work, service, clock = await _setup(pg_engine)
    initial = await _open(service, key="first")
    clock.now = initial.closes_at
    await _record(service, initial, {WINNER: 2, RUNNER_UP: 1})
    await _certify(service, initial)
    (previous,) = await service.list_offices(GUILD, ORDINARY)

    election = await _open(service, key="second")
    clock.now = election.closes_at
    await _record(service, election, {WINNER: 1, RUNNER_UP: 2}, source_url=f"{SOURCE}-successor")
    await _certify(service, election)
    assert await service.list_offices(GUILD, ORDINARY) == (previous,)
    async with unit_of_work() as repository:
        (successor,) = await repository.list_election_terms(election.guild_id, election.id)
    assert successor.starts_at == previous.ends_at
    assert successor.holder_discord_user_id == RUNNER_UP
    with pytest.raises(GovernmentError) as denied:
        await _open(service, key="third-before-successor")
    assert denied.value.reason is GovernmentErrorReason.ELECTION_IN_PROGRESS

    clock.now = previous.ends_at
    assert await service.list_offices(GUILD, ORDINARY) == (successor,)
    assignments = await service.office_role_assignments()
    assert {item.discord_user_id: item.present for item in assignments} == {WINNER: False, RUNNER_UP: True}
    async with pg_engine.begin() as connection:
        await connection.execute(
            update(CitizenRow).where(CitizenRow.id == successor.holder_citizen_id).values(
                status=CitizenStatus.SUSPENDED.value
            )
        )
    assert await service.list_offices(GUILD, ORDINARY) == ()
    assert not any(item.present for item in await service.office_role_assignments())


async def test_extension_preserves_evidence_and_failed_election_creates_no_office(
    pg_engine: AsyncEngine,
) -> None:
    _, service, clock = await _setup(pg_engine)
    election = await _open(service, key="president")
    clock.now = election.closes_at + timedelta(hours=3)
    initial = await _record(service, election, {WINNER: 1, RUNNER_UP: 0})
    assert initial.status is ElectionStatus.EXTENDED
    assert initial.extended_until == clock.now + timedelta(hours=24)
    clock.now = initial.extended_until
    final = await _record(service, election, {WINNER: 1, RUNNER_UP: 0})
    assert final.status is ElectionStatus.NO_QUORUM
    assert await service.list_results(GUILD, ORDINARY) == ()
    certified = await _certify(service, election)
    assert await service.list_results(GUILD, ORDINARY) == (certified,)
    assert await service.list_offices(GUILD, ORDINARY) == ()

    async with pg_engine.connect() as connection:
        evidence = (await connection.execute(
            select(ElectionProtocolRow.round_number, ElectionProtocolRow.source_url)
            .order_by(ElectionProtocolRow.round_number)
        )).all()
    assert evidence == [(1, SOURCE), (2, SOURCE)]
    with pytest.raises(IntegrityError, match="append-only"):
        async with pg_engine.begin() as connection:
            await connection.execute(update(ElectionProtocolRow).values(source_url="https://forged.example"))
    with pytest.raises(IntegrityError, match="snapshots are immutable"):
        async with pg_engine.begin() as connection:
            await connection.execute(update(ElectionRow).where(ElectionRow.id == election.id).values(quorum=1))


async def test_tied_runoff_requires_independent_attestation(pg_engine: AsyncEngine) -> None:
    _, service, clock = await _setup(pg_engine)
    election = await _open(service, key="initial")
    clock.now = election.closes_at
    recorded = await _record(service, election, {WINNER: 2, RUNNER_UP: 2})
    assert recorded.status is ElectionStatus.TIED
    with pytest.raises(GovernmentError) as denied:
        await _open(service, key="runoff", runoff_of_id=election.id)
    assert denied.value.reason is GovernmentErrorReason.INVALID_RUNOFF
    await _certify(service, election)
    runoff = await _open(service, key="runoff", runoff_of_id=election.id)
    assert runoff.runoff_of_id == election.id
    assert runoff.status is ElectionStatus.OPEN


async def test_second_certified_failure_starts_crisis_for_that_office(pg_engine: AsyncEngine) -> None:
    _, service, clock = await _setup(pg_engine)
    first = await _open(service, key="first-attempt")
    clock.now = first.closes_at
    await _record(service, first, {WINNER: 1, RUNNER_UP: 1})
    (extended,) = await service.list_unfinished_elections(GUILD, REVIEWER)
    assert extended.extended_until is not None
    clock.now = extended.extended_until
    await _record(service, first, {WINNER: 1, RUNNER_UP: 1})
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.ELECTION_IN_PROGRESS):
        await _open(service, key="uncertified-bypass")
    certified = await _certify(service, first)
    assert certified.failed and not certified.opens_political_crisis
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.RETRY_REQUIRED):
        await _open(service, key="unlinked-bypass")

    repeat_options = dict(
        office=OfficeKind.PRESIDENT, candidate_user_ids=(WINNER, ORDINARY), reason="Retry after failed poll",
        idempotency_key="retry-attempt", retry_of_id=first.id,
    )
    repeat = await service.open_election(GUILD, RECORDER, **repeat_options)
    assert repeat.retry_of_id == first.id and repeat.runoff_of_id is None
    clock.now = repeat.closes_at
    tied = await _record(
        service, repeat, {WINNER: 2, ORDINARY: 2}, source_url="https://poll.example.test/retry",
    )
    assert not tied.opens_political_crisis
    crisis = await _certify(service, repeat)
    assert crisis.opens_political_crisis
    assert await service.get_public_result(GUILD, ORDINARY, repeat.id) == crisis
    assert await _certify(service, repeat, SECOND_REVIEWER) == crisis
    assert (await service.open_election(GUILD, RECORDER, **repeat_options)).id == repeat.id
    assert await service.list_offices(GUILD, ORDINARY) == ()
    for runoff in (None, first.id, repeat.id):
        with pytest.raises(GovernmentError, match=GovernmentErrorReason.POLITICAL_CRISIS):
            await _open(service, key=f"third-{runoff}", runoff_of_id=runoff)

    with pytest.raises(GovernmentError, match=GovernmentErrorReason.CRISIS_RESOLUTION_NOT_PROPOSED):
        await service.confirm_crisis_resolution(GUILD, REVIEWER, repeat.id, reason="No proposal yet")
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await service.propose_crisis_resolution(GUILD, ORDINARY, repeat.id, reason="Not on commission")
    proposed = await service.propose_crisis_resolution(GUILD, RECORDER, repeat.id, reason="Manual RP completed")
    assert proposed.confirmed_at is None
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await service.get_election(GUILD, ORDINARY, repeat.id)
    assert (await service.get_election(GUILD, REVIEWER, repeat.id)).crisis_resolution == proposed
    assert (await service.get_public_result(GUILD, ORDINARY, repeat.id)).is_political_crisis_open
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.POLITICAL_CRISIS):
        await _open(service, key="pending-bypass")
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INDEPENDENT_CERTIFICATION_REQUIRED):
        await service.confirm_crisis_resolution(GUILD, RECORDER, repeat.id, reason="Same signer")
    first_confirmation, second_confirmation = await asyncio.wait_for(asyncio.gather(
        service.confirm_crisis_resolution(GUILD, REVIEWER, repeat.id, reason="Verified RP outcome"),
        service.confirm_crisis_resolution(GUILD, SECOND_REVIEWER, repeat.id, reason="Verified recovery"),
    ), timeout=10)
    assert first_confirmation == second_confirmation
    assert first_confirmation.proposed_by_citizen_id != first_confirmation.confirmed_by_citizen_id
    settled = await service.get_public_result(GUILD, ORDINARY, repeat.id)
    assert settled.crisis_resolution == first_confirmation and not settled.is_political_crisis_open
    assert await service.list_offices(GUILD, ORDINARY) == ()
    audit = AdminAuditService(
        PostgresAdminAuditUnitOfWork(pg_engine),
        operator_policy=AdminOperatorPolicy(discord_guild_id=GUILD, operator_user_ids={ORDINARY}),
    )
    crisis_actions = {
        event.action: event for event in await audit.list_recent(GUILD, ORDINARY)
        if event.subject_id == repeat.id and event.action.startswith("election.crisis.")
    }
    assert set(crisis_actions) == {"election.crisis.propose", "election.crisis.confirm"}
    assert crisis_actions["election.crisis.propose"].actor_discord_user_id == RECORDER
    assert crisis_actions["election.crisis.confirm"].actor_discord_user_id in {REVIEWER, SECOND_REVIEWER}
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_RUNOFF):
        await _open(service, key="old-retry-after-crisis", runoff_of_id=repeat.id)
    fresh = await _open(service, key="fresh-after-crisis")
    assert fresh.retry_of_id is None and fresh.status is ElectionStatus.OPEN
    assert await service.confirm_crisis_resolution(GUILD, REVIEWER, repeat.id, reason="Replay") == first_confirmation

    parliament = await service.open_election(
        GUILD, RECORDER, office=OfficeKind.PARLIAMENT, candidate_user_ids=(WINNER, RUNNER_UP),
        reason="Separate parliamentary cycle", idempotency_key="separate-office",
    )
    assert parliament.status is ElectionStatus.OPEN
    assert await service.list_offices(GUILD, ORDINARY) == ()


async def test_database_rejects_unsupported_winner_in_manual_protocol(pg_engine: AsyncEngine) -> None:
    _, service, clock = await _setup(pg_engine)
    election = await _open(service, key="president")
    clock.now = election.closes_at
    first, second = election.candidate_citizen_ids
    tallies = [
        [citizen_id, votes, user_id]
        for citizen_id, votes, user_id in zip(
            election.candidate_citizen_ids, (2, 1), election.candidate_discord_user_ids, strict=True,
        )
    ]
    with pytest.raises(IntegrityError, match="Winners do not match"):
        async with pg_engine.begin() as connection:
            await connection.execute(
                insert(ElectionPollSourceRow).values(
                    guild_id=election.guild_id, source_url=SOURCE, election_id=election.id,
                )
            )
            await connection.execute(
                insert(ElectionProtocolRow).values(
                    election_id=election.id, guild_id=election.guild_id, round_number=1,
                    source_url=SOURCE, participants=3, tallies=tallies, winners=[second],
                    recorded_by_citizen_id=election.opened_by_citizen_id, recorded_at=clock.now,
                    reason="Conflicts with the tallies",
                )
            )
            await connection.execute(
                update(ElectionRow).where(ElectionRow.id == election.id)
                .values(status=ElectionStatus.PENDING_CERTIFICATION.value)
            )
    recorded = await _record(service, election, {WINNER: 2, RUNNER_UP: 1})
    assert recorded.protocol.winners == (first,)


async def test_election_retry_guards_apply_to_direct_writes(pg_engine: AsyncEngine) -> None:
    _, service, clock = await _setup(pg_engine)
    initial = await _open(service, key="existing-mandate")
    clock.now = initial.closes_at
    await _record(service, initial, {WINNER: 2, RUNNER_UP: 1})
    await _certify(service, initial)
    active = await service.list_offices(GUILD, ORDINARY)
    assert len(active) == 1

    failed = await _open(service, key="failed-election")
    clock.now = failed.closes_at
    await _record(service, failed, {WINNER: 2, RUNNER_UP: 2}, source_url=f"{SOURCE}-failed")
    await _certify(service, failed)

    async def insert_attempt(*, reason: str, parent_id: int | None, earlier_id: int | None = None) -> None:
        identity = {} if earlier_id is None else {"id": earlier_id}
        with pytest.raises(IntegrityError, match=reason):
            async with pg_engine.begin() as connection:
                await connection.execute(insert(ElectionRow).values(
                    guild_id=failed.guild_id, office=failed.office.value, seats=failed.seats,
                    term_days=failed.term_days, quorum=failed.quorum, extension_hours=failed.extension_hours,
                    opens_at=clock.now, closes_at=clock.now + timedelta(hours=RULES.voting_hours),
                    opened_by_citizen_id=failed.opened_by_citizen_id, reason="Attempt to bypass service",
                    request_key="direct-write", eligible_citizen_ids=list(failed.eligible_citizen_ids),
                    candidate_citizen_ids=list(failed.candidate_citizen_ids),
                    candidate_discord_user_ids=list(failed.candidate_discord_user_ids), status="OPEN",
                    runoff_of_id=parent_id, retry_of_id=parent_id, **identity,
                ))

    await insert_attempt(reason="must link the latest failed election", parent_id=None)
    await insert_attempt(reason="must link the latest failed election", parent_id=initial.id)
    await insert_attempt(reason="must follow the latest election", parent_id=failed.id, earlier_id=initial.id - 1)
    retry = await _open(service, key="failed-retry", runoff_of_id=failed.id)
    with pytest.raises(IntegrityError, match="retry link is immutable"):
        async with pg_engine.begin() as connection:
            await connection.execute(update(ElectionRow).where(ElectionRow.id == retry.id).values(retry_of_id=None))

    clock.now = retry.closes_at
    await _record(service, retry, {WINNER: 2, RUNNER_UP: 2}, source_url=f"{SOURCE}-failed-retry")
    assert (await _certify(service, retry)).opens_political_crisis
    await insert_attempt(reason="retry already failed", parent_id=retry.id)
    async with pg_engine.connect() as connection:
        reviewer_id = await connection.scalar(select(ElectionCertificationRow.certified_by_citizen_id).where(
            ElectionCertificationRow.election_id == retry.id,
        ))
    assert reviewer_id is not None

    async def attest(table, actor_id: int) -> None:
        async with pg_engine.begin() as connection:
            await connection.execute(insert(table).values(
                guild_id=retry.guild_id, election_id=retry.id, actor_citizen_id=actor_id,
                occurred_at=clock.now, reason="RP completed",
            ))

    with pytest.raises(IntegrityError, match="separate reviewer after the proposal"):
        await attest(ElectionCrisisConfirmationRow, reviewer_id)
    with pytest.raises(IntegrityError, match="certified failure and a noncandidate"):
        await attest(ElectionCrisisProposalRow, retry.candidate_citizen_ids[0])
    await attest(ElectionCrisisProposalRow, retry.opened_by_citizen_id)
    with pytest.raises(IntegrityError, match="separate reviewer after the proposal"):
        await attest(ElectionCrisisConfirmationRow, retry.opened_by_citizen_id)
    await insert_attempt(reason="retry already failed", parent_id=None)
    await attest(ElectionCrisisConfirmationRow, reviewer_id)
    for table in (ElectionCrisisProposalRow, ElectionCrisisConfirmationRow):
        with pytest.raises(IntegrityError, match="append-only"):
            async with pg_engine.begin() as connection:
                await connection.execute(update(table).values(reason="Changed"))
    await insert_attempt(reason="retry already failed", parent_id=retry.id)
    assert (await _open(service, key="after-crisis")).retry_of_id is None
    assert await service.list_offices(GUILD, ORDINARY) == active
    clock.now = active[0].ends_at
    assert await service.list_offices(GUILD, ORDINARY) == ()


async def test_poll_source_is_reserved_across_offices_and_canonical_variants(pg_engine: AsyncEngine) -> None:
    _, service, clock = await _setup(pg_engine)
    president = await _open(service, key="president")
    parliament = await service.open_election(
        GUILD, RECORDER, office=OfficeKind.PARLIAMENT, candidate_user_ids=(WINNER, RUNNER_UP),
        reason="Open parliament poll", idempotency_key="parliament",
    )
    clock.now = max(president.closes_at, parliament.closes_at)
    await _record(
        service, president, {WINNER: 2, RUNNER_UP: 1},
        source_url="https://POLL.EXAMPLE.TEST:443/president/#details",
    )
    with pytest.raises(GovernmentError) as denied:
        await _record(service, parliament, {WINNER: 2, RUNNER_UP: 1})
    assert denied.value.reason is GovernmentErrorReason.INVALID_SOURCE
    async with pg_engine.connect() as connection:
        assert (await connection.execute(
            select(ElectionPollSourceRow.source_url, ElectionPollSourceRow.election_id)
        )).one() == (SOURCE, president.id)
