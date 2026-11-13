import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.app.market import MarketRules, MarketService
from civitas.app.private_jobs import PrivateJobError, PrivateJobErrorReason, PrivateJobService
from civitas.app.wallet import WalletFunding
from civitas.domain.ledger import AccountKind, LedgerError, LedgerErrorReason, TransactionKind
from civitas.domain.market import Company
from civitas.domain.private_jobs import PrivateJob, PrivateJobStatus
from civitas.infrastructure.db.ledger_models import FinancialAccountRow, FinancialTransactionRow
from civitas.infrastructure.db.private_job_models import PrivateJobRow
from civitas.infrastructure.db.private_jobs import PostgresPrivateJobUnitOfWork

pytestmark = pytest.mark.asyncio

GUILD = 100
EMPLOYER, WORKER, OTHER_WORKER, ARBITER, OUTSIDER = 11, 12, 13, 14, 15
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(days=1)


@dataclass(frozen=True)
class Scenario:
    unit_of_work: PostgresPrivateJobUnitOfWork
    citizens: CitizensService
    company: Company

    def jobs(self, *, now: datetime = NOW) -> PrivateJobService:
        return PrivateJobService(
            self.unit_of_work, guild_id=GUILD, authorize=self.citizens.authorize,
            arbiter_user_ids={EMPLOYER, ARBITER}, clock=lambda: now,
        )


@pytest_asyncio.fixture
async def scenario(pg_engine: AsyncEngine) -> Scenario:
    unit_of_work = PostgresPrivateJobUnitOfWork(pg_engine)
    funding = WalletFunding(starter_grant=1000, treasury_genesis=8000)
    citizens = CitizensService(
        unit_of_work, guild_id=GUILD,
        allowed_user_ids={EMPLOYER, WORKER, OTHER_WORKER, ARBITER, OUTSIDER},
        fund_verified=funding.fund_registered_citizen,
    )
    for user_id in (EMPLOYER, WORKER, OTHER_WORKER, ARBITER, OUTSIDER):
        await citizens.register(GUILD, user_id)

    market = MarketService(
        unit_of_work, guild_id=GUILD, authorize=citizens.authorize, arbiter_user_ids=(),
        rules=MarketRules(registration_fee=150, sales_tax_bp=1000, company_limit=1),
        funding=funding, clock=lambda: NOW,
    )
    company = await market.register_company(
        GUILD, EMPLOYER, name="Civitas Studio", activity="Design", idempotency_key="company",
    )
    offer = await market.publish_offer(
        GUILD, EMPLOYER, company_id=company.id, title="Poster", terms="Deliver two sketches",
        price=200, delivery_hours=24, idempotency_key="offer",
    )
    quote = await market.quote_order(GUILD, OUTSIDER, offer.id)
    order = await market.fund_order(GUILD, OUTSIDER, quote, idempotency_key="company-income")
    await market.deliver_order(GUILD, EMPLOYER, order.id)
    await market.accept_order(GUILD, OUTSIDER, order.id)
    assert await market.company_balance(GUILD, EMPLOYER, company.id) == 180
    return Scenario(unit_of_work, citizens, company)


async def _publish(
    scenario: Scenario, *, key: str, amount: int = 70, actor: int = EMPLOYER,
) -> PrivateJob:
    return await scenario.jobs().publish_job(
        GUILD, actor, company_id=scenario.company.id, title="Review the poster",
        terms="Provide five comments", amount=amount, deadline=DEADLINE,
        reason="Company design review", idempotency_key=key,
    )


async def _balances(scenario: Scenario, job: PrivateJob, worker: int) -> tuple[int, int, int, int]:
    citizen = await scenario.citizens.get_profile(GUILD, worker)
    assert citizen is not None
    async with scenario.unit_of_work() as repository:
        company = await repository.ensure_company_account(job.guild_id, job.company_id)
        escrow = await repository.ensure_private_job_escrow(job.guild_id, job.id)
        personal = await repository.ensure_account(job.guild_id, AccountKind.PERSONAL, citizen.id)
        treasury = await repository.ensure_account(job.guild_id, AccountKind.TREASURY)
        return tuple([await repository.get_balance(account.id) for account in (company, escrow, personal, treasury)])


async def _posting_count(engine: AsyncEngine, kind: TransactionKind) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow).where(FinancialTransactionRow.kind == kind)
        )


async def test_publication_reserves_once_and_insufficient_company_funds_roll_back(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    with pytest.raises(PrivateJobError) as foreign_company:
        await _publish(scenario, key="foreign", actor=WORKER)
    assert foreign_company.value.reason is PrivateJobErrorReason.COMPANY_NOT_OWNED

    first, repeat = await asyncio.wait_for(
        asyncio.gather(_publish(scenario, key="fund", amount=100), _publish(scenario, key="fund", amount=100)),
        timeout=10,
    )
    assert first == repeat and first.status is PrivateJobStatus.OPEN
    with pytest.raises(PrivateJobError) as reused_key:
        await _publish(scenario, key="fund", amount=101)
    assert reused_key.value.reason is PrivateJobErrorReason.INVALID_REQUEST_KEY
    with pytest.raises(LedgerError) as unfunded:
        await _publish(scenario, key="unfunded", amount=81)
    assert unfunded.value.reason is LedgerErrorReason.INSUFFICIENT_FUNDS

    assert await scenario.jobs().list_jobs(GUILD, WORKER) == (first,)
    assert await _balances(scenario, first, WORKER) == (80, 100, 1000, 8170)
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_FUND) == 1
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(PrivateJobRow)) == 1
        assert await connection.scalar(
            select(func.count()).select_from(FinancialAccountRow).where(FinancialAccountRow.private_job_id.is_not(None))
        ) == 1


async def test_competing_claims_and_repeated_acceptance_pay_only_the_winner(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    jobs = scenario.jobs()
    job = await _publish(scenario, key="competition")
    with pytest.raises(PrivateJobError) as employer_claim:
        await jobs.claim_job(GUILD, EMPLOYER, job.id)
    assert employer_claim.value.reason is PrivateJobErrorReason.CONFLICT_OF_INTEREST

    workers = (WORKER, OTHER_WORKER)
    outcomes = await asyncio.wait_for(asyncio.gather(
        *(jobs.claim_job(GUILD, worker, job.id) for worker in workers), return_exceptions=True,
    ), timeout=10)
    winners = [(worker, result) for worker, result in zip(workers, outcomes, strict=True) if isinstance(result, PrivateJob)]
    assert len(winners) == 1
    assert [error.reason for error in outcomes if isinstance(error, PrivateJobError)] == [
        PrivateJobErrorReason.JOB_ALREADY_CLAIMED,
    ]
    worker, claimed = winners[0]
    loser = OTHER_WORKER if worker == WORKER else WORKER
    assert claimed.worker_discord_user_id == worker
    assert await jobs.claim_job(GUILD, worker, job.id) == claimed
    assert await jobs.list_jobs(GUILD, loser) == ()
    await jobs.submit_job(GUILD, worker, job.id, evidence="Five comments delivered")

    restarted = scenario.jobs(now=DEADLINE)
    first, repeat = await asyncio.wait_for(asyncio.gather(
        jobs.accept_job(GUILD, EMPLOYER, job.id, reason="Reviewed the work"),
        restarted.accept_job(GUILD, EMPLOYER, job.id, reason="Reviewed the work"),
    ), timeout=10)
    assert first == repeat and first.status is PrivateJobStatus.PAID
    assert first.payout_transaction_id is not None
    assert await _balances(scenario, job, worker) == (110, 0, 1070, 8170)
    assert await _balances(scenario, job, loser) == (110, 0, 1000, 8170)
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_PAY) == 1
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_REFUND) == 0


async def test_dispute_evidence_stays_private_and_independent_refund_restores_company_cash(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    jobs = scenario.jobs()
    job = await _publish(scenario, key="dispute")
    await jobs.claim_job(GUILD, WORKER, job.id)
    evidence = "Private work proof"
    await jobs.submit_job(GUILD, WORKER, job.id, evidence=evidence)

    for user in (OUTSIDER, ARBITER):
        with pytest.raises(PrivateJobError) as private:
            await jobs.get_job(GUILD, user, job.id)
        assert private.value.reason is PrivateJobErrorReason.NOT_JOB_PARTY
    disputed = await jobs.dispute_job(GUILD, WORKER, job.id, reason="Disputed review")
    assert disputed.status is PrivateJobStatus.DISPUTED
    assert await jobs.list_my_jobs(GUILD, ARBITER) == (disputed,)
    assert (await jobs.get_job(GUILD, ARBITER, job.id)).evidence == evidence
    with pytest.raises(PrivateJobError) as private:
        await jobs.get_job(GUILD, OUTSIDER, job.id)
    assert private.value.reason is PrivateJobErrorReason.NOT_JOB_PARTY
    with pytest.raises(PrivateJobError) as conflict:
        await jobs.resolve_job(GUILD, EMPLOYER, job.id, pay=False, reason="Self-interested refund")
    assert conflict.value.reason is PrivateJobErrorReason.CONFLICT_OF_INTEREST
    with pytest.raises(PrivateJobError) as no_arbiter:
        await jobs.resolve_job(GUILD, OUTSIDER, job.id, pay=False, reason="Unauthorised refund")
    assert no_arbiter.value.reason is PrivateJobErrorReason.ARBITER_REQUIRED

    closed = await jobs.resolve_job(GUILD, ARBITER, job.id, pay=False, reason="Independent refund")
    assert closed.status is PrivateJobStatus.CANCELLED and closed.payout_transaction_id is not None
    assert await jobs.resolve_job(GUILD, ARBITER, job.id, pay=False, reason="Independent refund") == closed
    assert await _balances(scenario, job, WORKER) == (180, 0, 1000, 8170)
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_PAY) == 0
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_REFUND) == 1


async def test_same_worker_claim_retry_keeps_full_claimant_identity(scenario: Scenario) -> None:
    jobs = scenario.jobs()
    job = await _publish(scenario, key="same-worker")
    first, repeat = await asyncio.wait_for(asyncio.gather(
        jobs.claim_job(GUILD, WORKER, job.id), jobs.claim_job(GUILD, WORKER, job.id),
    ), timeout=10)
    assert first == repeat
    assert repeat.status is PrivateJobStatus.CLAIMED and repeat.worker_discord_user_id == WORKER


async def test_overdue_submission_can_be_recovered_by_independent_arbiter(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    jobs = scenario.jobs()
    job = await _publish(scenario, key="overdue-dispute")
    await jobs.claim_job(GUILD, WORKER, job.id)
    await jobs.submit_job(GUILD, WORKER, job.id, evidence="Unreviewed proof")
    with pytest.raises(PrivateJobError) as early:
        await jobs.dispute_job(GUILD, ARBITER, job.id, reason="Still within the deadline")
    assert early.value.reason is PrivateJobErrorReason.NOT_JOB_PARTY

    at_deadline = scenario.jobs(now=DEADLINE)
    assert [item.id for item in await at_deadline.list_my_jobs(GUILD, ARBITER)] == [job.id]
    assert (await at_deadline.get_job(GUILD, ARBITER, job.id)).evidence == "Unreviewed proof"
    assert await at_deadline.list_my_jobs(GUILD, OUTSIDER) == ()
    await at_deadline.dispute_job(GUILD, ARBITER, job.id, reason="Review overdue work")

    outcomes = await asyncio.wait_for(asyncio.gather(
        at_deadline.resolve_job(GUILD, ARBITER, job.id, pay=True, reason="Proof accepted"),
        at_deadline.resolve_job(GUILD, ARBITER, job.id, pay=False, reason="Proof rejected"),
        return_exceptions=True,
    ), timeout=10)
    assert len([result for result in outcomes if isinstance(result, PrivateJob)]) == 1
    assert [error.reason for error in outcomes if isinstance(error, PrivateJobError)] == [
        PrivateJobErrorReason.INVALID_STATUS,
    ]
    pay_count = await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_PAY)
    refund_count = await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_REFUND)
    assert pay_count + refund_count == 1
    assert await _balances(scenario, job, WORKER) == (110 + 70 * refund_count, 0, 1000 + 70 * pay_count, 8170)


async def test_expired_job_blocks_claim_or_submission_but_allows_repeated_voluntary_cancel(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    jobs = scenario.jobs()
    claimed = await _publish(scenario, key="claimed")
    open_job = await _publish(scenario, key="open")
    await jobs.claim_job(GUILD, WORKER, claimed.id)
    at_deadline = scenario.jobs(now=DEADLINE)
    assert await at_deadline.list_jobs(GUILD, OTHER_WORKER) == ()

    with pytest.raises(PrivateJobError) as expired_claim:
        await at_deadline.claim_job(GUILD, OTHER_WORKER, open_job.id)
    assert expired_claim.value.reason is PrivateJobErrorReason.DEADLINE_PASSED
    with pytest.raises(PrivateJobError) as expired_submission:
        await at_deadline.submit_job(GUILD, WORKER, claimed.id, evidence="Late proof")
    assert expired_submission.value.reason is PrivateJobErrorReason.DEADLINE_PASSED
    first, repeat = await asyncio.wait_for(asyncio.gather(
        at_deadline.cancel_job(GUILD, WORKER, claimed.id, reason="Voluntary cancellation"),
        at_deadline.cancel_job(GUILD, WORKER, claimed.id, reason="Voluntary cancellation"),
    ), timeout=10)
    assert first == repeat and first.status is PrivateJobStatus.CANCELLED
    assert await _balances(scenario, claimed, WORKER) == (110, 0, 1000, 8170)
    assert await _balances(scenario, open_job, OTHER_WORKER) == (110, 70, 1000, 8170)
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_REFUND) == 1

    with pytest.raises(PrivateJobError) as stranger:
        await at_deadline.cancel_job(GUILD, OUTSIDER, open_job.id, reason="Cannot cancel another company's job")
    assert stranger.value.reason is PrivateJobErrorReason.NOT_JOB_PARTY
    assert [item.id for item in await at_deadline.list_my_jobs(GUILD, ARBITER)] == [open_job.id]
    recovered = await at_deadline.cancel_job(GUILD, ARBITER, open_job.id, reason="Release expired reserve")
    assert recovered.status is PrivateJobStatus.CANCELLED
    assert await _balances(scenario, open_job, OTHER_WORKER) == (180, 0, 1000, 8170)
    assert await _posting_count(pg_engine, TransactionKind.PRIVATE_JOB_REFUND) == 2


async def test_funded_job_terms_cannot_be_changed_at_database_level(
    pg_engine: AsyncEngine, scenario: Scenario,
) -> None:
    job = await _publish(scenario, key="immutable")
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(PrivateJobRow).where(PrivateJobRow.id == job.id).values(amount=job.amount + 1)
            )
    assert await scenario.jobs().get_job(GUILD, EMPLOYER, job.id) == job
    assert await _balances(scenario, job, WORKER) == (110, 70, 1000, 8170)
