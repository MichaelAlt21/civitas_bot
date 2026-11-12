"""Weekly contract appropriations and audited state contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ContractStatus(StrEnum):
    PROPOSED = "PROPOSED"
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class StateContract:
    id: int
    guild_id: int
    title: str
    terms: str
    amount: int
    deadline: datetime
    recipient_citizen_id: int
    recipient_company_id: int | None
    proposer_citizen_id: int
    proposal_reason: str
    request_key: str
    created_at: datetime
    status: ContractStatus
    recipient_discord_user_id: int
    budget_period_id: int | None = None
    reserved_by_citizen_id: int | None = None
    reserved_at: datetime | None = None
    reserve_reason: str | None = None
    evidence: str | None = None
    submitted_at: datetime | None = None
    closed_by_citizen_id: int | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    payout_transaction_id: int | None = None


@dataclass(frozen=True, slots=True)
class ContractBudget:
    period_id: int
    guild_id: int
    start_at: datetime
    end_at: datetime
    overall_cap: int
    spent_total: int
    reserved_total: int
    contracts_cap: int
    contracts_spent: int
    contracts_reserved: int
    activated_at: datetime
    activated_by_citizen_id: int
    activation_reason: str
