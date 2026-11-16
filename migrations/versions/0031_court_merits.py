"""Allow civil and criminal decisions to use the existing appeal history."""

from alembic import op
from sqlalchemy import BigInteger

revision = "0031_court_merits"
down_revision = "0030_court_appeals"
branch_labels = None
depends_on = None

_TABLE = "court_act_decisions"
_OUTCOME = "ck_court_act_decisions_outcome"
_SCOPED_KIND = "uq_court_cases_guild_kind"
_CASE_FK = "fk_court_act_decisions_case_kind"
_ADMIN = "case_kind = 'ADMINISTRATIVE' AND outcome IN ('GRANTED', 'DENIED')"


def upgrade() -> None:
    op.create_unique_constraint(_SCOPED_KIND, "court_cases", ["guild_id", "id", "kind"])
    op.create_foreign_key(
        _CASE_FK, _TABLE, "court_cases", ["guild_id", "case_id", "case_kind"], ["guild_id", "id", "kind"],
    )
    op.alter_column(_TABLE, "revision_id", existing_type=BigInteger(), nullable=True)
    op.drop_constraint(_OUTCOME, _TABLE, type_="check")
    op.create_check_constraint(
        _OUTCOME, _TABLE,
        "(case_kind = 'ADMINISTRATIVE' AND revision_id IS NOT NULL AND outcome IN ('GRANTED', 'DENIED')) OR "
        "(case_kind = 'CRIMINAL' AND revision_id IS NOT NULL AND outcome IN ('GUILTY', 'ACQUITTED')) OR "
        "(case_kind = 'CIVIL' AND revision_id IS NULL AND outcome IN ('GRANTED', 'DENIED'))",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE court_act_decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_act_decisions WHERE case_kind <> 'ADMINISTRATIVE') THEN
                RAISE EXCEPTION 'Cannot downgrade while civil or criminal decisions exist';
            END IF;
        END $$;
    """)
    op.drop_constraint(_OUTCOME, _TABLE, type_="check")
    op.create_check_constraint(_OUTCOME, _TABLE, _ADMIN)
    op.alter_column(_TABLE, "revision_id", existing_type=BigInteger(), nullable=False)
    op.drop_constraint(_CASE_FK, _TABLE, type_="foreignkey")
    op.drop_constraint(_SCOPED_KIND, "court_cases", type_="unique")
