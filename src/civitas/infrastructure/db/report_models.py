from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Identity, Index,
    Numeric, String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class WeeklyReportDeliveryRow(Base):
    __tablename__ = "weekly_report_deliveries"
    __table_args__ = (
        UniqueConstraint("guild_id", "week_start", name="uq_weekly_report_delivery_week"),
        CheckConstraint(
            "EXTRACT(ISODOW FROM week_start) = 1 AND jsonb_typeof(payload) = 'object'",
            name="ck_weekly_report_delivery_report",
        ),
        CheckConstraint(
            "channel_id BETWEEN 1 AND 18446744073709551615 "
            "AND (message_id IS NULL OR message_id BETWEEN 1 AND 18446744073709551615)",
            name="ck_weekly_report_delivery_discord_ids",
        ),
        CheckConstraint(
            "attempts >= 0 AND (state = 'PENDING' AND lease_token IS NULL AND lease_until IS NULL "
            "AND message_id IS NULL AND delivered_at IS NULL OR "
            "state = 'CLAIMED' AND attempts > 0 AND length(lease_token) > 0 AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL AND message_id IS NULL AND delivered_at IS NULL OR "
            "state = 'SENT' AND attempts > 0 AND lease_token IS NULL AND lease_until IS NULL "
            "AND message_id IS NOT NULL AND delivered_at IS NOT NULL)",
            name="ck_weekly_report_delivery_state",
        ),
        Index("ix_weekly_report_delivery_due", "guild_id", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    week_start: Mapped[date] = mapped_column(Date)
    channel_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(8), server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
