import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.app.jobs import JobError, JobErrorReason, JobsRules, JobService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding
from civitas.domain.jobs import BudgetCaps, JobCategory, JobClaim, JobClaimStatus
from civitas.domain.ledger import AccountKind, TransactionKind
from civitas.domain.treasury import ContractStatus, StateContract
from civitas.infrastructure.db.jobs_models import BudgetArticleRow, BudgetPeriodRow
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow
from civitas.infrastructure.db.treasury import PostgresTreasuryUnitOfWork
from civitas.infrastructure.db.treasury_models import ContractBudgetApprovalRow, TreasuryAuditEventRow

pytestmark = pytest.mark.asyncio

GUILD = 100
PROPOSER = 11
APPROVER = 12
SECOND_APPROVER = 13
RECIPIENT = 21
NPC_REVIEWER = 31
NPC_WORKER = 32
OUTSIDER = 41
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


@dataclass
class Scenario:
    unit_of_work: PostgresTreasuryUnitOfWork
    citizens: CitizensService
    guild_id: int
    rules: TreasuryRules

    def treasury(self, now: datetime = NOW) -> TreasuryService:
        return TreasuryService(
            self.unit_of_work,
            guild_id=GUILD,
            authorize=self.citizens.authorize,
            initiator_user_ids={PROPOSER},
            # Proposer and recipient can approve other contracts, but not their own.
            approver_user_ids={PROPOSER, APPROVER, SECOND_APPROVER, RECIPIENT},
            rules=self.rules,
            clock=lambda: now,
        )

    def jobs(self) -> JobService:
        return JobService(
            self.unit_of_work,
            guild_id=GUILD,
            authorize=self.citizens.authorize,
            staff_user_ids={NPC_REVIEWER},
            rules=JobsRules(
                overall_weekly_limit=self.rules.caps.overall,
                npc_weekly_limit=self.rules.caps.npc,
                personal_weekly_limit=self.rules.caps.per_citizen,
                reward_min=1,
                reward_max=self.rules.caps.npc,
            ),
            clock=lambda: NOW,
        )


async def _scenario(
    engine: AsyncEngine, *, genesis: int = 8000, overall: int = 800, contracts: int = 250,
) -> Scenario:
    unit_of_work = PostgresTreasuryUnitOfWork(engine)
    users = {PROPOSER, APPROVER, SECOND_APPROVER, RECIPIENT, NPC_REVIEWER, NPC_WORKER, OUTSIDER}
    citizens = CitizensService(unit_of_work, guild_id=GUILD, allowed_user_ids=users)
    for user in sorted(users):
        await citizens.register(GUILD, user)
    async with unit_of_work() as repository:
        guild_id = await repository.ensure_guild(GUILD)
        await WalletFunding(starter_grant=0, treasury_genesis=genesis).initialize_guild(repository, guild_id)
    npc = min(overall, 300)
    rules = TreasuryRules(caps=BudgetCaps(overall, npc, npc), contracts_weekly_limit=contracts)
    return Scenario(unit_of_work, citizens, guild_id, rules)


async def _proposal(
    service: TreasuryService, amount: int, key: str, *, now: datetime = NOW,
) -> StateContract:
    return await service.propose_contract(
        GUILD, PROPOSER, recipient_user_id=RECIPIENT, title="Repair the town hall",
        terms="Repair the entrance", amount=amount, deadline=now + timedelta(days=2),
        reason="Keep the public building open", idempotency_key=key,
    )


async def _balances(scenario: Scenario) -> tuple[int, int]:
    recipient = await scenario.citizens.get_profile(GUILD, RECIPIENT)
    assert recipient is not None
    async with scenario.unit_of_work() as repository:
        treasury = await repository.ensure_account(scenario.guild_id, AccountKind.TREASURY)
        personal = await repository.ensure_account(scenario.guild_id, AccountKind.PERSONAL, recipient.id)
        return await repository.get_balance(treasury.id), await repository.get_balance(personal.id)


async def _payout_count(engine: AsyncEngine, kind: TransactionKind) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow).where(FinancialTransactionRow.kind == kind)
        )


async def test_unauthorized_spending_and_conflicts_leave_persistent_denial_audit(pg_engine: AsyncEngine) -> None:
    scenario = await _scenario(pg_engine)
    service = scenario.treasury()
    contract = await _proposal(service, 100, "audit-contract")

    # AT-13: a citizen who has no approval rights cannot make an encumbrance or payment.
    for operation, user, expected in (
        (service.reserve_contract, OUTSIDER, TreasuryErrorReason.APPROVER_REQUIRED),
        (service.reserve_contract, PROPOSER, TreasuryErrorReason.CONFLICT_OF_INTEREST),
        (service.reserve_contract, RECIPIENT, TreasuryErrorReason.CONFLICT_OF_INTEREST),
    ):
        with pytest.raises(TreasuryError) as failure:
            await operation(GUILD, user, contract.id, reason="Attempt to commit treasury funds")
        assert failure.value.reason is expected

    with pytest.raises(TreasuryError) as failure:
        await service.reserve_contract(GUILD, APPROVER, contract.id, reason="Reserve before activation")
    assert failure.value.reason is TreasuryErrorReason.BUDGET_NOT_ACTIVE
    await service.activate_budget(GUILD, APPROVER, reason="Approve the weekly article")
    await service.reserve_contract(GUILD, APPROVER, contract.id, reason="Reserve within budget")
    await service.submit_contract(GUILD, RECIPIENT, contract.id, evidence="Entrance repaired")
    with pytest.raises(TreasuryError) as failure:
        await service.preview_contract_action(GUILD, OUTSIDER, contract.id, action="pay")
    assert failure.value.reason is TreasuryErrorReason.APPROVER_REQUIRED
    with pytest.raises(TreasuryError) as failure:
        await service.pay_contract(GUILD, OUTSIDER, contract.id, reason="Attempt to spend public funds")
    assert failure.value.reason is TreasuryErrorReason.APPROVER_REQUIRED

    # The denial insert must survive rollback of the rejected financial action.
    async with pg_engine.connect() as connection:
        events = (await connection.execute(select(
            TreasuryAuditEventRow.actor_discord_user_id, TreasuryAuditEventRow.action,
            TreasuryAuditEventRow.reason, TreasuryAuditEventRow.contract_id,
        ).where(TreasuryAuditEventRow.discord_guild_id == GUILD).order_by(TreasuryAuditEventRow.id))).all()
    assert events == [
        (OUTSIDER, "contract.reserve", "approver_required", contract.id),
        (PROPOSER, "contract.reserve", "conflict_of_interest", contract.id),
        (RECIPIENT, "contract.reserve", "conflict_of_interest", contract.id),
        (APPROVER, "contract.reserve", "budget_not_active", contract.id),
        (OUTSIDER, "contract.pay", "approver_required", contract.id),
        (OUTSIDER, "contract.pay", "approver_required", contract.id),
    ]
    assert (await service.get_contract(GUILD, APPROVER, contract.id)).status is ContractStatus.SUBMITTED
    assert await _balances(scenario) == (8000, 0)
    assert await _payout_count(pg_engine, TransactionKind.STATE_CONTRACT_PAYOUT) == 0


@pytest.mark.parametrize(("genesis", "limit", "amount", "reason"), [
    (8000, 200, 400, TreasuryErrorReason.CONTRACTS_LIMIT),
    (100, 400, 150, TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS),
])
async def test_cash_and_contract_budget_each_limit_spending(
    pg_engine: AsyncEngine, genesis: int, limit: int, amount: int, reason: TreasuryErrorReason,
) -> None:
    scenario = await _scenario(pg_engine, genesis=genesis, contracts=limit)
    service = scenario.treasury()
    approved = await service.activate_budget(GUILD, APPROVER, reason="Approved budget")
    contract = await _proposal(service, amount, "over-limit")

    # AT-14: more cash than the article cap does not increase the spending limit.
    with pytest.raises(TreasuryError) as failure:
        await service.reserve_contract(GUILD, APPROVER, contract.id, reason="Try to reserve")
    assert failure.value.reason is reason
    assert await service.current_budget(GUILD, APPROVER) == approved
    assert await service.get_contract(GUILD, APPROVER, contract.id) == contract
    assert await _balances(scenario) == (genesis, 0)


@pytest.mark.parametrize(("genesis", "overall", "treasury_error", "npc_error"), [
    (1000, 100, TreasuryErrorReason.OVERALL_LIMIT, JobErrorReason.OVERALL_LIMIT),
    (100, 800, TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS, JobErrorReason.TREASURY_INSUFFICIENT_FUNDS),
])
async def test_npc_payout_and_contract_reservation_share_limits_under_concurrency(
    pg_engine: AsyncEngine, genesis: int, overall: int,
    treasury_error: TreasuryErrorReason, npc_error: JobErrorReason,
) -> None:
    scenario = await _scenario(pg_engine, genesis=genesis, overall=overall, contracts=min(overall, 250))
    service, jobs = scenario.treasury(), scenario.jobs()
    await service.activate_budget(GUILD, APPROVER, reason="Approved budget")
    contract = await _proposal(service, 80, "mixed-articles")
    task = await jobs.publish(
        GUILD, NPC_REVIEWER, category=JobCategory.SERVICES, title="Clean the square", requirements="Remove litter",
        reward=50, expires_at=NOW + timedelta(days=1),
    )
    await jobs.claim(GUILD, NPC_WORKER, task.id)
    await jobs.submit(GUILD, NPC_WORKER, task.id, "Square cleaned")

    reserved, paid = await asyncio.wait_for(asyncio.gather(
        service.reserve_contract(GUILD, APPROVER, contract.id, reason="Reserve contract"),
        jobs.review(GUILD, NPC_REVIEWER, task.id, approve=True, reason="Verified work"),
        return_exceptions=True,
    ), timeout=10)

    budget = await service.current_budget(GUILD, APPROVER)
    assert budget is not None
    if isinstance(reserved, StateContract):
        assert reserved.status is ContractStatus.RESERVED
        assert isinstance(paid, JobError) and paid.reason is npc_error
        assert (budget.spent_total, budget.reserved_total) == (0, 80)
        assert await _balances(scenario) == (genesis, 0)
        assert await _payout_count(pg_engine, TransactionKind.NPC_PAYOUT) == 0
    else:
        assert isinstance(reserved, TreasuryError) and reserved.reason is treasury_error
        assert isinstance(paid, JobClaim) and paid.status is JobClaimStatus.PAID
        assert (budget.spent_total, budget.reserved_total) == (50, 0)
        assert await _balances(scenario) == (genesis - 50, 0)
        assert await _payout_count(pg_engine, TransactionKind.NPC_PAYOUT) == 1


async def test_two_concurrent_acceptances_and_a_retry_post_exactly_one_payment(pg_engine: AsyncEngine) -> None:
    scenario = await _scenario(pg_engine)
    service = scenario.treasury()
    approved = await service.activate_budget(GUILD, APPROVER, reason="Approved weekly cap")
    repeated = await service.activate_budget(GUILD, SECOND_APPROVER, reason="Repeat activation")
    assert repeated == approved
    contract = await _proposal(service, 200, "paid-once")
    assert await _proposal(service, 200, "paid-once") == contract
    await service.reserve_contract(GUILD, APPROVER, contract.id, reason="Reserve for repairs")
    await service.submit_contract(GUILD, RECIPIENT, contract.id, evidence="Entrance repaired")

    first, second = await asyncio.wait_for(asyncio.gather(
        service.pay_contract(GUILD, APPROVER, contract.id, reason="Accepted work"),
        service.pay_contract(GUILD, SECOND_APPROVER, contract.id, reason="Accepted concurrently"),
    ), timeout=10)
    assert first == second and first.status is ContractStatus.PAID
    assert first.payout_transaction_id is not None
    assert await scenario.treasury().pay_contract(GUILD, APPROVER, contract.id, reason="Retry") == first
    assert await _payout_count(pg_engine, TransactionKind.STATE_CONTRACT_PAYOUT) == 1
    assert await _balances(scenario) == (7800, 200)
    budget = await service.current_budget(GUILD, APPROVER)
    assert budget is not None
    assert (budget.spent_total, budget.reserved_total, budget.contracts_spent, budget.contracts_reserved) == (
        200, 0, 200, 0,
    )
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(ContractBudgetApprovalRow)) == 1


async def test_old_reservations_protect_cash_and_are_released_or_spent_in_original_week(pg_engine: AsyncEngine) -> None:
    scenario = await _scenario(pg_engine, genesis=120)
    old = scenario.treasury()
    old_budget = await old.activate_budget(GUILD, APPROVER, reason="Week one budget")
    payable = await _proposal(old, 80, "old-payable")
    cancellable = await _proposal(old, 30, "old-cancellable")
    await old.reserve_contract(GUILD, APPROVER, payable.id, reason="First reservation")
    await old.reserve_contract(GUILD, APPROVER, cancellable.id, reason="Second reservation")
    await old.submit_contract(GUILD, RECIPIENT, payable.id, evidence="Work finished on time")

    next_week = NOW + timedelta(days=7)
    current = scenario.treasury(next_week)
    new_budget = await current.activate_budget(GUILD, APPROVER, reason="Week two budget")
    assert old_budget.period_id != new_budget.period_id
    fresh = await _proposal(current, 11, "new-week", now=next_week)
    with pytest.raises(TreasuryError) as failure:
        await current.reserve_contract(GUILD, APPROVER, fresh.id, reason="Still encumbered")
    assert failure.value.reason is TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS

    paid = await current.pay_contract(GUILD, APPROVER, payable.id, reason="Accepted after rollover")
    assert paid.status is ContractStatus.PAID and paid.budget_period_id == old_budget.period_id
    with pytest.raises(TreasuryError) as failure:
        await current.reserve_contract(GUILD, APPROVER, fresh.id, reason="Second reserve attempt")
    assert failure.value.reason is TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS

    cancelled = await current.cancel_contract(GUILD, APPROVER, cancellable.id, reason="Expired unused reserve")
    assert cancelled.status is ContractStatus.CANCELLED and cancelled.budget_period_id == old_budget.period_id
    reserved = await current.reserve_contract(GUILD, APPROVER, fresh.id, reason="Reserve released")
    assert reserved.status is ContractStatus.RESERVED and reserved.budget_period_id == new_budget.period_id
    assert await _balances(scenario) == (40, 80)

    async with pg_engine.connect() as connection:
        periods = (await connection.execute(select(
            BudgetPeriodRow.id, BudgetPeriodRow.spent_total, BudgetPeriodRow.reserved_total,
        ).order_by(BudgetPeriodRow.start_at))).all()
        articles = (await connection.execute(select(
            BudgetArticleRow.period_id, BudgetArticleRow.spent, BudgetArticleRow.reserved,
        ).where(BudgetArticleRow.code == "state_contracts").order_by(BudgetArticleRow.period_id))).all()
    assert periods == articles == [(old_budget.period_id, 80, 0), (new_budget.period_id, 0, 11)]
