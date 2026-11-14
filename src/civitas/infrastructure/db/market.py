from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.domain.market import Company, MarketOrder, Offer, OfferWithdrawal, OrderStatus

from .jobs import PostgresJobsRepository
from .market_models import OfferRow, OfferWithdrawalRow, OrderRow, OrganizationRow
from .models import CitizenRow


def _to_company(row: OrganizationRow, owner_discord_user_id: int) -> Company:
    return Company(
        id=row.id,
        guild_id=row.guild_id,
        owner_citizen_id=row.owner_citizen_id,
        owner_discord_user_id=owner_discord_user_id,
        name=row.name,
        name_key=row.name_key,
        activity=row.activity,
        fee_paid=row.fee_paid,
        registration_key=row.registration_key,
        created_at=row.created_at.astimezone(UTC),
    )


def _to_offer(row: OfferRow) -> Offer:
    return Offer(
        id=row.id,
        guild_id=row.guild_id,
        company_id=row.company_id,
        title=row.title,
        terms=row.terms,
        price=row.price,
        delivery_hours=row.delivery_hours,
        request_key=row.request_key,
        created_at=row.created_at.astimezone(UTC),
    )


def _to_order(row: OrderRow) -> MarketOrder:
    return MarketOrder(
        id=row.id,
        guild_id=row.guild_id,
        offer_id=row.offer_id,
        seller_company_id=row.seller_company_id,
        seller_citizen_id=row.seller_citizen_id,
        buyer_citizen_id=row.buyer_citizen_id,
        buyer_company_id=row.buyer_company_id,
        buyer_account_id=row.buyer_account_id,
        title=row.title,
        terms=row.terms,
        price=row.price,
        tax_bp=row.tax_bp,
        deadline=row.deadline.astimezone(UTC),
        created_at=row.created_at.astimezone(UTC),
        status=OrderStatus(row.status),
        request_key=row.request_key,
        delivered_at=row.delivered_at.astimezone(UTC) if row.delivered_at else None,
        disputed_at=row.disputed_at.astimezone(UTC) if row.disputed_at else None,
        disputed_by_citizen_id=row.disputed_by_citizen_id,
        dispute_reason=row.dispute_reason,
        closed_at=row.closed_at.astimezone(UTC) if row.closed_at else None,
        closed_by_citizen_id=row.closed_by_citizen_id,
        close_reason=row.close_reason,
    )


class PostgresMarketRepository(PostgresJobsRepository):
    async def _get_company(
        self, guild_id: int, criterion: ColumnElement[bool]
    ) -> Company | None:
        result = (
            await self._session.execute(
                select(OrganizationRow, CitizenRow.discord_user_id)
                .join(CitizenRow, CitizenRow.id == OrganizationRow.owner_citizen_id)
                .where(OrganizationRow.guild_id == guild_id, criterion)
            )
        ).one_or_none()
        return _to_company(result[0], int(result[1])) if result is not None else None

    async def get_company(self, guild_id: int, company_id: int) -> Company | None:
        return await self._get_company(guild_id, OrganizationRow.id == company_id)

    async def get_company_by_key(self, guild_id: int, key: str) -> Company | None:
        return await self._get_company(guild_id, OrganizationRow.registration_key == key)

    async def list_owned_companies(self, guild_id: int, citizen_id: int) -> tuple[Company, ...]:
        rows = await self._session.execute(
            select(OrganizationRow, CitizenRow.discord_user_id)
            .join(CitizenRow, CitizenRow.id == OrganizationRow.owner_citizen_id)
            .where(OrganizationRow.guild_id == guild_id, OrganizationRow.owner_citizen_id == citizen_id)
            .order_by(OrganizationRow.id)
        )
        return tuple(_to_company(row, int(owner_discord_user_id)) for row, owner_discord_user_id in rows)

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
    ) -> Company | None:
        row = await self._session.scalar(
            insert(OrganizationRow)
            .values(
                guild_id=guild_id, owner_citizen_id=owner_citizen_id,
                name=name, name_key=name_key, activity=activity, fee_paid=fee_paid,
                registration_key=registration_key, created_at=created_at,
            )
            .on_conflict_do_nothing()
            .returning(OrganizationRow)
        )
        return _to_company(row, owner_discord_user_id) if row is not None else None

    async def get_offer(self, guild_id: int, offer_id: int, *, lock: bool = False) -> Offer | None:
        statement = select(OfferRow).where(OfferRow.guild_id == guild_id, OfferRow.id == offer_id)
        if lock:
            statement = statement.with_for_update(key_share=True)
        row = await self._session.scalar(statement)
        return _to_offer(row) if row is not None else None

    async def get_offer_withdrawal(self, guild_id: int, offer_id: int) -> OfferWithdrawal | None:
        row = await self._session.scalar(select(OfferWithdrawalRow).where(
            OfferWithdrawalRow.guild_id == guild_id, OfferWithdrawalRow.offer_id == offer_id,
        ))
        return OfferWithdrawal(
            row.offer_id, row.withdrawn_at.astimezone(UTC), row.withdrawn_by_citizen_id, row.reason,
        ) if row is not None else None

    async def create_offer_withdrawal(self, guild_id: int, withdrawal: OfferWithdrawal) -> None:
        await self._session.execute(insert(OfferWithdrawalRow).values(
            guild_id=guild_id, offer_id=withdrawal.offer_id,
            withdrawn_by_citizen_id=withdrawal.withdrawn_by_citizen_id,
            withdrawn_at=withdrawal.withdrawn_at, reason=withdrawal.reason,
        ))

    async def get_offer_by_key(self, guild_id: int, key: str) -> Offer | None:
        row = await self._session.scalar(
            select(OfferRow).where(OfferRow.guild_id == guild_id, OfferRow.request_key == key)
        )
        return _to_offer(row) if row is not None else None

    async def list_offers(self, guild_id: int, limit: int) -> tuple[Offer, ...]:
        rows = await self._session.scalars(
            select(OfferRow).where(
                OfferRow.guild_id == guild_id,
                ~select(OfferWithdrawalRow.offer_id).where(OfferWithdrawalRow.offer_id == OfferRow.id).exists(),
            ).order_by(OfferRow.id.desc()).limit(limit)
        )
        return tuple(_to_offer(row) for row in rows)

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
    ) -> Offer | None:
        row = await self._session.scalar(
            insert(OfferRow)
            .values(
                guild_id=guild_id, company_id=company_id, title=title, terms=terms, price=price,
                delivery_hours=delivery_hours, request_key=request_key, created_at=created_at,
            )
            .on_conflict_do_nothing(constraint="uq_offers_request")
            .returning(OfferRow)
        )
        return _to_offer(row) if row is not None else None

    async def get_order(self, guild_id: int, order_id: int, *, lock: bool = False) -> MarketOrder | None:
        statement = select(OrderRow).where(OrderRow.guild_id == guild_id, OrderRow.id == order_id)
        if lock:
            statement = statement.with_for_update(key_share=True)
        row = await self._session.scalar(statement)
        return _to_order(row) if row is not None else None

    async def get_order_by_key(self, guild_id: int, key: str) -> MarketOrder | None:
        row = await self._session.scalar(
            select(OrderRow).where(OrderRow.guild_id == guild_id, OrderRow.request_key == key)
        )
        return _to_order(row) if row is not None else None

    async def list_orders(
        self, guild_id: int, citizen_id: int, *, include_disputes: bool, limit: int
    ) -> tuple[MarketOrder, ...]:
        criteria = [
            OrderRow.buyer_citizen_id == citizen_id,
            OrderRow.seller_citizen_id == citizen_id,
        ]
        if include_disputes:
            criteria.append(OrderRow.status == OrderStatus.DISPUTED.value)
        rows = await self._session.scalars(
            select(OrderRow)
            .where(OrderRow.guild_id == guild_id, or_(*criteria))
            .order_by(OrderRow.id.desc())
            .limit(limit)
        )
        return tuple(_to_order(row) for row in rows)

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
    ) -> MarketOrder | None:
        row = await self._session.scalar(
            insert(OrderRow)
            .values(
                guild_id=guild_id, offer_id=offer_id, seller_company_id=seller_company_id,
                seller_citizen_id=seller_citizen_id, buyer_citizen_id=buyer_citizen_id,
                buyer_company_id=buyer_company_id, buyer_account_id=buyer_account_id,
                title=title, terms=terms, price=price, tax_bp=tax_bp, deadline=deadline,
                created_at=created_at, request_key=request_key, status=OrderStatus.FUNDED.value,
            )
            .on_conflict_do_nothing(constraint="uq_orders_request")
            .returning(OrderRow)
        )
        return _to_order(row) if row is not None else None

    async def update_order(self, order: MarketOrder) -> None:
        await self._session.execute(
            update(OrderRow)
            .where(OrderRow.guild_id == order.guild_id, OrderRow.id == order.id)
            .values(
                status=order.status.value,
                delivered_at=order.delivered_at,
                disputed_at=order.disputed_at,
                disputed_by_citizen_id=order.disputed_by_citizen_id,
                dispute_reason=order.dispute_reason,
                closed_at=order.closed_at,
                closed_by_citizen_id=order.closed_by_citizen_id,
                close_reason=order.close_reason,
            )
        )


class PostgresMarketUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresMarketRepository]:
        async with self._sessions.begin() as session:
            yield PostgresMarketRepository(session)
