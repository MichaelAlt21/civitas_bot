import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.app.jobs import JobError, JobErrorReason, JobsRules, JobService, budget_week
from civitas.app.wallet import WalletFunding
from civitas.domain.jobs import BudgetCaps, JobCategory, JobClaim, JobClaimStatus
from civitas.domain.ledger import AccountKind, TransactionKind
from civitas.infrastructure.db.jobs import PostgresJobsUnitOfWork
from civitas.infrastructure.db.jobs_models import BudgetArticleRow, BudgetPeriodRow
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow

pytestmark = pytest.mark.asyncio

GUILD = 100
REVIEWERS = (11, 12)
WORKERS = (21, 22, 23, 24, 25)
MONDAY = datetime(2026, 11, 9, tzinfo=UTC)
NOW = MONDAY + timedelta(hours=12)
RULES = JobsRules(
    overall_weekly_limit=800, npc_weekly_limit=300, personal_weekly_limit=100,
    reward_min=10, reward_max=40,
)


def _service(unit_of_work: PostgresJobsUnitOfWork, citizens: CitizensService) -> JobService:
    return JobService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, staff_user_ids=REVIEWERS,
        rules=RULES, clock=lambda: NOW,
    )


async def _setup(
    engine: AsyncEngine, *, treasury_genesis: int = 8000
) -> tuple[PostgresJobsUnitOfWork, CitizensService, JobService, int]:
    unit_of_work = PostgresJobsUnitOfWork(engine)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids=(*REVIEWERS, *WORKERS)
    )
    for user_id in (*REVIEWERS, *WORKERS):
        await citizens.register(GUILD, user_id)
    async with unit_of_work() as repository:
        guild = await repository.ensure_guild(GUILD)
        await WalletFunding(
            starter_grant=0, treasury_genesis=treasury_genesis
        ).initialize_guild(repository, guild)
    return unit_of_work, citizens, _service(unit_of_work, citizens), guild


async def _submitted_task(service: JobService, worker: int, reward: int) -> int:
    task = await service.publish(
        GUILD, REVIEWERS[0], category=JobCategory.SERVICES, title="Document the town",
        requirements="Summarize a community event", reward=reward, expires_at=NOW + timedelta(days=1),
    )
    await service.claim(GUILD, worker, task.id)
    await service.submit(GUILD, worker, task.id, "Submitted the event summary")
    return task.id


async def _approve(service: JobService, task: int, reviewer: int = REVIEWERS[0]) -> JobClaim:
    return await service.review(GUILD, reviewer, task, approve=True, reason="Work confirmed")


async def test_double_approval_and_restart_pay_once(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert {"budget_periods", "budget_articles", "work_tasks", "work_claims"} <= set(tables)
    unit_of_work, citizens, service, guild = await _setup(pg_engine)
    task = await _submitted_task(service, WORKERS[0], 30)
    assert await service.list_submitted_task_ids(GUILD, REVIEWERS[0]) == (task,)

    first, second = await asyncio.wait_for(
        asyncio.gather(_approve(service, task), _approve(service, task, REVIEWERS[1])),
        timeout=10,
    )
    assert first == second
    assert first.status is JobClaimStatus.PAID
    restarted = _service(PostgresJobsUnitOfWork(pg_engine), citizens)
    assert await _approve(restarted, task) == first
    assert await restarted.list_submitted_task_ids(GUILD, REVIEWERS[0]) == ()

    async with unit_of_work() as repository:
        treasury = await repository.ensure_account(guild, AccountKind.TREASURY)
        recipient = await repository.ensure_account(guild, AccountKind.PERSONAL, first.citizen_id)
        issuer = await repository.ensure_account(guild, AccountKind.ISSUER)
        assert await repository.get_balance(treasury.id) == 7970
        assert await repository.get_balance(recipient.id) == 30
        assert await repository.get_balance(issuer.id) == -8000
        assert [entry.kind for entry in await repository.get_history(recipient.id, 10)] == [
            TransactionKind.NPC_PAYOUT
        ]
        assert await repository.get_citizen_paid(first.budget_period_id, first.citizen_id) == 30

    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(BudgetPeriodRow.spent_total)) == 30
        assert await connection.scalar(select(BudgetArticleRow.spent)) == 30
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind == TransactionKind.NPC_PAYOUT)
        ) == 1


async def test_weekly_personal_cap_rejects_next_reward(pg_engine: AsyncEngine) -> None:
    _, _, service, _ = await _setup(pg_engine)
    for reward in (40, 40, 20):
        await _approve(service, await _submitted_task(service, WORKERS[0], reward))
    rejected = await _submitted_task(service, WORKERS[0], 10)
    with pytest.raises(JobError) as failure:
        await _approve(service, rejected)
    assert failure.value.reason is JobErrorReason.PERSONAL_LIMIT
    detail = await service.get_review(GUILD, REVIEWERS[0], rejected)
    assert detail.claim.status is JobClaimStatus.SUBMITTED
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(BudgetArticleRow.spent)) == 100


async def test_weekly_npc_cap_serializes_independent_approvals(pg_engine: AsyncEngine) -> None:
    _, _, service, _ = await _setup(pg_engine)
    for worker, rewards in zip(WORKERS, ((40, 40, 20), (40, 40, 20), (40, 40))):
        for reward in rewards:
            await _approve(service, await _submitted_task(service, worker, reward))

    left = await _submitted_task(service, WORKERS[3], 20)
    right = await _submitted_task(service, WORKERS[4], 20)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            _approve(service, left), _approve(service, right, REVIEWERS[1]), return_exceptions=True,
        ),
        timeout=10,
    )
    assert len([outcome for outcome in outcomes if isinstance(outcome, JobClaim)]) == 1
    assert [outcome.reason for outcome in outcomes if isinstance(outcome, JobError)] == [
        JobErrorReason.NPC_LIMIT
    ]
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(BudgetArticleRow.spent)) == 300
        assert await connection.scalar(select(BudgetPeriodRow.spent_total)) == 300


async def test_restart_and_timezone_change_reuse_weekly_snapshot(pg_engine: AsyncEngine) -> None:
    unit_of_work, _, _, guild = await _setup(pg_engine)
    caps = BudgetCaps(overall=800, npc=300, per_citizen=100)
    paused = BudgetCaps(overall=800, npc=0, per_citizen=100)
    week, start, end = budget_week(NOW, UTC)
    async with unit_of_work() as repository:
        first = await repository.get_or_create_budget_period(
            guild, start, end, NOW, caps, week_start_date=week
        )

    restarted = PostgresJobsUnitOfWork(pg_engine)
    shifted_zone = ZoneInfo("Pacific/Honolulu")
    shifted_week, shifted_start, shifted_end = budget_week(NOW, shifted_zone)
    async with restarted() as repository:
        same = await repository.get_or_create_budget_period(
            guild, shifted_start, shifted_end, NOW, paused, week_start_date=shifted_week
        )
    assert same == first

    # It is still the same local week after the first UTC period has closed.
    after_utc_close = end + timedelta(hours=1)
    with pytest.raises(JobError) as failure:
        async with restarted() as repository:
            await repository.get_or_create_budget_period(
                guild, shifted_start, shifted_end, after_utc_close, paused, week_start_date=shifted_week
            )
    assert failure.value.reason is JobErrorReason.BUDGET_PERIOD_UNAVAILABLE

    next_week, next_start, next_end = budget_week(shifted_end, shifted_zone)
    async with restarted() as repository:
        fresh = await repository.get_or_create_budget_period(
            guild, next_start, next_end, shifted_end, paused, week_start_date=next_week
        )
    assert fresh.id != first.id
    assert fresh.caps == paused
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(BudgetPeriodRow)) == 2


async def test_treasury_failure_and_orphan_payout_are_atomic(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, service, guild = await _setup(pg_engine, treasury_genesis=10)
    task = await _submitted_task(service, WORKERS[0], 30)
    with pytest.raises(JobError) as failure:
        await _approve(service, task)
    assert failure.value.reason is JobErrorReason.TREASURY_INSUFFICIENT_FUNDS
    detail = await service.get_review(GUILD, REVIEWERS[0], task)
    assert detail.claim.status is JobClaimStatus.SUBMITTED

    reviewer = await citizens.get_profile(GUILD, REVIEWERS[0])
    assert reviewer is not None
    with pytest.raises(IntegrityError, match="NPC payout must identify a paid review"):
        async with unit_of_work() as repository:
            treasury = await repository.ensure_account(guild, AccountKind.TREASURY)
            recipient = await repository.ensure_account(guild, AccountKind.PERSONAL, detail.claim.citizen_id)
            await repository.post(
                guild, TransactionKind.NPC_PAYOUT, f"npc_payout:{detail.claim.id}",
                {treasury.id: -1, recipient.id: 1}, actor_citizen_id=reviewer.id, reason="raw orphan",
            )
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(BudgetPeriodRow)) == 0
        assert await connection.scalar(select(func.count()).select_from(FinancialTransactionRow)) == 1
