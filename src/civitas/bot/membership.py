"""Reconcile stored citizenship after missed Discord departure events."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

import discord

from civitas.app.citizen_roles import CitizenRoleRepository
from civitas.app.citizens import CitizensService
from civitas.domain.citizens import CitizenStatus

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 300
_UNKNOWN_MEMBER = 10007


async def _reconcile_membership(
    client: discord.Client, unit_of_work: Callable[[], AbstractAsyncContextManager[CitizenRoleRepository]],
    service: CitizensService, guild_id: int,
) -> None:
    guild = client.get_guild(guild_id)
    if guild is None or guild.unavailable or client.user is None:
        return
    actor_id = client.user.id
    async with unit_of_work() as repository:
        citizens = await repository.list_role_members(guild_id)
    for citizen in citizens:
        if citizen.status not in (CitizenStatus.VERIFIED, CitizenStatus.PENDING):
            continue
        observed_at = datetime.now(UTC)
        try:
            await guild.fetch_member(citizen.discord_user_id)
        except discord.NotFound as error:
            if error.code == _UNKNOWN_MEMBER:
                await service.mark_departed(
                    guild_id, citizen.discord_user_id, observed_at=observed_at, actor_discord_user_id=actor_id,
                )
            else:
                raise


@asynccontextmanager
async def run_membership_sync(
    client: discord.Client, unit_of_work: Callable[[], AbstractAsyncContextManager[CitizenRoleRepository]],
    service: CitizensService, *, guild_id: int, enabled: bool,
) -> AsyncIterator[None]:
    if not enabled:
        yield
        return

    async def run() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await _reconcile_membership(client, unit_of_work, service, guild_id)
            except Exception as error:
                logger.warning("Ошибка сверки состава Discord (%s)", type(error).__name__)
            await asyncio.sleep(_INTERVAL_SECONDS)

    task = asyncio.create_task(run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
