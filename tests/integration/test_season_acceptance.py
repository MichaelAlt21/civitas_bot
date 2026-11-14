from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.assets import AssetService
from civitas.app.citizens import CitizensService
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.jobs import JobService, JobsRules
from civitas.app.market import MarketRules, MarketService
from civitas.app.reports import ReportService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.assets import AssetKind
from civitas.domain.government import ElectionStatus, OfficeKind
from civitas.domain.jobs import BudgetCaps, JobCategory, JobClaimStatus
from civitas.domain.ledger import AccountKind, TransactionKind
from civitas.domain.market import OrderStatus
from civitas.domain.reports import BudgetArticleReport
from civitas.domain.treasury import ContractStatus
from civitas.infrastructure.db.ledger_models import FinancialAccountRow, FinancialTransactionRow, LedgerEntryRow
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork


@pytest.mark.asyncio
async def test_season_connects_elected_authority_company_income_ownership_and_public_report(
    pg_engine: AsyncEngine,
) -> None:
    guild = 101
    buyer, seller, deputy, recorder, verifier = range(201, 206)
    participants = (buyer, seller, deputy, recorder, verifier)
    async with pg_engine.connect() as connection:
        now = (await connection.execute(select(func.clock_timestamp()))).scalar_one().astimezone(UTC)
    # Put the exercise near local noon so ledger timestamps and the fixed
    # application clock stay in the same budget week at UTC boundaries.
    budget_timezone = timezone(timedelta(hours=12 - now.hour))
    clock = now - timedelta(days=2, hours=1)

    def current_time() -> datetime:
        return clock

    uow = PostgresReceiptUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        uow, guild_id=guild, allowed_user_ids=participants, fund_verified=funding.fund_registered_citizen,
    )
    access = {"guild_id": guild, "authorize": citizens.authorize}
    wallet = WalletService(uow, **access, funding=funding)
    for user_id in participants:
        await citizens.register(guild, user_id)
        assert await wallet.balance(guild, user_id) == 250

    election_rules = ElectionRules(
        min_eligible=5, quorum_bp=4000, quorum_min=3, parliament_seats=1, term_days=28,
        voting_hours=48, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
    )
    government = GovernmentService(
        uow, **access, commissioner_user_ids={recorder, verifier}, rules=election_rules, clock=current_time,
    )
    elections = []
    for office, candidate in ((OfficeKind.PRESIDENT, buyer), (OfficeKind.PARLIAMENT, deputy)):
        election = await government.open_election(
            guild, recorder, office=office, candidate_user_ids=(candidate,),
            reason="Open season election", idempotency_key=f"season-{office}",
        )
        elections.append((election, candidate))

    clock = now
    for election, candidate in elections:
        await government.record_protocol(
            guild, recorder, election.id, source_url=f"https://poll.example.test/{election.id}",
            participants=3, tallies={candidate: 3}, reason="Record aggregated results",
        )
        certified = await government.certify_election(guild, verifier, election.id, reason="Verify protocol")
        assert certified.status is ElectionStatus.APPLIED
    assert {term.office: term.holder_discord_user_id for term in await government.list_offices(guild, buyer)} == {
        OfficeKind.PRESIDENT: buyer, OfficeKind.PARLIAMENT: deputy,
    }

    jobs = JobService(
        uow, **access, staff_user_ids={verifier}, clock=current_time,
        rules=JobsRules(
            overall_weekly_limit=800, npc_weekly_limit=300, personal_weekly_limit=100,
            reward_min=10, reward_max=40, budget_timezone=budget_timezone,
        ),
    )
    deadline = now + timedelta(hours=2)
    task = await jobs.publish(
        guild, verifier, category=JobCategory.LOGISTICS, title="Deliver plans", requirements="Check a parcel",
        reward=30, expires_at=deadline,
    )
    await jobs.claim(guild, buyer, task.id)
    await jobs.submit(guild, buyer, task.id, "Parcel checked")
    job = await jobs.review(guild, verifier, task.id, approve=True, reason="Work verified")
    assert job.status is JobClaimStatus.PAID

    market = MarketService(
        uow, **access, arbiter_user_ids=(), clock=current_time, funding=funding,
        rules=MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1),
    )
    company_request = dict(name="Town Studio", activity="Drafting plans", idempotency_key="season-company")
    company = await market.register_company(guild, seller, **company_request)
    offer = await market.publish_offer(
        guild, seller, company_id=company.id, title="Sketch", terms="One set of plans", price=200,
        delivery_hours=24, idempotency_key="season-offer",
    )
    order_quote = await market.quote_order(guild, buyer, offer.id)
    order = await market.fund_order(guild, buyer, order_quote, idempotency_key="season-order")
    await market.deliver_order(guild, seller, order.id)
    sale = await market.accept_order(guild, buyer, order.id)
    assert sale.status is OrderStatus.COMPLETED and sale.tax == 20
    assert await market.company_balance(guild, seller, company.id) == 180

    treasury_rules = TreasuryRules(
        caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250, budget_timezone=budget_timezone,
    )

    def elected_treasury(unit_of_work: PostgresReceiptUnitOfWork) -> TreasuryService:
        return TreasuryService(
            unit_of_work, **access, initiator_user_ids=(), approver_user_ids=(),
            rules=treasury_rules, elected_authority=True, clock=current_time,
        )

    treasury = elected_treasury(uow)
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await treasury.activate_budget(guild, seller, reason="Applicant cannot approve the budget")
    await treasury.activate_budget(guild, deputy, reason="Approve public works")
    contract = await treasury.propose_contract(
        guild, buyer, recipient_company_id=company.id, title="Town hall plans", terms="File the blueprints",
        amount=60, deadline=deadline, reason="Public works", idempotency_key="season-contract",
    )
    await treasury.reserve_contract(guild, deputy, contract.id, reason="Budget verified")
    await treasury.submit_contract(guild, seller, contract.id, evidence="Blueprints filed")
    paid = await treasury.pay_contract(guild, deputy, contract.id, reason="Accept blueprints")
    assert paid.status is ContractStatus.PAID
    assert await market.company_balance(guild, seller, company.id) == 240

    assets = AssetService(uow, **access, staff_user_ids={deputy}, funding=funding, clock=current_time)
    office = await assets.issue_asset(
        guild, deputy, kind=AssetKind.OFFICE, name="Studio office", purpose="Public asset catalogue",
        price=220, reason="Offer office for sale", idempotency_key="season-list-office",
    )
    asset_quote = await assets.quote_asset(guild, seller, office.id, buyer_company_id=company.id)
    purchase = await assets.buy_asset(guild, seller, asset_quote, idempotency_key="season-buy-office")

    cutoff = now + timedelta(hours=1)
    reports = ReportService(uow, guild_id=guild, timezone=budget_timezone, clock=lambda: cutoff)
    report = await reports.get_report(guild)

    # Recreate the state-contract service as after a process restart and replay
    # completed operations through their normal entry points.
    assert await elected_treasury(PostgresReceiptUnitOfWork(pg_engine)).pay_contract(
        guild, deputy, contract.id, reason="Accept blueprints",
    ) == paid
    assert await market.register_company(guild, seller, **company_request) == company
    assert await market.accept_order(guild, buyer, order.id) == sale
    assert await assets.buy_asset(guild, seller, asset_quote, idempotency_key="season-buy-office") == purchase
    assert await jobs.review(guild, verifier, task.id, approve=True, reason="Work verified") == job
    await citizens.register(guild, buyer)
    assert await reports.get_report(guild) == report

    (owned,) = await assets.list_owned_assets(guild, seller, company_id=company.id)
    assert (owned.id, owned.owner_company_id) == (office.id, company.id)
    assert await market.company_balance(guild, seller, company.id) == 20
    assert await wallet.balance(guild, buyer) == 80
    assert await wallet.balance(guild, seller) == 100

    account, posting, entry = FinancialAccountRow, FinancialTransactionRow, LedgerEntryRow
    async with pg_engine.connect() as connection:
        balances = dict((await connection.execute(
            select(account.kind, func.sum(entry.delta))
            .join(entry, and_(entry.guild_id == account.guild_id, entry.account_id == account.id))
            .where(account.guild_id == company.guild_id).group_by(account.kind)
        )).all())
        posted = dict((await connection.execute(
            select(posting.kind, func.count()).where(posting.guild_id == company.guild_id).group_by(posting.kind)
        )).all())
        imbalances = (await connection.scalars(
            select(entry.transaction_id).where(entry.guild_id == company.guild_id)
            .group_by(entry.transaction_id).having(func.sum(entry.delta) != 0)
        )).all()
    assert balances == {
        AccountKind.ISSUER: -9250, AccountKind.TREASURY: 8300, AccountKind.PERSONAL: 930,
        AccountKind.COMPANY: 20, AccountKind.ESCROW: 0,
    }
    assert not imbalances
    assert posted == {
        TransactionKind.GENESIS: 1, TransactionKind.STARTER_GRANT: 5, TransactionKind.NPC_PAYOUT: 1,
        TransactionKind.COMPANY_FEE: 1, TransactionKind.ORDER_FUND: 1, TransactionKind.ORDER_SETTLE: 1,
        TransactionKind.STATE_CONTRACT_PAYOUT: 1, TransactionKind.ASSET_PURCHASE: 1,
    }
    assert (report.opening_balance, report.closing_balance, report.available_cash) == (0, 8300, 8300)
    assert report.closing_balance == balances[AccountKind.TREASURY]
    assert (report.company_fees, report.sales_tax, report.asset_sales) == (150, 20, 220)
    assert (report.npc_payouts, report.contract_payouts, report.open_escrow) == (30, 60, 0)
    assert (report.total_issued, report.starter_grants, report.transaction_count) == (9250, 1250, sum(posted.values()))
    assert (report.settled_orders, report.asset_purchases, report.budget_limit) == (1, 1, 800)
    assert report.budget_articles == (
        BudgetArticleReport("npc_work", 300, 30, 0), BudgetArticleReport("state_contracts", 250, 60, 0),
    )
