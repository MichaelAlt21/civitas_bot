from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.assets import AssetService
from civitas.app.citizens import CitizensService
from civitas.app.wallet import WalletFunding
from civitas.domain.assets import AssetKind, AssetResaleStatus
from civitas.domain.ledger import AccountKind, TransactionKind
from civitas.infrastructure.db.assets import PostgresAssetUnitOfWork


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defect", "error"),
    [
        ("no_completed_offer", "requires a completed offer"),
        ("wrong_tax", "must match the offer and its tax"),
        ("no_new_title", "requires a matching new title"),
    ],
)
async def test_database_rejects_incomplete_or_wrong_resale(
    pg_engine: AsyncEngine, defect: str, error: str,
) -> None:
    guild, seller, buyer = 100, 21, 22
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    unit_of_work = PostgresAssetUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=guild, allowed_user_ids={seller, buyer},
        fund_verified=funding.fund_registered_citizen,
    )
    await citizens.register(guild, seller)
    await citizens.register(guild, buyer)
    assets = AssetService(
        unit_of_work, guild_id=guild, authorize=citizens.authorize, staff_user_ids={seller},
        funding=funding, clock=lambda: now,
    )
    asset = await assets.issue_asset(
        guild, seller, kind=AssetKind.OFFICE, name="Office", purpose="Meeting space",
        price=100, reason="Catalog addition", idempotency_key="issue",
    )
    quote = await assets.quote_asset(guild, seller, asset.id)
    await assets.buy_asset(guild, seller, quote, idempotency_key="primary")
    seller_citizen = await citizens.get_profile(guild, seller)
    buyer_citizen = await citizens.get_profile(guild, buyer)
    assert seller_citizen is not None and buyer_citizen is not None
    async with unit_of_work() as repository:
        offer = await repository.create_resale_offer(
            guild_id=asset.guild_id, asset_id=asset.id, seller_citizen_id=seller_citizen.id,
            buyer_citizen_id=buyer_citizen.id, price=200, tax_bp=200,
            request_key="offer", created_at=now,
        )
        source = await repository.ensure_account(asset.guild_id, AccountKind.PERSONAL, buyer_citizen.id)
        destination = await repository.ensure_account(asset.guild_id, AccountKind.PERSONAL, seller_citizen.id)
        treasury = await repository.ensure_account(asset.guild_id, AccountKind.TREASURY)
    assert offer is not None

    tax = 3 if defect == "wrong_tax" else 4
    with pytest.raises(IntegrityError, match=error):
        async with unit_of_work() as repository:
            posting = await repository.post(
                asset.guild_id, TransactionKind.ASSET_RESALE, f"asset_resale:{offer.id}",
                {source.id: -200, destination.id: 200 - tax, treasury.id: tax},
                actor_citizen_id=buyer_citizen.id, reason="asset_resale",
            )
            if defect != "no_completed_offer":
                await repository.update_resale_offer(replace(
                    offer, status=AssetResaleStatus.COMPLETED,
                    transaction_id=posting.transaction_id, closed_at=now,
                ))
            if defect == "wrong_tax":
                await repository.assign_asset(
                    guild_id=asset.guild_id, asset_id=asset.id,
                    owner_citizen_id=buyer_citizen.id, owner_company_id=None,
                )
    async with unit_of_work() as repository:
        assert await repository.get_resale_offer(asset.guild_id, offer.id) == offer
        assert [await repository.get_balance(account.id) for account in (source, destination, treasury)] == [
            250, 150, 8100,
        ]
    (owned,) = await assets.list_owned_assets(guild, seller)
    assert owned.id == asset.id
    assert await assets.list_owned_assets(guild, buyer) == ()
