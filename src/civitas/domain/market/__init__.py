"""Companies, offers, and immutable commercial order terms."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

BASIS_POINTS = 10_000
MAX_DELIVERY_HOURS = 720


def sales_tax(price: int, tax_bp: int) -> int:
    return price * tax_bp // BASIS_POINTS


class OrderStatus(StrEnum):
    FUNDED = "FUNDED"
    DELIVERED = "DELIVERED"
    DISPUTED = "DISPUTED"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True, slots=True)
class Company:
    id: int
    guild_id: int
    owner_citizen_id: int
    owner_discord_user_id: int
    name: str
    name_key: str
    activity: str
    fee_paid: int
    registration_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Offer:
    id: int
    guild_id: int
    company_id: int
    title: str
    terms: str
    price: int
    delivery_hours: int
    request_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OfferWithdrawal:
    offer_id: int
    withdrawn_at: datetime
    withdrawn_by_citizen_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class OrderQuote:
    offer: Offer
    seller: Company
    buyer_company_id: int | None
    tax_bp: int
    tax: int
    deadline: datetime


@dataclass(frozen=True, slots=True)
class MarketOrder:
    id: int
    guild_id: int
    offer_id: int
    seller_company_id: int
    seller_citizen_id: int
    buyer_citizen_id: int
    buyer_company_id: int | None
    buyer_account_id: int
    title: str
    terms: str
    price: int
    tax_bp: int
    deadline: datetime
    created_at: datetime
    status: OrderStatus
    request_key: str
    delivered_at: datetime | None = None
    disputed_at: datetime | None = None
    disputed_by_citizen_id: int | None = None
    dispute_reason: str | None = None
    closed_at: datetime | None = None
    closed_by_citizen_id: int | None = None
    close_reason: str | None = None

    @property
    def tax(self) -> int:
        return sales_tax(self.price, self.tax_bp)
