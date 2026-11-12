from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.domain.assets import Asset, AssetKind, AssetPurchase, AssetStatus

from .asset_models import AssetRow, AssetTransferRow
from .market import PostgresMarketRepository
from .market_models import OrganizationRow


def _to_asset(row: AssetRow) -> Asset:
    return Asset(
        id=row.id,
        guild_id=row.guild_id,
        kind=AssetKind(row.kind),
        name=row.name,
        purpose=row.purpose,
        price=row.price,
        status=AssetStatus(row.status),
        issued_by_citizen_id=row.issued_by_citizen_id,
        issue_reason=row.issue_reason,
        issue_key=row.issue_key,
        created_at=row.created_at.astimezone(UTC),
        owner_citizen_id=row.owner_citizen_id,
        owner_company_id=row.owner_company_id,
    )


def _to_purchase(row: AssetTransferRow) -> AssetPurchase:
    assert row.buyer_account_id is not None and row.transaction_id is not None and row.price is not None
    return AssetPurchase(
        id=row.id,
        guild_id=row.guild_id,
        asset_id=row.asset_id,
        buyer_citizen_id=row.actor_citizen_id,
        buyer_company_id=row.buyer_company_id,
        buyer_account_id=row.buyer_account_id,
        price=row.price,
        request_key=row.request_key,
        transaction_id=row.transaction_id,
        purchased_at=row.created_at.astimezone(UTC),
    )


class PostgresAssetRepository(PostgresMarketRepository):
    async def get_asset(self, guild_id: int, asset_id: int, *, lock: bool = False) -> Asset | None:
        statement = select(AssetRow).where(AssetRow.guild_id == guild_id, AssetRow.id == asset_id)
        if lock:
            statement = statement.with_for_update(key_share=True)
        row = await self._session.scalar(statement)
        return _to_asset(row) if row is not None else None

    async def get_asset_by_issue_key(self, guild_id: int, key: str) -> Asset | None:
        row = await self._session.scalar(
            select(AssetRow).where(AssetRow.guild_id == guild_id, AssetRow.issue_key == key)
        )
        return _to_asset(row) if row is not None else None

    async def list_assets_for_sale(
        self, guild_id: int, limit: int, *, offset: int = 0
    ) -> tuple[Asset, ...]:
        rows = await self._session.scalars(
            select(AssetRow)
            .where(AssetRow.guild_id == guild_id, AssetRow.status == AssetStatus.FOR_SALE.value)
            .order_by(AssetRow.id)
            .limit(limit)
            .offset(offset)
        )
        return tuple(_to_asset(row) for row in rows)

    async def list_owned_assets(
        self, guild_id: int, citizen_id: int, company_id: int | None, limit: int, *, offset: int = 0
    ) -> tuple[Asset, ...]:
        owned_companies = select(OrganizationRow.id).where(
            OrganizationRow.guild_id == guild_id, OrganizationRow.owner_citizen_id == citizen_id
        )
        if company_id is None:
            owner = or_(
                AssetRow.owner_citizen_id == citizen_id,
                AssetRow.owner_company_id.in_(owned_companies),
            )
        else:
            owner = AssetRow.owner_company_id.in_(
                owned_companies.where(OrganizationRow.id == company_id)
            )
        rows = await self._session.scalars(
            select(AssetRow)
            .where(AssetRow.guild_id == guild_id, AssetRow.status == AssetStatus.SOLD.value, owner)
            .order_by(AssetRow.id)
            .limit(limit)
            .offset(offset)
        )
        return tuple(_to_asset(row) for row in rows)

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
    ) -> Asset | None:
        row = await self._session.scalar(
            insert(AssetRow)
            .values(
                guild_id=guild_id, kind=kind.value, name=name, purpose=purpose, price=price,
                status=AssetStatus.FOR_SALE.value, issued_by_citizen_id=issued_by_citizen_id,
                issue_reason=issue_reason, issue_key=issue_key, created_at=created_at,
            )
            .on_conflict_do_nothing(constraint="uq_assets_issue_request")
            .returning(AssetRow)
        )
        if row is None:
            return None
        await self._session.execute(
            insert(AssetTransferRow).values(
                guild_id=guild_id, asset_id=row.id, kind="ISSUE", actor_citizen_id=issued_by_citizen_id,
                request_key=issue_key, reason=issue_reason, created_at=created_at,
            )
        )
        return _to_asset(row)

    async def get_asset_purchase_by_key(self, guild_id: int, key: str) -> AssetPurchase | None:
        row = await self._session.scalar(
            select(AssetTransferRow).where(
                AssetTransferRow.guild_id == guild_id,
                AssetTransferRow.kind == "PURCHASE",
                AssetTransferRow.request_key == key,
            )
        )
        return _to_purchase(row) if row is not None else None

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
    ) -> AssetPurchase | None:
        row = await self._session.scalar(
            insert(AssetTransferRow)
            .values(
                guild_id=guild_id, asset_id=asset_id, kind="PURCHASE", actor_citizen_id=buyer_citizen_id,
                buyer_company_id=buyer_company_id, buyer_account_id=buyer_account_id,
                price=price, request_key=request_key, transaction_id=transaction_id,
                reason="primary_asset_purchase", created_at=purchased_at,
            )
            .on_conflict_do_nothing()
            .returning(AssetTransferRow)
        )
        return _to_purchase(row) if row is not None else None

    async def assign_asset(
        self,
        *,
        guild_id: int,
        asset_id: int,
        owner_citizen_id: int | None,
        owner_company_id: int | None,
    ) -> None:
        await self._session.execute(
            update(AssetRow)
            .where(AssetRow.guild_id == guild_id, AssetRow.id == asset_id)
            .values(
                status=AssetStatus.SOLD.value,
                owner_citizen_id=owner_citizen_id,
                owner_company_id=owner_company_id,
            )
        )


class PostgresAssetUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresAssetRepository]:
        async with self._sessions.begin() as session:
            yield PostgresAssetRepository(session)
