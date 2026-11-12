"""Read public ledger aggregates and persist retryable report deliveries."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.domain.reports import BudgetArticleReport, ReportDelivery, ReportWindow, TreasuryReport

from .jobs_models import BudgetPeriodRow
from .models import GuildRow
from .report_models import WeeklyReportDeliveryRow
from .treasury import PostgresTreasuryRepository


_REPORT_TOTALS = text("""
    WITH selected_guild AS (
        SELECT id FROM guilds WHERE discord_guild_id = :discord_guild_id
    ), report_period AS (
        SELECT id, overall_cap FROM budget_periods
        WHERE guild_id = (SELECT id FROM selected_guild) AND week_start_date = :week_start
    ), tracked_entries AS (
        SELECT account.kind AS account_kind, posting.kind AS tx_kind, posting.created_at,
            CAST(entry.delta AS numeric) AS delta
        FROM financial_accounts AS account
        JOIN ledger_entries AS entry ON entry.account_id = account.id AND entry.guild_id = account.guild_id
        JOIN financial_transactions AS posting ON posting.id = entry.transaction_id
            AND posting.guild_id = entry.guild_id
        WHERE account.guild_id = (SELECT id FROM selected_guild)
            AND account.kind IN ('TREASURY', 'ESCROW', 'ISSUER') AND posting.created_at < :as_of
    ), cash AS (
        SELECT
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at < :starts_at), 0)
                AS opening_balance,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY'), 0) AS closing_balance,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND delta > 0), 0) AS income,
            coalesce(sum(-delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND delta < 0), 0) AS expenses,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'COMPANY_FEE'), 0) AS company_fees,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'ORDER_SETTLE'), 0) AS sales_tax,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'ASSET_PURCHASE'), 0) AS asset_sales,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'GENESIS'), 0) AS treasury_issuance,
            coalesce(sum(-delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'NPC_PAYOUT'), 0) AS npc_payouts,
            coalesce(sum(-delta) FILTER (WHERE account_kind = 'TREASURY' AND created_at >= :starts_at
                AND tx_kind = 'STATE_CONTRACT_PAYOUT'), 0) AS contract_payouts,
            coalesce(sum(-delta) FILTER (WHERE account_kind = 'ISSUER' AND created_at >= :starts_at), 0)
                AS total_issued,
            coalesce(sum(-delta) FILTER (WHERE account_kind = 'ISSUER' AND created_at >= :starts_at
                AND tx_kind = 'STARTER_GRANT'), 0) AS starter_grants,
            coalesce(sum(delta) FILTER (WHERE account_kind = 'ESCROW'), 0) AS open_escrow
        FROM tracked_entries
    ), activity AS (
        SELECT count(*) AS transaction_count,
            count(*) FILTER (WHERE kind = 'ORDER_SETTLE') AS settled_orders,
            count(*) FILTER (WHERE kind = 'ASSET_PURCHASE') AS asset_purchases
        FROM financial_transactions
        WHERE guild_id = (SELECT id FROM selected_guild) AND created_at >= :starts_at AND created_at < :as_of
    ), contract_events AS (
        SELECT contract.amount, contract.budget_period_id, contract.status, contract.reserved_at,
            CASE WHEN contract.status = 'PAID' THEN posting.created_at ELSE contract.closed_at END AS released_at
        FROM state_contracts AS contract
        LEFT JOIN financial_transactions AS posting ON posting.id = contract.payout_transaction_id
            AND posting.guild_id = contract.guild_id
        WHERE contract.guild_id = (SELECT id FROM selected_guild)
    ), contract_positions AS (
        SELECT amount, budget_period_id, status, released_at,
            reserved_at < :as_of AND (released_at IS NULL OR released_at >= :as_of) AS held_at_cutoff
        FROM contract_events
    ), contract_totals AS (
        SELECT coalesce(sum(amount) FILTER (WHERE held_at_cutoff), 0) AS cash_reserved,
            coalesce(sum(amount) FILTER (WHERE held_at_cutoff
                AND budget_period_id = (SELECT id FROM report_period)), 0) AS period_reserved,
            coalesce(sum(amount) FILTER (WHERE status = 'PAID' AND released_at < :as_of
                AND budget_period_id = (SELECT id FROM report_period)), 0) AS period_spent
        FROM contract_positions
    ), npc_totals AS (
        SELECT coalesce(sum(task.reward), 0) AS period_spent
        FROM work_claims AS claim JOIN work_tasks AS task ON task.id = claim.task_id
        JOIN financial_transactions AS posting ON posting.id = claim.payout_transaction_id
            AND posting.guild_id = claim.guild_id
        WHERE claim.budget_period_id = (SELECT id FROM report_period)
            AND claim.status = 'PAID' AND posting.created_at < :as_of
    ), article_data AS (
        SELECT article.code, article.cap,
            CASE article.code WHEN 'npc_work' THEN npc_totals.period_spent
                WHEN 'state_contracts' THEN contract_totals.period_spent END AS spent,
            CASE article.code WHEN 'npc_work' THEN 0
                WHEN 'state_contracts' THEN contract_totals.period_reserved END AS reserved
        FROM budget_articles AS article
        LEFT JOIN contract_budget_approvals AS approval ON approval.period_id = article.period_id
        CROSS JOIN contract_totals CROSS JOIN npc_totals
        WHERE article.period_id = (SELECT id FROM report_period)
            AND (article.code = 'npc_work' OR (article.code = 'state_contracts' AND approval.activated_at < :as_of))
    )
    SELECT cash.*, activity.*, contract_totals.cash_reserved,
        (SELECT overall_cap FROM report_period) AS budget_limit,
        coalesce((SELECT jsonb_agg(to_jsonb(article_data) ORDER BY code) FROM article_data), '[]') AS budget_articles
    FROM cash CROSS JOIN activity CROSS JOIN contract_totals
""")


def _guild_id(discord_guild_id: int) -> Select:
    return select(GuildRow.id).where(GuildRow.discord_guild_id == Decimal(discord_guild_id))


class PostgresReportRepository(PostgresTreasuryRepository):
    async def get_active_report_window(self, guild_id: int, at: datetime) -> ReportWindow | None:
        row = (await self._session.execute(
            select(BudgetPeriodRow.week_start_date, BudgetPeriodRow.start_at, BudgetPeriodRow.end_at).where(
                BudgetPeriodRow.guild_id == _guild_id(guild_id).scalar_subquery(),
                BudgetPeriodRow.start_at <= at, BudgetPeriodRow.end_at > at,
            ).order_by(BudgetPeriodRow.start_at.desc()).limit(1)
        )).one_or_none()
        if row is None:
            return None
        return ReportWindow(row.week_start_date, row.start_at.astimezone(UTC), row.end_at.astimezone(UTC))

    async def get_report_window(self, guild_id: int, week_start: date) -> ReportWindow | None:
        row = (await self._session.execute(
            select(BudgetPeriodRow.start_at, BudgetPeriodRow.end_at).where(
                BudgetPeriodRow.guild_id == _guild_id(guild_id).scalar_subquery(),
                BudgetPeriodRow.week_start_date == week_start,
            )
        )).one_or_none()
        if row is None:
            return None
        return ReportWindow(week_start, row.start_at.astimezone(UTC), row.end_at.astimezone(UTC))

    async def read_report(self, guild_id: int, window: ReportWindow, *, as_of: datetime) -> TreasuryReport:
        row = (await self._session.execute(_REPORT_TOTALS, {
            "discord_guild_id": Decimal(guild_id), "week_start": window.week_start,
            "starts_at": window.starts_at, "as_of": as_of,
        })).mappings().one()
        return TreasuryReport(
            window=window, as_of=as_of,
            opening_balance=int(row["opening_balance"]), closing_balance=int(row["closing_balance"]),
            company_fees=int(row["company_fees"]), sales_tax=int(row["sales_tax"]),
            asset_sales=int(row["asset_sales"]), treasury_issuance=int(row["treasury_issuance"]),
            other_income=int(row["income"] - row["company_fees"] - row["sales_tax"]
                             - row["asset_sales"] - row["treasury_issuance"]),
            npc_payouts=int(row["npc_payouts"]), contract_payouts=int(row["contract_payouts"]),
            other_expenses=int(row["expenses"] - row["npc_payouts"] - row["contract_payouts"]),
            total_issued=int(row["total_issued"]), starter_grants=int(row["starter_grants"]),
            transaction_count=int(row["transaction_count"]), settled_orders=int(row["settled_orders"]),
            asset_purchases=int(row["asset_purchases"]), open_escrow=int(row["open_escrow"]),
            cash_reserved=int(row["cash_reserved"]),
            budget_limit=int(row["budget_limit"]) if row["budget_limit"] is not None else None,
            budget_articles=tuple(
                BudgetArticleReport(item["code"], int(item["cap"]), int(item["spent"]), int(item["reserved"]))
                for item in row["budget_articles"]
            ),
        )

    async def latest_enqueued_week(self, guild_id: int) -> date | None:
        return await self._session.scalar(
            select(func.max(WeeklyReportDeliveryRow.week_start)).where(
                WeeklyReportDeliveryRow.guild_id == _guild_id(guild_id).scalar_subquery()
            )
        )

    async def enqueue_report(
        self, guild_id: int, channel_id: int, report: TreasuryReport, *, now: datetime,
    ) -> bool:
        local_guild_id = await self._session.scalar(_guild_id(guild_id))
        if local_guild_id is None:
            return False
        queued = await self._session.scalar(
            insert(WeeklyReportDeliveryRow).values(
                guild_id=local_guild_id, week_start=report.window.week_start, channel_id=Decimal(channel_id),
                payload=report.to_payload(), next_attempt_at=now, created_at=now,
            ).on_conflict_do_nothing(constraint="uq_weekly_report_delivery_week").returning(WeeklyReportDeliveryRow.id)
        )
        return queued is not None

    async def claim_due_report(
        self, guild_id: int, *, now: datetime, lease_until: datetime, lease_token: str,
    ) -> ReportDelivery | None:
        candidate = (
            select(WeeklyReportDeliveryRow.id).where(
                WeeklyReportDeliveryRow.guild_id == _guild_id(guild_id).scalar_subquery(),
                WeeklyReportDeliveryRow.next_attempt_at <= now,
                or_(
                    WeeklyReportDeliveryRow.state == "PENDING",
                    and_(WeeklyReportDeliveryRow.state == "CLAIMED", WeeklyReportDeliveryRow.lease_until <= now),
                ),
            ).order_by(WeeklyReportDeliveryRow.week_start).limit(1).with_for_update(skip_locked=True)
        )
        row = await self._session.scalar(
            update(WeeklyReportDeliveryRow).where(WeeklyReportDeliveryRow.id == candidate.scalar_subquery())
            .values(state="CLAIMED", attempts=WeeklyReportDeliveryRow.attempts + 1,
                    lease_token=lease_token, lease_until=lease_until).returning(WeeklyReportDeliveryRow)
        )
        if row is None:
            return None
        return ReportDelivery(
            guild_id, int(row.channel_id), TreasuryReport.from_payload(row.payload), row.attempts, lease_token,
        )

    async def _complete_report_claim(self, guild_id: int, week_start: date, lease_token: str, **changes) -> bool:
        completed = await self._session.scalar(
            update(WeeklyReportDeliveryRow).where(
                WeeklyReportDeliveryRow.guild_id == _guild_id(guild_id).scalar_subquery(),
                WeeklyReportDeliveryRow.week_start == week_start,
                WeeklyReportDeliveryRow.state == "CLAIMED", WeeklyReportDeliveryRow.lease_token == lease_token,
            ).values(lease_token=None, lease_until=None, **changes).returning(WeeklyReportDeliveryRow.id)
        )
        return completed is not None

    async def mark_report_sent(
        self, guild_id: int, week_start: date, lease_token: str, *, message_id: int, now: datetime,
    ) -> bool:
        return await self._complete_report_claim(
            guild_id, week_start, lease_token, state="SENT", message_id=Decimal(message_id), delivered_at=now,
        )

    async def retry_report(
        self, guild_id: int, week_start: date, lease_token: str, *, next_attempt_at: datetime,
    ) -> bool:
        return await self._complete_report_claim(
            guild_id, week_start, lease_token, state="PENDING", next_attempt_at=next_attempt_at,
        )


class PostgresReportUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresReportRepository]:
        async with self._sessions.begin() as session:
            yield PostgresReportRepository(session)
