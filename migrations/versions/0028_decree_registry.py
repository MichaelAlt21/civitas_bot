"""Distinguish presidential decrees from Senate laws in the act registry."""

from alembic import op
from sqlalchemy import Column, Text, text

revision = "0028_decree_registry"
down_revision = "0027_court_docket"
branch_labels = None
depends_on = None

_REVISION_LINK = ("guild_id", "id", "code", "effective_at", "proposer_citizen_id", "proposed_at")
_CONFIRMATION_LINK = ("guild_id", "revision_id", "code", "effective_at", "proposer_citizen_id", "proposed_at")


def _replace_confirmation_constraints(*, include_kind: bool) -> None:
    op.drop_constraint("fk_law_confirmations_revision", "law_confirmations", type_="foreignkey")
    op.drop_constraint("uq_law_revisions_confirmation_link", "law_revisions", type_="unique")
    op.drop_constraint("uq_law_confirmations_slot", "law_confirmations", type_="unique")

    kind_column = ("act_kind",) if include_kind else ()
    revision_columns = [*_REVISION_LINK, *kind_column]
    confirmation_columns = [*_CONFIRMATION_LINK, *kind_column]
    op.create_unique_constraint("uq_law_revisions_confirmation_link", "law_revisions", revision_columns)
    op.create_unique_constraint(
        "uq_law_confirmations_slot", "law_confirmations", ["guild_id", *kind_column, "code", "effective_at"],
    )
    op.create_foreign_key(
        "fk_law_confirmations_revision", "law_confirmations", "law_revisions", confirmation_columns, revision_columns,
    )


def upgrade() -> None:
    for table in ("law_revisions", "law_confirmations"):
        op.add_column(table, Column("act_kind", Text(), nullable=False, server_default=text("'LAW'")))
        op.create_check_constraint(f"ck_{table}_act_kind", table, "act_kind IN ('LAW', 'DECREE')")
    _replace_confirmation_constraints(include_kind=True)
    op.execute("""
        CREATE FUNCTION civitas_guard_criminal_charge_act_kind() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM law_confirmations
                WHERE guild_id = NEW.guild_id AND revision_id = NEW.law_revision_id AND act_kind = 'LAW'
            ) THEN
                RAISE EXCEPTION 'Criminal charges require a law revision' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER court_cases_criminal_act_kind
        BEFORE INSERT ON court_cases
        FOR EACH ROW WHEN (NEW.kind = 'CRIMINAL')
        EXECUTE FUNCTION civitas_guard_criminal_charge_act_kind()
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE law_revisions, law_confirmations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM law_revisions WHERE act_kind = 'DECREE') THEN
                RAISE EXCEPTION 'Cannot downgrade while decree revision history exists';
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER court_cases_criminal_act_kind ON court_cases")
    op.execute("DROP FUNCTION civitas_guard_criminal_charge_act_kind()")
    _replace_confirmation_constraints(include_kind=False)
    for table in ("law_confirmations", "law_revisions"):
        op.drop_constraint(f"ck_{table}_act_kind", table, type_="check")
        op.drop_column(table, "act_kind")
