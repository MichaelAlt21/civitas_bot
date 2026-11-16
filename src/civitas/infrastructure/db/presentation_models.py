from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class GuildPresentationRow(Base):
    __tablename__ = "guild_presentations"
    __table_args__ = (
        CheckConstraint(
            "discord_guild_id BETWEEN 1 AND 18446744073709551615",
            name="ck_guild_presentations_discord_guild_id",
        ),
        CheckConstraint(
            "citizen_role_id BETWEEN 1 AND 18446744073709551615 AND citizen_role_id <> discord_guild_id",
            name="ck_guild_presentations_citizen_role",
        ),
        CheckConstraint(
            "president_role_id BETWEEN 1 AND 18446744073709551615 AND president_role_id <> discord_guild_id",
            name="ck_guild_presentations_president_role",
        ),
        CheckConstraint(
            "parliament_role_id BETWEEN 1 AND 18446744073709551615 AND parliament_role_id <> discord_guild_id",
            name="ck_guild_presentations_parliament_role",
        ),
        CheckConstraint(
            "report_channel_id BETWEEN 1 AND 18446744073709551615",
            name="ck_guild_presentations_report_channel",
        ),
        CheckConstraint(
            "citizen_role_id <> president_role_id AND citizen_role_id <> parliament_role_id "
            "AND president_role_id <> parliament_role_id",
            name="ck_guild_presentations_distinct_roles",
        ),
    )

    discord_guild_id: Mapped[Decimal] = mapped_column(Numeric(20, 0), primary_key=True, autoincrement=False)
    citizen_role_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    president_role_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    parliament_role_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    report_channel_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))


class PresentationAuditRow(Base):
    __tablename__ = "presentation_audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_presentation_audit_actor",
        ),
        CheckConstraint(
            "binding IN ('citizen_role_id', 'president_role_id', 'parliament_role_id', 'report_channel_id')",
            name="ck_presentation_audit_binding",
        ),
        CheckConstraint(
            "previous_discord_id BETWEEN 1 AND 18446744073709551615",
            name="ck_presentation_audit_previous",
        ),
        CheckConstraint(
            "new_discord_id BETWEEN 1 AND 18446744073709551615",
            name="ck_presentation_audit_new",
        ),
        CheckConstraint(
            "previous_discord_id IS DISTINCT FROM new_discord_id",
            name="ck_presentation_audit_change",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    discord_guild_id: Mapped[Decimal] = mapped_column(
        Numeric(20, 0), ForeignKey("guild_presentations.discord_guild_id"),
    )
    actor_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    binding: Mapped[str] = mapped_column(String(24))
    previous_discord_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    new_discord_id: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
