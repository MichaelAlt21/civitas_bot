from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class CourtNominationRow(Base):
    __tablename__ = "court_nominations"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_court_nominations_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_court_nominations_request"),
        ForeignKeyConstraint(
            ["guild_id", "president_term_id"], ["public_offices.guild_id", "public_offices.id"],
            name="fk_court_nominations_president_term",
        ),
        ForeignKeyConstraint(
            ["guild_id", "proposer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_nominations_proposer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "candidate_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_nominations_candidate",
        ),
        CheckConstraint(
            "length(btrim(request_key)) > 0 AND length(btrim(reason)) > 0",
            name="ck_court_nominations_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    president_term_id: Mapped[int] = mapped_column(BigInteger)
    proposer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    candidate_citizen_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))


class CourtApprovalRow(Base):
    __tablename__ = "court_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "nomination_id"], ["court_nominations.guild_id", "court_nominations.id"],
            name="fk_court_approvals_nomination",
        ),
        ForeignKeyConstraint(
            ["guild_id", "deputy_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_approvals_deputy",
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_court_approvals_reason"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nomination_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deputy_citizen_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourtAppointmentRow(Base):
    __tablename__ = "court_appointments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "nomination_id"], ["court_nominations.guild_id", "court_nominations.id"],
            name="fk_court_appointments_nomination",
        ),
        CheckConstraint("starts_at < ends_at", name="ck_court_appointments_term"),
        Index("ix_court_appointments_guild_interval", "guild_id", "starts_at", "ends_at"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nomination_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
