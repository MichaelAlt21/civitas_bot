"""Authorize registration and profile access for the configured guild."""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenAuditAction, CitizenAuditEvent, CitizenStatus

MAX_DISCORD_ID = 2**64 - 1


def is_discord_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_DISCORD_ID


class AccessDeniedReason(StrEnum):
    WRONG_GUILD = "wrong_guild"
    NOT_ALLOWED = "not_allowed"


class CitizenshipAccessError(Exception):
    def __init__(self, reason: AccessDeniedReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class RegistrationOutcome(StrEnum):
    CREATED = "created"
    RESTORED = "restored"
    EXISTING = "existing"


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    citizen: Citizen
    outcome: RegistrationOutcome


class CitizensRepository(Protocol):
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...

    async def get_or_create_verified(
        self, guild_id: int, user_id: int, now: datetime
    ) -> tuple[Citizen, bool]:
        """Insert a verified citizen or lock and return the existing citizen."""
        ...

    async def restore(self, citizen: Citizen) -> None: ...

    async def append_audit(self, event: CitizenAuditEvent) -> None: ...


class CitizensService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[CitizensRepository]],
        *,
        guild_id: int,
        allowed_user_ids: Iterable[int],
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        allowed_ids = tuple(allowed_user_ids)
        if not all(is_discord_id(user_id) for user_id in allowed_ids):
            raise ValueError("allowed_user_ids must contain Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._allowed_user_ids = frozenset(allowed_ids)

    def authorize(self, guild_id: int | None, user_id: int) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
        if not is_discord_id(user_id) or user_id not in self._allowed_user_ids:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)

    async def register(self, guild_id: int | None, user_id: int) -> RegistrationResult:
        self.authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            now = datetime.now(UTC)
            citizen, created = await repository.get_or_create_verified(self._guild_id, user_id, now)
            if created:
                await repository.append_audit(
                    CitizenAuditEvent(citizen, CitizenAuditAction.REGISTERED, now, "discord_allowlist")
                )
                return RegistrationResult(citizen, RegistrationOutcome.CREATED)

            if citizen.status == CitizenStatus.LEFT:
                citizen = replace(citizen, status=CitizenStatus.VERIFIED, verified_at=now)
                await repository.restore(citizen)
                await repository.append_audit(
                    CitizenAuditEvent(citizen, CitizenAuditAction.RESTORED, now, "discord_allowlist")
                )
                return RegistrationResult(citizen, RegistrationOutcome.RESTORED)

            return RegistrationResult(citizen, RegistrationOutcome.EXISTING)

    async def get_profile(self, guild_id: int | None, user_id: int) -> Citizen | None:
        self.authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            return await repository.get_profile(self._guild_id, user_id)
