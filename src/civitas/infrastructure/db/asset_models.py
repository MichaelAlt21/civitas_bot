from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Identity, Index, String, Text, UniqueConstraint,
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
