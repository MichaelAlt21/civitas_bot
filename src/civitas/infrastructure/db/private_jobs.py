"""Persist one-off company jobs within the shared ledger transaction."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.domain.private_jobs import PrivateJob, PrivateJobStatus

from .models import CitizenRow
from .private_job_models import PrivateJobRow
from .reports import PostgresReportRepository


def _as_utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def _to_job(row: PrivateJobRow, worker_discord_id: Decimal | None = None) -> PrivateJob:
    return PrivateJob(
        id=row.id,
        guild_id=row.guild_id,
        company_id=row.company_id,
        owner_citizen_id=row.owner_citizen_id,
        title=row.title,
        terms=row.terms,
        amount=row.amount,
        deadline=row.deadline.astimezone(UTC),
        request_key=row.request_key,
        publication_reason=row.publication_reason,
        created_at=row.created_at.astimezone(UTC),
        status=PrivateJobStatus(row.status),
        worker_citizen_id=row.worker_citizen_id,
        worker_discord_user_id=int(worker_discord_id) if worker_discord_id is not None else None,
        claimed_at=_as_utc(row.claimed_at),
        submitted_at=_as_utc(row.submitted_at),
        evidence=row.evidence,
        disputed_at=_as_utc(row.disputed_at),
        disputed_by_citizen_id=row.disputed_by_citizen_id,
        dispute_reason=row.dispute_reason,
        closed_at=_as_utc(row.closed_at),
        closed_by_citizen_id=row.closed_by_citizen_id,
        close_reason=row.close_reason,
        payout_transaction_id=row.payout_transaction_id,
    )


def _select_job():
    return select(PrivateJobRow, CitizenRow.discord_user_id).outerjoin(
        CitizenRow,
        (CitizenRow.id == PrivateJobRow.worker_citizen_id) & (CitizenRow.guild_id == PrivateJobRow.guild_id),
    )


class PostgresPrivateJobRepository(PostgresReportRepository):
    async def get_private_job(self, guild_id: int, job_id: int, *, lock: bool = False) -> PrivateJob | None:
        scope = (PrivateJobRow.guild_id == guild_id, PrivateJobRow.id == job_id)
        if lock:
            # After waiting for a claim, read the claimant from a fresh statement.
            # A join in the locking query can still use its pre-lock snapshot.
            locked_id = await self._session.scalar(
                select(PrivateJobRow.id).where(*scope).with_for_update(key_share=True)
            )
            if locked_id is None:
                return None
        row = (await self._session.execute(_select_job().where(*scope))).one_or_none()
        return _to_job(*row) if row is not None else None

    async def get_private_job_by_key(self, guild_id: int, key: str) -> PrivateJob | None:
        row = (await self._session.execute(_select_job().where(
            PrivateJobRow.guild_id == guild_id, PrivateJobRow.request_key == key,
        ))).one_or_none()
        return _to_job(*row) if row is not None else None

    async def create_private_job(
        self, *, guild_id: int, company_id: int, owner_citizen_id: int, title: str, terms: str,
        amount: int, deadline: datetime, request_key: str, publication_reason: str, created_at: datetime,
    ) -> PrivateJob | None:
        row = await self._session.scalar(
            insert(PrivateJobRow).values(
                guild_id=guild_id, company_id=company_id, owner_citizen_id=owner_citizen_id,
                title=title, terms=terms, amount=amount, deadline=deadline,
                request_key=request_key, publication_reason=publication_reason, created_at=created_at,
                status=PrivateJobStatus.OPEN.value,
            ).on_conflict_do_nothing(constraint="uq_private_jobs_request").returning(PrivateJobRow)
        )
        return _to_job(row) if row is not None else None

    async def update_private_job(self, job: PrivateJob) -> None:
        await self._session.execute(
            update(PrivateJobRow).where(PrivateJobRow.guild_id == job.guild_id, PrivateJobRow.id == job.id).values(
                status=job.status.value,
                worker_citizen_id=job.worker_citizen_id, claimed_at=job.claimed_at,
                submitted_at=job.submitted_at, evidence=job.evidence,
                disputed_at=job.disputed_at, disputed_by_citizen_id=job.disputed_by_citizen_id,
                dispute_reason=job.dispute_reason, closed_at=job.closed_at,
                closed_by_citizen_id=job.closed_by_citizen_id, close_reason=job.close_reason,
                payout_transaction_id=job.payout_transaction_id,
            )
        )

    async def list_open_private_jobs(
        self, guild_id: int, now: datetime, limit: int, *, offset: int = 0,
    ) -> tuple[PrivateJob, ...]:
        rows = await self._session.scalars(select(PrivateJobRow).where(
            PrivateJobRow.guild_id == guild_id, PrivateJobRow.status == PrivateJobStatus.OPEN.value,
            PrivateJobRow.deadline > now,
        ).order_by(PrivateJobRow.id).limit(limit).offset(offset))
        return tuple(_to_job(row) for row in rows)

    async def list_my_private_jobs(
        self, guild_id: int, citizen_id: int, limit: int, *, offset: int = 0, include_disputes: bool = False,
        overdue_at: datetime | None = None,
    ) -> tuple[PrivateJob, ...]:
        visible = [PrivateJobRow.owner_citizen_id == citizen_id, PrivateJobRow.worker_citizen_id == citizen_id]
        if include_disputes:
            visible.append(PrivateJobRow.status == PrivateJobStatus.DISPUTED.value)
            if overdue_at is not None:
                visible.append(and_(
                    PrivateJobRow.status.in_(
                        (PrivateJobStatus.OPEN.value, PrivateJobStatus.CLAIMED.value, PrivateJobStatus.SUBMITTED.value)
                    ),
                    PrivateJobRow.deadline <= overdue_at,
                ))
        rows = await self._session.execute(_select_job().where(
            PrivateJobRow.guild_id == guild_id, or_(*visible),
        ).order_by(PrivateJobRow.id.desc()).limit(limit).offset(offset))
        return tuple(_to_job(*row) for row in rows)


class PostgresPrivateJobUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresPrivateJobRepository]:
        async with self._sessions.begin() as session:
            yield PostgresPrivateJobRepository(session)
