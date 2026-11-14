"""Read a bounded history of critical Civitas actions for technical operators."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id

ADMIN_AUDIT_PAGE_SIZE = 10
ADMIN_AUDIT_MAX_PAGE = 100


@dataclass(frozen=True, slots=True)
class AdminAuditEntry:
    occurred_at: datetime
    action: str
    actor_discord_user_id: int
    subject_kind: str
    subject_id: int | None
    result: str
    reason_code: str | None


class AdminAuditErrorReason(StrEnum):
    OPERATOR_REQUIRED = "operator_required"
    INVALID_PAGE = "invalid_page"


class AdminAuditError(Exception):
    def __init__(self, reason: AdminAuditErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class AdminAuditRepository(Protocol):
    async def list_admin_audit(self, guild_id: int, *, limit: int, offset: int) -> tuple[AdminAuditEntry, ...]: ...


class _OperatorPolicy(Protocol):
    def allows(self, discord_guild_id: int | None, actor_user_id: int) -> bool: ...


class AdminAuditService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[AdminAuditRepository]],
        *,
        operator_policy: _OperatorPolicy,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._operator_policy = operator_policy

    async def list_recent(
        self, guild_id: int | None, user_id: int, *, page: int = 1,
    ) -> tuple[AdminAuditEntry, ...]:
        if not is_discord_id(guild_id) or not self._operator_policy.allows(guild_id, user_id):
            raise AdminAuditError(AdminAuditErrorReason.OPERATOR_REQUIRED)
        if type(page) is not int or not 1 <= page <= ADMIN_AUDIT_MAX_PAGE:
            raise AdminAuditError(AdminAuditErrorReason.INVALID_PAGE)

        async with self._unit_of_work() as repository:
            return await repository.list_admin_audit(
                guild_id, limit=ADMIN_AUDIT_PAGE_SIZE, offset=(page - 1) * ADMIN_AUDIT_PAGE_SIZE,
            )
