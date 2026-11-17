"""File in-game court cases and schedule hearings in the configured guild."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.court_sanctions import CourtSentence
from civitas.domain.government import OfficeKind
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id
from .court import CourtNomination
from .law import ActKind, LawRevision
from .prosecutor import ProsecutorMotion

CASE_STATEMENT_MAX_LENGTH = 1800
ACT_DECISION_REASON_MAX_LENGTH = 1000
CASE_PAGE_SIZE = 10
CASE_MAX_PAGE = 1000


class CaseKind(StrEnum):
    ADMINISTRATIVE = "ADMINISTRATIVE"
    CRIMINAL = "CRIMINAL"
    CIVIL = "CIVIL"


class AdministrativeOutcome(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"


class MeritsOutcome(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    GUILTY = "GUILTY"
    ACQUITTED = "ACQUITTED"


class CourtFineStatus(StrEnum):
    PENDING = "pending"
    HELD = "held"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    SETTLED = "settled"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MeritsDecision:
    guild_id: int
    case_id: int
    kind: CaseKind
    revision_id: int | None
    outcome: MeritsOutcome
    reason: str
    judge_citizen_id: int
    decided_at: datetime
    sentence: CourtSentence | None = None


@dataclass(frozen=True, slots=True)
class AdministrativeDecision:
    guild_id: int
    case_id: int
    revision_id: int
    act_kind: ActKind
    outcome: AdministrativeOutcome
    reason: str
    judge_citizen_id: int
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class CourtCase:
    id: int
    guild_id: int
    kind: CaseKind
    filer_citizen_id: int
    filer_user_id: int
    respondent_citizen_id: int | None
    respondent_user_id: int | None
    for_state: bool
    against_state: bool
    law_revision_id: int | None
    occurred_at: datetime | None
    statement: str
    filed_at: datetime
    hearing_at: datetime | None = None
    scheduled_by_citizen_id: int | None = None
    act_kind: ActKind | None = None
    administrative_outcome: AdministrativeOutcome | None = None
    merits_outcome: MeritsOutcome | None = None


@dataclass(frozen=True, slots=True)
class CaseSummary:
    id: int
    kind: CaseKind
    law_revision_id: int | None
    filed_at: datetime
    hearing_at: datetime | None
    act_kind: ActKind | None = None
    administrative_outcome: AdministrativeOutcome | None = None
    merits_outcome: MeritsOutcome | None = None


class CourtCaseErrorReason(StrEnum):
    SUBSTITUTE_NOT_ALLOWED = "substitute_not_allowed"
    INVALID_REQUEST = "invalid_request"
    INVALID_STATEMENT = "invalid_statement"
    INVALID_TIME = "invalid_time"
    INVALID_PARTIES = "invalid_parties"
    INVALID_LAW = "invalid_law"
    INVALID_DECREE = "invalid_decree"
    NOT_FOUND = "not_found"
    PROSECUTOR_REQUIRED = "prosecutor_required"
    JUDGE_REQUIRED = "judge_required"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    INVALID_OUTCOME = "invalid_outcome"
    INVALID_SENTENCE = "invalid_sentence"
    INVALID_REASON = "invalid_reason"
    ADMINISTRATIVE_REQUIRED = "administrative_required"
    HEARING_REQUIRED = "hearing_required"
    ALREADY_DECIDED = "already_decided"


class CourtCaseError(Exception):
    def __init__(self, reason: CourtCaseErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class CourtCaseRepository(Protocol):
    async def is_recused(self, case: CourtCase, role: str, actor_id: int) -> bool: ...
    async def add_recusal(self, case: CourtCase, role: str, actor_id: int, now: datetime) -> None: ...
    async def has_active_office(self, guild_id: int, citizen_id: int, office: OfficeKind, at: datetime) -> bool: ...
    async def propose_substitute(self, case: CourtCase, actor_id: int, candidate_id: int, now: datetime, key: str, role: str) -> int: ...
    async def find_substitute_request(
        self, case: CourtCase, actor_id: int, candidate_id: int, key: str, role: str,
    ) -> int | None: ...
    async def get_substitute_proposal(
        self, guild_id: int, proposal_id: int, role: str,
    ) -> tuple[int, int, int, int | None, int | None] | None: ...
    async def sign_substitute(self, guild_id: int, proposal_id: int, senator_id: int, now: datetime, role: str) -> bool: ...
    async def get_substitute(self, guild_id: int, case_id: int, role: str = "JUDGE") -> int | None: ...
    async def get_fine_status(self, guild_id: int, case_id: int, respondent_id: int) -> CourtFineStatus | None: ...
    async def custody_role_assignments(self, guild_id: int, *, at: datetime) -> dict[int, bool]: ...
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...
    async def is_allowed(self, guild_id: int, user_id: int) -> bool: ...
    async def lock_election_cycle(self, guild_id: int) -> None: ...

    async def get_active_prosecutor(self, discord_guild_id: int) -> ProsecutorMotion | None: ...
    async def get_active_court_nomination(self, discord_guild_id: int, at: datetime) -> CourtNomination | None: ...
    async def get_law_revision(self, discord_guild_id: int, revision_id: int) -> LawRevision | None: ...
    async def get_effective_law(self, discord_guild_id: int, code: str, at: datetime) -> LawRevision | None: ...

    async def find_case(self, guild_id: int, request_key: str) -> CourtCase | None: ...
    async def add_case(self, case: CourtCase, request_key: str) -> CourtCase: ...
    async def get_case(self, discord_guild_id: int, case_id: int) -> CourtCase | None: ...
    async def list_cases(self, discord_guild_id: int, *, limit: int, offset: int) -> tuple[CourtCase, ...]: ...
    async def schedule_hearing(
        self, case: CourtCase, *, judge_citizen_id: int, hearing_at: datetime, scheduled_at: datetime, request_key: str,
    ) -> CourtCase: ...

    async def get_act_decision(self, discord_guild_id: int, case_id: int) -> AdministrativeDecision | None: ...
    async def find_act_decision(self, guild_id: int, request_key: str) -> AdministrativeDecision | None: ...
    async def add_act_decision(self, decision: AdministrativeDecision, request_key: str) -> AdministrativeDecision: ...
    async def get_merits_decision(self, discord_guild_id: int, case_id: int) -> MeritsDecision | None: ...
    async def find_merits_decision(self, guild_id: int, request_key: str) -> MeritsDecision | None: ...
    async def add_merits_decision(self, decision: MeritsDecision, request_key: str) -> MeritsDecision: ...
    async def list_returned_acts(
        self, discord_guild_id: int, act_kind: ActKind, *, limit: int, offset: int,
    ) -> tuple[AdministrativeDecision, ...]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _statement(value: str) -> str:
    if not isinstance(value, str):
        raise CourtCaseError(CourtCaseErrorReason.INVALID_STATEMENT)
    result = value.replace("\r\n", "\n").strip()
    if (not result or len(result) > CASE_STATEMENT_MAX_LENGTH
            or any(not char.isprintable() and char not in "\n\t" for char in result)):
        raise CourtCaseError(CourtCaseErrorReason.INVALID_STATEMENT)
    return result


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise CourtCaseError(CourtCaseErrorReason.INVALID_TIME)
    try:
        timestamp = datetime.fromisoformat(value.strip())
        if timestamp.utcoffset() is None:
            raise ValueError("Time zone required")
        return timestamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise CourtCaseError(CourtCaseErrorReason.INVALID_TIME) from None


def _check_request_key(value: str) -> None:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH or not value.isprintable()):
        raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)


def _check_number(value: int, reason: CourtCaseErrorReason) -> None:
    if type(value) is not int or not 1 <= value <= MAX_AMOUNT:
        raise CourtCaseError(reason)


def _summary(case: CourtCase) -> CaseSummary:
    return CaseSummary(
        case.id, case.kind, case.law_revision_id, case.filed_at, case.hearing_at, case.act_kind,
        case.administrative_outcome, case.merits_outcome,
    )


def _public_reason(value: str) -> str:
    if not isinstance(value, str):
        raise CourtCaseError(CourtCaseErrorReason.INVALID_REASON)
    result = value.strip()
    if not result or len(result) > ACT_DECISION_REASON_MAX_LENGTH or not result.isprintable():
        raise CourtCaseError(CourtCaseErrorReason.INVALID_REASON)
    return result


class CourtCaseService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[CourtCaseRepository]], *,
        guild_id: int, clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._clock = clock

    async def file_civil(
        self, guild_id: int | None, user_id: int, *, statement: str, respondent_user_id: int | None = None,
        for_state: bool = False, against_state: bool = False, idempotency_key: str,
    ) -> CourtCase:
        if (type(for_state) is not bool or type(against_state) is not bool or (for_state and against_state)
                or (against_state and respondent_user_id is not None)
                or (not against_state and not is_discord_id(respondent_user_id))):
            raise CourtCaseError(CourtCaseErrorReason.INVALID_PARTIES)
        return await self._file(
            guild_id, user_id, kind=CaseKind.CIVIL, statement=statement, idempotency_key=idempotency_key,
            respondent_user_id=respondent_user_id, for_state=for_state, against_state=against_state,
        )

    async def file_charge(
        self, guild_id: int | None, user_id: int, *, respondent_user_id: int, law_revision_id: int,
        occurred_at: str, statement: str, idempotency_key: str,
    ) -> CourtCase:
        _check_number(law_revision_id, CourtCaseErrorReason.INVALID_LAW)
        if not is_discord_id(respondent_user_id):
            raise CourtCaseError(CourtCaseErrorReason.INVALID_PARTIES)
        return await self._file(
            guild_id, user_id, kind=CaseKind.CRIMINAL, statement=statement, idempotency_key=idempotency_key,
            respondent_user_id=respondent_user_id, law_revision_id=law_revision_id, occurred_at=_timestamp(occurred_at),
            for_state=True, act_kind=ActKind.LAW,
        )

    async def file_challenge(
        self, guild_id: int | None, user_id: int, *, law_revision_id: int, statement: str, idempotency_key: str,
    ) -> CourtCase:
        _check_number(law_revision_id, CourtCaseErrorReason.INVALID_LAW)
        return await self._file(
            guild_id, user_id, kind=CaseKind.ADMINISTRATIVE, statement=statement, idempotency_key=idempotency_key,
            law_revision_id=law_revision_id, against_state=True, act_kind=ActKind.LAW,
        )

    async def file_decree_challenge(
        self, guild_id: int | None, user_id: int, *, decree_revision_id: int, statement: str, idempotency_key: str,
    ) -> CourtCase:
        _check_number(decree_revision_id, CourtCaseErrorReason.INVALID_DECREE)
        return await self._file(
            guild_id, user_id, kind=CaseKind.ADMINISTRATIVE, statement=statement, idempotency_key=idempotency_key,
            law_revision_id=decree_revision_id, against_state=True, act_kind=ActKind.DECREE,
        )

    async def _file(
        self, guild_id: int | None, user_id: int, *, kind: CaseKind, statement: str, idempotency_key: str,
        respondent_user_id: int | None = None, for_state: bool = False, against_state: bool = False,
        law_revision_id: int | None = None, occurred_at: datetime | None = None, act_kind: ActKind | None = None,
    ) -> CourtCase:
        self._require_guild(guild_id)
        statement = _statement(statement)
        _check_request_key(idempotency_key)
        if respondent_user_id == user_id:
            raise CourtCaseError(CourtCaseErrorReason.INVALID_PARTIES)

        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            is_prosecutor = False
            if kind is CaseKind.ADMINISTRATIVE or for_state:
                prosecutor = await repository.get_active_prosecutor(self._guild_id)
                is_prosecutor = prosecutor is not None and prosecutor.subject_citizen_id == actor.id
                if for_state and not is_prosecutor:
                    raise CourtCaseError(CourtCaseErrorReason.PROSECUTOR_REQUIRED)
            existing = await repository.find_case(actor.guild_id, idempotency_key)
            if existing is not None:
                if (existing.filer_citizen_id, existing.kind, existing.respondent_user_id, existing.against_state,
                    existing.law_revision_id, existing.occurred_at, existing.statement, existing.act_kind) != (
                    actor.id, kind, respondent_user_id, against_state, law_revision_id, occurred_at, statement, act_kind,
                ) or (kind is not CaseKind.ADMINISTRATIVE and existing.for_state != for_state):
                    raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
                if existing.for_state and not is_prosecutor:
                    raise CourtCaseError(CourtCaseErrorReason.PROSECUTOR_REQUIRED)
                return existing

            now = self._now()
            respondent = await repository.get_profile(self._guild_id, respondent_user_id) if respondent_user_id else None
            if respondent_user_id is not None and respondent is None:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_PARTIES)
            if law_revision_id is not None:
                await self._require_act(repository, law_revision_id, act_kind=act_kind, occurred_at=occurred_at, now=now)

            record = CourtCase(
                id=0, guild_id=actor.guild_id, kind=kind, filer_citizen_id=actor.id, filer_user_id=user_id,
                respondent_citizen_id=respondent.id if respondent is not None else None,
                respondent_user_id=respondent_user_id, for_state=is_prosecutor, against_state=against_state,
                law_revision_id=law_revision_id, occurred_at=occurred_at, statement=statement, filed_at=now,
                act_kind=act_kind,
            )
            return await repository.add_case(record, idempotency_key)

    async def case(self, guild_id: int | None, user_id: int, case_id: int) -> tuple[CourtCase | CaseSummary, bool]:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        async with self._unit_of_work() as repository:
            record = await self._require_case(repository, case_id)
            if user_id == record.respondent_user_id or (not record.for_state and user_id == record.filer_user_id):
                return record, True
            actor = await self._eligible_citizen(repository, user_id)
            if actor is not None:
                now = self._now()
                judge = await repository.get_active_court_nomination(self._guild_id, now)
                substitute_id = await repository.get_substitute(record.guild_id, record.id)
                if substitute_id == actor.id or (
                    substitute_id is None and judge is not None and judge.candidate_citizen_id == actor.id
                    and not await repository.is_recused(record, "JUDGE", actor.id)
                ):
                    return record, True
                if record.for_state or record.against_state:
                    if await self._state_representative(repository, record) == actor.id:
                        return record, True
            return _summary(record), False

    async def list_cases(self, guild_id: int | None, *, page: int = 1) -> tuple[CaseSummary, ...]:
        self._require_guild(guild_id)
        if type(page) is not int or not 1 <= page <= CASE_MAX_PAGE:
            raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
        async with self._unit_of_work() as repository:
            return tuple(map(_summary, await repository.list_cases(
                self._guild_id, limit=CASE_PAGE_SIZE, offset=(page - 1) * CASE_PAGE_SIZE,
            )))

    async def schedule_hearing(
        self, guild_id: int | None, user_id: int, case_id: int, *, hearing_at: str, idempotency_key: str,
    ) -> CourtCase:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        _check_request_key(idempotency_key)
        hearing = _timestamp(hearing_at)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            record = await self._case_for_judge(repository, actor, case_id, now)
            return await repository.schedule_hearing(
                record, judge_citizen_id=actor.id, hearing_at=hearing, scheduled_at=now, request_key=idempotency_key,
            )

    async def decide_challenge(
        self, guild_id: int | None, user_id: int, case_id: int, *, outcome: AdministrativeOutcome,
        reason: str, idempotency_key: str,
    ) -> AdministrativeDecision:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        _check_request_key(idempotency_key)
        if not isinstance(outcome, AdministrativeOutcome):
            raise CourtCaseError(CourtCaseErrorReason.INVALID_OUTCOME)
        reason = _public_reason(reason)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            case = await self._case_for_judge(repository, actor, case_id, now)
            existing = await repository.find_act_decision(actor.guild_id, idempotency_key)
            if existing is not None:
                if (existing.case_id, existing.judge_citizen_id, existing.outcome, existing.reason) != (
                    case_id, actor.id, outcome, reason,
                ):
                    raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
                return existing
            if await repository.find_merits_decision(actor.guild_id, idempotency_key) is not None:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)

            if case.kind is not CaseKind.ADMINISTRATIVE or case.law_revision_id is None or case.act_kind is None:
                raise CourtCaseError(CourtCaseErrorReason.ADMINISTRATIVE_REQUIRED)
            if case.administrative_outcome is not None:
                raise CourtCaseError(CourtCaseErrorReason.ALREADY_DECIDED)
            if case.hearing_at is None or case.hearing_at > now:
                raise CourtCaseError(CourtCaseErrorReason.HEARING_REQUIRED)
            return await repository.add_act_decision(AdministrativeDecision(
                guild_id=actor.guild_id, case_id=case_id, revision_id=case.law_revision_id, act_kind=case.act_kind,
                outcome=outcome, reason=reason, judge_citizen_id=actor.id, decided_at=now,
            ), idempotency_key)

    async def decide_merits(
        self, guild_id: int | None, user_id: int, case_id: int, *, outcome: MeritsOutcome,
        reason: str, idempotency_key: str, sentence: CourtSentence | None = None,
    ) -> MeritsDecision:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        _check_request_key(idempotency_key)
        reason = _public_reason(reason)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            case = await self._case_for_judge(repository, actor, case_id, now)
            allowed = {
                CaseKind.CIVIL: (MeritsOutcome.GRANTED, MeritsOutcome.DENIED),
                CaseKind.CRIMINAL: (MeritsOutcome.GUILTY, MeritsOutcome.ACQUITTED),
            }.get(case.kind, ())
            if not isinstance(outcome, MeritsOutcome) or outcome not in allowed:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_OUTCOME)
            if sentence is not None and (
                not isinstance(sentence, CourtSentence)
                or outcome is not MeritsOutcome.GUILTY
            ):
                raise CourtCaseError(CourtCaseErrorReason.INVALID_SENTENCE)
            existing = await repository.find_merits_decision(actor.guild_id, idempotency_key)
            if existing is not None:
                if (
                    existing.case_id, existing.judge_citizen_id, existing.outcome, existing.reason, existing.sentence,
                ) != (
                    case_id, actor.id, outcome, reason, sentence,
                ):
                    raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
                return existing
            if await repository.find_act_decision(actor.guild_id, idempotency_key) is not None:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
            if case.merits_outcome is not None:
                raise CourtCaseError(CourtCaseErrorReason.ALREADY_DECIDED)
            if case.hearing_at is None or case.hearing_at > now:
                raise CourtCaseError(CourtCaseErrorReason.HEARING_REQUIRED)
            return await repository.add_merits_decision(MeritsDecision(
                actor.guild_id, case_id, case.kind, case.law_revision_id, outcome, reason, actor.id, now, sentence,
            ), idempotency_key)

    async def custody_role_assignments(self) -> dict[int, bool]:
        async with self._unit_of_work() as repository:
            return await repository.custody_role_assignments(self._guild_id, at=self._now())

    async def fine_status(self, guild_id: int | None, user_id: int, case_id: int) -> CourtFineStatus | None:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        async with self._unit_of_work() as repository:
            actor = await self._eligible_citizen(repository, user_id)
            if actor is None:
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
            return await repository.get_fine_status(actor.guild_id, case_id, actor.id)

    async def decision(self, guild_id: int | None, case_id: int) -> AdministrativeDecision | MeritsDecision | None:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        async with self._unit_of_work() as repository:
            case = await self._require_case(repository, case_id)
            if case.kind is CaseKind.ADMINISTRATIVE:
                return await repository.get_act_decision(self._guild_id, case_id)
            return await repository.get_merits_decision(self._guild_id, case_id)

    async def list_returns(
        self, guild_id: int | None, act_kind: ActKind, *, page: int = 1,
    ) -> tuple[AdministrativeDecision, ...]:
        self._require_guild(guild_id)
        if not isinstance(act_kind, ActKind) or type(page) is not int or not 1 <= page <= CASE_MAX_PAGE:
            raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST)
        async with self._unit_of_work() as repository:
            return await repository.list_returned_acts(
                self._guild_id, act_kind, limit=CASE_PAGE_SIZE, offset=(page - 1) * CASE_PAGE_SIZE,
            )

    async def recuse(self, guild_id: int | None, user_id: int, case_id: int, *, role: str) -> None:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            case = await self._require_case(repository, case_id)
            if role == "JUDGE":
                judge = await repository.get_active_court_nomination(self._guild_id, now)
                allowed = (
                    judge is not None and judge.candidate_citizen_id == actor.id
                    and case.administrative_outcome is None and case.merits_outcome is None
                )
            elif role == "PROSECUTOR" and (case.for_state or case.against_state):
                prosecutor = await repository.get_active_prosecutor(self._guild_id)
                allowed = prosecutor is not None and prosecutor.subject_citizen_id == actor.id
            else:
                allowed = False
            if not allowed:
                raise CourtCaseError(CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED)
            await repository.add_recusal(case, role, actor.id, now)

    async def propose_case_judge(
        self, guild_id: int | None, user_id: int, case_id: int, candidate_user_id: int, *, idempotency_key: str,
    ) -> int:
        return await self.propose_case_substitute(
            guild_id, user_id, case_id, candidate_user_id, idempotency_key=idempotency_key, role="JUDGE",
        )

    async def propose_case_substitute(
        self, guild_id: int | None, user_id: int, case_id: int, candidate_user_id: int, *,
        idempotency_key: str, role: str,
    ) -> int:
        self._require_guild(guild_id)
        _check_number(case_id, CourtCaseErrorReason.NOT_FOUND)
        _check_request_key(idempotency_key)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            case = await self._require_case(repository, case_id)
            candidate = await self._eligible_citizen(repository, candidate_user_id) if is_discord_id(candidate_user_id) else None
            if (
                not await repository.has_active_office(actor.guild_id, actor.id, OfficeKind.PRESIDENT, now)
                or candidate is None or candidate.id == actor.id
            ):
                raise CourtCaseError(CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED)
            prior = await repository.find_substitute_request(case, actor.id, candidate.id, idempotency_key, role)
            if prior is not None:
                return prior
            await self._check_substitute_candidate(repository, case, candidate.id, now, role)
            return await repository.propose_substitute(case, actor.id, candidate.id, now, idempotency_key, role)

    async def approve_case_judge(self, guild_id: int | None, user_id: int, proposal_id: int) -> bool:
        return await self.approve_case_substitute(guild_id, user_id, proposal_id, role="JUDGE")

    async def substitute_details(
        self, guild_id: int | None, user_id: int, proposal_id: int, *, role: str,
    ) -> tuple[int, int, int]:
        self._require_guild(guild_id)
        _check_number(proposal_id, CourtCaseErrorReason.NOT_FOUND)
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        async with self._unit_of_work() as repository:
            actor = await self._eligible_citizen(repository, user_id)
            if actor is None:
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
            proposal = await repository.get_substitute_proposal(actor.guild_id, proposal_id, role)
            if proposal is None:
                raise CourtCaseError(CourtCaseErrorReason.NOT_FOUND)
            case_id, proposer_id, candidate_user_id, first_signer, second_signer = proposal
            if actor.id != proposer_id and not await repository.has_active_office(
                actor.guild_id, actor.id, OfficeKind.PARLIAMENT, self._now(),
            ):
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
            return case_id, candidate_user_id, sum(signer is not None for signer in (first_signer, second_signer))

    async def approve_case_substitute(
        self, guild_id: int | None, user_id: int, proposal_id: int, *, role: str,
    ) -> bool:
        self._require_guild(guild_id)
        _check_number(proposal_id, CourtCaseErrorReason.NOT_FOUND)
        async with self._unit_of_work() as repository:
            actor = await self._lock_for_actor(repository, user_id)
            now = self._now()
            proposal = await repository.get_substitute_proposal(actor.guild_id, proposal_id, role)
            if proposal is None:
                raise CourtCaseError(CourtCaseErrorReason.NOT_FOUND)
            case_id, proposer_id, candidate_user_id, first_signer, second_signer = proposal
            if actor.id in (first_signer, second_signer):
                return second_signer is not None
            candidate = await self._eligible_citizen(repository, candidate_user_id)
            if (
                candidate is None or actor.id in (proposer_id, candidate.id)
                or not await repository.has_active_office(actor.guild_id, actor.id, OfficeKind.PARLIAMENT, now)
                or not await repository.has_active_office(actor.guild_id, proposer_id, OfficeKind.PRESIDENT, now)
            ):
                raise CourtCaseError(CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED)
            case = await self._require_case(repository, case_id)
            await self._check_substitute_candidate(repository, case, candidate.id, now, role)
            return await repository.sign_substitute(actor.guild_id, proposal_id, actor.id, now, role)

    async def _check_substitute_candidate(
        self, repository: CourtCaseRepository, case: CourtCase, candidate_id: int, now: datetime, role: str,
    ) -> None:
        if role == "JUDGE":
            await self._check_case_judge_candidate(repository, case, candidate_id, now)
            return
        prosecutor = await repository.get_active_prosecutor(self._guild_id)
        if (
            role != "PROSECUTOR" or not (case.for_state or case.against_state) or prosecutor is None
            or (
                prosecutor.subject_citizen_id != case.respondent_citizen_id
                and not await repository.is_recused(case, role, prosecutor.subject_citizen_id)
            )
            or candidate_id in (case.filer_citizen_id, case.respondent_citizen_id)
            or await repository.is_recused(case, role, candidate_id)
            or await repository.get_substitute(case.guild_id, case.id, role) is not None
        ):
            raise CourtCaseError(CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED)
        judge = await repository.get_active_court_nomination(self._guild_id, now)
        if (
            (judge is not None and judge.candidate_citizen_id == candidate_id)
            or await repository.get_substitute(case.guild_id, case.id) == candidate_id
        ):
            raise CourtCaseError(CourtCaseErrorReason.CONFLICT_OF_INTEREST)

    async def _check_case_judge_candidate(
        self, repository: CourtCaseRepository, case: CourtCase, candidate_id: int, now: datetime,
    ) -> None:
        incumbent = await repository.get_active_court_nomination(self._guild_id, now)
        if (
            incumbent is None
            or case.administrative_outcome is not None or case.merits_outcome is not None
            or await repository.get_substitute(case.guild_id, case.id) is not None
            or not await self._has_judicial_conflict(repository, case, incumbent.candidate_citizen_id)
        ):
            raise CourtCaseError(CourtCaseErrorReason.SUBSTITUTE_NOT_ALLOWED)
        if await self._has_judicial_conflict(repository, case, candidate_id):
            raise CourtCaseError(CourtCaseErrorReason.CONFLICT_OF_INTEREST)

    async def _case_for_judge(
        self, repository: CourtCaseRepository, actor: Citizen, case_id: int, now: datetime,
    ) -> CourtCase:
        record = await self._require_case(repository, case_id)
        substitute_id = await repository.get_substitute(record.guild_id, case_id)
        judge = await repository.get_active_court_nomination(self._guild_id, now)
        authorized_id = substitute_id if substitute_id is not None else judge.candidate_citizen_id if judge else None
        if actor.id != authorized_id:
            raise CourtCaseError(CourtCaseErrorReason.JUDGE_REQUIRED)
        if await self._has_judicial_conflict(repository, record, actor.id):
            raise CourtCaseError(CourtCaseErrorReason.CONFLICT_OF_INTEREST)
        return record

    async def _has_judicial_conflict(
        self, repository: CourtCaseRepository, record: CourtCase, citizen_id: int,
    ) -> bool:
        if citizen_id in (record.filer_citizen_id, record.respondent_citizen_id) or await repository.is_recused(
            record, "JUDGE", citizen_id,
        ):
            return True
        if record.kind is CaseKind.ADMINISTRATIVE and record.law_revision_id is not None:
            revision = await repository.get_law_revision(self._guild_id, record.law_revision_id)
            if revision is not None and citizen_id in (revision.proposer_citizen_id, revision.confirmed_by_citizen_id):
                return True
        if record.for_state or record.against_state:
            if await self._state_representative(repository, record) == citizen_id:
                return True
        return False

    async def _state_representative(self, repository: CourtCaseRepository, case: CourtCase) -> int | None:
        substitute = await repository.get_substitute(case.guild_id, case.id, "PROSECUTOR")
        if substitute is not None:
            return substitute
        prosecutor = await repository.get_active_prosecutor(self._guild_id)
        if (
            prosecutor is not None and prosecutor.subject_citizen_id != case.respondent_citizen_id
            and not await repository.is_recused(case, "PROSECUTOR", prosecutor.subject_citizen_id)
        ):
            return prosecutor.subject_citizen_id
        return None

    async def _lock_for_actor(self, repository: CourtCaseRepository, user_id: int) -> Citizen:
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        actor = await repository.get_profile(self._guild_id, user_id)
        if actor is None:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        await repository.lock_election_cycle(actor.guild_id)
        actor = await self._eligible_citizen(repository, user_id)
        if actor is None:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        return actor

    async def _eligible_citizen(self, repository: CourtCaseRepository, user_id: int) -> Citizen | None:
        citizen = await repository.get_profile(self._guild_id, user_id)
        if citizen is None or citizen.status is not CitizenStatus.VERIFIED:
            return None
        return citizen if await repository.is_allowed(self._guild_id, user_id) else None

    async def _require_act(
        self, repository: CourtCaseRepository, revision_id: int, *, act_kind: ActKind | None,
        occurred_at: datetime | None, now: datetime,
    ) -> None:
        revision = await repository.get_law_revision(self._guild_id, revision_id)
        if revision is None or revision.confirmed_at is None or revision.act_kind is not act_kind:
            reason = CourtCaseErrorReason.INVALID_DECREE if act_kind is ActKind.DECREE else CourtCaseErrorReason.INVALID_LAW
            raise CourtCaseError(reason)
        if occurred_at is not None:
            if occurred_at > now:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_TIME)
            effective = await repository.get_effective_law(self._guild_id, revision.code, occurred_at)
            if effective is None or effective.id != revision_id:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_LAW)

    async def _require_case(self, repository: CourtCaseRepository, case_id: int) -> CourtCase:
        record = await repository.get_case(self._guild_id, case_id)
        if record is None:
            raise CourtCaseError(CourtCaseErrorReason.NOT_FOUND)
        return record

    def _require_guild(self, guild_id: int | None) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Court clock must include a time zone")
        return now.astimezone(UTC)
