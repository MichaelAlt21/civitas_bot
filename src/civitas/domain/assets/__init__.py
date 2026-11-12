"""Unique state assets and their primary-sale receipts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from civitas.domain.market import Company


class AssetKind(StrEnum):
    HOUSING = "housing"
    OFFICE = "office"
    TOOLS = "tools"
    TRANSPORT = "transport"
    EQUIPMENT = "equipment"


class AssetStatus(StrEnum):
    FOR_SALE = "FOR_SALE"
    SOLD = "SOLD"


@dataclass(frozen=True, slots=True)
class Asset:
    id: int
    guild_id: int
    kind: AssetKind
    name: str
    purpose: str
    price: int
    status: AssetStatus
    issued_by_citizen_id: int
    issue_reason: str
    issue_key: str
    created_at: datetime
    owner_citizen_id: int | None = None
    owner_company_id: int | None = None


@dataclass(frozen=True, slots=True)
class AssetQuote:
    asset: Asset
    buyer_company: Company | None


@dataclass(frozen=True, slots=True)
class AssetPurchase:
    id: int
    guild_id: int
    asset_id: int
    buyer_citizen_id: int
    buyer_company_id: int | None
    buyer_account_id: int
    price: int
    request_key: str
    transaction_id: int
    purchased_at: datetime
