from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class EconomyMaintenanceRow(Base):
    __tablename__ = "economy_maintenance"

    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.id"), primary_key=True, autoincrement=False,
    )
    read_only: Mapped[bool] = mapped_column(Boolean)


class EconomyMaintenanceAuditRow(Base):
    __tablename__ = "economy_maintenance_audit"
    __table_args__ = (
        CheckConstraint(
            "actor_discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_economy_maintenance_audit_actor",
        ),
        CheckConstraint(
            "previous_read_only <> read_only AND length(btrim(reason)) > 0",
            name="ck_economy_maintenance_audit_change",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    actor_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    previous_read_only: Mapped[bool] = mapped_column(Boolean)
    read_only: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
