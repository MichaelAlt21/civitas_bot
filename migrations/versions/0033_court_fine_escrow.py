"""Add a dedicated ledger escrow for each judicial fine."""

from alembic import op
from sqlalchemy import BigInteger, Column

revision = "0033_court_fine_escrow"
down_revision = "0032_court_sentence"
branch_labels = None
depends_on = None

_ACCOUNTS = "financial_accounts"
_TRANSACTIONS = "financial_transactions"
_OWNER = "uq_financial_accounts_owner"
_OWNER_CHECK = "ck_financial_accounts_kind_owner"
_KIND_CHECK = "ck_financial_transactions_kind"
_OWNERS = ["guild_id", "kind", "citizen_id", "organization_id", "order_id", "private_job_id"]
_PREVIOUS_OWNER = (
    "(private_job_id IS NULL AND ((kind = 'PERSONAL' AND citizen_id IS NOT NULL AND organization_id IS NULL "
    "AND order_id IS NULL) OR (kind IN ('ISSUER', 'TREASURY') AND citizen_id IS NULL AND organization_id IS "
    "NULL AND order_id IS NULL) OR (kind = 'COMPANY' AND citizen_id IS NULL AND organization_id IS NOT NULL "
    "AND order_id IS NULL) OR (kind = 'ESCROW' AND citizen_id IS NULL AND organization_id IS NULL AND "
    "order_id IS NOT NULL))) OR (kind = 'ESCROW' AND citizen_id IS NULL AND organization_id IS NULL AND "
    "order_id IS NULL AND private_job_id IS NOT NULL)"
)
_PREVIOUS_KINDS = (
    "kind IN ('GENESIS', 'STARTER_GRANT', 'TRANSFER', 'NPC_PAYOUT', 'COMPANY_FEE', 'ORDER_FUND', "
    "'ORDER_SETTLE', 'ORDER_REFUND', 'ASSET_PURCHASE', 'ASSET_RESALE', 'STATE_CONTRACT_PAYOUT', "
    "'PRIVATE_JOB_FUND', 'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND')"
)


def _market_trigger(*, court_fines: bool) -> None:
    excluded = (
        "'ASSET_PURCHASE', 'STATE_CONTRACT_PAYOUT', 'PRIVATE_JOB_FUND', "
        "'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND', 'ASSET_RESALE'"
    )
    if court_fines:
        excluded += ", 'COURT_FINE_HOLD'"
    op.execute("DROP TRIGGER market_validate_posting ON financial_transactions")
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER market_validate_posting
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind NOT IN ({excluded})) EXECUTE FUNCTION civitas_validate_market_posting()
    """)


def upgrade() -> None:
    op.add_column(_ACCOUNTS, Column("court_case_id", BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_financial_accounts_court_case", _ACCOUNTS, "court_act_decisions",
        ["guild_id", "court_case_id"], ["guild_id", "case_id"],
    )
    op.drop_constraint(_OWNER, _ACCOUNTS, type_="unique")
    op.create_unique_constraint(_OWNER, _ACCOUNTS, [*_OWNERS, "court_case_id"], postgresql_nulls_not_distinct=True)
    op.drop_constraint(_OWNER_CHECK, _ACCOUNTS, type_="check")
    op.create_check_constraint(
        _OWNER_CHECK, _ACCOUNTS,
        f"(court_case_id IS NULL AND ({_PREVIOUS_OWNER})) OR "
        "(kind = 'ESCROW' AND court_case_id IS NOT NULL AND citizen_id IS NULL "
        "AND organization_id IS NULL AND order_id IS NULL AND private_job_id IS NULL)",
    )
    op.drop_constraint(_KIND_CHECK, _TRANSACTIONS, type_="check")
    op.create_check_constraint(_KIND_CHECK, _TRANSACTIONS, _PREVIOUS_KINDS[:-1] + ", 'COURT_FINE_HOLD')")
    _market_trigger(court_fines=True)
    op.execute("""
        CREATE FUNCTION civitas_validate_court_fine_hold() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.entry_count <> 2 OR NOT EXISTS (
                SELECT 1 FROM court_act_decisions AS decision
                JOIN court_cases AS court_case ON court_case.id = decision.case_id
                JOIN financial_accounts AS escrow
                    ON escrow.guild_id = decision.guild_id AND escrow.court_case_id = decision.case_id
                JOIN financial_accounts AS personal
                    ON personal.guild_id = decision.guild_id AND personal.kind = 'PERSONAL'
                    AND personal.citizen_id = court_case.respondent_citizen_id
                JOIN ledger_entries AS credit ON credit.transaction_id = NEW.id AND credit.account_id = escrow.id
                JOIN ledger_entries AS debit ON debit.transaction_id = NEW.id AND debit.account_id = personal.id
                WHERE decision.guild_id = NEW.guild_id
                    AND NEW.idempotency_key = 'court_fine_hold:' || decision.case_id
                    AND NEW.actor_citizen_id = decision.judge_citizen_id AND NEW.reason = decision.reason
                    AND credit.delta = decision.fine_amount AND debit.delta = -decision.fine_amount
            ) THEN
                RAISE EXCEPTION 'Court fine hold must match the judgment' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER court_fine_validate_hold
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind = 'COURT_FINE_HOLD') EXECUTE FUNCTION civitas_validate_court_fine_hold()
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE financial_accounts, financial_transactions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM financial_accounts WHERE court_case_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while court fine escrow exists';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER court_fine_validate_hold ON financial_transactions")
    op.execute("DROP FUNCTION civitas_validate_court_fine_hold()")
    _market_trigger(court_fines=False)
    op.drop_constraint(_KIND_CHECK, _TRANSACTIONS, type_="check")
    op.create_check_constraint(_KIND_CHECK, _TRANSACTIONS, _PREVIOUS_KINDS)
    op.drop_constraint(_OWNER_CHECK, _ACCOUNTS, type_="check")
    op.create_check_constraint(_OWNER_CHECK, _ACCOUNTS, _PREVIOUS_OWNER)
    op.drop_constraint(_OWNER, _ACCOUNTS, type_="unique")
    op.create_unique_constraint(_OWNER, _ACCOUNTS, _OWNERS, postgresql_nulls_not_distinct=True)
    op.drop_constraint("fk_financial_accounts_court_case", _ACCOUNTS, type_="foreignkey")
    op.drop_column(_ACCOUNTS, "court_case_id")
