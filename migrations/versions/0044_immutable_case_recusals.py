"""Keep the judicial recusal history append-only."""

from alembic import op

revision = "0044_immutable_case_recusals"
down_revision = "0043_court_removal_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER court_case_recusals_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON court_case_recusals
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER court_case_recusals_immutable ON court_case_recusals")
