"""Reconcile the Discord citizen role with persisted citizenship status."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus

logger = logging.getLogger(__name__)


class CitizenRoleRepository(Protocol):
    async def list_role_members(self, guild_id: int) -> tuple[Citizen, ...]: ...


class CitizenRoleGateway(Protocol):
    async def set_citizen_role(self, discord_user_id: int, *, present: bool) -> None: ...


class CitizenRoleReconciler:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[CitizenRoleRepository]],
        gateway: CitizenRoleGateway,
        *,
        guild_id: int,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._gateway = gateway
        self._guild_id = guild_id

    async def reconcile(self) -> None:
        async with self._unit_of_work() as repository:
            citizens = await repository.list_role_members(self._guild_id)

        for citizen in citizens:
            try:
                await self._gateway.set_citizen_role(
                    citizen.discord_user_id, present=citizen.status == CitizenStatus.VERIFIED
                )
            except Exception as error:
                logger.warning("Не удалось синхронизировать роль гражданина (%s)", type(error).__name__)
