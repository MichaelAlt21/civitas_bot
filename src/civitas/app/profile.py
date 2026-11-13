"""Read a citizen's public offices, companies and owned property without balances."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from civitas.domain.assets import Asset
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import OfficeKind, OfficeTerm
from civitas.domain.market import Company


class ProfileRepository(Protocol):
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...

    async def list_offices(self, guild_id: int, at: datetime) -> tuple[OfficeTerm, ...]: ...

    async def list_owned_companies(self, guild_id: int, citizen_id: int) -> tuple[Company, ...]: ...

    async def list_owned_assets(
        self, guild_id: int, citizen_id: int, company_id: int | None, limit: int, *, offset: int = 0,
    ) -> tuple[Asset, ...]: ...


@dataclass(frozen=True, slots=True)
class ProfileDetails:
    citizen: Citizen
    offices: tuple[OfficeKind, ...] = ()
    companies: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()


class ProfileService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[ProfileRepository]],
        *, authorize: Callable[[int | None, int], None],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._authorize = authorize

    async def get_profile(self, guild_id: int | None, user_id: int) -> ProfileDetails | None:
        self._authorize(guild_id, user_id)
        assert guild_id is not None  # Authorization requires the configured guild.
        async with self._unit_of_work() as repository:
            citizen = await repository.get_profile(guild_id, user_id)
            if citizen is None:
                return None
            terms = await repository.list_offices(citizen.guild_id, datetime.now(UTC)) if citizen.status == CitizenStatus.VERIFIED else ()
            companies = await repository.list_owned_companies(citizen.guild_id, citizen.id)
            assets = await repository.list_owned_assets(citizen.guild_id, citizen.id, None, 5)
        return ProfileDetails(
            citizen,
            tuple(term.office for term in terms if term.holder_citizen_id == citizen.id),
            tuple(company.name for company in companies[:5]),
            tuple(asset.name for asset in assets),
        )
