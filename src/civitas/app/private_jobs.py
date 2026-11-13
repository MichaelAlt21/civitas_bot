"""Authorize company-funded work and update escrow and work state atomically."""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id
from civitas.app.market import MarketRepository
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH, Account, AccountKind, TransactionKind
from civitas.domain.private_jobs import PrivateJob, PrivateJobStatus

_PAGE_SIZE = 10
_OVERDUE_REVIEWABLE = (PrivateJobStatus.OPEN, PrivateJobStatus.CLAIMED, PrivateJobStatus.SUBMITTED)


class PrivateJobErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    COMPANY_NOT_FOUND = "company_not_found"
    COMPANY_NOT_OWNED = "company_not_owned"
    JOB_NOT_FOUND = "job_not_found"
    NOT_JOB_PARTY = "not_job_party"
    ARBITER_REQUIRED = "arbiter_required"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_TEXT = "invalid_text"
    INVALID_DEADLINE = "invalid_deadline"
    INVALID_REQUEST_KEY = "invalid_request_key"
    INVALID_PAGE = "invalid_page"
    INVALID_STATUS = "invalid_status"
    DEADLINE_PASSED = "deadline_passed"
    WORKER_NOT_VERIFIED = "worker_not_verified"
    JOB_ALREADY_CLAIMED = "job_already_claimed"


class PrivateJobError(Exception):
    def __init__(self, reason: PrivateJobErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class PrivateJobRepository(MarketRepository, Protocol):
    async def get_private_job(self, guild_id: int, job_id: int, *, lock: bool = False) -> PrivateJob | None: ...

    async def get_private_job_by_key(self, guild_id: int, key: str) -> PrivateJob | None: ...

    async def create_private_job(
        self, *, guild_id: int, company_id: int, owner_citizen_id: int, title: str, terms: str, amount: int,
        deadline: datetime, request_key: str, publication_reason: str, created_at: datetime,
    ) -> PrivateJob | None: ...

    async def update_private_job(self, job: PrivateJob) -> None: ...

    async def list_open_private_jobs(
        self, guild_id: int, now: datetime, limit: int, *, offset: int = 0,
    ) -> tuple[PrivateJob, ...]: ...

    async def list_my_private_jobs(
        self, guild_id: int, citizen_id: int, limit: int, *, offset: int = 0, include_disputes: bool = False,
        overdue_at: datetime | None = None,
    ) -> tuple[PrivateJob, ...]: ...

    async def ensure_private_job_escrow(self, guild_id: int, job_id: int) -> Account: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _text(value: str, max_length: int) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise PrivateJobError(PrivateJobErrorReason.INVALID_TEXT)
    value = value.strip()
    if not 1 <= len(value) <= max_length:
        raise PrivateJobError(PrivateJobErrorReason.INVALID_TEXT)
    return value


def _offset(page: int) -> int:
    if type(page) is not int or not 1 <= page <= MAX_AMOUNT // _PAGE_SIZE + 1:
        raise PrivateJobError(PrivateJobErrorReason.INVALID_PAGE)
    return (page - 1) * _PAGE_SIZE


def _valid_id(value: int) -> bool:
    return type(value) is int and 1 <= value <= MAX_AMOUNT


class PrivateJobService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[PrivateJobRepository]], *, guild_id: int,
        authorize: Callable[[int | None, int], None], arbiter_user_ids: Iterable[int],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        arbiters = tuple(arbiter_user_ids)
        if not all(is_discord_id(user_id) for user_id in arbiters):
            raise ValueError("Private work arbiter IDs must be Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._arbiter_user_ids = frozenset(arbiters)
        self._clock = clock

    async def publish_job(
        self, guild_id: int | None, user_id: int, *, company_id: int, title: str, terms: str,
        amount: int, deadline: datetime, reason: str, idempotency_key: str,
    ) -> PrivateJob:
        self._authorize(guild_id, user_id)
        title, terms, reason = _text(title, 80), _text(terms, 500), _text(reason, 200)
        if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
            raise PrivateJobError(PrivateJobErrorReason.INVALID_AMOUNT)
        if not isinstance(deadline, datetime) or deadline.utcoffset() is None:
            raise PrivateJobError(PrivateJobErrorReason.INVALID_DEADLINE)
        if (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH or "\0" in idempotency_key
        ):
            raise PrivateJobError(PrivateJobErrorReason.INVALID_REQUEST_KEY)
        deadline = deadline.astimezone(UTC)
        async with self._unit_of_work() as repository:
            owner = await self._citizen(repository, user_id, lock=True)
            company = await repository.get_company(owner.guild_id, company_id) if _valid_id(company_id) else None
            if company is None:
                raise PrivateJobError(PrivateJobErrorReason.COMPANY_NOT_FOUND)
            if company.owner_citizen_id != owner.id:
                raise PrivateJobError(PrivateJobErrorReason.COMPANY_NOT_OWNED)

            existing = await repository.get_private_job_by_key(owner.guild_id, idempotency_key)
            if existing is None:
                now = self._clock()
                if deadline <= now:
                    raise PrivateJobError(PrivateJobErrorReason.INVALID_DEADLINE)
                job = await repository.create_private_job(
                    guild_id=owner.guild_id, company_id=company_id, owner_citizen_id=owner.id, title=title,
                    terms=terms, amount=amount, deadline=deadline, request_key=idempotency_key,
                    publication_reason=reason, created_at=now,
                )
                if job is not None:
                    source = await repository.ensure_company_account(owner.guild_id, company_id)
                    escrow = await repository.ensure_private_job_escrow(owner.guild_id, job.id)
                    await repository.post(
                        owner.guild_id, TransactionKind.PRIVATE_JOB_FUND, f"private_job_fund:{job.id}",
                        {source.id: -amount, escrow.id: amount}, actor_citizen_id=owner.id, reason=reason,
                    )
                    return job
                existing = await repository.get_private_job_by_key(owner.guild_id, idempotency_key)
            if existing is None or (
                existing.company_id, existing.owner_citizen_id, existing.title, existing.terms,
                existing.amount, existing.deadline, existing.publication_reason,
            ) != (company_id, owner.id, title, terms, amount, deadline, reason):
                raise PrivateJobError(PrivateJobErrorReason.INVALID_REQUEST_KEY)
            return existing

    async def list_jobs(self, guild_id: int | None, user_id: int, *, page: int = 1) -> tuple[PrivateJob, ...]:
        self._authorize(guild_id, user_id)
        offset = _offset(page)
        async with self._unit_of_work() as repository:
            viewer = await self._citizen(repository, user_id)
            return await repository.list_open_private_jobs(viewer.guild_id, self._clock(), _PAGE_SIZE, offset=offset)

    async def list_my_jobs(self, guild_id: int | None, user_id: int, *, page: int = 1) -> tuple[PrivateJob, ...]:
        self._authorize(guild_id, user_id)
        offset = _offset(page)
        async with self._unit_of_work() as repository:
            viewer = await self._citizen(repository, user_id)
            return await repository.list_my_private_jobs(
                viewer.guild_id, viewer.id, _PAGE_SIZE, offset=offset,
                include_disputes=user_id in self._arbiter_user_ids,
                overdue_at=self._clock(),
            )

    async def get_job(self, guild_id: int | None, user_id: int, job_id: int) -> PrivateJob:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            viewer = await self._citizen(repository, user_id)
            job = await self._job(repository, viewer.guild_id, job_id)
            if (
                job.status == PrivateJobStatus.OPEN or viewer.id in (job.owner_citizen_id, job.worker_citizen_id)
                or (user_id in self._arbiter_user_ids and (
                    job.disputed_at is not None
                    or (job.status in _OVERDUE_REVIEWABLE and self._clock() >= job.deadline)
                ))
            ):
                return job
            raise PrivateJobError(PrivateJobErrorReason.NOT_JOB_PARTY)

    async def claim_job(self, guild_id: int | None, user_id: int, job_id: int) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id) as (repository, actor, job, _):
            if actor.id == job.owner_citizen_id:
                raise PrivateJobError(PrivateJobErrorReason.CONFLICT_OF_INTEREST)
            if job.worker_citizen_id == actor.id:
                return job
            if job.worker_citizen_id is not None:
                raise PrivateJobError(PrivateJobErrorReason.JOB_ALREADY_CLAIMED)
            self._require_status(job, PrivateJobStatus.OPEN)
            now = self._before_deadline(job)
            claimed = replace(
                job, status=PrivateJobStatus.CLAIMED, worker_citizen_id=actor.id,
                worker_discord_user_id=actor.discord_user_id, claimed_at=now,
            )
            await repository.update_private_job(claimed)
            return claimed

    async def submit_job(self, guild_id: int | None, user_id: int, job_id: int, *, evidence: str) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id) as (repository, actor, job, _):
            self._require_party(actor, job.worker_citizen_id)
            evidence = _text(evidence, 500)
            if job.submitted_at is not None and job.evidence == evidence:
                return job
            self._require_status(job, PrivateJobStatus.CLAIMED)
            submitted = replace(job, status=PrivateJobStatus.SUBMITTED, evidence=evidence, submitted_at=self._before_deadline(job))
            await repository.update_private_job(submitted)
            return submitted

    async def accept_job(self, guild_id: int | None, user_id: int, job_id: int, *, reason: str) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id) as (repository, actor, job, worker):
            self._require_party(actor, job.owner_citizen_id)
            reason = _text(reason, 200)
            if job.status == PrivateJobStatus.PAID:
                return job
            self._require_status(job, PrivateJobStatus.SUBMITTED)
            return await self._close(repository, actor, job, worker, pay=True, reason=reason)

    async def cancel_job(self, guild_id: int | None, user_id: int, job_id: int, *, reason: str) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id) as (repository, actor, job, _):
            if not (user_id in self._arbiter_user_ids and self._clock() >= job.deadline):
                self._require_party(actor, job.owner_citizen_id, job.worker_citizen_id)
            reason = _text(reason, 200)
            if job.status == PrivateJobStatus.CANCELLED:
                return job
            self._require_status(job, PrivateJobStatus.OPEN, PrivateJobStatus.CLAIMED)
            return await self._close(repository, actor, job, None, pay=False, reason=reason)

    async def dispute_job(self, guild_id: int | None, user_id: int, job_id: int, *, reason: str) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id) as (repository, actor, job, _):
            reason = _text(reason, 200)
            now = self._clock()
            if not (user_id in self._arbiter_user_ids and now >= job.deadline):
                self._require_party(actor, job.owner_citizen_id, job.worker_citizen_id)
            if job.disputed_by_citizen_id == actor.id and job.dispute_reason == reason:
                return job
            self._require_status(job, PrivateJobStatus.SUBMITTED)
            disputed = replace(
                job, status=PrivateJobStatus.DISPUTED, disputed_at=now,
                disputed_by_citizen_id=actor.id, dispute_reason=reason,
            )
            await repository.update_private_job(disputed)
            return disputed

    async def resolve_job(
        self, guild_id: int | None, user_id: int, job_id: int, *, pay: bool, reason: str,
    ) -> PrivateJob:
        async with self._action(guild_id, user_id, job_id, arbiter=True) as (repository, actor, job, worker):
            if actor.id in (job.owner_citizen_id, job.worker_citizen_id):
                raise PrivateJobError(PrivateJobErrorReason.CONFLICT_OF_INTEREST)
            reason = _text(reason, 200)
            decision = PrivateJobStatus.PAID if pay else PrivateJobStatus.CANCELLED
            if job.status == decision and job.disputed_at is not None:
                return job
            self._require_status(job, PrivateJobStatus.DISPUTED)
            return await self._close(repository, actor, job, worker, pay=pay, reason=reason)

    async def _close(
        self, repository: PrivateJobRepository, actor: Citizen, job: PrivateJob, worker: Citizen | None,
        *, pay: bool, reason: str,
    ) -> PrivateJob:
        if pay and worker is None:
            raise PrivateJobError(PrivateJobErrorReason.WORKER_NOT_VERIFIED)
        if pay:
            destination = await repository.ensure_account(job.guild_id, AccountKind.PERSONAL, worker.id)
            kind, status = TransactionKind.PRIVATE_JOB_PAY, PrivateJobStatus.PAID
        else:
            destination = await repository.ensure_company_account(job.guild_id, job.company_id)
            kind, status = TransactionKind.PRIVATE_JOB_REFUND, PrivateJobStatus.CANCELLED
        escrow = await repository.ensure_private_job_escrow(job.guild_id, job.id)
        receipt = await repository.post(
            job.guild_id, kind, f"{kind.value.lower()}:{job.id}", {escrow.id: -job.amount, destination.id: job.amount},
            actor_citizen_id=actor.id, reason=reason,
        )
        closed = replace(
            job, status=status, closed_at=self._clock(), closed_by_citizen_id=actor.id,
            close_reason=reason, payout_transaction_id=receipt.transaction_id,
        )
        await repository.update_private_job(closed)
        return closed

    @asynccontextmanager
    async def _action(
        self, guild_id: int | None, user_id: int, job_id: int, *, arbiter: bool = False,
    ) -> AsyncIterator[tuple[PrivateJobRepository, Citizen, PrivateJob, Citizen | None]]:
        self._authorize(guild_id, user_id)
        if arbiter and user_id not in self._arbiter_user_ids:
            raise PrivateJobError(PrivateJobErrorReason.ARBITER_REQUIRED)
        async with self._unit_of_work() as repository:
            actor = await self._citizen(repository, user_id)
            job = await self._job(repository, actor.guild_id, job_id, lock=True)
            ids = (user_id,) if job.worker_discord_user_id is None else (user_id, job.worker_discord_user_id)
            citizens = await repository.get_citizens(self._guild_id, ids, lock=True)
            verified = {citizen.id: citizen for citizen in citizens if citizen.status == CitizenStatus.VERIFIED}
            locked_actor = verified.get(actor.id)
            if locked_actor is None:
                raise PrivateJobError(PrivateJobErrorReason.CITIZEN_NOT_VERIFIED)
            worker = verified.get(job.worker_citizen_id) if job.worker_citizen_id is not None else None
            yield repository, locked_actor, job, worker

    async def _citizen(self, repository: PrivateJobRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise PrivateJobError(PrivateJobErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    @staticmethod
    async def _job(
        repository: PrivateJobRepository, guild_id: int, job_id: int, *, lock: bool = False,
    ) -> PrivateJob:
        job = await repository.get_private_job(guild_id, job_id, lock=lock) if _valid_id(job_id) else None
        if job is None:
            raise PrivateJobError(PrivateJobErrorReason.JOB_NOT_FOUND)
        return job

    def _before_deadline(self, job: PrivateJob) -> datetime:
        now = self._clock()
        if now >= job.deadline:
            raise PrivateJobError(PrivateJobErrorReason.DEADLINE_PASSED)
        return now

    @staticmethod
    def _require_party(actor: Citizen, *party_ids: int | None) -> None:
        if actor.id not in party_ids:
            raise PrivateJobError(PrivateJobErrorReason.NOT_JOB_PARTY)

    @staticmethod
    def _require_status(job: PrivateJob, *statuses: PrivateJobStatus) -> None:
        if job.status not in statuses:
            raise PrivateJobError(PrivateJobErrorReason.INVALID_STATUS)
