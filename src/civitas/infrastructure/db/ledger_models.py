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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class FinancialAccountRow(Base):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "kind", "citizen_id", "organization_id", "order_id",
            name="uq_financial_accounts_owner",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("guild_id", "id", name="uq_financial_accounts_guild_id"),
        ForeignKeyConstraint(
            ["guild_id", "citizen_id"],
            ["citizens.guild_id", "citizens.id"],
            name="fk_financial_accounts_citizen",
        ),
        ForeignKeyConstraint(
            ["guild_id", "organization_id"],
            ["organizations.guild_id", "organizations.id"],
            name="fk_financial_accounts_organization",
        ),
        ForeignKeyConstraint(
            ["guild_id", "order_id"],
            ["orders.guild_id", "orders.id"],
            name="fk_financial_accounts_order",
            use_alter=True,
        ),
        CheckConstraint(
            "(kind = 'PERSONAL' AND citizen_id IS NOT NULL AND organization_id IS NULL AND order_id IS NULL) OR "
            "(kind IN ('ISSUER', 'TREASURY') AND citizen_id IS NULL AND organization_id IS NULL AND order_id IS NULL) OR "
            "(kind = 'COMPANY' AND citizen_id IS NULL AND organization_id IS NOT NULL AND order_id IS NULL) OR "
            "(kind = 'ESCROW' AND citizen_id IS NULL AND organization_id IS NULL AND order_id IS NOT NULL)",
            name="ck_financial_accounts_kind_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    kind: Mapped[str] = mapped_column(String(16))
    citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    organization_id: Mapped[int | None] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FinancialTransactionRow(Base):
    __tablename__ = "financial_transactions"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "kind", "idempotency_key", name="uq_financial_transactions_operation"
        ),
        UniqueConstraint("guild_id", "id", name="uq_financial_transactions_guild_id"),
        ForeignKeyConstraint(
            ["guild_id", "actor_citizen_id"],
            ["citizens.guild_id", "citizens.id"],
            name="fk_financial_transactions_actor",
        ),
        CheckConstraint(
            "kind IN ('GENESIS', 'STARTER_GRANT', 'TRANSFER', 'NPC_PAYOUT', "
            "'COMPANY_FEE', 'ORDER_FUND', 'ORDER_SETTLE', 'ORDER_REFUND', 'ASSET_PURCHASE', "
            "'STATE_CONTRACT_PAYOUT')",
            name="ck_financial_transactions_kind",
        ),
        CheckConstraint("entry_count >= 2", name="ck_financial_transactions_entry_count"),
        CheckConstraint(
            "length(idempotency_key) > 0 AND length(reason) > 0",
            name="ck_financial_transactions_audit_fields",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id"))
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    entry_count: Mapped[int] = mapped_column(Integer)
    actor_citizen_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerEntryRow(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "transaction_id"],
            ["financial_transactions.guild_id", "financial_transactions.id"],
            name="fk_ledger_entries_transaction",
        ),
        ForeignKeyConstraint(
            ["guild_id", "account_id"],
            ["financial_accounts.guild_id", "financial_accounts.id"],
            name="fk_ledger_entries_account",
        ),
        CheckConstraint("delta <> 0", name="ck_ledger_entries_nonzero"),
        Index("ix_ledger_entries_account_transaction", "account_id", "transaction_id"),
    )

    transaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    delta: Mapped[int] = mapped_column(BigInteger)
