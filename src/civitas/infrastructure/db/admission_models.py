from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class AdmissionRosterRow(Base):
    __tablename__ = "admission_rosters"
    __table_args__ = (
        CheckConstraint(
            "discord_guild_id BETWEEN 1 AND 18446744073709551615",
            name="ck_admission_rosters_discord_guild_id",
        ),
    )

    discord_guild_id: Mapped[Decimal] = mapped_column(Numeric(20, 0), primary_key=True, autoincrement=False)


class AdmittedUserRow(Base):
    __tablename__ = "admitted_users"
    __table_args__ = (
        CheckConstraint(
            "discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_admitted_users_discord_user_id",
        ),
    )

    discord_guild_id: Mapped[Decimal] = mapped_column(
        Numeric(20, 0), ForeignKey("admission_rosters.discord_guild_id"), primary_key=True,
    )
    discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0), primary_key=True)


class AdmissionAuditRow(Base):
    __tablename__ = "admission_audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_admission_audit_actor",
        ),
        CheckConstraint(
            "target_discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_admission_audit_target",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_admission_audit_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    discord_guild_id: Mapped[Decimal] = mapped_column(
        Numeric(20, 0), ForeignKey("admission_rosters.discord_guild_id"),
    )
    actor_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    target_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    allowed: Mapped[bool]
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
