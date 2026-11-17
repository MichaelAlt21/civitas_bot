"""File and independently certify public administrative appeals."""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT, LedgerError, LedgerErrorReason

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id
from .court_cases import AdministrativeDecision, CaseKind, CourtCase, MeritsDecision
from .government import GovernmentError, poll_quorum, public_poll_source
from .law import LawRevision
from .prosecutor import ProsecutorMotion

_FILING_PERIOD = timedelta(hours=24)
_VOTING_PERIOD = timedelta(hours=48)
_EXTENSION_PERIOD = timedelta(hours=24)


class AppealOutcome(StrEnum):
    EXTENDED = "EXTENDED"
    OVERTURNED = "OVERTURNED"
    UPHELD = "UPHELD"
    NO_QUORUM = "NO_QUORUM"


@dataclass(frozen=True, slots=True)
class CourtAppeal:
    guild_id: int
    case_id: int
    appellant_citizen_id: int
    filed_at: datetime
    opened_by_citizen_id: int | None = None
    opened_at: datetime | None = None
    source_url: str | None = None
    eligible_user_ids: tuple[int, ...] = ()
    eligible_count: int = 0
    quorum: int | None = None
    closes_at: datetime | None = None
    round_number: int = 1
    outcome: AppealOutcome | None = None
    resolved_at: datetime | None = None
    participants: int | None = None
    votes_to_cancel: int | None = None


@dataclass(frozen=True, slots=True)
class AppealTally:
    guild_id: int
    case_id: int
    round_number: int
    participants: int
    votes_to_cancel: int
    recorded_by_citizen_id: int
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class AppealConfirmation:
    guild_id: int
    case_id: int
    round_number: int
    confirmed_by_citizen_id: int
    confirmed_at: datetime
    outcome: AppealOutcome
    extended_until: datetime | None = None


class CourtAppealErrorReason(StrEnum):
    NOT_FOUND = "not_found"
    PARTY_REQUIRED = "party_required"
    DEADLINE_PASSED = "deadline_passed"
    COMMISSIONER_REQUIRED = "commissioner_required"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    INVALID_SOURCE = "invalid_source"
    SOURCE_IN_USE = "source_in_use"
    INSUFFICIENT_COMMISSION = "insufficient_commission"
    INVALID_ROUND = "invalid_round"
    OPEN_REQUIRED = "open_required"
    VOTING_IN_PROGRESS = "voting_in_progress"
    INVALID_TALLY = "invalid_tally"
    TALLY_REQUIRED = "tally_required"
    INDEPENDENT_CONFIRMATION = "independent_confirmation"
    ALREADY_FILED = "already_filed"
    ALREADY_OPEN = "already_open"
    ALREADY_RECORDED = "already_recorded"
    ALREADY_CONFIRMED = "already_confirmed"
    ALREADY_RESOLVED = "already_resolved"


class CourtAppealError(Exception):
    def __init__(self, reason: CourtAppealErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class CourtAppealRepository(Protocol):
    async def is_recused(self, case: CourtCase, role: str, actor_id: int) -> bool: ...
    async def hearing_judge_ids(self, guild_id: int, case_id: int) -> set[int]: ...
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...
    async def is_allowed(self, discord_guild_id: int, user_id: int) -> bool: ...
    async def lock_election_cycle(self, guild_id: int) -> None: ...
    async def list_verified_admitted_citizens(self, guild_id: int) -> tuple[Citizen, ...]: ...
    async def get_active_prosecutor(self, discord_guild_id: int) -> ProsecutorMotion | None: ...
    async def get_substitute(self, guild_id: int, case_id: int, role: str) -> int | None: ...
    async def get_case(self, discord_guild_id: int, case_id: int) -> CourtCase | None: ...
    async def get_act_decision(self, discord_guild_id: int, case_id: int) -> AdministrativeDecision | None: ...
    async def get_merits_decision(self, discord_guild_id: int, case_id: int) -> MeritsDecision | None: ...
    async def get_law_revision(self, discord_guild_id: int, revision_id: int) -> LawRevision | None: ...

    async def get_court_appeal(self, discord_guild_id: int, case_id: int) -> CourtAppeal | None: ...
    async def add_court_appeal(self, appeal: CourtAppeal) -> CourtAppeal: ...
    async def open_court_appeal(self, appeal: CourtAppeal) -> CourtAppeal: ...
    async def appeal_source_in_use(self, guild_id: int, source_url: str) -> bool: ...
    async def get_appeal_tally(self, guild_id: int, case_id: int, round_number: int) -> AppealTally | None: ...
    async def add_appeal_tally(self, tally: AppealTally) -> AppealTally: ...
    async def get_appeal_confirmation(
        self, guild_id: int, case_id: int, round_number: int,
    ) -> AppealConfirmation | None: ...
    async def add_appeal_confirmation(self, confirmation: AppealConfirmation) -> CourtAppeal: ...
    async def reconcile_court_fines(
        self, discord_guild_id: int, *, now: datetime, filing_period: timedelta,
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _require_round(round_number: int) -> None:
    if type(round_number) is not int or round_number not in (1, 2):
        raise CourtAppealError(CourtAppealErrorReason.INVALID_ROUND)


def _require_open(appeal: CourtAppeal) -> datetime:
    if appeal.closes_at is None:
        raise CourtAppealError(CourtAppealErrorReason.OPEN_REQUIRED)
    return appeal.closes_at


def _require_closed_round(appeal: CourtAppeal, round_number: int, now: datetime) -> None:
    closes_at = _require_open(appeal)
    if appeal.outcome is not None:
        raise CourtAppealError(CourtAppealErrorReason.ALREADY_RESOLVED)
    if round_number != appeal.round_number:
        raise CourtAppealError(CourtAppealErrorReason.INVALID_ROUND)
    if now < closes_at:
        raise CourtAppealError(CourtAppealErrorReason.VOTING_IN_PROGRESS)


class CourtAppealService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[CourtAppealRepository]], *,
        guild_id: int, commissioner_user_ids: Iterable[int], quorum_bp: int, quorum_min: int,
        poll_hosts: Iterable[str], clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        commissioners = frozenset(commissioner_user_ids)
        if not is_discord_id(guild_id) or any(not is_discord_id(user_id) for user_id in commissioners):
            raise ValueError("Appeal guild and commissioner IDs must be Discord snowflakes")
        if type(quorum_bp) is not int or type(quorum_min) is not int or not 1 <= quorum_bp <= 10_000 or quorum_min < 1:
            raise ValueError("Invalid appeal quorum")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._commissioners = commissioners
        self._quorum_bp = quorum_bp
        self._quorum_min = quorum_min
        self._poll_hosts = frozenset(host.lower() for host in poll_hosts)
        self._clock = clock

    async def reconcile_fines(self) -> None:
        try:
            async with self._unit_of_work() as repository:
                await repository.reconcile_court_fines(self._guild_id, now=self._now(), filing_period=_FILING_PERIOD)
        except LedgerError as error:
            if error.reason is not LedgerErrorReason.ECONOMY_READ_ONLY:
                raise

    async def file(self, guild_id: int | None, user_id: int, case_id: int) -> CourtAppeal:
        self._require_scope(guild_id, case_id)
        async with self._unit_of_work() as repository:
            actor = await self._write_actor(repository, user_id)
            case, decision = await self._case_and_decision(repository, case_id)
            state_representative = await self._state_representative(repository, case)
            personal_filer = not case.for_state and actor.id == case.filer_citizen_id
            if not personal_filer and actor.id not in (case.respondent_citizen_id, state_representative):
                raise CourtAppealError(CourtAppealErrorReason.PARTY_REQUIRED)
            existing = await repository.get_court_appeal(self._guild_id, case_id)
            if existing is not None:
                if existing.appellant_citizen_id != actor.id:
                    raise CourtAppealError(CourtAppealErrorReason.ALREADY_FILED)
                return existing
            now = self._now()
            if now < decision.decided_at or now > decision.decided_at + _FILING_PERIOD:
                raise CourtAppealError(CourtAppealErrorReason.DEADLINE_PASSED)
            return await repository.add_court_appeal(CourtAppeal(actor.guild_id, case_id, actor.id, now))

    async def open(self, guild_id: int | None, user_id: int, case_id: int, *, source_url: str) -> CourtAppeal:
        self._require_scope(guild_id, case_id)
        try:
            source = public_poll_source(source_url, self._poll_hosts)
        except GovernmentError:
            raise CourtAppealError(CourtAppealErrorReason.INVALID_SOURCE) from None
        async with self._unit_of_work() as repository:
            actor, appeal, excluded = await self._commission_case(repository, user_id, case_id)
            if appeal.source_url is not None:
                if appeal.source_url != source:
                    raise CourtAppealError(CourtAppealErrorReason.ALREADY_OPEN)
                return appeal
            electorate = await repository.list_verified_admitted_citizens(actor.guild_id)
            eligible_user_ids = tuple(sorted(citizen.discord_user_id for citizen in electorate))
            if sum(citizen.discord_user_id in self._commissioners and citizen.id not in excluded
                   for citizen in electorate) < 2:
                raise CourtAppealError(CourtAppealErrorReason.INSUFFICIENT_COMMISSION)
            if await repository.appeal_source_in_use(actor.guild_id, source):
                raise CourtAppealError(CourtAppealErrorReason.SOURCE_IN_USE)
            now = self._now()
            return await repository.open_court_appeal(replace(
                appeal, opened_by_citizen_id=actor.id, opened_at=now, source_url=source,
                eligible_user_ids=eligible_user_ids, eligible_count=len(eligible_user_ids),
                quorum=poll_quorum(len(eligible_user_ids), self._quorum_bp, self._quorum_min),
                closes_at=now + _VOTING_PERIOD,
            ))

    async def voters(self, guild_id: int | None, user_id: int, case_id: int) -> tuple[int, ...]:
        self._require_scope(guild_id, case_id)
        async with self._unit_of_work() as repository:
            _, appeal, _ = await self._commission_case(repository, user_id, case_id, write=False)
            _require_open(appeal)
            return appeal.eligible_user_ids

    async def record(
        self, guild_id: int | None, user_id: int, case_id: int, *,
        round_number: int, participants: int, votes_to_cancel: int,
    ) -> AppealTally:
        self._require_scope(guild_id, case_id)
        _require_round(round_number)
        async with self._unit_of_work() as repository:
            actor, appeal, _ = await self._commission_case(repository, user_id, case_id)
            _require_open(appeal)
            if (type(participants) is not int or type(votes_to_cancel) is not int
                    or not 0 <= votes_to_cancel <= participants <= appeal.eligible_count):
                raise CourtAppealError(CourtAppealErrorReason.INVALID_TALLY)
            existing = await repository.get_appeal_tally(actor.guild_id, case_id, round_number)
            if existing is not None:
                if (existing.recorded_by_citizen_id, existing.participants, existing.votes_to_cancel) != (
                    actor.id, participants, votes_to_cancel,
                ):
                    raise CourtAppealError(CourtAppealErrorReason.ALREADY_RECORDED)
                return existing
            now = self._now()
            _require_closed_round(appeal, round_number, now)
            return await repository.add_appeal_tally(
                AppealTally(actor.guild_id, case_id, round_number, participants, votes_to_cancel, actor.id, now),
            )

    async def confirm(self, guild_id: int | None, user_id: int, case_id: int, *, round_number: int) -> CourtAppeal:
        self._require_scope(guild_id, case_id)
        _require_round(round_number)
        async with self._unit_of_work() as repository:
            actor, appeal, _ = await self._commission_case(repository, user_id, case_id)
            tally = await repository.get_appeal_tally(actor.guild_id, case_id, round_number)
            if tally is not None and tally.recorded_by_citizen_id == actor.id:
                raise CourtAppealError(CourtAppealErrorReason.INDEPENDENT_CONFIRMATION)
            prior = await repository.get_appeal_confirmation(actor.guild_id, case_id, round_number)
            if prior is not None:
                if prior.confirmed_by_citizen_id != actor.id:
                    raise CourtAppealError(CourtAppealErrorReason.ALREADY_CONFIRMED)
                return appeal
            now = self._now()
            _require_closed_round(appeal, round_number, now)
            if tally is None:
                raise CourtAppealError(CourtAppealErrorReason.TALLY_REQUIRED)
            assert appeal.quorum is not None
            extended_until = None
            if tally.participants < appeal.quorum:
                outcome = AppealOutcome.EXTENDED if round_number == 1 else AppealOutcome.NO_QUORUM
                if outcome is AppealOutcome.EXTENDED:
                    extended_until = now + _EXTENSION_PERIOD
            else:
                outcome = AppealOutcome.OVERTURNED if tally.votes_to_cancel * 2 > tally.participants else AppealOutcome.UPHELD
            return await repository.add_appeal_confirmation(AppealConfirmation(
                actor.guild_id, case_id, round_number, actor.id, now, outcome, extended_until,
            ))

    async def show(self, guild_id: int | None, case_id: int) -> CourtAppeal:
        self._require_scope(guild_id, case_id)
        async with self._unit_of_work() as repository:
            return replace(await self._appeal(repository, case_id), eligible_user_ids=())

    async def _commission_case(
        self, repository: CourtAppealRepository, user_id: int, case_id: int, *, write: bool = True,
    ) -> tuple[Citizen, CourtAppeal, set[int]]:
        if not is_discord_id(user_id) or user_id not in self._commissioners:
            raise CourtAppealError(CourtAppealErrorReason.COMMISSIONER_REQUIRED)
        actor = await self._write_actor(repository, user_id) if write else await self._eligible_actor(repository, user_id)
        case, decision = await self._case_and_decision(repository, case_id)
        appeal = await self._appeal(repository, case_id)
        excluded = {case.filer_citizen_id, decision.judge_citizen_id, appeal.appellant_citizen_id}
        revision = (
            await repository.get_law_revision(self._guild_id, decision.revision_id)
            if isinstance(decision, AdministrativeDecision) else None
        )
        if revision is not None:
            excluded.add(revision.proposer_citizen_id)
            if revision.confirmed_by_citizen_id is not None:
                excluded.add(revision.confirmed_by_citizen_id)
        excluded.update(await repository.hearing_judge_ids(case.guild_id, case.id))
        state_representative = await self._state_representative(repository, case)
        if state_representative is not None:
            excluded.add(state_representative)
        if case.respondent_citizen_id is not None:
            excluded.add(case.respondent_citizen_id)
        if actor.id in excluded:
            raise CourtAppealError(CourtAppealErrorReason.CONFLICT_OF_INTEREST)
        return actor, appeal, excluded

    async def _case_and_decision(
        self, repository: CourtAppealRepository, case_id: int,
    ) -> tuple[CourtCase, AdministrativeDecision | MeritsDecision]:
        case = await repository.get_case(self._guild_id, case_id)
        if case is None:
            raise CourtAppealError(CourtAppealErrorReason.NOT_FOUND)
        if case.kind is CaseKind.ADMINISTRATIVE:
            decision = await repository.get_act_decision(self._guild_id, case_id)
        else:
            decision = await repository.get_merits_decision(self._guild_id, case_id)
        if decision is None:
            raise CourtAppealError(CourtAppealErrorReason.NOT_FOUND)
        return case, decision

    async def _appeal(self, repository: CourtAppealRepository, case_id: int) -> CourtAppeal:
        appeal = await repository.get_court_appeal(self._guild_id, case_id)
        if appeal is None:
            raise CourtAppealError(CourtAppealErrorReason.NOT_FOUND)
        return appeal

    async def _state_representative(self, repository: CourtAppealRepository, case: CourtCase) -> int | None:
        if not (case.for_state or case.against_state):
            return None
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

    async def _write_actor(self, repository: CourtAppealRepository, user_id: int) -> Citizen:
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        actor = await repository.get_profile(self._guild_id, user_id)
        if actor is None:
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        await repository.lock_election_cycle(actor.guild_id)
        return await self._eligible_actor(repository, user_id)

    async def _eligible_actor(self, repository: CourtAppealRepository, user_id: int) -> Citizen:
        actor = await repository.get_profile(self._guild_id, user_id)
        if (actor is None or actor.status is not CitizenStatus.VERIFIED
                or not await repository.is_allowed(self._guild_id, user_id)):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
        return actor

    def _require_scope(self, guild_id: int | None, case_id: int) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
        if type(case_id) is not int or not 1 <= case_id <= MAX_AMOUNT:
            raise CourtAppealError(CourtAppealErrorReason.NOT_FOUND)

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Appeal clock must include a time zone")
        return now.astimezone(UTC)
