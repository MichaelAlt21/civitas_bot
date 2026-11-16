"""Appoint and remove the prosecutor in the configured guild."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import OfficeKind, OfficeTerm
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id

PROSECUTOR_REASON_MAX_LENGTH = 200
_REQUIRED_APPROVALS = 2


class ProsecutorAction(StrEnum):
    APPOINT = "APPOINT"
    REMOVE = "REMOVE"


@dataclass(frozen=True, slots=True)
class ProsecutorMotion:
    id: int
    guild_id: int
    action: ProsecutorAction
    president_term_id: int
    proposer_citizen_id: int
    subject_citizen_id: int
    subject_user_id: int
    appointment_id: int | None
    reason: str
    proposed_at: datetime
    approval_count: int = 0
    applied_at: datetime | None = None


class ProsecutorErrorReason(StrEnum):
    PRESIDENT_REQUIRED = "president_required"
    SENATOR_REQUIRED = "senator_required"
    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_ACTION = "invalid_action"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    OFFICE_OCCUPIED = "office_occupied"
    OFFICE_VACANT = "office_vacant"
    MOTION_NOT_FOUND = "motion_not_found"
    MOTION_EXPIRED = "motion_expired"
    STALE_APPOINTMENT = "stale_appointment"
    INVALID_REASON = "invalid_reason"
    INVALID_REQUEST = "invalid_request"


class ProsecutorError(Exception):
    def __init__(self, reason: ProsecutorErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ProsecutorRepository(Protocol):
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...

    async def lock_election_cycle(self, guild_id: int) -> None: ...

    async def get_current_presidential_term(self, guild_id: int, at: datetime) -> tuple[int, OfficeTerm] | None: ...

    async def is_allowed(self, discord_guild_id: int, user_id: int) -> bool: ...

    async def has_active_office(self, guild_id: int, citizen_id: int, office: OfficeKind, at: datetime) -> bool: ...

    async def find_prosecutor_motion(self, guild_id: int, key: str) -> ProsecutorMotion | None: ...

    async def add_prosecutor_motion(self, motion: ProsecutorMotion, key: str) -> ProsecutorMotion: ...

    async def get_prosecutor_motion(self, discord_guild_id: int, motion_id: int) -> ProsecutorMotion | None: ...

    async def get_active_prosecutor(self, discord_guild_id: int) -> ProsecutorMotion | None: ...

    async def add_prosecutor_approval(
        self, guild_id: int, motion_id: int, senator_citizen_id: int, *, reason: str, at: datetime,
    ) -> bool: ...

    async def apply_prosecutor_motion(self, guild_id: int, motion_id: int, *, at: datetime) -> ProsecutorMotion: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_reason(value: str) -> str:
    if not isinstance(value, str):
        raise ProsecutorError(ProsecutorErrorReason.INVALID_REASON)
    reason = value.strip()
    if not reason or len(reason) > PROSECUTOR_REASON_MAX_LENGTH or not reason.isprintable():
        raise ProsecutorError(ProsecutorErrorReason.INVALID_REASON)
    return reason


class ProsecutorService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[ProsecutorRepository]],
        *,
        guild_id: int,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._clock = clock

    async def current(self, guild_id: int | None) -> ProsecutorMotion | None:
        self._require_guild(guild_id)
        async with self._unit_of_work() as repository:
            return await repository.get_active_prosecutor(self._guild_id)

    async def motion(self, guild_id: int | None, motion_id: int) -> ProsecutorMotion:
        self._require_guild(guild_id)
        self._validate_motion_id(motion_id)
        async with self._unit_of_work() as repository:
            return await self._require_motion(repository, motion_id)

    async def propose(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        action: ProsecutorAction,
        candidate_user_id: int | None = None,
        reason: str,
        idempotency_key: str,
    ) -> ProsecutorMotion:
        self._require_guild(guild_id)
        reason = _validate_reason(reason)
        if not isinstance(action, ProsecutorAction):
            raise ProsecutorError(ProsecutorErrorReason.INVALID_ACTION)
        if action is ProsecutorAction.APPOINT and not is_discord_id(candidate_user_id):
            raise ProsecutorError(ProsecutorErrorReason.INVALID_CANDIDATE)
        if action is ProsecutorAction.REMOVE and candidate_user_id is not None:
            raise ProsecutorError(ProsecutorErrorReason.INVALID_REQUEST)
        if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH or not idempotency_key.isprintable()):
            raise ProsecutorError(ProsecutorErrorReason.INVALID_REQUEST)

        async with self._unit_of_work() as repository:
            president = await self._lock_for_actor(repository, user_id)
            existing = await repository.find_prosecutor_motion(president.guild_id, idempotency_key)
            if existing is not None:
                saved_candidate = existing.subject_user_id if existing.action is ProsecutorAction.APPOINT else None
                if (existing.proposer_citizen_id, existing.action, saved_candidate, existing.reason) != (
                    president.id, action, candidate_user_id, reason,
                ):
                    raise ProsecutorError(ProsecutorErrorReason.INVALID_REQUEST)
                return existing

            now = self._now()
            presidential_term = await repository.get_current_presidential_term(president.guild_id, now)
            if presidential_term is None or presidential_term[1].holder_citizen_id != president.id:
                raise ProsecutorError(ProsecutorErrorReason.PRESIDENT_REQUIRED)
            incumbent = await repository.get_active_prosecutor(self._guild_id)
            if action is ProsecutorAction.APPOINT:
                if incumbent is not None:
                    raise ProsecutorError(ProsecutorErrorReason.OFFICE_OCCUPIED)
                assert candidate_user_id is not None
                candidate = await self._get_eligible_citizen(repository, candidate_user_id)
                if candidate is None:
                    raise ProsecutorError(ProsecutorErrorReason.INVALID_CANDIDATE)
                subject_citizen_id = candidate.id
                subject_user_id = candidate.discord_user_id
                appointment_id = None
            else:
                if incumbent is None:
                    raise ProsecutorError(ProsecutorErrorReason.OFFICE_VACANT)
                subject_citizen_id = incumbent.subject_citizen_id
                subject_user_id = incumbent.subject_user_id
                appointment_id = incumbent.id
            motion = ProsecutorMotion(
                id=0,
                guild_id=president.guild_id,
                action=action,
                president_term_id=presidential_term[0],
                proposer_citizen_id=president.id,
                subject_citizen_id=subject_citizen_id,
                subject_user_id=subject_user_id,
                appointment_id=appointment_id,
                reason=reason,
                proposed_at=now,
            )
            return await repository.add_prosecutor_motion(motion, idempotency_key)

    async def approve(
        self, guild_id: int | None, user_id: int, motion_id: int, *, reason: str,
    ) -> ProsecutorMotion:
        self._require_guild(guild_id)
        self._validate_motion_id(motion_id)
        reason = _validate_reason(reason)
        async with self._unit_of_work() as repository:
            senator = await self._lock_for_actor(repository, user_id)
            now = self._now()
            if not await repository.has_active_office(senator.guild_id, senator.id, OfficeKind.PARLIAMENT, now):
                raise ProsecutorError(ProsecutorErrorReason.SENATOR_REQUIRED)
            motion = await self._require_motion(repository, motion_id)
            if senator.id in (motion.proposer_citizen_id, motion.subject_citizen_id):
                raise ProsecutorError(ProsecutorErrorReason.CONFLICT_OF_INTEREST)
            if motion.applied_at is not None:
                return motion

            presidential_term = await repository.get_current_presidential_term(senator.guild_id, now)
            if (presidential_term is None or presidential_term[0] != motion.president_term_id
                    or presidential_term[1].holder_citizen_id != motion.proposer_citizen_id):
                raise ProsecutorError(ProsecutorErrorReason.MOTION_EXPIRED)
            incumbent = await repository.get_active_prosecutor(self._guild_id)
            if motion.action is ProsecutorAction.APPOINT:
                if incumbent is not None:
                    raise ProsecutorError(ProsecutorErrorReason.OFFICE_OCCUPIED)
                candidate = await self._get_eligible_citizen(repository, motion.subject_user_id)
                if candidate is None or candidate.id != motion.subject_citizen_id:
                    raise ProsecutorError(ProsecutorErrorReason.INVALID_CANDIDATE)
            elif (incumbent is None or incumbent.id != motion.appointment_id
                  or incumbent.subject_citizen_id != motion.subject_citizen_id):
                raise ProsecutorError(ProsecutorErrorReason.STALE_APPOINTMENT)

            added = await repository.add_prosecutor_approval(
                senator.guild_id, motion.id, senator.id, reason=reason, at=now,
            )
            if not added:
                return motion
            count = motion.approval_count + 1
            if count < _REQUIRED_APPROVALS:
                return replace(motion, approval_count=count)
            return await repository.apply_prosecutor_motion(senator.guild_id, motion.id, at=now)

    async def _require_motion(self, repository: ProsecutorRepository, motion_id: int) -> ProsecutorMotion:
        motion = await repository.get_prosecutor_motion(self._guild_id, motion_id)
        if motion is None:
            raise ProsecutorError(ProsecutorErrorReason.MOTION_NOT_FOUND)
        return motion

    async def _lock_for_actor(self, repository: ProsecutorRepository, user_id: int) -> Citizen:
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

    async def _get_eligible_citizen(self, repository: ProsecutorRepository, user_id: int) -> Citizen | None:
        citizen = await repository.get_profile(self._guild_id, user_id)
        if citizen is None or citizen.status is not CitizenStatus.VERIFIED:
            return None
        return citizen if await repository.is_allowed(self._guild_id, user_id) else None

    def _require_guild(self, guild_id: int | None) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)

    @staticmethod
    def _validate_motion_id(motion_id: int) -> None:
        if type(motion_id) is not int or not 1 <= motion_id <= MAX_AMOUNT:
            raise ProsecutorError(ProsecutorErrorReason.MOTION_NOT_FOUND)

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Prosecutor clock must include a time zone")
        return now.astimezone(UTC)
