import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import MAX_DISCORD_ID, CitizensService
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding
from civitas.domain.citizens import CitizenStatus
from civitas.domain.government import Election, ElectionStatus, OfficeKind
from civitas.domain.jobs import BudgetCaps
from civitas.domain.treasury import ContractStatus
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.government_models import (
    ElectionCertificationRow,
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
    service: GovernmentService, election: Election, tallies: dict[int, int],
) -> Election:
    return await service.record_protocol(
        GUILD, RECORDER, election.id, source_url=SOURCE, participants=sum(tallies.values()),
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
    clock.now = election.closes_at
    recorded = await _record(service, election, {WINNER: 2, RUNNER_UP: 1})
    assert recorded.status is ElectionStatus.PENDING_CERTIFICATION
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
    await _record(service, election, {WINNER: 1, RUNNER_UP: 2})
    await _certify(service, election)
    assert await service.list_offices(GUILD, ORDINARY) == (previous,)
    async with unit_of_work() as repository:
        (successor,) = await repository.list_election_terms(election.guild_id, election.id)
    assert successor.starts_at == previous.ends_at
    assert successor.holder_discord_user_id == RUNNER_UP

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
