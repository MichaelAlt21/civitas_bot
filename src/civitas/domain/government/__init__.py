"""External election records and time-bounded public offices."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OfficeKind(StrEnum):
    PRESIDENT = "PRESIDENT"
    PARLIAMENT = "PARLIAMENT"


class ElectionStatus(StrEnum):
    OPEN = "OPEN"
    EXTENDED = "EXTENDED"
    NO_QUORUM = "NO_QUORUM"
    TIED = "TIED"
    PENDING_CERTIFICATION = "PENDING_CERTIFICATION"
    APPLIED = "APPLIED"


@dataclass(frozen=True, slots=True)
class CandidateTally:
    candidate_citizen_id: int
    votes: int
    candidate_discord_user_id: int


@dataclass(frozen=True, slots=True)
class ElectionProtocol:
    source_url: str
    participants: int
    tallies: tuple[CandidateTally, ...]
    winners: tuple[int, ...]
    recorded_by_citizen_id: int
    recorded_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class CrisisResolution:
    election_id: int
    proposed_by_citizen_id: int
    proposed_at: datetime
    proposal_reason: str
    confirmed_by_citizen_id: int | None = None
    confirmed_at: datetime | None = None
    confirmation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Election:
    id: int
    guild_id: int
    office: OfficeKind
    seats: int
    term_days: int
    quorum: int
    opens_at: datetime
    closes_at: datetime
    opened_by_citizen_id: int
    reason: str
    request_key: str
    eligible_citizen_ids: tuple[int, ...]
    candidate_citizen_ids: tuple[int, ...]
    status: ElectionStatus = ElectionStatus.OPEN
    protocol: ElectionProtocol | None = None
    extended_until: datetime | None = None
    certified_by_citizen_id: int | None = None
    certified_at: datetime | None = None
    certification_reason: str | None = None
    runoff_of_id: int | None = None
    extension_hours: int = 24
    candidate_discord_user_ids: tuple[int, ...] = ()
    retry_of_id: int | None = None
    crisis_resolution: CrisisResolution | None = None

    @property
    def failed(self) -> bool:
        return self.certified_at is not None and self.status in (ElectionStatus.NO_QUORUM, ElectionStatus.TIED)

    @property
    def opens_political_crisis(self) -> bool:
        return self.failed and self.retry_of_id is not None

    @property
    def is_political_crisis_open(self) -> bool:
        return self.opens_political_crisis and (
            self.crisis_resolution is None or self.crisis_resolution.confirmed_at is None
        )


@dataclass(frozen=True, slots=True)
class OfficeTerm:
    election_id: int
    office: OfficeKind
    seat: int
    holder_citizen_id: int
    holder_discord_user_id: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class OfficeRoleAssignment:
    office: OfficeKind
    discord_user_id: int
    present: bool
