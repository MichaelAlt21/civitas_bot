"""Persist a voluntary recusal for a specific court case."""

from alembic import op

revision = "0040_case_recusals"
down_revision = "0039_case_substitutes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE court_case_recusals (
            guild_id bigint NOT NULL,
            case_id bigint NOT NULL,
            case_kind varchar(20) NOT NULL,
            role varchar(16) NOT NULL,
            actor_id bigint NOT NULL,
            recused_at timestamptz NOT NULL,
            PRIMARY KEY (guild_id, case_id, role, actor_id),
            CONSTRAINT fk_case_recusals_case FOREIGN KEY (guild_id, case_id, case_kind)
                REFERENCES court_cases(guild_id, id, kind),
            CONSTRAINT fk_case_recusals_actor FOREIGN KEY (guild_id, actor_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_case_recusals_role CHECK (role IN ('JUDGE', 'PROSECUTOR'))
        )
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE court_case_recusals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_case_recusals) THEN
                RAISE EXCEPTION 'Cannot downgrade while case recusal history exists';
            END IF;
        END $$;
    """)
    op.drop_table("court_case_recusals")
