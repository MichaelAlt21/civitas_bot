"""Citizenship state and audit events."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CitizenStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    SUSPENDED = "SUSPENDED"
    LEFT = "LEFT"


@dataclass(frozen=True, slots=True)
class Citizen:
    id: int
    guild_id: int
    discord_user_id: int
    status: CitizenStatus
    joined_at: datetime
    verified_at: datetime | None


class CitizenAuditAction(StrEnum):
    REGISTERED = "citizen.registered"
    RESTORED = "citizen.restored"


@dataclass(frozen=True, slots=True)
class CitizenAuditEvent:
    citizen: Citizen
    action: CitizenAuditAction
    occurred_at: datetime
    reason: str
