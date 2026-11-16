from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ProsecutorMotionRow(Base):
    __tablename__ = "prosecutor_motions"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_prosecutor_motions_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_prosecutor_motions_request"),
        ForeignKeyConstraint(
            ["guild_id", "president_term_id"], ["public_offices.guild_id", "public_offices.id"],
            name="fk_prosecutor_motions_president_term",
        ),
        ForeignKeyConstraint(
            ["guild_id", "proposer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_prosecutor_motions_proposer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "subject_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_prosecutor_motions_subject",
        ),
        ForeignKeyConstraint(
            ["guild_id", "appointment_id"], ["prosecutor_motions.guild_id", "prosecutor_motions.id"],
            name="fk_prosecutor_motions_appointment",
        ),
        CheckConstraint(
            "(action = 'APPOINT' AND appointment_id IS NULL) OR "
            "(action = 'REMOVE' AND appointment_id IS NOT NULL AND appointment_id <> id)",
            name="ck_prosecutor_motions_action",
        ),
        CheckConstraint(
            "length(btrim(request_key)) > 0 AND length(btrim(reason)) > 0",
            name="ck_prosecutor_motions_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)
    president_term_id: Mapped[int] = mapped_column(BigInteger)
    proposer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    subject_citizen_id: Mapped[int] = mapped_column(BigInteger)
    appointment_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))


class ProsecutorApprovalRow(Base):
    __tablename__ = "prosecutor_approvals"
    __table_args__ = (
        UniqueConstraint("guild_id", "motion_id", "senator_citizen_id", name="uq_prosecutor_approvals_signer"),
        ForeignKeyConstraint(
            ["guild_id", "motion_id"], ["prosecutor_motions.guild_id", "prosecutor_motions.id"],
            name="fk_prosecutor_approvals_motion",
        ),
        ForeignKeyConstraint(
            ["guild_id", "senator_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_prosecutor_approvals_senator",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_prosecutor_approvals_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    motion_id: Mapped[int] = mapped_column(BigInteger)
    senator_citizen_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProsecutorResolutionRow(Base):
    __tablename__ = "prosecutor_resolutions"
    __table_args__ = (
        UniqueConstraint("guild_id", "motion_id", name="uq_prosecutor_resolutions_motion"),
        ForeignKeyConstraint(
            ["guild_id", "motion_id"], ["prosecutor_motions.guild_id", "prosecutor_motions.id"],
            name="fk_prosecutor_resolutions_motion",
        ),
        Index("ix_prosecutor_resolutions_history", "guild_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    motion_id: Mapped[int] = mapped_column(BigInteger)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
