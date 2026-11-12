import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.app.market import MarketError, MarketErrorReason, MarketRules, MarketService
from civitas.app.wallet import WalletFunding
from civitas.domain.ledger import MAX_AMOUNT, AccountKind, TransactionKind
from civitas.domain.market import Company, MarketOrder, Offer, OrderStatus
from civitas.infrastructure.db.ledger_models import FinancialAccountRow, FinancialTransactionRow
from civitas.infrastructure.db.market import PostgresMarketUnitOfWork
from civitas.infrastructure.db.market_models import OrderRow, OrganizationRow

pytestmark = pytest.mark.asyncio

GUILD = 100
BUYER, SELLER, ARBITER, OTHER_ARBITER = 11, 12, 13, 14
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


def _market(
    unit_of_work: PostgresMarketUnitOfWork, citizens: CitizensService, *, tax_bp: int = 1000
) -> MarketService:
    return MarketService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize,
        arbiter_user_ids={SELLER, ARBITER, OTHER_ARBITER},
        rules=MarketRules(registration_fee=150, sales_tax_bp=tax_bp, company_limit=1),
        funding=WalletFunding(starter_grant=1000, treasury_genesis=8000),
        clock=lambda: NOW,
    )


async def _setup(
    engine: AsyncEngine,
) -> tuple[PostgresMarketUnitOfWork, CitizensService, MarketService]:
    unit_of_work = PostgresMarketUnitOfWork(engine)
    funding = WalletFunding(starter_grant=1000, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids={BUYER, SELLER, ARBITER, OTHER_ARBITER},
        fund_verified=funding.fund_registered_citizen,
    )
    for user_id in (BUYER, SELLER, ARBITER, OTHER_ARBITER):
        await citizens.register(GUILD, user_id)
    return unit_of_work, citizens, _market(unit_of_work, citizens)


async def _company(
    service: MarketService, owner: int = SELLER, name: str = "Civitas Studio"
) -> Company:
    return await service.register_company(
        GUILD, owner, name=name, activity="Design and illustration",
        idempotency_key=f"registration:{owner}", expected_fee=150,
    )


async def _offer(service: MarketService, company: Company, price: int = 200) -> Offer:
    return await service.publish_offer(
        GUILD, company.owner_discord_user_id, company_id=company.id, title="Town crest",
        terms="Deliver two sketches", price=price, delivery_hours=24,
        idempotency_key=f"offer:{company.id}",
    )


async def _order(
    service: MarketService, offer: Offer, key: str, *, buyer: int = BUYER,
    company: Company | None = None,
) -> MarketOrder:
    quote = await service.quote_order(GUILD, buyer, offer.id, buyer_company_id=company.id if company else None)
    return await service.fund_order(GUILD, buyer, quote, idempotency_key=key)


async def _balances(
    unit_of_work: PostgresMarketUnitOfWork, order: MarketOrder
) -> tuple[int, int, int, int]:
    async with unit_of_work() as repository:
        seller = await repository.ensure_company_account(order.guild_id, order.seller_company_id)
        treasury = await repository.ensure_account(order.guild_id, AccountKind.TREASURY)
        escrow = await repository.ensure_escrow_account(order.guild_id, order.id)
        return (
            await repository.get_balance(order.buyer_account_id),
            await repository.get_balance(seller.id),
            await repository.get_balance(treasury.id),
            await repository.get_balance(escrow.id),
        )


async def test_company_fee_and_repeated_registration_are_atomic(pg_engine: AsyncEngine) -> None:
    unit_of_work, _, service = await _setup(pg_engine)
    first, replay = await asyncio.wait_for(
        asyncio.gather(_company(service), _company(service)), timeout=10
    )
    assert replay == first
    assert first.fee_paid == 150
    assert await service.list_owned_companies(GUILD, SELLER) == (first,)
    with pytest.raises(MarketError) as failure:
        await _company(service, BUYER, "  civitas   studio ")
    assert failure.value.reason is MarketErrorReason.NAME_TAKEN
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(first.guild_id, AccountKind.PERSONAL, first.owner_citizen_id)
        company = await repository.ensure_company_account(first.guild_id, first.id)
        treasury = await repository.ensure_account(first.guild_id, AccountKind.TREASURY)
        assert [await repository.get_balance(a.id) for a in (personal, company, treasury)] == [850, 0, 8150]
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(OrganizationRow)) == 1
        assert await connection.scalar(
            select(func.count()).select_from(FinancialAccountRow)
            .where(FinancialAccountRow.kind == AccountKind.COMPANY)
        ) == 1
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind == TransactionKind.COMPANY_FEE)
        ) == 1


async def test_funded_tax_snapshot_survives_restart_and_concurrent_acceptance(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, service = await _setup(pg_engine)
    offer = await _offer(service, await _company(service))
    quote = await service.quote_order(GUILD, BUYER, offer.id)
    first, replay = await asyncio.wait_for(
        asyncio.gather(
            service.fund_order(GUILD, BUYER, quote, idempotency_key="first-order"),
            service.fund_order(GUILD, BUYER, quote, idempotency_key="first-order"),
        ),
        timeout=10,
    )
    assert replay == first
    assert await _balances(unit_of_work, first) == (800, 0, 8150, 200)
    updated = _market(PostgresMarketUnitOfWork(pg_engine), citizens, tax_bp=1500)

    await updated.deliver_order(GUILD, SELLER, first.id)
    accepted, repeated = await asyncio.wait_for(
        asyncio.gather(
            updated.accept_order(GUILD, BUYER, first.id),
            updated.accept_order(GUILD, BUYER, first.id),
        ),
        timeout=10,
    )
    assert accepted == repeated and accepted.status is OrderStatus.COMPLETED
    assert accepted.tax_bp == 1000 and accepted.tax == 20
    assert await _balances(unit_of_work, first) == (800, 180, 8170, 0)

    second = await _order(updated, offer, "second-order")
    await updated.deliver_order(GUILD, SELLER, second.id)
    accepted = await updated.accept_order(GUILD, BUYER, second.id)
    assert accepted.tax_bp == 1500 and accepted.tax == 30
    assert await _balances(unit_of_work, second) == (600, 350, 8200, 0)
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind == TransactionKind.ORDER_SETTLE)
        ) == 2


async def test_unperformed_order_returns_exact_price_once(pg_engine: AsyncEngine) -> None:
    unit_of_work, _, service = await _setup(pg_engine)
    offer = await _offer(service, await _company(service))
    order = await _order(service, offer, "cancel-order")
    assert await _balances(unit_of_work, order) == (800, 0, 8150, 200)
    first, repeated = await asyncio.wait_for(
        asyncio.gather(
            service.cancel_order(GUILD, BUYER, order.id), service.cancel_order(GUILD, BUYER, order.id)
        ),
        timeout=10,
    )
    assert first == repeated and first.status is OrderStatus.REFUNDED
    assert await _balances(unit_of_work, order) == (1000, 0, 8150, 0)


async def test_conflicting_arbiters_can_close_escrow_only_once(pg_engine: AsyncEngine) -> None:
    unit_of_work, _, service = await _setup(pg_engine)
    offer = await _offer(service, await _company(service))
    order = await _order(service, offer, "disputed-order")
    disputed = await service.dispute_order(GUILD, BUYER, order.id, reason="Delivery disagreed")
    with pytest.raises(MarketError) as conflict:
        await service.resolve_order(GUILD, SELLER, order.id, settle=True, reason="Party is not independent")
    assert conflict.value.reason is MarketErrorReason.ARBITER_CONFLICT

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            service.resolve_order(GUILD, ARBITER, order.id, settle=True, reason="Accept the work"),
            service.resolve_order(GUILD, OTHER_ARBITER, order.id, settle=False, reason="Return the money"),
            return_exceptions=True,
        ),
        timeout=10,
    )
    decisions = [item for item in outcomes if isinstance(item, MarketOrder)]
    errors = [item.reason for item in outcomes if isinstance(item, MarketError)]
    assert len(decisions) == 1
    assert errors == [MarketErrorReason.INVALID_STATUS]
    assert decisions[0].disputed_by_citizen_id == disputed.disputed_by_citizen_id
    expected = (800, 180, 8170, 0) if decisions[0].status is OrderStatus.COMPLETED else (1000, 0, 8150, 0)
    assert await _balances(unit_of_work, order) == expected
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind.in_([TransactionKind.ORDER_SETTLE, TransactionKind.ORDER_REFUND]))
        ) == 1


async def test_company_buyer_receives_refund_in_its_original_account(pg_engine: AsyncEngine) -> None:
    unit_of_work, _, service = await _setup(pg_engine)
    buyer_company = await _company(service, BUYER, "Buyer Studio")
    seller_company = await _company(service)
    income_offer = await _offer(service, buyer_company, price=300)
    income_order = await _order(service, income_offer, "buyer-company-income", buyer=ARBITER)
    await service.deliver_order(GUILD, BUYER, income_order.id)
    await service.accept_order(GUILD, ARBITER, income_order.id)
    assert await service.company_balance(GUILD, BUYER, buyer_company.id) == 270

    offer = await _offer(service, seller_company)
    order = await _order(service, offer, "corporate-order", company=buyer_company)
    assert await _balances(unit_of_work, order) == (70, 0, 8330, 200)
    await service.cancel_order(GUILD, BUYER, order.id)
    assert await _balances(unit_of_work, order) == (270, 0, 8330, 0)
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(
            order.guild_id, AccountKind.PERSONAL, buyer_company.owner_citizen_id
        )
        assert await repository.get_balance(personal.id) == 850


@pytest.mark.parametrize("tax_bp, expected_company, expected_treasury", [(0, 200, 8150), (10000, 0, 8350)])
async def test_tax_edges_do_not_create_zero_entries(
    pg_engine: AsyncEngine, tax_bp: int, expected_company: int, expected_treasury: int
) -> None:
    unit_of_work, citizens, _ = await _setup(pg_engine)
    service = _market(unit_of_work, citizens, tax_bp=tax_bp)
    offer = await _offer(service, await _company(service))
    order = await _order(service, offer, "tax-edge-order")
    await service.deliver_order(GUILD, SELLER, order.id)
    await service.accept_order(GUILD, BUYER, order.id)
    assert await _balances(unit_of_work, order) == (800, expected_company, expected_treasury, 0)
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(FinancialTransactionRow.entry_count)
            .where(FinancialTransactionRow.kind == TransactionKind.ORDER_SETTLE)
        ) == 2


async def test_funded_snapshot_cannot_be_rewritten(pg_engine: AsyncEngine) -> None:
    _, _, service = await _setup(pg_engine)
    offer = await _offer(service, await _company(service))
    order = await _order(service, offer, "snapshot-order")
    with pytest.raises(IntegrityError, match="snapshot is immutable"):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(OrderRow).where(OrderRow.id == order.id).values(tax_bp=9000, terms="Edited terms")
            )
    assert await service.get_order(GUILD, BUYER, order.id) == order


async def test_tax_rounds_down_exactly_at_bigint_boundary(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresMarketUnitOfWork(pg_engine)
    citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids={BUYER, SELLER})
    buyer = (await citizens.register(GUILD, BUYER)).citizen
    await citizens.register(GUILD, SELLER)
    async with unit_of_work() as repository:
        issuer = await repository.ensure_account(buyer.guild_id, AccountKind.ISSUER)
        personal = await repository.ensure_account(buyer.guild_id, AccountKind.PERSONAL, buyer.id)
        await repository.post(
            buyer.guild_id, TransactionKind.STARTER_GRANT, f"starter_grant:{buyer.id}",
            {issuer.id: -MAX_AMOUNT, personal.id: MAX_AMOUNT}, reason="boundary fixture",
        )
    service = MarketService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=MarketRules(registration_fee=0, sales_tax_bp=1000, company_limit=1),
        funding=WalletFunding(starter_grant=0, treasury_genesis=0),
        clock=lambda: NOW,
    )
    company = await service.register_company(
        GUILD, SELLER, name="Boundary Studio", activity="Design", idempotency_key="free-company"
    )
    offer = await _offer(service, company, price=MAX_AMOUNT)
    order = await _order(service, offer, "bigint-order")
    await service.deliver_order(GUILD, SELLER, order.id)
    completed = await service.accept_order(GUILD, BUYER, order.id)
    assert completed.tax == MAX_AMOUNT // 10
    assert await _balances(unit_of_work, order) == (0, MAX_AMOUNT - completed.tax, completed.tax, 0)
