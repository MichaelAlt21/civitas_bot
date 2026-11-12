"""NPC tasks, claims, and weekly budget snapshots."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobCategory(StrEnum):
    INDUSTRY = "industry"
    LOGISTICS = "logistics"
    SERVICES = "services"
    EVENTS = "events"


class JobClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"
    SUBMITTED = "SUBMITTED"
    PAID = "PAID"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class JobTask:
    id: int
    guild_id: int
    category: JobCategory
    title: str
    requirements: str
    reward: int
    expires_at: datetime
    created_at: datetime
    published_by_citizen_id: int


@dataclass(frozen=True, slots=True)
class JobClaim:
    id: int
    guild_id: int
    task_id: int
    citizen_id: int
    discord_user_id: int
    status: JobClaimStatus
    claimed_at: datetime
    evidence: str | None = None
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewer_citizen_id: int | None = None
    review_reason: str | None = None
    payout_transaction_id: int | None = None
    budget_period_id: int | None = None


@dataclass(frozen=True, slots=True)
class JobDetail:
    task: JobTask
    claim: JobClaim | None


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    overall: int
    npc: int
    per_citizen: int


@dataclass(frozen=True, slots=True)
class BudgetPeriod:
    id: int
    guild_id: int
    start_at: datetime
    end_at: datetime
    caps: BudgetCaps
    spent_total: int
    spent_npc: int
    reserved_total: int = 0
