from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.jobs import JobError, JobErrorReason, JobService, JobsRepository, JobsRules, budget_week
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.jobs import BudgetCaps, BudgetPeriod, JobCategory, JobClaim, JobClaimStatus, JobTask
from civitas.domain.ledger import Account, AccountKind, LedgerError, LedgerErrorReason, PostResult, TransactionKind

DISCORD_GUILD = 100
GUILD = 1
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
REVIEWER = Citizen(1, GUILD, 200, CitizenStatus.VERIFIED, NOW, NOW)
PLAYER = Citizen(2, GUILD, 201, CitizenStatus.VERIFIED, NOW, NOW)
OTHER = Citizen(3, GUILD, 202, CitizenStatus.VERIFIED, NOW, NOW)
TASK = JobTask(7, GUILD, JobCategory.LOGISTICS, "Тест", "Условия", 30, NOW + timedelta(hours=1), NOW, REVIEWER.id)
CLAIM = JobClaim(8, GUILD, TASK.id, PLAYER.id, PLAYER.discord_user_id, JobClaimStatus.CLAIMED, NOW)
SUBMISSION = replace(CLAIM, status=JobClaimStatus.SUBMITTED, evidence="Выполнено", submitted_at=NOW)
RULES = JobsRules(overall_weekly_limit=800, npc_weekly_limit=300, personal_weekly_limit=100, reward_min=10, reward_max=40)
PERIOD = BudgetPeriod(9, GUILD, datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC), RULES.caps, 0, 0)
TREASURY = Account(10, GUILD, AccountKind.TREASURY, None)
PLAYER_ACCOUNT = Account(11, GUILD, AccountKind.PERSONAL, PLAYER.id)


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=JobsRepository)

    def get_citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        return tuple(
            citizen for citizen in (REVIEWER, PLAYER, OTHER) if citizen.discord_user_id in user_ids
        )

    def update_claim(claim: JobClaim) -> None:
        repo.get_claim.return_value = claim

    repo.get_citizens.side_effect = get_citizens
    repo.get_task.return_value = TASK
    repo.get_claim.return_value = SUBMISSION
    repo.update_claim.side_effect = update_claim
    repo.create_task.return_value = TASK
    repo.create_claim.return_value = CLAIM
    repo.list_available_tasks.return_value = (TASK,)
    repo.list_submitted_task_ids.return_value = (TASK.id,)
    repo.get_or_create_budget_period.return_value = PERIOD
    repo.get_citizen_paid.return_value = 0
    repo.ensure_account.side_effect = (TREASURY, PLAYER_ACCOUNT)
    repo.post.return_value = PostResult(12, created=True)
    return repo


def make_service(
    repository: AsyncMock,
    *,
    now: datetime = NOW,
    rules: JobsRules = RULES,
    staff_ids: tuple[int, ...] = (REVIEWER.discord_user_id,),
) -> JobService:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[JobsRepository]:
        yield repository

    citizens = CitizensService(
        lambda: pytest.fail("Job authorization should only consult configured Discord IDs"),
        guild_id=DISCORD_GUILD,
        allowed_user_ids=[REVIEWER.discord_user_id, PLAYER.discord_user_id, OTHER.discord_user_id],
    )
    return JobService(
        unit_of_work,
        guild_id=DISCORD_GUILD,
        authorize=citizens.authorize,
        staff_user_ids=staff_ids,
        rules=rules,
        clock=lambda: now,
    )


def test_budget_week_is_monday_in_local_timezone() -> None:
    sunday_utc = datetime(2026, 11, 8, 22, tzinfo=UTC)

    assert budget_week(sunday_utc, timezone(timedelta(hours=3))) == (
        date(2026, 11, 9),
        datetime(2026, 11, 8, 21, tzinfo=UTC),
        datetime(2026, 11, 15, 21, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_publish_checks_actor_and_reward_and_persists_normalized_task(repository: AsyncMock) -> None:
    jobs = make_service(repository)
    details = dict(
        category=JobCategory.LOGISTICS,
        title="  Тест  ",
        requirements="Условия",
        reward=30,
        expires_at=TASK.expires_at,
    )

    with pytest.raises(CitizenshipAccessError):
        await jobs.publish(None, REVIEWER.discord_user_id, **details)
    with pytest.raises(JobError, match=JobErrorReason.STAFF_REQUIRED):
        await jobs.publish(DISCORD_GUILD, PLAYER.discord_user_id, **details)
    with pytest.raises(JobError, match=JobErrorReason.INVALID_REWARD):
        await jobs.publish(DISCORD_GUILD, REVIEWER.discord_user_id, **(details | {"reward": 41}))
    with pytest.raises(JobError, match=JobErrorReason.INVALID_DEADLINE):
        await jobs.publish(DISCORD_GUILD, REVIEWER.discord_user_id, **(details | {"expires_at": NOW}))

    assert await jobs.publish(DISCORD_GUILD, REVIEWER.discord_user_id, **details) == TASK
    repository.create_task.assert_awaited_once_with(
        guild_id=GUILD,
        category=JobCategory.LOGISTICS,
        title="Тест",
        requirements="Условия",
        reward=30,
        expires_at=TASK.expires_at,
        created_at=NOW,
        published_by_citizen_id=REVIEWER.id,
    )
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_and_details_hide_private_submission_from_other_citizens(repository: AsyncMock) -> None:
    jobs = make_service(repository)

    assert await jobs.list_tasks(DISCORD_GUILD, OTHER.discord_user_id) == (TASK,)
    repository.list_available_tasks.assert_awaited_once_with(DISCORD_GUILD, NOW, 10)
    assert (await jobs.get_task_details(DISCORD_GUILD, OTHER.discord_user_id, TASK.id)).claim is None
    assert (await jobs.get_task_details(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id)).claim == SUBMISSION
    assert (await jobs.get_review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id)).claim == SUBMISSION
    assert await jobs.list_submitted_task_ids(DISCORD_GUILD, REVIEWER.discord_user_id) == (TASK.id,)
    with pytest.raises(JobError, match=JobErrorReason.STAFF_REQUIRED):
        await jobs.get_review(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id)


@pytest.mark.asyncio
async def test_claim_one_per_task_with_idempotent_owner_retry(repository: AsyncMock) -> None:
    jobs = make_service(repository)
    repository.get_claim.side_effect = (None, CLAIM, CLAIM)

    assert await jobs.claim(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id) == CLAIM
    assert await jobs.claim(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id) == CLAIM
    with pytest.raises(JobError, match=JobErrorReason.TASK_ALREADY_CLAIMED):
        await jobs.claim(DISCORD_GUILD, OTHER.discord_user_id, TASK.id)

    repository.get_task.assert_awaited_with(DISCORD_GUILD, TASK.id, lock=True)
    repository.create_claim.assert_awaited_once_with(
        guild_id=GUILD,
        task_id=TASK.id,
        citizen_id=PLAYER.id,
        discord_user_id=PLAYER.discord_user_id,
        claimed_at=NOW,
    )


@pytest.mark.asyncio
async def test_claim_cooldown_and_deadline_do_not_create_claim(repository: AsyncMock) -> None:
    repository.get_claim.return_value = None
    repository.get_latest_claim_at.return_value = NOW - timedelta(minutes=9)
    jobs = make_service(repository, rules=replace(RULES, claim_cooldown_minutes=10))
    expired_jobs = make_service(repository, now=TASK.expires_at)

    with pytest.raises(JobError, match=JobErrorReason.COOLDOWN_ACTIVE):
        await jobs.claim(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id)
    with pytest.raises(JobError, match=JobErrorReason.TASK_EXPIRED):
        await expired_jobs.claim(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id)

    repository.create_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_submission_requires_owner_before_deadline_and_replays_same_proof(repository: AsyncMock) -> None:
    repository.get_claim.return_value = CLAIM
    jobs = make_service(repository)
    expired_jobs = make_service(repository, now=TASK.expires_at)

    with pytest.raises(JobError, match=JobErrorReason.CLAIM_NOT_OWNED):
        await jobs.submit(DISCORD_GUILD, OTHER.discord_user_id, TASK.id, "Выполнено")
    with pytest.raises(JobError, match=JobErrorReason.TASK_EXPIRED):
        await expired_jobs.submit(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, "Выполнено")
    assert await jobs.submit(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, "Выполнено") == SUBMISSION
    assert await expired_jobs.submit(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, "Выполнено") == SUBMISSION
    with pytest.raises(JobError, match=JobErrorReason.INVALID_STATUS):
        await jobs.submit(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, "Другое доказательство")

    repository.update_claim.assert_awaited_once_with(SUBMISSION)
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_transfers_from_treasury_once_and_records_period(repository: AsyncMock) -> None:
    review_time = TASK.expires_at + timedelta(hours=1)
    jobs = make_service(repository, now=review_time)

    paid = await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=True, reason="Проверено")
    repeated = await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=True, reason="Повтор")

    assert paid == repeated
    assert paid.status == JobClaimStatus.PAID
    assert paid.review_reason == "Проверено"
    assert paid.payout_transaction_id == 12
    assert paid.budget_period_id == PERIOD.id
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.NPC_PAYOUT,
        f"npc_payout:{CLAIM.id}",
        {TREASURY.id: -TASK.reward, PLAYER_ACCOUNT.id: TASK.reward},
        actor_citizen_id=REVIEWER.id,
        reason="Проверено",
    )
    repository.record_budget_spend.assert_awaited_once_with(PERIOD, TASK.reward)
    repository.update_claim.assert_awaited_once_with(paid)
    repository.get_or_create_budget_period.assert_awaited_once_with(
        GUILD, PERIOD.start_at, PERIOD.end_at, review_time, RULES.caps, week_start_date=date(2026, 11, 9)
    )


@pytest.mark.asyncio
async def test_self_approval_is_blocked_and_rejection_has_no_payout(repository: AsyncMock) -> None:
    jobs = make_service(repository, staff_ids=(REVIEWER.discord_user_id, PLAYER.discord_user_id))

    with pytest.raises(JobError, match=JobErrorReason.SELF_APPROVAL):
        await jobs.review(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, approve=True, reason="Проверено")
    with pytest.raises(JobError, match=JobErrorReason.SELF_APPROVAL):
        await jobs.review(DISCORD_GUILD, PLAYER.discord_user_id, TASK.id, approve=False, reason="Неполно")
    rejected = await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=False, reason="Неполно")
    assert rejected.status == JobClaimStatus.REJECTED
    assert rejected.review_reason == "Неполно"
    assert rejected.payout_transaction_id is None
    assert await jobs.review(
        DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=False, reason="Неполно"
    ) == rejected
    repository.update_claim.assert_awaited_once_with(rejected)
    repository.get_or_create_budget_period.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.parametrize(
    ("suspended", "reason"),
    [
        (REVIEWER, JobErrorReason.CITIZEN_NOT_VERIFIED),
        (PLAYER, JobErrorReason.CLAIMANT_NOT_VERIFIED),
    ],
)
@pytest.mark.asyncio
async def test_approval_requires_both_citizens_to_be_verified(
    repository: AsyncMock, suspended: Citizen, reason: JobErrorReason
) -> None:
    repository.get_citizens.side_effect = None
    repository.get_citizens.return_value = tuple(
        replace(citizen, status=CitizenStatus.SUSPENDED) if citizen == suspended else citizen
        for citizen in (REVIEWER, PLAYER)
    )
    jobs = make_service(repository)

    with pytest.raises(JobError, match=reason):
        await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=True, reason="Проверено")

    repository.update_claim.assert_not_awaited()
    repository.get_or_create_budget_period.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.parametrize(
    ("caps", "total_spent", "npc_spent", "personal_spent", "reason"),
    [
        (BudgetCaps(100, 100, 100), 80, 0, 0, JobErrorReason.OVERALL_LIMIT),
        (BudgetCaps(800, 100, 100), 80, 80, 0, JobErrorReason.NPC_LIMIT),
        (BudgetCaps(800, 300, 80), 60, 60, 60, JobErrorReason.PERSONAL_LIMIT),
    ],
)
@pytest.mark.asyncio
async def test_approval_checks_persisted_limits_before_ledger(
    repository: AsyncMock,
    caps: BudgetCaps,
    total_spent: int,
    npc_spent: int,
    personal_spent: int,
    reason: JobErrorReason,
) -> None:
    repository.get_or_create_budget_period.return_value = replace(
        PERIOD, caps=caps, spent_total=total_spent, spent_npc=npc_spent
    )
    repository.get_citizen_paid.return_value = personal_spent
    jobs = make_service(repository)

    with pytest.raises(JobError, match=reason):
        await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=True, reason="Проверено")

    repository.post.assert_not_awaited()
    repository.record_budget_spend.assert_not_awaited()
    repository.update_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_insufficient_treasury_does_not_record_approval(repository: AsyncMock) -> None:
    jobs = make_service(repository)
    repository.post.side_effect = LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)

    with pytest.raises(JobError, match=JobErrorReason.TREASURY_INSUFFICIENT_FUNDS):
        await jobs.review(DISCORD_GUILD, REVIEWER.discord_user_id, TASK.id, approve=True, reason="Проверено")

    repository.record_budget_spend.assert_not_awaited()
    repository.update_claim.assert_not_awaited()
