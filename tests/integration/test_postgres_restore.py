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
from civitas.app.jobs import JobService, JobsRules
from civitas.app.market import MarketRules, MarketService
from civitas.app.private_jobs import PrivateJobService
from civitas.app.treasury import TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.assets import AssetKind
from civitas.domain.jobs import BudgetCaps, JobCategory
from civitas.infrastructure.db import create_db_engine
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
        "orders": "SELECT id, status, price, tax_bp, buyer_account_id FROM orders ORDER BY id",
        "assets": "SELECT id, status, owner_citizen_id, owner_company_id FROM assets ORDER BY id",
        "titles": "SELECT asset_id, kind, buyer_company_id, transaction_id FROM asset_transfers ORDER BY asset_id, kind",
        "receipt_preferences": "SELECT citizen_id, locale FROM receipt_preferences ORDER BY citizen_id",
        "receipts": "SELECT transaction_id, citizen_id, locale, sent_at FROM private_receipts ORDER BY id",
        "maintenance": "SELECT guild_id, read_only FROM economy_maintenance ORDER BY guild_id",
        "maintenance_audit": "SELECT actor_discord_user_id, read_only, reason FROM economy_maintenance_audit ORDER BY id",
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
    market = MarketService(
        uow, guild_id=101, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1),
        funding=funding,
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
    await market.accept_order(101, 202, order.id)
    assert await market.company_balance(101, 201, company.id) == 180
    assert await wallet.balance(101, 202) == 80
    assets = AssetService(
        uow, guild_id=101, authorize=citizens.authorize, staff_user_ids={202}, funding=funding,
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

    async with PostgresMaintenanceUnitOfWork(pg_engine)() as repository:
        assert await repository.set_economy_read_only(
            101, True, actor_discord_user_id=901, reason="Fixture pause before backup", at=now,
        )

    expected = await _snapshot(pg_engine)
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
                async with PostgresMaintenanceUnitOfWork(restored)() as repository:
                    assert await repository.get_economy_read_only(101)
                    assert await repository.set_economy_read_only(
                        101, False, actor_discord_user_id=901, reason="Recovered fixture", at=now + timedelta(days=7),
                    )
                restored_assets = AssetService(
                    PostgresPrivateJobUnitOfWork(restored), guild_id=101, authorize=citizens.authorize,
                    staff_user_ids={202}, funding=funding,
                )
                assert [
                    item.id for item in await restored_assets.list_owned_assets(101, 201, company_id=company.id)
                ] == [asset.id]
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
