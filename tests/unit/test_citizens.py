from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from civitas.app.citizens import (
    AccessDeniedReason,
    CitizenshipAccessError,
    CitizensService,
    RegistrationOutcome,
)
from civitas.domain.citizens import Citizen, CitizenAuditAction, CitizenAuditEvent, CitizenStatus

DISCORD_GUILD_ID = 100
DISCORD_USER_ID = 200


class InMemoryCitizensRepository:
    def __init__(self) -> None:
        self.citizens: dict[int, Citizen] = {}
        self.audit_events: list[CitizenAuditEvent] = []
        self.scopes_opened = 0

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator["InMemoryCitizensRepository"]:
        self.scopes_opened += 1
        yield self

    async def get_profile(self, guild_id: int, user_id: int, *, lock: bool = False) -> Citizen | None:
        assert guild_id == DISCORD_GUILD_ID
        return self.citizens.get(user_id)

    async def get_or_create_verified(
        self, guild_id: int, user_id: int, now: datetime
    ) -> tuple[Citizen, bool]:
        citizen = await self.get_profile(guild_id, user_id)
        if citizen is not None:
            return citizen, False
        citizen = Citizen(len(self.citizens) + 1, 1, user_id, CitizenStatus.VERIFIED, now, now)
        self.citizens[user_id] = citizen
        return citizen, True

    async def update_citizen_status(self, citizen: Citizen) -> None:
        self.citizens[citizen.discord_user_id] = citizen

    async def append_audit(self, event: CitizenAuditEvent) -> None:
        self.audit_events.append(event)


@pytest.fixture
def repository() -> InMemoryCitizensRepository:
    return InMemoryCitizensRepository()


@pytest.fixture
def service(repository: InMemoryCitizensRepository) -> CitizensService:
    return CitizensService(
        repository.unit_of_work, guild_id=DISCORD_GUILD_ID, allowed_user_ids=[DISCORD_USER_ID]
    )


@pytest.mark.asyncio
async def test_registers_once_and_reads_the_same_profile(
    service: CitizensService, repository: InMemoryCitizensRepository
) -> None:
    assert await service.get_profile(DISCORD_GUILD_ID, DISCORD_USER_ID) is None

    result = await service.register(DISCORD_GUILD_ID, DISCORD_USER_ID)

    assert result.outcome == RegistrationOutcome.CREATED
    assert result.citizen.status == CitizenStatus.VERIFIED
    assert result.citizen.joined_at.tzinfo is UTC
    assert result.citizen.joined_at == result.citizen.verified_at
    assert await service.get_profile(DISCORD_GUILD_ID, DISCORD_USER_ID) == result.citizen
    for _ in range(10):
        repeated = await service.register(DISCORD_GUILD_ID, DISCORD_USER_ID)
        assert repeated.outcome == RegistrationOutcome.EXISTING
        assert repeated.citizen == result.citizen
    event, = repository.audit_events
    assert event.action == CitizenAuditAction.REGISTERED
    assert event.citizen == result.citizen
    assert event.occurred_at == result.citizen.joined_at
    assert event.reason == "discord_allowlist"


@pytest.mark.parametrize(
    ("guild_id", "user_id", "reason"),
    [
        (None, DISCORD_USER_ID, AccessDeniedReason.WRONG_GUILD),
        (DISCORD_GUILD_ID + 1, DISCORD_USER_ID, AccessDeniedReason.WRONG_GUILD),
        (DISCORD_GUILD_ID, DISCORD_USER_ID + 1, AccessDeniedReason.NOT_ALLOWED),
        (DISCORD_GUILD_ID, 0, AccessDeniedReason.NOT_ALLOWED),
    ],
)
@pytest.mark.asyncio
async def test_rejects_unauthorized_access_before_opening_the_repository(
    service: CitizensService,
    repository: InMemoryCitizensRepository,
    guild_id: int | None,
    user_id: int,
    reason: AccessDeniedReason,
) -> None:
    with pytest.raises(CitizenshipAccessError) as start_error:
        service.authorize(guild_id, user_id)
    with pytest.raises(CitizenshipAccessError) as register_error:
        await service.register(guild_id, user_id)
    with pytest.raises(CitizenshipAccessError) as profile_error:
        await service.get_profile(guild_id, user_id)

    assert start_error.value.reason == register_error.value.reason == profile_error.value.reason == reason
    assert repository.scopes_opened == 0


@pytest.mark.asyncio
async def test_restores_the_same_citizen_on_readmission(
    service: CitizensService, repository: InMemoryCitizensRepository
) -> None:
    original_join = datetime(2020, 1, 1, tzinfo=UTC)
    original = Citizen(42, 1, DISCORD_USER_ID, CitizenStatus.LEFT, original_join, original_join)
    repository.citizens[DISCORD_USER_ID] = original

    restored = await service.register(DISCORD_GUILD_ID, DISCORD_USER_ID)
    repeated = await service.register(DISCORD_GUILD_ID, DISCORD_USER_ID)

    assert restored.outcome == RegistrationOutcome.RESTORED
    assert restored.citizen.id == original.id
    assert restored.citizen.status == CitizenStatus.VERIFIED
    assert restored.citizen.joined_at == original.joined_at
    assert restored.citizen.verified_at != original.verified_at
    assert repeated.outcome == RegistrationOutcome.EXISTING
    assert repeated.citizen == restored.citizen
    event, = repository.audit_events
    assert event.action == CitizenAuditAction.RESTORED
    assert event.citizen == restored.citizen


@pytest.mark.parametrize("status", [CitizenStatus.VERIFIED, CitizenStatus.PENDING, CitizenStatus.SUSPENDED])
@pytest.mark.asyncio
async def test_trusted_departure_is_idempotent_and_preserves_suspension(
    repository: InMemoryCitizensRepository, status: CitizenStatus,
) -> None:
    service = CitizensService(repository.unit_of_work, guild_id=DISCORD_GUILD_ID, allowed_user_ids=())
    joined = datetime(2020, 1, 1, tzinfo=UTC)
    verified = joined if status is CitizenStatus.VERIFIED else None
    repository.citizens[DISCORD_USER_ID] = Citizen(42, 1, DISCORD_USER_ID, status, joined, verified)
    actor = 300
    observed = joined + timedelta(days=1)

    changed = await service.mark_departed(
        DISCORD_GUILD_ID, DISCORD_USER_ID, observed_at=observed, actor_discord_user_id=actor,
    )
    assert not await service.mark_departed(DISCORD_GUILD_ID, DISCORD_USER_ID, observed_at=observed)
    citizen = repository.citizens[DISCORD_USER_ID]
    assert citizen.verified_at == verified
    if status is CitizenStatus.SUSPENDED:
        assert not changed and citizen.status is status
        assert repository.audit_events == []
    else:
        assert changed and citizen.status is CitizenStatus.LEFT
        assert repository.audit_events == [CitizenAuditEvent(
            citizen, CitizenAuditAction.LEFT, observed, "discord_membership", actor,
        )]


@pytest.mark.asyncio
async def test_trusted_departure_ignores_other_guilds_unknown_users_and_stale_observations(
    service: CitizensService, repository: InMemoryCitizensRepository,
) -> None:
    assert not await service.mark_departed(DISCORD_GUILD_ID + 1, DISCORD_USER_ID)
    assert not await service.mark_departed(DISCORD_GUILD_ID, 0)
    assert repository.scopes_opened == 0
    assert not await service.mark_departed(DISCORD_GUILD_ID, DISCORD_USER_ID + 1)

    joined = datetime(2020, 1, 1, tzinfo=UTC)
    verified = joined + timedelta(days=3)
    citizen = Citizen(42, 1, DISCORD_USER_ID, CitizenStatus.VERIFIED, joined, verified)
    repository.citizens[DISCORD_USER_ID] = citizen
    assert not await service.mark_departed(DISCORD_GUILD_ID, DISCORD_USER_ID, observed_at=joined)
    assert not await service.mark_departed(DISCORD_GUILD_ID, DISCORD_USER_ID, observed_at=verified)
    assert repository.citizens[DISCORD_USER_ID] == citizen
    assert repository.audit_events == []


@pytest.mark.parametrize("status", [CitizenStatus.PENDING, CitizenStatus.SUSPENDED])
@pytest.mark.asyncio
async def test_registration_does_not_override_other_existing_statuses(
    service: CitizensService, repository: InMemoryCitizensRepository, status: CitizenStatus
) -> None:
    original = Citizen(42, 1, DISCORD_USER_ID, status, datetime(2020, 1, 1, tzinfo=UTC), None)
    repository.citizens[DISCORD_USER_ID] = original

    result = await service.register(DISCORD_GUILD_ID, DISCORD_USER_ID)

    assert result.outcome == RegistrationOutcome.EXISTING
    assert result.citizen == original
    assert repository.audit_events == []


@pytest.mark.asyncio
async def test_allows_more_than_thirty_eligible_citizens(
    repository: InMemoryCitizensRepository,
) -> None:
    allowed_ids = range(1, 32)
    service = CitizensService(
        repository.unit_of_work, guild_id=DISCORD_GUILD_ID, allowed_user_ids=allowed_ids
    )

    for user_id in allowed_ids:
        result = await service.register(DISCORD_GUILD_ID, user_id)
        assert result.outcome == RegistrationOutcome.CREATED

    assert len(repository.citizens) == 31
