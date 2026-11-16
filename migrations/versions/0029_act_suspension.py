"""Record administrative decisions and their effect on challenged acts."""

from alembic import op
from sqlalchemy import text

revision = "0029_act_suspension"
down_revision = "0028_decree_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_court_cases_administrative_target", "court_cases", ["guild_id", "id", "kind", "law_revision_id"],
    )
    op.execute("""
        CREATE TABLE court_act_decisions (
            case_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            revision_id bigint NOT NULL,
            case_kind varchar(20) NOT NULL DEFAULT 'ADMINISTRATIVE',
            outcome varchar(20) NOT NULL,
            reason text NOT NULL,
            judge_citizen_id bigint NOT NULL,
            decided_at timestamptz NOT NULL,
            request_key varchar(128) NOT NULL,
            CONSTRAINT uq_court_act_decisions_request UNIQUE (guild_id, request_key),
            CONSTRAINT fk_court_act_decisions_case FOREIGN KEY (guild_id, case_id, case_kind, revision_id)
                REFERENCES court_cases(guild_id, id, kind, law_revision_id),
            CONSTRAINT fk_court_act_decisions_judge FOREIGN KEY (guild_id, judge_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_court_act_decisions_outcome CHECK (case_kind = 'ADMINISTRATIVE' AND outcome IN ('GRANTED', 'DENIED')),
            CONSTRAINT ck_court_act_decisions_content CHECK (
                char_length(reason) <= 1000 AND char_length(btrim(reason)) > 0 AND char_length(btrim(request_key)) > 0
            )
        )
    """)
    op.create_index(
        "ix_court_act_decisions_active_suspension", "court_act_decisions", ["guild_id", "revision_id", "decided_at"],
        postgresql_where=text("outcome = 'GRANTED'"),
    )
    op.execute("""
        CREATE TRIGGER court_act_decisions_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON court_act_decisions
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE court_act_decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_act_decisions) THEN
                RAISE EXCEPTION 'Cannot downgrade while administrative decision history exists';
            END IF;
        END $$
    """)
    op.drop_table("court_act_decisions")
    op.drop_constraint("uq_court_cases_administrative_target", "court_cases", type_="unique")
