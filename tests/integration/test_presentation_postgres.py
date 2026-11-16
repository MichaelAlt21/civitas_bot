import asyncio

import pytest
from sqlalchemy import select

from civitas.app.admission import AdmissionError
from civitas.app.presentation import GuildPresentation, PresentationBinding, PresentationError, PresentationService
from civitas.infrastructure.db.presentation import PostgresPresentationUnitOfWork
from civitas.infrastructure.db.presentation_models import PresentationAuditRow


@pytest.mark.asyncio
async def test_presentation_serializes_role_changes_and_does_not_restore_cleared_defaults(pg_engine):
    service = PresentationService(PostgresPresentationUnitOfWork(pg_engine), guild_id=101)
    initial = GuildPresentation(report_channel_id=301)
    await service.initialize(initial)

    async def set_role(binding):
        return await service.set_binding(101, 401, administrator=True, binding=binding, value=201)

    with pytest.raises(AdmissionError, match="administrator_required"):
        await service.status(101, 401, administrator=False)

    choices = (PresentationBinding.CITIZEN_ROLE, PresentationBinding.CUSTODY_ROLE)
    results = await asyncio.gather(*(set_role(binding) for binding in choices), return_exceptions=True)
    winner = choices[results.index(True)]
    failure = next(result for result in results if isinstance(result, PresentationError))
    assert failure.reason.value == "duplicate_role"
    assert getattr(await service.current(), winner.value) == 201
    assert not await set_role(winner)
    assert await service.set_binding(101, 401, administrator=True, binding=winner, value=None)
    assert await service.set_binding(
        101, 401, administrator=True, binding=PresentationBinding.REPORT_CHANNEL, value=None,
    )

    restarted = PresentationService(PostgresPresentationUnitOfWork(pg_engine), guild_id=101)
    await restarted.initialize(initial)
    assert await restarted.status(101, 401, administrator=True) == GuildPresentation()
    async with pg_engine.connect() as connection:
        fields = await connection.execute(select(
            PresentationAuditRow.binding, PresentationAuditRow.previous_discord_id,
            PresentationAuditRow.new_discord_id, PresentationAuditRow.actor_discord_user_id,
        ).order_by(PresentationAuditRow.id))
        assert fields.all() == [
            (winner.value, None, 201, 401),
            (winner.value, 201, None, 401),
            (PresentationBinding.REPORT_CHANNEL.value, 301, None, 401),
        ]
