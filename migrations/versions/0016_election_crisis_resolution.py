"""Require independent attestations before opening elections after an RP crisis."""

from alembic import op

revision = "0016_election_crisis_resolution"
down_revision = "0015_election_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE election_crisis_proposals (
            election_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            actor_citizen_id bigint NOT NULL,
            occurred_at timestamptz NOT NULL,
            reason text NOT NULL,
            CONSTRAINT uq_crisis_proposals_guild_election UNIQUE (guild_id, election_id),
            CONSTRAINT fk_crisis_proposals_election FOREIGN KEY (guild_id, election_id)
                REFERENCES elections(guild_id, id),
            CONSTRAINT fk_crisis_proposals_actor FOREIGN KEY (guild_id, actor_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_crisis_proposals_reason CHECK (length(btrim(reason)) BETWEEN 1 AND 200)
        )
    """)
    op.execute("""
        CREATE TABLE election_crisis_confirmations (
            election_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            actor_citizen_id bigint NOT NULL,
            occurred_at timestamptz NOT NULL,
            reason text NOT NULL,
            CONSTRAINT fk_crisis_confirmations_proposal FOREIGN KEY (guild_id, election_id)
                REFERENCES election_crisis_proposals(guild_id, election_id),
            CONSTRAINT fk_crisis_confirmations_actor FOREIGN KEY (guild_id, actor_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_crisis_confirmations_reason CHECK (length(btrim(reason)) BETWEEN 1 AND 200)
        )
    """)
    op.execute("""
        CREATE FUNCTION civitas_guard_crisis_attestation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            proposal election_crisis_proposals%ROWTYPE;
        BEGIN
            PERFORM id FROM citizens WHERE guild_id = NEW.guild_id AND id = NEW.actor_citizen_id
                AND status = 'VERIFIED' FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Crisis attestation requires a verified citizen' USING ERRCODE = 'check_violation';
            END IF;
            PERFORM id FROM guilds WHERE id = NEW.guild_id FOR NO KEY UPDATE;
            IF NOT EXISTS (
                SELECT 1 FROM elections AS crisis
                JOIN election_certifications AS certificate ON certificate.election_id = crisis.id
                WHERE crisis.guild_id = NEW.guild_id AND crisis.id = NEW.election_id
                    AND crisis.status IN ('TIED', 'NO_QUORUM')
                    AND coalesce(crisis.retry_of_id, crisis.runoff_of_id) IS NOT NULL
                    AND NOT (NEW.actor_citizen_id = ANY(crisis.candidate_citizen_ids))
                    AND NEW.occurred_at >= certificate.certified_at
                    AND NOT EXISTS (
                        SELECT 1 FROM elections AS successor WHERE successor.guild_id = crisis.guild_id
                            AND successor.office = crisis.office AND successor.id > crisis.id
                    )
            ) THEN
                RAISE EXCEPTION 'Crisis attestation requires the latest certified failure and a noncandidate'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF TG_TABLE_NAME = 'election_crisis_confirmations' THEN
                SELECT * INTO proposal FROM election_crisis_proposals
                    WHERE guild_id = NEW.guild_id AND election_id = NEW.election_id;
                IF proposal.election_id IS NULL OR NEW.actor_citizen_id = proposal.actor_citizen_id
                    OR NEW.occurred_at < proposal.occurred_at THEN
                    RAISE EXCEPTION 'Crisis confirmation requires a separate reviewer after the proposal'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for table in ("election_crisis_proposals", "election_crisis_confirmations"):
        op.execute(f"""
            CREATE TRIGGER election_crisis_attestation_guard BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION civitas_guard_crisis_attestation()
        """)
        op.execute(f"""
            CREATE TRIGGER election_crisis_attestation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
        """)
    op.execute("""
        CREATE FUNCTION civitas_can_open_after_crisis(
            target_guild_id bigint, target_office text, new_election_id bigint, new_opens_at timestamptz,
            retry_parent_id bigint
        ) RETURNS boolean
        LANGUAGE plpgsql AS $$
        BEGIN
            IF retry_parent_id IS NOT NULL THEN
                RETURN false;
            END IF;
            PERFORM id FROM guilds WHERE id = target_guild_id FOR NO KEY UPDATE;
            RETURN EXISTS (
                SELECT 1 FROM elections AS crisis
                JOIN election_crisis_confirmations AS confirmation ON confirmation.election_id = crisis.id
                WHERE crisis.guild_id = target_guild_id AND crisis.office = target_office
                    AND crisis.id < new_election_id AND confirmation.occurred_at <= new_opens_at
                    AND NOT EXISTS (
                        SELECT 1 FROM elections AS successor WHERE successor.guild_id = crisis.guild_id
                            AND successor.office = crisis.office AND successor.id > crisis.id
                    )
            );
        END;
        $$
    """)
    op.execute("DROP TRIGGER election_retry_guard ON elections")
    op.execute("""
        CREATE TRIGGER election_retry_guard_update BEFORE UPDATE OF retry_of_id ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_retry()
    """)
    op.execute("""
        CREATE TRIGGER election_retry_guard BEFORE INSERT ON elections
        FOR EACH ROW WHEN (NOT civitas_can_open_after_crisis(
            NEW.guild_id, NEW.office, NEW.id, NEW.opens_at, coalesce(NEW.retry_of_id, NEW.runoff_of_id)
        )) EXECUTE FUNCTION civitas_guard_election_retry()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM election_crisis_proposals) THEN
                RAISE EXCEPTION 'Cannot downgrade while election crisis resolution history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER election_retry_guard ON elections")
    op.execute("DROP TRIGGER election_retry_guard_update ON elections")
    op.execute("""
        CREATE TRIGGER election_retry_guard BEFORE INSERT OR UPDATE OF retry_of_id ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_retry()
    """)
    op.execute("DROP FUNCTION civitas_can_open_after_crisis(bigint, text, bigint, timestamptz, bigint)")
    op.drop_table("election_crisis_confirmations")
    op.drop_table("election_crisis_proposals")
    op.execute("DROP FUNCTION civitas_guard_crisis_attestation()")
