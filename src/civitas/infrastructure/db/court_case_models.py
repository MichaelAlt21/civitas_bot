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
        UniqueConstraint("guild_id", "id", "kind", name="uq_court_cases_guild_kind"),
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


class CourtCaseRecusalRow(Base):
    __tablename__ = "court_case_recusals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "case_id", "case_kind"], ["court_cases.guild_id", "court_cases.id", "court_cases.kind"],
            name="fk_case_recusals_case",
        ),
        ForeignKeyConstraint(["guild_id", "actor_id"], ["citizens.guild_id", "citizens.id"], name="fk_case_recusals_actor"),
        CheckConstraint("role IN ('JUDGE', 'PROSECUTOR')", name="ck_case_recusals_role"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_kind: Mapped[str] = mapped_column(String(20))
    recused_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourtCaseSubstituteRow(Base):
    __tablename__ = "court_case_substitutes"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_case_substitutes_request"),
        ForeignKeyConstraint(
            ["guild_id", "case_id", "case_kind"], ["court_cases.guild_id", "court_cases.id", "court_cases.kind"],
            name="fk_case_substitutes_case",
        ),
        ForeignKeyConstraint(["guild_id", "candidate_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_case_substitutes_candidate_id"),
        ForeignKeyConstraint(["guild_id", "proposer_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_case_substitutes_proposer_id"),
        ForeignKeyConstraint(["guild_id", "first_senator_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_case_substitutes_first_senator_id"),
        ForeignKeyConstraint(["guild_id", "second_senator_id"], ["citizens.guild_id", "citizens.id"],
                             name="fk_case_substitutes_second_senator_id"),
        CheckConstraint("role IN ('JUDGE', 'PROSECUTOR')", name="ck_case_substitutes_role"),
        CheckConstraint(
            "(first_senator_id IS NULL) = (first_signed_at IS NULL) "
            "AND (second_senator_id IS NULL) = (second_signed_at IS NULL) "
            "AND (first_signed_at IS NULL OR first_signed_at >= proposed_at) "
            "AND (second_signed_at IS NULL OR (first_signed_at IS NOT NULL AND second_signed_at >= first_signed_at))",
            name="ck_case_substitutes_signatures",
        ),
        CheckConstraint(
            "candidate_id <> proposer_id AND first_senator_id <> proposer_id AND first_senator_id <> candidate_id "
            "AND second_senator_id <> proposer_id AND second_senator_id <> candidate_id "
            "AND first_senator_id <> second_senator_id",
            name="ck_case_substitutes_independence",
        ),
        Index(
            "uq_case_substitutes_confirmed", "guild_id", "case_id", "role",
            unique=True, postgresql_where=text("second_signed_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    case_id: Mapped[int] = mapped_column(BigInteger)
    case_kind: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(16))
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    proposer_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
    first_senator_id: Mapped[int | None] = mapped_column(BigInteger)
    first_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    second_senator_id: Mapped[int | None] = mapped_column(BigInteger)
    second_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
            ["guild_id", "case_id", "case_kind"], ["court_cases.guild_id", "court_cases.id", "court_cases.kind"],
            name="fk_court_act_decisions_case_kind",
        ),
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
            "(case_kind = 'ADMINISTRATIVE' AND revision_id IS NOT NULL AND outcome IN ('GRANTED', 'DENIED')) OR "
            "(case_kind = 'CRIMINAL' AND revision_id IS NOT NULL AND outcome IN ('GUILTY', 'ACQUITTED')) OR "
            "(case_kind = 'CIVIL' AND revision_id IS NULL AND outcome IN ('GRANTED', 'DENIED'))",
            name="ck_court_act_decisions_outcome",
        ),
        CheckConstraint(
            "(sanction_article IS NULL AND fine_amount IS NULL AND custody_hours IS NULL) OR (sanction_article IS NOT NULL "
            "AND char_length(btrim(sanction_article)) > 0 AND case_kind = 'CRIMINAL' AND outcome = 'GUILTY' AND "
            "(fine_amount IS NOT NULL OR custody_hours IS NOT NULL))",
            name="ck_court_act_decisions_sentence",
        ),
        CheckConstraint(
            "(fine_amount IS NULL AND fine_min IS NULL AND fine_max IS NULL) OR (fine_amount IS NOT NULL AND fine_min IS "
            "NOT NULL AND fine_max IS NOT NULL AND fine_amount BETWEEN 10 AND 100 AND fine_min >= 0 AND fine_amount BETWEEN "
            "fine_min AND fine_max)",
            name="ck_court_act_decisions_fine",
        ),
        CheckConstraint(
            "(custody_hours IS NULL AND custody_min IS NULL AND custody_max IS NULL) OR (custody_hours IS NOT NULL AND "
            "custody_min IS NOT NULL AND custody_max IS NOT NULL AND custody_hours BETWEEN 1 AND 24 AND custody_min >= 0 "
            "AND custody_hours BETWEEN custody_min AND custody_max)",
            name="ck_court_act_decisions_custody",
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
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    case_kind: Mapped[str] = mapped_column(String(20), server_default=text("'ADMINISTRATIVE'"))
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    judge_citizen_id: Mapped[int] = mapped_column(BigInteger)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
    sanction_article: Mapped[str | None] = mapped_column(String(120))
    fine_amount: Mapped[int | None] = mapped_column(BigInteger)
    fine_min: Mapped[int | None] = mapped_column(BigInteger)
    fine_max: Mapped[int | None] = mapped_column(BigInteger)
    custody_hours: Mapped[int | None] = mapped_column(BigInteger)
    custody_min: Mapped[int | None] = mapped_column(BigInteger)
    custody_max: Mapped[int | None] = mapped_column(BigInteger)
