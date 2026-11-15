import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.assets import AssetError, AssetErrorReason, AssetService
from civitas.app.citizens import CitizensService
from civitas.app.market import MarketRules, MarketService
from civitas.app.receipts import ReceiptService
from civitas.app.reports import ReportService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.assets import Asset, AssetKind, AssetPurchase, AssetResaleStatus, AssetStatus
from civitas.domain.ledger import AccountKind, LedgerError, LedgerErrorReason, TransactionKind
from civitas.infrastructure.db.asset_models import AssetRow, AssetTransferRow
from civitas.infrastructure.db.assets import PostgresAssetUnitOfWork
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork
from civitas.infrastructure.db.reports import PostgresReportUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = 100
STAFF, BUYER, OTHER = 11, 21, 22
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


async def _setup(
    engine: AsyncEngine, *, resale_tax_bp: int | None = None,
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
        funding=funding, clock=lambda: NOW, resale_tax_bp=resale_tax_bp,
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
    unit_of_work, citizens, assets, funding = await _setup(pg_engine, resale_tax_bp=200)
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
    with pytest.raises(AssetError, match=AssetErrorReason.ASSET_NOT_OWNED):
        await assets.create_resale_offer(
            GUILD, BUYER, asset_id=issued.id, buyer_user_id=OTHER, price=200, idempotency_key="company-resale",
        )
    async with unit_of_work() as repository:
        personal = await repository.ensure_account(issued.guild_id, AccountKind.PERSONAL, company.owner_citizen_id)
        treasury = await repository.ensure_account(issued.guild_id, AccountKind.TREASURY)
        assert await repository.get_balance(personal.id) == 100
        assert await repository.get_balance(treasury.id) == 8270


async def test_directed_resale_is_atomic_private_and_replayable_across_rate_changes(pg_engine: AsyncEngine) -> None:
    def migrate(connection: Connection, *, upgrade: bool) -> None:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        if upgrade:
            command.upgrade(config, "head")
        else:
            command.downgrade(config, "0018_runoff_candidate_attrition")

    # Exercise resale of property already purchased before this feature shipped.
    async with pg_engine.begin() as connection:
        await connection.run_sync(migrate, upgrade=False)
    uow, citizens, assets, funding = await _setup(pg_engine, resale_tax_bp=200)
    issued = await _issue(assets)
    original_quote = await assets.quote_asset(GUILD, BUYER, issued.id)
    original_purchase = await assets.buy_asset(GUILD, BUYER, original_quote, idempotency_key="original")
    async with pg_engine.begin() as connection:
        await connection.run_sync(migrate, upgrade=True)

    listing = dict(asset_id=issued.id, buyer_user_id=OTHER, price=200, idempotency_key="resale-1")
    offer = await assets.create_resale_offer(GUILD, BUYER, **listing)
    assert await assets.create_resale_offer(GUILD, BUYER, **listing) == offer
    assert await assets.list_resale_offers(GUILD, BUYER) == (offer,)
    assert await assets.list_resale_offers(GUILD, OTHER) == (offer,)
    assert await assets.list_resale_offers(GUILD, STAFF) == ()
    preview = await assets.quote_resale(GUILD, OTHER, offer.id)
    assert preview.offer == offer and preview.asset.name == issued.name and preview.asset.purpose == issued.purpose
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_NOT_FOUND):
        await assets.get_resale_offer(GUILD, STAFF, offer.id)
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_NOT_FOUND):
        await assets.buy_resale(GUILD, BUYER, offer.id)
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_ALREADY_OPEN):
        await assets.create_resale_offer(GUILD, BUYER, **(listing | {"idempotency_key": "resale-duplicate"}))

    restarted = AssetService(
        uow, guild_id=GUILD, authorize=citizens.authorize, staff_user_ids={STAFF},
        funding=funding, clock=lambda: NOW, resale_tax_bp=500,
    )
    first, second = await asyncio.wait_for(asyncio.gather(
        restarted.buy_resale(GUILD, OTHER, offer.id),
        restarted.buy_resale(GUILD, OTHER, offer.id),
    ), timeout=10)
    assert first == second and first.status == AssetResaleStatus.COMPLETED and first.tax == 4
    assert first.transaction_id is not None
    assert await assets.list_owned_assets(GUILD, BUYER) == ()
    (owned,) = await assets.list_owned_assets(GUILD, OTHER)
    assert owned.id == issued.id and owned.owner_citizen_id == first.buyer_citizen_id

    receipts = ReceiptService(PostgresReceiptUnitOfWork(pg_engine), guild_id=GUILD, authorize=citizens.authorize)
    debit = await receipts.get_receipt(GUILD, OTHER, first.transaction_id)
    credit = await receipts.get_receipt(GUILD, BUYER, first.transaction_id)
    assert debit is not None and (debit.kind, debit.delta) == (TransactionKind.ASSET_RESALE, -200)
    assert credit is not None and (credit.kind, credit.delta) == (TransactionKind.ASSET_RESALE, 196)
    assert await receipts.get_receipt(GUILD, STAFF, first.transaction_id) is None

    next_offer = await assets.create_resale_offer(
        GUILD, OTHER, asset_id=issued.id, buyer_user_id=STAFF, price=49, idempotency_key="resale-2",
    )
    resale = await assets.buy_resale(GUILD, STAFF, next_offer.id)
    assert resale.tax == 0 and resale.transaction_id != first.transaction_id
    assert await assets.buy_resale(GUILD, OTHER, first.id) == first
    assert await assets.buy_asset(GUILD, BUYER, original_quote, idempotency_key="original") == original_purchase
    assert await assets.list_owned_assets(GUILD, OTHER) == ()
    (finally_owned,) = await assets.list_owned_assets(GUILD, STAFF)
    assert finally_owned.id == issued.id and finally_owned.owner_citizen_id == resale.buyer_citizen_id

    wallet = WalletService(uow, guild_id=GUILD, authorize=citizens.authorize, funding=funding)
    assert [await wallet.balance(GUILD, actor) for actor in (BUYER, OTHER, STAFF)] == [346, 99, 201]
    assert (await assets.list_resale_offers(GUILD, OTHER)) == (resale, first)
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow)
            .where(FinancialTransactionRow.kind == TransactionKind.ASSET_RESALE)
        ) == 2
    reports = ReportService(
        PostgresReportUnitOfWork(pg_engine), guild_id=GUILD, timezone=UTC,
        clock=lambda: debit.created_at + timedelta(microseconds=1),
    )
    report = await reports.get_report(GUILD)
    assert (report.asset_sales, report.asset_resale_tax, report.closing_balance) == (100, 4, 8104)


async def test_cancelled_unauthorized_or_unfunded_resale_preserves_property(pg_engine: AsyncEngine) -> None:
    uow, citizens, assets, funding = await _setup(pg_engine, resale_tax_bp=200)
    asset = await _issue(assets)
    quote = await assets.quote_asset(GUILD, BUYER, asset.id)
    await assets.buy_asset(GUILD, BUYER, quote, idempotency_key="primary")
    listing = dict(asset_id=asset.id, buyer_user_id=OTHER, price=300, idempotency_key="unfunded")
    with pytest.raises(AssetError, match=AssetErrorReason.ASSET_NOT_OWNED):
        await assets.create_resale_offer(GUILD, STAFF, **listing)
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_SELF_TRADE):
        await assets.create_resale_offer(GUILD, BUYER, **(listing | {"buyer_user_id": BUYER}))
    offer = await assets.create_resale_offer(GUILD, BUYER, **listing)
    with pytest.raises(LedgerError, match=LedgerErrorReason.INSUFFICIENT_FUNDS):
        await assets.buy_resale(GUILD, OTHER, offer.id)
    assert await assets.get_resale_offer(GUILD, BUYER, offer.id) == offer
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_NOT_FOUND):
        await assets.cancel_resale_offer(GUILD, OTHER, offer.id)
    cancelled = await assets.cancel_resale_offer(GUILD, BUYER, offer.id)
    assert cancelled.status == AssetResaleStatus.CANCELLED
    assert await assets.cancel_resale_offer(GUILD, BUYER, offer.id) == cancelled
    with pytest.raises(AssetError, match=AssetErrorReason.RESALE_NOT_OPEN):
        await assets.buy_resale(GUILD, OTHER, offer.id)

    (still_owned,) = await assets.list_owned_assets(GUILD, BUYER)
    assert still_owned.id == asset.id and still_owned.owner_citizen_id == offer.seller_citizen_id
    wallet = WalletService(uow, guild_id=GUILD, authorize=citizens.authorize, funding=funding)
    assert await wallet.balance(GUILD, BUYER) == 150
    assert await wallet.balance(GUILD, OTHER) == 250
