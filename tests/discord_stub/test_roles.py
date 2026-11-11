import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from civitas.bot import roles
from civitas.domain.citizens import Citizen, CitizenStatus

GUILD_ID = 100
ROLE_ID = 200
USER_ID = 300


def _member_with_role(role: discord.Role | None):
    return SimpleNamespace(
        get_role=Mock(return_value=role), add_roles=AsyncMock(), remove_roles=AsyncMock()
    )


@pytest.fixture
def discord_server():
    role = SimpleNamespace(
        id=ROLE_ID, name="Citizen", permissions=discord.Permissions.none(),
        is_assignable=Mock(return_value=True),
    )
    guild = SimpleNamespace(
        unavailable=False, get_role=Mock(return_value=role), fetch_member=AsyncMock(),
        me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
    )
    client = SimpleNamespace(
        get_guild=Mock(return_value=guild), wait_until_ready=AsyncMock(),
        is_closed=Mock(return_value=False),
    )
    return client, guild, role


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("present", "has_role"), [(True, False), (True, True), (False, True), (False, False)]
)
async def test_gateway_changes_only_the_configured_role(discord_server, present, has_role):
    _, guild, role = discord_server
    member = _member_with_role(role if has_role else None)
    guild.fetch_member.return_value = member

    await roles.DiscordCitizenRoleGateway(guild, role).set_citizen_role(USER_ID, present=present)

    guild.fetch_member.assert_awaited_once_with(USER_ID)
    member.get_role.assert_called_once_with(ROLE_ID)
    if present and not has_role:
        member.add_roles.assert_awaited_once_with(role, reason="Civitas citizenship sync")
    else:
        member.add_roles.assert_not_awaited()
    if not present and has_role:
        member.remove_roles.assert_awaited_once_with(role, reason="Civitas citizenship sync")
    else:
        member.remove_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_skips_departed_discord_member(discord_server):
    _, guild, role = discord_server
    guild.fetch_member.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), {"code": 10007, "message": "Unknown Member"}
    )

    await roles.DiscordCitizenRoleGateway(guild, role).set_citizen_role(USER_ID, present=True)

    guild.fetch_member.assert_awaited_once_with(USER_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["administrator", "manage_guild", "manage_roles"])
async def test_worker_refuses_privileged_role(discord_server, permission):
    client, guild, role = discord_server
    setattr(role.permissions, permission, True)
    unit_of_work = Mock()

    await roles._reconcile_once(client, unit_of_work, GUILD_ID, ROLE_ID)

    guild.get_role.assert_called_once_with(ROLE_ID)
    guild.fetch_member.assert_not_awaited()
    unit_of_work.assert_not_called()


@pytest.mark.asyncio
async def test_worker_retries_after_a_database_failure(discord_server, monkeypatch):
    client, guild, role = discord_server
    attempts = 0
    synced = asyncio.Event()
    timestamp = datetime.now(UTC)
    record = Citizen(1, 1, USER_ID, CitizenStatus.VERIFIED, timestamp, timestamp)
    repository = SimpleNamespace(list_role_members=AsyncMock(return_value=(record,)))

    @asynccontextmanager
    async def unit_of_work():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")
        yield repository

    member = _member_with_role(None)
    member.add_roles.side_effect = lambda *_args, **_kwargs: synced.set()
    guild.fetch_member.return_value = member
    monkeypatch.setattr(roles, "_SYNC_INTERVAL_SECONDS", 0)

    async with roles.run_role_sync(client, unit_of_work, guild_id=GUILD_ID, role_id=ROLE_ID):
        await asyncio.wait_for(synced.wait(), timeout=1)

    client.wait_until_ready.assert_awaited_once()
    repository.list_role_members.assert_awaited_with(GUILD_ID)
    member.add_roles.assert_awaited_with(role, reason="Civitas citizenship sync")
