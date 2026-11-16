"""Manage the guild's registration and election admission list."""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Protocol

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id

ADMISSION_PAGE_SIZE = 10
ADMISSION_MAX_PAGE = 1000
ADMISSION_REASON_MAX_LENGTH = 200


class AdmissionErrorReason(StrEnum):
    ADMINISTRATOR_REQUIRED = "administrator_required"
    INVALID_TARGET = "invalid_target"
    INVALID_REASON = "invalid_reason"
    INVALID_PAGE = "invalid_page"


class AdmissionError(Exception):
    def __init__(self, reason: AdmissionErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class AdmissionRepository(Protocol):
    async def initialize(self, guild_id: int, initial_user_ids: Iterable[int]) -> None: ...

    async def is_allowed(self, guild_id: int, user_id: int) -> bool: ...

    async def list_allowed(self, guild_id: int, *, limit: int, offset: int) -> tuple[int, ...]: ...

    async def set_allowed(
        self, guild_id: int, user_id: int, *, allowed: bool, actor_user_id: int, reason: str,
    ) -> bool: ...


class AdmissionService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[AdmissionRepository]], *, guild_id: int,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id

    async def initialize(self, initial_user_ids: Iterable[int]) -> None:
        initial_ids = frozenset(initial_user_ids)
        if not all(is_discord_id(user_id) for user_id in initial_ids):
            raise ValueError("initial_user_ids must contain Discord snowflakes")
        async with self._unit_of_work() as repository:
            await repository.initialize(self._guild_id, initial_ids)

    async def authorize(self, guild_id: int | None, user_id: int) -> None:
        self._require_guild(guild_id)
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        async with self._unit_of_work() as repository:
            if not await repository.is_allowed(self._guild_id, user_id):
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)

    async def list_allowed(
        self, guild_id: int | None, actor_user_id: int, *, administrator: bool, page: int = 1,
    ) -> tuple[int, ...]:
        self._require_administrator(guild_id, actor_user_id, administrator)
        if type(page) is not int or not 1 <= page <= ADMISSION_MAX_PAGE:
            raise AdmissionError(AdmissionErrorReason.INVALID_PAGE)
        async with self._unit_of_work() as repository:
            return await repository.list_allowed(
                self._guild_id, limit=ADMISSION_PAGE_SIZE, offset=(page - 1) * ADMISSION_PAGE_SIZE,
            )

    async def set_allowed(
        self,
        guild_id: int | None,
        actor_user_id: int,
        *,
        administrator: bool,
        target_user_id: int,
        allowed: bool,
        reason: str = "",
    ) -> bool:
        self._require_administrator(guild_id, actor_user_id, administrator)
        if not is_discord_id(target_user_id):
            raise AdmissionError(AdmissionErrorReason.INVALID_TARGET)
        reason = reason.strip()
        if reason and (not reason.isprintable() or len(reason) > ADMISSION_REASON_MAX_LENGTH):
            raise AdmissionError(AdmissionErrorReason.INVALID_REASON)
        if not allowed and not reason:
            raise AdmissionError(AdmissionErrorReason.INVALID_REASON)
        async with self._unit_of_work() as repository:
            return await repository.set_allowed(
                self._guild_id, target_user_id, allowed=allowed, actor_user_id=actor_user_id,
                reason=reason or "discord_administrator",
            )

    def _require_guild(self, guild_id: int | None) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)

    def _require_administrator(self, guild_id: int | None, actor_user_id: int, administrator: bool) -> None:
        self._require_guild(guild_id)
        if administrator is not True or not is_discord_id(actor_user_id):
            raise AdmissionError(AdmissionErrorReason.ADMINISTRATOR_REQUIRED)
