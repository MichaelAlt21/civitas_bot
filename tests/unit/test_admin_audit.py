from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from civitas.app.admin_audit import (
    ADMIN_AUDIT_MAX_PAGE,
    ADMIN_AUDIT_PAGE_SIZE,
    AdminAuditEntry,
    AdminAuditError,
    AdminAuditErrorReason,
    AdminAuditRepository,
    AdminAuditService,
)
from civitas.app.admin_operators import AdminOperatorPolicy

GUILD_ID, OPERATOR_ID = 101, 201


@pytest.fixture
def audit():
    repository = AsyncMock(spec=AdminAuditRepository)
    scopes: list[bool] = []

    @asynccontextmanager
    async def unit_of_work():
        scopes.append(True)
        yield repository

    policy = AdminOperatorPolicy(discord_guild_id=GUILD_ID, operator_user_ids={OPERATOR_ID})
    return AdminAuditService(unit_of_work, operator_policy=policy), repository, scopes


@pytest.mark.parametrize(("guild_id", "user_id"), [(None, OPERATOR_ID), (102, OPERATOR_ID), (GUILD_ID, 202)])
@pytest.mark.asyncio
async def test_audit_denies_dm_other_guild_and_unlisted_actor_before_reading(audit, guild_id, user_id):
    service, repository, scopes = audit
    with pytest.raises(AdminAuditError) as denied:
        await service.list_recent(guild_id, user_id)
    assert denied.value.reason is AdminAuditErrorReason.OPERATOR_REQUIRED
    assert scopes == []
    repository.list_admin_audit.assert_not_awaited()


@pytest.mark.parametrize("page", [False, -1, 0, 1.0, ADMIN_AUDIT_MAX_PAGE + 1])
@pytest.mark.asyncio
async def test_audit_rejects_invalid_page_before_reading(audit, page):
    service, repository, scopes = audit
    with pytest.raises(AdminAuditError) as denied:
        await service.list_recent(GUILD_ID, OPERATOR_ID, page=page)
    assert denied.value.reason is AdminAuditErrorReason.INVALID_PAGE
    assert scopes == []
    repository.list_admin_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_queries_only_one_bounded_page_in_the_operator_guild(audit):
    service, repository, _ = audit
    entry = AdminAuditEntry(datetime(2026, 11, 14, tzinfo=UTC), "citizen.left", 202, "citizen", 42, "success", None)
    repository.list_admin_audit.return_value = (entry,)
    assert await service.list_recent(GUILD_ID, OPERATOR_ID, page=ADMIN_AUDIT_MAX_PAGE) == (entry,)
    repository.list_admin_audit.assert_awaited_once_with(
        GUILD_ID, limit=ADMIN_AUDIT_PAGE_SIZE, offset=(ADMIN_AUDIT_MAX_PAGE - 1) * ADMIN_AUDIT_PAGE_SIZE,
    )
