from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .admin_operators import AdminOperatorPolicy

MAINTENANCE_REASON_MAX_LENGTH = 250


class MaintenanceErrorReason(StrEnum):
    OPERATOR_REQUIRED = "operator_required"
    INVALID_REASON = "invalid_reason"


class MaintenanceError(Exception):
    def __init__(self, reason: MaintenanceErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    enabled: bool
    changed: bool


class MaintenanceRepository(Protocol):
    async def get_economy_read_only(self, discord_guild_id: int) -> bool: ...

    async def set_economy_read_only(
        self, discord_guild_id: int, read_only: bool, *, actor_discord_user_id: int, reason: str, at: datetime,
    ) -> bool: ...


class MaintenanceService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[MaintenanceRepository]], *,
        operator_policy: AdminOperatorPolicy, guild_id: int,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._operator_policy = operator_policy
        self._guild_id = guild_id

    def _authorize(self, guild_id: int | None, user_id: int) -> None:
        if not self._operator_policy.allows(guild_id, user_id):
            raise MaintenanceError(MaintenanceErrorReason.OPERATOR_REQUIRED)

    async def status(self, guild_id: int | None, user_id: int) -> bool:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            return await repository.get_economy_read_only(self._guild_id)

    async def set_read_only(
        self, guild_id: int | None, user_id: int, *, enabled: bool, reason: str,
    ) -> MaintenanceResult:
        self._authorize(guild_id, user_id)
        reason = reason.strip() if isinstance(reason, str) else ""
        if not 1 <= len(reason) <= MAINTENANCE_REASON_MAX_LENGTH or not reason.isprintable():
            raise MaintenanceError(MaintenanceErrorReason.INVALID_REASON)
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        async with self._unit_of_work() as repository:
            changed = await repository.set_economy_read_only(
                self._guild_id, enabled, actor_discord_user_id=user_id, reason=reason, at=datetime.now(UTC),
            )
        return MaintenanceResult(enabled, changed)
