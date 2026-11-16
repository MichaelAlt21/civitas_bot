from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class CourtAppealRow(Base):
    __tablename__ = "court_appeals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "case_id"], ["court_act_decisions.guild_id", "court_act_decisions.case_id"],
            name="fk_court_appeals_decision",
        ),
        ForeignKeyConstraint(
            ["guild_id", "appellant_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_appeals_appellant",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    appellant_citizen_id: Mapped[int] = mapped_column(BigInteger)
    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourtAppealOpeningRow(Base):
    __tablename__ = "court_appeal_openings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "case_id"], ["court_appeals.guild_id", "court_appeals.case_id"],
            name="fk_court_appeal_openings_appeal",
        ),
        ForeignKeyConstraint(
            ["guild_id", "opened_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_appeal_openings_actor",
        ),
        UniqueConstraint("guild_id", "source_url", name="uq_court_appeal_openings_source"),
        CheckConstraint(
            "cardinality(eligible_user_ids) > 0 AND quorum > 0 AND length(source_url) BETWEEN 1 AND 500 "
            "AND closes_at = opened_at + interval '48 hours'", name="ck_court_appeal_openings_rules",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opened_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(Text)
    eligible_user_ids: Mapped[list[Decimal]] = mapped_column(ARRAY(Numeric(20, 0)))
    quorum: Mapped[int] = mapped_column(Integer)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourtAppealTallyRow(Base):
    __tablename__ = "court_appeal_tallies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "case_id"], ["court_appeal_openings.guild_id", "court_appeal_openings.case_id"],
            name="fk_court_appeal_tallies_ballot",
        ),
        ForeignKeyConstraint(
            ["guild_id", "recorded_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_appeal_tallies_actor",
        ),
        CheckConstraint(
            "round_number IN (1, 2) AND participants >= 0 AND votes_to_cancel BETWEEN 0 AND participants",
            name="ck_court_appeal_tallies_votes",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    participants: Mapped[int] = mapped_column(Integer)
    votes_to_cancel: Mapped[int] = mapped_column(Integer)
    recorded_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourtAppealConfirmationRow(Base):
    __tablename__ = "court_appeal_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "case_id", "round_number"],
            ["court_appeal_tallies.guild_id", "court_appeal_tallies.case_id", "court_appeal_tallies.round_number"],
            name="fk_court_appeal_confirmations_tally",
        ),
        ForeignKeyConstraint(
            ["guild_id", "confirmed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_appeal_confirmations_actor",
        ),
        CheckConstraint(
            "(outcome = 'EXTENDED' AND round_number = 1 AND extended_until IS NOT NULL "
            "AND extended_until = confirmed_at + interval '24 hours') OR "
            "(outcome IN ('OVERTURNED', 'UPHELD', 'NO_QUORUM') AND extended_until IS NULL)",
            name="ck_court_appeal_confirmations_outcome",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    confirmed_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))
    extended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
