from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class OrganizationRow(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_organizations_guild_id"),
        UniqueConstraint("guild_id", "name_key", name="uq_organizations_guild_name"),
        UniqueConstraint("guild_id", "registration_key", name="uq_organizations_request"),
        ForeignKeyConstraint(
            ["guild_id", "owner_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_organizations_owner",
        ),
        CheckConstraint("fee_paid >= 0", name="ck_organizations_fee"),
        Index("ix_organizations_owner", "guild_id", "owner_citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    owner_citizen_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    activity: Mapped[str] = mapped_column(Text)
    fee_paid: Mapped[int] = mapped_column(BigInteger)
    registration_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OfferRow(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_offers_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_offers_request"),
        ForeignKeyConstraint(
            ["guild_id", "company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_offers_company",
        ),
        CheckConstraint("price > 0", name="ck_offers_price"),
        CheckConstraint("delivery_hours > 0", name="ck_offers_delivery_hours"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    company_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger)
    delivery_hours: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_orders_guild_id"),
        UniqueConstraint("guild_id", "request_key", name="uq_orders_request"),
        ForeignKeyConstraint(
            ["guild_id", "offer_id"], ["offers.guild_id", "offers.id"], name="fk_orders_offer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "seller_company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_orders_seller_company",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_orders_buyer_company",
        ),
        ForeignKeyConstraint(
            ["guild_id", "seller_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_orders_seller_citizen",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_orders_buyer_citizen",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_account_id"],
            ["financial_accounts.guild_id", "financial_accounts.id"],
            name="fk_orders_buyer_account",
        ),
        ForeignKeyConstraint(
            ["guild_id", "disputed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_orders_dispute_actor",
        ),
        ForeignKeyConstraint(
            ["guild_id", "closed_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_orders_close_actor",
        ),
        CheckConstraint("price > 0 AND tax_bp BETWEEN 0 AND 10000", name="ck_orders_price_tax"),
        CheckConstraint("deadline > created_at", name="ck_orders_deadline"),
        CheckConstraint("seller_citizen_id <> buyer_citizen_id", name="ck_orders_distinct_parties"),
        CheckConstraint(
            "status IN ('FUNDED', 'DELIVERED', 'DISPUTED', 'COMPLETED', 'REFUNDED')",
            name="ck_orders_status",
        ),
        CheckConstraint(
            "(status <> 'DELIVERED' OR delivered_at IS NOT NULL) AND "
            "(status <> 'DISPUTED' OR disputed_at IS NOT NULL)",
            name="ck_orders_progress",
        ),
        CheckConstraint(
            "(disputed_at IS NULL AND disputed_by_citizen_id IS NULL AND dispute_reason IS NULL) OR "
            "(disputed_at IS NOT NULL AND disputed_by_citizen_id IS NOT NULL AND dispute_reason IS NOT NULL)",
            name="ck_orders_dispute",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'REFUNDED') AND closed_at IS NOT NULL "
            "AND closed_by_citizen_id IS NOT NULL AND close_reason IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED', 'REFUNDED') AND closed_at IS NULL "
            "AND closed_by_citizen_id IS NULL AND close_reason IS NULL)",
            name="ck_orders_closed",
        ),
        Index("ix_orders_buyer", "guild_id", "buyer_citizen_id"),
        Index("ix_orders_seller", "guild_id", "seller_citizen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    offer_id: Mapped[int] = mapped_column(BigInteger)
    seller_company_id: Mapped[int] = mapped_column(BigInteger)
    seller_citizen_id: Mapped[int] = mapped_column(BigInteger)
    buyer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    buyer_company_id: Mapped[int | None] = mapped_column(BigInteger)
    buyer_account_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger)
    tax_bp: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    request_key: Mapped[str] = mapped_column(String(128))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disputed_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    dispute_reason: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    close_reason: Mapped[str | None] = mapped_column(Text)
