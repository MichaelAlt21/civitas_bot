"""Record two independent election cancellation attestations and preserve the attempt slot."""

from alembic import op

revision = "0017_election_cancellation"
down_revision = "0016_election_crisis_resolution"
branch_labels = None
depends_on = None


def _active_index(*, cancellation: bool) -> None:
    predicate = " AND NOT cancelled" if cancellation else ""
    op.execute(f"""
        CREATE UNIQUE INDEX uq_elections_active_office ON elections(guild_id, office)
        WHERE status IN ('OPEN', 'EXTENDED', 'PENDING_CERTIFICATION'){predicate}
    """)


def _retry_index(*, replacements: bool) -> None:
    predicate = " WHERE replacement_of_id IS NULL" if replacements else ""
    op.execute(f"""
        CREATE UNIQUE INDEX uq_elections_retry ON elections (coalesce(retry_of_id, runoff_of_id)){predicate}
    """)


def _legacy_state_guard(*, cancellation: bool) -> None:
    # The old state machine only accepts an actual status transition on UPDATE.
    # Keep it on all original columns and let the new trigger govern cancellation.
    columns = """ OF id, guild_id, office, seats, term_days, quorum, extension_hours, opens_at,
        closes_at, opened_by_citizen_id, reason, request_key, eligible_citizen_ids, candidate_citizen_ids,
        candidate_discord_user_ids, status, extended_until, runoff_of_id""" if cancellation else ""
    op.execute("DROP TRIGGER politics_election_state ON elections")
    op.execute(f"""
        CREATE TRIGGER politics_election_state BEFORE INSERT OR UPDATE{columns} OR DELETE ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election()
    """)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE elections ADD COLUMN replacement_of_id bigint,
        ADD COLUMN cancelled boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT uq_elections_replacement UNIQUE (replacement_of_id),
        ADD CONSTRAINT fk_elections_replacement FOREIGN KEY (guild_id, replacement_of_id)
            REFERENCES elections(guild_id, id)
    """)
    op.execute("ALTER TABLE elections DROP CONSTRAINT uq_elections_runoff")
    op.execute("DROP INDEX uq_elections_active_office")
    op.execute("DROP INDEX uq_elections_retry")
    _active_index(cancellation=True)
    _retry_index(replacements=True)
    _legacy_state_guard(cancellation=True)
    op.execute("""
        CREATE TABLE election_cancellation_proposals (
            election_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            actor_citizen_id bigint NOT NULL,
            occurred_at timestamptz NOT NULL,
            reason text NOT NULL,
            CONSTRAINT uq_cancel_proposals_guild_election UNIQUE (guild_id, election_id),
            CONSTRAINT fk_cancel_proposals_election FOREIGN KEY (guild_id, election_id)
                REFERENCES elections(guild_id, id),
            CONSTRAINT fk_cancel_proposals_actor FOREIGN KEY (guild_id, actor_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_cancel_proposals_reason CHECK (length(btrim(reason)) BETWEEN 1 AND 200)
        )
    """)
    op.execute("""
        CREATE TABLE election_cancellation_confirmations (
            election_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            actor_citizen_id bigint NOT NULL,
            occurred_at timestamptz NOT NULL,
            reason text NOT NULL,
            CONSTRAINT fk_cancel_confirmations_proposal FOREIGN KEY (guild_id, election_id)
                REFERENCES election_cancellation_proposals(guild_id, election_id),
            CONSTRAINT fk_cancel_confirmations_actor FOREIGN KEY (guild_id, actor_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_cancel_confirmations_reason CHECK (length(btrim(reason)) BETWEEN 1 AND 200)
        )
    """)
    _install_guards()


def _install_guards() -> None:
    op.execute("""
        CREATE FUNCTION civitas_guard_cancellation_attestation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            ballot elections%ROWTYPE;
            proposal election_cancellation_proposals%ROWTYPE;
        BEGIN
            SELECT * INTO ballot FROM elections WHERE guild_id = NEW.guild_id AND id = NEW.election_id
                FOR NO KEY UPDATE;
            IF ballot.id IS NULL OR ballot.cancelled OR ballot.status = 'APPLIED' OR NEW.occurred_at < ballot.opens_at
                OR EXISTS (SELECT 1 FROM election_certifications WHERE election_id = ballot.id)
                OR EXISTS (SELECT 1 FROM elections WHERE guild_id = ballot.guild_id
                    AND office = ballot.office AND id > ballot.id)
            THEN
                RAISE EXCEPTION 'Cancellation requires the latest uncertified election'
                    USING ERRCODE = 'check_violation';
            END IF;
            PERFORM id FROM citizens WHERE guild_id = NEW.guild_id AND id = NEW.actor_citizen_id
                AND status = 'VERIFIED' FOR NO KEY UPDATE;
            IF NOT FOUND OR NEW.actor_citizen_id = ANY(ballot.candidate_citizen_ids) THEN
                RAISE EXCEPTION 'Cancellation requires a verified noncandidate' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_TABLE_NAME = 'election_cancellation_confirmations' THEN
                SELECT * INTO proposal FROM election_cancellation_proposals WHERE election_id = ballot.id;
                IF proposal.election_id IS NULL OR NEW.actor_citizen_id = proposal.actor_citizen_id
                    OR NEW.occurred_at < proposal.occurred_at THEN
                    RAISE EXCEPTION 'Cancellation confirmation requires a separate reviewer after the proposal'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for table in ("election_cancellation_proposals", "election_cancellation_confirmations"):
        op.execute(f"""
            CREATE TRIGGER election_cancellation_attestation_guard BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION civitas_guard_cancellation_attestation()
        """)
        op.execute(f"""
            CREATE TRIGGER election_cancellation_attestation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
        """)
    op.execute("""
        CREATE FUNCTION civitas_mark_election_cancelled() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE elections SET cancelled = true WHERE guild_id = NEW.guild_id AND id = NEW.election_id;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER election_cancellation_marked AFTER INSERT ON election_cancellation_confirmations
        FOR EACH ROW EXECUTE FUNCTION civitas_mark_election_cancelled()
    """)
    op.execute("""
        CREATE FUNCTION civitas_guard_election_cancellation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            ballot_cancelled boolean;
        BEGIN
            IF TG_TABLE_NAME <> 'elections' THEN
                SELECT cancelled INTO ballot_cancelled FROM elections
                    WHERE guild_id = NEW.guild_id AND id = NEW.election_id FOR NO KEY UPDATE;
                IF ballot_cancelled THEN
                    RAISE EXCEPTION 'Cancelled election cannot record or certify a protocol'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.cancelled THEN
                    RAISE EXCEPTION 'Election must be opened before it can be cancelled' USING ERRCODE = 'check_violation';
                END IF;
            ELSIF OLD.cancelled OR NEW.replacement_of_id IS DISTINCT FROM OLD.replacement_of_id
                OR (NEW.cancelled AND NOT EXISTS (
                    SELECT 1 FROM election_cancellation_confirmations WHERE election_id = NEW.id
                )) THEN
                RAISE EXCEPTION 'Election cancellation and replacement history is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER election_cancellation_state BEFORE INSERT OR UPDATE ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_cancellation()
    """)
    for table in ("election_protocols", "election_certifications"):
        op.execute(f"""
            CREATE TRIGGER election_cancellation_state BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_cancellation()
        """)
    op.execute("""
        CREATE FUNCTION civitas_guard_election_replacement() RETURNS trigger
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
                OR (NEW.runoff_of_id IS NOT NULL AND NEW.candidate_citizen_ids IS DISTINCT FROM previous.candidate_citizen_ids)
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
    op.execute("""
        CREATE TRIGGER election_replacement_guard BEFORE INSERT ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_replacement()
    """)
    op.execute("DROP TRIGGER election_retry_guard ON elections")
    op.execute("""
        CREATE TRIGGER election_retry_guard BEFORE INSERT ON elections
        FOR EACH ROW WHEN (NEW.replacement_of_id IS NULL AND NOT civitas_can_open_after_crisis(
            NEW.guild_id, NEW.office, NEW.id, NEW.opens_at, coalesce(NEW.retry_of_id, NEW.runoff_of_id)
        )) EXECUTE FUNCTION civitas_guard_election_retry()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM election_cancellation_proposals) THEN
                RAISE EXCEPTION 'Cannot downgrade while election cancellation history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER election_replacement_guard ON elections")
    op.execute("DROP FUNCTION civitas_guard_election_replacement()")
    op.execute("DROP TRIGGER election_retry_guard ON elections")
    op.execute("""
        CREATE TRIGGER election_retry_guard BEFORE INSERT ON elections
        FOR EACH ROW WHEN (NOT civitas_can_open_after_crisis(
            NEW.guild_id, NEW.office, NEW.id, NEW.opens_at, coalesce(NEW.retry_of_id, NEW.runoff_of_id)
        )) EXECUTE FUNCTION civitas_guard_election_retry()
    """)
    for table in ("elections", "election_protocols", "election_certifications"):
        op.execute(f"DROP TRIGGER election_cancellation_state ON {table}")
    op.execute("DROP FUNCTION civitas_guard_election_cancellation()")
    op.drop_table("election_cancellation_confirmations")
    op.drop_table("election_cancellation_proposals")
    op.execute("DROP FUNCTION civitas_mark_election_cancelled()")
    op.execute("DROP FUNCTION civitas_guard_cancellation_attestation()")
    op.execute("DROP INDEX uq_elections_active_office")
    op.execute("DROP INDEX uq_elections_retry")
    _legacy_state_guard(cancellation=False)
    op.execute("ALTER TABLE elections DROP COLUMN cancelled, DROP COLUMN replacement_of_id")
    op.execute("ALTER TABLE elections ADD CONSTRAINT uq_elections_runoff UNIQUE (runoff_of_id)")
    _active_index(cancellation=False)
    _retry_index(replacements=False)
