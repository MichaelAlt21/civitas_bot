"""Authorize state issuance, primary purchases and personal resales."""

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id
from civitas.app.market import MarketRepository
from civitas.app.wallet import WalletFunding
from civitas.domain.assets import (
    Asset, AssetKind, AssetPurchase, AssetQuote, AssetResale, AssetResaleQuote, AssetResaleStatus, AssetStatus,
)
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH, AccountKind, TransactionKind
from civitas.domain.market import BASIS_POINTS, Company

ASSET_PAGE_SIZE = 10


class AssetErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    STAFF_REQUIRED = "staff_required"
    INVALID_KIND = "invalid_kind"
    INVALID_TEXT = "invalid_text"
    INVALID_PRICE = "invalid_price"
    INVALID_PAGE = "invalid_page"
    INVALID_REQUEST_KEY = "invalid_request_key"
    ASSET_NOT_FOUND = "asset_not_found"
    ASSET_SOLD = "asset_sold"
    COMPANY_NOT_FOUND = "company_not_found"
    COMPANY_NOT_OWNED = "company_not_owned"
    QUOTE_CHANGED = "quote_changed"
    RESALE_DISABLED = "resale_disabled"
    RESALE_NOT_FOUND = "resale_not_found"
    RESALE_ALREADY_OPEN = "resale_already_open"
    ASSET_NOT_OWNED = "asset_not_owned"
    RESALE_SELF_TRADE = "resale_self_trade"
    RESALE_NOT_OPEN = "resale_not_open"


class AssetError(Exception):
    def __init__(self, reason: AssetErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class AssetRepository(MarketRepository, Protocol):
    async def get_asset(self, guild_id: int, asset_id: int, *, lock: bool = False) -> Asset | None: ...

    async def get_asset_by_issue_key(self, guild_id: int, key: str) -> Asset | None: ...

    async def list_assets_for_sale(self, guild_id: int, limit: int, *, offset: int = 0) -> tuple[Asset, ...]: ...

    async def list_owned_assets(
        self, guild_id: int, citizen_id: int, company_id: int | None, limit: int, *, offset: int = 0
    ) -> tuple[Asset, ...]: ...

    async def create_asset(
        self,
        *,
        guild_id: int,
        kind: AssetKind,
        name: str,
        purpose: str,
        price: int,
        issued_by_citizen_id: int,
        issue_reason: str,
        issue_key: str,
        created_at: datetime,
    ) -> Asset | None: ...

    async def get_asset_purchase_by_key(self, guild_id: int, key: str) -> AssetPurchase | None: ...

    async def create_asset_purchase(
        self,
        *,
        guild_id: int,
        asset_id: int,
        buyer_citizen_id: int,
        buyer_company_id: int | None,
        buyer_account_id: int,
        price: int,
        request_key: str,
        transaction_id: int,
        purchased_at: datetime,
    ) -> AssetPurchase | None: ...

    async def assign_asset(
        self,
        *,
        guild_id: int,
        asset_id: int,
        owner_citizen_id: int | None,
        owner_company_id: int | None,
    ) -> None: ...

    async def get_resale_offer(
        self, guild_id: int, offer_id: int, *, lock: bool = False
    ) -> AssetResale | None: ...

    async def get_resale_offer_by_key(self, guild_id: int, key: str) -> AssetResale | None: ...

    async def create_resale_offer(
        self, *, guild_id: int, asset_id: int, seller_citizen_id: int, buyer_citizen_id: int,
        price: int, tax_bp: int, request_key: str, created_at: datetime,
    ) -> AssetResale | None: ...

    async def list_resale_offers(
        self, guild_id: int, citizen_id: int, limit: int, *, offset: int = 0,
    ) -> tuple[AssetResale, ...]: ...

    async def update_resale_offer(self, offer: AssetResale) -> None: ...

    async def get_resale_citizens(self, guild_id: int, citizen_ids: tuple[int, ...]) -> tuple[Citizen, ...]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise AssetError(AssetErrorReason.INVALID_TEXT)
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise AssetError(AssetErrorReason.INVALID_TEXT)
    return value


def _validate_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or "\0" in key
    ):
        raise AssetError(AssetErrorReason.INVALID_REQUEST_KEY)


def _valid_id(value: int) -> bool:
    return type(value) is int and 1 <= value <= MAX_AMOUNT


def _page_offset(page: int) -> int:
    if type(page) is not int or not 1 <= page <= MAX_AMOUNT // ASSET_PAGE_SIZE + 1:
        raise AssetError(AssetErrorReason.INVALID_PAGE)
    return (page - 1) * ASSET_PAGE_SIZE


class AssetService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[AssetRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        staff_user_ids: Iterable[int],
        funding: WalletFunding,
        clock: Callable[[], datetime] = _utcnow,
        resale_tax_bp: int | None = None,
    ) -> None:
        staff_ids = tuple(staff_user_ids)
        if not all(is_discord_id(user_id) for user_id in staff_ids):
            raise ValueError("Asset staff IDs must be Discord snowflakes")
        if resale_tax_bp is not None and (type(resale_tax_bp) is not int or not 0 <= resale_tax_bp < BASIS_POINTS):
            raise ValueError("Resale tax must be between 0 and 9999 basis points")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._staff_user_ids = frozenset(staff_ids)
        self._funding = funding
        self._clock = clock
        self._resale_tax_bp = resale_tax_bp

    @property
    def resale_enabled(self) -> bool:
        return self._resale_tax_bp is not None

    async def issue_asset(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        kind: AssetKind,
        name: str,
        purpose: str,
        price: int,
        reason: str,
        idempotency_key: str,
    ) -> Asset:
        self._authorize(guild_id, user_id)
        if user_id not in self._staff_user_ids:
            raise AssetError(AssetErrorReason.STAFF_REQUIRED)
        if not isinstance(kind, AssetKind):
            raise AssetError(AssetErrorReason.INVALID_KIND)
        if type(price) is not int or not 1 <= price <= MAX_AMOUNT:
            raise AssetError(AssetErrorReason.INVALID_PRICE)
        _validate_key(idempotency_key)
        name, purpose, reason = _text(name, 80), _text(purpose, 300), _text(reason, 200)
        async with self._unit_of_work() as repository:
            staff = await self._citizen(repository, user_id, lock=True)
            prior = await repository.get_asset_by_issue_key(staff.guild_id, idempotency_key)
            if prior is None:
                created = await repository.create_asset(
                    guild_id=staff.guild_id,
                    kind=kind,
                    name=name,
                    purpose=purpose,
                    price=price,
                    issued_by_citizen_id=staff.id,
                    issue_reason=reason,
                    issue_key=idempotency_key,
                    created_at=self._clock(),
                )
                if created is not None:
                    return created
                prior = await repository.get_asset_by_issue_key(staff.guild_id, idempotency_key)
            if prior is None or (prior.issued_by_citizen_id, prior.kind, prior.name, prior.purpose, prior.price, prior.issue_reason) != (
                staff.id, kind, name, purpose, price, reason
            ):
                raise AssetError(AssetErrorReason.INVALID_REQUEST_KEY)
            return prior

    async def catalog(self, guild_id: int | None, user_id: int, *, page: int = 1) -> tuple[Asset, ...]:
        self._authorize(guild_id, user_id)
        offset = _page_offset(page)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await repository.list_assets_for_sale(citizen.guild_id, ASSET_PAGE_SIZE, offset=offset)

    async def asset_info(self, guild_id: int | None, user_id: int, asset_id: int) -> Asset:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await self._available_asset(repository, citizen.guild_id, asset_id)

    async def list_owned_assets(
        self, guild_id: int | None, user_id: int, *, company_id: int | None = None, page: int = 1
    ) -> tuple[Asset, ...]:
        self._authorize(guild_id, user_id)
        offset = _page_offset(page)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            if company_id is not None:
                await self._owned_company(repository, citizen, company_id)
            return await repository.list_owned_assets(
                citizen.guild_id, citizen.id, company_id, ASSET_PAGE_SIZE, offset=offset
            )

    async def quote_asset(
        self, guild_id: int | None, user_id: int, asset_id: int, *, buyer_company_id: int | None = None
    ) -> AssetQuote:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id)
            asset = await self._available_asset(repository, buyer.guild_id, asset_id)
            company = None
            if buyer_company_id is not None:
                company = await self._owned_company(repository, buyer, buyer_company_id)
            return AssetQuote(asset, company)

    async def buy_asset(
        self, guild_id: int | None, user_id: int, quote: AssetQuote, *, idempotency_key: str
    ) -> AssetPurchase:
        self._authorize(guild_id, user_id)
        _validate_key(idempotency_key)
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id, lock=True)
            company_id = quote.buyer_company.id if quote.buyer_company is not None else None
            prior = await repository.get_asset_purchase_by_key(buyer.guild_id, idempotency_key)
            if prior is not None:
                if (prior.buyer_citizen_id, prior.buyer_company_id, prior.asset_id, prior.price) != (
                    buyer.id, company_id, quote.asset.id, quote.asset.price
                ):
                    raise AssetError(AssetErrorReason.INVALID_REQUEST_KEY)
                return prior

            asset = await self._available_asset(repository, buyer.guild_id, quote.asset.id, lock=True)
            if asset != quote.asset:
                raise AssetError(AssetErrorReason.QUOTE_CHANGED)
            if company_id is None:
                source = await self._funding.fund_registered_citizen(repository, buyer)
            else:
                company = await self._owned_company(repository, buyer, company_id)
                if company != quote.buyer_company:
                    raise AssetError(AssetErrorReason.QUOTE_CHANGED)
                source = await repository.ensure_company_account(buyer.guild_id, company.id)
            treasury = await repository.ensure_account(buyer.guild_id, AccountKind.TREASURY)
            posting = await repository.post(
                buyer.guild_id,
                TransactionKind.ASSET_PURCHASE,
                f"asset_purchase:{asset.id}",
                {source.id: -asset.price, treasury.id: asset.price},
                actor_citizen_id=buyer.id,
                reason="primary_asset_purchase",
            )
            receipt = await repository.create_asset_purchase(
                guild_id=buyer.guild_id,
                asset_id=asset.id,
                buyer_citizen_id=buyer.id,
                buyer_company_id=company_id,
                buyer_account_id=source.id,
                price=asset.price,
                request_key=idempotency_key,
                transaction_id=posting.transaction_id,
                purchased_at=self._clock(),
            )
            if receipt is None:
                raise AssetError(AssetErrorReason.INVALID_REQUEST_KEY)
            await repository.assign_asset(
                guild_id=buyer.guild_id,
                asset_id=asset.id,
                owner_citizen_id=buyer.id if company_id is None else None,
                owner_company_id=company_id,
            )
            return receipt

    async def create_resale_offer(
        self, guild_id: int | None, user_id: int, *, asset_id: int, buyer_user_id: int,
        price: int, idempotency_key: str,
    ) -> AssetResale:
        self._authorize(guild_id, user_id)
        tax_bp = self._require_resale_tax()
        self._authorize(guild_id, buyer_user_id)
        if buyer_user_id == user_id:
            raise AssetError(AssetErrorReason.RESALE_SELF_TRADE)
        if not _valid_id(price):
            raise AssetError(AssetErrorReason.INVALID_PRICE)
        _validate_key(idempotency_key)
        async with self._unit_of_work() as repository:
            citizens = await repository.get_citizens(self._guild_id, (user_id, buyer_user_id), lock=True)
            verified = {citizen.discord_user_id: citizen for citizen in citizens if citizen.status == CitizenStatus.VERIFIED}
            if user_id not in verified or buyer_user_id not in verified:
                raise AssetError(AssetErrorReason.CITIZEN_NOT_VERIFIED)
            seller, buyer = verified[user_id], verified[buyer_user_id]
            prior = await repository.get_resale_offer_by_key(seller.guild_id, idempotency_key)
            if prior is None:
                await self._lock_personal_asset(repository, seller, asset_id)
                created = await repository.create_resale_offer(
                    guild_id=seller.guild_id, asset_id=asset_id, seller_citizen_id=seller.id,
                    buyer_citizen_id=buyer.id, price=price, tax_bp=tax_bp,
                    request_key=idempotency_key, created_at=self._clock(),
                )
                if created is not None:
                    return created
                prior = await repository.get_resale_offer_by_key(seller.guild_id, idempotency_key)
            if prior is None:
                raise AssetError(AssetErrorReason.RESALE_ALREADY_OPEN)
            if (prior.asset_id, prior.seller_citizen_id, prior.buyer_citizen_id, prior.price) != (
                asset_id, seller.id, buyer.id, price
            ):
                raise AssetError(AssetErrorReason.INVALID_REQUEST_KEY)
            return prior

    async def list_resale_offers(
        self, guild_id: int | None, user_id: int, *, page: int = 1,
    ) -> tuple[AssetResale, ...]:
        self._authorize(guild_id, user_id)
        self._require_resale_tax()
        offset = _page_offset(page)
        async with self._unit_of_work() as repository:
            actor = await self._citizen(repository, user_id)
            return await repository.list_resale_offers(actor.guild_id, actor.id, ASSET_PAGE_SIZE, offset=offset)

    async def get_resale_offer(self, guild_id: int | None, user_id: int, offer_id: int) -> AssetResale:
        self._authorize(guild_id, user_id)
        self._require_resale_tax()
        async with self._unit_of_work() as repository:
            actor = await self._citizen(repository, user_id)
            return await self._resale_offer(repository, actor, offer_id)

    async def quote_resale(self, guild_id: int | None, user_id: int, offer_id: int) -> AssetResaleQuote:
        self._authorize(guild_id, user_id)
        self._require_resale_tax()
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id)
            offer = await self._resale_offer(repository, buyer, offer_id)
            if offer.buyer_citizen_id != buyer.id:
                raise AssetError(AssetErrorReason.RESALE_NOT_FOUND)
            if offer.status == AssetResaleStatus.CANCELLED:
                raise AssetError(AssetErrorReason.RESALE_NOT_OPEN)
            asset = await repository.get_asset(buyer.guild_id, offer.asset_id)
            if asset is None:
                raise AssetError(AssetErrorReason.ASSET_NOT_FOUND)
            return AssetResaleQuote(offer, asset)

    async def buy_resale(self, guild_id: int | None, user_id: int, offer_id: int) -> AssetResale:
        self._authorize(guild_id, user_id)
        self._require_resale_tax()
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id)
            offer = await self._resale_offer(repository, buyer, offer_id)
            if offer.buyer_citizen_id != buyer.id:
                raise AssetError(AssetErrorReason.RESALE_NOT_FOUND)
            if offer.status == AssetResaleStatus.COMPLETED:
                return offer
            if offer.status != AssetResaleStatus.OPEN:
                raise AssetError(AssetErrorReason.RESALE_NOT_OPEN)

            locked = await repository.get_resale_citizens(buyer.guild_id, (buyer.id, offer.seller_citizen_id))
            verified = {citizen.id: citizen for citizen in locked if citizen.status == CitizenStatus.VERIFIED}
            if buyer.id not in verified or offer.seller_citizen_id not in verified:
                raise AssetError(AssetErrorReason.CITIZEN_NOT_VERIFIED)
            seller = verified[offer.seller_citizen_id]
            self._authorize(guild_id, seller.discord_user_id)
            asset = await repository.get_asset(buyer.guild_id, offer.asset_id, lock=True)
            offer = await self._resale_offer(repository, buyer, offer_id, lock=True)
            if offer.status == AssetResaleStatus.COMPLETED:
                return offer
            if offer.status != AssetResaleStatus.OPEN:
                raise AssetError(AssetErrorReason.RESALE_NOT_OPEN)
            if asset is None or asset.owner_citizen_id != seller.id:
                raise AssetError(AssetErrorReason.ASSET_NOT_OWNED)

            debit = await self._funding.fund_registered_citizen(repository, verified[buyer.id])
            credit = await self._funding.fund_registered_citizen(repository, seller)
            tax = offer.tax
            entries = {debit.id: -offer.price, credit.id: offer.price - tax}
            if tax:
                treasury = await repository.ensure_account(buyer.guild_id, AccountKind.TREASURY)
                entries[treasury.id] = tax
            posting = await repository.post(
                buyer.guild_id, TransactionKind.ASSET_RESALE, f"asset_resale:{offer.id}", entries,
                actor_citizen_id=buyer.id, reason="asset_resale",
            )
            completed = replace(
                offer, status=AssetResaleStatus.COMPLETED, transaction_id=posting.transaction_id,
                closed_at=self._clock(),
            )
            await repository.update_resale_offer(completed)
            await repository.assign_asset(
                guild_id=buyer.guild_id, asset_id=offer.asset_id, owner_citizen_id=buyer.id, owner_company_id=None,
            )
            return completed

    async def cancel_resale_offer(self, guild_id: int | None, user_id: int, offer_id: int) -> AssetResale:
        self._authorize(guild_id, user_id)
        self._require_resale_tax()
        async with self._unit_of_work() as repository:
            seller = await self._citizen(repository, user_id, lock=True)
            offer = await self._resale_offer(repository, seller, offer_id, lock=True)
            if offer.seller_citizen_id != seller.id:
                raise AssetError(AssetErrorReason.RESALE_NOT_FOUND)
            if offer.status == AssetResaleStatus.CANCELLED:
                return offer
            if offer.status != AssetResaleStatus.OPEN:
                raise AssetError(AssetErrorReason.RESALE_NOT_OPEN)
            cancelled = replace(offer, status=AssetResaleStatus.CANCELLED, closed_at=self._clock())
            await repository.update_resale_offer(cancelled)
            return cancelled

    def _require_resale_tax(self) -> int:
        if self._resale_tax_bp is None:
            raise AssetError(AssetErrorReason.RESALE_DISABLED)
        return self._resale_tax_bp

    @staticmethod
    async def _resale_offer(
        repository: AssetRepository, actor: Citizen, offer_id: int, *, lock: bool = False,
    ) -> AssetResale:
        if not _valid_id(offer_id):
            raise AssetError(AssetErrorReason.RESALE_NOT_FOUND)
        offer = await repository.get_resale_offer(actor.guild_id, offer_id, lock=lock)
        if offer is None or actor.id not in (offer.buyer_citizen_id, offer.seller_citizen_id):
            raise AssetError(AssetErrorReason.RESALE_NOT_FOUND)
        return offer

    @staticmethod
    async def _lock_personal_asset(repository: AssetRepository, seller: Citizen, asset_id: int) -> None:
        if not _valid_id(asset_id):
            raise AssetError(AssetErrorReason.ASSET_NOT_FOUND)
        asset = await repository.get_asset(seller.guild_id, asset_id, lock=True)
        if asset is None:
            raise AssetError(AssetErrorReason.ASSET_NOT_FOUND)
        if asset.owner_citizen_id != seller.id:
            raise AssetError(AssetErrorReason.ASSET_NOT_OWNED)

    async def _citizen(self, repository: AssetRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise AssetError(AssetErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    @staticmethod
    async def _available_asset(
        repository: AssetRepository, guild_id: int, asset_id: int, *, lock: bool = False
    ) -> Asset:
        if not _valid_id(asset_id):
            raise AssetError(AssetErrorReason.ASSET_NOT_FOUND)
        asset = await repository.get_asset(guild_id, asset_id, lock=lock)
        if asset is None:
            raise AssetError(AssetErrorReason.ASSET_NOT_FOUND)
        if asset.status != AssetStatus.FOR_SALE:
            raise AssetError(AssetErrorReason.ASSET_SOLD)
        return asset

    @staticmethod
    async def _owned_company(repository: AssetRepository, citizen: Citizen, company_id: int) -> Company:
        if not _valid_id(company_id):
            raise AssetError(AssetErrorReason.COMPANY_NOT_FOUND)
        company = await repository.get_company(citizen.guild_id, company_id)
        if company is None:
            raise AssetError(AssetErrorReason.COMPANY_NOT_FOUND)
        if company.owner_citizen_id != citizen.id:
            raise AssetError(AssetErrorReason.COMPANY_NOT_OWNED)
        return company
