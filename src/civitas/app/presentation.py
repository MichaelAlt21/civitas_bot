"""Configure the guild's Discord roles and public report channel."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .admission import AdmissionError, AdmissionErrorReason
from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id


class PresentationBinding(StrEnum):
    CITIZEN_ROLE = "citizen_role_id"
    PRESIDENT_ROLE = "president_role_id"
    PARLIAMENT_ROLE = "parliament_role_id"
    REPORT_CHANNEL = "report_channel_id"


class PresentationErrorReason(StrEnum):
    INVALID_BINDING = "invalid_binding"
    INVALID_TARGET = "invalid_target"
    DUPLICATE_ROLE = "duplicate_role"


class PresentationError(Exception):
    def __init__(self, reason: PresentationErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class GuildPresentation:
    citizen_role_id: int | None = None
    president_role_id: int | None = None
    parliament_role_id: int | None = None
    report_channel_id: int | None = None


class PresentationRepository(Protocol):
    async def initialize(self, guild_id: int, initial: GuildPresentation) -> None: ...

    async def get_presentation(self, guild_id: int) -> GuildPresentation: ...

    async def set_binding(
        self, guild_id: int, binding: PresentationBinding, value: int | None, *, actor_user_id: int,
    ) -> bool: ...


class PresentationService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[PresentationRepository]], *, guild_id: int,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id

    async def initialize(self, initial: GuildPresentation) -> None:
        async with self._unit_of_work() as repository:
            await repository.initialize(self._guild_id, initial)

    async def current(self) -> GuildPresentation:
        async with self._unit_of_work() as repository:
            return await repository.get_presentation(self._guild_id)

    async def status(self, guild_id: int | None, actor_user_id: int, *, administrator: bool) -> GuildPresentation:
        self._require_administrator(guild_id, actor_user_id, administrator)
        return await self.current()

    async def set_binding(
        self,
        guild_id: int | None,
        actor_user_id: int,
        *,
        administrator: bool,
        binding: PresentationBinding,
        value: int | None,
    ) -> bool:
        self._require_administrator(guild_id, actor_user_id, administrator)
        if not isinstance(binding, PresentationBinding):
            raise PresentationError(PresentationErrorReason.INVALID_BINDING)
        if value is not None and (not is_discord_id(value) or value == self._guild_id):
            raise PresentationError(PresentationErrorReason.INVALID_TARGET)
        async with self._unit_of_work() as repository:
            return await repository.set_binding(self._guild_id, binding, value, actor_user_id=actor_user_id)

    def _require_administrator(self, guild_id: int | None, actor_user_id: int, administrator: bool) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
        if administrator is not True or not is_discord_id(actor_user_id):
            raise AdmissionError(AdmissionErrorReason.ADMINISTRATOR_REQUIRED)
