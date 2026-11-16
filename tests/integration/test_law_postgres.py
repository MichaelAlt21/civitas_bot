import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.admission import AdmissionService
from civitas.app.citizens import CitizensService
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.law import LawError, LawErrorReason, LawService
from civitas.domain.government import OfficeKind
from civitas.infrastructure.db.admission import PostgresAdmissionUnitOfWork
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.law import PostgresLawUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = 101_000_000_000_000_001
CHANNEL = 101_000_000_000_000_002
MESSAGE = 101_000_000_000_000_003
DEPUTY_1, DEPUTY_2, DEPUTY_3, ORDINARY, RECORDER, CERTIFIER = range(201, 207)


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 15, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest_asyncio.fixture
async def law_registry(pg_engine: AsyncEngine) -> tuple[LawService, Clock]:
    citizen_ids = (DEPUTY_1, DEPUTY_2, DEPUTY_3, ORDINARY, RECORDER, CERTIFIER)
    admission = AdmissionService(PostgresAdmissionUnitOfWork(pg_engine), guild_id=GUILD)
    await admission.initialize(citizen_ids)
    unit_of_work = PostgresGovernmentUnitOfWork(pg_engine)
    citizens = CitizensService(unit_of_work, guild_id=GUILD, authorize_admission=admission.authorize)
    for user_id in citizen_ids:
        await citizens.register(GUILD, user_id)

    clock = Clock()
    government = GovernmentService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize,
        commissioner_user_ids=(RECORDER, CERTIFIER), use_persisted_admission=True, clock=clock,
        rules=ElectionRules(
            min_eligible=3, quorum_bp=4000, quorum_min=3, parliament_seats=3, term_days=28,
            voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
        ),
    )
    election = await government.open_election(
        GUILD, RECORDER, office=OfficeKind.PARLIAMENT, candidate_user_ids=(DEPUTY_1, DEPUTY_2, DEPUTY_3),
        reason="Parliamentary election", idempotency_key="law-parliament",
    )
    clock.now = election.closes_at
    await government.record_protocol(
        GUILD, RECORDER, election.id, source_url="https://poll.example.test/parliament",
        participants=5, tallies={DEPUTY_1: 2, DEPUTY_2: 2, DEPUTY_3: 1}, reason="Public results",
    )
    await government.certify_election(GUILD, CERTIFIER, election.id, reason="Confirmed publication")
    return LawService(PostgresLawUnitOfWork(pg_engine), guild_id=GUILD, clock=clock), clock


def _publication(message: int, *, guild_id: int = GUILD) -> str:
    return f"https://discord.com/channels/{guild_id}/{CHANNEL}/{message}"


async def test_law_registration_requires_independent_deputies_and_hides_drafts(law_registry):
    laws, clock = law_registry
    effective_at = clock.now + timedelta(hours=1)
    payload = dict(
        code="CIV-01", title="Public notices", text="Notices are published in the public archive.",
        source_url=_publication(MESSAGE), effective_at=effective_at.isoformat(), idempotency_key="public-notices",
    )
    with pytest.raises(LawError, match=LawErrorReason.DEPUTY_REQUIRED):
        await laws.register(GUILD, ORDINARY, **payload)

    draft = await laws.register(GUILD, DEPUTY_1, **payload)
    assert await laws.register(GUILD, DEPUTY_1, **payload) == draft
    assert await laws.revision(GUILD, DEPUTY_2, draft.id) == draft
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await laws.revision(GUILD, ORDINARY, draft.id)
    assert not await laws.list_effective(GUILD)
    with pytest.raises(LawError, match=LawErrorReason.INDEPENDENT_CONFIRMATION):
        await laws.confirm(GUILD, DEPUTY_1, draft.id)
    with pytest.raises(LawError, match=LawErrorReason.DEPUTY_REQUIRED):
        await laws.confirm(GUILD, ORDINARY, draft.id)

    first, replay = await asyncio.gather(
        laws.confirm(GUILD, DEPUTY_2, draft.id),
        laws.confirm(GUILD, DEPUTY_2, draft.id),
    )
    assert first == replay == await laws.revision(GUILD, ORDINARY, draft.id)
    assert first.id == draft.id
    assert not await laws.list_effective(GUILD)
    clock.now = effective_at
    assert await laws.show(GUILD, payload["code"]) == first
    assert await laws.list_effective(GUILD) == (first,)


async def test_law_revisions_switch_at_the_effective_time_and_keep_history(law_registry):
    laws, clock = law_registry
    first_start = clock.now + timedelta(hours=1)
    first_draft = await laws.register(
        GUILD, DEPUTY_1, code="CIV-02", title="Archive", text="Archive version one.",
        source_url=_publication(MESSAGE), effective_at=first_start.isoformat(), idempotency_key="archive-1",
    )
    first = await laws.confirm(GUILD, DEPUTY_2, first_draft.id)
    clock.now = first_start
    assert await laws.show(GUILD, "CIV-02") == first

    second_start = first_start + timedelta(hours=3)
    second_draft = await laws.register(
        GUILD, DEPUTY_2, code="CIV-02", title="Archive amendment", text="Archive version two.",
        source_url=_publication(MESSAGE + 1), effective_at=second_start.isoformat(), idempotency_key="archive-2",
    )
    second = await laws.confirm(GUILD, DEPUTY_3, second_draft.id)
    clock.now = second_start - timedelta(microseconds=1)
    assert await laws.show(GUILD, "CIV-02") == first
    assert await laws.list_effective(GUILD) == (first,)

    clock.now = second_start
    assert await laws.show(GUILD, "CIV-02") == second
    assert await laws.show(GUILD, "CIV-02", at=first_start.isoformat()) == first
    assert await laws.list_effective(GUILD) == (second,)
    original = await laws.revision(GUILD, ORDINARY, first_draft.id)
    assert original == first
    assert original.text == "Archive version one." and original.source_url == _publication(MESSAGE)


async def test_law_requires_own_guild_publication_and_a_unique_effective_slot(law_registry):
    laws, clock = law_registry
    effective_at = clock.now + timedelta(hours=2)
    payload = dict(
        code="CIV-03", title="Public procedures", text="First proposed text.",
        source_url=_publication(MESSAGE), effective_at=effective_at.isoformat(), idempotency_key="first-procedures",
    )
    with pytest.raises(LawError, match=LawErrorReason.INVALID_SOURCE):
        await laws.register(GUILD, DEPUTY_1, **(payload | {"source_url": _publication(MESSAGE, guild_id=GUILD + 4)}))
    first_draft = await laws.register(GUILD, DEPUTY_1, **payload)
    competing_draft = await laws.register(GUILD, DEPUTY_2, **(payload | {
        "text": "Competing text.", "source_url": _publication(MESSAGE + 1),
        "effective_at": effective_at.astimezone(timezone(timedelta(hours=3))).isoformat(),
        "idempotency_key": "competing-procedures",
    }))
    first = await laws.confirm(GUILD, DEPUTY_2, first_draft.id)
    with pytest.raises(LawError, match=LawErrorReason.OCCUPIED_EFFECTIVE_AT):
        await laws.confirm(GUILD, DEPUTY_3, competing_draft.id)
    with pytest.raises(LawError, match=LawErrorReason.REVISION_NOT_FOUND):
        await laws.revision(GUILD, ORDINARY, competing_draft.id)
    clock.now = effective_at
    assert await laws.show(GUILD, "CIV-03") == first
    assert await laws.list_effective(GUILD) == (first,)
