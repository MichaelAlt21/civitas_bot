"""Persist weekly public reports and retryable Discord delivery state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_weekly_reports"
down_revision = "0006_treasury"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_report_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("channel_id", sa.Numeric(20, 0), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("state", sa.String(8), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("message_id", sa.Numeric(20, 0)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("guild_id", "week_start", name="uq_weekly_report_delivery_week"),
        sa.CheckConstraint(
            "EXTRACT(ISODOW FROM week_start) = 1 AND jsonb_typeof(payload) = 'object'",
            name="ck_weekly_report_delivery_report",
        ),
        sa.CheckConstraint(
            "channel_id BETWEEN 1 AND 18446744073709551615 "
            "AND (message_id IS NULL OR message_id BETWEEN 1 AND 18446744073709551615)",
            name="ck_weekly_report_delivery_discord_ids",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND (state = 'PENDING' AND lease_token IS NULL AND lease_until IS NULL "
            "AND message_id IS NULL AND delivered_at IS NULL OR "
            "state = 'CLAIMED' AND attempts > 0 AND length(lease_token) > 0 AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL AND message_id IS NULL AND delivered_at IS NULL OR "
            "state = 'SENT' AND attempts > 0 AND lease_token IS NULL AND lease_until IS NULL "
            "AND message_id IS NOT NULL AND delivered_at IS NOT NULL)",
            name="ck_weekly_report_delivery_state",
        ),
    )
    op.create_index("ix_weekly_report_delivery_due", "weekly_report_deliveries", ["guild_id", "state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("weekly_report_deliveries")
