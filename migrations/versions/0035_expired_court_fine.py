"""Allow settlement after the filing deadline when no appeal was filed."""

from alembic import op

revision = "0035_expired_court_fine"
down_revision = "0034_appealed_court_fine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION civitas_validate_court_fine_close() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.entry_count <> 2 OR NOT EXISTS (
                SELECT 1 FROM court_act_decisions AS decision
                JOIN court_cases AS court_case ON court_case.id = decision.case_id
                LEFT JOIN court_appeal_confirmations AS appeal
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
                    AND debit.delta = -decision.fine_amount AND credit.delta = decision.fine_amount
                    AND (
                        (NEW.kind = 'COURT_FINE_REFUND' AND appeal.outcome = 'OVERTURNED'
                            AND NEW.actor_citizen_id = appeal.confirmed_by_citizen_id
                            AND destination.kind = 'PERSONAL'
                            AND destination.citizen_id = court_case.respondent_citizen_id)
                        OR (NEW.kind = 'COURT_FINE_SETTLE' AND appeal.outcome IN ('UPHELD', 'NO_QUORUM')
                            AND NEW.actor_citizen_id = appeal.confirmed_by_citizen_id
                            AND destination.kind = 'TREASURY')
                        OR (NEW.kind = 'COURT_FINE_SETTLE' AND NEW.actor_citizen_id IS NULL
                            AND destination.kind = 'TREASURY'
                            AND NEW.created_at > decision.decided_at + interval '24 hours'
                            AND NOT EXISTS (
                                SELECT 1 FROM court_appeals AS filed
                                WHERE filed.guild_id = decision.guild_id AND filed.case_id = decision.case_id
                            ))
                    )
            ) THEN
                RAISE EXCEPTION 'Court fine close must match the confirmed appeal' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$;
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE financial_transactions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM financial_transactions
                WHERE kind = 'COURT_FINE_SETTLE' AND actor_citizen_id IS NULL
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while unappealed court settlements exist';
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION civitas_validate_court_fine_close() RETURNS trigger LANGUAGE plpgsql AS $$
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
