"""Project the citizenship status onto a single configured Discord role."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import discord

from civitas.app.citizen_roles import CitizenRoleReconciler, CitizenRoleRepository

logger = logging.getLogger(__name__)
_SYNC_INTERVAL_SECONDS = 60
_UNKNOWN_MEMBER = 10007


class DiscordCitizenRoleGateway:
    def __init__(self, guild: discord.Guild, role: discord.Role) -> None:
        self._guild = guild
        self._role = role

    async def set_citizen_role(self, discord_user_id: int, *, present: bool) -> None:
        await set_member_role(
            self._guild, self._role, discord_user_id, present=present, reason="Civitas citizenship sync"
        )


def safe_assignable_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    role = guild.get_role(role_id)
    bot_member = guild.me
    if (
        role is None
        or bot_member is None
        or not bot_member.guild_permissions.manage_roles
        or not role.is_assignable()
    ):
        return None

    permissions = role.permissions
    if permissions.administrator or permissions.manage_guild or permissions.manage_roles:
        return None
    return role


async def set_member_role(
    guild: discord.Guild, role: discord.Role, discord_user_id: int, *, present: bool, reason: str
) -> None:
    try:
        member = await guild.fetch_member(discord_user_id)
    except discord.NotFound as error:
        if error.code == _UNKNOWN_MEMBER:
            return
        raise

    has_role = member.get_role(role.id) is not None
    if present and not has_role:
        await member.add_roles(role, reason=reason)
    elif not present and has_role:
        await member.remove_roles(role, reason=reason)


async def _reconcile_once(
    client: discord.Client,
    unit_of_work: Callable[[], AbstractAsyncContextManager[CitizenRoleRepository]],
    guild_id: int,
    role_id: int,
) -> None:
    guild = client.get_guild(guild_id)
    if guild is None or guild.unavailable:
        logger.warning("Сервер Discord недоступен для синхронизации роли гражданина")
        return

    role = safe_assignable_role(guild, role_id)
    if role is None:
        logger.warning("Проверьте ID роли гражданина, иерархию ролей и право Manage Roles у бота")
        return

    gateway = DiscordCitizenRoleGateway(guild, role)
    await CitizenRoleReconciler(unit_of_work, gateway, guild_id=guild_id).reconcile()


@asynccontextmanager
async def run_role_sync(
    client: discord.Client,
    unit_of_work: Callable[[], AbstractAsyncContextManager[CitizenRoleRepository]],
    *,
    guild_id: int,
    role_id: int | None,
) -> AsyncIterator[None]:
    if role_id is None:
        yield
        return

    async def sync_periodically() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await _reconcile_once(client, unit_of_work, guild_id, role_id)
            except Exception as error:
                logger.warning("Сверка роли гражданина завершилась ошибкой (%s)", type(error).__name__)
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)

    task = asyncio.create_task(sync_periodically())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
