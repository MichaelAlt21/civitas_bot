import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from civitas.app.reports import ReportAccessError, ReportService
from civitas.domain.reports import BudgetArticleReport, ReportDelivery, ReportWindow, TreasuryReport

GUILD_ID = 100
CHANNEL_ID = 200
MONDAY = datetime(2026, 11, 9, 12, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = MONDAY

    def __call__(self) -> datetime:
        return self.now


@dataclass
class QueuedReport:
    guild_id: int
    channel_id: int
    report: TreasuryReport
    next_attempt_at: datetime
    attempts: int = 0
    lease_until: datetime | None = None
    token: str | None = None
    message_id: int | None = None


class FakeRepository:
    def __init__(self) -> None:
        self.active = False
        self.saved_windows: dict[date, ReportWindow] = {}
        self.queued: dict[date, QueuedReport] = {}
        self.closing_balance = 395

    @asynccontextmanager
    async def unit_of_work(self):
        assert not self.active
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    async def get_report_window(self, guild_id, week_start):
        return self.saved_windows.get(week_start)

    async def get_active_report_window(self, guild_id, at):
        return next((window for window in self.saved_windows.values() if window.starts_at <= at < window.ends_at), None)

    async def read_report(self, guild_id, window, *, as_of):
        return TreasuryReport(
            window=window, as_of=as_of, opening_balance=400, closing_balance=self.closing_balance,
            company_fees=25, sales_tax=10, asset_sales=40, treasury_issuance=0, other_income=0,
            npc_payouts=25, contract_payouts=55, other_expenses=0, total_issued=0, starter_grants=0,
            transaction_count=5, settled_orders=1, asset_purchases=1, open_escrow=45,
            cash_reserved=80, budget_limit=200,
            budget_articles=(BudgetArticleReport("state_contracts", 150, 55, 80),),
        )

    async def latest_enqueued_week(self, guild_id):
        return max(self.queued, default=None)

    async def enqueue_report(self, guild_id, channel_id, report, *, now):
        week_start = report.window.week_start
        if week_start in self.queued:
            return False
        snapshot = TreasuryReport.from_payload(json.loads(json.dumps(report.to_payload())))
        self.queued[week_start] = QueuedReport(guild_id, channel_id, snapshot, now)
        return True

    async def claim_due_report(self, guild_id, *, now, lease_until, lease_token):
        for week in sorted(self.queued):
            row = self.queued[week]
            if row.message_id is not None or row.next_attempt_at > now or (
                row.lease_until is not None and row.lease_until > now
            ):
                continue
            row.lease_until = lease_until
            row.token = lease_token
            row.attempts += 1
            return ReportDelivery(row.guild_id, row.channel_id, row.report, row.attempts, lease_token)
        return None

    async def mark_report_sent(self, guild_id, week_start, lease_token, *, message_id, now):
        row = self.queued[week_start]
        if row.token != lease_token:
            return False
        row.message_id = message_id
        return True

    async def retry_report(self, guild_id, week_start, lease_token, *, next_attempt_at):
        row = self.queued[week_start]
        if row.token != lease_token:
            return False
        row.next_attempt_at = next_attempt_at
        row.lease_until = None
        row.token = None
        return True


def _service(repository, clock, *, channel_id=CHANNEL_ID, timezone=UTC):
    return ReportService(
        repository.unit_of_work, guild_id=GUILD_ID, timezone=timezone,
        publication_channel_id=channel_id, clock=clock,
    )


@pytest.mark.asyncio
async def test_current_and_previous_week_follow_local_monday_across_dst():
    repository = FakeRepository()
    clock = Clock(datetime(2026, 3, 29, 22, 30, tzinfo=UTC))
    service = _service(repository, clock, timezone=ZoneInfo("Europe/Paris"))

    current = await service.get_report(GUILD_ID)
    previous = await service.get_report(GUILD_ID, previous=True)

    assert current.window.week_start == date(2026, 3, 30)
    assert current.window.starts_at == datetime(2026, 3, 29, 22, tzinfo=UTC)
    assert current.as_of == clock.now
    assert previous.window.week_start == date(2026, 3, 23)
    assert previous.as_of == previous.window.ends_at == current.window.starts_at
    assert previous.window.ends_at - previous.window.starts_at == timedelta(hours=167)


@pytest.mark.asyncio
async def test_report_reads_persisted_period_boundaries_and_public_totals():
    repository = FakeRepository()
    week = MONDAY.date()
    window = ReportWindow(week, MONDAY - timedelta(hours=5), MONDAY + timedelta(days=6))
    repository.saved_windows[week] = window

    report = await _service(repository, Clock()).get_report(GUILD_ID)

    assert report.window == window
    assert report.opening_balance + report.total_income - report.total_expenses == report.closing_balance
    assert report.available_cash == 315
    assert report.transaction_count == 5
    assert TreasuryReport.from_payload(json.loads(json.dumps(report.to_payload()))) == report
    assert set(report.to_payload()["budget_articles"][0]) == {"code", "cap", "spent", "reserved"}


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [None, GUILD_ID + 1, True])
async def test_report_is_only_visible_in_configured_guild(guild_id):
    repository = FakeRepository()
    with pytest.raises(ReportAccessError):
        await _service(repository, Clock()).get_report(guild_id)


@pytest.mark.asyncio
async def test_unconfigured_delivery_channel_leaves_manual_report_enabled():
    repository = FakeRepository()
    service = _service(repository, Clock(), channel_id=None)
    send = AsyncMock()

    assert await service.get_report(GUILD_ID)
    assert not await service.run_due(send)
    assert not repository.queued
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_failure_retries_frozen_snapshot_after_restart():
    repository = FakeRepository()
    clock = Clock()
    original = _service(repository, clock)

    async def unavailable(delivery):
        assert not repository.active
        assert delivery.attempts == 1
        raise ConnectionError("Offline")

    assert not await original.run_due(unavailable)
    week_start = MONDAY.date() - timedelta(days=7)
    queued = repository.queued[week_start]
    assert queued.attempts == 1
    assert queued.message_id is None
    assert queued.next_attempt_at == MONDAY + timedelta(seconds=30)

    repository.closing_balance = 900
    restarted = _service(repository, clock, channel_id=999)
    send = AsyncMock(return_value=300)
    assert not await restarted.run_due(send)
    send.assert_not_awaited()

    clock.now += timedelta(seconds=30)
    assert await restarted.run_due(send)
    delivery = send.await_args.args[0]
    assert (delivery.channel_id, delivery.attempts, delivery.report.closing_balance) == (CHANNEL_ID, 2, 395)
    assert queued.message_id == 300
    assert not await restarted.run_due(send)
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupted_worker_reclaims_expired_lease():
    repository = FakeRepository()
    clock = Clock()
    original = _service(repository, clock)

    async def interrupted(delivery):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await original.run_due(interrupted)

    restarted = _service(repository, clock)
    send = AsyncMock(return_value=300)
    assert not await restarted.run_due(send)
    clock.now += timedelta(minutes=2)
    assert await restarted.run_due(send)
    assert send.await_args.args[0].attempts == 2


@pytest.mark.asyncio
async def test_missed_weeks_are_queued_sequentially_after_restart():
    repository = FakeRepository()
    clock = Clock()
    send = AsyncMock(return_value=300)
    assert await _service(repository, clock).run_due(send)
    clock.now += timedelta(weeks=2)
    restarted = _service(repository, clock)

    assert await restarted.run_due(send)
    assert await restarted.run_due(send)
    assert not await restarted.run_due(send)
    assert list(repository.queued) == [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]


@pytest.mark.asyncio
async def test_scheduler_uses_saved_end_before_enqueuing_closed_report():
    repository = FakeRepository()
    clock = Clock()
    last_week = MONDAY.date() - timedelta(days=7)
    ends_at = clock.now + timedelta(hours=1)
    repository.saved_windows[last_week] = ReportWindow(last_week, ends_at - timedelta(days=7), ends_at)
    service = _service(repository, clock)
    send = AsyncMock(return_value=300)

    assert await service.run_due(send)
    assert last_week not in repository.queued
    clock.now = ends_at
    assert await service.run_due(send)
    assert repository.queued[last_week].report.as_of == ends_at


@pytest.mark.asyncio
async def test_timezone_change_across_monday_preserves_active_period_and_previous_cutoff():
    repository = FakeRepository()
    clock = Clock(datetime(2026, 11, 9, 1, tzinfo=UTC))
    previous_nominal_week = date(2026, 11, 2)
    persisted = ReportWindow(
        previous_nominal_week, datetime(2026, 11, 2, 10, tzinfo=UTC), datetime(2026, 11, 9, 10, tzinfo=UTC),
    )
    repository.saved_windows[previous_nominal_week] = persisted
    service = _service(repository, clock, timezone=UTC)

    assert (await service.get_report(GUILD_ID)).window == persisted
    previous = await service.get_report(GUILD_ID, previous=True)
    assert previous.window.week_start == date(2026, 10, 26)
    assert previous.as_of == previous.window.ends_at == persisted.starts_at

    clock.now = persisted.ends_at + timedelta(hours=1)
    current = await service.get_report(GUILD_ID)
    assert current.window.week_start == date(2026, 11, 9)
    assert current.window.starts_at == persisted.ends_at
