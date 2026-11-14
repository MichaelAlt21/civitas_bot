from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.domain.government import ElectionStatus, OfficeKind
from civitas.infrastructure.db.government import PostgresGovernmentRepository, PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.government_models import ElectionPollSourceRow, ElectionProtocolRow


def _migrate(connection: Connection, *, upgrade: bool) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["connection"] = connection
    if upgrade:
        command.upgrade(config, "head")
    else:
        command.downgrade(config, "0009_government")


@pytest.mark.asyncio
async def test_election_upgrade_preserves_legacy_evidence_and_reserves_earliest_source(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with pg_engine.begin() as connection:
        await connection.run_sync(_migrate, upgrade=False)
        # The current ORM maps this nullable field. Leave the column without
        # its later constraints while producing genuine 0009 election rows.
        await connection.execute(text("ALTER TABLE elections ADD COLUMN retry_of_id bigint"))

    guild, recorder, reviewer, candidate = 101, 201, 202, 203
    unit_of_work = PostgresGovernmentUnitOfWork(pg_engine)
    citizens = CitizensService(unit_of_work, guild_id=guild, allowed_user_ids={recorder, reviewer, candidate})
    for user in (recorder, reviewer, candidate):
        await citizens.register(guild, user)

    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    service = GovernmentService(
        unit_of_work, guild_id=guild, authorize=citizens.authorize, commissioner_user_ids={recorder, reviewer},
        rules=ElectionRules(
            min_eligible=3, quorum_bp=4000, quorum_min=2, parliament_seats=1, term_days=28,
            voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
        ),
        clock=lambda: now,
    )
    president = await service.open_election(
        guild, recorder, office=OfficeKind.PRESIDENT, candidate_user_ids=(candidate,),
        reason="Initial presidential poll", idempotency_key="president",
    )
    parliament = await service.open_election(
        guild, recorder, office=OfficeKind.PARLIAMENT, candidate_user_ids=(candidate,),
        reason="Initial parliamentary poll", idempotency_key="parliament",
    )
    canonical = "https://poll.example.test/round?view=final"
    original = "https://POLL.EXAMPLE.TEST:0443/round/?view=final#details"
    protocol = dict(participants=2, tallies={candidate: 2}, reason="Record aggregate votes")

    async def accept_legacy_source(self, guild_id, election_id, source_url) -> bool:
        # Revision 0009 permitted reuse and stored the original URL on each protocol.
        return True

    now = parliament.closes_at
    with monkeypatch.context() as legacy:
        legacy.setattr(PostgresGovernmentRepository, "bind_poll_source", accept_legacy_source)
        await service.record_protocol(guild, recorder, parliament.id, source_url=original, **protocol)
        await service.certify_election(guild, reviewer, parliament.id, reason="Verify parliamentary result")
        now += timedelta(hours=1)
        await service.record_protocol(guild, recorder, president.id, source_url=canonical, **protocol)
        await service.certify_election(guild, reviewer, president.id, reason="Verify presidential result")

    legacy_results = await service.list_results(guild, candidate)
    legacy_offices = await service.list_offices(guild, candidate)
    assert len(legacy_results) == len(legacy_offices) == 2
    async with pg_engine.begin() as connection:
        await connection.execute(text("ALTER TABLE elections DROP COLUMN retry_of_id"))
        await connection.run_sync(_migrate, upgrade=True)

    assert await service.list_results(guild, candidate) == legacy_results
    assert await service.list_offices(guild, candidate) == legacy_offices
    async with pg_engine.connect() as connection:
        bindings = (await connection.execute(
            select(ElectionPollSourceRow.source_url, ElectionPollSourceRow.election_id)
            .where(ElectionPollSourceRow.guild_id == president.guild_id)
        )).all()
        evidence = (await connection.execute(
            select(ElectionProtocolRow.election_id, ElectionProtocolRow.source_url)
            .where(ElectionProtocolRow.guild_id == president.guild_id).order_by(ElectionProtocolRow.recorded_at)
        )).all()
    assert bindings == [(canonical, parliament.id)]
    assert evidence == [(parliament.id, original), (president.id, canonical)]

    next_election = await service.open_election(
        guild, recorder, office=OfficeKind.PRESIDENT, candidate_user_ids=(candidate,),
        reason="Elect successor", idempotency_key="successor",
    )
    now = next_election.closes_at
    with pytest.raises(GovernmentError) as rejected:
        await service.record_protocol(guild, recorder, next_election.id, source_url=canonical, **protocol)
    assert rejected.value.reason is GovernmentErrorReason.INVALID_SOURCE
    recorded = await service.record_protocol(
        guild, recorder, next_election.id, source_url="https://poll.example.test:/round?view=next", **protocol,
    )
    assert recorded.status is ElectionStatus.PENDING_CERTIFICATION
