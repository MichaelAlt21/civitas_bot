"""Allow signatures to be appended, each once, to a case substitute proposal."""

from alembic import op

revision = "0045_case_substitute_guard"
down_revision = "0044_immutable_case_recusals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION civitas_guard_case_substitute() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.first_senator_id IS NOT NULL OR NEW.second_senator_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Case substitute must be proposed before signing' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(OLD.id, OLD.guild_id, OLD.case_id, OLD.case_kind, OLD.role, OLD.candidate_id,
                   OLD.proposer_id, OLD.proposed_at, OLD.request_key)
                IS DISTINCT FROM
                ROW(NEW.id, NEW.guild_id, NEW.case_id, NEW.case_kind, NEW.role, NEW.candidate_id,
                    NEW.proposer_id, NEW.proposed_at, NEW.request_key)
                OR OLD.second_senator_id IS NOT NULL
                OR (OLD.first_senator_id IS NULL AND (NEW.first_senator_id IS NULL OR NEW.second_senator_id IS NOT NULL))
                OR (OLD.first_senator_id IS NOT NULL AND (
                    NEW.second_senator_id IS NULL OR NEW.first_senator_id IS DISTINCT FROM OLD.first_senator_id
                    OR NEW.first_signed_at IS DISTINCT FROM OLD.first_signed_at
                ))
            THEN
                RAISE EXCEPTION 'Case substitute signature history is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER court_case_substitute_guard BEFORE INSERT OR UPDATE ON court_case_substitutes
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_case_substitute()
    """)
    op.execute("""
        CREATE TRIGGER court_case_substitute_immutable BEFORE DELETE OR TRUNCATE ON court_case_substitutes
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER court_case_substitute_immutable ON court_case_substitutes")
    op.execute("DROP TRIGGER court_case_substitute_guard ON court_case_substitutes")
    op.execute("DROP FUNCTION civitas_guard_case_substitute()")
