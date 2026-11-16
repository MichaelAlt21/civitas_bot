from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Identity, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class LawRevisionRow(Base):
    __tablename__ = "law_revisions"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_law_revisions_request"),
        UniqueConstraint(
            "guild_id", "id", "code", "effective_at", "proposer_citizen_id", "proposed_at", "act_kind",
            name="uq_law_revisions_confirmation_link",
        ),
        ForeignKeyConstraint(
            ["guild_id", "proposer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_law_revisions_proposer",
        ),
        CheckConstraint(
            "length(btrim(code)) > 0 AND length(btrim(title)) > 0 AND length(btrim(text)) > 0 "
            "AND length(btrim(source_url)) > 0 AND length(btrim(request_key)) > 0",
            name="ck_law_revisions_content",
        ),
        CheckConstraint("act_kind IN ('LAW', 'DECREE')", name="ck_law_revisions_act_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    proposer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
    act_kind: Mapped[str] = mapped_column(Text, server_default="LAW")


class LawConfirmationRow(Base):
    __tablename__ = "law_confirmations"
    __table_args__ = (
        UniqueConstraint("guild_id", "act_kind", "code", "effective_at", name="uq_law_confirmations_slot"),
        ForeignKeyConstraint(
            ["guild_id", "revision_id", "code", "effective_at", "proposer_citizen_id", "proposed_at", "act_kind"],
            ["law_revisions.guild_id", "law_revisions.id", "law_revisions.code", "law_revisions.effective_at",
             "law_revisions.proposer_citizen_id", "law_revisions.proposed_at", "law_revisions.act_kind"],
            name="fk_law_confirmations_revision",
        ),
        ForeignKeyConstraint(
            ["guild_id", "confirmed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_law_confirmations_deputy",
        ),
        CheckConstraint(
            "proposer_citizen_id <> confirmed_by_citizen_id AND proposed_at <= confirmed_at",
            name="ck_law_confirmations_independence",
        ),
        CheckConstraint("act_kind IN ('LAW', 'DECREE')", name="ck_law_confirmations_act_kind"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    proposer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    act_kind: Mapped[str] = mapped_column(Text, server_default="LAW")
