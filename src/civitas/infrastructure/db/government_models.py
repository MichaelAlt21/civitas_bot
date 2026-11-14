from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Identity, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

_ACTIVE_ELECTION = "status IN ('OPEN', 'EXTENDED', 'PENDING_CERTIFICATION')"


class ElectionRow(Base):
    __tablename__ = "elections"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_elections_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_elections_request"),
        UniqueConstraint("runoff_of_id", name="uq_elections_runoff"),
        ForeignKeyConstraint(
            ["guild_id", "opened_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_elections_opener",
        ),
        ForeignKeyConstraint(
            ["guild_id", "runoff_of_id"], ["elections.guild_id", "elections.id"],
            name="fk_elections_runoff",
        ),
        ForeignKeyConstraint(
            ["guild_id", "retry_of_id"], ["elections.guild_id", "elections.id"],
            name="fk_elections_retry",
        ),
        CheckConstraint(
            "retry_of_id IS NULL OR runoff_of_id IS NULL OR retry_of_id = runoff_of_id",
            name="ck_elections_retry_link",
        ),
        CheckConstraint("office IN ('PRESIDENT', 'PARLIAMENT')", name="ck_elections_office"),
        CheckConstraint(
            "seats > 0 AND term_days > 0 AND quorum > 0 AND extension_hours > 0 "
            "AND opens_at < closes_at AND (extended_until IS NULL OR extended_until > closes_at)",
            name="ck_elections_rules",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'EXTENDED', 'NO_QUORUM', 'TIED', 'PENDING_CERTIFICATION', 'APPLIED')",
            name="ck_elections_status",
        ),
        Index(
            "uq_elections_active_office", "guild_id", "office",
            unique=True, postgresql_where=text(_ACTIVE_ELECTION),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    office: Mapped[str] = mapped_column(String(16))
    seats: Mapped[int] = mapped_column(Integer)
    term_days: Mapped[int] = mapped_column(Integer)
    quorum: Mapped[int] = mapped_column(Integer)
    extension_hours: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opened_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128))
    eligible_citizen_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    candidate_citizen_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    candidate_discord_user_ids: Mapped[list[int]] = mapped_column(ARRAY(Numeric(20, 0)))
    status: Mapped[str] = mapped_column(String(32))
    extended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runoff_of_id: Mapped[int | None] = mapped_column(BigInteger)
    retry_of_id: Mapped[int | None] = mapped_column(BigInteger)


Index("uq_elections_retry", func.coalesce(ElectionRow.retry_of_id, ElectionRow.runoff_of_id), unique=True)


class ElectionProtocolRow(Base):
    __tablename__ = "election_protocols"
    __table_args__ = (
        UniqueConstraint("election_id", "round_number", name="uq_election_protocols_round"),
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["elections.guild_id", "elections.id"],
            name="fk_election_protocols_election",
        ),
        ForeignKeyConstraint(
            ["guild_id", "recorded_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_election_protocols_recorder",
        ),
        CheckConstraint(
            "round_number IN (1, 2) AND participants >= 0", name="ck_election_protocols_counts"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    election_id: Mapped[int] = mapped_column(BigInteger)
    round_number: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    participants: Mapped[int] = mapped_column(Integer)
    tallies: Mapped[list[list[int]]] = mapped_column(JSONB)
    winners: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    recorded_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)


class ElectionPollSourceRow(Base):
    __tablename__ = "election_poll_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["elections.guild_id", "elections.id"],
            name="fk_election_poll_sources_election",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, primary_key=True)
    election_id: Mapped[int] = mapped_column(BigInteger)


class ElectionCertificationRow(Base):
    __tablename__ = "election_certifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["elections.guild_id", "elections.id"],
            name="fk_election_certifications_election",
        ),
        ForeignKeyConstraint(
            ["guild_id", "certified_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_election_certifications_reviewer",
        ),
    )

    election_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    certified_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    certified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)


class ElectionCrisisProposalRow(Base):
    __tablename__ = "election_crisis_proposals"
    __table_args__ = (
        UniqueConstraint("guild_id", "election_id", name="uq_crisis_proposals_guild_election"),
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["elections.guild_id", "elections.id"],
            name="fk_crisis_proposals_election",
        ),
        ForeignKeyConstraint(
            ["guild_id", "actor_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_crisis_proposals_actor",
        ),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 200", name="ck_crisis_proposals_reason"),
    )

    election_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    actor_citizen_id: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)


class ElectionCrisisConfirmationRow(Base):
    __tablename__ = "election_crisis_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["election_crisis_proposals.guild_id", "election_crisis_proposals.election_id"],
            name="fk_crisis_confirmations_proposal",
        ),
        ForeignKeyConstraint(
            ["guild_id", "actor_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_crisis_confirmations_actor",
        ),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 200", name="ck_crisis_confirmations_reason"),
    )

    election_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    actor_citizen_id: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)


class PublicOfficeRow(Base):
    __tablename__ = "public_offices"
    __table_args__ = (
        UniqueConstraint("election_id", "seat", name="uq_public_offices_election_seat"),
        UniqueConstraint("election_id", "holder_citizen_id", name="uq_public_offices_election_holder"),
        ForeignKeyConstraint(
            ["guild_id", "election_id"], ["elections.guild_id", "elections.id"],
            name="fk_public_offices_election",
        ),
        ForeignKeyConstraint(
            ["guild_id", "holder_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_public_offices_holder",
        ),
        CheckConstraint("office IN ('PRESIDENT', 'PARLIAMENT')", name="ck_public_offices_office"),
        CheckConstraint("seat > 0 AND starts_at < ends_at", name="ck_public_offices_term"),
        Index("ix_public_offices_guild_interval", "guild_id", "office", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    election_id: Mapped[int] = mapped_column(BigInteger)
    office: Mapped[str] = mapped_column(String(16))
    seat: Mapped[int] = mapped_column(Integer)
    holder_citizen_id: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
