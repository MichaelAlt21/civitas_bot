"""Company registration and settlement of escrow-funded service orders."""

import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id
from civitas.app.wallet import WalletFunding, WalletRepository
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH, Account, AccountKind, TransactionKind
from civitas.domain.market import BASIS_POINTS, MAX_DELIVERY_HOURS, Company, MarketOrder, Offer, OrderQuote, OrderStatus, sales_tax

_LIST_LIMIT = 10


class MarketErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    COMPANY_NOT_FOUND = "company_not_found"
    COMPANY_NOT_OWNED = "company_not_owned"
    NAME_TAKEN = "name_taken"
    COMPANY_LIMIT = "company_limit"
    OFFER_NOT_FOUND = "offer_not_found"
    ORDER_NOT_FOUND = "order_not_found"
    NOT_ORDER_PARTY = "not_order_party"
    ARBITER_REQUIRED = "arbiter_required"
    ARBITER_CONFLICT = "arbiter_conflict"
    SELF_TRADE = "self_trade"
    INVALID_PRICE = "invalid_price"
    INVALID_TEXT = "invalid_text"
    INVALID_DEADLINE = "invalid_deadline"
    INVALID_REQUEST_KEY = "invalid_request_key"
    QUOTE_CHANGED = "quote_changed"
    INVALID_STATUS = "invalid_status"
    DEADLINE_PASSED = "deadline_passed"


class MarketError(Exception):
    def __init__(self, reason: MarketErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketRules:
    registration_fee: int
    sales_tax_bp: int
    company_limit: int

    def __post_init__(self) -> None:
        if type(self.registration_fee) is not int or not 0 <= self.registration_fee <= MAX_AMOUNT:
            raise ValueError("Company registration fee must be a non-negative BIGINT integer")
        if type(self.sales_tax_bp) is not int or not 0 <= self.sales_tax_bp <= BASIS_POINTS:
            raise ValueError("Sales tax must be between 0 and 10,000 basis points")
        if type(self.company_limit) is not int or not 1 <= self.company_limit <= MAX_AMOUNT:
            raise ValueError("Company limit must be a positive BIGINT integer")


class MarketRepository(WalletRepository, Protocol):
    async def ensure_company_account(self, guild_id: int, company_id: int) -> Account: ...

    async def ensure_escrow_account(self, guild_id: int, order_id: int) -> Account: ...

    async def get_company(self, guild_id: int, company_id: int) -> Company | None: ...

    async def get_company_by_key(self, guild_id: int, key: str) -> Company | None: ...

    async def list_owned_companies(self, guild_id: int, citizen_id: int) -> tuple[Company, ...]: ...

    async def create_company(
        self,
        *,
        guild_id: int,
        owner_citizen_id: int,
        owner_discord_user_id: int,
        name: str,
        name_key: str,
        activity: str,
        fee_paid: int,
        registration_key: str,
        created_at: datetime,
    ) -> Company | None: ...

    async def get_offer(self, guild_id: int, offer_id: int) -> Offer | None: ...

    async def get_offer_by_key(self, guild_id: int, key: str) -> Offer | None: ...

    async def list_offers(self, guild_id: int, limit: int) -> tuple[Offer, ...]: ...

    async def create_offer(
        self,
        *,
        guild_id: int,
        company_id: int,
        title: str,
        terms: str,
        price: int,
        delivery_hours: int,
        request_key: str,
        created_at: datetime,
    ) -> Offer | None: ...

    async def get_order(self, guild_id: int, order_id: int, *, lock: bool = False) -> MarketOrder | None: ...

    async def get_order_by_key(self, guild_id: int, key: str) -> MarketOrder | None: ...

    async def list_orders(
        self, guild_id: int, citizen_id: int, *, include_disputes: bool, limit: int
    ) -> tuple[MarketOrder, ...]: ...

    async def create_order(
        self,
        *,
        guild_id: int,
        offer_id: int,
        seller_company_id: int,
        seller_citizen_id: int,
        buyer_citizen_id: int,
        buyer_company_id: int | None,
        buyer_account_id: int,
        title: str,
        terms: str,
        price: int,
        tax_bp: int,
        deadline: datetime,
        created_at: datetime,
        request_key: str,
    ) -> MarketOrder | None: ...

    async def update_order(self, order: MarketOrder) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise MarketError(MarketErrorReason.INVALID_TEXT)
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise MarketError(MarketErrorReason.INVALID_TEXT)
    return value


def _validate_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or "\0" in key
    ):
        raise MarketError(MarketErrorReason.INVALID_REQUEST_KEY)


def _valid_id(value: int) -> bool:
    return type(value) is int and 1 <= value <= MAX_AMOUNT


class MarketService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[MarketRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        arbiter_user_ids: Iterable[int],
        rules: MarketRules,
        funding: WalletFunding,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        arbiter_ids = tuple(arbiter_user_ids)
        if not all(is_discord_id(user_id) for user_id in arbiter_ids):
            raise ValueError("Arbiter IDs must be Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._arbiter_user_ids = frozenset(arbiter_ids)
        self._rules = rules
        self._funding = funding
        self._clock = clock

    @property
    def registration_fee(self) -> int:
        return self._rules.registration_fee

    async def register_company(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        name: str,
        activity: str,
        idempotency_key: str,
        expected_fee: int | None = None,
    ) -> Company:
        self._authorize(guild_id, user_id)
        _validate_key(idempotency_key)
        if not isinstance(name, str):
            raise MarketError(MarketErrorReason.INVALID_TEXT)
        name = _text(" ".join(unicodedata.normalize("NFKC", name).split()), 80)
        name_key = name.casefold()
        activity = _text(activity, 200)
        async with self._unit_of_work() as repository:
            owner = await self._citizen(repository, user_id, lock=True)
            prior = await repository.get_company_by_key(owner.guild_id, idempotency_key)
            if prior is not None:
                return self._replay_company(prior, owner.id, name_key, activity, expected_fee)
            fee = self.registration_fee
            if expected_fee is not None and expected_fee != fee:
                raise MarketError(MarketErrorReason.QUOTE_CHANGED)
            owned = await repository.list_owned_companies(owner.guild_id, owner.id)
            if len(owned) >= self._rules.company_limit:
                raise MarketError(MarketErrorReason.COMPANY_LIMIT)
            company = await repository.create_company(
                guild_id=owner.guild_id,
                owner_citizen_id=owner.id,
                owner_discord_user_id=owner.discord_user_id,
                name=name,
                name_key=name_key,
                activity=activity,
                fee_paid=fee,
                registration_key=idempotency_key,
                created_at=self._clock(),
            )
            if company is None:
                prior = await repository.get_company_by_key(owner.guild_id, idempotency_key)
                if prior is not None:
                    return self._replay_company(prior, owner.id, name_key, activity, expected_fee)
                raise MarketError(MarketErrorReason.NAME_TAKEN)

            await repository.ensure_company_account(owner.guild_id, company.id)
            if fee:
                source = await self._funding.fund_registered_citizen(repository, owner)
                treasury = await repository.ensure_account(owner.guild_id, AccountKind.TREASURY)
                await repository.post(
                    owner.guild_id,
                    TransactionKind.COMPANY_FEE,
                    f"company_fee:{company.id}",
                    {source.id: -fee, treasury.id: fee},
                    actor_citizen_id=owner.id,
                    reason="company_registration",
                )
            return company

    async def company_info(self, guild_id: int | None, user_id: int, company_id: int) -> Company:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await self._company(repository, citizen.guild_id, company_id)

    async def company_balance(self, guild_id: int | None, user_id: int, company_id: int) -> int:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            owner = await self._citizen(repository, user_id)
            company = await self._owned_company(repository, owner, company_id)
            account = await repository.ensure_company_account(owner.guild_id, company.id)
            return await repository.get_balance(account.id)

    async def list_owned_companies(self, guild_id: int | None, user_id: int) -> tuple[Company, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            owner = await self._citizen(repository, user_id)
            return await repository.list_owned_companies(owner.guild_id, owner.id)

    async def publish_offer(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        company_id: int,
        title: str,
        terms: str,
        price: int,
        delivery_hours: int,
        idempotency_key: str,
    ) -> Offer:
        self._authorize(guild_id, user_id)
        _validate_key(idempotency_key)
        title = _text(title, 80)
        terms = _text(terms, 500)
        if type(price) is not int or not 1 <= price <= MAX_AMOUNT:
            raise MarketError(MarketErrorReason.INVALID_PRICE)
        if type(delivery_hours) is not int or not 1 <= delivery_hours <= MAX_DELIVERY_HOURS:
            raise MarketError(MarketErrorReason.INVALID_DEADLINE)
        async with self._unit_of_work() as repository:
            owner = await self._citizen(repository, user_id, lock=True)
            company = await self._owned_company(repository, owner, company_id)
            prior = await repository.get_offer_by_key(owner.guild_id, idempotency_key)
            if prior is None:
                offer = await repository.create_offer(
                    guild_id=owner.guild_id,
                    company_id=company.id,
                    title=title,
                    terms=terms,
                    price=price,
                    delivery_hours=delivery_hours,
                    request_key=idempotency_key,
                    created_at=self._clock(),
                )
                if offer is not None:
                    return offer
                prior = await repository.get_offer_by_key(owner.guild_id, idempotency_key)
            if prior is None or (prior.company_id, prior.title, prior.terms, prior.price, prior.delivery_hours) != (
                company.id, title, terms, price, delivery_hours
            ):
                raise MarketError(MarketErrorReason.INVALID_REQUEST_KEY)
            return prior

    async def list_offers(self, guild_id: int | None, user_id: int) -> tuple[Offer, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await repository.list_offers(citizen.guild_id, _LIST_LIMIT)

    async def quote_order(
        self, guild_id: int | None, user_id: int, offer_id: int, *, buyer_company_id: int | None = None
    ) -> OrderQuote:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id)
            offer, seller = await self._trade_parties(repository, buyer, offer_id, buyer_company_id)
            tax_bp = self._rules.sales_tax_bp
            deadline = self._clock() + timedelta(hours=offer.delivery_hours)
            return OrderQuote(offer, seller, buyer_company_id, tax_bp, sales_tax(offer.price, tax_bp), deadline)

    async def fund_order(
        self, guild_id: int | None, user_id: int, quote: OrderQuote, *, idempotency_key: str
    ) -> MarketOrder:
        self._authorize(guild_id, user_id)
        _validate_key(idempotency_key)
        async with self._unit_of_work() as repository:
            buyer = await self._citizen(repository, user_id, lock=True)
            prior = await repository.get_order_by_key(buyer.guild_id, idempotency_key)
            if prior is not None:
                return self._replay_order(prior, buyer.id, quote)
            offer, seller = await self._trade_parties(repository, buyer, quote.offer.id, quote.buyer_company_id)
            now = self._clock()
            if (
                quote.offer != offer
                or quote.seller != seller
                or quote.tax_bp != self._rules.sales_tax_bp
                or quote.tax != sales_tax(offer.price, quote.tax_bp)
                or quote.deadline.utcoffset() is None
                or not now < quote.deadline <= now + timedelta(hours=offer.delivery_hours)
            ):
                raise MarketError(MarketErrorReason.QUOTE_CHANGED)
            if quote.buyer_company_id is None:
                source = await self._funding.fund_registered_citizen(repository, buyer)
            else:
                source = await repository.ensure_company_account(buyer.guild_id, quote.buyer_company_id)
            order = await repository.create_order(
                guild_id=buyer.guild_id,
                offer_id=offer.id,
                seller_company_id=seller.id,
                seller_citizen_id=seller.owner_citizen_id,
                buyer_citizen_id=buyer.id,
                buyer_company_id=quote.buyer_company_id,
                buyer_account_id=source.id,
                title=offer.title,
                terms=offer.terms,
                price=offer.price,
                tax_bp=quote.tax_bp,
                deadline=quote.deadline,
                created_at=now,
                request_key=idempotency_key,
            )
            if order is None:
                prior = await repository.get_order_by_key(buyer.guild_id, idempotency_key)
                if prior is None:
                    raise MarketError(MarketErrorReason.INVALID_REQUEST_KEY)
                return self._replay_order(prior, buyer.id, quote)
            escrow = await repository.ensure_escrow_account(buyer.guild_id, order.id)
            await repository.post(
                buyer.guild_id,
                TransactionKind.ORDER_FUND,
                f"order_fund:{order.id}",
                {source.id: -order.price, escrow.id: order.price},
                actor_citizen_id=buyer.id,
                reason="confirmed_order",
            )
            return order

    async def list_orders(self, guild_id: int | None, user_id: int) -> tuple[MarketOrder, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await repository.list_orders(
                citizen.guild_id, citizen.id, include_disputes=user_id in self._arbiter_user_ids, limit=_LIST_LIMIT
            )

    async def get_order(self, guild_id: int | None, user_id: int, order_id: int) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id, lock=False) as (_, citizen, order):
            if citizen.id not in (order.buyer_citizen_id, order.seller_citizen_id) and not (
                user_id in self._arbiter_user_ids and order.disputed_at is not None
            ):
                raise MarketError(MarketErrorReason.NOT_ORDER_PARTY)
            return order

    async def deliver_order(self, guild_id: int | None, user_id: int, order_id: int) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id) as (repository, citizen, order):
            self._require_party(citizen.id, order.seller_citizen_id)
            if order.delivered_at is not None:
                return order
            self._require_status(order, OrderStatus.FUNDED)
            now = self._clock()
            if now >= order.deadline:
                raise MarketError(MarketErrorReason.DEADLINE_PASSED)
            delivered = replace(order, status=OrderStatus.DELIVERED, delivered_at=now)
            await repository.update_order(delivered)
            return delivered

    async def accept_order(self, guild_id: int | None, user_id: int, order_id: int) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id) as (repository, citizen, order):
            self._require_party(citizen.id, order.buyer_citizen_id)
            if order.status == OrderStatus.COMPLETED:
                return order
            self._require_status(order, OrderStatus.DELIVERED)
            return await self._close_order(repository, order, citizen.id, OrderStatus.COMPLETED, "buyer_accepted")

    async def cancel_order(self, guild_id: int | None, user_id: int, order_id: int) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id) as (repository, citizen, order):
            self._require_party(citizen.id, order.buyer_citizen_id)
            if order.status == OrderStatus.REFUNDED:
                return order
            self._require_status(order, OrderStatus.FUNDED)
            return await self._close_order(repository, order, citizen.id, OrderStatus.REFUNDED, "buyer_cancelled")

    async def dispute_order(
        self, guild_id: int | None, user_id: int, order_id: int, *, reason: str
    ) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id) as (repository, citizen, order):
            self._require_party(citizen.id, order.buyer_citizen_id, order.seller_citizen_id)
            reason = _text(reason, 200)
            if order.disputed_by_citizen_id == citizen.id and order.dispute_reason == reason:
                return order
            self._require_status(order, OrderStatus.FUNDED, OrderStatus.DELIVERED)
            disputed = replace(
                order,
                status=OrderStatus.DISPUTED,
                disputed_at=self._clock(),
                disputed_by_citizen_id=citizen.id,
                dispute_reason=reason,
            )
            await repository.update_order(disputed)
            return disputed

    async def resolve_order(
        self, guild_id: int | None, user_id: int, order_id: int, *, settle: bool, reason: str
    ) -> MarketOrder:
        async with self._order_action(guild_id, user_id, order_id, arbiter=True) as (repository, citizen, order):
            if citizen.id in (order.buyer_citizen_id, order.seller_citizen_id):
                raise MarketError(MarketErrorReason.ARBITER_CONFLICT)
            reason = _text(reason, 200)
            decision = OrderStatus.COMPLETED if settle else OrderStatus.REFUNDED
            if order.status == decision and order.disputed_at is not None:
                return order
            self._require_status(order, OrderStatus.DISPUTED)
            return await self._close_order(repository, order, citizen.id, decision, reason)

    async def _close_order(
        self, repository: MarketRepository, order: MarketOrder, actor_id: int, decision: OrderStatus, reason: str
    ) -> MarketOrder:
        closed = replace(
            order, status=decision, closed_at=self._clock(), closed_by_citizen_id=actor_id, close_reason=reason
        )
        escrow = await repository.ensure_escrow_account(order.guild_id, order.id)
        entries = {escrow.id: -order.price}
        if decision == OrderStatus.COMPLETED:
            kind = TransactionKind.ORDER_SETTLE
            tax = order.tax
            if order.price > tax:
                seller = await repository.ensure_company_account(order.guild_id, order.seller_company_id)
                entries[seller.id] = order.price - tax
            if tax:
                treasury = await repository.ensure_account(order.guild_id, AccountKind.TREASURY)
                entries[treasury.id] = tax
        else:
            kind = TransactionKind.ORDER_REFUND
            entries[order.buyer_account_id] = order.price
        await repository.update_order(closed)
        await repository.post(
            order.guild_id,
            kind,
            f"{kind.value.lower()}:{order.id}",
            entries,
            actor_citizen_id=actor_id,
            reason=reason,
        )
        return closed

    @asynccontextmanager
    async def _order_action(
        self, guild_id: int | None, user_id: int, order_id: int, *, lock: bool = True, arbiter: bool = False
    ) -> AsyncIterator[tuple[MarketRepository, Citizen, MarketOrder]]:
        self._authorize(guild_id, user_id)
        if arbiter and user_id not in self._arbiter_user_ids:
            raise MarketError(MarketErrorReason.ARBITER_REQUIRED)
        if not _valid_id(order_id):
            raise MarketError(MarketErrorReason.ORDER_NOT_FOUND)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id, lock=lock)
            order = await repository.get_order(citizen.guild_id, order_id, lock=lock)
            if order is None:
                raise MarketError(MarketErrorReason.ORDER_NOT_FOUND)
            yield repository, citizen, order

    async def _citizen(self, repository: MarketRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise MarketError(MarketErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    @staticmethod
    async def _company(repository: MarketRepository, guild_id: int, company_id: int) -> Company:
        if not _valid_id(company_id):
            raise MarketError(MarketErrorReason.COMPANY_NOT_FOUND)
        company = await repository.get_company(guild_id, company_id)
        if company is None:
            raise MarketError(MarketErrorReason.COMPANY_NOT_FOUND)
        return company

    async def _owned_company(self, repository: MarketRepository, owner: Citizen, company_id: int) -> Company:
        company = await self._company(repository, owner.guild_id, company_id)
        if company.owner_citizen_id != owner.id:
            raise MarketError(MarketErrorReason.COMPANY_NOT_OWNED)
        return company

    async def _trade_parties(
        self, repository: MarketRepository, buyer: Citizen, offer_id: int, buyer_company_id: int | None
    ) -> tuple[Offer, Company]:
        if not _valid_id(offer_id):
            raise MarketError(MarketErrorReason.OFFER_NOT_FOUND)
        offer = await repository.get_offer(buyer.guild_id, offer_id)
        if offer is None:
            raise MarketError(MarketErrorReason.OFFER_NOT_FOUND)
        seller = await self._company(repository, buyer.guild_id, offer.company_id)
        if seller.owner_citizen_id == buyer.id:
            raise MarketError(MarketErrorReason.SELF_TRADE)
        if buyer_company_id is not None:
            await self._owned_company(repository, buyer, buyer_company_id)
        return offer, seller

    @staticmethod
    def _replay_company(
        company: Company, owner_id: int, name_key: str, activity: str, expected_fee: int | None
    ) -> Company:
        if (company.owner_citizen_id, company.name_key, company.activity) != (owner_id, name_key, activity) or (
            expected_fee is not None and expected_fee != company.fee_paid
        ):
            raise MarketError(MarketErrorReason.INVALID_REQUEST_KEY)
        return company

    @staticmethod
    def _replay_order(order: MarketOrder, buyer_id: int, quote: OrderQuote) -> MarketOrder:
        if (
            order.buyer_citizen_id != buyer_id
            or order.buyer_company_id != quote.buyer_company_id
            or order.offer_id != quote.offer.id
            or order.seller_company_id != quote.seller.id
            or (order.title, order.terms, order.price) != (quote.offer.title, quote.offer.terms, quote.offer.price)
            or order.tax_bp != quote.tax_bp
            or order.tax != quote.tax
            or order.deadline != quote.deadline
        ):
            raise MarketError(MarketErrorReason.INVALID_REQUEST_KEY)
        return order

    @staticmethod
    def _require_party(citizen_id: int, *authorized: int) -> None:
        if citizen_id not in authorized:
            raise MarketError(MarketErrorReason.NOT_ORDER_PARTY)

    @staticmethod
    def _require_status(order: MarketOrder, *expected: OrderStatus) -> None:
        if order.status not in expected:
            raise MarketError(MarketErrorReason.INVALID_STATUS)
