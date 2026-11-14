from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from civitas.app.admin_audit import ADMIN_AUDIT_PAGE_SIZE, AdminAuditService
from civitas.app.admin_operators import AdminOperatorPolicy
from civitas.app.citizens import CitizensService, MAX_DISCORD_ID
from civitas.infrastructure.db.admin_audit import PostgresAdminAuditUnitOfWork
from civitas.infrastructure.db.citizens import PostgresCitizenUnitOfWork
from civitas.infrastructure.db.maintenance_models import EconomyMaintenanceAuditRow
from civitas.infrastructure.db.treasury_models import TreasuryAuditEventRow

GUILD_ID, OTHER_GUILD_ID = MAX_DISCORD_ID, MAX_DISCORD_ID - 1
OPERATOR_ID, CITIZEN_ID, OTHER_CITIZEN_ID = MAX_DISCORD_ID - 2, 101, 202


@pytest.mark.asyncio
async def test_operator_audit_is_guild_scoped_paginated_and_projects_only_safe_fields(pg_engine):
    citizens = PostgresCitizenUnitOfWork(pg_engine)
    local_service = CitizensService(citizens, guild_id=GUILD_ID, allowed_user_ids={CITIZEN_ID})
    local = (await local_service.register(GUILD_ID, CITIZEN_ID)).citizen
    other_service = CitizensService(citizens, guild_id=OTHER_GUILD_ID, allowed_user_ids={OTHER_CITIZEN_ID})
    foreign = (await other_service.register(OTHER_GUILD_ID, OTHER_CITIZEN_ID)).citizen
    now = datetime.now(UTC)
    private_reason = "Private moderation evidence <@202> https://example.test/private"

    async with pg_engine.begin() as connection:
        await connection.execute(insert(EconomyMaintenanceAuditRow), [
            dict(guild_id=local.guild_id, actor_discord_user_id=OPERATOR_ID, previous_read_only=False,
                 read_only=True, reason=private_reason, occurred_at=now),
            dict(guild_id=foreign.guild_id, actor_discord_user_id=OTHER_CITIZEN_ID, previous_read_only=False,
                 read_only=True, reason=private_reason, occurred_at=now + timedelta(seconds=1)),
        ])
        await connection.execute(insert(TreasuryAuditEventRow), [
            dict(discord_guild_id=GUILD_ID, actor_discord_user_id=CITIZEN_ID, action="contract.pay",
                 contract_id=contract_id, reason="approver_required" if contract_id != 1 else private_reason,
                 occurred_at=now - timedelta(microseconds=1))
            for contract_id in range(1, ADMIN_AUDIT_PAGE_SIZE + 1)
        ])
        await connection.execute(insert(TreasuryAuditEventRow).values(
            discord_guild_id=GUILD_ID, actor_discord_user_id=CITIZEN_ID, action="unsafe.<@202>",
            contract_id=None, reason=private_reason, occurred_at=now + timedelta(seconds=1),
        ))

    service = AdminAuditService(
        PostgresAdminAuditUnitOfWork(pg_engine),
        operator_policy=AdminOperatorPolicy(discord_guild_id=GUILD_ID, operator_user_ids={OPERATOR_ID}),
    )
    first = await service.list_recent(GUILD_ID, OPERATOR_ID)
    second = await service.list_recent(GUILD_ID, OPERATOR_ID, page=2)
    assert len(first) == ADMIN_AUDIT_PAGE_SIZE
    assert first[0].occurred_at == now
    assert first[0].actor_discord_user_id == OPERATOR_ID
    assert first[0].action == "economy.maintenance.paused"
    assert first[0].result == "success"
    assert first[0].reason_code is None
    assert [event.subject_id for event in first[1:]] == list(range(ADMIN_AUDIT_PAGE_SIZE, 1, -1))
    assert all(event.result == "denied" and event.reason_code == "approver_required" for event in first[1:])
    assert [event.action for event in second] == ["contract.pay", "citizen.registered"]
    assert second[0].subject_id == 1 and second[0].reason_code is None
    assert second[1].subject_id == local.id and second[1].reason_code == "discord_allowlist"
    assert all(event.actor_discord_user_id != OTHER_CITIZEN_ID for event in first + second)
    assert private_reason not in repr([asdict(event) for event in first + second])

    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(EconomyMaintenanceAuditRow.reason).where(
            EconomyMaintenanceAuditRow.guild_id == local.guild_id,
        )) == private_reason
