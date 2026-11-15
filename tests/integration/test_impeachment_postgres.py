import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError, CitizensService
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRules, TreasuryService
from civitas.domain.citizens import CitizenStatus
from civitas.domain.government import Election, OfficeKind, OfficeTerm
from civitas.domain.jobs import BudgetCaps
from civitas.infrastructure.db.admin_audit import PostgresAdminAuditUnitOfWork
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.government_models import ImpeachmentConfirmationRow, PublicOfficeRow

pytestmark = pytest.mark.asyncio

GUILD = 101
PRESIDENT, SUCCESSOR, RECORDER, REVIEWER, SECOND_REVIEWER, ORDINARY, DEPARTING = range(201, 208)
DEPUTIES = (208, 209, 210)
VOTERS = tuple(range(201, 212))
LATE_VOTER = 212
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
RULES = ElectionRules(
    min_eligible=10, quorum_bp=4000, quorum_min=4, parliament_seats=3, term_days=28,
    voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
)
POLL_HOST = "https://poll.example.test"
IMPEACHMENT_URL = f"{POLL_HOST}/public-impeachment"


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def _government(unit_of_work, citizens: CitizensService, clock: Clock) -> GovernmentService:
    return GovernmentService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize,
        commissioner_user_ids={PRESIDENT, RECORDER, REVIEWER, SECOND_REVIEWER}, rules=RULES, clock=clock,
    )


async def _setup(engine: AsyncEngine):
    unit_of_work = PostgresGovernmentUnitOfWork(engine)
    citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids=(*VOTERS, LATE_VOTER))
    for user_id in VOTERS:
        await citizens.register(GUILD, user_id)
    clock = Clock()
    government = _government(unit_of_work, citizens, clock)
    election = await _open_president(government, key="original-president")
    await _elect(government, clock, election, tallies={PRESIDENT: 4, SUCCESSOR: 1})
    (incumbent,) = await government.list_offices(GUILD, ORDINARY)
    return unit_of_work, citizens, government, clock, incumbent


async def _open_president(government: GovernmentService, *, key: str) -> Election:
    return await government.open_election(
        GUILD, RECORDER, office=OfficeKind.PRESIDENT, candidate_user_ids=(PRESIDENT, SUCCESSOR),
        reason="Open presidential poll", idempotency_key=key,
    )


async def _elect(government: GovernmentService, clock: Clock, election: Election, *, tallies: dict[int, int]) -> None:
    clock.now = election.closes_at
    await government.record_protocol(
        GUILD, RECORDER, election.id, source_url=f"{POLL_HOST}/election-{election.id}",
        participants=sum(tallies.values()), tallies=tallies, reason="Read public election results",
    )
    await government.certify_election(GUILD, REVIEWER, election.id, reason="Verify election totals")


async def _record_impeachment(
    government: GovernmentService, impeachment_id: int, *, participants: int = 6, votes_for: int = 4,
    actor: int = RECORDER, source_url: str = IMPEACHMENT_URL,
):
    return await government.record_impeachment(
        GUILD, actor, impeachment_id, participants=participants, votes_for=votes_for,
        source_url=source_url, reason="Public presidential accountability vote",
    )


async def _confirm_impeachment(government: GovernmentService, impeachment_id: int, actor: int = REVIEWER):
    return await government.confirm_impeachment(GUILD, actor, impeachment_id, reason="Verify public vote totals")


async def test_new_impeachment_uses_current_allowlist_without_changing_open_vote(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, government, clock, _ = await _setup(pg_engine)
    original = await government.open_impeachment(GUILD, RECORDER, idempotency_key="original-ballot")
    assert original.quorum == 5
    assert await government.list_impeachment_voters(GUILD, REVIEWER, original.id) == VOTERS

    current_voters = tuple(user_id for user_id in VOTERS if user_id != DEPARTING)
    current_citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids=(*current_voters, LATE_VOTER))
    restarted = _government(unit_of_work, current_citizens, clock)
    excluded = await citizens.get_profile(GUILD, DEPARTING)
    assert excluded is not None and excluded.status is CitizenStatus.VERIFIED

    new_ballot = await restarted.open_impeachment(GUILD, RECORDER, idempotency_key="new-ballot")
    assert new_ballot.term_id == original.term_id
    assert new_ballot.quorum == 4
    assert await restarted.list_impeachment_voters(GUILD, REVIEWER, new_ballot.id) == current_voters
    assert await restarted.list_impeachment_voters(GUILD, REVIEWER, original.id) == VOTERS
    assert await restarted.open_impeachment(GUILD, RECORDER, idempotency_key="original-ballot") == original


@pytest.mark.parametrize(("participants", "votes_for"), [(4, 4), (6, 3)], ids=["no-quorum", "below-two-thirds"])
async def test_insufficient_impeachment_vote_preserves_incumbent(
    pg_engine: AsyncEngine, participants: int, votes_for: int,
) -> None:
    _, _, government, _, incumbent = await _setup(pg_engine)
    motion = await government.open_impeachment(GUILD, RECORDER, idempotency_key="failed-impeachment")
    assert motion.quorum == 5
    recorded = await _record_impeachment(government, motion.id, participants=participants, votes_for=votes_for)
    assert not recorded.has_support
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.IMPEACHMENT_INSUFFICIENT_SUPPORT):
        await _confirm_impeachment(government, motion.id)
    assert await government.list_offices(GUILD, ORDINARY) == (incumbent,)


async def test_impeachment_signatures_freeze_voters_and_revoke_only_president_authority(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, government, clock, incumbent = await _setup(pg_engine)
    parliament = await government.open_election(
        GUILD, RECORDER, office=OfficeKind.PARLIAMENT, candidate_user_ids=DEPUTIES,
        reason="Open parliamentary poll", idempotency_key="parliament",
    )
    await _elect(government, clock, parliament, tallies={DEPUTIES[0]: 3, DEPUTIES[1]: 2, DEPUTIES[2]: 1})
    deputies = tuple(term for term in await government.list_offices(GUILD, ORDINARY) if term.office == OfficeKind.PARLIAMENT)
    assert len(deputies) == 3

    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await government.open_impeachment(GUILD, ORDINARY, idempotency_key="unauthorized")
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.IMPEACHMENT_CONFLICT):
        await government.open_impeachment(GUILD, PRESIDENT, idempotency_key="own-impeachment")
    motion = await government.open_impeachment(GUILD, RECORDER, idempotency_key="president-impeachment")
    assert motion.president_discord_user_id == PRESIDENT
    assert motion.election_id == incumbent.election_id
    assert motion.quorum == 5 and len(motion.eligible_citizen_ids) == len(VOTERS)
    assert await government.open_impeachment(GUILD, RECORDER, idempotency_key="president-impeachment") == motion
    assert await government.list_impeachment_voters(GUILD, REVIEWER, motion.id) == VOTERS
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await government.list_impeachment_voters(GUILD, ORDINARY, motion.id)
    with pytest.raises(CitizenshipAccessError, match=AccessDeniedReason.WRONG_GUILD):
        await government.list_impeachment_voters(GUILD + 1, REVIEWER, motion.id)

    departed = await citizens.get_profile(GUILD, DEPARTING)
    assert departed is not None and departed.verified_at is not None
    assert await citizens.mark_departed(GUILD, DEPARTING, observed_at=departed.verified_at + timedelta(seconds=1))
    await citizens.register(GUILD, LATE_VOTER)
    assert await government.list_impeachment_voters(GUILD, REVIEWER, motion.id) == VOTERS

    with pytest.raises(GovernmentError, match=GovernmentErrorReason.IMPEACHMENT_UNRECORDED):
        await _confirm_impeachment(government, motion.id)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await _record_impeachment(government, motion.id, actor=ORDINARY)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.IMPEACHMENT_CONFLICT):
        await _record_impeachment(government, motion.id, actor=PRESIDENT)
    for participants, votes_for in ((len(VOTERS) + 1, 6), (5, 6)):
        with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_IMPEACHMENT_TALLY):
            await _record_impeachment(government, motion.id, participants=participants, votes_for=votes_for)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_SOURCE):
        await _record_impeachment(government, motion.id, source_url=f"{POLL_HOST}/election-{incumbent.election_id}")

    recorded = await _record_impeachment(government, motion.id)
    assert recorded.has_support and recorded.confirmed_at is None
    assert (recorded.participants, recorded.votes_for) == (6, 4)
    for actor in (RECORDER, PRESIDENT):
        with pytest.raises(GovernmentError, match=GovernmentErrorReason.IMPEACHMENT_CONFLICT):
            await _confirm_impeachment(government, motion.id, actor)
    with pytest.raises(GovernmentError, match=GovernmentErrorReason.COMMISSIONER_REQUIRED):
        await _confirm_impeachment(government, motion.id, ORDINARY)

    treasury = TreasuryService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, initiator_user_ids=(), approver_user_ids=(),
        rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
        elected_authority=True, clock=clock,
    )
    proposal = dict(
        recipient_user_id=ORDINARY, title="Repair lights", terms="Replace spent lighting",
        amount=50, deadline=clock.now + timedelta(days=1), reason="Approved public purpose",
    )
    await treasury.propose_contract(GUILD, PRESIDENT, **proposal, idempotency_key="before-impeachment")
    async with unit_of_work() as repository:
        assert await repository.has_active_office(motion.guild_id, incumbent.holder_citizen_id, OfficeKind.PRESIDENT, clock.now)

    clock.now += timedelta(minutes=1)
    confirmed = await _confirm_while_contract_waits(
        pg_engine, unit_of_work, citizens, government, clock, treasury, motion.id, proposal,
    )
    assert confirmed.confirmed_at == clock.now
    assert confirmed.recorded_by_citizen_id != confirmed.confirmed_by_citizen_id
    assert confirmed.source_url == IMPEACHMENT_URL
    assert await government.get_public_impeachment(GUILD, ORDINARY, motion.id) == replace(
        confirmed, eligible_citizen_ids=(), confirmation_reason=None,
    )
    async with PostgresAdminAuditUnitOfWork(pg_engine)() as repository:
        audit = await repository.list_admin_audit(GUILD, limit=100, offset=0)
    assert {
        (event.action, event.actor_discord_user_id, event.result)
        for event in audit if event.subject_kind == "impeachment" and event.subject_id == motion.id
    } == {
        ("impeachment.open", RECORDER, "success"),
        ("impeachment.record", RECORDER, "success"),
        ("impeachment.confirm", REVIEWER, "success"),
    }
    assert await government.list_offices(GUILD, ORDINARY) == deputies
    async with unit_of_work() as repository:
        roles = await repository.list_office_role_assignments(GUILD, clock.now)
        assert {role.discord_user_id for role in roles if role.present} == set(DEPUTIES)
        assert any(role.discord_user_id == PRESIDENT and not role.present for role in roles)
    await treasury.activate_budget(GUILD, DEPUTIES[0], reason="Deputy still holds a mandate")
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.INITIATOR_REQUIRED):
        await treasury.propose_contract(GUILD, PRESIDENT, **proposal, idempotency_key="after-impeachment")


async def _confirm_while_contract_waits(engine, unit_of_work, citizens, government, clock, treasury, motion_id, proposal):
    confirmation_ready = asyncio.Event()
    finish_confirmation = asyncio.Event()

    @asynccontextmanager
    async def hold_confirmation():
        async with unit_of_work() as repository:
            yield repository
            confirmation_ready.set()
            await finish_confirmation.wait()

    holding_government = _government(hold_confirmation, citizens, clock)
    first = asyncio.create_task(_confirm_impeachment(holding_government, motion_id))
    contenders = [first]
    try:
        await asyncio.wait_for(confirmation_ready.wait(), timeout=10)
        spending = asyncio.create_task(treasury.propose_contract(
            GUILD, PRESIDENT, **proposal, idempotency_key="during-impeachment",
        ))
        contenders.append(spending)
        async with asyncio.timeout(10):
            while True:
                async with engine.connect() as connection:
                    waiting = await connection.scalar(text("""
                        SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                            WHERE datname = current_database() AND wait_event_type = 'Lock'
                            AND query LIKE '%FROM citizens%' AND query LIKE '%FOR NO KEY UPDATE%')
                    """))
                if waiting:
                    break
                assert not spending.done(), "The proposal committed before impeachment released its locks"
                await asyncio.sleep(0.02)
        contenders.append(asyncio.create_task(_confirm_impeachment(government, motion_id, SECOND_REVIEWER)))
    finally:
        finish_confirmation.set()
        results = await asyncio.wait_for(asyncio.gather(*contenders, return_exceptions=True), timeout=10)

    confirmed, denied, replay = results
    assert not isinstance(confirmed, BaseException), confirmed
    assert replay == confirmed
    assert isinstance(denied, TreasuryError) and denied.reason is TreasuryErrorReason.INITIATOR_REQUIRED
    return confirmed


@pytest.mark.parametrize("next_poll", ["none", "open", "scheduled"])
async def test_impeachment_preserves_successor_or_allows_one_new_term(pg_engine: AsyncEngine, next_poll: str) -> None:
    unit_of_work, _, government, clock, incumbent = await _setup(pg_engine)
    election: Election | None = None
    scheduled: OfficeTerm | None = None
    if next_poll != "none":
        election = await _open_president(government, key="next-presidential-election")
    if next_poll == "scheduled":
        assert election is not None
        await _elect(government, clock, election, tallies={PRESIDENT: 1, SUCCESSOR: 4})
        async with unit_of_work() as repository:
            (scheduled,) = await repository.list_election_terms(election.guild_id, election.id)
        assert scheduled.starts_at == incumbent.ends_at

    clock.now += timedelta(minutes=1)
    motion = await government.open_impeachment(GUILD, RECORDER, idempotency_key="vacancy-impeachment")
    await _record_impeachment(government, motion.id)
    await _confirm_impeachment(government, motion.id)

    assert await government.list_offices(GUILD, ORDINARY) == ()
    if next_poll != "none":
        with pytest.raises(GovernmentError, match=GovernmentErrorReason.ELECTION_IN_PROGRESS):
            await _open_president(government, key="would-duplicate-next-election")
    else:
        election = await _open_president(government, key="snap-presidential-election")
        with pytest.raises(GovernmentError, match=GovernmentErrorReason.ELECTION_IN_PROGRESS):
            await _open_president(government, key="would-duplicate-snap-election")
    assert election is not None

    if scheduled is None:
        assert await government.list_unfinished_elections(GUILD, RECORDER) == (election,)
        await _elect(government, clock, election, tallies={PRESIDENT: 1, SUCCESSOR: 4})
        (successor,) = await government.list_offices(GUILD, ORDINARY)
        assert successor.starts_at == clock.now
    else:
        async with unit_of_work() as repository:
            assert await repository.list_election_terms(election.guild_id, election.id) == (scheduled,)
        clock.now = scheduled.starts_at
        (successor,) = await government.list_offices(GUILD, ORDINARY)
        assert successor == scheduled
    assert successor.election_id == election.id and successor.holder_discord_user_id == SUCCESSOR
    assert successor.ends_at - successor.starts_at == timedelta(days=28)
    async with pg_engine.connect() as connection:
        term_count = len((await connection.execute(select(PublicOfficeRow.id))).all())
    assert term_count == 2


@pytest.mark.parametrize(
    ("participants", "votes_for", "signer"),
    [(4, 4, REVIEWER), (6, 3, REVIEWER), (6, 4, RECORDER), (6, 4, PRESIDENT)],
    ids=["no-quorum", "below-two-thirds", "same-recorder", "president"],
)
async def test_database_rejects_invalid_impeachment_confirmation(
    pg_engine: AsyncEngine, participants: int, votes_for: int, signer: int,
) -> None:
    _, citizens, government, clock, incumbent = await _setup(pg_engine)
    ballot = await government.open_impeachment(GUILD, RECORDER, idempotency_key="illegal-confirmation")
    await _record_impeachment(government, ballot.id, participants=participants, votes_for=votes_for)
    actor = await citizens.get_profile(GUILD, signer)
    assert actor is not None
    with pytest.raises(IntegrityError, match="qualifying vote and an independent second attestation"):
        async with pg_engine.begin() as connection:
            await connection.execute(insert(ImpeachmentConfirmationRow).values(
                guild_id=ballot.guild_id, term_id=ballot.term_id, impeachment_id=ballot.id,
                confirmed_by_citizen_id=actor.id, confirmed_at=clock.now,
                confirmation_reason="Attempted direct confirmation",
            ))
    assert await government.list_offices(GUILD, ORDINARY) == (incumbent,)
