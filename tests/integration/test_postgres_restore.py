import asyncio
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from civitas.app.assets import AssetService
from civitas.app.citizens import CitizensService
from civitas.app.government import ElectionRules, GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.app.jobs import JobService, JobsRules
from civitas.app.market import MarketError, MarketErrorReason, MarketRules, MarketService
from civitas.app.private_jobs import PrivateJobService
from civitas.app.treasury import TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.assets import AssetKind
from civitas.domain.government import OfficeKind
from civitas.domain.jobs import BudgetCaps, JobCategory
from civitas.infrastructure.db import create_db_engine
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork
from civitas.infrastructure.db.maintenance import PostgresMaintenanceUnitOfWork
from civitas.infrastructure.db.private_jobs import PostgresPrivateJobUnitOfWork
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork


async def _run_postgres_tool(tool: Path, arguments: list[str], environment: dict[str, str]) -> None:
    result = await asyncio.to_thread(
        subprocess.run, [str(tool), *arguments], env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


async def _snapshot(engine) -> dict[str, list[tuple]]:
    queries = {
        "revision": "SELECT version_num FROM alembic_version ORDER BY version_num",
        "citizens": "SELECT id, discord_user_id, status FROM citizens ORDER BY id",
        "audit": "SELECT id, citizen_id, action FROM audit_events ORDER BY id",
        "accounts": "SELECT id, kind, citizen_id FROM financial_accounts ORDER BY id",
        "transactions": "SELECT id, kind, idempotency_key FROM financial_transactions ORDER BY id",
        "entries": "SELECT transaction_id, account_id, delta FROM ledger_entries ORDER BY 1, 2",
        "claims": "SELECT id, status, payout_transaction_id, budget_period_id FROM work_claims ORDER BY id",
        "budget": "SELECT id, spent_total, reserved_total FROM budget_periods ORDER BY id",
        "budget_articles": "SELECT period_id, code, spent, reserved FROM budget_articles ORDER BY period_id, code",
        "budget_approvals": "SELECT period_id, activated_by_citizen_id FROM contract_budget_approvals ORDER BY period_id",
        "state_contracts": "SELECT id, status, budget_period_id, amount, payout_transaction_id FROM state_contracts ORDER BY id",
        "private_jobs": "SELECT id, status, amount, worker_citizen_id, payout_transaction_id FROM private_jobs ORDER BY id",
        "orders": "SELECT id, offer_id, status, price, tax_bp, buyer_account_id FROM orders ORDER BY id",
        "offer_withdrawals": (
            "SELECT offer_id, guild_id, withdrawn_by_citizen_id, withdrawn_at, reason "
            "FROM offer_withdrawals ORDER BY offer_id"
        ),
        "assets": "SELECT id, status, owner_citizen_id, owner_company_id FROM assets ORDER BY id",
        "titles": "SELECT asset_id, kind, buyer_company_id, transaction_id FROM asset_transfers ORDER BY asset_id, kind",
        "asset_resales": (
            "SELECT id, asset_id, seller_citizen_id, buyer_citizen_id, price, tax_bp, status, transaction_id "
            "FROM asset_resales ORDER BY id"
        ),
        "receipt_preferences": "SELECT citizen_id, locale FROM receipt_preferences ORDER BY citizen_id",
        "receipts": "SELECT transaction_id, citizen_id, locale, sent_at FROM private_receipts ORDER BY id",
        "maintenance": "SELECT guild_id, read_only FROM economy_maintenance ORDER BY guild_id",
        "maintenance_audit": "SELECT actor_discord_user_id, read_only, reason FROM economy_maintenance_audit ORDER BY id",
        "elections": "SELECT id, office, status, cancelled, replacement_of_id FROM elections ORDER BY id",
        "poll_sources": "SELECT source_url, election_id FROM election_poll_sources ORDER BY source_url",
        "protocols": (
            "SELECT election_id, round_number, source_url, tallies, winners, recorded_by_citizen_id, recorded_at, reason "
            "FROM election_protocols ORDER BY election_id, round_number"
        ),
        "certifications": (
            "SELECT election_id, certified_by_citizen_id, certified_at, reason "
            "FROM election_certifications ORDER BY election_id"
        ),
        "office_terms": (
            "SELECT election_id, office, seat, holder_citizen_id, starts_at, ends_at "
            "FROM public_offices ORDER BY election_id, seat"
        ),
        "cancellation_proposals": (
            "SELECT election_id, actor_citizen_id, occurred_at, reason "
            "FROM election_cancellation_proposals ORDER BY election_id"
        ),
        "cancellation_confirmations": (
            "SELECT election_id, actor_citizen_id, occurred_at, reason "
            "FROM election_cancellation_confirmations ORDER BY election_id"
        ),
        "impeachment_ballots": (
            "SELECT id, term_id, eligible_citizen_ids, quorum, opened_by_citizen_id, opened_at, request_key "
            "FROM impeachment_ballots ORDER BY id"
        ),
        "impeachment_protocols": (
            "SELECT impeachment_id, source_url, participants, votes_for, recorded_by_citizen_id, recorded_at, public_reason "
            "FROM impeachment_protocols ORDER BY impeachment_id"
        ),
        "impeachment_confirmations": (
            "SELECT term_id, impeachment_id, confirmed_by_citizen_id, confirmed_at, confirmation_reason "
            "FROM impeachment_confirmations ORDER BY term_id"
        ),
    }
    async with engine.connect() as connection:
        return {
            name: list((await connection.execute(text(query))).tuples()) for name, query in queries.items()
        }


@pytest.mark.asyncio
async def test_postgres_dump_restore_preserves_citizens_ledger_and_settled_transactions(pg_engine, tmp_path):
    bin_path = os.getenv("CIVITAS_TEST_PG_BIN")
    if not bin_path:
        pytest.skip("Set CIVITAS_TEST_PG_BIN to verify backup and restore with PostgreSQL client tools")
    suffix = ".exe" if os.name == "nt" else ""
    pg_dump = Path(bin_path) / f"pg_dump{suffix}"
    pg_restore = Path(bin_path) / f"pg_restore{suffix}"
    assert pg_dump.is_file() and pg_restore.is_file()

    uow = PostgresPrivateJobUnitOfWork(pg_engine)
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    funding = WalletFunding(starter_grant=250, treasury_genesis=8000)
    citizens = CitizensService(
        uow, guild_id=101, allowed_user_ids={201, 202, 203}, fund_verified=funding.fund_registered_citizen,
    )
    wallet = WalletService(uow, guild_id=101, authorize=citizens.authorize, funding=funding)
    jobs = JobService(
        uow, guild_id=101, authorize=citizens.authorize, staff_user_ids={201},
        rules=JobsRules(overall_weekly_limit=800, npc_weekly_limit=300, personal_weekly_limit=100, reward_min=10, reward_max=40),
        clock=lambda: now,
    )
    await citizens.register(101, 201)
    worker = (await citizens.register(101, 202)).citizen
    await citizens.register(101, 203)
    async with PostgresReceiptUnitOfWork(pg_engine)() as repository:
        await repository.set_receipt_preference(worker.guild_id, worker.id, "ru")
    task = await jobs.publish(
        101, 201, category=JobCategory.LOGISTICS, title="Test delivery",
        requirements="Deliver a fictional parcel", reward=30,
        expires_at=now + timedelta(hours=1),
    )
    await jobs.claim(101, 202, task.id)
    await jobs.submit(101, 202, task.id, "Fictional delivery complete")
    await jobs.review(101, 201, task.id, approve=True, reason="Verified fixture")
    assert await wallet.balance(101, 202) == 280
    market_rules = MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1)
    market = MarketService(
        uow, guild_id=101, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=market_rules, funding=funding,
    )
    company = await market.register_company(
        101, 201, name="Civitas Fixture", activity="Fictional deliveries", idempotency_key="restore-company"
    )
    offer = await market.publish_offer(
        101, 201, company_id=company.id, title="Fictional order", terms="Test escrow",
        price=200, delivery_hours=24, idempotency_key="restore-offer",
    )
    quote = await market.quote_order(101, 202, offer.id)
    order = await market.fund_order(101, 202, quote, idempotency_key="restore-order")
    await market.deliver_order(101, 201, order.id)
    sale = await market.accept_order(101, 202, order.id)
    withdrawal = await market.withdraw_offer(101, 201, offer.id, reason="Catalogue closed before backup")
    assert await market.company_balance(101, 201, company.id) == 180
    assert await wallet.balance(101, 202) == 80
    assets = AssetService(
        uow, guild_id=101, authorize=citizens.authorize, staff_user_ids={202}, funding=funding, resale_tax_bp=200,
    )
    asset = await assets.issue_asset(
        101, 202, kind=AssetKind.OFFICE, name="Fixture office", purpose="Fictional company workspace",
        price=70, reason="Approved test catalogue", idempotency_key="restore-asset-issue",
    )
    asset_quote = await assets.quote_asset(101, 201, asset.id, buyer_company_id=company.id)
    await assets.buy_asset(101, 201, asset_quote, idempotency_key="restore-asset-buy")
    assert await market.company_balance(101, 201, company.id) == 110
    assert [item.id for item in await assets.list_owned_assets(101, 201, company_id=company.id)] == [asset.id]
    treasury = TreasuryService(
        uow, guild_id=101, authorize=citizens.authorize, initiator_user_ids={202}, approver_user_ids={203},
        rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
        clock=lambda: now,
    )
    await treasury.activate_budget(101, 203, reason="Approved fixture budget")
    for key, amount in (("restore-contract-paid", 50), ("restore-contract-reserved", 30)):
        contract = await treasury.propose_contract(
            101, 202, recipient_company_id=company.id, title="Fictional public contract", terms="Verified work",
            amount=amount, deadline=now + timedelta(hours=1), reason="Fixture need",
            idempotency_key=key,
        )
        await treasury.reserve_contract(101, 203, contract.id, reason="Verified available budget")
        if amount == 50:
            await treasury.submit_contract(101, 201, contract.id, evidence="Fictional work completed")
            await treasury.pay_contract(101, 203, contract.id, reason="Accepted fixture result")
    assert await market.company_balance(101, 201, company.id) == 160

    private_jobs = PrivateJobService(uow, guild_id=101, authorize=citizens.authorize, arbiter_user_ids={203}, clock=lambda: now)
    paid_job = await private_jobs.publish_job(
        101, 201, company_id=company.id, title="Inspect a fixture", terms="Return a brief inspection",
        amount=40, deadline=now + timedelta(hours=1), reason="Fixture work", idempotency_key="restore-job-paid",
    )
    await private_jobs.claim_job(101, 202, paid_job.id)
    await private_jobs.submit_job(101, 202, paid_job.id, evidence="Inspection complete")
    await private_jobs.accept_job(101, 201, paid_job.id, reason="Inspection accepted")
    open_job = await private_jobs.publish_job(
        101, 201, company_id=company.id, title="Check an open fixture", terms="Return a checklist",
        amount=20, deadline=now + timedelta(hours=1), reason="Fixture work", idempotency_key="restore-job-open",
    )
    assert await wallet.balance(101, 202) == 120
    assert await market.company_balance(101, 201, company.id) == 100

    personal_asset = await assets.issue_asset(
        101, 202, kind=AssetKind.HOUSING, name="Fixture apartment", purpose="Fictional residence",
        price=50, reason="Approved test catalogue", idempotency_key="restore-personal-issue",
    )
    personal_quote = await assets.quote_asset(101, 202, personal_asset.id)
    await assets.buy_asset(101, 202, personal_quote, idempotency_key="restore-personal-buy")
    resale = await assets.create_resale_offer(
        101, 202, asset_id=personal_asset.id, buyer_user_id=203, price=200, idempotency_key="restore-resale",
    )
    completed_resale = await assets.buy_resale(101, 203, resale.id)
    assert await wallet.balance(101, 202) == 266
    assert await wallet.balance(101, 203) == 50

    election_now = now
    election_rules = ElectionRules(
        min_eligible=3, quorum_bp=4_000, quorum_min=2, parliament_seats=1,
        term_days=28, voting_hours=1, extension_hours=24, poll_hosts=frozenset({"poll.example.test"}),
    )

    def government(engine):
        return GovernmentService(
            PostgresGovernmentUnitOfWork(engine), guild_id=101, authorize=citizens.authorize,
            commissioner_user_ids={202, 203}, rules=election_rules, clock=lambda: election_now,
        )

    politics = government(pg_engine)
    former = await politics.open_election(
        101, 202, office=OfficeKind.PRESIDENT, candidate_user_ids=(201,),
        reason="Fixture former presidential term", idempotency_key="restore-former-president",
    )
    election_now = former.closes_at
    await politics.record_protocol(
        101, 202, former.id, source_url="https://poll.example.test/former-president",
        participants=2, tallies={201: 2}, reason="Recorded initial presidential poll",
    )
    await politics.certify_election(101, 203, former.id, reason="Independent first presidential check")
    election_now += timedelta(minutes=1)
    ballot = await politics.open_impeachment(101, 202, idempotency_key="restore-impeachment")
    await politics.record_impeachment(
        101, 202, ballot.id, source_url="https://poll.example.test/impeachment",
        participants=3, votes_for=2, reason="Recorded public confidence vote",
    )
    impeachment = await politics.confirm_impeachment(101, 203, ballot.id, reason="Verified confidence vote")
    assert impeachment.confirmed_at == election_now
    assert await politics.list_offices(101, 201) == ()
    president = await politics.open_election(
        101, 202, office=OfficeKind.PRESIDENT, candidate_user_ids=(201,),
        reason="Fixture presidential election", idempotency_key="restore-president",
    )
    election_now = president.closes_at
    await politics.record_protocol(
        101, 202, president.id, source_url="https://poll.example.test/president",
        participants=2, tallies={201: 2}, reason="Recorded presidential poll",
    )
    certified = await politics.certify_election(101, 203, president.id, reason="Independent presidential check")
    parliament = await politics.open_election(
        101, 202, office=OfficeKind.PARLIAMENT, candidate_user_ids=(201,),
        reason="Fixture parliament election", idempotency_key="restore-parliament",
    )
    election_now = parliament.closes_at
    cancelled_source = "https://poll.example.test/parliament"
    await politics.record_protocol(
        101, 202, parliament.id, source_url=cancelled_source, participants=2,
        tallies={201: 2}, reason="Recorded parliament poll",
    )
    await politics.propose_election_cancellation(101, 202, parliament.id, reason="External poll invalidated")
    cancellation = await politics.confirm_election_cancellation(
        101, 203, parliament.id, reason="Independent cancellation check",
    )

    async with PostgresMaintenanceUnitOfWork(pg_engine)() as repository:
        assert await repository.set_economy_read_only(
            101, True, actor_discord_user_id=901, reason="Fixture pause before backup", at=now,
        )

    expected = await _snapshot(pg_engine)
    assert [(row.offer_id, row.status) for row in expected["orders"]] == [(offer.id, "COMPLETED")]
    assert expected["offer_withdrawals"] == [(
        offer.id, offer.guild_id, company.owner_citizen_id, withdrawal.withdrawn_at, withdrawal.reason,
    )]
    assert expected["asset_resales"] == [(
        resale.id, personal_asset.id, resale.seller_citizen_id, resale.buyer_citizen_id,
        200, 200, "COMPLETED", completed_resale.transaction_id,
    )]
    assert expected["receipts"] and all(row.locale == "ru" for row in expected["receipts"])
    assert [row.read_only for row in expected["maintenance"]] == [True]
    assert [(row.status, row.amount) for row in expected["state_contracts"]] == [("PAID", 50), ("RESERVED", 30)]
    assert [(row.status, row.amount) for row in expected["private_jobs"]] == [("PAID", 40), ("OPEN", 20)]
    assert [(row.spent_total, row.reserved_total) for row in expected["budget"]] == [(80, 30)]
    assert [(row.code, row.spent, row.reserved) for row in expected["budget_articles"]] == [
        ("npc_work", 30, 0), ("state_contracts", 50, 30),
    ]
    async with pg_engine.connect() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
    database = f"civitas_restore_{uuid4().hex}"
    url = pg_engine.url
    credentials = dict(os.environ)
    credentials.pop("PGOPTIONS", None)
    if url.password is not None:
        credentials["PGPASSWORD"] = url.password
    options = ["--no-password"]
    for flag, value in (("--host", url.host), ("--port", url.port), ("--username", url.username)):
        if value is not None:
            options.extend((flag, str(value)))
    archive = tmp_path / "civitas.dump"
    await _run_postgres_tool(pg_dump, [
        *options, "--format=custom", "--file", str(archive), "--schema", schema, "--dbname", url.database,
    ], credentials)

    async with pg_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f"CREATE DATABASE {database}"))
        try:
            await _run_postgres_tool(pg_restore, [
                *options, "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
                "--dbname", database, str(archive),
            ], credentials)
            restored = create_db_engine(url.set(database=database))
            try:
                assert await _snapshot(restored) == expected
                restored_politics = government(restored)
                restored_impeachment = await restored_politics.get_public_impeachment(101, 201, ballot.id)
                assert restored_impeachment.confirmed_at == impeachment.confirmed_at
                assert await restored_politics.confirm_impeachment(
                    101, 203, ballot.id, reason="Verified confidence vote",
                ) == impeachment
                assert await restored_politics.get_public_result(101, 201, president.id) == certified
                (term,) = await restored_politics.list_offices(101, 201)
                assert (term.election_id, term.holder_discord_user_id) == (president.id, 201)
                async with PostgresGovernmentUnitOfWork(restored)() as repository:
                    assert await repository.has_active_office(
                        president.guild_id, term.holder_citizen_id, OfficeKind.PRESIDENT, election_now,
                    )
                assert (await restored_politics.get_election(101, 202, parliament.id)).cancellation == cancellation
                with pytest.raises(GovernmentError, match=GovernmentErrorReason.REPLACEMENT_REQUIRED):
                    await restored_politics.open_election(
                        101, 202, office=OfficeKind.PARLIAMENT, candidate_user_ids=(201,),
                        reason="Unlinked replacement", idempotency_key="restore-unlinked-parliament",
                    )
                replacement = await restored_politics.open_election(
                    101, 202, office=OfficeKind.PARLIAMENT, candidate_user_ids=(201,),
                    reason="Linked replacement", idempotency_key="restore-replacement-parliament",
                    replacement_of_id=parliament.id,
                )
                election_now = replacement.closes_at
                with pytest.raises(GovernmentError, match=GovernmentErrorReason.INVALID_SOURCE):
                    await restored_politics.record_protocol(
                        101, 202, replacement.id, source_url=cancelled_source, participants=2,
                        tallies={201: 2}, reason="Try previously used source",
                    )
                async with PostgresMaintenanceUnitOfWork(restored)() as repository:
                    assert await repository.get_economy_read_only(101)
                    assert await repository.set_economy_read_only(
                        101, False, actor_discord_user_id=901, reason="Recovered fixture", at=now + timedelta(days=7),
                    )
                restored_market = MarketService(
                    PostgresPrivateJobUnitOfWork(restored), guild_id=101, authorize=citizens.authorize,
                    arbiter_user_ids=(), rules=market_rules, funding=funding,
                )
                with pytest.raises(MarketError, match=MarketErrorReason.OFFER_WITHDRAWN):
                    await restored_market.quote_order(101, 202, offer.id)
                assert await restored_market.accept_order(101, 202, sale.id) == sale
                restored_assets = AssetService(
                    PostgresPrivateJobUnitOfWork(restored), guild_id=101, authorize=citizens.authorize,
                    staff_user_ids={202}, funding=funding, resale_tax_bp=200,
                )
                assert [
                    item.id for item in await restored_assets.list_owned_assets(101, 201, company_id=company.id)
                ] == [asset.id]
                assert [item.id for item in await restored_assets.list_owned_assets(101, 203)] == [personal_asset.id]
                assert await restored_assets.buy_resale(101, 203, resale.id) == completed_resale
                restored_treasury = TreasuryService(
                    PostgresPrivateJobUnitOfWork(restored), guild_id=101, authorize=citizens.authorize,
                    initiator_user_ids={202}, approver_user_ids={203},
                    rules=TreasuryRules(caps=BudgetCaps(800, 300, 100), contracts_weekly_limit=250),
                    clock=lambda: now + timedelta(days=7),
                )
                paid, reserved = expected["state_contracts"]
                await restored_treasury.pay_contract(101, 203, paid.id, reason="Accepted fixture result")
                await restored_treasury.cancel_contract(101, 203, reserved.id, reason="Released after restore")
                recovered = await _snapshot(restored)
                assert recovered["transactions"] == expected["transactions"]
                assert recovered["entries"] == expected["entries"]
                assert [(row.status, row.amount) for row in recovered["state_contracts"]] == [
                    ("PAID", 50), ("CANCELLED", 30),
                ]
                assert [row.reserved_total for row in recovered["budget"]] == [0]
                restored_jobs = PrivateJobService(
                    PostgresPrivateJobUnitOfWork(restored), guild_id=101,
                    authorize=citizens.authorize, arbiter_user_ids={203}, clock=lambda: now + timedelta(days=7),
                )
                await restored_jobs.accept_job(101, 201, paid_job.id, reason="Inspection accepted")
                await restored_jobs.cancel_job(101, 201, open_job.id, reason="Cancelled after restore")
                final = await _snapshot(restored)
                assert [(row.status, row.amount) for row in final["private_jobs"]] == [("PAID", 40), ("CANCELLED", 20)]
                assert final["transactions"][:-1] == expected["transactions"]
                assert final["transactions"][-1].kind == "PRIVATE_JOB_REFUND"
            finally:
                await restored.dispose()
        finally:
            await admin.execute(text(f"DROP DATABASE {database}"))
