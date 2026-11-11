import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from civitas.app.citizen_roles import CitizenRoleReconciler
from civitas.domain.citizens import Citizen, CitizenStatus

DISCORD_GUILD_ID = 100
JOINED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class RoleRepository:
    def __init__(self, *statuses: CitizenStatus) -> None:
        self.members = tuple(
            Citizen(
                id=index,
                guild_id=1,
                discord_user_id=200 + index,
                status=status,
                joined_at=JOINED_AT,
                verified_at=JOINED_AT if status == CitizenStatus.VERIFIED else None,
            )
            for index, status in enumerate(statuses, start=1)
        )
        self.scope_open = False
        self.requested_guilds: list[int] = []

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator["RoleRepository"]:
        self.scope_open = True
        try:
            yield self
        finally:
            self.scope_open = False

    async def list_role_members(self, guild_id: int) -> tuple[Citizen, ...]:
        assert self.scope_open
        self.requested_guilds.append(guild_id)
        return self.members


class RoleGateway:
    def __init__(self, repository: RoleRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[int, bool, bool]] = []
        self.failed_ids: set[int] = set()

    async def set_citizen_role(self, discord_user_id: int, *, present: bool) -> None:
        self.calls.append((discord_user_id, present, self.repository.scope_open))
        if discord_user_id in self.failed_ids:
            raise RuntimeError("private gateway response")


@pytest.mark.asyncio
async def test_reconciles_all_statuses_outside_the_read_transaction() -> None:
    repository = RoleRepository(
        CitizenStatus.VERIFIED, CitizenStatus.PENDING, CitizenStatus.SUSPENDED, CitizenStatus.LEFT
    )
    gateway = RoleGateway(repository)
    service = CitizenRoleReconciler(repository.unit_of_work, gateway, guild_id=DISCORD_GUILD_ID)

    await service.reconcile()

    assert repository.requested_guilds == [DISCORD_GUILD_ID]
    assert gateway.calls == [
        (201, True, False),
        (202, False, False),
        (203, False, False),
        (204, False, False),
    ]


@pytest.mark.asyncio
async def test_a_member_failure_preserves_other_updates_and_retries_next_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = RoleRepository(CitizenStatus.VERIFIED, CitizenStatus.LEFT)
    gateway = RoleGateway(repository)
    gateway.failed_ids.add(201)
    service = CitizenRoleReconciler(repository.unit_of_work, gateway, guild_id=DISCORD_GUILD_ID)

    with caplog.at_level(logging.WARNING, logger="civitas.app.citizen_roles"):
        await service.reconcile()

    assert gateway.calls == [(201, True, False), (202, False, False)]
    assert "RuntimeError" in caplog.text
    assert "private gateway response" not in caplog.text

    gateway.calls.clear()
    gateway.failed_ids.clear()
    await service.reconcile()

    assert gateway.calls == [(201, True, False), (202, False, False)]
