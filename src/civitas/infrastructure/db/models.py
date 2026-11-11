from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GuildRow(Base):
    __tablename__ = "guilds"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", name="uq_guilds_discord_guild_id"),
        CheckConstraint(
            "discord_guild_id BETWEEN 1 AND 18446744073709551615",
            name="ck_guilds_discord_guild_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    discord_guild_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CitizenRow(Base):
    __tablename__ = "citizens"
    __table_args__ = (
        UniqueConstraint("guild_id", "discord_user_id", name="uq_citizens_guild_user"),
        UniqueConstraint("guild_id", "id", name="uq_citizens_guild_id"),
        CheckConstraint(
            "discord_user_id BETWEEN 1 AND 18446744073709551615",
            name="ck_citizens_discord_user_id",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'SUSPENDED', 'LEFT')",
            name="ck_citizens_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    status: Mapped[str] = mapped_column(String(16))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    actor_discord_user_id: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    action: Mapped[str] = mapped_column(String(64))
    citizen_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("citizens.id"))
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(32))
