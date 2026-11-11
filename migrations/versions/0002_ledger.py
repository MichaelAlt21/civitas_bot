"""Create the double-entry ledger and its PostgreSQL safeguards."""

import sqlalchemy as sa
from alembic import op

revision = "0002_ledger"
down_revision = "0001_citizenship"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_citizens_guild_id", "citizens", ["guild_id", "id"])
    op.create_table(
        "financial_accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("citizen_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "guild_id", "kind", "citizen_id",
            name="uq_financial_accounts_owner",
            postgresql_nulls_not_distinct=True,
        ),
        sa.UniqueConstraint("guild_id", "id", name="uq_financial_accounts_guild_id"),
        sa.ForeignKeyConstraint(
            ["guild_id", "citizen_id"],
            ["citizens.guild_id", "citizens.id"],
            name="fk_financial_accounts_citizen",
        ),
        sa.CheckConstraint(
            "(kind = 'PERSONAL' AND citizen_id IS NOT NULL) OR "
            "(kind IN ('ISSUER', 'TREASURY') AND citizen_id IS NULL)",
            name="ck_financial_accounts_kind_owner",
        ),
    )
    op.create_table(
        "financial_transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("actor_citizen_id", sa.BigInteger()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "guild_id", "kind", "idempotency_key", name="uq_financial_transactions_operation"
        ),
        sa.UniqueConstraint("guild_id", "id", name="uq_financial_transactions_guild_id"),
        sa.ForeignKeyConstraint(
            ["guild_id", "actor_citizen_id"],
            ["citizens.guild_id", "citizens.id"],
            name="fk_financial_transactions_actor",
        ),
        sa.CheckConstraint(
            "kind IN ('GENESIS', 'STARTER_GRANT', 'TRANSFER')",
            name="ck_financial_transactions_kind",
        ),
        sa.CheckConstraint("entry_count >= 2", name="ck_financial_transactions_entry_count"),
        sa.CheckConstraint(
            "length(idempotency_key) > 0 AND length(reason) > 0",
            name="ck_financial_transactions_audit_fields",
        ),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("transaction_id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["guild_id", "transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_ledger_entries_transaction",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "account_id"],
            ["financial_accounts.guild_id", "financial_accounts.id"],
            name="fk_ledger_entries_account",
        ),
        sa.CheckConstraint("delta <> 0", name="ck_ledger_entries_nonzero"),
    )
    op.create_index("ix_ledger_entries_account_transaction", "ledger_entries", ["account_id", "transaction_id"])
    op.execute("""
        CREATE FUNCTION civitas_reject_ledger_edit() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Ledger history is append-only' USING ERRCODE = 'check_violation';
        END;
        $$
    """)
    for table in ("financial_accounts", "financial_transactions", "ledger_entries"):
        op.execute(f"""
            CREATE TRIGGER ledger_no_edits
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_ledger_edit()
        """)

    # The immutable entry count also rejects balanced additions to a posting that
    # was already committed, even when no existing rows were changed.
    op.execute("""
        CREATE FUNCTION civitas_validate_ledger_posting() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            posting_id bigint;
            posting_kind text;
            posting_key text;
            expected_entries integer;
            actual_entries bigint;
            net numeric;
            issuer_entries bigint;
            issuer_debits bigint;
            treasury_credits bigint;
            personal_credit_owner bigint;
        BEGIN
            IF TG_TABLE_NAME = 'financial_transactions' THEN
                posting_id := NEW.id;
            ELSE
                posting_id := NEW.transaction_id;
            END IF;

            SELECT kind, idempotency_key, entry_count
              INTO STRICT posting_kind, posting_key, expected_entries
              FROM financial_transactions WHERE id = posting_id;

            SELECT count(*), coalesce(sum(entry.delta), 0),
                   count(*) FILTER (WHERE account.kind = 'ISSUER'),
                   count(*) FILTER (WHERE account.kind = 'ISSUER' AND entry.delta < 0),
                   count(*) FILTER (WHERE account.kind = 'TREASURY' AND entry.delta > 0),
                   max(account.citizen_id) FILTER (WHERE account.kind = 'PERSONAL' AND entry.delta > 0)
              INTO actual_entries, net, issuer_entries, issuer_debits, treasury_credits, personal_credit_owner
              FROM ledger_entries AS entry
              JOIN financial_accounts AS account ON account.id = entry.account_id
             WHERE entry.transaction_id = posting_id;

            IF actual_entries <> expected_entries OR net <> 0 THEN
                RAISE EXCEPTION 'Incomplete or unbalanced posting' USING ERRCODE = 'check_violation';
            END IF;

            IF posting_kind = 'GENESIS' AND (
                posting_key <> 'genesis' OR actual_entries <> 2
                OR issuer_debits <> 1 OR treasury_credits <> 1
            ) THEN
                RAISE EXCEPTION 'Invalid genesis posting' USING ERRCODE = 'check_violation';
            END IF;

            IF posting_kind = 'STARTER_GRANT' AND (
                actual_entries <> 2 OR issuer_debits <> 1 OR personal_credit_owner IS NULL
                OR posting_key <> 'starter_grant:' || personal_credit_owner
            ) THEN
                RAISE EXCEPTION 'Invalid starter grant' USING ERRCODE = 'check_violation';
            END IF;

            IF posting_kind = 'TRANSFER' AND issuer_entries <> 0 THEN
                RAISE EXCEPTION 'Transfer cannot use the issuer' USING ERRCODE = 'check_violation';
            END IF;

            -- Match the repository lock order, including for direct inserts.
            PERFORM account.id
              FROM financial_accounts AS account
              JOIN ledger_entries AS entry ON entry.account_id = account.id
             WHERE entry.transaction_id = posting_id
             ORDER BY account.id
               FOR NO KEY UPDATE OF account;

            IF EXISTS (
                SELECT 1
                  FROM ledger_entries AS touched
                  JOIN financial_accounts AS account ON account.id = touched.account_id
                  JOIN ledger_entries AS history ON history.account_id = account.id
                 WHERE touched.transaction_id = posting_id
                 GROUP BY account.id, account.kind
                HAVING sum(history.delta) NOT BETWEEN
                    CASE WHEN account.kind = 'ISSUER' THEN -9223372036854775808 ELSE 0 END
                    AND 9223372036854775807
            ) THEN
                RAISE EXCEPTION 'Ledger balance outside its permitted range' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    for table in ("financial_transactions", "ledger_entries"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER ledger_validate_posting
            AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION civitas_validate_ledger_posting()
        """)


def downgrade() -> None:
    op.drop_table("ledger_entries")
    op.drop_table("financial_transactions")
    op.drop_table("financial_accounts")
    op.execute("DROP FUNCTION civitas_validate_ledger_posting()")
    op.execute("DROP FUNCTION civitas_reject_ledger_edit()")
    op.drop_constraint("uq_citizens_guild_id", "citizens", type_="unique")
