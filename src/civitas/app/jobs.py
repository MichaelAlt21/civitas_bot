"""NPC job eligibility, review, and atomic budgeted payouts."""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id
from civitas.app.wallet import WalletRepository
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.jobs import BudgetCaps, BudgetPeriod, JobCategory, JobClaim, JobClaimStatus, JobDetail, JobTask
from civitas.domain.ledger import MAX_AMOUNT, AccountKind, LedgerError, LedgerErrorReason, TransactionKind

_LIST_LIMIT = 10
_ONE_MINUTE = timedelta(minutes=1)


class JobErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    STAFF_REQUIRED = "staff_required"
    TASK_NOT_FOUND = "task_not_found"
    TASK_EXPIRED = "task_expired"
    TASK_ALREADY_CLAIMED = "task_already_claimed"
    CLAIM_NOT_FOUND = "claim_not_found"
    CLAIM_NOT_OWNED = "claim_not_owned"
    INVALID_STATUS = "invalid_status"
    SELF_APPROVAL = "self_approval"
    CLAIMANT_NOT_VERIFIED = "claimant_not_verified"
    COOLDOWN_ACTIVE = "cooldown_active"
    INVALID_REWARD = "invalid_reward"
    INVALID_TEXT = "invalid_text"
    INVALID_DEADLINE = "invalid_deadline"
    INVALID_CATEGORY = "invalid_category"
    OVERALL_LIMIT = "overall_limit"
    NPC_LIMIT = "npc_limit"
    PERSONAL_LIMIT = "personal_limit"
    BUDGET_PERIOD_UNAVAILABLE = "budget_period_unavailable"
    TREASURY_INSUFFICIENT_FUNDS = "treasury_insufficient_funds"


class JobError(Exception):
    def __init__(self, reason: JobErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobsRules:
    overall_weekly_limit: int
    npc_weekly_limit: int
    personal_weekly_limit: int
    reward_min: int
    reward_max: int
    budget_timezone: tzinfo = UTC
    claim_cooldown_minutes: int = 0

    def __post_init__(self) -> None:
        limits = (self.overall_weekly_limit, self.npc_weekly_limit, self.personal_weekly_limit)
        if any(type(limit) is not int or not 0 <= limit <= MAX_AMOUNT for limit in limits):
            raise ValueError("Weekly caps must be non-negative BIGINT integers")
        if self.npc_weekly_limit > self.overall_weekly_limit:
            raise ValueError("NPC cap cannot exceed the overall cap")
        if (
            type(self.reward_min) is not int
            or type(self.reward_max) is not int
            or not 1 <= self.reward_min <= self.reward_max <= MAX_AMOUNT
        ):
            raise ValueError("Reward bounds must be positive BIGINT integers in ascending order")
        if type(self.claim_cooldown_minutes) is not int or self.claim_cooldown_minutes < 0:
            raise ValueError("Claim cooldown must be a non-negative number of minutes")
        if not isinstance(self.budget_timezone, tzinfo):
            raise ValueError("Budget timezone must be a tzinfo instance")

    @property
    def caps(self) -> BudgetCaps:
        return BudgetCaps(self.overall_weekly_limit, self.npc_weekly_limit, self.personal_weekly_limit)


class JobsRepository(WalletRepository, Protocol):
    async def list_available_tasks(
        self, guild_id: int, now: datetime, limit: int
    ) -> tuple[JobTask, ...]: ...

    async def list_submitted_task_ids(self, guild_id: int, limit: int) -> tuple[int, ...]: ...

    async def get_task(self, guild_id: int, task_id: int, *, lock: bool = False) -> JobTask | None: ...

    async def get_claim(self, task_id: int, *, lock: bool = False) -> JobClaim | None: ...

    async def create_task(
        self,
        *,
        guild_id: int,
        category: JobCategory,
        title: str,
        requirements: str,
        reward: int,
        expires_at: datetime,
        created_at: datetime,
        published_by_citizen_id: int,
    ) -> JobTask: ...

    async def create_claim(
        self,
        *,
        guild_id: int,
        task_id: int,
        citizen_id: int,
        discord_user_id: int,
        claimed_at: datetime,
    ) -> JobClaim: ...

    async def update_claim(self, claim: JobClaim) -> None: ...

    async def get_latest_claim_at(self, guild_id: int, citizen_id: int) -> datetime | None: ...

    async def get_or_create_budget_period(
        self,
        guild_id: int,
        start_at: datetime,
        end_at: datetime,
        at: datetime,
        caps: BudgetCaps,
        *,
        week_start_date: date,
    ) -> BudgetPeriod: ...

    async def get_citizen_paid(self, period_id: int, citizen_id: int) -> int: ...

    async def record_budget_spend(self, period: BudgetPeriod, amount: int) -> None: ...


def budget_week(at: datetime, timezone: tzinfo) -> tuple[date, datetime, datetime]:
    local_date = at.astimezone(timezone).date()
    monday = local_date - timedelta(days=local_date.weekday())
    next_monday = monday + timedelta(days=7)
    return (
        monday,
        datetime.combine(monday, time.min, tzinfo=timezone).astimezone(UTC),
        datetime.combine(next_monday, time.min, tzinfo=timezone).astimezone(UTC),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: str, max_length: int) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise JobError(JobErrorReason.INVALID_TEXT)
    normalized = value.strip()
    if not 1 <= len(normalized) <= max_length:
        raise JobError(JobErrorReason.INVALID_TEXT)
    return normalized


class JobService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[JobsRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        staff_user_ids: Iterable[int],
        rules: JobsRules,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        staff_ids = tuple(staff_user_ids)
        if not all(is_discord_id(user_id) for user_id in staff_ids):
            raise ValueError("Staff IDs must be Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._staff_user_ids = frozenset(staff_ids)
        self._rules = rules
        self._clock = clock

    async def list_tasks(self, guild_id: int | None, user_id: int) -> tuple[JobTask, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            await self._verified_citizen(repository, user_id)
            return await repository.list_available_tasks(self._guild_id, self._clock(), _LIST_LIMIT)

    async def get_task_details(self, guild_id: int | None, user_id: int, task_id: int) -> JobDetail:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._verified_citizen(repository, user_id)
            task = await self._task(repository, task_id)
            claim = await repository.get_claim(task.id)
            if claim is not None and claim.citizen_id != citizen.id and user_id not in self._staff_user_ids:
                claim = None
            return JobDetail(task, claim)

    async def list_submitted_task_ids(self, guild_id: int | None, user_id: int) -> tuple[int, ...]:
        self._authorize_staff(guild_id, user_id)
        async with self._unit_of_work() as repository:
            await self._verified_citizen(repository, user_id)
            return await repository.list_submitted_task_ids(self._guild_id, _LIST_LIMIT)

    async def get_review(self, guild_id: int | None, user_id: int, task_id: int) -> JobDetail:
        self._authorize_staff(guild_id, user_id)
        async with self._unit_of_work() as repository:
            await self._verified_citizen(repository, user_id)
            task = await self._task(repository, task_id)
            return JobDetail(task, await self._claim(repository, task.id))

    async def publish(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        category: JobCategory,
        title: str,
        requirements: str,
        reward: int,
        expires_at: datetime,
    ) -> JobTask:
        self._authorize_staff(guild_id, user_id)
        if not isinstance(category, JobCategory):
            raise JobError(JobErrorReason.INVALID_CATEGORY)
        if type(reward) is not int or not self._rules.reward_min <= reward <= self._rules.reward_max:
            raise JobError(JobErrorReason.INVALID_REWARD)
        title = _text(title, 80)
        requirements = _text(requirements, 500)
        if not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
            raise JobError(JobErrorReason.INVALID_DEADLINE)
        async with self._unit_of_work() as repository:
            citizen = await self._verified_citizen(repository, user_id, lock=True)
            now = self._clock()
            if expires_at <= now:
                raise JobError(JobErrorReason.INVALID_DEADLINE)
            return await repository.create_task(
                guild_id=citizen.guild_id,
                category=category,
                title=title,
                requirements=requirements,
                reward=reward,
                expires_at=expires_at.astimezone(UTC),
                created_at=now,
                published_by_citizen_id=citizen.id,
            )

    async def claim(self, guild_id: int | None, user_id: int, task_id: int) -> JobClaim:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            task = await self._task(repository, task_id, lock=True)
            existing = await repository.get_claim(task.id, lock=True)
            citizen = await self._verified_citizen(repository, user_id, lock=True)
            if existing is not None:
                if existing.citizen_id == citizen.id:
                    return existing
                raise JobError(JobErrorReason.TASK_ALREADY_CLAIMED)
            now = self._clock()
            self._check_deadline(task, now)
            if self._rules.claim_cooldown_minutes:
                last = await repository.get_latest_claim_at(task.guild_id, citizen.id)
                if last is not None and (now - last) // _ONE_MINUTE < self._rules.claim_cooldown_minutes:
                    raise JobError(JobErrorReason.COOLDOWN_ACTIVE)
            return await repository.create_claim(
                guild_id=task.guild_id,
                task_id=task.id,
                citizen_id=citizen.id,
                discord_user_id=citizen.discord_user_id,
                claimed_at=now,
            )

    async def submit(
        self, guild_id: int | None, user_id: int, task_id: int, evidence: str
    ) -> JobClaim:
        self._authorize(guild_id, user_id)
        evidence = _text(evidence, 500)
        async with self._unit_of_work() as repository:
            task = await self._task(repository, task_id, lock=True)
            claim = await self._claim(repository, task.id, lock=True)
            citizen = await self._verified_citizen(repository, user_id, lock=True)
            if claim.citizen_id != citizen.id:
                raise JobError(JobErrorReason.CLAIM_NOT_OWNED)
            if claim.status == JobClaimStatus.SUBMITTED and claim.evidence == evidence:
                return claim
            if claim.status != JobClaimStatus.CLAIMED:
                raise JobError(JobErrorReason.INVALID_STATUS)
            now = self._clock()
            self._check_deadline(task, now)
            submitted = replace(claim, status=JobClaimStatus.SUBMITTED, evidence=evidence, submitted_at=now)
            await repository.update_claim(submitted)
            return submitted

    async def review(
        self, guild_id: int | None, user_id: int, task_id: int, *, approve: bool, reason: str
    ) -> JobClaim:
        self._authorize_staff(guild_id, user_id)
        reason = _text(reason, 200)
        async with self._unit_of_work() as repository:
            task = await self._task(repository, task_id, lock=True)
            claim = await self._claim(repository, task.id, lock=True)
            citizens = await repository.get_citizens(
                self._guild_id, (user_id, claim.discord_user_id), lock=True
            )
            verified = {
                citizen.discord_user_id: citizen
                for citizen in citizens
                if citizen.status == CitizenStatus.VERIFIED
            }
            reviewer = verified.get(user_id)
            if reviewer is None:
                raise JobError(JobErrorReason.CITIZEN_NOT_VERIFIED)
            if reviewer.id == claim.citizen_id:
                raise JobError(JobErrorReason.SELF_APPROVAL)
            decision = JobClaimStatus.PAID if approve else JobClaimStatus.REJECTED
            if claim.status == decision:
                return claim
            if claim.status != JobClaimStatus.SUBMITTED:
                raise JobError(JobErrorReason.INVALID_STATUS)
            now = self._clock()
            reviewed = replace(
                claim,
                status=decision,
                reviewed_at=now,
                reviewer_citizen_id=reviewer.id,
                review_reason=reason,
            )
            if approve:
                if claim.discord_user_id not in verified:
                    raise JobError(JobErrorReason.CLAIMANT_NOT_VERIFIED)
                reviewed = await self._payout(repository, task, reviewed, reviewer.id, now)
            await repository.update_claim(reviewed)
            return reviewed

    async def _payout(
        self, repository: JobsRepository, task: JobTask, claim: JobClaim, reviewer_id: int, now: datetime
    ) -> JobClaim:
        week_start_date, start, end = budget_week(now, self._rules.budget_timezone)
        period = await repository.get_or_create_budget_period(
            task.guild_id, start, end, now, self._rules.caps, week_start_date=week_start_date
        )
        if period.spent_total + task.reward > period.caps.overall:
            raise JobError(JobErrorReason.OVERALL_LIMIT)
        if period.spent_npc + task.reward > period.caps.npc:
            raise JobError(JobErrorReason.NPC_LIMIT)
        if await repository.get_citizen_paid(period.id, claim.citizen_id) + task.reward > period.caps.per_citizen:
            raise JobError(JobErrorReason.PERSONAL_LIMIT)

        treasury = await repository.ensure_account(task.guild_id, AccountKind.TREASURY)
        recipient = await repository.ensure_account(task.guild_id, AccountKind.PERSONAL, claim.citizen_id)
        assert claim.review_reason is not None
        try:
            payout = await repository.post(
                task.guild_id,
                TransactionKind.NPC_PAYOUT,
                f"npc_payout:{claim.id}",
                {treasury.id: -task.reward, recipient.id: task.reward},
                actor_citizen_id=reviewer_id,
                reason=claim.review_reason,
            )
        except LedgerError as error:
            if error.reason == LedgerErrorReason.INSUFFICIENT_FUNDS:
                raise JobError(JobErrorReason.TREASURY_INSUFFICIENT_FUNDS) from error
            raise
        await repository.record_budget_spend(period, task.reward)
        return replace(claim, payout_transaction_id=payout.transaction_id, budget_period_id=period.id)

    def _authorize_staff(self, guild_id: int | None, user_id: int) -> None:
        self._authorize(guild_id, user_id)
        if user_id not in self._staff_user_ids:
            raise JobError(JobErrorReason.STAFF_REQUIRED)

    async def _verified_citizen(self, repository: JobsRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise JobError(JobErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    async def _task(self, repository: JobsRepository, task_id: int, *, lock: bool = False) -> JobTask:
        if type(task_id) is not int or not 1 <= task_id <= MAX_AMOUNT:
            raise JobError(JobErrorReason.TASK_NOT_FOUND)
        task = await repository.get_task(self._guild_id, task_id, lock=lock)
        if task is None:
            raise JobError(JobErrorReason.TASK_NOT_FOUND)
        return task

    @staticmethod
    async def _claim(repository: JobsRepository, task_id: int, *, lock: bool = False) -> JobClaim:
        claim = await repository.get_claim(task_id, lock=lock)
        if claim is None:
            raise JobError(JobErrorReason.CLAIM_NOT_FOUND)
        return claim

    @staticmethod
    def _check_deadline(task: JobTask, now: datetime) -> None:
        if now >= task.expires_at:
            raise JobError(JobErrorReason.TASK_EXPIRED)
