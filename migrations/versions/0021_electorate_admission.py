"""Use the new immutable electorate snapshot when validating runoff slates."""

from alembic import op

revision = "0021_electorate_admission"
down_revision = "0020_presidential_impeachment"
branch_labels = None
depends_on = None


def _replace_retry_guard(*, use_admission: bool) -> None:
    admission_argument = ", NEW.eligible_citizen_ids" if use_admission else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION civitas_guard_election_retry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            parent elections%ROWTYPE;
            parent_certified_at timestamptz;
            source_id bigint;
            original_slate_ids bigint[];
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
                    SELECT array_agg(candidate_id) INTO original_slate_ids
                    FROM ranked WHERE votes = (SELECT max(votes) FROM ranked);
                ELSE
                    original_slate_ids := parent.candidate_citizen_ids;
                END IF;
                IF NEW.runoff_of_id IS DISTINCT FROM parent.id OR original_slate_ids IS NULL
                    OR NOT civitas_valid_remaining_runoff_slate(
                        NEW.guild_id, original_slate_ids, NEW.candidate_citizen_ids, NEW.seats{admission_argument}
                    )
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


def _replace_replacement_guard(*, use_admission: bool) -> None:
    admission_argument = ", NEW.eligible_citizen_ids" if use_admission else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION civitas_guard_election_replacement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous elections%ROWTYPE;
        BEGIN
            PERFORM id FROM guilds WHERE id = NEW.guild_id FOR NO KEY UPDATE;
            SELECT * INTO previous FROM elections WHERE guild_id = NEW.guild_id AND office = NEW.office
                ORDER BY id DESC LIMIT 1;
            IF NOT coalesce(previous.cancelled, false) THEN
                IF NEW.replacement_of_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Replacement requires the latest cancelled election for this office'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.replacement_of_id IS DISTINCT FROM previous.id OR NEW.id <= previous.id
                OR coalesce(NEW.retry_of_id, NEW.runoff_of_id) IS DISTINCT FROM coalesce(previous.retry_of_id, previous.runoff_of_id)
                OR NEW.runoff_of_id IS DISTINCT FROM previous.runoff_of_id
                OR NEW.seats <> previous.seats OR NEW.term_days <> previous.term_days
                OR (NEW.runoff_of_id IS NOT NULL AND NOT civitas_valid_remaining_runoff_slate(
                    NEW.guild_id, previous.candidate_citizen_ids, NEW.candidate_citizen_ids, NEW.seats{admission_argument}
                ))
                OR NOT EXISTS (
                    SELECT 1 FROM election_cancellation_confirmations
                    WHERE election_id = previous.id AND occurred_at <= NEW.opens_at
                ) THEN
                RAISE EXCEPTION 'Replacement must preserve the latest cancelled election slot'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)


def _install_slate_helper(*, use_admission: bool) -> None:
    admission_parameter = ", admitted_voter_ids bigint[]" if use_admission else ""
    admission_filter = "\n                    AND id = ANY(admitted_voter_ids)" if use_admission else ""
    op.execute(f"""
        CREATE FUNCTION civitas_valid_remaining_runoff_slate(
            target_guild_id bigint, original_slate_ids bigint[], submitted_slate_ids bigint[], seats integer{admission_parameter}
        ) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            WITH verified AS (
                SELECT id FROM citizens
                WHERE guild_id = target_guild_id AND status = 'VERIFIED'{admission_filter}
                    AND (id = ANY(original_slate_ids) OR id = ANY(submitted_slate_ids))
            ), slates AS (
                SELECT coalesce(array_agg(id) FILTER (WHERE id = ANY(original_slate_ids)), ARRAY[]::bigint[])
                           AS remaining,
                       count(*) FILTER (WHERE id = ANY(submitted_slate_ids)) AS verified_submitted_count
                FROM verified
            )
            SELECT cardinality(submitted_slate_ids) >= seats
                AND verified_submitted_count = cardinality(submitted_slate_ids)
                AND (cardinality(remaining) < seats OR (
                    submitted_slate_ids @> remaining AND submitted_slate_ids <@ remaining
                ))
            FROM slates
        $$
    """)


def upgrade() -> None:
    _install_slate_helper(use_admission=True)
    _replace_retry_guard(use_admission=True)
    _replace_replacement_guard(use_admission=True)
    op.execute("DROP FUNCTION civitas_valid_remaining_runoff_slate(bigint, bigint[], bigint[], integer)")


def downgrade() -> None:
    _install_slate_helper(use_admission=False)
    _replace_replacement_guard(use_admission=False)
    _replace_retry_guard(use_admission=False)
    op.execute("DROP FUNCTION civitas_valid_remaining_runoff_slate(bigint, bigint[], bigint[], integer, bigint[])")
