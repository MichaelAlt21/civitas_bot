from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, Index, String, Text, UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class CourtCaseRow(Base):
    __tablename__ = "court_cases"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", "filed_at", name="uq_court_cases_guild_filing"),
        UniqueConstraint("guild_id", "id", "kind", "law_revision_id", name="uq_court_cases_administrative_target"),
        UniqueConstraint("guild_id", "request_key", name="uq_court_cases_request"),
        ForeignKeyConstraint(
            ["guild_id", "filer_citizen_id"], ["citizens.guild_id", "citizens.id"], name="fk_court_cases_filer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "respondent_citizen_id"], ["citizens.guild_id", "citizens.id"], name="fk_court_cases_respondent",
        ),
        ForeignKeyConstraint(
            ["guild_id", "law_revision_id"], ["law_confirmations.guild_id", "law_confirmations.revision_id"],
            name="fk_court_cases_confirmed_law",
        ),
        CheckConstraint(
            "char_length(statement) <= 1800 AND char_length(btrim(statement)) > 0 "
            "AND char_length(btrim(request_key)) > 0",
            name="ck_court_cases_content",
        ),
        CheckConstraint("filer_citizen_id <> respondent_citizen_id", name="ck_court_cases_distinct_parties"),
        CheckConstraint(
            "(kind = 'ADMINISTRATIVE' AND against_state AND respondent_citizen_id IS NULL "
            "AND law_revision_id IS NOT NULL AND occurred_at IS NULL) "
            "OR (kind = 'CRIMINAL' AND for_state AND NOT against_state AND respondent_citizen_id IS NOT NULL "
            "AND law_revision_id IS NOT NULL AND occurred_at IS NOT NULL AND occurred_at <= filed_at) "
            "OR (kind = 'CIVIL' AND NOT (for_state AND against_state) "
            "AND against_state = (respondent_citizen_id IS NULL) AND law_revision_id IS NULL AND occurred_at IS NULL)",
            name="ck_court_cases_parties",
        ),
        Index("ix_court_cases_guild_id", "guild_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20))
    filer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    respondent_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    for_state: Mapped[bool] = mapped_column(Boolean)
    against_state: Mapped[bool] = mapped_column(Boolean)
    law_revision_id: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    statement: Mapped[str] = mapped_column(Text)
    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))


class CourtHearingRow(Base):
    __tablename__ = "court_hearings"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_court_hearings_request"),
        ForeignKeyConstraint(
            ["guild_id", "case_id", "case_filed_at"], ["court_cases.guild_id", "court_cases.id", "court_cases.filed_at"],
            name="fk_court_hearings_case",
        ),
        ForeignKeyConstraint(
            ["guild_id", "judge_citizen_id"], ["citizens.guild_id", "citizens.id"], name="fk_court_hearings_judge",
        ),
        CheckConstraint(
            "case_filed_at <= scheduled_at AND scheduled_at < hearing_at AND char_length(btrim(request_key)) > 0",
            name="ck_court_hearings_chronology",
        ),
        Index("ix_court_hearings_case_history", "guild_id", "case_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    case_id: Mapped[int] = mapped_column(BigInteger)
    case_filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    judge_citizen_id: Mapped[int] = mapped_column(BigInteger)
    hearing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))


class CourtActDecisionRow(Base):
    __tablename__ = "court_act_decisions"
    __table_args__ = (
        UniqueConstraint("guild_id", "case_id", name="uq_court_act_decisions_guild_case"),
        UniqueConstraint("guild_id", "request_key", name="uq_court_act_decisions_request"),
        ForeignKeyConstraint(
            ["guild_id", "case_id", "case_kind", "revision_id"],
            ["court_cases.guild_id", "court_cases.id", "court_cases.kind", "court_cases.law_revision_id"],
            name="fk_court_act_decisions_case",
        ),
        ForeignKeyConstraint(
            ["guild_id", "judge_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_court_act_decisions_judge",
        ),
        CheckConstraint(
            "case_kind = 'ADMINISTRATIVE' AND outcome IN ('GRANTED', 'DENIED')",
            name="ck_court_act_decisions_outcome",
        ),
        CheckConstraint(
            "char_length(reason) <= 1000 AND char_length(btrim(reason)) > 0 AND char_length(btrim(request_key)) > 0",
            name="ck_court_act_decisions_content",
        ),
        Index(
            "ix_court_act_decisions_active_suspension", "guild_id", "revision_id", "decided_at",
            postgresql_where=text("outcome = 'GRANTED'"),
        ),
    )

    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    revision_id: Mapped[int] = mapped_column(BigInteger)
    case_kind: Mapped[str] = mapped_column(String(20), server_default=text("'ADMINISTRATIVE'"))
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    judge_citizen_id: Mapped[int] = mapped_column(BigInteger)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
