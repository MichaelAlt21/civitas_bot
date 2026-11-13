from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentRepository, GovernmentService
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import Election, ElectionStatus, OfficeKind, OfficeRoleAssignment

DISCORD_GUILD = 100
GUILD = 1
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
CITIZENS = tuple(Citizen(index, GUILD, 200 + index, CitizenStatus.VERIFIED, NOW, NOW) for index in range(1, 11))
FIRST, SECOND, ALICE, BOB, CAROL, DAN, PLAYER, *_ = CITIZENS
RULES = ElectionRules(
    min_eligible=10, quorum_bp=4000, quorum_min=4, parliament_seats=3, term_days=28,
    voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example"}),
)
POLL = "https://poll.example/results/17"


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=GovernmentRepository)
    elections: dict[int, Election] = {}

    def get_citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        return tuple(citizen for citizen in CITIZENS if citizen.discord_user_id in user_ids)

    def create(election: Election) -> Election:
        stored = replace(election, id=len(elections) + 1)
        elections[stored.id] = stored
        return stored

    def update(election: Election) -> None:
        elections[election.id] = election

    repo.ensure_guild.return_value = GUILD
    repo.get_citizens.side_effect = get_citizens
    repo.list_verified_citizens.return_value = CITIZENS
    repo.get_election.side_effect = lambda guild_id, election_id, **opts: elections.get(election_id)
    repo.get_election_by_key.side_effect = lambda guild_id, key: next(
        (election for election in elections.values() if election.request_key == key), None
    )
    repo.create_election.side_effect = create
    repo.update_election.side_effect = update
    repo.apply_election.side_effect = update
    repo.list_results.side_effect = lambda guild_id, limit: tuple(
        election for election in elections.values() if election.certified_at is not None
    )
    return repo


def service(repository: AsyncMock, *, now: datetime = NOW, rules: ElectionRules = RULES) -> GovernmentService:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[GovernmentRepository]:
        yield repository

    citizens = CitizensService(
        lambda: pytest.fail("Authorization should not use a separate transaction"),
        guild_id=DISCORD_GUILD,
        allowed_user_ids=[citizen.discord_user_id for citizen in CITIZENS],
    )
    return GovernmentService(
        unit_of_work, guild_id=DISCORD_GUILD, authorize=citizens.authorize,
        commissioner_user_ids=[FIRST.discord_user_id, SECOND.discord_user_id, CAROL.discord_user_id],
        rules=rules, clock=lambda: now,
    )


async def open_president(gov: GovernmentService, *, runoff_of_id: int | None = None) -> Election:
    return await gov.open_election(
        DISCORD_GUILD, FIRST.discord_user_id, office=OfficeKind.PRESIDENT,
        candidate_user_ids=(ALICE.discord_user_id, BOB.discord_user_id), reason="Очередные выборы",
        idempotency_key="open" if runoff_of_id is None else "runoff", runoff_of_id=runoff_of_id,
    )


@pytest.mark.asyncio
async def test_open_snapshots_verified_eligibility_and_rules_once(repository: AsyncMock) -> None:
    gov = service(repository)
    opened = await open_president(gov)
    assert await open_president(gov) == opened
    assert opened.seats == 1
    assert opened.quorum == 4
    assert opened.term_days == 28
    assert opened.closes_at == NOW + timedelta(hours=48)
    assert opened.eligible_citizen_ids == tuple(citizen.id for citizen in CITIZENS)
    assert opened.candidate_discord_user_ids == (ALICE.discord_user_id, BOB.discord_user_id)
    repository.create_election.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_requires_population_threshold_and_valid_slate(repository: AsyncMock) -> None:
    gov = service(repository)
    repository.list_verified_citizens.return_value = CITIZENS[:-1]
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INSUFFICIENT_ELIGIBLE):
        await open_president(gov)
    repository.list_verified_citizens.return_value = CITIZENS
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_CANDIDATES):
        await gov.open_election(
            DISCORD_GUILD, FIRST.discord_user_id, office=OfficeKind.PARLIAMENT,
            candidate_user_ids=(ALICE.discord_user_id, BOB.discord_user_id), reason="Выборы", idempotency_key="open",
        )
    repository.create_election.assert_not_awaited()


@pytest.mark.parametrize("source", ["http://poll.example/result", "https://poll.example.invalid/results", "https://poll.example@other.invalid/results", "https://poll.example:8080/results"])
@pytest.mark.asyncio
async def test_manual_results_accept_only_configured_https_sources(repository: AsyncMock, source: str) -> None:
    opened = await open_president(service(repository))
    gov = service(repository, now=opened.closes_at)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_SOURCE):
        await gov.record_protocol(
            DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=source, participants=4,
            tallies={ALICE.discord_user_id: 4, BOB.discord_user_id: 0}, reason="Протокол",
        )
    repository.update_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_result_checks_voting_window_and_single_vote_tallies(repository: AsyncMock) -> None:
    gov = service(repository)
    opened = await open_president(gov)
    after = service(repository, now=opened.closes_at)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.VOTING_NOT_CLOSED):
        await gov.record_protocol(
            DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=4,
            tallies={ALICE.discord_user_id: 3, BOB.discord_user_id: 1}, reason="Протокол",
        )
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_TALLY):
        await after.record_protocol(
            DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=4,
            tallies={ALICE.discord_user_id: 3, BOB.discord_user_id: 2}, reason="Протокол",
        )
    repository.update_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_independent_certification_applies_once_and_only_then_is_public(repository: AsyncMock) -> None:
    opened = await open_president(service(repository))
    gov = service(repository, now=opened.closes_at)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await gov.certify_election(DISCORD_GUILD, PLAYER.discord_user_id, opened.id, reason="Попытка")
    pending = await gov.record_protocol(
        DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=5,
        tallies={ALICE.discord_user_id: 3, BOB.discord_user_id: 1}, reason="Протокол",
    )
    assert pending.status == ElectionStatus.PENDING_CERTIFICATION
    assert pending.protocol.winners == (ALICE.id,)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.RESULT_NOT_PUBLIC):
        await gov.get_public_result(DISCORD_GUILD, 999, opened.id)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INDEPENDENT_CERTIFICATION_REQUIRED):
        await gov.certify_election(DISCORD_GUILD, FIRST.discord_user_id, opened.id, reason="Самопроверка")
    applied = await gov.certify_election(DISCORD_GUILD, SECOND.discord_user_id, opened.id, reason="Независимая сверка")
    assert applied.status == ElectionStatus.APPLIED
    assert applied.certified_by_citizen_id == SECOND.id
    assert applied.certification_reason == "Независимая сверка"
    assert await gov.certify_election(DISCORD_GUILD, SECOND.discord_user_id, opened.id, reason="Повтор") == applied
    assert await gov.get_public_result(DISCORD_GUILD, 999, opened.id) == applied
    assert await gov.list_results(DISCORD_GUILD, 999) == (applied,)
    repository.apply_election.assert_awaited_once_with(applied)


@pytest.mark.asyncio
async def test_candidate_commissioner_cannot_record_or_certify(repository: AsyncMock) -> None:
    opened = await service(repository).open_election(
        DISCORD_GUILD, FIRST.discord_user_id, office=OfficeKind.PRESIDENT,
        candidate_user_ids=(ALICE.discord_user_id, CAROL.discord_user_id), reason="Выборы", idempotency_key="open",
    )
    gov = service(repository, now=opened.closes_at)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.CANDIDATE_CONFLICT):
        await gov.record_protocol(
            DISCORD_GUILD, CAROL.discord_user_id, opened.id, source_url=POLL, participants=4,
            tallies={ALICE.discord_user_id: 3, CAROL.discord_user_id: 1}, reason="Протокол",
        )
    await gov.record_protocol(
        DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=4,
        tallies={ALICE.discord_user_id: 3, CAROL.discord_user_id: 1}, reason="Протокол",
    )
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.CANDIDATE_CONFLICT):
        await gov.certify_election(DISCORD_GUILD, CAROL.discord_user_id, opened.id, reason="Сверка")
    repository.apply_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_quorum_extends_once_even_when_final_tally_is_identical(repository: AsyncMock) -> None:
    opened = await open_president(service(repository))
    manual_check = opened.closes_at + timedelta(hours=4)
    gov = service(repository, now=manual_check)
    protocol = dict(
        source_url=POLL, participants=3, tallies={ALICE.discord_user_id: 2, BOB.discord_user_id: 1}, reason="Протокол",
    )
    extended = await gov.record_protocol(DISCORD_GUILD, FIRST.discord_user_id, opened.id, **protocol)
    assert extended.status == ElectionStatus.EXTENDED
    assert extended.extended_until == manual_check + timedelta(hours=24)
    assert await gov.record_protocol(DISCORD_GUILD, FIRST.discord_user_id, opened.id, **protocol) == extended
    after = service(repository, now=extended.extended_until)
    no_quorum = await after.record_protocol(DISCORD_GUILD, FIRST.discord_user_id, opened.id, **protocol)
    assert no_quorum.status == ElectionStatus.NO_QUORUM
    assert no_quorum.extended_until == extended.extended_until
    assert no_quorum.protocol.winners == ()
    result = await after.certify_election(DISCORD_GUILD, SECOND.discord_user_id, opened.id, reason="Сверка")
    assert await after.get_public_result(DISCORD_GUILD, PLAYER.discord_user_id, opened.id) == result
    repository.apply_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_tied_president_uses_certified_runoff_and_no_arbitrary_winner(repository: AsyncMock) -> None:
    opened = await open_president(service(repository))
    gov = service(repository, now=opened.closes_at)
    tied = await gov.record_protocol(
        DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=4,
        tallies={ALICE.discord_user_id: 2, BOB.discord_user_id: 2}, reason="Ничья",
    )
    assert tied.status == ElectionStatus.TIED
    assert tied.protocol.winners == ()
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_RUNOFF):
        await open_president(gov, runoff_of_id=opened.id)
    await gov.certify_election(DISCORD_GUILD, SECOND.discord_user_id, opened.id, reason="Сверка")
    runoff = await open_president(gov, runoff_of_id=opened.id)
    assert runoff.runoff_of_id == opened.id
    assert runoff.candidate_discord_user_ids == opened.candidate_discord_user_ids
    repository.apply_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_parliament_cutoff_tie_seats_no_one(repository: AsyncMock) -> None:
    opened = await service(repository).open_election(
        DISCORD_GUILD, FIRST.discord_user_id, office=OfficeKind.PARLIAMENT,
        candidate_user_ids=tuple(candidate.discord_user_id for candidate in (ALICE, BOB, CAROL, DAN)),
        reason="Выборы", idempotency_key="open",
    )
    gov = service(repository, now=opened.closes_at)
    tied = await gov.record_protocol(
        DISCORD_GUILD, FIRST.discord_user_id, opened.id, source_url=POLL, participants=8,
        tallies=dict(zip(opened.candidate_discord_user_ids, (4, 2, 1, 1), strict=True)), reason="Протокол",
    )
    assert tied.status == ElectionStatus.TIED
    assert tied.protocol.winners == ()
    repository.apply_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_office_snapshot_includes_revocations_and_public_reads_are_guild_scoped(repository: AsyncMock) -> None:
    assignments = (
        OfficeRoleAssignment(OfficeKind.PRESIDENT, ALICE.discord_user_id, False),
        OfficeRoleAssignment(OfficeKind.PRESIDENT, BOB.discord_user_id, True),
    )
    repository.list_office_role_assignments.return_value = assignments
    repository.list_offices.return_value = ()
    gov = service(repository)

    assert await gov.office_role_assignments() == assignments
    assert await gov.list_offices(DISCORD_GUILD, 999) == ()
    with pytest.raises(CitizenshipAccessError):
        await gov.list_offices(None, 999)
    repository.list_office_role_assignments.assert_awaited_once_with(DISCORD_GUILD, NOW)
    repository.list_offices.assert_awaited_once_with(GUILD, NOW)
