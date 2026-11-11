from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.app.jobs import JobError, JobErrorReason
from civitas.domain.jobs import BudgetCaps, BudgetPeriod, JobCategory, JobClaim, JobClaimStatus, JobTask

from .jobs_models import BudgetArticleRow, BudgetPeriodRow, WorkClaimRow, WorkTaskRow
from .ledger import PostgresLedgerRepository
from .models import CitizenRow, GuildRow

_NPC_ARTICLE = "npc_work"


def _to_task(row: WorkTaskRow) -> JobTask:
    return JobTask(
        id=row.id,
        guild_id=row.guild_id,
        category=JobCategory(row.category),
        title=row.title,
        requirements=row.requirements,
        reward=row.reward,
        expires_at=row.expires_at.astimezone(UTC),
        created_at=row.created_at.astimezone(UTC),
        published_by_citizen_id=row.published_by_citizen_id,
    )


def _to_claim(row: WorkClaimRow, discord_user_id: int) -> JobClaim:
    return JobClaim(
        id=row.id,
        guild_id=row.guild_id,
        task_id=row.task_id,
        citizen_id=row.citizen_id,
        discord_user_id=discord_user_id,
        status=JobClaimStatus(row.status),
        claimed_at=row.claimed_at.astimezone(UTC),
        evidence=row.evidence,
        submitted_at=row.submitted_at.astimezone(UTC) if row.submitted_at else None,
        reviewed_at=row.reviewed_at.astimezone(UTC) if row.reviewed_at else None,
        reviewer_citizen_id=row.reviewer_citizen_id,
        review_reason=row.review_reason,
        payout_transaction_id=row.payout_transaction_id,
        budget_period_id=row.budget_period_id,
    )


class PostgresJobsRepository(PostgresLedgerRepository):
    async def list_available_tasks(
        self, guild_id: int, now: datetime, limit: int
    ) -> tuple[JobTask, ...]:
        rows = await self._session.scalars(
            select(WorkTaskRow)
            .join(GuildRow, GuildRow.id == WorkTaskRow.guild_id)
            .where(
                GuildRow.discord_guild_id == Decimal(guild_id),
                WorkTaskRow.expires_at > now,
                ~exists().where(WorkClaimRow.task_id == WorkTaskRow.id),
            )
            .order_by(WorkTaskRow.expires_at, WorkTaskRow.id)
            .limit(limit)
        )
        return tuple(_to_task(row) for row in rows)

    async def list_submitted_task_ids(self, guild_id: int, limit: int) -> tuple[int, ...]:
        rows = await self._session.scalars(
            select(WorkClaimRow.task_id)
            .join(GuildRow, GuildRow.id == WorkClaimRow.guild_id)
            .where(
                GuildRow.discord_guild_id == Decimal(guild_id),
                WorkClaimRow.status == JobClaimStatus.SUBMITTED.value,
            )
            .order_by(WorkClaimRow.submitted_at, WorkClaimRow.id)
            .limit(limit)
        )
        return tuple(rows)

    async def get_task(
        self, guild_id: int, task_id: int, *, lock: bool = False
    ) -> JobTask | None:
        statement = (
            select(WorkTaskRow)
            .join(GuildRow, GuildRow.id == WorkTaskRow.guild_id)
            .where(GuildRow.discord_guild_id == Decimal(guild_id), WorkTaskRow.id == task_id)
        )
        if lock:
            statement = statement.with_for_update(of=WorkTaskRow, key_share=True)
        row = await self._session.scalar(statement)
        return _to_task(row) if row is not None else None

    async def get_claim(self, task_id: int, *, lock: bool = False) -> JobClaim | None:
        statement = (
            select(WorkClaimRow, CitizenRow.discord_user_id)
            .join(CitizenRow, CitizenRow.id == WorkClaimRow.citizen_id)
            .where(WorkClaimRow.task_id == task_id)
        )
        if lock:
            statement = statement.with_for_update(of=WorkClaimRow, key_share=True)
        result = (await self._session.execute(statement)).one_or_none()
        return _to_claim(result[0], int(result[1])) if result is not None else None

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
    ) -> JobTask:
        row = (
            await self._session.scalars(
                insert(WorkTaskRow)
                .values(
                    guild_id=guild_id,
                    category=category.value,
                    title=title,
                    requirements=requirements,
                    reward=reward,
                    expires_at=expires_at,
                    created_at=created_at,
                    published_by_citizen_id=published_by_citizen_id,
                )
                .returning(WorkTaskRow)
            )
        ).one()
        return _to_task(row)

    async def create_claim(
        self,
        *,
        guild_id: int,
        task_id: int,
        citizen_id: int,
        discord_user_id: int,
        claimed_at: datetime,
    ) -> JobClaim:
        row = (
            await self._session.scalars(
                insert(WorkClaimRow)
                .values(
                    guild_id=guild_id,
                    task_id=task_id,
                    citizen_id=citizen_id,
                    status=JobClaimStatus.CLAIMED.value,
                    claimed_at=claimed_at,
                )
                .returning(WorkClaimRow)
            )
        ).one()
        return _to_claim(row, discord_user_id)

    async def update_claim(self, claim: JobClaim) -> None:
        await self._session.execute(
            update(WorkClaimRow)
            .where(WorkClaimRow.guild_id == claim.guild_id, WorkClaimRow.id == claim.id)
            .values(
                status=claim.status.value,
                evidence=claim.evidence,
                submitted_at=claim.submitted_at,
                reviewed_at=claim.reviewed_at,
                reviewer_citizen_id=claim.reviewer_citizen_id,
                review_reason=claim.review_reason,
                payout_transaction_id=claim.payout_transaction_id,
                budget_period_id=claim.budget_period_id,
            )
        )

    async def get_latest_claim_at(self, guild_id: int, citizen_id: int) -> datetime | None:
        claimed_at = await self._session.scalar(
            select(func.max(WorkClaimRow.claimed_at)).where(
                WorkClaimRow.guild_id == guild_id, WorkClaimRow.citizen_id == citizen_id
            )
        )
        return claimed_at.astimezone(UTC) if claimed_at is not None else None

    async def _get_active_period(self, guild_id: int, at: datetime) -> BudgetPeriodRow | None:
        return await self._session.scalar(
            select(BudgetPeriodRow)
            .where(
                BudgetPeriodRow.guild_id == guild_id,
                BudgetPeriodRow.start_at <= at,
                BudgetPeriodRow.end_at > at,
            )
            .with_for_update(key_share=True)
        )

    async def get_or_create_budget_period(
        self,
        guild_id: int,
        start_at: datetime,
        end_at: datetime,
        at: datetime,
        caps: BudgetCaps,
        *,
        week_start_date: date,
    ) -> BudgetPeriod:
        period = await self._get_active_period(guild_id, at)
        if period is None:
            # Serialize creation across processes, including callers using
            # different timezone settings for the same guild.
            await self._session.scalar(
                select(GuildRow.id).where(GuildRow.id == guild_id).with_for_update(key_share=True)
            )
            period = await self._get_active_period(guild_id, at)
        if period is None:
            period = await self._create_budget_period(
                guild_id, start_at, end_at, at, caps, week_start_date
            )

        article = (
            await self._session.scalars(
                select(BudgetArticleRow)
                .where(BudgetArticleRow.period_id == period.id, BudgetArticleRow.code == _NPC_ARTICLE)
                .with_for_update(key_share=True)
            )
        ).one()
        return BudgetPeriod(
            id=period.id,
            guild_id=period.guild_id,
            start_at=period.start_at.astimezone(UTC),
            end_at=period.end_at.astimezone(UTC),
            caps=BudgetCaps(overall=period.overall_cap, npc=article.cap, per_citizen=article.personal_cap),
            spent_total=period.spent_total,
            spent_npc=article.spent,
        )

    async def _create_budget_period(
        self,
        guild_id: int,
        start_at: datetime,
        end_at: datetime,
        at: datetime,
        caps: BudgetCaps,
        week_start_date: date,
    ) -> BudgetPeriodRow:
        previous = await self._session.scalar(
            select(BudgetPeriodRow)
            .where(BudgetPeriodRow.guild_id == guild_id, BudgetPeriodRow.end_at <= at)
            .order_by(BudgetPeriodRow.end_at.desc())
            .limit(1)
        )
        following = await self._session.scalar(
            select(BudgetPeriodRow)
            .where(BudgetPeriodRow.guild_id == guild_id, BudgetPeriodRow.start_at > at)
            .order_by(BudgetPeriodRow.start_at)
            .limit(1)
        )
        # A timezone change can place this instant in a logical week already
        # budgeted. It cannot create a second allowance for that week.
        if (
            (previous is not None and previous.week_start_date >= week_start_date)
            or (following is not None and following.week_start_date <= week_start_date)
        ):
            raise JobError(JobErrorReason.BUDGET_PERIOD_UNAVAILABLE)
        if previous is not None:
            start_at = max(start_at, previous.end_at)
        if following is not None:
            end_at = min(end_at, following.start_at)

        period = (
            await self._session.scalars(
                insert(BudgetPeriodRow)
                .values(
                    guild_id=guild_id,
                    week_start_date=week_start_date,
                    start_at=start_at,
                    end_at=end_at,
                    overall_cap=caps.overall,
                )
                .returning(BudgetPeriodRow)
            )
        ).one()
        await self._session.execute(
            insert(BudgetArticleRow).values(
                period_id=period.id, code=_NPC_ARTICLE, cap=caps.npc, personal_cap=caps.per_citizen
            )
        )
        return period

    async def get_citizen_paid(self, period_id: int, citizen_id: int) -> int:
        amount = await self._session.scalar(
            select(func.coalesce(func.sum(WorkTaskRow.reward), 0))
            .join(WorkClaimRow, WorkClaimRow.task_id == WorkTaskRow.id)
            .where(
                WorkClaimRow.budget_period_id == period_id,
                WorkClaimRow.citizen_id == citizen_id,
                WorkClaimRow.status == JobClaimStatus.PAID.value,
            )
        )
        return int(amount)

    async def record_budget_spend(self, period: BudgetPeriod, amount: int) -> None:
        await self._session.execute(
            update(BudgetPeriodRow)
            .where(BudgetPeriodRow.guild_id == period.guild_id, BudgetPeriodRow.id == period.id)
            .values(spent_total=BudgetPeriodRow.spent_total + amount)
        )
        await self._session.execute(
            update(BudgetArticleRow)
            .where(BudgetArticleRow.period_id == period.id, BudgetArticleRow.code == _NPC_ARTICLE)
            .values(spent=BudgetArticleRow.spent + amount)
        )


class PostgresJobsUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresJobsRepository]:
        async with self._sessions.begin() as session:
            yield PostgresJobsRepository(session)
