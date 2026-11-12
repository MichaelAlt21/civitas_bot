from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.market import MarketError, MarketErrorReason, MarketRepository, MarketRules, MarketService
from civitas.app.wallet import WalletFunding
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import Account, AccountKind, PostResult, TransactionKind
from civitas.domain.market import Company, MarketOrder, Offer, OrderStatus, sales_tax

DISCORD_GUILD = 100
GUILD = 1
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
SELLER = Citizen(1, GUILD, 200, CitizenStatus.VERIFIED, NOW, NOW)
BUYER = Citizen(2, GUILD, 201, CitizenStatus.VERIFIED, NOW, NOW)
ARBITER = Citizen(3, GUILD, 202, CitizenStatus.VERIFIED, NOW, NOW)
SELLER_COMPANY = Company(10, GUILD, SELLER.id, SELLER.discord_user_id, "Сервис", "сервис", "Услуги", 150, "seller", NOW)
BUYER_COMPANY = Company(11, GUILD, BUYER.id, BUYER.discord_user_id, "Артель", "артель", "Услуги", 150, "buyer", NOW)
OFFER = Offer(12, GUILD, SELLER_COMPANY.id, "Доставка", "Два рейса", 200, 24, "offer", NOW)
PERSONAL = Account(20, GUILD, AccountKind.PERSONAL, BUYER.id)
TREASURY = Account(21, GUILD, AccountKind.TREASURY, None)
SELLER_ACCOUNT = Account(22, GUILD, AccountKind.COMPANY, None, company_id=SELLER_COMPANY.id)
BUYER_ACCOUNT = Account(23, GUILD, AccountKind.COMPANY, None, company_id=BUYER_COMPANY.id)
ESCROW = Account(24, GUILD, AccountKind.ESCROW, None, order_id=30)
FUNDED = MarketOrder(
    30, GUILD, OFFER.id, SELLER_COMPANY.id, SELLER.id, BUYER.id, None, PERSONAL.id,
    OFFER.title, OFFER.terms, OFFER.price, 1000, NOW + timedelta(hours=24), NOW, OrderStatus.FUNDED, "order",
)
RULES = MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1)


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=MarketRepository)
    orders_by_key: dict[str, MarketOrder] = {}

    def get_citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        return tuple(citizen for citizen in (SELLER, BUYER, ARBITER) if citizen.discord_user_id in user_ids)

    def create_company(**values: object) -> Company:
        company = Company(id=BUYER_COMPANY.id, **values)
        repo.get_company_by_key.return_value = company
        return company

    def create_order(**values: object) -> MarketOrder:
        order = MarketOrder(id=FUNDED.id, status=OrderStatus.FUNDED, **values)
        orders_by_key[order.request_key] = order
        repo.get_order.return_value = order
        return order

    def update_order(order: MarketOrder) -> None:
        repo.get_order.return_value = order

    repo.get_citizens.side_effect = get_citizens
    repo.get_company.side_effect = lambda guild_id, company_id: {
        SELLER_COMPANY.id: SELLER_COMPANY,
        BUYER_COMPANY.id: BUYER_COMPANY,
    }.get(company_id)
    repo.get_company_by_key.return_value = None
    repo.list_owned_companies.return_value = ()
    repo.create_company.side_effect = create_company
    repo.get_offer.return_value = OFFER
    repo.get_offer_by_key.return_value = None
    repo.create_offer.return_value = OFFER
    repo.get_order.return_value = FUNDED
    repo.get_order_by_key.side_effect = lambda guild_id, key: orders_by_key.get(key)
    repo.create_order.side_effect = create_order
    repo.update_order.side_effect = update_order
    repo.get_post.return_value = PostResult(3, False)
    repo.ensure_account.side_effect = lambda guild_id, kind, citizen_id=None: {
        AccountKind.PERSONAL: PERSONAL,
        AccountKind.TREASURY: TREASURY,
    }[kind]
    repo.ensure_company_account.side_effect = lambda guild_id, company_id: {
        SELLER_COMPANY.id: SELLER_ACCOUNT,
        BUYER_COMPANY.id: BUYER_ACCOUNT,
    }[company_id]
    repo.ensure_escrow_account.return_value = ESCROW
    repo.get_balance.return_value = 180
    return repo


def service(
    repository: AsyncMock,
    *,
    rules: MarketRules = RULES,
    now: datetime = NOW,
    arbiters: tuple[int, ...] = (ARBITER.discord_user_id,),
) -> MarketService:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[MarketRepository]:
        yield repository

    citizens = CitizensService(
        lambda: pytest.fail("Authorization should not use a separate transaction"),
        guild_id=DISCORD_GUILD,
        allowed_user_ids=[SELLER.discord_user_id, BUYER.discord_user_id, ARBITER.discord_user_id],
    )
    return MarketService(
        unit_of_work,
        guild_id=DISCORD_GUILD,
        authorize=citizens.authorize,
        arbiter_user_ids=arbiters,
        rules=rules,
        funding=WalletFunding(starter_grant=250, treasury_genesis=8000),
        clock=lambda: now,
    )


@pytest.mark.asyncio
async def test_company_registration_creates_separate_account_and_charges_only_once(repository: AsyncMock) -> None:
    market = service(repository)
    arguments = {"activity": "Доставка", "idempotency_key": "register", "expected_fee": 150}
    created = await market.register_company(DISCORD_GUILD, BUYER.discord_user_id, name="  АРТЕЛЬ  ", **arguments)
    repeated = await market.register_company(DISCORD_GUILD, BUYER.discord_user_id, name="артель", **arguments)

    assert created == repeated
    assert (created.name, created.name_key, created.activity) == ("АРТЕЛЬ", "артель", "Доставка")
    repository.create_company.assert_awaited_once()
    repository.ensure_company_account.assert_awaited_once_with(GUILD, created.id)
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.COMPANY_FEE,
        f"company_fee:{created.id}",
        {PERSONAL.id: -150, TREASURY.id: 150},
        actor_citizen_id=BUYER.id,
        reason="company_registration",
    )


@pytest.mark.asyncio
async def test_company_cap_and_conflicting_name_precede_money_movements(repository: AsyncMock) -> None:
    market = service(repository)
    repository.list_owned_companies.return_value = (BUYER_COMPANY,)
    with pytest.raises(MarketError, match=MarketErrorReason.COMPANY_LIMIT):
        await market.register_company(DISCORD_GUILD, BUYER.discord_user_id, name="Новая", activity="Услуги", idempotency_key="a")
    repository.create_company.assert_not_awaited()

    repository.list_owned_companies.return_value = ()
    repository.create_company.side_effect = None
    repository.create_company.return_value = None
    with pytest.raises(MarketError, match=MarketErrorReason.NAME_TAKEN):
        await market.register_company(DISCORD_GUILD, BUYER.discord_user_id, name="Сервис", activity="Услуги", idempotency_key="b")
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_offer_and_company_balance_require_company_owner(repository: AsyncMock) -> None:
    market = service(repository)
    assert await market.company_info(DISCORD_GUILD, BUYER.discord_user_id, SELLER_COMPANY.id) == SELLER_COMPANY
    with pytest.raises(MarketError, match=MarketErrorReason.COMPANY_NOT_OWNED):
        await market.company_balance(DISCORD_GUILD, BUYER.discord_user_id, SELLER_COMPANY.id)
    with pytest.raises(MarketError, match=MarketErrorReason.COMPANY_NOT_OWNED):
        await market.publish_offer(
            DISCORD_GUILD, BUYER.discord_user_id, company_id=SELLER_COMPANY.id, title=OFFER.title,
            terms=OFFER.terms, price=OFFER.price, delivery_hours=OFFER.delivery_hours, idempotency_key="offer",
        )
    repository.create_offer.assert_not_awaited()
    assert await market.company_balance(DISCORD_GUILD, SELLER.discord_user_id, SELLER_COMPANY.id) == 180


@pytest.mark.asyncio
async def test_funding_preserves_quote_and_replays_after_tax_change(repository: AsyncMock) -> None:
    market = service(repository)
    quote = await market.quote_order(DISCORD_GUILD, BUYER.discord_user_id, OFFER.id)
    assert quote.tax == 20
    assert quote.deadline == NOW + timedelta(hours=24)
    assert quote.seller == SELLER_COMPANY
    repository.post.assert_not_awaited()

    order = await market.fund_order(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="order")
    changed = service(repository, rules=replace(RULES, sales_tax_bp=1500))
    assert await changed.fund_order(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="order") == order
    with pytest.raises(MarketError, match=MarketErrorReason.QUOTE_CHANGED):
        await changed.fund_order(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="new")
    assert (await changed.quote_order(DISCORD_GUILD, BUYER.discord_user_id, OFFER.id)).tax == 30
    repository.create_order.assert_awaited_once()
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.ORDER_FUND,
        f"order_fund:{order.id}",
        {PERSONAL.id: -200, ESCROW.id: 200},
        actor_citizen_id=BUYER.id,
        reason="confirmed_order",
    )


@pytest.mark.asyncio
async def test_corporate_buyer_is_authorized_and_uses_company_account(repository: AsyncMock) -> None:
    market = service(repository)
    with pytest.raises(MarketError, match=MarketErrorReason.SELF_TRADE):
        await market.quote_order(DISCORD_GUILD, SELLER.discord_user_id, OFFER.id)
    with pytest.raises(MarketError, match=MarketErrorReason.COMPANY_NOT_OWNED):
        await market.quote_order(DISCORD_GUILD, ARBITER.discord_user_id, OFFER.id, buyer_company_id=BUYER_COMPANY.id)

    quote = await market.quote_order(DISCORD_GUILD, BUYER.discord_user_id, OFFER.id, buyer_company_id=BUYER_COMPANY.id)
    order = await market.fund_order(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="corporate")
    assert (order.buyer_company_id, order.buyer_account_id) == (BUYER_COMPANY.id, BUYER_ACCOUNT.id)
    assert repository.post.await_args.args[3] == {BUYER_ACCOUNT.id: -200, ESCROW.id: 200}
    repository.ensure_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_requires_delivery_and_uses_frozen_tax_once(repository: AsyncMock) -> None:
    market = service(repository, rules=replace(RULES, sales_tax_bp=1500))
    with pytest.raises(MarketError, match=MarketErrorReason.INVALID_STATUS):
        await market.accept_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)
    with pytest.raises(MarketError, match=MarketErrorReason.NOT_ORDER_PARTY):
        await market.deliver_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)
    delivered = await market.deliver_order(DISCORD_GUILD, SELLER.discord_user_id, FUNDED.id)
    assert delivered.status == OrderStatus.DELIVERED
    with pytest.raises(MarketError, match=MarketErrorReason.INVALID_STATUS):
        await market.cancel_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)

    completed = await market.accept_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)
    assert await market.accept_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id) == completed
    assert completed.status == OrderStatus.COMPLETED
    assert completed.tax == 20
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.ORDER_SETTLE,
        f"order_settle:{FUNDED.id}",
        {ESCROW.id: -200, SELLER_ACCOUNT.id: 180, TREASURY.id: 20},
        actor_citizen_id=BUYER.id,
        reason="buyer_accepted",
    )


@pytest.mark.asyncio
async def test_pre_delivery_cancellation_refunds_original_corporate_account(repository: AsyncMock) -> None:
    repository.get_order.return_value = replace(
        FUNDED, buyer_company_id=BUYER_COMPANY.id, buyer_account_id=BUYER_ACCOUNT.id
    )
    market = service(repository)
    refunded = await market.cancel_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)

    assert refunded.status == OrderStatus.REFUNDED
    assert await market.cancel_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id) == refunded
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.ORDER_REFUND,
        f"order_refund:{FUNDED.id}",
        {ESCROW.id: -200, BUYER_ACCOUNT.id: 200},
        actor_citizen_id=BUYER.id,
        reason="buyer_cancelled",
    )
    repository.ensure_account.assert_not_awaited()
    repository.ensure_company_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbitration_is_visible_only_to_parties_and_independent_arbiter(repository: AsyncMock) -> None:
    market = service(repository, arbiters=(SELLER.discord_user_id, ARBITER.discord_user_id))
    with pytest.raises(CitizenshipAccessError):
        await market.get_order(None, BUYER.discord_user_id, FUNDED.id)
    with pytest.raises(MarketError, match=MarketErrorReason.NOT_ORDER_PARTY):
        await market.get_order(DISCORD_GUILD, ARBITER.discord_user_id, FUNDED.id)
    disputed = await market.dispute_order(DISCORD_GUILD, SELLER.discord_user_id, FUNDED.id, reason="Нужна проверка")
    assert disputed.status == OrderStatus.DISPUTED
    assert await market.get_order(DISCORD_GUILD, ARBITER.discord_user_id, FUNDED.id) == disputed
    with pytest.raises(MarketError, match=MarketErrorReason.ARBITER_REQUIRED):
        await market.resolve_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id, settle=False, reason="Проверено")
    with pytest.raises(MarketError, match=MarketErrorReason.ARBITER_CONFLICT):
        await market.resolve_order(DISCORD_GUILD, SELLER.discord_user_id, FUNDED.id, settle=False, reason="Проверено")

    refunded = await market.resolve_order(DISCORD_GUILD, ARBITER.discord_user_id, FUNDED.id, settle=False, reason="Проверено")
    assert refunded.status == OrderStatus.REFUNDED
    assert refunded.dispute_reason == "Нужна проверка"
    assert refunded.close_reason == "Проверено"
    assert refunded.closed_by_citizen_id == ARBITER.id
    repository.post.assert_awaited_once()


@pytest.mark.parametrize(("tax_bp", "credit"), [(0, SELLER_ACCOUNT), (10000, TREASURY)])
@pytest.mark.asyncio
async def test_zero_ledger_deltas_are_omitted_at_tax_boundaries(
    repository: AsyncMock, tax_bp: int, credit: Account
) -> None:
    repository.get_order.return_value = replace(FUNDED, status=OrderStatus.DELIVERED, delivered_at=NOW, tax_bp=tax_bp)

    await service(repository).accept_order(DISCORD_GUILD, BUYER.discord_user_id, FUNDED.id)

    assert repository.post.await_args.args[3] == {ESCROW.id: -200, credit.id: 200}
    assert sales_tax(19, 1000) == 1


@pytest.mark.asyncio
async def test_seller_cannot_deliver_after_deadline(repository: AsyncMock) -> None:
    market = service(repository, now=FUNDED.deadline)

    with pytest.raises(MarketError, match=MarketErrorReason.DEADLINE_PASSED):
        await market.deliver_order(DISCORD_GUILD, SELLER.discord_user_id, FUNDED.id)

    repository.update_order.assert_not_awaited()
    repository.post.assert_not_awaited()
