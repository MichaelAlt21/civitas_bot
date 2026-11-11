"""Account, transaction, and posting types for the ledger."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_AMOUNT = 2**63 - 1
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class AccountKind(StrEnum):
    ISSUER = "ISSUER"
    TREASURY = "TREASURY"
    PERSONAL = "PERSONAL"


class TransactionKind(StrEnum):
    GENESIS = "GENESIS"
    STARTER_GRANT = "STARTER_GRANT"
    TRANSFER = "TRANSFER"
    NPC_PAYOUT = "NPC_PAYOUT"


@dataclass(frozen=True, slots=True)
class Account:
    id: int
    guild_id: int
    kind: AccountKind
    citizen_id: int | None


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    transaction_id: int
    kind: TransactionKind
    delta: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PostResult:
    transaction_id: int
    created: bool


class LedgerErrorReason(StrEnum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    BALANCE_OVERFLOW = "balance_overflow"
    INVALID_POSTING = "invalid_posting"


class LedgerError(Exception):
    def __init__(self, reason: LedgerErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)
