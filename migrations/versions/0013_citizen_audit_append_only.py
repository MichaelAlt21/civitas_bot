"""Protect citizenship audit history from edits (FR-14, AT-26)."""

from alembic import op

revision = "0013_citizen_audit_append_only"
down_revision = "0012_economy_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER audit_events_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_immutable ON audit_events")
