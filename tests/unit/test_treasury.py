from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from civitas.app.citizens import CitizensService
from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryRepository, TreasuryRules, TreasuryService
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import OfficeKind
from civitas.domain.jobs import BudgetCaps, BudgetPeriod
from civitas.domain.ledger import Account, AccountKind, PostResult, TransactionKind
from civitas.domain.market import Company
from civitas.domain.treasury import ContractBudget, ContractStatus, StateContract

DISCORD_GUILD = 101
GUILD = 1
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
START = datetime(2026, 11, 9, tzinfo=UTC)
END = START + timedelta(days=7)
PROPOSER = Citizen(1, GUILD, 201, CitizenStatus.VERIFIED, NOW, NOW)
RECIPIENT = Citizen(2, GUILD, 202, CitizenStatus.VERIFIED, NOW, NOW)
APPROVER = Citizen(3, GUILD, 203, CitizenStatus.VERIFIED, NOW, NOW)
OUTSIDER = Citizen(4, GUILD, 204, CitizenStatus.VERIFIED, NOW, NOW)
COMPANY = Company(10, GUILD, RECIPIENT.id, RECIPIENT.discord_user_id, "Workshop", "workshop", "Tools", 150, "company", NOW)
TREASURY = Account(11, GUILD, AccountKind.TREASURY, None)
PERSONAL = Account(12, GUILD, AccountKind.PERSONAL, RECIPIENT.id)
CORPORATE = Account(13, GUILD, AccountKind.COMPANY, None, company_id=COMPANY.id)
CAPS = BudgetCaps(800, 300, 100)
PERIOD = BudgetPeriod(id=15, guild_id=GUILD, start_at=START, end_at=END, caps=CAPS, spent_total=100, spent_npc=100)
BUDGET = ContractBudget(15, GUILD, START, END, 800, 100, 0, 250, 0, 0, NOW, APPROVER.id, "Budget approved")
DRAFT = StateContract(
    id=30, guild_id=GUILD, title="Bridge repair", terms="Repair the fictional bridge", amount=200,
    deadline=NOW + timedelta(hours=1), recipient_citizen_id=RECIPIENT.id, recipient_company_id=None,
    proposer_citizen_id=PROPOSER.id, proposal_reason="Urgent work", request_key="interaction-1", created_at=NOW,
    status=ContractStatus.PROPOSED, recipient_discord_user_id=RECIPIENT.discord_user_id,
)
RESERVED = replace(
    DRAFT, status=ContractStatus.RESERVED, budget_period_id=BUDGET.period_id,
    reserved_by_citizen_id=APPROVER.id, reserved_at=NOW, reserve_reason="Award",
)
SUBMITTED = replace(RESERVED, status=ContractStatus.SUBMITTED, submitted_at=NOW, evidence="Work completed")


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=TreasuryRepository)

    def get_citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        return tuple(c for c in (PROPOSER, RECIPIENT, APPROVER, OUTSIDER) if c.discord_user_id in user_ids)

    repo.get_citizens.side_effect = get_citizens
    repo.get_contract.return_value = DRAFT
    repo.get_contract_by_key.return_value = None
    repo.get_contract_budget.return_value = BUDGET
    repo.get_contract_budget_by_period.return_value = BUDGET
    repo.get_or_create_budget_period.return_value = PERIOD
    repo.activate_contract_budget.return_value = BUDGET
    repo.lock_treasury_available.return_value = 1_000
    repo.get_company.return_value = COMPANY
    repo.ensure_account.side_effect = lambda guild_id, kind, citizen_id=None: {
        AccountKind.PERSONAL: PERSONAL, AccountKind.TREASURY: TREASURY,
    }[kind]
    repo.ensure_company_account.return_value = CORPORATE
    repo.post.return_value = PostResult(99, True)

    def save_contract(contract: StateContract) -> None:
        repo.get_contract.return_value = contract
        repo.get_contract_by_key.return_value = contract

    def create_contract(**values: object) -> StateContract:
        contract = StateContract(id=DRAFT.id, status=ContractStatus.PROPOSED, **values)
        save_contract(contract)
        return contract

    repo.create_contract.side_effect = create_contract
    repo.update_contract.side_effect = save_contract
    return repo


@pytest.fixture
def service_factory(repository: AsyncMock):
    def create(*, approvers=(APPROVER.discord_user_id,), now=NOW, transactions=None, elected=False) -> TreasuryService:
        @asynccontextmanager
        async def unit_of_work():
            if transactions is not None:
                transactions.append("begin")
            try:
                yield repository
            except Exception:
                if transactions is not None:
                    transactions.append("rollback")
                raise
            else:
                if transactions is not None:
                    transactions.append("commit")

        citizens = CitizensService(
            lambda: pytest.fail("Authorization must not open a transaction"), guild_id=DISCORD_GUILD,
            allowed_user_ids=[c.discord_user_id for c in (PROPOSER, RECIPIENT, APPROVER, OUTSIDER)],
        )
        return TreasuryService(
            unit_of_work, guild_id=DISCORD_GUILD, authorize=citizens.authorize,
            initiator_user_ids=[PROPOSER.discord_user_id], approver_user_ids=approvers,
            rules=TreasuryRules(caps=CAPS, contracts_weekly_limit=250), clock=lambda: now,
            elected_authority=elected,
        )

    return create


def proposal(*, company_id=None) -> dict:
    return {
        "recipient_user_id": RECIPIENT.discord_user_id if company_id is None else None,
        "recipient_company_id": company_id,
        "title": DRAFT.title,
        "terms": DRAFT.terms,
        "amount": DRAFT.amount,
        "deadline": DRAFT.deadline,
        "reason": DRAFT.proposal_reason,
        "idempotency_key": DRAFT.request_key,
    }


@pytest.mark.asyncio
async def test_elected_treasury_uses_current_offices_instead_of_manual_lists(repository, service_factory):
    service = service_factory(elected=True)
    repository.has_active_office.side_effect = lambda guild, citizen, office, at: (
        guild == GUILD and at == NOW and (citizen, office) in {
            (OUTSIDER.id, OfficeKind.PRESIDENT), (APPROVER.id, OfficeKind.PARLIAMENT),
        }
    )

    with pytest.raises(TreasuryError, match=TreasuryErrorReason.INITIATOR_REQUIRED):
        await service.propose_contract(DISCORD_GUILD, PROPOSER.discord_user_id, **proposal())
    created = await service.propose_contract(DISCORD_GUILD, OUTSIDER.discord_user_id, **proposal())
    assert created.proposer_citizen_id == OUTSIDER.id
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await service.activate_budget(DISCORD_GUILD, OUTSIDER.discord_user_id, reason="Not a deputy")
    assert await service.activate_budget(DISCORD_GUILD, APPROVER.discord_user_id, reason="Approved") == BUDGET

    repository.has_active_office.side_effect = None
    repository.has_active_office.return_value = False
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await service.preview_contract_action(DISCORD_GUILD, APPROVER.discord_user_id, created.id, action="reserve")
    repository.append_contract_denial.assert_awaited()


@pytest.mark.asyncio
async def test_budget_activation_reuses_existing_snapshot(repository, service_factory):
    service = service_factory()
    repository.get_contract_budget.side_effect = [None, BUDGET]

    first = await service.activate_budget(DISCORD_GUILD, APPROVER.discord_user_id, reason="Budget approved")
    second = await service.activate_budget(DISCORD_GUILD, APPROVER.discord_user_id, reason="Repeated request")

    assert first == second == BUDGET
    repository.get_or_create_budget_period.assert_awaited_once_with(
        GUILD, START, END, NOW, CAPS, week_start_date=date(2026, 11, 9)
    )
    repository.activate_contract_budget.assert_awaited_once_with(PERIOD, 250, APPROVER.id, NOW, "Budget approved")


@pytest.mark.parametrize("company_id", [None, COMPANY.id])
@pytest.mark.asyncio
async def test_proposal_has_no_reserve_and_same_request_returns_draft(repository, service_factory, company_id):
    service = service_factory()
    kwargs = proposal(company_id=company_id)
    first = await service.propose_contract(DISCORD_GUILD, PROPOSER.discord_user_id, **kwargs)
    repeated = await service.propose_contract(DISCORD_GUILD, PROPOSER.discord_user_id, **kwargs)

    assert first == repeated
    assert first.status is ContractStatus.PROPOSED
    assert first.recipient_citizen_id == RECIPIENT.id
    assert first.recipient_company_id == company_id
    repository.create_contract.assert_awaited_once()
    repository.record_contract_reservation.assert_not_awaited()
    repository.post.assert_not_awaited()
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.INVALID_REQUEST_KEY):
        await service.propose_contract(DISCORD_GUILD, PROPOSER.discord_user_id, **(kwargs | {"amount": DRAFT.amount + 1}))


@pytest.mark.parametrize(("budget", "cash", "reason"), [
    (None, 10_000, TreasuryErrorReason.BUDGET_NOT_ACTIVE),
    (replace(BUDGET, reserved_total=600), 10_000, TreasuryErrorReason.OVERALL_LIMIT),
    (replace(BUDGET, contracts_reserved=51), 10_000, TreasuryErrorReason.CONTRACTS_LIMIT),
    (BUDGET, 199, TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS),
])
@pytest.mark.asyncio
async def test_reservation_requires_budget_article_and_unreserved_cash(repository, service_factory, budget, cash, reason):
    repository.get_contract_budget.return_value = budget
    repository.lock_treasury_available.return_value = cash
    with pytest.raises(TreasuryError, match=reason):
        await service_factory().reserve_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Award")

    repository.update_contract.assert_not_awaited()
    repository.record_contract_reservation.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.parametrize(("actor", "reason"), [
    (OUTSIDER, TreasuryErrorReason.APPROVER_REQUIRED),
    (PROPOSER, TreasuryErrorReason.CONFLICT_OF_INTEREST),
    (RECIPIENT, TreasuryErrorReason.CONFLICT_OF_INTEREST),
])
@pytest.mark.asyncio
async def test_denial_commits_audit_after_failed_attempt(repository, service_factory, actor, reason):
    transactions = []
    service = service_factory(approvers=(APPROVER.discord_user_id, PROPOSER.discord_user_id, RECIPIENT.discord_user_id), transactions=transactions)

    with pytest.raises(TreasuryError, match=reason):
        await service.reserve_contract(DISCORD_GUILD, actor.discord_user_id, DRAFT.id, reason="Award")

    assert transactions == (["begin", "commit"] if actor == OUTSIDER else ["begin", "rollback", "begin", "commit"])
    repository.append_contract_denial.assert_awaited_once_with(
        DISCORD_GUILD, actor.discord_user_id, "contract.reserve", DRAFT.id, reason.value, NOW,
    )
    repository.record_contract_reservation.assert_not_awaited()


@pytest.mark.parametrize("action", ["reserve", "pay"])
@pytest.mark.asyncio
async def test_expense_preview_checks_independence_and_audits_unauthorized_attempts(repository, service_factory, action):
    service = service_factory(approvers=(APPROVER.discord_user_id, PROPOSER.discord_user_id))
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.APPROVER_REQUIRED):
        await service.preview_contract_action(DISCORD_GUILD, OUTSIDER.discord_user_id, DRAFT.id, action=action)
    repository.get_contract.assert_not_awaited()

    with pytest.raises(TreasuryError, match=TreasuryErrorReason.CONFLICT_OF_INTEREST):
        await service.preview_contract_action(DISCORD_GUILD, PROPOSER.discord_user_id, DRAFT.id, action=action)
    assert await service.preview_contract_action(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, action=action) == DRAFT

    assert [call.args for call in repository.append_contract_denial.await_args_list] == [
        (DISCORD_GUILD, OUTSIDER.discord_user_id, f"contract.{action}", DRAFT.id, "approver_required", NOW),
        (DISCORD_GUILD, PROPOSER.discord_user_id, f"contract.{action}", DRAFT.id, "conflict_of_interest", NOW),
    ]
    assert all(call.kwargs == {"lock": False} for call in repository.get_contract.await_args_list)
    repository.update_contract.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reserve_and_submit_change_status_without_moving_money(repository, service_factory):
    service = service_factory()
    first = await service.reserve_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Award")
    repeated = await service.reserve_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Award")
    assert first == repeated == RESERVED
    repository.record_contract_reservation.assert_awaited_once_with(BUDGET, DRAFT.amount)

    with pytest.raises(TreasuryError, match=TreasuryErrorReason.NOT_CONTRACT_PARTY):
        await service.submit_contract(DISCORD_GUILD, OUTSIDER.discord_user_id, DRAFT.id, evidence="Proof")
    submitted = await service.submit_contract(DISCORD_GUILD, RECIPIENT.discord_user_id, DRAFT.id, evidence="Work completed")
    assert submitted == SUBMITTED
    repository.post.assert_not_awaited()


@pytest.mark.parametrize(("company_id", "destination"), [(None, PERSONAL), (COMPANY.id, CORPORATE)])
@pytest.mark.asyncio
async def test_payment_consumes_original_period_once_even_after_rollover(repository, service_factory, company_id, destination):
    repository.get_contract.return_value = replace(SUBMITTED, recipient_company_id=company_id)
    service = service_factory(now=END + timedelta(hours=1))
    paid = await service.pay_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Accepted")
    replay = await service.pay_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Accepted")

    assert paid == replay
    assert paid.status is ContractStatus.PAID and paid.payout_transaction_id == 99
    repository.get_contract_budget.assert_not_awaited()
    repository.get_contract_budget_by_period.assert_awaited_once_with(GUILD, BUDGET.period_id, lock=True)
    repository.release_contract_reservation.assert_awaited_once_with(BUDGET, DRAFT.amount, spent=True)
    repository.post.assert_awaited_once_with(
        GUILD, TransactionKind.STATE_CONTRACT_PAYOUT, f"state_contract_payout:{DRAFT.id}",
        {TREASURY.id: -DRAFT.amount, destination.id: DRAFT.amount}, actor_citizen_id=APPROVER.id, reason="Accepted",
    )


@pytest.mark.asyncio
async def test_expired_contract_cancellation_releases_reserve_once(repository, service_factory):
    repository.get_contract.return_value = RESERVED
    service = service_factory(now=END)
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.DEADLINE_PASSED):
        await service.submit_contract(DISCORD_GUILD, RECIPIENT.discord_user_id, DRAFT.id, evidence="Late work")

    first = await service.cancel_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Deadline passed")
    repeated = await service.cancel_contract(DISCORD_GUILD, APPROVER.discord_user_id, DRAFT.id, reason="Deadline passed")
    assert first == repeated and first.status is ContractStatus.CANCELLED
    repository.release_contract_reservation.assert_awaited_once_with(BUDGET, DRAFT.amount, spent=False)
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_proof_is_only_returned_to_parties_and_staff(repository, service_factory):
    repository.get_contract.return_value = SUBMITTED
    repository.list_contracts.return_value = ()
    service = service_factory()
    with pytest.raises(TreasuryError, match=TreasuryErrorReason.NOT_CONTRACT_PARTY):
        await service.get_contract(DISCORD_GUILD, OUTSIDER.discord_user_id, DRAFT.id)
    assert (await service.get_contract(DISCORD_GUILD, RECIPIENT.discord_user_id, DRAFT.id)).evidence == SUBMITTED.evidence
    assert await service.list_contracts(DISCORD_GUILD, OUTSIDER.discord_user_id) == ()
    repository.list_contracts.assert_awaited_once_with(GUILD, OUTSIDER.id, include_all=False, limit=10)
