from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Identity, Index, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class AssetRow(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("guild_id", "id", name="uq_assets_guild_id"),
        UniqueConstraint("guild_id", "issue_key", name="uq_assets_issue_request"),
        ForeignKeyConstraint(
            ["guild_id", "issued_by_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_assets_issuer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "owner_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_assets_citizen_owner",
        ),
        ForeignKeyConstraint(
            ["guild_id", "owner_company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_assets_company_owner",
        ),
        CheckConstraint(
            "kind IN ('housing', 'office', 'tools', 'transport', 'equipment')",
            name="ck_assets_kind",
        ),
        CheckConstraint("price > 0", name="ck_assets_price"),
        CheckConstraint(
            "(status = 'FOR_SALE' AND owner_citizen_id IS NULL AND owner_company_id IS NULL) OR "
            "(status = 'SOLD' AND (owner_citizen_id IS NULL) <> (owner_company_id IS NULL))",
            name="ck_assets_current_owner",
        ),
        Index("ix_assets_catalog", "guild_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    issued_by_citizen_id: Mapped[int] = mapped_column(BigInteger)
    issue_reason: Mapped[str] = mapped_column(Text)
    issue_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    owner_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_company_id: Mapped[int | None] = mapped_column(BigInteger)


class AssetTransferRow(Base):
    __tablename__ = "asset_transfers"
    __table_args__ = (
        UniqueConstraint("asset_id", "kind", name="uq_asset_transfers_asset_kind"),
        UniqueConstraint("guild_id", "kind", "request_key", name="uq_asset_transfers_request"),
        UniqueConstraint("transaction_id", name="uq_asset_transfers_posting"),
        ForeignKeyConstraint(
            ["guild_id", "asset_id"], ["assets.guild_id", "assets.id"], name="fk_asset_transfers_asset",
        ),
        ForeignKeyConstraint(
            ["guild_id", "actor_citizen_id"], ["citizens.guild_id", "citizens.id"],
            name="fk_asset_transfers_actor",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_company_id"], ["organizations.guild_id", "organizations.id"],
            name="fk_asset_transfers_buyer_company",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_account_id"], ["financial_accounts.guild_id", "financial_accounts.id"],
            name="fk_asset_transfers_buyer_account",
        ),
        ForeignKeyConstraint(
            ["guild_id", "transaction_id"], ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_asset_transfers_posting",
        ),
        CheckConstraint(
            "(kind = 'ISSUE' AND buyer_company_id IS NULL AND buyer_account_id IS NULL "
            "AND transaction_id IS NULL AND price IS NULL) OR "
            "(kind = 'PURCHASE' AND buyer_account_id IS NOT NULL AND transaction_id IS NOT NULL "
            "AND price IS NOT NULL AND price > 0)",
            name="ck_asset_transfers_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    asset_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    actor_citizen_id: Mapped[int] = mapped_column(BigInteger)
    buyer_company_id: Mapped[int | None] = mapped_column(BigInteger)
    buyer_account_id: Mapped[int | None] = mapped_column(BigInteger)
    price: Mapped[int | None] = mapped_column(BigInteger)
    request_key: Mapped[str] = mapped_column(String(128))
    transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AssetResaleRow(Base):
    __tablename__ = "asset_resales"
    __table_args__ = (
        UniqueConstraint("guild_id", "request_key", name="uq_asset_resales_request"),
        UniqueConstraint("transaction_id", name="uq_asset_resales_posting"),
        ForeignKeyConstraint(
            ["guild_id", "asset_id"], ["assets.guild_id", "assets.id"], name="fk_asset_resales_asset",
        ),
        ForeignKeyConstraint(
            ["guild_id", "seller_citizen_id"], ["citizens.guild_id", "citizens.id"], name="fk_asset_resales_seller",
        ),
        ForeignKeyConstraint(
            ["guild_id", "buyer_citizen_id"], ["citizens.guild_id", "citizens.id"], name="fk_asset_resales_buyer",
        ),
        ForeignKeyConstraint(
            ["guild_id", "transaction_id"], ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_asset_resales_posting",
        ),
        CheckConstraint(
            "price > 0 AND tax_bp BETWEEN 0 AND 9999 AND seller_citizen_id <> buyer_citizen_id "
            "AND length(btrim(request_key)) > 0",
            name="ck_asset_resales_terms",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL AND transaction_id IS NULL) OR "
            "(status = 'CANCELLED' AND closed_at IS NOT NULL AND transaction_id IS NULL) OR "
            "(status = 'COMPLETED' AND closed_at IS NOT NULL AND transaction_id IS NOT NULL)",
            name="ck_asset_resales_status",
        ),
        CheckConstraint("closed_at >= created_at", name="ck_asset_resales_closure_time"),
        Index(
            "uq_asset_resales_open_asset", "guild_id", "asset_id",
            unique=True, postgresql_where=text("status = 'OPEN'"),
        ),
        Index(
            "ix_asset_resales_completed_asset", "guild_id", "asset_id", "id",
            postgresql_where=text("status = 'COMPLETED'"),
        ),
        Index("ix_asset_resales_seller", "guild_id", "seller_citizen_id", "id"),
        Index("ix_asset_resales_buyer", "guild_id", "buyer_citizen_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    asset_id: Mapped[int] = mapped_column(BigInteger)
    seller_citizen_id: Mapped[int] = mapped_column(BigInteger)
    buyer_citizen_id: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[int] = mapped_column(BigInteger)
    tax_bp: Mapped[int]
    request_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
