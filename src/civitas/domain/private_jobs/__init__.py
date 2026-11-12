"""One-off company work and its escrow-backed lifecycle."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PrivateJobStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    SUBMITTED = "SUBMITTED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    DISPUTED = "DISPUTED"


@dataclass(frozen=True, slots=True)
class PrivateJob:
    id: int
    guild_id: int
    company_id: int
    owner_citizen_id: int
    title: str
    terms: str
    amount: int
    deadline: datetime
    request_key: str
    publication_reason: str
    created_at: datetime
    status: PrivateJobStatus = PrivateJobStatus.OPEN
    worker_citizen_id: int | None = None
    worker_discord_user_id: int | None = None
    claimed_at: datetime | None = None
    submitted_at: datetime | None = None
    evidence: str | None = None
    disputed_at: datetime | None = None
    disputed_by_citizen_id: int | None = None
    dispute_reason: str | None = None
    closed_at: datetime | None = None
    closed_by_citizen_id: int | None = None
    close_reason: str | None = None
    payout_transaction_id: int | None = None
