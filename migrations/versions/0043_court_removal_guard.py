"""Make judicial removal proposals and recorded signatures immutable."""

from alembic import op

revision = "0043_court_removal_guard"
down_revision = "0042_court_term_cutoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION civitas_guard_court_removal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.first_senator_id IS NOT NULL OR NEW.second_senator_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Judicial removal must be proposed before signing'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(OLD.id, OLD.guild_id, OLD.nomination_id, OLD.president_term_id,
                   OLD.proposer_id, OLD.reason, OLD.request_key, OLD.proposed_at)
                IS DISTINCT FROM
                ROW(NEW.id, NEW.guild_id, NEW.nomination_id, NEW.president_term_id,
                    NEW.proposer_id, NEW.reason, NEW.request_key, NEW.proposed_at)
                OR OLD.second_senator_id IS NOT NULL
                OR (OLD.first_senator_id IS NULL AND (NEW.first_senator_id IS NULL OR NEW.second_senator_id IS NOT NULL))
                OR (OLD.first_senator_id IS NOT NULL AND (
                    NEW.second_senator_id IS NULL OR NEW.first_senator_id IS DISTINCT FROM OLD.first_senator_id
                    OR NEW.first_signed_at IS DISTINCT FROM OLD.first_signed_at
                ))
            THEN
                RAISE EXCEPTION 'Judicial removal signature history is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM court_appointments AS appointment
                WHERE appointment.guild_id = NEW.guild_id AND appointment.nomination_id = NEW.nomination_id
                  AND appointment.starts_at <= NEW.proposed_at
                  AND coalesce(NEW.second_signed_at, NEW.first_signed_at) < appointment.ends_at
            ) THEN
                RAISE EXCEPTION 'Judicial removal must be signed within the target mandate'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER court_removal_guard BEFORE INSERT OR UPDATE ON court_removal_proposals
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_court_removal()
    """)
    op.execute("""
        CREATE TRIGGER court_removal_immutable BEFORE DELETE OR TRUNCATE ON court_removal_proposals
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER court_removal_immutable ON court_removal_proposals")
    op.execute("DROP TRIGGER court_removal_guard ON court_removal_proposals")
    op.execute("DROP FUNCTION civitas_guard_court_removal()")
