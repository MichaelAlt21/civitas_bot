"""Create guilds, citizens and registration audit.

Revision ID: 0001_citizenship
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_citizenship"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guilds",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("discord_guild_id", sa.Numeric(20, 0), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("discord_guild_id", name="uq_guilds_discord_guild_id"),
        sa.CheckConstraint(
            "discord_guild_id BETWEEN 1 AND 18446744073709551615",
            name="ck_guilds_discord_guild_id",
        ),
    )
    op.create_table(
        "citizens",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("discord_user_id", sa.Numeric(20, 0), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("guild_id", "discord_user_id", name="uq_citizens_guild_user"),
        sa.CheckConstraint(
            "discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_citizens_discord_user_id",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'SUSPENDED', 'LEFT')",
            name="ck_citizens_status",
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("actor_discord_user_id", sa.Numeric(20, 0), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column(
            "citizen_id", sa.BigInteger(), sa.ForeignKey("citizens.id"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("citizens")
    op.drop_table("guilds")
