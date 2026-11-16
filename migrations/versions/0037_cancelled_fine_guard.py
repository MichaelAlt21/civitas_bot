"""Reject a new escrow hold after the related verdict is overturned."""

from alembic import op

revision = "0037_cancelled_fine_guard"
down_revision = "0036_custody_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION civitas_reject_cancelled_fine_hold() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM court_appeal_confirmations AS appeal
                WHERE appeal.guild_id = NEW.guild_id AND appeal.outcome = 'OVERTURNED'
                    AND NEW.idempotency_key = 'court_fine_hold:' || appeal.case_id
            ) THEN
                RAISE EXCEPTION 'Cannot hold an overturned court fine' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER court_fine_reject_cancelled_hold
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind = 'COURT_FINE_HOLD') EXECUTE FUNCTION civitas_reject_cancelled_fine_hold()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER court_fine_reject_cancelled_hold ON financial_transactions")
    op.execute("DROP FUNCTION civitas_reject_cancelled_fine_hold()")
