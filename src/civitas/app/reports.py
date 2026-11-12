"""Read public treasury aggregates and deliver each completed budget week."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Protocol
from uuid import uuid4

from civitas.app.citizens import is_discord_id
from civitas.app.jobs import budget_week
from civitas.domain.reports import ReportDelivery, ReportWindow, TreasuryReport

_WEEK = timedelta(days=7)
_SEND_TIMEOUT_SECONDS = 45
_LEASE_DURATION = timedelta(minutes=2)
_FIRST_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 30 * 60
_logger = logging.getLogger(__name__)


class ReportAccessError(Exception):
    """The report is only available in the configured Discord guild."""


class ReportRepository(Protocol):
    async def get_active_report_window(self, guild_id: int, at: datetime) -> ReportWindow | None: ...

    async def get_report_window(self, guild_id: int, week_start: date) -> ReportWindow | None: ...

    async def read_report(self, guild_id: int, window: ReportWindow, *, as_of: datetime) -> TreasuryReport: ...

    async def latest_enqueued_week(self, guild_id: int) -> date | None: ...

    async def enqueue_report(
        self, guild_id: int, channel_id: int, report: TreasuryReport, *, now: datetime,
    ) -> bool: ...

    async def claim_due_report(
        self, guild_id: int, *, now: datetime, lease_until: datetime, lease_token: str,
    ) -> ReportDelivery | None: ...

    async def mark_report_sent(
        self, guild_id: int, week_start: date, lease_token: str, *, message_id: int, now: datetime,
    ) -> bool: ...

    async def retry_report(
        self, guild_id: int, week_start: date, lease_token: str, *, next_attempt_at: datetime,
    ) -> bool: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_delay(attempts: int) -> timedelta:
    seconds = _FIRST_RETRY_SECONDS * 2 ** min(max(attempts - 1, 0), 6)
    return timedelta(seconds=min(seconds, _MAX_RETRY_SECONDS))


class ReportService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[ReportRepository]],
        *,
        guild_id: int,
        timezone: tzinfo,
        publication_channel_id: int | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not is_discord_id(guild_id) or (
            publication_channel_id is not None and not is_discord_id(publication_channel_id)
        ):
            raise ValueError("Report guild and channel IDs must be Discord snowflakes")
        if not isinstance(timezone, tzinfo):
            raise ValueError("Budget timezone must be a tzinfo instance")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._timezone = timezone
        self._channel_id = publication_channel_id
        self._clock = clock

    async def get_report(self, guild_id: int | None, *, previous: bool = False) -> TreasuryReport:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise ReportAccessError
        now = self._clock()
        async with self._unit_of_work() as repository:
            active = await repository.get_active_report_window(self._guild_id, now)
            if active is not None and not previous:
                window = active
            else:
                current_week = active.week_start if active is not None else budget_week(now, self._timezone)[0]
                week_start = current_week - _WEEK if previous else current_week
                window = await self._report_window(repository, week_start, active=active)
            cutoff = max(window.starts_at, min(window.ends_at, now))
            return await repository.read_report(self._guild_id, window, as_of=cutoff)

    async def run_due(self, send: Callable[[ReportDelivery], Awaitable[int]]) -> bool:
        """Queue one closed week and attempt one delivery. Return whether it was acknowledged."""
        if self._channel_id is None:
            return False

        now = self._clock()
        await self._enqueue_next_week(now, self._channel_id)
        async with self._unit_of_work() as repository:
            delivery = await repository.claim_due_report(
                self._guild_id, now=now, lease_until=now + _LEASE_DURATION, lease_token=uuid4().hex,
            )
        if delivery is None:
            return False

        week_start = delivery.report.window.week_start
        try:
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                message_id = await send(delivery)
                if not is_discord_id(message_id):
                    raise ValueError("Delivery did not return a Discord message ID")
        except Exception:
            _logger.warning("Treasury report for week %s was not acknowledged (attempt %s)", week_start, delivery.attempts)
            async with self._unit_of_work() as repository:
                await repository.retry_report(
                    self._guild_id, week_start, delivery.lease_token,
                    next_attempt_at=self._clock() + _retry_delay(delivery.attempts),
                )
            return False

        async with self._unit_of_work() as repository:
            return await repository.mark_report_sent(
                self._guild_id, week_start, delivery.lease_token, message_id=message_id, now=self._clock(),
            )

    async def _enqueue_next_week(self, now: datetime, channel_id: int) -> None:
        async with self._unit_of_work() as repository:
            active = await repository.get_active_report_window(self._guild_id, now)
            current_week = active.week_start if active is not None else budget_week(now, self._timezone)[0]
            last_enqueued = await repository.latest_enqueued_week(self._guild_id)
            week_start = last_enqueued + _WEEK if last_enqueued is not None else current_week - _WEEK
            if week_start >= current_week:
                return
            window = await self._report_window(repository, week_start, active=active)
            if window.ends_at > now:
                return
            report = await repository.read_report(self._guild_id, window, as_of=window.ends_at)
            await repository.enqueue_report(self._guild_id, channel_id, report, now=now)

    async def _report_window(
        self, repository: ReportRepository, week_start: date, *, active: ReportWindow | None,
    ) -> ReportWindow:
        saved = await repository.get_report_window(self._guild_id, week_start)
        if saved is not None:
            return saved
        anchor = datetime.combine(week_start, time.min, tzinfo=self._timezone)
        _, starts_at, ends_at = budget_week(anchor, self._timezone)
        prior = await repository.get_report_window(self._guild_id, week_start - _WEEK)
        if prior is not None:
            starts_at = max(starts_at, prior.ends_at)
        if active is not None and week_start + _WEEK == active.week_start:
            ends_at = active.starts_at
        return ReportWindow(week_start, starts_at, ends_at)
