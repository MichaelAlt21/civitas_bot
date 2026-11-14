"""Reserve each canonical poll source for one election and bound scheduled terms."""

from alembic import op

revision = "0011_election_safeguards"
down_revision = "0010_private_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE election_poll_sources (
            guild_id bigint NOT NULL,
            source_url text NOT NULL,
            election_id bigint NOT NULL,
            PRIMARY KEY (guild_id, source_url),
            CONSTRAINT fk_election_poll_sources_election FOREIGN KEY (guild_id, election_id)
                REFERENCES elections(guild_id, id)
        )
    """)
    op.execute("""
        CREATE FUNCTION civitas_election_source_key(source text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT AS $$
            SELECT 'https://' || lower(regexp_replace(parts[1], '[:](0*443)?$', ''))
                || regexp_replace(parts[2], '/+$', '')
                || CASE WHEN query = '' THEN '' ELSE '?' || query END
            FROM regexp_match(source, '^https://([^/?#]+)([^?#]*)(?:[?]([^#]*))?(?:#.*)?$', 'i')
                AS matched(parts)
            CROSS JOIN LATERAL (SELECT coalesce(parts[3], '') AS query) AS normalized
        $$
    """)
    # Legacy reuses remain visible in their immutable protocols. The original
    # earliest recorded association owns the key for all future submissions.
    op.execute("""
        INSERT INTO election_poll_sources (guild_id, source_url, election_id)
        SELECT DISTINCT ON (guild_id, source_key) guild_id, source_key, election_id
        FROM (
            SELECT guild_id, civitas_election_source_key(source_url) AS source_key,
                   election_id, recorded_at, id
            FROM election_protocols
        ) AS recorded
        WHERE source_key IS NOT NULL
        ORDER BY guild_id, source_key, recorded_at, id
    """)
    op.execute("""
        CREATE TRIGGER election_poll_sources_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON election_poll_sources
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)
    op.execute("""
        CREATE FUNCTION civitas_guard_election_protocol_source() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE source_key text;
        BEGIN
            source_key := civitas_election_source_key(NEW.source_url);
            IF source_key IS NULL OR NOT EXISTS (
                SELECT 1 FROM election_poll_sources
                WHERE guild_id = NEW.guild_id AND election_id = NEW.election_id AND source_url = source_key
            ) THEN
                RAISE EXCEPTION 'Poll source belongs to another election or has not been bound'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER election_protocol_bound_source BEFORE INSERT ON election_protocols
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_election_protocol_source()
    """)
    op.execute("""
        CREATE FUNCTION civitas_guard_future_office_election() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM id FROM guilds WHERE id = NEW.guild_id FOR NO KEY UPDATE;
            IF EXISTS (
                SELECT 1 FROM public_offices WHERE guild_id = NEW.guild_id AND office = NEW.office
                    AND starts_at > NEW.opens_at
            ) THEN
                RAISE EXCEPTION 'An office already has a future mandate' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER election_future_term_guard BEFORE INSERT ON elections
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_future_office_election()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER election_future_term_guard ON elections")
    op.execute("DROP FUNCTION civitas_guard_future_office_election()")
    op.execute("DROP TRIGGER election_protocol_bound_source ON election_protocols")
    op.execute("DROP FUNCTION civitas_guard_election_protocol_source()")
    op.execute("DROP FUNCTION civitas_election_source_key(text)")
    op.drop_table("election_poll_sources")
