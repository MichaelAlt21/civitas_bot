import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from civitas.app.assets import AssetService
from civitas.app.citizens import CitizensService
from civitas.app.jobs import JobService, JobsRules, budget_week
from civitas.app.market import MarketRules, MarketService
from civitas.app.reports import ReportService
from civitas.app.treasury import TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding
from civitas.domain.assets import AssetKind
from civitas.domain.jobs import BudgetCaps, JobCategory
from civitas.domain.reports import BudgetArticleReport, ReportWindow
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow
from civitas.infrastructure.db.report_models import WeeklyReportDeliveryRow
from civitas.infrastructure.db.reports import PostgresReportUnitOfWork

pytestmark = pytest.mark.asyncio
GUILD = 100
BUYER = 11
SELLER = 12
REVIEWER = 13
CHANNEL_ID = 18446744073709551614
MESSAGE_ID = 18446744073709551615


async def test_report_reconciles_cash_budget_reserves_and_private_escrow(pg_engine):
    uow = PostgresReportUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=1000, treasury_genesis=8000)
    citizens = CitizensService(
        uow, guild_id=GUILD, allowed_user_ids={BUYER, SELLER, REVIEWER}, fund_verified=funding.fund_registered_citizen,
    )
    for user_id in (BUYER, SELLER, REVIEWER):
        await citizens.register(GUILD, user_id)
    market = MarketService(
        uow, guild_id=GUILD, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1), funding=funding,
    )
    company = await market.register_company(
        GUILD, SELLER, name="Private Company", activity="Illustration", idempotency_key="report-company",
    )
    offer = await market.publish_offer(
        GUILD, SELLER, company_id=company.id, title="Private Order", terms="Two sketches",
        price=200, delivery_hours=24, idempotency_key="report-offer",
    )
    quote = await market.quote_order(GUILD, BUYER, offer.id)
    order = await market.fund_order(GUILD, BUYER, quote, idempotency_key="report-settlement")
    await market.deliver_order(GUILD, SELLER, order.id)
    await market.accept_order(GUILD, BUYER, order.id)
    quote = await market.quote_order(GUILD, BUYER, offer.id)
    await market.fund_order(GUILD, BUYER, quote, idempotency_key="report-held-escrow")

    assets = AssetService(uow, guild_id=GUILD, authorize=citizens.authorize, staff_user_ids={REVIEWER}, funding=funding)
    asset = await assets.issue_asset(
        GUILD, REVIEWER, kind=AssetKind.OFFICE, name="Private Office", purpose="Illustration",
        price=70, reason="Catalogue fixture", idempotency_key="report-property",
    )
    asset_quote = await assets.quote_asset(GUILD, BUYER, asset.id)
    await assets.buy_asset(GUILD, BUYER, asset_quote, idempotency_key="report-property-sale")

    jobs = JobService(
        uow, guild_id=GUILD, authorize=citizens.authorize, staff_user_ids={REVIEWER},
        rules=JobsRules(overall_weekly_limit=800, npc_weekly_limit=300, personal_weekly_limit=100, reward_min=10, reward_max=40),
    )
    task = await jobs.publish(
        GUILD, REVIEWER, category=JobCategory.LOGISTICS, title="Private Task", requirements="Deliver a parcel",
        reward=30, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await jobs.claim(GUILD, BUYER, task.id)
    await jobs.submit(GUILD, BUYER, task.id, "Private proof")
    await jobs.review(GUILD, REVIEWER, task.id, approve=True, reason="Fixture accepted")

    treasury = TreasuryService(
        uow, guild_id=GUILD, authorize=citizens.authorize, initiator_user_ids={BUYER}, approver_user_ids={REVIEWER},
        rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
    )
    await treasury.activate_budget(GUILD, REVIEWER, reason="Approve the fixture")
    contract = await treasury.propose_contract(
        GUILD, BUYER, recipient_company_id=company.id, title="Private contract", terms="Complete design",
        amount=100, deadline=datetime.now(UTC) + timedelta(hours=1), reason="Fixture work",
        idempotency_key="report-contract-paid",
    )
    await treasury.reserve_contract(GUILD, REVIEWER, contract.id, reason="Funds available")
    await treasury.submit_contract(GUILD, SELLER, contract.id, evidence="Private evidence")
    held_cutoff = datetime.now(UTC)
    # The app closure clock can be later than the PostgreSQL posting clock.
    # A cutoff between them sees the debit and its reserve release together.
    payout_service = TreasuryService(
        uow, guild_id=GUILD, authorize=citizens.authorize, initiator_user_ids={BUYER}, approver_user_ids={REVIEWER},
        rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
        clock=lambda: datetime.now(UTC) + timedelta(minutes=10),
    )
    paid = await payout_service.pay_contract(GUILD, REVIEWER, contract.id, reason="Work accepted")
    other = await treasury.propose_contract(
        GUILD, BUYER, recipient_company_id=company.id, title="Private contract reserve", terms="Other work",
        amount=60, deadline=datetime.now(UTC) + timedelta(hours=1), reason="Fixture work",
        idempotency_key="report-contract-held",
    )
    await treasury.reserve_contract(GUILD, REVIEWER, other.id, reason="Funds available")

    reports = ReportService(uow, guild_id=GUILD, timezone=UTC)
    report = await reports.get_report(GUILD)
    assert report.as_of < paid.closed_at
    assert (report.opening_balance, report.closing_balance, report.available_cash) == (0, 8110, 8050)
    assert report.closing_balance == report.opening_balance + report.total_income - report.total_expenses
    assert (report.company_fees, report.sales_tax, report.asset_sales, report.treasury_issuance) == (150, 20, 70, 8000)
    assert (report.npc_payouts, report.contract_payouts, report.other_income, report.other_expenses) == (30, 100, 0, 0)
    assert (report.total_issued, report.starter_grants, report.transaction_count) == (11000, 3000, 11)
    assert (report.settled_orders, report.asset_purchases, report.open_escrow) == (1, 1, 200)
    assert report.budget_limit == 800
    assert report.budget_articles == (
        BudgetArticleReport("npc_work", 300, 30, 0),
        BudgetArticleReport("state_contracts", 250, 100, 60),
    )
    assert "Private" not in str(report.to_payload())

    async with uow() as repository:
        assert await repository.get_active_report_window(GUILD, report.as_of) == report.window
        historical = await repository.read_report(GUILD, report.window, as_of=held_cutoff)
        next_start = report.window.ends_at
        next_week, _, next_end = budget_week(next_start, UTC)
        opening = await repository.read_report(GUILD, ReportWindow(next_week, next_start, next_end), as_of=next_start)
        foreign = await repository.read_report(GUILD + 1, report.window, as_of=report.as_of)
    assert (historical.closing_balance, historical.cash_reserved, historical.contract_payouts) == (8210, 100, 0)
    assert historical.budget_articles[-1] == BudgetArticleReport("state_contracts", 250, 0, 100)
    assert (opening.opening_balance, opening.closing_balance, opening.transaction_count) == (8110, 8110, 0)
    assert (foreign.closing_balance, foreign.transaction_count, foreign.budget_articles) == (0, 0, ())


async def test_queue_claim_expiry_and_ack_survive_restart_without_duplicate_work(pg_engine):
    uow = PostgresReportUnitOfWork(pg_engine)
    monday = datetime(2026, 11, 16, 9, tzinfo=UTC)
    async with uow() as repository:
        await repository.ensure_guild(GUILD)
        previous, start, end = budget_week(monday - timedelta(days=7), UTC)
        report = await repository.read_report(GUILD, ReportWindow(previous, start, end), as_of=end)

    async def enqueue() -> bool:
        async with PostgresReportUnitOfWork(pg_engine)() as repository:
            return await repository.enqueue_report(GUILD, CHANNEL_ID, report, now=monday)

    async def claim(token: str, at: datetime):
        async with PostgresReportUnitOfWork(pg_engine)() as repository:
            return await repository.claim_due_report(
                GUILD, now=at, lease_until=at + timedelta(minutes=2), lease_token=token,
            )

    assert sorted(await asyncio.gather(enqueue(), enqueue())) == [False, True]
    first, competing = await asyncio.gather(claim("first", monday), claim("competing", monday))
    assert sum(item is not None for item in (first, competing)) == 1
    initial = first or competing
    assert initial.report == report and initial.attempts == 1
    assert initial.channel_id == CHANNEL_ID
    assert await claim("too-early", monday + timedelta(seconds=119)) is None
    restarted = await claim("restarted", monday + timedelta(minutes=2))
    assert restarted.report == report and restarted.attempts == 2

    async with uow() as repository:
        assert not await repository.mark_report_sent(
            GUILD, previous, initial.lease_token, message_id=MESSAGE_ID, now=monday + timedelta(minutes=2),
        )
        assert await repository.retry_report(
            GUILD, previous, restarted.lease_token, next_attempt_at=monday + timedelta(minutes=3),
        )
    assert await claim("before-retry", monday + timedelta(seconds=179)) is None
    latest = await claim("last", monday + timedelta(minutes=3))
    assert latest.attempts == 3
    async with uow() as repository:
        assert await repository.mark_report_sent(
            GUILD, previous, latest.lease_token, message_id=MESSAGE_ID, now=monday + timedelta(minutes=3),
        )
        assert await repository.latest_enqueued_week(GUILD) == previous
    assert await claim("already-sent", monday + timedelta(minutes=10)) is None
    assert not await enqueue()
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(WeeklyReportDeliveryRow)) == 1
        assert await connection.scalar(select(WeeklyReportDeliveryRow.message_id)) == MESSAGE_ID
        assert await connection.scalar(select(func.count()).select_from(FinancialTransactionRow)) == 0


async def test_worker_retries_same_snapshot_after_failed_send_without_reposting_ledger(pg_engine):
    uow = PostgresReportUnitOfWork(pg_engine)
    async with uow() as repository:
        local_guild = await repository.ensure_guild(GUILD)
        await WalletFunding(starter_grant=0, treasury_genesis=8000).initialize_guild(repository, local_guild)

    now = datetime(2026, 11, 16, 9, tzinfo=UTC)
    deliveries = []

    def restart() -> ReportService:
        return ReportService(
            PostgresReportUnitOfWork(pg_engine), guild_id=GUILD, timezone=UTC,
            publication_channel_id=CHANNEL_ID, clock=lambda: now,
        )

    async def send(delivery) -> int:
        deliveries.append(delivery)
        if len(deliveries) == 1:
            raise OSError("Synthetic Discord outage")
        return MESSAGE_ID

    assert not await restart().run_due(send)
    now += timedelta(seconds=29)
    assert not await restart().run_due(send)
    assert len(deliveries) == 1
    now += timedelta(seconds=1)
    assert await restart().run_due(send)
    assert not await restart().run_due(send)
    assert [item.attempts for item in deliveries] == [1, 2]
    assert deliveries[0].report == deliveries[1].report
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(WeeklyReportDeliveryRow)) == 1
        assert await connection.scalar(select(WeeklyReportDeliveryRow.state)) == "SENT"
        assert await connection.scalar(select(func.count()).select_from(FinancialTransactionRow)) == 1
