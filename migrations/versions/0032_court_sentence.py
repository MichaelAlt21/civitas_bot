"""Persist the article and numerical terms of a court sentence."""

from alembic import op
from sqlalchemy import BigInteger, Column, String

revision = "0032_court_sentence"
down_revision = "0031_court_merits"
branch_labels = None
depends_on = None

_TABLE = "court_act_decisions"
_NUMERICAL_COLUMNS = ("fine_amount", "fine_min", "fine_max", "custody_hours", "custody_min", "custody_max")
_CHECKS = (
    ("ck_court_act_decisions_sentence",
     "(sanction_article IS NULL AND fine_amount IS NULL AND custody_hours IS NULL) OR (sanction_article IS "
     "NOT NULL AND char_length(btrim(sanction_article)) > 0 AND case_kind = 'CRIMINAL' AND outcome = "
     "'GUILTY' AND (fine_amount IS NOT NULL OR custody_hours IS NOT NULL))"),
    ("ck_court_act_decisions_fine",
     "(fine_amount IS NULL AND fine_min IS NULL AND fine_max IS NULL) OR (fine_amount IS NOT NULL AND "
     "fine_min IS NOT NULL AND fine_max IS NOT NULL AND fine_amount BETWEEN 10 AND 100 AND fine_min >= 0 "
     "AND fine_amount BETWEEN fine_min AND fine_max)"),
    ("ck_court_act_decisions_custody",
     "(custody_hours IS NULL AND custody_min IS NULL AND custody_max IS NULL) OR (custody_hours IS NOT "
     "NULL AND custody_min IS NOT NULL AND custody_max IS NOT NULL AND custody_hours BETWEEN 1 AND 24 AND "
     "custody_min >= 0 AND custody_hours BETWEEN custody_min AND custody_max)"),
)


def upgrade() -> None:
    op.add_column(_TABLE, Column("sanction_article", String(120), nullable=True))
    for name in _NUMERICAL_COLUMNS:
        op.add_column(_TABLE, Column(name, BigInteger(), nullable=True))
    for name, expression in _CHECKS:
        op.create_check_constraint(name, _TABLE, expression)


def downgrade() -> None:
    op.execute("LOCK TABLE court_act_decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_act_decisions WHERE sanction_article IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while court sentences exist';
            END IF;
        END $$;
    """)
    for name, _ in reversed(_CHECKS):
        op.drop_constraint(name, _TABLE, type_="check")
    for name in (*reversed(_NUMERICAL_COLUMNS), "sanction_article"):
        op.drop_column(_TABLE, name)
