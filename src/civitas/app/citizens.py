"""Manage citizenship and access for the configured guild."""

from collections.abc import Awaitable, Callable, Iterable
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
    async def get_profile(self, guild_id: int, user_id: int, *, lock: bool = False) -> Citizen | None: ...

    async def get_or_create_verified(
        self, guild_id: int, user_id: int, now: datetime
    ) -> tuple[Citizen, bool]:
        """Insert a verified citizen or lock and return the existing citizen."""
        ...

    async def update_citizen_status(self, citizen: Citizen) -> None: ...

    async def append_audit(self, event: CitizenAuditEvent) -> None: ...


class CitizensService[Repository: CitizensRepository]:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[Repository]],
        *,
        guild_id: int,
        allowed_user_ids: Iterable[int],
        fund_verified: Callable[[Repository, Citizen], Awaitable[object]] | None = None,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        allowed_ids = tuple(allowed_user_ids)
        if not all(is_discord_id(user_id) for user_id in allowed_ids):
            raise ValueError("allowed_user_ids must contain Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._allowed_user_ids = frozenset(allowed_ids)
        self._fund_verified = fund_verified

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
                outcome = RegistrationOutcome.CREATED
            elif citizen.status == CitizenStatus.LEFT:
                now = datetime.now(UTC)
                citizen = replace(citizen, status=CitizenStatus.VERIFIED, verified_at=now)
                await repository.update_citizen_status(citizen)
                await repository.append_audit(
                    CitizenAuditEvent(citizen, CitizenAuditAction.RESTORED, now, "discord_allowlist")
                )
                outcome = RegistrationOutcome.RESTORED
            else:
                outcome = RegistrationOutcome.EXISTING

            if self._fund_verified is not None and citizen.status is CitizenStatus.VERIFIED:
                await self._fund_verified(repository, citizen)
            return RegistrationResult(citizen, outcome)

    async def mark_departed(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        observed_at: datetime | None = None,
        actor_discord_user_id: int | None = None,
    ) -> bool:
        """Record departure reported by the trusted Discord membership adapter."""
        if not is_discord_id(guild_id) or guild_id != self._guild_id or not is_discord_id(user_id):
            return False
        if actor_discord_user_id is not None and not is_discord_id(actor_discord_user_id):
            raise ValueError("actor_discord_user_id must be a Discord snowflake")
        if observed_at is None:
            observed_at = datetime.now(UTC)
        elif observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a time zone")
        else:
            observed_at = observed_at.astimezone(UTC)

        async with self._unit_of_work() as repository:
            citizen = await repository.get_profile(self._guild_id, user_id, lock=True)
            if citizen is None or citizen.status not in (CitizenStatus.VERIFIED, CitizenStatus.PENDING):
                return False
            if observed_at <= (citizen.verified_at or citizen.joined_at):
                return False
            departed = replace(citizen, status=CitizenStatus.LEFT)
            await repository.update_citizen_status(departed)
            await repository.append_audit(CitizenAuditEvent(
                departed, CitizenAuditAction.LEFT, observed_at, "discord_membership", actor_discord_user_id,
            ))
            return True

    async def get_profile(self, guild_id: int | None, user_id: int) -> Citizen | None:
        self.authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            return await repository.get_profile(self._guild_id, user_id)
