"""Retire offers without changing funded orders or their original terms."""

from alembic import op

revision = "0014_market_offer_withdrawal"
down_revision = "0013_citizen_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE offer_withdrawals (
            offer_id bigint PRIMARY KEY,
            guild_id bigint NOT NULL,
            withdrawn_by_citizen_id bigint NOT NULL,
            withdrawn_at timestamptz NOT NULL,
            reason text NOT NULL,
            CONSTRAINT fk_offer_withdrawals_offer FOREIGN KEY (guild_id, offer_id)
                REFERENCES offers(guild_id, id),
            CONSTRAINT fk_offer_withdrawals_actor FOREIGN KEY (guild_id, withdrawn_by_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT ck_offer_withdrawals_reason CHECK (length(btrim(reason)) BETWEEN 1 AND 200)
        )
    """)
    op.execute("""
        CREATE TRIGGER offer_withdrawals_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON offer_withdrawals
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)
    op.execute("""
        CREATE FUNCTION civitas_guard_offer_availability() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            owner_id bigint;
            offered_at timestamptz;
        BEGIN
            SELECT company.owner_citizen_id, offer.created_at INTO owner_id, offered_at
              FROM offers AS offer
              JOIN organizations AS company ON company.guild_id = offer.guild_id AND company.id = offer.company_id
             WHERE offer.guild_id = NEW.guild_id AND offer.id = NEW.offer_id
             FOR NO KEY UPDATE OF offer;
            IF TG_TABLE_NAME = 'offer_withdrawals' THEN
                IF NEW.withdrawn_by_citizen_id IS DISTINCT FROM owner_id
                    OR NEW.withdrawn_at < offered_at THEN
                    RAISE EXCEPTION 'Only the offer owner can withdraw an existing offer'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF EXISTS (SELECT 1 FROM offer_withdrawals WHERE offer_id = NEW.offer_id) THEN
                RAISE EXCEPTION 'Cannot fund a withdrawn offer' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER offer_withdrawal_owner
        BEFORE INSERT ON offer_withdrawals
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_offer_availability()
    """)
    op.execute("""
        CREATE TRIGGER market_order_offer_available
        BEFORE INSERT ON orders
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_offer_availability()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM offer_withdrawals) THEN
                RAISE EXCEPTION 'Cannot downgrade while offer withdrawal history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER market_order_offer_available ON orders")
    op.drop_table("offer_withdrawals")
    op.execute("DROP FUNCTION civitas_guard_offer_availability()")
