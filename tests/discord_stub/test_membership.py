from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from civitas.bot.membership import _reconcile_membership
from civitas.domain.citizens import Citizen, CitizenStatus


@pytest.mark.asyncio
async def test_membership_reconciles_definitive_departures_outside_read_transaction():
    now = datetime.now(UTC)
    citizens = (
        Citizen(1, 1, 20, CitizenStatus.VERIFIED, now, now),
        Citizen(2, 1, 21, CitizenStatus.PENDING, now, None),
        Citizen(3, 1, 22, CitizenStatus.SUSPENDED, now, now),
    )
    active = False

    @asynccontextmanager
    async def uow():
        nonlocal active
        active = True
        try:
            yield SimpleNamespace(list_role_members=AsyncMock(return_value=citizens))
        finally:
            active = False

    async def fetch_member(user_id):
        assert not active
        if user_id == 21:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10007})

    guild = SimpleNamespace(unavailable=False, fetch_member=AsyncMock(side_effect=fetch_member))
    client = SimpleNamespace(get_guild=Mock(return_value=guild), user=SimpleNamespace(id=900))
    service = SimpleNamespace(mark_departed=AsyncMock(return_value=True))
    await _reconcile_membership(client, uow, service, 101)

    assert [call.args[0] for call in guild.fetch_member.await_args_list] == [20, 21]
    change = service.mark_departed.await_args
    assert change.args == (101, 21)
    assert change.kwargs["actor_discord_user_id"] == 900
    assert change.kwargs["observed_at"] >= now
