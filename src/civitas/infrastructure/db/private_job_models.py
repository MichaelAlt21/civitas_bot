from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Identity,
    Index, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class PrivateJobRow(Base):
    __tablename__ = "private_jobs"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_private_jobs_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_private_jobs_request"),
        UniqueConstraint("payout_transaction_id", name="uq_private_jobs_payout"),
        ForeignKeyConstraint(
            ["guild_id", "company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_private_jobs_company",
        ),
        ForeignKeyConstraint(
            ["guild_id", "owner_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_private_jobs_owner",
        ),
        ForeignKeyConstraint(
            ["guild_id", "worker_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_private_jobs_worker",
        ),
        ForeignKeyConstraint(
            ["guild_id", "disputed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_private_jobs_dispute_actor",
        ),
        ForeignKeyConstraint(
            ["guild_id", "closed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_private_jobs_close_actor",
        ),
        ForeignKeyConstraint(
            ["guild_id", "payout_transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_private_jobs_payout",
        ),
        CheckConstraint(
            "amount > 0 AND deadline > created_at AND length(request_key) > 0 "
            "AND length(publication_reason) > 0",
            name="ck_private_jobs_terms",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'CLAIMED', 'SUBMITTED', 'DISPUTED', 'PAID', 'CANCELLED')",
            name="ck_private_jobs_status",
        ),
        CheckConstraint(
            "(worker_citizen_id IS NULL AND claimed_at IS NULL AND status IN ('OPEN', 'CANCELLED')) OR "
            "(worker_citizen_id IS NOT NULL AND worker_citizen_id <> owner_citizen_id "
            "AND claimed_at IS NOT NULL AND claimed_at >= created_at AND claimed_at < deadline AND status <> 'OPEN')",
            name="ck_private_jobs_claim",
        ),
        CheckConstraint(
            "(evidence IS NULL AND submitted_at IS NULL AND status IN ('OPEN', 'CLAIMED', 'CANCELLED')) OR "
            "(evidence IS NOT NULL AND submitted_at IS NOT NULL AND claimed_at IS NOT NULL "
            "AND submitted_at >= claimed_at AND submitted_at < deadline AND status IN ('SUBMITTED', 'DISPUTED', 'PAID', 'CANCELLED'))",
            name="ck_private_jobs_submission",
        ),
        CheckConstraint(
            "(dispute_reason IS NULL AND disputed_at IS NULL AND disputed_by_citizen_id IS NULL AND status <> 'DISPUTED') OR "
            "(dispute_reason IS NOT NULL AND disputed_at IS NOT NULL AND disputed_by_citizen_id IS NOT NULL "
            "AND submitted_at IS NOT NULL AND disputed_at >= submitted_at AND status IN ('DISPUTED', 'PAID', 'CANCELLED'))",
            name="ck_private_jobs_dispute",
        ),
        CheckConstraint(
            "(status IN ('PAID', 'CANCELLED') AND close_reason IS NOT NULL AND closed_at IS NOT NULL "
            "AND closed_at >= coalesce(disputed_at, submitted_at, claimed_at, created_at) "
            "AND closed_by_citizen_id IS NOT NULL AND payout_transaction_id IS NOT NULL) OR "
            "(status NOT IN ('PAID', 'CANCELLED') AND close_reason IS NULL AND closed_at IS NULL "
            "AND closed_by_citizen_id IS NULL AND payout_transaction_id IS NULL)",
            name="ck_private_jobs_closure",
        ),
        Index("ix_private_jobs_catalog", "guild_id", "status", "id"),
        Index("ix_private_jobs_owner", "guild_id", "owner_citizen_id"),
        Index("ix_private_jobs_worker", "guild_id", "worker_citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    company_id: Mapped[int] = mapped_column(BigInteger)
    owner_citizen_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(BigInteger)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
    publication_reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    worker_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str | None] = mapped_column(Text)
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disputed_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    dispute_reason: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    close_reason: Mapped[str | None] = mapped_column(Text)
    payout_transaction_id: Mapped[int | None] = mapped_column(BigInteger)
