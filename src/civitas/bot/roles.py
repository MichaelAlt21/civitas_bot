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
        try:
            member = await self._guild.fetch_member(discord_user_id)
        except discord.NotFound as error:
            if error.code == _UNKNOWN_MEMBER:
                return
            raise

        has_role = member.get_role(self._role.id) is not None
        if present and not has_role:
            await member.add_roles(self._role, reason="Civitas citizenship sync")
        elif not present and has_role:
            await member.remove_roles(self._role, reason="Civitas citizenship sync")


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

    role = guild.get_role(role_id)
    bot_member = guild.me
    if (
        role is None
        or bot_member is None
        or not bot_member.guild_permissions.manage_roles
        or not role.is_assignable()
    ):
        logger.warning("Проверьте ID роли гражданина, иерархию ролей и право Manage Roles у бота")
        return

    permissions = role.permissions
    if permissions.administrator or permissions.manage_guild or permissions.manage_roles:
        logger.warning("Выберите роль гражданина без Administrator, Manage Server и Manage Roles")
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
