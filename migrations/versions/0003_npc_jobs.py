"""Persist NPC tasks, claim reviews, weekly budgets and payouts."""

import sqlalchemy as sa
from alembic import op

revision = "0003_npc_jobs"
down_revision = "0002_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _set_ledger_kinds(include_npc=True)
    op.create_table(
        "budget_periods",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("week_start_date", sa.Date(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overall_cap", sa.BigInteger(), nullable=False),
        sa.Column("spent_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("guild_id", "start_at", name="uq_budget_periods_guild_start"),
        sa.UniqueConstraint("guild_id", "week_start_date", name="uq_budget_periods_guild_week"),
        sa.UniqueConstraint("guild_id", "id", name="uq_budget_periods_guild_id"),
        sa.CheckConstraint("start_at < end_at", name="ck_budget_periods_interval"),
        sa.CheckConstraint(
            "EXTRACT(ISODOW FROM week_start_date) = 1", name="ck_budget_periods_week_monday"
        ),
        sa.CheckConstraint(
            "overall_cap >= 0 AND spent_total BETWEEN 0 AND overall_cap",
            name="ck_budget_periods_cap",
        ),
    )
    op.create_table(
        "budget_articles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("period_id", sa.BigInteger(), sa.ForeignKey("budget_periods.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("cap", sa.BigInteger(), nullable=False),
        sa.Column("personal_cap", sa.BigInteger(), nullable=False),
        sa.Column("spent", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("period_id", "code", name="uq_budget_articles_period_code"),
        sa.CheckConstraint(
            "personal_cap >= 0 AND spent BETWEEN 0 AND cap",
            name="ck_budget_articles_cap",
        ),
    )
    op.create_table(
        "work_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("reward", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by_citizen_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("guild_id", "id", name="uq_work_tasks_guild_id"),
        sa.ForeignKeyConstraint(
            ["guild_id", "published_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_tasks_publisher",
        ),
        sa.CheckConstraint("reward > 0", name="ck_work_tasks_reward"),
        sa.CheckConstraint("created_at < expires_at", name="ck_work_tasks_interval"),
        sa.CheckConstraint(
            "category IN ('industry', 'logistics', 'services', 'events')",
            name="ck_work_tasks_category",
        ),
    )
    op.create_table(
        "work_claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("citizen_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewer_citizen_id", sa.BigInteger()),
        sa.Column("review_reason", sa.Text()),
        sa.Column("payout_transaction_id", sa.BigInteger()),
        sa.Column("budget_period_id", sa.BigInteger()),
        sa.UniqueConstraint("task_id", name="uq_work_claims_task"),
        sa.UniqueConstraint("payout_transaction_id", name="uq_work_claims_payout"),
        sa.ForeignKeyConstraint(
            ["guild_id", "task_id"], ["work_tasks.guild_id", "work_tasks.id"],
            name="fk_work_claims_task",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_claims_citizen",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "reviewer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_work_claims_reviewer",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "budget_period_id"], ["budget_periods.guild_id", "budget_periods.id"],
            name="fk_work_claims_budget_period",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "payout_transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_work_claims_payout",
        ),
        sa.CheckConstraint(
            "status IN ('CLAIMED', 'SUBMITTED', 'PAID', 'REJECTED')",
            name="ck_work_claims_status",
        ),
        sa.CheckConstraint(
            "(status = 'CLAIMED' AND evidence IS NULL AND submitted_at IS NULL) OR "
            "(status <> 'CLAIMED' AND evidence IS NOT NULL AND submitted_at >= claimed_at "
            "AND submitted_at IS NOT NULL)",
            name="ck_work_claims_submission",
        ),
        sa.CheckConstraint(
            "(status IN ('PAID', 'REJECTED') AND reviewed_at IS NOT NULL "
            "AND reviewed_at >= submitted_at AND reviewer_citizen_id IS NOT NULL "
            "AND reviewer_citizen_id <> citizen_id) OR "
            "(status IN ('CLAIMED', 'SUBMITTED') AND reviewed_at IS NULL "
            "AND reviewer_citizen_id IS NULL AND review_reason IS NULL)",
            name="ck_work_claims_review",
        ),
        sa.CheckConstraint(
            "(status = 'PAID' AND budget_period_id IS NOT NULL AND payout_transaction_id IS NOT NULL) OR "
            "(status <> 'PAID' AND budget_period_id IS NULL AND payout_transaction_id IS NULL)",
            name="ck_work_claims_paid",
        ),
    )
    op.create_index("ix_work_claims_citizen_claimed", "work_claims", ["guild_id", "citizen_id", "claimed_at"])
    op.create_index("ix_work_claims_budget_citizen", "work_claims", ["budget_period_id", "citizen_id"])
    _install_payout_guards()


def _set_ledger_kinds(*, include_npc: bool) -> None:
    op.drop_constraint("ck_financial_transactions_kind", "financial_transactions", type_="check")
    kinds = "'GENESIS', 'STARTER_GRANT', 'TRANSFER'"
    if include_npc:
        kinds += ", 'NPC_PAYOUT'"
    op.create_check_constraint(
        "ck_financial_transactions_kind", "financial_transactions", f"kind IN ({kinds})"
    )


def _install_payout_guards() -> None:
    op.execute("""
        CREATE FUNCTION civitas_reject_npc_history_edit() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'NPC task and review history is immutable' USING ERRCODE = 'check_violation';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER npc_published_task_no_edits
        BEFORE UPDATE OR DELETE OR TRUNCATE ON work_tasks
        FOR EACH STATEMENT EXECUTE FUNCTION civitas_reject_npc_history_edit()
    """)
    op.execute("""
        CREATE TRIGGER npc_final_review_no_edits
        BEFORE UPDATE OR DELETE ON work_claims
        FOR EACH ROW WHEN (OLD.status IN ('PAID', 'REJECTED'))
        EXECUTE FUNCTION civitas_reject_npc_history_edit()
    """)
    op.execute("""
        CREATE FUNCTION civitas_validate_npc_payout() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            posting_id bigint;
            posting financial_transactions%ROWTYPE;
            claim work_claims%ROWTYPE;
            task work_tasks%ROWTYPE;
            period budget_periods%ROWTYPE;
            article budget_articles%ROWTYPE;
            paid_total numeric;
            paid_personal numeric;
        BEGIN
            IF TG_TABLE_NAME = 'financial_transactions' THEN
                posting_id := NEW.id;
            ELSE
                posting_id := NEW.payout_transaction_id;
            END IF;

            SELECT * INTO posting FROM financial_transactions WHERE id = posting_id;
            SELECT * INTO claim FROM work_claims WHERE payout_transaction_id = posting_id;
            IF claim.id IS NULL OR claim.status <> 'PAID' OR posting.kind <> 'NPC_PAYOUT'
                OR posting.idempotency_key <> 'npc_payout:' || claim.id
                OR posting.actor_citizen_id IS DISTINCT FROM claim.reviewer_citizen_id
                OR posting.entry_count <> 2
            THEN
                RAISE EXCEPTION 'NPC payout must identify a paid review' USING ERRCODE = 'check_violation';
            END IF;

            SELECT * INTO STRICT task FROM work_tasks WHERE id = claim.task_id;
            IF NOT EXISTS (
                SELECT 1 FROM ledger_entries AS entry
                JOIN financial_accounts AS account ON account.id = entry.account_id
                WHERE entry.transaction_id = posting_id AND account.kind = 'TREASURY'
                    AND entry.delta = -task.reward
            ) OR NOT EXISTS (
                SELECT 1 FROM ledger_entries AS entry
                JOIN financial_accounts AS account ON account.id = entry.account_id
                WHERE entry.transaction_id = posting_id AND account.kind = 'PERSONAL'
                    AND account.citizen_id = claim.citizen_id AND entry.delta = task.reward
            ) THEN
                RAISE EXCEPTION 'NPC payout must move the agreed reward from the treasury to the claimant'
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT * INTO STRICT period FROM budget_periods WHERE id = claim.budget_period_id;
            SELECT * INTO article FROM budget_articles WHERE period_id = period.id AND code = 'npc_work';
            SELECT coalesce(sum(t.reward), 0),
                   coalesce(sum(t.reward) FILTER (WHERE c.citizen_id = claim.citizen_id), 0)
              INTO paid_total, paid_personal
              FROM work_claims AS c JOIN work_tasks AS t ON t.id = c.task_id
             WHERE c.budget_period_id = period.id AND c.status = 'PAID';
            IF article.id IS NULL OR article.cap > period.overall_cap
                OR claim.reviewed_at < period.start_at OR claim.reviewed_at >= period.end_at
                OR paid_total <> article.spent OR article.spent > period.spent_total
                OR paid_personal > article.personal_cap
            THEN
                RAISE EXCEPTION 'NPC payout does not match the weekly budget'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER npc_validate_ledger_payout
        AFTER INSERT ON financial_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.kind = 'NPC_PAYOUT')
        EXECUTE FUNCTION civitas_validate_npc_payout()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER npc_validate_paid_claim
        AFTER INSERT OR UPDATE ON work_claims DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.status = 'PAID')
        EXECUTE FUNCTION civitas_validate_npc_payout()
    """)


def downgrade() -> None:
    _set_ledger_kinds(include_npc=False)
    op.execute("DROP TRIGGER npc_validate_ledger_payout ON financial_transactions")
    op.drop_table("work_claims")
    op.drop_table("work_tasks")
    op.drop_table("budget_articles")
    op.drop_table("budget_periods")
    op.execute("DROP FUNCTION civitas_validate_npc_payout()")
    op.execute("DROP FUNCTION civitas_reject_npc_history_edit()")
