"""Link a corrected act to the judgment that suspended its predecessor."""

from alembic import op
from sqlalchemy import BigInteger, Column

revision = "0038_act_correction"
down_revision = "0037_cancelled_fine_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("law_revisions", Column("correction_case_id", BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_law_revisions_correction", "law_revisions", "court_act_decisions",
        ["guild_id", "correction_case_id"], ["guild_id", "case_id"],
    )


def downgrade() -> None:
    op.execute("LOCK TABLE law_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM law_revisions WHERE correction_case_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while act correction links exist';
            END IF;
        END $$;
    """)
    op.drop_constraint("fk_law_revisions_correction", "law_revisions", type_="foreignkey")
    op.drop_column("law_revisions", "correction_case_id")
