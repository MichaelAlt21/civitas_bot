"""A private ledger receipt leased for a single delivery attempt."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from civitas.domain.ledger import TransactionKind


@dataclass(frozen=True, slots=True)
class ReceiptDetails:
    transaction_id: int
    delta: int
    kind: TransactionKind
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReceiptDelivery:
    id: int
    recipient_discord_user_id: int
    locale: str
    delta: int
    kind: TransactionKind
    transaction_id: int
    created_at: datetime
    lease_token: UUID
    attempts: int
