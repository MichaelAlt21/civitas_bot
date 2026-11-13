"""Record and independently certify aggregate external election protocols."""

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id
from civitas.app.wallet import WalletRepository
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import (
    CandidateTally,
    Election,
    ElectionProtocol,
    ElectionStatus,
    OfficeKind,
    OfficeRoleAssignment,
    OfficeTerm,
)
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH

_PUBLIC_OUTCOMES = {ElectionStatus.NO_QUORUM, ElectionStatus.TIED, ElectionStatus.APPLIED}
_CERTIFIABLE = {ElectionStatus.NO_QUORUM, ElectionStatus.TIED, ElectionStatus.PENDING_CERTIFICATION}
_VOTING = {ElectionStatus.OPEN, ElectionStatus.EXTENDED}
_BASIS_POINTS = 10_000


class GovernmentErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    COMMISSIONER_REQUIRED = "commissioner_required"
    INVALID_OFFICE = "invalid_office"
    INVALID_CANDIDATES = "invalid_candidates"
    INSUFFICIENT_ELIGIBLE = "insufficient_eligible"
    INVALID_TEXT = "invalid_text"
    INVALID_REQUEST_KEY = "invalid_request_key"
    ELECTION_IN_PROGRESS = "election_in_progress"
    ELECTION_NOT_FOUND = "election_not_found"
    RESULT_NOT_PUBLIC = "result_not_public"
    INVALID_STATUS = "invalid_status"
    VOTING_NOT_CLOSED = "voting_not_closed"
    INVALID_SOURCE = "invalid_source"
    INVALID_TALLY = "invalid_tally"
    CANDIDATE_CONFLICT = "candidate_conflict"
    INDEPENDENT_CERTIFICATION_REQUIRED = "independent_certification_required"
    INVALID_RUNOFF = "invalid_runoff"


class GovernmentError(Exception):
    def __init__(self, reason: GovernmentErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ElectionRules:
    min_eligible: int
    quorum_bp: int
    quorum_min: int
    parliament_seats: int
    term_days: int
    voting_hours: int
    extension_hours: int
    poll_hosts: frozenset[str]

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.min_eligible, 10_000), (self.quorum_bp, _BASIS_POINTS),
            (self.quorum_min, 10_000), (self.parliament_seats, 100),
            (self.term_days, 3650), (self.voting_hours, 8760), (self.extension_hours, 8760),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Election rules require positive values within supported bounds")
        if not self.poll_hosts or any(
            not isinstance(host, str) or not host or any(char in host for char in "/:@?#\\ ")
            for host in self.poll_hosts
        ):
            raise ValueError("Poll source hosts must be configured as bare hostnames")


class GovernmentRepository(WalletRepository, Protocol):
    async def list_verified_citizens(self, guild_id: int) -> tuple[Citizen, ...]: ...

    async def get_election(self, guild_id: int, election_id: int, *, lock: bool = False) -> Election | None: ...

    async def get_election_by_key(self, guild_id: int, key: str) -> Election | None: ...

    async def create_election(self, election: Election) -> Election | None: ...

    async def update_election(self, election: Election) -> None: ...

    async def apply_election(self, election: Election) -> tuple[OfficeTerm, ...]: ...

    async def list_offices(self, guild_id: int, at: datetime) -> tuple[OfficeTerm, ...]: ...

    async def list_results(self, guild_id: int, limit: int) -> tuple[Election, ...]: ...

    async def list_office_role_assignments(
        self, discord_guild_id: int, at: datetime
    ) -> tuple[OfficeRoleAssignment, ...]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _reason(value: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise GovernmentError(GovernmentErrorReason.INVALID_TEXT)
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise GovernmentError(GovernmentErrorReason.INVALID_TEXT)
    return value


def _validate_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or "\0" in key
    ):
        raise GovernmentError(GovernmentErrorReason.INVALID_REQUEST_KEY)


class GovernmentService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[GovernmentRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        commissioner_user_ids: Iterable[int],
        rules: ElectionRules,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        commissioners = tuple(commissioner_user_ids)
        if not all(is_discord_id(user_id) for user_id in commissioners):
            raise ValueError("Commissioner IDs must be Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._commissioners = frozenset(commissioners)
        self._rules = rules
        self._poll_hosts = frozenset(host.lower() for host in rules.poll_hosts)
        self._clock = clock

    async def open_election(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        office: OfficeKind,
        candidate_user_ids: tuple[int, ...],
        reason: str,
        idempotency_key: str,
        runoff_of_id: int | None = None,
    ) -> Election:
        self._commissioner(guild_id, user_id)
        _validate_key(idempotency_key)
        reason = _reason(reason)
        if not isinstance(office, OfficeKind):
            raise GovernmentError(GovernmentErrorReason.INVALID_OFFICE)
        if any(not is_discord_id(candidate_id) for candidate_id in candidate_user_ids):
            raise GovernmentError(GovernmentErrorReason.INVALID_CANDIDATES)
        candidates = set(candidate_user_ids)
        if not candidates or len(candidates) != len(candidate_user_ids):
            raise GovernmentError(GovernmentErrorReason.INVALID_CANDIDATES)
        if user_id in candidates:
            raise GovernmentError(GovernmentErrorReason.CANDIDATE_CONFLICT)

        async with self._unit_of_work() as repository:
            commissioner = await self._verified(repository, user_id, lock=True)
            existing = await repository.get_election_by_key(commissioner.guild_id, idempotency_key)
            if existing is not None:
                return self._replay_open(existing, commissioner.id, office, candidates, reason, runoff_of_id)
            eligible = await repository.list_verified_citizens(commissioner.guild_id)
            by_discord_id = {citizen.discord_user_id: citizen for citizen in eligible}
            if len(eligible) < self._rules.min_eligible:
                raise GovernmentError(GovernmentErrorReason.INSUFFICIENT_ELIGIBLE)
            if not candidates.issubset(by_discord_id):
                raise GovernmentError(GovernmentErrorReason.INVALID_CANDIDATES)
            seats = 1 if office == OfficeKind.PRESIDENT else self._rules.parliament_seats
            term_days = self._rules.term_days
            if runoff_of_id is not None:
                parent = await self._election(repository, commissioner.guild_id, runoff_of_id)
                if (
                    parent.office != office or parent.status != ElectionStatus.TIED
                    or parent.certified_at is None or parent.protocol is None
                    or candidates != self._runoff_candidates(parent)
                ):
                    raise GovernmentError(GovernmentErrorReason.INVALID_RUNOFF)
                seats, term_days = parent.seats, parent.term_days
            if len(candidates) < seats:
                raise GovernmentError(GovernmentErrorReason.INVALID_CANDIDATES)
            quorum = max(
                self._rules.quorum_min,
                (len(eligible) * self._rules.quorum_bp + _BASIS_POINTS - 1) // _BASIS_POINTS,
            )
            if quorum > len(eligible):
                raise GovernmentError(GovernmentErrorReason.INSUFFICIENT_ELIGIBLE)
            ordered_candidates = sorted((by_discord_id[user] for user in candidates), key=lambda citizen: citizen.id)
            now = self._clock()
            election = Election(
                id=0,
                guild_id=commissioner.guild_id,
                office=office,
                seats=seats,
                term_days=term_days,
                quorum=quorum,
                opens_at=now,
                closes_at=now + timedelta(hours=self._rules.voting_hours),
                opened_by_citizen_id=commissioner.id,
                reason=reason,
                request_key=idempotency_key,
                eligible_citizen_ids=tuple(sorted(citizen.id for citizen in eligible)),
                candidate_citizen_ids=tuple(citizen.id for citizen in ordered_candidates),
                candidate_discord_user_ids=tuple(citizen.discord_user_id for citizen in ordered_candidates),
                runoff_of_id=runoff_of_id,
                extension_hours=self._rules.extension_hours,
            )
            created = await repository.create_election(election)
            if created is not None:
                return created
            existing = await repository.get_election_by_key(commissioner.guild_id, idempotency_key)
            if existing is not None:
                return self._replay_open(existing, commissioner.id, office, candidates, reason, runoff_of_id)
            raise GovernmentError(GovernmentErrorReason.ELECTION_IN_PROGRESS)

    async def record_protocol(
        self,
        guild_id: int | None,
        user_id: int,
        election_id: int,
        *,
        source_url: str,
        participants: int,
        tallies: Mapping[int, int],
        reason: str,
    ) -> Election:
        async with self._commission_action(guild_id, user_id, election_id) as (repository, actor, election):
            source_url = self._validate_source(source_url)
            reason = _reason(reason)
            candidate_tallies = self._validate_tallies(election, participants, tallies)
            previous = election.protocol
            now = self._clock()
            extension_closed = (
                election.status == ElectionStatus.EXTENDED
                and election.extended_until is not None
                and now >= election.extended_until
            )
            if not extension_closed and previous is not None and (
                previous.source_url, previous.participants, previous.tallies, previous.recorded_by_citizen_id, previous.reason
            ) == (source_url, participants, candidate_tallies, actor.id, reason):
                return election
            if election.status not in _VOTING:
                raise GovernmentError(GovernmentErrorReason.INVALID_STATUS)
            if now < (election.extended_until or election.closes_at):
                raise GovernmentError(GovernmentErrorReason.VOTING_NOT_CLOSED)

            winners = ()
            extended_until = election.extended_until
            if participants < election.quorum:
                if election.status == ElectionStatus.OPEN:
                    status = ElectionStatus.EXTENDED
                    extended_until = now + timedelta(hours=election.extension_hours)
                else:
                    status = ElectionStatus.NO_QUORUM
            else:
                winners = self._winners(candidate_tallies, election.seats)
                status = ElectionStatus.PENDING_CERTIFICATION if winners else ElectionStatus.TIED
            recorded = replace(
                election,
                status=status,
                extended_until=extended_until,
                protocol=ElectionProtocol(source_url, participants, candidate_tallies, winners, actor.id, now, reason),
            )
            await repository.update_election(recorded)
            return recorded

    async def certify_election(
        self, guild_id: int | None, user_id: int, election_id: int, *, reason: str
    ) -> Election:
        async with self._commission_action(guild_id, user_id, election_id) as (repository, actor, election):
            reason = _reason(reason)
            if election.protocol is None:
                raise GovernmentError(GovernmentErrorReason.INVALID_STATUS)
            if actor.id == election.protocol.recorded_by_citizen_id:
                raise GovernmentError(GovernmentErrorReason.INDEPENDENT_CERTIFICATION_REQUIRED)
            if election.certified_at is not None:
                return election
            if election.status not in _CERTIFIABLE:
                raise GovernmentError(GovernmentErrorReason.INVALID_STATUS)
            status = election.status
            if status == ElectionStatus.PENDING_CERTIFICATION:
                status = ElectionStatus.APPLIED
            certified = replace(
                election,
                status=status,
                certified_by_citizen_id=actor.id,
                certified_at=self._clock(),
                certification_reason=reason,
            )
            if status == ElectionStatus.APPLIED:
                await repository.apply_election(certified)
            else:
                await repository.update_election(certified)
            return certified

    async def list_offices(self, guild_id: int | None, user_id: int) -> tuple[OfficeTerm, ...]:
        self._public_access(guild_id, user_id)
        async with self._unit_of_work() as repository:
            internal_guild_id = await repository.ensure_guild(self._guild_id)
            return await repository.list_offices(internal_guild_id, self._clock())

    async def list_results(self, guild_id: int | None, user_id: int) -> tuple[Election, ...]:
        self._public_access(guild_id, user_id)
        async with self._unit_of_work() as repository:
            internal_guild_id = await repository.ensure_guild(self._guild_id)
            return await repository.list_results(internal_guild_id, 10)

    async def get_public_result(self, guild_id: int | None, user_id: int, election_id: int) -> Election:
        self._public_access(guild_id, user_id)
        async with self._unit_of_work() as repository:
            internal_guild_id = await repository.ensure_guild(self._guild_id)
            election = await self._election(repository, internal_guild_id, election_id)
            if election.status not in _PUBLIC_OUTCOMES or election.certified_at is None:
                raise GovernmentError(GovernmentErrorReason.RESULT_NOT_PUBLIC)
            return election

    async def get_election(self, guild_id: int | None, user_id: int, election_id: int) -> Election:
        if user_id not in self._commissioners:
            return await self.get_public_result(guild_id, user_id, election_id)
        self._commissioner(guild_id, user_id)
        async with self._unit_of_work() as repository:
            actor = await self._verified(repository, user_id)
            return await self._election(repository, actor.guild_id, election_id)

    async def office_role_assignments(self) -> tuple[OfficeRoleAssignment, ...]:
        async with self._unit_of_work() as repository:
            return await repository.list_office_role_assignments(self._guild_id, self._clock())

    @asynccontextmanager
    async def _commission_action(
        self, guild_id: int | None, user_id: int, election_id: int
    ) -> AsyncIterator[tuple[GovernmentRepository, Citizen, Election]]:
        self._commissioner(guild_id, user_id)
        async with self._unit_of_work() as repository:
            actor = await self._verified(repository, user_id)
            election = await self._election(repository, actor.guild_id, election_id, lock=True)
            actor = await self._verified(repository, user_id, lock=True)
            if actor.id in election.candidate_citizen_ids:
                raise GovernmentError(GovernmentErrorReason.CANDIDATE_CONFLICT)
            yield repository, actor, election

    def _public_access(self, guild_id: int | None, user_id: int) -> None:
        if guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
        if not is_discord_id(user_id):
            raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)

    def _commissioner(self, guild_id: int | None, user_id: int) -> None:
        self._authorize(guild_id, user_id)
        if user_id not in self._commissioners:
            raise GovernmentError(GovernmentErrorReason.COMMISSIONER_REQUIRED)

    async def _verified(self, repository: GovernmentRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise GovernmentError(GovernmentErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    @staticmethod
    async def _election(
        repository: GovernmentRepository, guild_id: int, election_id: int, *, lock: bool = False
    ) -> Election:
        if type(election_id) is not int or not 1 <= election_id <= MAX_AMOUNT:
            raise GovernmentError(GovernmentErrorReason.ELECTION_NOT_FOUND)
        election = await repository.get_election(guild_id, election_id, lock=lock)
        if election is None:
            raise GovernmentError(GovernmentErrorReason.ELECTION_NOT_FOUND)
        return election

    def _validate_source(self, source_url: str) -> str:
        if not isinstance(source_url, str) or not 1 <= len(source_url) <= 500 or any(
            char.isspace() or ord(char) < 32 or char == "\\" for char in source_url
        ):
            raise GovernmentError(GovernmentErrorReason.INVALID_SOURCE)
        try:
            parts = urlsplit(source_url)
            allowed = (
                parts.scheme == "https" and parts.hostname in self._poll_hosts and parts.port in (None, 443)
                and parts.username is None and parts.password is None and bool(parts.path.strip("/"))
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise GovernmentError(GovernmentErrorReason.INVALID_SOURCE)
        return source_url

    @staticmethod
    def _validate_tallies(
        election: Election, participants: int, tallies: Mapping[int, int]
    ) -> tuple[CandidateTally, ...]:
        if (
            type(participants) is not int or not 0 <= participants <= len(election.eligible_citizen_ids)
            or set(tallies) != set(election.candidate_discord_user_ids)
            or any(not is_discord_id(candidate) for candidate in tallies)
            or any(type(votes) is not int or not 0 <= votes <= participants for votes in tallies.values())
            or sum(tallies.values()) > participants
        ):
            raise GovernmentError(GovernmentErrorReason.INVALID_TALLY)
        return tuple(
            CandidateTally(citizen_id, tallies[user_id], user_id)
            for citizen_id, user_id in zip(election.candidate_citizen_ids, election.candidate_discord_user_ids, strict=True)
        )

    @staticmethod
    def _winners(tallies: tuple[CandidateTally, ...], seats: int) -> tuple[int, ...]:
        ranked = sorted(tallies, key=lambda tally: (-tally.votes, tally.candidate_citizen_id))
        cutoff = ranked[seats - 1].votes
        if cutoff == 0 or (len(ranked) > seats and cutoff == ranked[seats].votes):
            return ()
        return tuple(tally.candidate_citizen_id for tally in ranked[:seats])

    @staticmethod
    def _runoff_candidates(parent: Election) -> set[int]:
        assert parent.protocol is not None
        if parent.office == OfficeKind.PARLIAMENT:
            return set(parent.candidate_discord_user_ids)
        highest = max(tally.votes for tally in parent.protocol.tallies)
        return {tally.candidate_discord_user_id for tally in parent.protocol.tallies if tally.votes == highest}

    @staticmethod
    def _replay_open(
        election: Election, actor_id: int, office: OfficeKind, candidates: set[int], reason: str, runoff_of_id: int | None
    ) -> Election:
        if (election.opened_by_citizen_id, election.office, election.reason, election.runoff_of_id) != (
            actor_id, office, reason, runoff_of_id
        ) or set(election.candidate_discord_user_ids) != candidates:
            raise GovernmentError(GovernmentErrorReason.INVALID_REQUEST_KEY)
        return election
