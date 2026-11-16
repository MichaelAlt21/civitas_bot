"""Persist the administrator's Discord game-custody role binding."""

from alembic import op
from sqlalchemy import Column, Numeric

revision = "0036_custody_role"
down_revision = "0035_expired_court_fine"
branch_labels = None
depends_on = None

_SETTINGS = "guild_presentations"
_AUDIT = "presentation_audit_events"
_DISTINCT = "ck_guild_presentations_distinct_roles"
_BINDING = "ck_presentation_audit_binding"
_ROLE = "ck_guild_presentations_custody_role"
_PREVIOUS_DISTINCT = (
    "citizen_role_id <> president_role_id AND citizen_role_id <> parliament_role_id "
    "AND president_role_id <> parliament_role_id"
)
_PREVIOUS_BINDINGS = "'citizen_role_id', 'president_role_id', 'parliament_role_id', 'report_channel_id'"


def _checks(*, custody: bool) -> None:
    op.drop_constraint(_DISTINCT, _SETTINGS, type_="check")
    op.drop_constraint(_BINDING, _AUDIT, type_="check")
    extra_roles = (
        " AND custody_role_id <> citizen_role_id AND custody_role_id <> president_role_id "
        "AND custody_role_id <> parliament_role_id"
    ) if custody else ""
    extra_binding = ", 'custody_role_id'" if custody else ""
    op.create_check_constraint(_DISTINCT, _SETTINGS, _PREVIOUS_DISTINCT + extra_roles)
    op.create_check_constraint(_BINDING, _AUDIT, f"binding IN ({_PREVIOUS_BINDINGS}{extra_binding})")


def upgrade() -> None:
    op.add_column(_SETTINGS, Column("custody_role_id", Numeric(20, 0), nullable=True))
    op.create_check_constraint(
        _ROLE, _SETTINGS,
        "custody_role_id BETWEEN 1 AND 18446744073709551615 AND custody_role_id <> discord_guild_id",
    )
    _checks(custody=True)


def downgrade() -> None:
    op.execute("LOCK TABLE guild_presentations, presentation_audit_events IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM guild_presentations WHERE custody_role_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM presentation_audit_events WHERE binding = 'custody_role_id')
            THEN
                RAISE EXCEPTION 'Cannot downgrade while custody role history exists';
            END IF;
        END $$;
    """)
    _checks(custody=False)
    op.drop_constraint(_ROLE, _SETTINGS, type_="check")
    op.drop_column(_SETTINGS, "custody_role_id")
