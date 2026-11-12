from datetime import date, datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Identity, Index, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class BudgetPeriodRow(Base):
    __tablename__ = "budget_periods"
    __table_args__ = (
        UniqueConstraint("guild_id", "start_at", name="uq_budget_periods_guild_start"),
        UniqueConstraint("guild_id", "week_start_date", name="uq_budget_periods_guild_week"),
        UniqueConstraint("guild_id", "id", name="uq_budget_periods_guild_id"),
        CheckConstraint("start_at < end_at", name="ck_budget_periods_interval"),
        CheckConstraint(
            "EXTRACT(ISODOW FROM week_start_date) = 1", name="ck_budget_periods_week_monday"
        ),
        CheckConstraint(
            "overall_cap >= 0 AND spent_total BETWEEN 0 AND overall_cap "
            "AND reserved_total BETWEEN 0 AND overall_cap - spent_total",
            name="ck_budget_periods_cap",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    week_start_date: Mapped[date] = mapped_column(Date)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    overall_cap: Mapped[int] = mapped_column(BigInteger)
    spent_total: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    reserved_total: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class BudgetArticleRow(Base):
    __tablename__ = "budget_articles"
    __table_args__ = (
        UniqueConstraint("period_id", "code", name="uq_budget_articles_period_code"),
        CheckConstraint(
            "personal_cap >= 0 AND spent BETWEEN 0 AND cap AND reserved BETWEEN 0 AND cap - spent",
            name="ck_budget_articles_cap",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    period_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("budget_periods.id"))
    code: Mapped[str] = mapped_column(String(32))
    cap: Mapped[int] = mapped_column(BigInteger)
    personal_cap: Mapped[int] = mapped_column(BigInteger)
    spent: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    reserved: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class WorkTaskRow(Base):
    __tablename__ = "work_tasks"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_work_tasks_guild_id"),
        ForeignKeyConstraint(
            ["guild_id", "published_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_tasks_publisher",
        ),
        CheckConstraint("reward > 0", name="ck_work_tasks_reward"),
        CheckConstraint("created_at < expires_at", name="ck_work_tasks_interval"),
        CheckConstraint(
            "category IN ('industry', 'logistics', 'services', 'events')",
            name="ck_work_tasks_category",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    category: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    requirements: Mapped[str] = mapped_column(Text)
    reward: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_by_citizen_id: Mapped[int] = mapped_column(BigInteger)


class WorkClaimRow(Base):
    __tablename__ = "work_claims"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_work_claims_task"),
        UniqueConstraint("payout_transaction_id", name="uq_work_claims_payout"),
        ForeignKeyConstraint(
            ["guild_id", "task_id"], ["work_tasks.guild_id", "work_tasks.id"],
            name="fk_work_claims_task",
        ),
        ForeignKeyConstraint(
            ["guild_id", "citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_claims_citizen",
        ),
        ForeignKeyConstraint(
            ["guild_id", "reviewer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_claims_reviewer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "budget_period_id"], ["budget_periods.guild_id", "budget_periods.id"],
            name="fk_work_claims_budget_period",
        ),
        ForeignKeyConstraint(
            ["guild_id", "payout_transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_work_claims_payout",
        ),
        CheckConstraint(
            "status IN ('CLAIMED', 'SUBMITTED', 'PAID', 'REJECTED')",
            name="ck_work_claims_status",
        ),
        CheckConstraint(
            "(status = 'CLAIMED' AND evidence IS NULL AND submitted_at IS NULL) OR "
            "(status <> 'CLAIMED' AND evidence IS NOT NULL AND submitted_at >= claimed_at "
            "AND submitted_at IS NOT NULL)",
            name="ck_work_claims_submission",
        ),
        CheckConstraint(
            "(status IN ('PAID', 'REJECTED') AND reviewed_at IS NOT NULL "
            "AND reviewed_at >= submitted_at AND reviewer_citizen_id IS NOT NULL "
            "AND reviewer_citizen_id <> citizen_id) OR "
            "(status IN ('CLAIMED', 'SUBMITTED') AND reviewed_at IS NULL "
            "AND reviewer_citizen_id IS NULL AND review_reason IS NULL)",
            name="ck_work_claims_review",
        ),
        CheckConstraint(
            "(status = 'PAID' AND budget_period_id IS NOT NULL AND payout_transaction_id IS NOT NULL) OR "
            "(status <> 'PAID' AND budget_period_id IS NULL AND payout_transaction_id IS NULL)",
            name="ck_work_claims_paid",
        ),
        Index("ix_work_claims_citizen_claimed", "guild_id", "citizen_id", "claimed_at"),
        Index("ix_work_claims_budget_citizen", "budget_period_id", "citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[int] = mapped_column(BigInteger)
    citizen_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    review_reason: Mapped[str | None] = mapped_column(Text)
    payout_transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    budget_period_id: Mapped[int | None] = mapped_column(BigInteger)
