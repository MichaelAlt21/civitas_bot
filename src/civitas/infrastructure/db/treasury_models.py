from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Identity,
    Index, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ContractBudgetApprovalRow(Base):
    __tablename__ = "contract_budget_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "period_id"], ["budget_periods.guild_id", "budget_periods.id"],
            name="fk_contract_budget_approval_period",
        ),
        ForeignKeyConstraint(
            ["guild_id", "activated_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_contract_budget_approval_actor",
        ),
    )

    period_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    activated_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activation_reason: Mapped[str] = mapped_column(Text)


class StateContractRow(Base):
    __tablename__ = "state_contracts"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_state_contracts_request"),
        UniqueConstraint("payout_transaction_id", name="uq_state_contracts_payout"),
        ForeignKeyConstraint(
            ["guild_id", "recipient_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_state_contracts_recipient",
        ),
        ForeignKeyConstraint(
            ["guild_id", "recipient_company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_state_contracts_company",
        ),
        ForeignKeyConstraint(
            ["guild_id", "proposer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_state_contracts_proposer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "budget_period_id"], ["budget_periods.guild_id", "budget_periods.id"],
            name="fk_state_contracts_period",
        ),
        ForeignKeyConstraint(
            ["guild_id", "reserved_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_state_contracts_reserver",
        ),
        ForeignKeyConstraint(
            ["guild_id", "closed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_state_contracts_closer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "payout_transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_state_contracts_payout",
        ),
        CheckConstraint("amount > 0 AND deadline > created_at", name="ck_state_contracts_terms"),
        CheckConstraint(
            "status IN ('PROPOSED', 'RESERVED', 'SUBMITTED', 'PAID', 'CANCELLED')",
            name="ck_state_contracts_status",
        ),
        CheckConstraint(
            "(budget_period_id IS NULL AND reserved_by_citizen_id IS NULL "
            "AND reserved_at IS NULL AND reserve_reason IS NULL "
            "AND status IN ('PROPOSED', 'CANCELLED')) OR "
            "(budget_period_id IS NOT NULL AND reserved_by_citizen_id IS NOT NULL "
            "AND reserved_at IS NOT NULL AND reserve_reason IS NOT NULL AND status <> 'PROPOSED')",
            name="ck_state_contracts_reservation",
        ),
        CheckConstraint(
            "(evidence IS NULL AND submitted_at IS NULL AND status NOT IN ('SUBMITTED', 'PAID')) OR "
            "(evidence IS NOT NULL AND submitted_at IS NOT NULL AND reserved_at IS NOT NULL "
            "AND submitted_at >= reserved_at AND status IN ('SUBMITTED', 'PAID', 'CANCELLED'))",
            name="ck_state_contracts_submission",
        ),
        CheckConstraint(
            "(status IN ('PAID', 'CANCELLED') AND closed_at IS NOT NULL "
            "AND closed_by_citizen_id IS NOT NULL AND close_reason IS NOT NULL) OR "
            "(status NOT IN ('PAID', 'CANCELLED') AND closed_at IS NULL "
            "AND closed_by_citizen_id IS NULL AND close_reason IS NULL)",
            name="ck_state_contracts_closure",
        ),
        CheckConstraint(
            "(status = 'PAID') = (payout_transaction_id IS NOT NULL)",
            name="ck_state_contracts_paid",
        ),
        Index("ix_state_contracts_budget_status", "budget_period_id", "status"),
        Index("ix_state_contracts_recipient", "guild_id", "recipient_citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    title: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(BigInteger)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recipient_citizen_id: Mapped[int] = mapped_column(BigInteger)
    recipient_company_id: Mapped[int | None] = mapped_column(BigInteger)
    proposer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    proposal_reason: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    budget_period_id: Mapped[int | None] = mapped_column(BigInteger)
    reserved_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reserve_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)
    payout_transaction_id: Mapped[int | None] = mapped_column(BigInteger)


class TreasuryAuditEventRow(Base):
    __tablename__ = "treasury_audit_events"
    __table_args__ = (
        CheckConstraint(
            "discord_guild_id BETWEEN 1 AND 18446744073709551615 "
            "AND actor_discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_treasury_audit_discord_ids",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    discord_guild_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    actor_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    action: Mapped[str] = mapped_column(String(32))
    contract_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
