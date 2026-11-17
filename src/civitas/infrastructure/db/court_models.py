from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, Index, String, Text, UniqueConstraint, text,
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


class CourtRemovalProposalRow(Base):
    __tablename__ = "court_removal_proposals"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_court_removals_request"),
        ForeignKeyConstraint(
            ["guild_id", "nomination_id"], ["court_appointments.guild_id", "court_appointments.nomination_id"],
            name="fk_court_removals_appointment",
        ),
        ForeignKeyConstraint(
            ["guild_id", "president_term_id"], ["public_offices.guild_id", "public_offices.id"],
            name="fk_court_removals_president_term",
        ),
        ForeignKeyConstraint(["guild_id", "proposer_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_court_removals_proposer"),
        ForeignKeyConstraint(["guild_id", "first_senator_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_court_removals_first"),
        ForeignKeyConstraint(["guild_id", "second_senator_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_court_removals_second"),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 1000 AND char_length(btrim(request_key)) > 0",
            name="ck_court_removals_content",
        ),
        CheckConstraint(
            "(first_senator_id IS NULL) = (first_signed_at IS NULL) "
            "AND (second_senator_id IS NULL) = (second_signed_at IS NULL) "
            "AND (first_signed_at IS NULL OR first_signed_at >= proposed_at) "
            "AND (second_signed_at IS NULL OR (first_signed_at IS NOT NULL AND second_signed_at >= first_signed_at)) "
            "AND first_senator_id <> proposer_id AND second_senator_id <> proposer_id "
            "AND first_senator_id <> second_senator_id",
            name="ck_court_removals_signatures",
        ),
        Index(
            "uq_court_removals_confirmed", "guild_id", "nomination_id",
            unique=True, postgresql_where=text("second_signed_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    nomination_id: Mapped[int] = mapped_column(BigInteger)
    president_term_id: Mapped[int] = mapped_column(BigInteger)
    proposer_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_senator_id: Mapped[int | None] = mapped_column(BigInteger)
    first_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    second_senator_id: Mapped[int | None] = mapped_column(BigInteger)
    second_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
