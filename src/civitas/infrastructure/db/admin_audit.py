"""Project operator audit rows without reading free-form evidence or ledger details."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from decimal import Decimal

from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from civitas.app.admin_audit import AdminAuditEntry
from civitas.app.citizens import AccessDeniedReason
from civitas.app.treasury import TreasuryErrorReason
from civitas.domain.citizens import CitizenAuditAction
from civitas.domain.ledger import LedgerErrorReason

_TREASURY_ACTIONS = (
    "budget.activate", "contract.propose", "contract.reserve", "contract.submit", "contract.pay", "contract.cancel",
)
_TREASURY_REASONS = tuple(reason.value for reason in (*AccessDeniedReason, *TreasuryErrorReason, *LedgerErrorReason))

_AUDIT = text("""
    WITH guild_scope AS (
        SELECT id FROM guilds WHERE discord_guild_id = :discord_guild_id
    ), citizen_actions AS (
        SELECT guild_id, activated_by_citizen_id AS actor_id, activated_at AS occurred_at,
               'budget.activate' AS action, 'budget_period' AS subject_kind, period_id AS subject_id,
               'success' AS result, period_id AS event_id
          FROM contract_budget_approvals WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, proposer_citizen_id, created_at, 'contract.propose', 'contract', id, 'success', id
          FROM state_contracts WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, reserved_by_citizen_id, reserved_at, 'contract.reserve', 'contract', id, 'success', id
          FROM state_contracts WHERE guild_id = (SELECT id FROM guild_scope) AND reserved_at IS NOT NULL
        UNION ALL
        SELECT guild_id, closed_by_citizen_id, closed_at,
               CASE status WHEN 'PAID' THEN 'contract.pay' ELSE 'contract.cancel' END,
               'contract', id, lower(status), id
          FROM state_contracts WHERE guild_id = (SELECT id FROM guild_scope) AND status IN ('PAID', 'CANCELLED')
        UNION ALL
        SELECT guild_id, opened_by_citizen_id, opens_at, 'election.open', 'election', id, 'success', id
          FROM elections WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, recorded_by_citizen_id, recorded_at, 'election.record', 'election', election_id, 'success', id
          FROM election_protocols WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT attestation.guild_id, attestation.certified_by_citizen_id, attestation.certified_at,
               'election.certify', 'election', election.id, lower(election.status), election.id
          FROM election_certifications AS attestation
          JOIN elections AS election ON election.guild_id = attestation.guild_id AND election.id = attestation.election_id
         WHERE attestation.guild_id = (SELECT id FROM guild_scope) AND election.status IN ('APPLIED', 'NO_QUORUM', 'TIED')
        UNION ALL
        SELECT guild_id, actor_citizen_id, occurred_at, 'election.crisis.propose', 'election', election_id, 'success', election_id
          FROM election_crisis_proposals WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, actor_citizen_id, occurred_at, 'election.crisis.confirm', 'election', election_id, 'success', election_id
          FROM election_crisis_confirmations WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, actor_citizen_id, created_at, 'asset.issue', 'asset', asset_id, 'success', id
          FROM asset_transfers WHERE guild_id = (SELECT id FROM guild_scope) AND kind = 'ISSUE'
        UNION ALL
        SELECT guild_id, reviewer_citizen_id, reviewed_at, 'npc.review', 'npc_task', task_id, lower(status), id
          FROM work_claims WHERE guild_id = (SELECT id FROM guild_scope) AND status IN ('PAID', 'REJECTED')
        UNION ALL
        SELECT guild_id, closed_by_citizen_id, closed_at, 'market.arbitrate', 'market_order', id, lower(status), id
          FROM orders WHERE guild_id = (SELECT id FROM guild_scope) AND disputed_at IS NOT NULL
             AND status IN ('COMPLETED', 'REFUNDED')
        UNION ALL
        SELECT guild_id, withdrawn_by_citizen_id, withdrawn_at, 'market.offer.withdraw',
               'market_offer', offer_id, 'success', offer_id
          FROM offer_withdrawals WHERE guild_id = (SELECT id FROM guild_scope)
        UNION ALL
        SELECT guild_id, closed_by_citizen_id, closed_at, 'work.arbitrate', 'private_job', id, lower(status), id
          FROM private_jobs WHERE guild_id = (SELECT id FROM guild_scope) AND disputed_at IS NOT NULL
             AND status IN ('PAID', 'CANCELLED')
    ), events AS (
        SELECT action.occurred_at, action.action, citizen.discord_user_id AS actor_discord_user_id,
               action.subject_kind, action.subject_id, action.result, NULL::text AS reason_code, action.event_id
          FROM citizen_actions AS action
          JOIN citizens AS citizen ON citizen.guild_id = action.guild_id AND citizen.id = action.actor_id
        UNION ALL
        SELECT occurred_at, action, actor_discord_user_id, 'citizen', citizen_id, 'success',
               CASE WHEN reason IN ('discord_allowlist', 'discord_membership') THEN reason END, id
          FROM audit_events WHERE guild_id = (SELECT id FROM guild_scope) AND action = ANY(:citizen_actions)
        UNION ALL
        SELECT occurred_at, action, actor_discord_user_id,
               CASE action WHEN 'budget.activate' THEN 'budget_period' ELSE 'contract' END,
               contract_id, 'denied', CASE WHEN reason = ANY(:treasury_reasons) THEN reason END, id
          FROM treasury_audit_events WHERE discord_guild_id = :discord_guild_id AND action = ANY(:treasury_actions)
        UNION ALL
        SELECT occurred_at,
               CASE WHEN read_only THEN 'economy.maintenance.paused' ELSE 'economy.maintenance.resumed' END,
               actor_discord_user_id, 'economy', NULL::bigint, 'success', NULL::text, id
          FROM economy_maintenance_audit WHERE guild_id = (SELECT id FROM guild_scope)
    )
    SELECT occurred_at, action, actor_discord_user_id, subject_kind, subject_id, result, reason_code
      FROM events
     ORDER BY occurred_at DESC, action, result, event_id DESC
     LIMIT :limit OFFSET :offset
""").bindparams(
    bindparam("citizen_actions", type_=ARRAY(String)),
    bindparam("treasury_actions", type_=ARRAY(String)),
    bindparam("treasury_reasons", type_=ARRAY(String)),
)


class PostgresAdminAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_admin_audit(self, guild_id: int, *, limit: int, offset: int) -> tuple[AdminAuditEntry, ...]:
        rows = await self._session.execute(_AUDIT, {
            "discord_guild_id": Decimal(guild_id),
            "citizen_actions": [action.value for action in CitizenAuditAction],
            "treasury_actions": list(_TREASURY_ACTIONS),
            "treasury_reasons": list(_TREASURY_REASONS),
            "limit": limit,
            "offset": offset,
        })
        return tuple(AdminAuditEntry(
            occurred_at=row.occurred_at.astimezone(UTC),
            action=row.action,
            actor_discord_user_id=int(row.actor_discord_user_id),
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            result=row.result,
            reason_code=row.reason_code,
        ) for row in rows)


class PostgresAdminAuditUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresAdminAuditRepository]:
        async with self._sessions() as session:
            yield PostgresAdminAuditRepository(session)
