"""Add directed personal asset resales and validation of settlement and title history."""

from alembic import op

revision = "0019_asset_resale"
down_revision = "0018_runoff_candidate_attrition"
branch_labels = None
depends_on = None

_POSTINGS = (
    "'GENESIS', 'STARTER_GRANT', 'TRANSFER', 'NPC_PAYOUT', 'COMPANY_FEE', "
    "'ORDER_FUND', 'ORDER_SETTLE', 'ORDER_REFUND', 'ASSET_PURCHASE', 'STATE_CONTRACT_PAYOUT', "
    "'PRIVATE_JOB_FUND', 'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND'"
)
_MARKET_EXCLUSIONS = (
    "'ASSET_PURCHASE', 'STATE_CONTRACT_PAYOUT', 'PRIVATE_JOB_FUND', 'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND'"
)


def _set_posting_constraints(*, resale: bool) -> None:
    suffix = ", 'ASSET_RESALE'" if resale else ""
    op.drop_constraint("ck_financial_transactions_kind", "financial_transactions", type_="check")
    op.create_check_constraint(
        "ck_financial_transactions_kind", "financial_transactions", f"kind IN ({_POSTINGS}{suffix})",
    )
    op.execute("DROP TRIGGER market_validate_posting ON financial_transactions")
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER market_validate_posting
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind NOT IN ({_MARKET_EXCLUSIONS}{suffix}))
        EXECUTE FUNCTION civitas_validate_market_posting()
    """)


def _set_title_guards(*, resale: bool) -> None:
    # The primary receipt continues to prove the initial payment and title.
    # For personal assets only, any later validated receipt proves the current title.
    resale_transition = """
            IF OLD.status = 'SOLD' AND NEW.status = 'SOLD' AND OLD.owner_company_id IS NULL
                AND NEW.owner_company_id IS NULL AND OLD.owner_citizen_id <> NEW.owner_citizen_id
                AND EXISTS (
                    SELECT 1
                    FROM (
                        SELECT seller_citizen_id, buyer_citizen_id
                        FROM asset_resales
                        WHERE guild_id = OLD.guild_id AND asset_id = OLD.id AND status = 'COMPLETED'
                        ORDER BY id DESC LIMIT 1
                    ) AS latest
                    WHERE latest.seller_citizen_id = OLD.owner_citizen_id
                        AND latest.buyer_citizen_id = NEW.owner_citizen_id
                )
            THEN
                RETURN NEW;
            END IF;
    """ if resale else ""
    title_change_error = (
        "Only the primary sale or a completed resale can change an asset title"
        if resale else "Only the primary sale can change an asset title"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION civitas_guard_asset_title() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Asset title history is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'FOR_SALE' THEN
                    RAISE EXCEPTION 'A new asset must start as state property' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(OLD.id, OLD.guild_id, OLD.kind, OLD.name, OLD.purpose, OLD.price,
                    OLD.issued_by_citizen_id, OLD.issue_reason, OLD.issue_key, OLD.created_at)
                IS DISTINCT FROM
                ROW(NEW.id, NEW.guild_id, NEW.kind, NEW.name, NEW.purpose, NEW.price,
                    NEW.issued_by_citizen_id, NEW.issue_reason, NEW.issue_key, NEW.created_at)
            THEN
                RAISE EXCEPTION 'Asset issuance details are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status = 'FOR_SALE' AND NEW.status = 'SOLD' THEN
                RETURN NEW;
            END IF;
            {resale_transition}
            RAISE EXCEPTION '{title_change_error}'
                USING ERRCODE = 'check_violation';
        END;
        $$
    """)

    resale_declaration = "resale_buyer_id bigint;" if resale else ""
    resale_reader = """
            SELECT buyer_citizen_id INTO resale_buyer_id
            FROM asset_resales
            WHERE guild_id = property.guild_id AND asset_id = property.id AND status = 'COMPLETED'
            ORDER BY id DESC LIMIT 1;
    """ if resale else ""
    expected_owner = "coalesce(resale_buyer_id, purchase.actor_citizen_id)" if resale else "purchase.actor_citizen_id"
    op.execute(f"""
        CREATE OR REPLACE FUNCTION civitas_validate_asset_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            target_asset_id bigint;
            property assets%ROWTYPE;
            issuance asset_transfers%ROWTYPE;
            purchase asset_transfers%ROWTYPE;
            posting financial_transactions%ROWTYPE;
            {resale_declaration}
        BEGIN
            IF TG_TABLE_NAME = 'financial_transactions' THEN
                SELECT asset_id INTO target_asset_id FROM asset_transfers
                    WHERE transaction_id = NEW.id AND kind = 'PURCHASE';
                IF target_asset_id IS NULL THEN
                    RAISE EXCEPTION 'Asset posting requires a purchase receipt' USING ERRCODE = 'check_violation';
                END IF;
            ELSIF TG_TABLE_NAME = 'assets' THEN
                target_asset_id := NEW.id;
            ELSE
                target_asset_id := NEW.asset_id;
            END IF;

            SELECT * INTO STRICT property FROM assets WHERE id = target_asset_id;
            SELECT * INTO issuance FROM asset_transfers WHERE asset_id = property.id AND kind = 'ISSUE';
            IF issuance.id IS NULL OR issuance.actor_citizen_id <> property.issued_by_citizen_id
                OR issuance.request_key <> property.issue_key OR issuance.reason <> property.issue_reason
                OR issuance.created_at <> property.created_at
            THEN
                RAISE EXCEPTION 'State issuance requires its audited title record' USING ERRCODE = 'check_violation';
            END IF;

            SELECT * INTO purchase FROM asset_transfers WHERE asset_id = property.id AND kind = 'PURCHASE';
            IF property.status = 'FOR_SALE' AND purchase.id IS NULL THEN
                RETURN NULL;
            END IF;
            {resale_reader}
            IF property.status <> 'SOLD' OR purchase.id IS NULL OR purchase.price <> property.price
                OR purchase.created_at < property.created_at
                OR property.owner_company_id IS DISTINCT FROM purchase.buyer_company_id
                OR (purchase.buyer_company_id IS NULL AND property.owner_citizen_id IS DISTINCT FROM {expected_owner})
                OR (purchase.buyer_company_id IS NOT NULL AND property.owner_citizen_id IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Current title and purchase receipt must agree' USING ERRCODE = 'check_violation';
            END IF;

            SELECT * INTO STRICT posting FROM financial_transactions WHERE id = purchase.transaction_id;
            IF posting.kind <> 'ASSET_PURCHASE' OR posting.idempotency_key <> 'asset_purchase:' || property.id
                OR posting.actor_citizen_id IS DISTINCT FROM purchase.actor_citizen_id
                OR posting.reason <> purchase.reason OR posting.entry_count <> 2
                OR NOT EXISTS (
                    SELECT 1 FROM ledger_entries AS entry
                    JOIN financial_accounts AS a ON a.id = entry.account_id
                    WHERE entry.transaction_id = posting.id AND a.id = purchase.buyer_account_id
                        AND entry.delta = -property.price
                        AND (
                            (purchase.buyer_company_id IS NULL AND a.kind = 'PERSONAL'
                                AND a.citizen_id = purchase.actor_citizen_id)
                            OR (purchase.buyer_company_id IS NOT NULL AND a.kind = 'COMPANY'
                                AND a.organization_id = purchase.buyer_company_id AND EXISTS (
                                    SELECT 1 FROM organizations
                                    WHERE id = purchase.buyer_company_id
                                        AND owner_citizen_id = purchase.actor_citizen_id
                                ))
                        )
                ) OR NOT EXISTS (
                    SELECT 1 FROM ledger_entries AS entry
                    JOIN financial_accounts AS a ON a.id = entry.account_id
                    WHERE entry.transaction_id = posting.id AND a.kind = 'TREASURY'
                        AND entry.delta = property.price
                )
            THEN
                RAISE EXCEPTION 'Asset purchase must move the agreed price from the owner to the treasury'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
    """)


def _create_resales() -> None:
    op.execute("""
        CREATE TABLE asset_resales (
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_id bigint NOT NULL,
            asset_id bigint NOT NULL,
            seller_citizen_id bigint NOT NULL,
            buyer_citizen_id bigint NOT NULL,
            price bigint NOT NULL,
            tax_bp integer NOT NULL,
            request_key varchar(128) NOT NULL,
            created_at timestamptz NOT NULL,
            status varchar(16) NOT NULL,
            transaction_id bigint,
            closed_at timestamptz,
            CONSTRAINT uq_asset_resales_request UNIQUE (guild_id, request_key),
            CONSTRAINT uq_asset_resales_posting UNIQUE (transaction_id),
            CONSTRAINT fk_asset_resales_asset FOREIGN KEY (guild_id, asset_id)
                REFERENCES assets(guild_id, id),
            CONSTRAINT fk_asset_resales_seller FOREIGN KEY (guild_id, seller_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT fk_asset_resales_buyer FOREIGN KEY (guild_id, buyer_citizen_id)
                REFERENCES citizens(guild_id, id),
            CONSTRAINT fk_asset_resales_posting FOREIGN KEY (guild_id, transaction_id)
                REFERENCES financial_transactions(guild_id, id),
            CONSTRAINT ck_asset_resales_terms CHECK (
                price > 0 AND tax_bp BETWEEN 0 AND 9999 AND seller_citizen_id <> buyer_citizen_id
                AND length(btrim(request_key)) > 0
            ),
            CONSTRAINT ck_asset_resales_status CHECK (
                (status = 'OPEN' AND closed_at IS NULL AND transaction_id IS NULL) OR
                (status = 'CANCELLED' AND closed_at IS NOT NULL AND transaction_id IS NULL) OR
                (status = 'COMPLETED' AND closed_at IS NOT NULL AND transaction_id IS NOT NULL)
            ),
            CONSTRAINT ck_asset_resales_closure_time CHECK (closed_at >= created_at)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_asset_resales_open_asset ON asset_resales (guild_id, asset_id) WHERE status = 'OPEN'
    """)
    op.execute("""
        CREATE INDEX ix_asset_resales_completed_asset ON asset_resales (guild_id, asset_id, id) WHERE status = 'COMPLETED'
    """)
    op.create_index("ix_asset_resales_seller", "asset_resales", ["guild_id", "seller_citizen_id", "id"])
    op.create_index("ix_asset_resales_buyer", "asset_resales", ["guild_id", "buyer_citizen_id", "id"])


def _install_resale_guards() -> None:
    op.execute("""
        CREATE FUNCTION civitas_guard_asset_resale_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            current_owner bigint;
            previous_buyer bigint;
            previous_id bigint;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Asset resale history is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'OPEN' THEN
                    RAISE EXCEPTION 'An asset resale must start with an open offer' USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                IF OLD.status <> 'OPEN' OR NEW.status NOT IN ('COMPLETED', 'CANCELLED') OR
                    ROW(OLD.id, OLD.guild_id, OLD.asset_id, OLD.seller_citizen_id, OLD.buyer_citizen_id,
                        OLD.price, OLD.tax_bp, OLD.request_key, OLD.created_at)
                    IS DISTINCT FROM
                    ROW(NEW.id, NEW.guild_id, NEW.asset_id, NEW.seller_citizen_id, NEW.buyer_citizen_id,
                        NEW.price, NEW.tax_bp, NEW.request_key, NEW.created_at)
                THEN
                    RAISE EXCEPTION 'Asset resale terms and closed offers are immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.status = 'CANCELLED' THEN
                    RETURN NEW;
                END IF;
            END IF;
            SELECT owner_citizen_id INTO current_owner FROM assets
                WHERE guild_id = NEW.guild_id AND id = NEW.asset_id AND status = 'SOLD'
                FOR NO KEY UPDATE;
            SELECT id, buyer_citizen_id INTO previous_id, previous_buyer FROM asset_resales
                WHERE guild_id = NEW.guild_id AND asset_id = NEW.asset_id AND status = 'COMPLETED'
                ORDER BY id DESC LIMIT 1;
            IF current_owner IS DISTINCT FROM NEW.seller_citizen_id
                OR (previous_id IS NOT NULL AND (previous_id >= NEW.id OR previous_buyer <> current_owner))
            THEN
                RAISE EXCEPTION 'An asset resale requires the current personal owner'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER asset_resales_guard_transition
        BEFORE INSERT OR UPDATE OR DELETE ON asset_resales
        FOR EACH ROW EXECUTE FUNCTION civitas_guard_asset_resale_transition()
    """)
    op.execute("""
        CREATE TRIGGER asset_resales_no_truncate BEFORE TRUNCATE ON asset_resales
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
    """)
    op.execute("""
        CREATE FUNCTION civitas_validate_asset_resale() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            completed asset_resales%ROWTYPE;
            posting financial_transactions%ROWTYPE;
            expected_tax bigint;
            latest_buyer bigint;
        BEGIN
            IF TG_TABLE_NAME = 'financial_transactions' THEN
                SELECT * INTO completed FROM asset_resales WHERE transaction_id = NEW.id;
                IF completed.id IS NULL THEN
                    RAISE EXCEPTION 'Asset resale posting requires a completed offer' USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                completed := NEW;
            END IF;
            SELECT * INTO STRICT posting FROM financial_transactions WHERE id = completed.transaction_id;
            expected_tax := div(completed.price::numeric * completed.tax_bp, 10000)::bigint;
            IF posting.kind <> 'ASSET_RESALE' OR posting.idempotency_key <> 'asset_resale:' || completed.id
                OR posting.actor_citizen_id IS DISTINCT FROM completed.buyer_citizen_id
                OR posting.reason <> 'asset_resale'
                OR posting.entry_count <> (CASE WHEN expected_tax = 0 THEN 2 ELSE 3 END)
                OR NOT EXISTS (
                    SELECT 1 FROM ledger_entries AS entry
                    JOIN financial_accounts AS a ON a.id = entry.account_id
                    WHERE entry.transaction_id = posting.id AND entry.delta = -completed.price
                        AND a.kind = 'PERSONAL' AND a.citizen_id = completed.buyer_citizen_id
                ) OR NOT EXISTS (
                    SELECT 1 FROM ledger_entries AS entry
                    JOIN financial_accounts AS a ON a.id = entry.account_id
                    WHERE entry.transaction_id = posting.id AND entry.delta = completed.price - expected_tax
                        AND a.kind = 'PERSONAL' AND a.citizen_id = completed.seller_citizen_id
                ) OR (expected_tax > 0 AND NOT EXISTS (
                    SELECT 1 FROM ledger_entries AS entry
                    JOIN financial_accounts AS a ON a.id = entry.account_id
                    WHERE entry.transaction_id = posting.id AND entry.delta = expected_tax AND a.kind = 'TREASURY'
                ))
            THEN
                RAISE EXCEPTION 'Asset resale posting must match the offer and its tax'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT buyer_citizen_id INTO STRICT latest_buyer FROM asset_resales
                WHERE guild_id = completed.guild_id AND asset_id = completed.asset_id AND status = 'COMPLETED'
                ORDER BY id DESC LIMIT 1;
            IF NOT EXISTS (
                SELECT 1 FROM assets WHERE guild_id = completed.guild_id AND id = completed.asset_id
                    AND status = 'SOLD' AND owner_citizen_id = latest_buyer
            ) THEN
                RAISE EXCEPTION 'Completed asset resale requires a matching new title' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER asset_resales_validate_payment
        AFTER UPDATE ON asset_resales DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.status = 'COMPLETED')
        EXECUTE FUNCTION civitas_validate_asset_resale()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER asset_resales_validate_posting
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind = 'ASSET_RESALE')
        EXECUTE FUNCTION civitas_validate_asset_resale()
    """)


def upgrade() -> None:
    _create_resales()
    _set_posting_constraints(resale=True)
    _set_title_guards(resale=True)
    _install_resale_guards()


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM asset_resales) THEN
                RAISE EXCEPTION 'Cannot downgrade assets while resale history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER asset_resales_validate_posting ON financial_transactions")
    _set_title_guards(resale=False)
    _set_posting_constraints(resale=False)
    op.drop_table("asset_resales")
    op.execute("DROP FUNCTION civitas_validate_asset_resale()")
    op.execute("DROP FUNCTION civitas_guard_asset_resale_transition()")
