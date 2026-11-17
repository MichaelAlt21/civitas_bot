"""Allow a successor to start after a completed judicial removal."""

from alembic import op

revision = "0042_court_term_cutoff"
down_revision = "0041_court_removal"
branch_labels = None
depends_on = None


def _appointment_guard(overlap: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION civitas_guard_court_appointment() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM guilds WHERE id = NEW.guild_id FOR NO KEY UPDATE;
            IF (
                SELECT count(*) FROM court_approvals AS approval
                JOIN court_nominations AS nomination
                    ON approval.guild_id = nomination.guild_id AND approval.nomination_id = nomination.id
                WHERE approval.guild_id = NEW.guild_id AND approval.nomination_id = NEW.nomination_id
                    AND approval.approved_at BETWEEN nomination.proposed_at AND NEW.starts_at
            ) <> 2 THEN
                RAISE EXCEPTION 'Court appointment requires two approved signatures' USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM court_appointments AS active
                WHERE active.guild_id = NEW.guild_id {overlap}
            ) THEN
                RAISE EXCEPTION 'Chief justice appointment overlaps an existing term' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)


def upgrade() -> None:
    _appointment_guard("""
        AND active.starts_at < NEW.ends_at AND NEW.starts_at < coalesce((
                        SELECT removal.second_signed_at FROM court_removal_proposals AS removal
                        WHERE removal.guild_id = active.guild_id AND removal.nomination_id = active.nomination_id
                            AND removal.second_signed_at IS NOT NULL
                    ), active.ends_at)
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE court_removal_proposals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_removal_proposals WHERE second_signed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot restore the old term rules after a judicial removal';
            END IF;
        END $$;
    """)
    _appointment_guard("AND active.starts_at < NEW.ends_at AND NEW.starts_at < active.ends_at")
