"""Validate release of court fines after the appeal is decided."""

from alembic import op

revision = "0034_appealed_court_fine"
down_revision = "0033_court_fine_escrow"
branch_labels = None
depends_on = None

_TABLE = "financial_transactions"
_KIND = "ck_financial_transactions_kind"
_BASE_KINDS = (
    "'GENESIS', 'STARTER_GRANT', 'TRANSFER', 'NPC_PAYOUT', 'COMPANY_FEE', 'ORDER_FUND', 'ORDER_SETTLE', "
    "'ORDER_REFUND', 'ASSET_PURCHASE', 'ASSET_RESALE', 'STATE_CONTRACT_PAYOUT', "
    "'PRIVATE_JOB_FUND', 'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND', 'COURT_FINE_HOLD'"
)
_TERMINAL = "'COURT_FINE_REFUND', 'COURT_FINE_SETTLE'"


def _configure_kinds(*, closing: bool) -> None:
    kinds = _BASE_KINDS + (f", {_TERMINAL}" if closing else "")
    excluded = (
        "'ASSET_PURCHASE', 'ASSET_RESALE', 'STATE_CONTRACT_PAYOUT', "
        "'PRIVATE_JOB_FUND', 'PRIVATE_JOB_PAY', 'PRIVATE_JOB_REFUND', 'COURT_FINE_HOLD'"
    ) + (f", {_TERMINAL}" if closing else "")
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({kinds})")
    op.execute("DROP TRIGGER market_validate_posting ON financial_transactions")
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER market_validate_posting
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind NOT IN ({excluded})) EXECUTE FUNCTION civitas_validate_market_posting()
    """)


def upgrade() -> None:
    _configure_kinds(closing=True)
    op.execute("""
        CREATE FUNCTION civitas_validate_court_fine_close() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.entry_count <> 2 OR NOT EXISTS (
                SELECT 1 FROM court_act_decisions AS decision
                JOIN court_cases AS court_case ON court_case.id = decision.case_id
                JOIN court_appeal_confirmations AS appeal
                    ON appeal.guild_id = decision.guild_id AND appeal.case_id = decision.case_id
                JOIN financial_transactions AS hold
                    ON hold.guild_id = decision.guild_id AND hold.kind = 'COURT_FINE_HOLD'
                    AND hold.idempotency_key = 'court_fine_hold:' || decision.case_id
                JOIN ledger_entries AS debit ON debit.transaction_id = NEW.id
                JOIN financial_accounts AS escrow ON escrow.id = debit.account_id
                    AND escrow.court_case_id = decision.case_id
                JOIN ledger_entries AS credit ON credit.transaction_id = NEW.id
                JOIN financial_accounts AS destination ON destination.id = credit.account_id
                WHERE decision.guild_id = NEW.guild_id AND NEW.reason = decision.reason
                    AND NEW.idempotency_key = lower(NEW.kind) || ':' || decision.case_id
                    AND NEW.actor_citizen_id = appeal.confirmed_by_citizen_id
                    AND debit.delta = -decision.fine_amount AND credit.delta = decision.fine_amount
                    AND (
                        (NEW.kind = 'COURT_FINE_REFUND' AND appeal.outcome = 'OVERTURNED'
                            AND destination.kind = 'PERSONAL'
                            AND destination.citizen_id = court_case.respondent_citizen_id)
                        OR (NEW.kind = 'COURT_FINE_SETTLE' AND appeal.outcome IN ('UPHELD', 'NO_QUORUM')
                            AND destination.kind = 'TREASURY')
                    )
            ) THEN
                RAISE EXCEPTION 'Court fine close must match the confirmed appeal' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;
    """)
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER court_fine_validate_close
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind IN ({_TERMINAL})) EXECUTE FUNCTION civitas_validate_court_fine_close()
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE financial_transactions IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM financial_transactions WHERE kind IN ({_TERMINAL})) THEN
                RAISE EXCEPTION 'Cannot downgrade while closed court fines exist';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER court_fine_validate_close ON financial_transactions")
    op.execute("DROP FUNCTION civitas_validate_court_fine_close()")
    _configure_kinds(closing=False)
