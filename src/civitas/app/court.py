"""Nominate and appoint the chief justice in the configured guild."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import OfficeKind, OfficeTerm
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id

COURT_REASON_MAX_LENGTH = 200
_TERM_LENGTH = timedelta(days=28)
_REQUIRED_APPROVALS = 2


@dataclass(frozen=True, slots=True)
class CourtNomination:
    id: int
    guild_id: int
    president_term_id: int
    proposer_citizen_id: int
    candidate_citizen_id: int
    candidate_user_id: int
    reason: str
    proposed_at: datetime
    approval_count: int = 0
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CourtErrorReason(StrEnum):
    PRESIDENT_REQUIRED = "president_required"
    DEPUTY_REQUIRED = "deputy_required"
    INVALID_CANDIDATE = "invalid_candidate"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    OFFICE_OCCUPIED = "office_occupied"
    NOMINATION_NOT_FOUND = "nomination_not_found"
    NOMINATION_EXPIRED = "nomination_expired"
    INVALID_REASON = "invalid_reason"
    INVALID_REQUEST = "invalid_request"


class CourtError(Exception):
    def __init__(self, reason: CourtErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class CourtRepository(Protocol):
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...

    async def lock_election_cycle(self, guild_id: int) -> None: ...

    async def get_current_presidential_term(self, guild_id: int, at: datetime) -> tuple[int, OfficeTerm] | None: ...

    async def is_allowed(self, discord_guild_id: int, user_id: int) -> bool: ...

    async def has_active_office(self, guild_id: int, citizen_id: int, office: OfficeKind, at: datetime) -> bool: ...

    async def get_court_nomination(self, guild_id: int, nomination_id: int) -> CourtNomination | None: ...

    async def get_court_nomination_for_discord(self, guild_id: int, nomination_id: int) -> CourtNomination | None: ...

    async def find_court_nomination(self, guild_id: int, request_key: str) -> CourtNomination | None: ...

    async def add_court_nomination(self, nomination: CourtNomination, request_key: str) -> CourtNomination: ...

    async def add_court_approval(
        self, guild_id: int, nomination_id: int, deputy_citizen_id: int, *, reason: str, at: datetime,
    ) -> bool: ...

    async def activate_court_nomination(
        self, guild_id: int, nomination_id: int, *, starts_at: datetime, ends_at: datetime,
    ) -> CourtNomination: ...

    async def get_active_court_nomination(self, discord_guild_id: int, at: datetime) -> CourtNomination | None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_reason(value: str) -> str:
    if not isinstance(value, str):
        raise CourtError(CourtErrorReason.INVALID_REASON)
    reason = value.strip()
    if not reason or len(reason) > COURT_REASON_MAX_LENGTH or not reason.isprintable():
        raise CourtError(CourtErrorReason.INVALID_REASON)
    return reason


def _validate_nomination_id(nomination_id: int) -> None:
    if type(nomination_id) is not int or not 1 <= nomination_id <= MAX_AMOUNT:
        raise CourtError(CourtErrorReason.NOMINATION_NOT_FOUND)


class CourtService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[CourtRepository]],
        *,
        guild_id: int,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._clock = clock

    async def current(self, guild_id: int | None) -> CourtNomination | None:
        self._require_guild(guild_id)
        async with self._unit_of_work() as repository:
            return await repository.get_active_court_nomination(self._guild_id, self._now())

    async def get_nomination(self, guild_id: int | None, nomination_id: int) -> CourtNomination:
        self._require_guild(guild_id)
        _validate_nomination_id(nomination_id)
        async with self._unit_of_work() as repository:
            nomination = await repository.get_court_nomination_for_discord(self._guild_id, nomination_id)
            if nomination is None:
                raise CourtError(CourtErrorReason.NOMINATION_NOT_FOUND)
            return nomination

    async def nominate(
        self, guild_id: int | None, user_id: int, *, candidate_user_id: int, reason: str, idempotency_key: str,
    ) -> CourtNomination:
        self._require_guild(guild_id)
        reason = _validate_reason(reason)
        if not is_discord_id(candidate_user_id):
            raise CourtError(CourtErrorReason.INVALID_CANDIDATE)
        if (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH or not idempotency_key.isprintable()
        ):
            raise CourtError(CourtErrorReason.INVALID_REQUEST)

        async with self._unit_of_work() as repository:
            president = await self._lock_for_actor(repository, user_id)
            existing = await repository.find_court_nomination(president.guild_id, idempotency_key)
            if existing is not None:
                if (existing.proposer_citizen_id, existing.candidate_user_id, existing.reason) != (
                    president.id, candidate_user_id, reason,
                ):
                    raise CourtError(CourtErrorReason.INVALID_REQUEST)
                return existing

            now = self._now()
            active = await repository.get_current_presidential_term(president.guild_id, now)
            if active is None or active[1].holder_citizen_id != president.id:
                raise CourtError(CourtErrorReason.PRESIDENT_REQUIRED)
            if await repository.get_active_court_nomination(self._guild_id, now) is not None:
                raise CourtError(CourtErrorReason.OFFICE_OCCUPIED)
            candidate = await self._get_eligible_citizen(repository, candidate_user_id)
            if candidate is None:
                raise CourtError(CourtErrorReason.INVALID_CANDIDATE)
            if candidate.id == president.id:
                raise CourtError(CourtErrorReason.CONFLICT_OF_INTEREST)
            return await repository.add_court_nomination(CourtNomination(
                id=0, guild_id=president.guild_id, president_term_id=active[0], proposer_citizen_id=president.id,
                candidate_citizen_id=candidate.id, candidate_user_id=candidate_user_id, reason=reason, proposed_at=now,
            ), idempotency_key)

    async def approve(
        self, guild_id: int | None, user_id: int, nomination_id: int, *, reason: str,
    ) -> CourtNomination:
        self._require_guild(guild_id)
        _validate_nomination_id(nomination_id)
        reason = _validate_reason(reason)
        async with self._unit_of_work() as repository:
            deputy = await self._lock_for_actor(repository, user_id)
            now = self._now()
            if not await repository.has_active_office(deputy.guild_id, deputy.id, OfficeKind.PARLIAMENT, now):
                raise CourtError(CourtErrorReason.DEPUTY_REQUIRED)
            nomination = await repository.get_court_nomination(deputy.guild_id, nomination_id)
            if nomination is None:
                raise CourtError(CourtErrorReason.NOMINATION_NOT_FOUND)
            if deputy.id in (nomination.proposer_citizen_id, nomination.candidate_citizen_id):
                raise CourtError(CourtErrorReason.CONFLICT_OF_INTEREST)
            if nomination.starts_at is not None:
                return nomination

            active = await repository.get_current_presidential_term(deputy.guild_id, now)
            if (
                active is None or active[0] != nomination.president_term_id
                or active[1].holder_citizen_id != nomination.proposer_citizen_id
            ):
                raise CourtError(CourtErrorReason.NOMINATION_EXPIRED)
            if await repository.get_active_court_nomination(self._guild_id, now) is not None:
                raise CourtError(CourtErrorReason.OFFICE_OCCUPIED)
            if await self._get_eligible_citizen(repository, nomination.candidate_user_id) is None:
                raise CourtError(CourtErrorReason.INVALID_CANDIDATE)
            approved = await repository.add_court_approval(
                deputy.guild_id, nomination.id, deputy.id, reason=reason, at=now,
            )
            if not approved:
                return nomination

            count = nomination.approval_count + 1
            if count < _REQUIRED_APPROVALS:
                return replace(nomination, approval_count=count)
            return await repository.activate_court_nomination(
                deputy.guild_id, nomination.id, starts_at=now, ends_at=now + _TERM_LENGTH,
            )

    async def _lock_for_actor(self, repository: CourtRepository, user_id: int) -> Citizen:
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        actor = await repository.get_profile(self._guild_id, user_id)
        if actor is None:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        await repository.lock_election_cycle(actor.guild_id)
        fresh_actor = await self._get_eligible_citizen(repository, user_id)
        if fresh_actor is None:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        return fresh_actor

    async def _get_eligible_citizen(self, repository: CourtRepository, user_id: int) -> Citizen | None:
        citizen = await repository.get_profile(self._guild_id, user_id)
        if citizen is None or citizen.status is not CitizenStatus.VERIFIED:
            return None
        return citizen if await repository.is_allowed(self._guild_id, user_id) else None

    def _require_guild(self, guild_id: int | None) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Court clock must include a time zone")
        return now.astimezone(UTC)
