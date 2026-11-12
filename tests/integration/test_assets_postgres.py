import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.assets import AssetError, AssetErrorReason, AssetService
from civitas.app.citizens import CitizensService
from civitas.app.market import MarketRules, MarketService
from civitas.app.wallet import WalletFunding
from civitas.domain.assets import Asset, AssetKind, AssetPurchase, AssetStatus
from civitas.domain.ledger import AccountKind, LedgerError, LedgerErrorReason, TransactionKind
from civitas.infrastructure.db.asset_models import AssetRow, AssetTransferRow
from civitas.infrastructure.db.assets import PostgresAssetUnitOfWork
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow

pytestmark = pytest.mark.asyncio

GUILD = 100
STAFF, BUYER, OTHER = 11, 21, 22
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


async def _setup(
    engine: AsyncEngine,
) -> tuple[PostgresAssetUnitOfWork, CitizensService, AssetService, WalletFunding]:
    unit_of_work = PostgresAssetUnitOfWork(engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD, allowed_user_ids={STAFF, BUYER, OTHER},
        fund_verified=funding.fund_registered_citizen,
    )
    for user_id in (STAFF, BUYER, OTHER):
        await citizens.register(GUILD, user_id)
    assets = AssetService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, staff_user_ids={STAFF},
        funding=funding, clock=lambda: NOW,
    )
    return unit_of_work, citizens, assets, funding


async def _issue(
    service: AssetService, *, user: int = STAFF, price: int = 100, key: str = "issue-1"
) -> Asset:
    return await service.issue_asset(
        GUILD, user, kind=AssetKind.OFFICE, name="Office 14", purpose="Meeting space",
        price=price, reason="Catalog addition", idempotency_key=key,
    )


async def test_concurrent_primary_purchase_and_retry_pay_once(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, assets, _ = await _setup(pg_engine)
    with pytest.raises(AssetError) as denied:
        await _issue(assets, user=BUYER)
    assert denied.value.reason is AssetErrorReason.STAFF_REQUIRED
    issued = await _issue(assets)
    assert await _issue(assets) == issued
    assert await assets.catalog(GUILD, BUYER) == (issued,)

    buyer_quote = await assets.quote_asset(GUILD, BUYER, issued.id)
    other_quote = await assets.quote_asset(GUILD, OTHER, issued.id)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            assets.buy_asset(GUILD, BUYER, buyer_quote, idempotency_key="buyer"),
            assets.buy_asset(GUILD, OTHER, other_quote, idempotency_key="other"),
            return_exceptions=True,
        ),
        timeout=10,
    )
    receipts = [item for item in outcomes if isinstance(item, AssetPurchase)]
    assert len(receipts) == 1
    assert [item.reason for item in outcomes if isinstance(item, AssetError)] == [
        AssetErrorReason.ASSET_SOLD
    ]
    receipt = receipts[0]
    winner, loser = (BUYER, OTHER) if isinstance(outcomes[0], AssetPurchase) else (OTHER, BUYER)
    quote, key = (buyer_quote, "buyer") if winner == BUYER else (other_quote, "other")
    assert await assets.buy_asset(GUILD, winner, quote, idempotency_key=key) == receipt
    assert await assets.catalog(GUILD, loser) == ()
    (sold,) = await assets.list_owned_assets(GUILD, winner)
    assert (sold.status, sold.owner_citizen_id, sold.owner_company_id) == (
        AssetStatus.SOLD, receipt.buyer_citizen_id, None,
    )
    assert await assets.list_owned_assets(GUILD, loser) == ()

    losing_citizen = await citizens.get_profile(GUILD, loser)
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(issued.guild_id, AccountKind.PERSONAL, losing_citizen.id)
        treasury = await repository.ensure_account(issued.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(receipt.buyer_account_id) == 150
        assert await repository.get_balance(personal.id) == 250
        assert await repository.get_balance(treasury.id) == 8100

    async with pg_engine.connect() as connection:
        history = (await connection.execute(
            select(AssetTransferRow.kind, AssetTransferRow.actor_citizen_id)
            .where(AssetTransferRow.asset_id == issued.id).order_by(AssetTransferRow.id)
        )).all()
        assert history == [("ISSUE", issued.issued_by_citizen_id), ("PURCHASE", receipt.buyer_citizen_id)]
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind == TransactionKind.ASSET_PURCHASE)
        ) == 1


async def test_insufficient_funds_preserve_state_title_and_account_balances(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, assets, _ = await _setup(pg_engine)
    issued = await _issue(assets, price=300)
    quote = await assets.quote_asset(GUILD, BUYER, issued.id)
    with pytest.raises(LedgerError) as denied:
        await assets.buy_asset(GUILD, BUYER, quote, idempotency_key="unfunded")
    assert denied.value.reason is LedgerErrorReason.INSUFFICIENT_FUNDS
    assert await assets.catalog(GUILD, BUYER) == (issued,)
    assert await assets.list_owned_assets(GUILD, BUYER) == ()

    buyer = await citizens.get_profile(GUILD, BUYER)
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(issued.guild_id, AccountKind.PERSONAL, buyer.id)
        treasury = await repository.ensure_account(issued.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(personal.id) == 250
        assert await repository.get_balance(treasury.id) == 8000
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(AssetTransferRow)) == 1


async def test_sold_asset_cannot_be_bought_again_or_reassigned(pg_engine: AsyncEngine) -> None:
    _, citizens, assets, _ = await _setup(pg_engine)
    issued = await _issue(assets)
    quote = await assets.quote_asset(GUILD, BUYER, issued.id)
    receipt = await assets.buy_asset(GUILD, BUYER, quote, idempotency_key="first-buy")
    with pytest.raises(AssetError) as denied:
        await assets.quote_asset(GUILD, OTHER, issued.id)
    assert denied.value.reason is AssetErrorReason.ASSET_SOLD

    other = await citizens.get_profile(GUILD, OTHER)
    with pytest.raises(IntegrityError, match="Only the primary sale"):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(AssetRow).where(AssetRow.id == issued.id).values(owner_citizen_id=other.id)
            )
    with pytest.raises(IntegrityError, match="append-only"):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(AssetTransferRow).where(AssetTransferRow.id == receipt.id).values(reason="Changed")
            )
    (still_owned,) = await assets.list_owned_assets(GUILD, BUYER)
    assert still_owned.owner_citizen_id == receipt.buyer_citizen_id
    assert await assets.list_owned_assets(GUILD, OTHER) == ()


async def test_purchase_requires_receipt_and_matching_title(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, assets, _ = await _setup(pg_engine)
    issued = await _issue(assets)
    buyer = await citizens.get_profile(GUILD, BUYER)
    with pytest.raises(IntegrityError, match="Asset posting requires a purchase receipt"):
        async with unit_of_work() as repository:
            personal = await repository.ensure_account(issued.guild_id, AccountKind.PERSONAL, buyer.id)
            treasury = await repository.ensure_account(issued.guild_id, AccountKind.TREASURY)
            await repository.post(
                issued.guild_id, TransactionKind.ASSET_PURCHASE, f"asset_purchase:{issued.id}",
                {personal.id: -issued.price, treasury.id: issued.price},
                actor_citizen_id=buyer.id, reason="primary_asset_purchase",
            )
    assert await assets.catalog(GUILD, BUYER) == (issued,)
    async with unit_of_work() as repository:
        assert await repository.get_balance(personal.id) == 250
        assert await repository.get_balance(treasury.id) == 8000


async def test_owned_company_can_buy_and_company_title_is_separate(pg_engine: AsyncEngine) -> None:
    unit_of_work, citizens, assets, funding = await _setup(pg_engine)
    market = MarketService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1),
        funding=funding, clock=lambda: NOW,
    )
    company = await market.register_company(
        GUILD, BUYER, name="Civitas Studio", activity="Design", idempotency_key="company",
    )
    offer = await market.publish_offer(
        GUILD, BUYER, company_id=company.id, title="Poster", terms="Two sketches",
        price=200, delivery_hours=24, idempotency_key="offer",
    )
    order_quote = await market.quote_order(GUILD, OTHER, offer.id)
    order = await market.fund_order(GUILD, OTHER, order_quote, idempotency_key="order")
    await market.deliver_order(GUILD, BUYER, order.id)
    await market.accept_order(GUILD, OTHER, order.id)
    assert await market.company_balance(GUILD, BUYER, company.id) == 180

    issued = await _issue(assets)
    with pytest.raises(AssetError) as denied:
        await assets.quote_asset(GUILD, OTHER, issued.id, buyer_company_id=company.id)
    assert denied.value.reason is AssetErrorReason.COMPANY_NOT_OWNED

    quote = await assets.quote_asset(GUILD, BUYER, issued.id, buyer_company_id=company.id)
    receipt = await assets.buy_asset(GUILD, BUYER, quote, idempotency_key="company-buy")
    (owned,) = await assets.list_owned_assets(GUILD, BUYER, company_id=company.id)
    assert (owned.owner_citizen_id, owned.owner_company_id) == (None, company.id)
    assert receipt.buyer_company_id == company.id
    assert await assets.list_owned_assets(GUILD, BUYER) == (owned,)
    assert await market.company_balance(GUILD, BUYER, company.id) == 80
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(issued.guild_id, AccountKind.PERSONAL, company.owner_citizen_id)
        treasury = await repository.ensure_account(issued.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(personal.id) == 100
        assert await repository.get_balance(treasury.id) == 8270
