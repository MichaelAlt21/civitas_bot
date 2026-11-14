from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, Index,
    Integer, Numeric, String, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ReceiptPreferenceRow(Base):
    __tablename__ = "receipt_preferences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_receipt_preferences_citizen",
        ),
        CheckConstraint("locale IN ('ru', 'en')", name="ck_receipt_preferences_locale"),
    )

    citizen_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    locale: Mapped[str] = mapped_column(String(2))


class PrivateReceiptRow(Base):
    __tablename__ = "private_receipts"
    __table_args__ = (
        UniqueConstraint("transaction_id", "account_id", name="uq_private_receipts_posting"),
        ForeignKeyConstraint(
            ["guild_id", "citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_private_receipts_citizen",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "account_id"], ["ledger_entries.transaction_id", "ledger_entries.account_id"],
            name="fk_private_receipts_entry",
        ),
        CheckConstraint("locale IN ('ru', 'en')", name="ck_private_receipts_locale"),
        CheckConstraint("attempts >= 0", name="ck_private_receipts_attempts"),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_until IS NULL)",
            name="ck_private_receipts_lease",
        ),
        CheckConstraint(
            "((sent_at IS NULL AND discord_message_id IS NULL) OR "
            "(sent_at IS NOT NULL AND discord_message_id IS NOT NULL "
            "AND discord_message_id BETWEEN 1 AND 18446744073709551615)) "
            "AND (sent_at IS NULL OR suppressed_at IS NULL) "
            "AND ((sent_at IS NULL AND suppressed_at IS NULL) OR lease_token IS NULL)",
            name="ck_private_receipts_delivery",
        ),
        Index(
            "ix_private_receipts_due", "guild_id", "next_attempt_at", "id",
            postgresql_where=text("sent_at IS NULL AND suppressed_at IS NULL"),
        ),
        Index("ix_private_receipts_citizen", "guild_id", "citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    citizen_id: Mapped[int] = mapped_column(BigInteger)
    transaction_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[int] = mapped_column(BigInteger)
    locale: Mapped[str] = mapped_column(String(2))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discord_message_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
