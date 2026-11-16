"""Persist filings, public poll snapshots and independent appeal confirmations."""

from alembic import op

revision = "0030_court_appeals"
down_revision = "0029_act_suspension"
branch_labels = None
depends_on = None

_TABLES = ("court_appeals", "court_appeal_openings", "court_appeal_tallies", "court_appeal_confirmations")


def upgrade() -> None:
    op.create_unique_constraint("uq_court_act_decisions_guild_case", "court_act_decisions", ["guild_id", "case_id"])
    op.execute("""
        CREATE TABLE court_appeals (
            guild_id bigint NOT NULL,
            case_id bigint NOT NULL,
            appellant_citizen_id bigint NOT NULL,
            filed_at timestamptz NOT NULL,
            PRIMARY KEY (guild_id, case_id),
            CONSTRAINT fk_court_appeals_decision FOREIGN KEY (guild_id, case_id)
                REFERENCES court_act_decisions(guild_id, case_id),
            CONSTRAINT fk_court_appeals_appellant FOREIGN KEY (guild_id, appellant_citizen_id)
                REFERENCES citizens(guild_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE court_appeal_openings (
            guild_id bigint NOT NULL,
            case_id bigint NOT NULL,
            opened_by_citizen_id bigint NOT NULL,
            opened_at timestamptz NOT NULL,
            source_url text NOT NULL,
            eligible_user_ids numeric(20, 0)[] NOT NULL,
            quorum integer NOT NULL,
            closes_at timestamptz NOT NULL,
            PRIMARY KEY (guild_id, case_id),
            CONSTRAINT fk_court_appeal_openings_appeal FOREIGN KEY (guild_id, case_id)
                REFERENCES court_appeals(guild_id, case_id),
            CONSTRAINT fk_court_appeal_openings_actor FOREIGN KEY (guild_id, opened_by_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT uq_court_appeal_openings_source UNIQUE (guild_id, source_url),
            CONSTRAINT ck_court_appeal_openings_rules CHECK (
                cardinality(eligible_user_ids) > 0 AND quorum > 0
                AND length(source_url) BETWEEN 1 AND 500
                AND closes_at = opened_at + interval '48 hours'
            )
        )
    """)
    op.execute("""
        CREATE TABLE court_appeal_tallies (
            guild_id bigint NOT NULL,
            case_id bigint NOT NULL,
            round_number integer NOT NULL,
            participants integer NOT NULL,
            votes_to_cancel integer NOT NULL,
            recorded_by_citizen_id bigint NOT NULL,
            recorded_at timestamptz NOT NULL,
            PRIMARY KEY (guild_id, case_id, round_number),
            CONSTRAINT fk_court_appeal_tallies_ballot FOREIGN KEY (guild_id, case_id)
                REFERENCES court_appeal_openings(guild_id, case_id),
            CONSTRAINT fk_court_appeal_tallies_actor FOREIGN KEY (guild_id, recorded_by_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_court_appeal_tallies_votes CHECK (
                round_number IN (1, 2) AND participants >= 0 AND votes_to_cancel BETWEEN 0 AND participants
            )
        )
    """)
    op.execute("""
        CREATE TABLE court_appeal_confirmations (
            guild_id bigint NOT NULL,
            case_id bigint NOT NULL,
            round_number integer NOT NULL,
            confirmed_by_citizen_id bigint NOT NULL,
            confirmed_at timestamptz NOT NULL,
            outcome varchar(16) NOT NULL,
            extended_until timestamptz,
            PRIMARY KEY (guild_id, case_id, round_number),
            CONSTRAINT fk_court_appeal_confirmations_tally FOREIGN KEY (guild_id, case_id, round_number)
                REFERENCES court_appeal_tallies(guild_id, case_id, round_number),
            CONSTRAINT fk_court_appeal_confirmations_actor FOREIGN KEY (guild_id, confirmed_by_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_court_appeal_confirmations_outcome CHECK (
                (outcome = 'EXTENDED' AND round_number = 1 AND extended_until IS NOT NULL
                 AND extended_until = confirmed_at + interval '24 hours')
                OR (outcome IN ('OVERTURNED', 'UPHELD', 'NO_QUORUM') AND extended_until IS NULL)
            )
        )
    """)
    op.execute("""
        CREATE FUNCTION civitas_check_court_appeal_filing() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE decision_time timestamptz;
        BEGIN
            SELECT decided_at INTO decision_time FROM court_act_decisions
            WHERE guild_id = NEW.guild_id AND case_id = NEW.case_id;
            IF NOT FOUND OR NEW.filed_at NOT BETWEEN decision_time AND decision_time + interval '24 hours' THEN
                RAISE EXCEPTION 'Court appeal filed outside the 24-hour window' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION civitas_check_court_appeal_opening() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE filing_time timestamptz;
        BEGIN
            SELECT filed_at INTO filing_time FROM court_appeals
            WHERE guild_id = NEW.guild_id AND case_id = NEW.case_id;
            IF NOT FOUND OR NEW.opened_at < filing_time THEN
                RAISE EXCEPTION 'Court appeal ballot precedes its filing' USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM unnest(NEW.eligible_user_ids) AS voter(user_id)
                WHERE voter.user_id IS NULL OR voter.user_id NOT BETWEEN 1 AND 18446744073709551615
            ) OR cardinality(NEW.eligible_user_ids) <> (
                SELECT count(DISTINCT voter.user_id) FROM unnest(NEW.eligible_user_ids) AS voter(user_id)
            ) THEN
                RAISE EXCEPTION 'Court appeal roster has invalid or repeated voters' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION civitas_check_court_appeal_tally() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            deadline timestamptz;
            eligible_count integer;
        BEGIN
            SELECT CASE WHEN NEW.round_number = 1 THEN ballot.closes_at ELSE extension.extended_until END,
                   cardinality(ballot.eligible_user_ids)
              INTO deadline, eligible_count
              FROM court_appeal_openings AS ballot
              LEFT JOIN court_appeal_confirmations AS extension
                ON (extension.guild_id, extension.case_id) = (ballot.guild_id, ballot.case_id)
               AND extension.round_number = 1 AND extension.outcome = 'EXTENDED'
             WHERE ballot.guild_id = NEW.guild_id AND ballot.case_id = NEW.case_id;
            IF NOT FOUND OR deadline IS NULL OR NEW.recorded_at < deadline OR NEW.participants > eligible_count THEN
                RAISE EXCEPTION 'Court appeal tally is outside the ballot rules' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION civitas_check_court_appeal_confirmation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            tally court_appeal_tallies%ROWTYPE;
            required_quorum integer;
            expected_outcome text;
        BEGIN
            SELECT * INTO tally FROM court_appeal_tallies
            WHERE guild_id = NEW.guild_id AND case_id = NEW.case_id AND round_number = NEW.round_number;
            IF NOT FOUND OR NEW.confirmed_by_citizen_id = tally.recorded_by_citizen_id
                OR NEW.confirmed_at < tally.recorded_at THEN
                RAISE EXCEPTION 'Court appeal tally requires an independent confirmation' USING ERRCODE = 'check_violation';
            END IF;
            SELECT quorum INTO required_quorum FROM court_appeal_openings
            WHERE guild_id = NEW.guild_id AND case_id = NEW.case_id;
            expected_outcome := CASE
                WHEN tally.participants < required_quorum AND NEW.round_number = 1 THEN 'EXTENDED'
                WHEN tally.participants < required_quorum THEN 'NO_QUORUM'
                WHEN tally.votes_to_cancel::bigint * 2 > tally.participants THEN 'OVERTURNED'
                ELSE 'UPHELD'
            END;
            IF NEW.outcome <> expected_outcome THEN
                RAISE EXCEPTION 'Court appeal outcome disagrees with the confirmed votes' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for table, event in zip(_TABLES, ("filing", "opening", "tally", "confirmation"), strict=True):
        op.execute(f"""
            CREATE TRIGGER {table}_rules BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION civitas_check_court_appeal_{event}()
        """)
        op.execute(f"""
            CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
        """)


def downgrade() -> None:
    op.execute(f"LOCK TABLE {', '.join(_TABLES)} IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM court_appeals) THEN
                RAISE EXCEPTION 'Cannot downgrade while court appeal history exists';
            END IF;
        END $$
    """)
    for table, event in reversed(tuple(zip(_TABLES, ("filing", "opening", "tally", "confirmation"), strict=True))):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION civitas_check_court_appeal_{event}()")
    op.drop_constraint("uq_court_act_decisions_guild_case", "court_act_decisions", type_="unique")
