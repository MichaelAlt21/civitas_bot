"""Allow one retry of a failed election before entering a political crisis."""

from alembic import op

revision = "0015_election_retry"
down_revision = "0014_market_offer_withdrawal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE elections ADD COLUMN retry_of_id bigint,
        ADD CONSTRAINT fk_elections_retry FOREIGN KEY (guild_id, retry_of_id) REFERENCES elections(guild_id, id),
        ADD CONSTRAINT ck_elections_retry_link CHECK (
            retry_of_id IS NULL OR runoff_of_id IS NULL OR retry_of_id = runoff_of_id
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_elections_retry ON elections (coalesce(retry_of_id, runoff_of_id))")
    op.execute("""
        CREATE FUNCTION civitas_guard_election_retry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            parent elections%ROWTYPE;
            parent_certified_at timestamptz;
            source_id bigint;
            eligible_runoff_ids bigint[];
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.retry_of_id IS DISTINCT FROM OLD.retry_of_id THEN
                    RAISE EXCEPTION 'Election retry link is immutable' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            PERFORM id FROM guilds WHERE id = NEW.guild_id FOR NO KEY UPDATE;
            SELECT * INTO parent FROM elections
                WHERE guild_id = NEW.guild_id AND office = NEW.office ORDER BY id DESC LIMIT 1;
            IF parent.id IS NOT NULL AND NEW.id <= parent.id THEN
                RAISE EXCEPTION 'A new election must follow the latest election for this office'
                    USING ERRCODE = 'check_violation';
            END IF;
            source_id := coalesce(NEW.retry_of_id, NEW.runoff_of_id);
            IF parent.id IS NULL OR parent.status = 'APPLIED' THEN
                IF source_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Election retry requires the latest certified failure for this office'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF parent.status NOT IN ('TIED', 'NO_QUORUM') THEN
                RAISE EXCEPTION 'An election for this office is already in progress' USING ERRCODE = 'check_violation';
            END IF;

            SELECT certified_at INTO parent_certified_at FROM election_certifications
                WHERE guild_id = parent.guild_id AND election_id = parent.id;
            IF parent_certified_at IS NULL OR NEW.opens_at < parent_certified_at THEN
                RAISE EXCEPTION 'Election retry requires a certified failure' USING ERRCODE = 'check_violation';
            END IF;
            IF coalesce(parent.retry_of_id, parent.runoff_of_id) IS NOT NULL THEN
                RAISE EXCEPTION 'The election retry already failed for this office' USING ERRCODE = 'check_violation';
            END IF;
            IF source_id IS DISTINCT FROM parent.id THEN
                RAISE EXCEPTION 'Election retry must link the latest failed election for this office'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.seats <> parent.seats OR NEW.term_days <> parent.term_days THEN
                RAISE EXCEPTION 'Election retry must preserve the office size and term'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF parent.status = 'TIED' THEN
                IF NEW.office = 'PRESIDENT' THEN
                    WITH latest AS (
                        SELECT tallies FROM election_protocols WHERE election_id = parent.id
                        ORDER BY round_number DESC LIMIT 1
                    ), ranked AS (
                        SELECT (item->>0)::bigint AS candidate_id, (item->>1)::integer AS votes
                        FROM latest CROSS JOIN LATERAL jsonb_array_elements(tallies) AS tally(item)
                    )
                    SELECT array_agg(candidate_id) INTO eligible_runoff_ids
                    FROM ranked WHERE votes = (SELECT max(votes) FROM ranked);
                ELSE
                    eligible_runoff_ids := parent.candidate_citizen_ids;
                END IF;
                IF NEW.runoff_of_id IS DISTINCT FROM parent.id OR eligible_runoff_ids IS NULL
                    OR NOT (NEW.candidate_citizen_ids @> eligible_runoff_ids AND NEW.candidate_citizen_ids <@ eligible_runoff_ids)
                THEN
                    RAISE EXCEPTION 'Election retry must preserve the runoff rules for an attested tie'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.runoff_of_id IS NOT NULL THEN
                RAISE EXCEPTION 'A quorum failure requires a new nomination round' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER election_retry_guard BEFORE INSERT OR UPDATE OF retry_of_id ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_retry()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM elections WHERE retry_of_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while election retry history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER election_retry_guard ON elections")
    op.execute("DROP FUNCTION civitas_guard_election_retry()")
    op.execute("DROP INDEX uq_elections_retry")
    op.execute("ALTER TABLE elections DROP COLUMN retry_of_id")
