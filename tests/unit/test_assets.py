from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from civitas.app.assets import AssetError, AssetErrorReason, AssetRepository, AssetService
from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.wallet import WalletFunding
from civitas.domain.assets import Asset, AssetKind, AssetPurchase, AssetStatus
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT, Account, AccountKind, LedgerError, LedgerErrorReason, PostResult, TransactionKind
from civitas.domain.market import Company

DISCORD_GUILD = 100
GUILD = 1
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
STAFF = Citizen(1, GUILD, 200, CitizenStatus.VERIFIED, NOW, NOW)
BUYER = Citizen(2, GUILD, 201, CitizenStatus.VERIFIED, NOW, NOW)
OTHER = Citizen(3, GUILD, 202, CitizenStatus.VERIFIED, NOW, NOW)
COMPANY = Company(10, GUILD, BUYER.id, BUYER.discord_user_id, "Артель", "артель", "Услуги", 150, "company", NOW)
AVAILABLE = Asset(4, GUILD, AssetKind.OFFICE, "Офис", "Рабочее место", 200, AssetStatus.FOR_SALE, STAFF.id, "Первая продажа", "issue", NOW)
PERSONAL = Account(20, GUILD, AccountKind.PERSONAL, BUYER.id)
TREASURY = Account(21, GUILD, AccountKind.TREASURY, None)
CORPORATE = Account(22, GUILD, AccountKind.COMPANY, None, company_id=COMPANY.id)


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=AssetRepository)
    assets_by_key: dict[str, Asset] = {}
    receipts_by_key: dict[str, AssetPurchase] = {}

    def get_citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        return tuple(citizen for citizen in (STAFF, BUYER, OTHER) if citizen.discord_user_id in user_ids)

    def create_asset(**values: object) -> Asset:
        asset = Asset(id=AVAILABLE.id, status=AssetStatus.FOR_SALE, **values)
        assets_by_key[asset.issue_key] = asset
        return asset

    def create_receipt(**values: object) -> AssetPurchase:
        receipt = AssetPurchase(id=30, **values)
        receipts_by_key[receipt.request_key] = receipt
        return receipt

    def assign_asset(guild_id: int, asset_id: int, owner_citizen_id: int | None, owner_company_id: int | None) -> None:
        assert (guild_id, asset_id) == (GUILD, AVAILABLE.id)
        repo.get_asset.return_value = replace(
            AVAILABLE,
            status=AssetStatus.SOLD,
            owner_citizen_id=owner_citizen_id,
            owner_company_id=owner_company_id,
        )

    repo.get_citizens.side_effect = get_citizens
    repo.get_asset.return_value = AVAILABLE
    repo.get_company.return_value = COMPANY
    repo.get_asset_by_issue_key.side_effect = lambda guild_id, key: assets_by_key.get(key)
    repo.get_asset_purchase_by_key.side_effect = lambda guild_id, key: receipts_by_key.get(key)
    repo.create_asset.side_effect = create_asset
    repo.create_asset_purchase.side_effect = create_receipt
    repo.assign_asset.side_effect = assign_asset
    repo.get_post.return_value = PostResult(2, False)
    repo.post.return_value = PostResult(23, True)
    repo.ensure_company_account.return_value = CORPORATE
    repo.ensure_account.side_effect = lambda guild_id, kind, citizen_id=None: {
        AccountKind.PERSONAL: PERSONAL,
        AccountKind.TREASURY: TREASURY,
    }[kind]
    return repo


@pytest.fixture
def assets(repository: AsyncMock) -> AssetService:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[AssetRepository]:
        yield repository

    citizens = CitizensService(
        lambda: pytest.fail("Authorization should not use a separate transaction"),
        guild_id=DISCORD_GUILD,
        allowed_user_ids=[STAFF.discord_user_id, BUYER.discord_user_id, OTHER.discord_user_id],
    )
    return AssetService(
        unit_of_work,
        guild_id=DISCORD_GUILD,
        authorize=citizens.authorize,
        staff_user_ids=[STAFF.discord_user_id],
        funding=WalletFunding(starter_grant=250, treasury_genesis=8000),
        clock=lambda: NOW,
    )


def issue_values() -> dict:
    return {
        "kind": AssetKind.OFFICE,
        "name": " Офис ",
        "purpose": "Рабочее место",
        "price": 200,
        "reason": "Первая продажа",
        "idempotency_key": "issue",
    }


@pytest.mark.asyncio
async def test_staff_issuance_records_actor_and_reason_once(assets: AssetService, repository: AsyncMock) -> None:
    with pytest.raises(CitizenshipAccessError):
        await assets.issue_asset(None, STAFF.discord_user_id, **issue_values())
    with pytest.raises(AssetError, match=AssetErrorReason.STAFF_REQUIRED):
        await assets.issue_asset(DISCORD_GUILD, BUYER.discord_user_id, **issue_values())

    issued = await assets.issue_asset(DISCORD_GUILD, STAFF.discord_user_id, **issue_values())
    assert issued == AVAILABLE
    assert await assets.issue_asset(DISCORD_GUILD, STAFF.discord_user_id, **issue_values()) == issued
    with pytest.raises(AssetError, match=AssetErrorReason.INVALID_REQUEST_KEY):
        await assets.issue_asset(DISCORD_GUILD, STAFF.discord_user_id, **(issue_values() | {"price": 201}))
    repository.create_asset.assert_awaited_once()
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_staff_also_needs_verified_status(assets: AssetService, repository: AsyncMock) -> None:
    repository.get_citizens.side_effect = None
    repository.get_citizens.return_value = (replace(STAFF, status=CitizenStatus.SUSPENDED),)

    with pytest.raises(AssetError, match=AssetErrorReason.CITIZEN_NOT_VERIFIED):
        await assets.issue_asset(DISCORD_GUILD, STAFF.discord_user_id, **issue_values())

    repository.create_asset.assert_not_awaited()


@pytest.mark.parametrize("price", [0, -1, True, MAX_AMOUNT + 1])
@pytest.mark.asyncio
async def test_issue_requires_positive_integer_price(assets: AssetService, repository: AsyncMock, price: int) -> None:
    with pytest.raises(AssetError, match=AssetErrorReason.INVALID_PRICE):
        await assets.issue_asset(DISCORD_GUILD, STAFF.discord_user_id, **(issue_values() | {"price": price}))

    repository.create_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_purchase_moves_price_and_title_once_and_refuses_resale(assets: AssetService, repository: AsyncMock) -> None:
    quote = await assets.quote_asset(DISCORD_GUILD, BUYER.discord_user_id, AVAILABLE.id)
    receipt = await assets.buy_asset(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="buy")
    assert await assets.buy_asset(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="buy") == receipt
    assert (receipt.asset_id, receipt.buyer_citizen_id, receipt.transaction_id) == (AVAILABLE.id, BUYER.id, 23)
    assert receipt.buyer_company_id is None
    assert receipt.price == AVAILABLE.price
    repository.get_asset.assert_awaited_with(GUILD, AVAILABLE.id, lock=True)
    repository.post.assert_awaited_once_with(
        GUILD,
        TransactionKind.ASSET_PURCHASE,
        f"asset_purchase:{AVAILABLE.id}",
        {PERSONAL.id: -200, TREASURY.id: 200},
        actor_citizen_id=BUYER.id,
        reason="primary_asset_purchase",
    )
    repository.assign_asset.assert_awaited_once_with(
        guild_id=GUILD, asset_id=AVAILABLE.id, owner_citizen_id=BUYER.id, owner_company_id=None
    )

    with pytest.raises(AssetError, match=AssetErrorReason.ASSET_SOLD):
        await assets.buy_asset(DISCORD_GUILD, OTHER.discord_user_id, quote, idempotency_key="other")
    with pytest.raises(AssetError, match=AssetErrorReason.ASSET_SOLD):
        await assets.asset_info(DISCORD_GUILD, BUYER.discord_user_id, AVAILABLE.id)
    with pytest.raises(AssetError, match=AssetErrorReason.INVALID_REQUEST_KEY):
        await assets.buy_asset(DISCORD_GUILD, OTHER.discord_user_id, quote, idempotency_key="buy")
    repository.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_corporate_purchase_checks_company_owner_and_uses_its_account(assets: AssetService, repository: AsyncMock) -> None:
    with pytest.raises(AssetError, match=AssetErrorReason.COMPANY_NOT_OWNED):
        await assets.quote_asset(DISCORD_GUILD, OTHER.discord_user_id, AVAILABLE.id, buyer_company_id=COMPANY.id)

    quote = await assets.quote_asset(DISCORD_GUILD, BUYER.discord_user_id, AVAILABLE.id, buyer_company_id=COMPANY.id)
    receipt = await assets.buy_asset(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="buy")
    assert (receipt.buyer_citizen_id, receipt.buyer_company_id, receipt.buyer_account_id) == (BUYER.id, COMPANY.id, CORPORATE.id)
    assert repository.post.await_args.args[3] == {CORPORATE.id: -200, TREASURY.id: 200}
    repository.ensure_account.assert_awaited_once_with(GUILD, AccountKind.TREASURY)
    repository.assign_asset.assert_awaited_once_with(
        guild_id=GUILD, asset_id=AVAILABLE.id, owner_citizen_id=None, owner_company_id=COMPANY.id
    )


@pytest.mark.asyncio
async def test_failed_or_stale_purchase_does_not_assign_title(assets: AssetService, repository: AsyncMock) -> None:
    quote = await assets.quote_asset(DISCORD_GUILD, BUYER.discord_user_id, AVAILABLE.id)
    repository.get_asset.return_value = replace(AVAILABLE, price=201)
    with pytest.raises(AssetError, match=AssetErrorReason.QUOTE_CHANGED):
        await assets.buy_asset(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="buy")
    repository.post.assert_not_awaited()

    repository.get_asset.return_value = AVAILABLE
    repository.post.side_effect = LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
    with pytest.raises(LedgerError, match=LedgerErrorReason.INSUFFICIENT_FUNDS):
        await assets.buy_asset(DISCORD_GUILD, BUYER.discord_user_id, quote, idempotency_key="buy")
    repository.create_asset_purchase.assert_not_awaited()
    repository.assign_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_and_owned_listing_use_guild_and_owner_scope(assets: AssetService, repository: AsyncMock) -> None:
    sold = replace(AVAILABLE, status=AssetStatus.SOLD, owner_company_id=COMPANY.id)
    repository.list_assets_for_sale.return_value = (AVAILABLE,)
    repository.list_owned_assets.return_value = (sold,)

    assert await assets.catalog(DISCORD_GUILD, BUYER.discord_user_id) == (AVAILABLE,)
    assert await assets.catalog(DISCORD_GUILD, BUYER.discord_user_id, page=2) == (AVAILABLE,)
    assert await assets.list_owned_assets(DISCORD_GUILD, BUYER.discord_user_id) == (sold,)
    with pytest.raises(AssetError, match=AssetErrorReason.COMPANY_NOT_OWNED):
        await assets.list_owned_assets(DISCORD_GUILD, OTHER.discord_user_id, company_id=COMPANY.id)
    assert await assets.list_owned_assets(DISCORD_GUILD, BUYER.discord_user_id, company_id=COMPANY.id, page=3) == (sold,)

    assert [call.kwargs for call in repository.list_assets_for_sale.await_args_list] == [{"offset": 0}, {"offset": 10}]
    assert [call.args for call in repository.list_owned_assets.await_args_list] == [
        (GUILD, BUYER.id, None, 10), (GUILD, BUYER.id, COMPANY.id, 10)
    ]
    assert [call.kwargs for call in repository.list_owned_assets.await_args_list] == [{"offset": 0}, {"offset": 20}]


@pytest.mark.parametrize("page", [0, -1, True, MAX_AMOUNT])
@pytest.mark.asyncio
async def test_invalid_page_is_rejected_before_database_reads(
    assets: AssetService, repository: AsyncMock, page: int
) -> None:
    with pytest.raises(AssetError, match=AssetErrorReason.INVALID_PAGE):
        await assets.catalog(DISCORD_GUILD, BUYER.discord_user_id, page=page)
    with pytest.raises(AssetError, match=AssetErrorReason.INVALID_PAGE):
        await assets.list_owned_assets(DISCORD_GUILD, BUYER.discord_user_id, page=page)

    repository.get_citizens.assert_not_awaited()
