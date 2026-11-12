"""Authorize state contracts and reserve budget and treasury cash atomically."""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import CitizenshipAccessError, is_discord_id
from civitas.app.jobs import JobError, JobErrorReason, JobsRepository, budget_week
from civitas.app.market import MarketRepository
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.jobs import BudgetCaps, BudgetPeriod
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH, AccountKind, LedgerError, TransactionKind
from civitas.domain.treasury import ContractBudget, ContractStatus, StateContract

_LIST_LIMIT = 10


class TreasuryErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    INITIATOR_REQUIRED = "initiator_required"
    APPROVER_REQUIRED = "approver_required"
    RECIPIENT_REQUIRED = "recipient_required"
    RECIPIENT_NOT_VERIFIED = "recipient_not_verified"
    COMPANY_NOT_FOUND = "company_not_found"
    CONTRACT_NOT_FOUND = "contract_not_found"
    NOT_CONTRACT_PARTY = "not_contract_party"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_TEXT = "invalid_text"
    INVALID_DEADLINE = "invalid_deadline"
    INVALID_REQUEST_KEY = "invalid_request_key"
    INVALID_STATUS = "invalid_status"
    DEADLINE_PASSED = "deadline_passed"
    BUDGET_NOT_ACTIVE = "budget_not_active"
    BUDGET_PERIOD_UNAVAILABLE = "budget_period_unavailable"
    CONTRACTS_LIMIT = "contracts_limit"
    OVERALL_LIMIT = "overall_limit"
    TREASURY_INSUFFICIENT_FUNDS = "treasury_insufficient_funds"


class TreasuryError(Exception):
    def __init__(self, reason: TreasuryErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class TreasuryRules:
    caps: BudgetCaps
    contracts_weekly_limit: int
    budget_timezone: tzinfo = UTC

    def __post_init__(self) -> None:
        limits = (self.caps.overall, self.caps.npc, self.caps.per_citizen, self.contracts_weekly_limit)
        if any(type(limit) is not int or not 0 <= limit <= MAX_AMOUNT for limit in limits):
            raise ValueError("Weekly caps must be non-negative BIGINT integers")
        if self.caps.npc > self.caps.overall or self.contracts_weekly_limit > self.caps.overall:
            raise ValueError("Article caps cannot exceed the overall weekly cap")
        if not isinstance(self.budget_timezone, tzinfo):
            raise ValueError("Budget timezone must be a tzinfo instance")


class TreasuryRepository(JobsRepository, MarketRepository, Protocol):
    async def get_contract(self, guild_id: int, contract_id: int, *, lock: bool = False) -> StateContract | None: ...

    async def get_contract_by_key(self, guild_id: int, key: str) -> StateContract | None: ...

    async def list_contracts(
        self, guild_id: int, citizen_id: int, *, include_all: bool, limit: int
    ) -> tuple[StateContract, ...]: ...

    async def create_contract(
        self,
        *,
        guild_id: int,
        title: str,
        terms: str,
        amount: int,
        deadline: datetime,
        recipient_citizen_id: int,
        recipient_discord_user_id: int,
        recipient_company_id: int | None,
        proposer_citizen_id: int,
        proposal_reason: str,
        request_key: str,
        created_at: datetime,
    ) -> StateContract | None: ...

    async def update_contract(self, contract: StateContract) -> None: ...

    async def get_contract_budget(
        self, guild_id: int, at: datetime, *, lock: bool = False
    ) -> ContractBudget | None: ...

    async def get_contract_budget_by_period(
        self, guild_id: int, period_id: int, *, lock: bool = False
    ) -> ContractBudget | None: ...

    async def activate_contract_budget(
        self, period: BudgetPeriod, cap: int, actor_id: int, at: datetime, reason: str
    ) -> ContractBudget: ...

    async def lock_treasury_available(
        self, guild_id: int, account_id: int, *, recipient_account_id: int | None = None
    ) -> int: ...

    async def record_contract_reservation(self, budget: ContractBudget, amount: int) -> None: ...

    async def release_contract_reservation(self, budget: ContractBudget, amount: int, *, spent: bool) -> None: ...

    async def append_contract_denial(
        self, discord_guild_id: int, actor_discord_user_id: int, action: str,
        contract_id: int | None, reason: str, at: datetime,
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _valid_id(value: int) -> bool:
    return type(value) is int and 1 <= value <= MAX_AMOUNT


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise TreasuryError(TreasuryErrorReason.INVALID_TEXT)
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise TreasuryError(TreasuryErrorReason.INVALID_TEXT)
    return value


class TreasuryService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[TreasuryRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        initiator_user_ids: Iterable[int],
        approver_user_ids: Iterable[int],
        rules: TreasuryRules,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        initiators, approvers = tuple(initiator_user_ids), tuple(approver_user_ids)
        if not all(is_discord_id(user_id) for user_id in (*initiators, *approvers)):
            raise ValueError("Treasury staff IDs must be Discord snowflakes")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._initiator_user_ids = frozenset(initiators)
        self._approver_user_ids = frozenset(approvers)
        self._rules = rules
        self._clock = clock

    async def current_budget(self, guild_id: int | None, user_id: int) -> ContractBudget | None:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            citizen = await self._citizen(repository, user_id)
            return await repository.get_contract_budget(citizen.guild_id, self._clock())

    async def activate_budget(self, guild_id: int | None, user_id: int, *, reason: str) -> ContractBudget:
        async with self._audit_denial(guild_id, user_id, "budget.activate"):
            self._authorize(guild_id, user_id)
            self._require_approver(user_id)
            reason = _text(reason, 200)
            async with self._unit_of_work() as repository:
                actor = await self._citizen(repository, user_id, lock=True)
                now = self._clock()
                existing = await repository.get_contract_budget(actor.guild_id, now, lock=True)
                if existing is not None:
                    return existing
                week_start_date, start, end = budget_week(now, self._rules.budget_timezone)
                try:
                    period = await repository.get_or_create_budget_period(
                        actor.guild_id, start, end, now, self._rules.caps, week_start_date=week_start_date
                    )
                except JobError as error:
                    if error.reason == JobErrorReason.BUDGET_PERIOD_UNAVAILABLE:
                        raise TreasuryError(TreasuryErrorReason.BUDGET_PERIOD_UNAVAILABLE) from error
                    raise
                cap = self._rules.contracts_weekly_limit
                if cap > period.caps.overall:
                    raise TreasuryError(TreasuryErrorReason.OVERALL_LIMIT)
                return await repository.activate_contract_budget(period, cap, actor.id, now, reason)

    async def propose_contract(
        self,
        guild_id: int | None,
        user_id: int,
        *,
        recipient_user_id: int | None = None,
        recipient_company_id: int | None = None,
        title: str,
        terms: str,
        amount: int,
        deadline: datetime,
        reason: str,
        idempotency_key: str,
    ) -> StateContract:
        async with self._audit_denial(guild_id, user_id, "contract.propose"):
            self._authorize(guild_id, user_id)
            if user_id not in self._initiator_user_ids:
                raise TreasuryError(TreasuryErrorReason.INITIATOR_REQUIRED)
            if (recipient_user_id is None) == (recipient_company_id is None):
                raise TreasuryError(TreasuryErrorReason.RECIPIENT_REQUIRED)
            if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
                raise TreasuryError(TreasuryErrorReason.INVALID_AMOUNT)
            if not isinstance(deadline, datetime) or deadline.utcoffset() is None:
                raise TreasuryError(TreasuryErrorReason.INVALID_DEADLINE)
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
                or "\0" in idempotency_key
            ):
                raise TreasuryError(TreasuryErrorReason.INVALID_REQUEST_KEY)
            deadline = deadline.astimezone(UTC)
            title, terms, reason = _text(title, 80), _text(terms, 500), _text(reason, 200)
            async with self._unit_of_work() as repository:
                proposer = await self._citizen(repository, user_id, lock=True)
                prior = await repository.get_contract_by_key(proposer.guild_id, idempotency_key)
                if prior is None:
                    now = self._clock()
                    if deadline <= now:
                        raise TreasuryError(TreasuryErrorReason.INVALID_DEADLINE)
                    recipient = await self._recipient(repository, proposer.guild_id, recipient_user_id, recipient_company_id)
                    contract = await repository.create_contract(
                        guild_id=proposer.guild_id,
                        title=title,
                        terms=terms,
                        amount=amount,
                        deadline=deadline,
                        recipient_citizen_id=recipient.id,
                        recipient_discord_user_id=recipient.discord_user_id,
                        recipient_company_id=recipient_company_id,
                        proposer_citizen_id=proposer.id,
                        proposal_reason=reason,
                        request_key=idempotency_key,
                        created_at=now,
                    )
                    if contract is not None:
                        return contract
                    prior = await repository.get_contract_by_key(proposer.guild_id, idempotency_key)
                if prior is None or (
                    prior.proposer_citizen_id, prior.recipient_company_id,
                    prior.recipient_discord_user_id if prior.recipient_company_id is None else None,
                    prior.title, prior.terms, prior.amount, prior.deadline, prior.proposal_reason,
                ) != (proposer.id, recipient_company_id, recipient_user_id, title, terms, amount, deadline, reason):
                    raise TreasuryError(TreasuryErrorReason.INVALID_REQUEST_KEY)
                return prior

    async def reserve_contract(
        self, guild_id: int | None, user_id: int, contract_id: int, *, reason: str
    ) -> StateContract:
        async with self._change(guild_id, user_id, contract_id, "contract.reserve", approver=True) as (
            repository, actor, contract, recipient,
        ):
            reason = _text(reason, 200)
            if contract.status in (ContractStatus.RESERVED, ContractStatus.SUBMITTED, ContractStatus.PAID):
                return contract
            self._require_status(contract, ContractStatus.PROPOSED)
            self._require_recipient(recipient)
            now = self._clock()
            self._check_deadline(contract, now)
            budget = await repository.get_contract_budget(contract.guild_id, now, lock=True)
            if budget is None:
                raise TreasuryError(TreasuryErrorReason.BUDGET_NOT_ACTIVE)
            if budget.spent_total + budget.reserved_total + contract.amount > budget.overall_cap:
                raise TreasuryError(TreasuryErrorReason.OVERALL_LIMIT)
            if budget.contracts_spent + budget.contracts_reserved + contract.amount > budget.contracts_cap:
                raise TreasuryError(TreasuryErrorReason.CONTRACTS_LIMIT)
            treasury = await repository.ensure_account(contract.guild_id, AccountKind.TREASURY)
            available = await repository.lock_treasury_available(contract.guild_id, treasury.id)
            if available < contract.amount:
                raise TreasuryError(TreasuryErrorReason.TREASURY_INSUFFICIENT_FUNDS)
            await repository.record_contract_reservation(budget, contract.amount)
            reserved = replace(
                contract, status=ContractStatus.RESERVED, budget_period_id=budget.period_id,
                reserved_by_citizen_id=actor.id, reserved_at=now, reserve_reason=reason,
            )
            await repository.update_contract(reserved)
            return reserved

    async def submit_contract(
        self, guild_id: int | None, user_id: int, contract_id: int, *, evidence: str
    ) -> StateContract:
        async with self._change(guild_id, user_id, contract_id, "contract.submit") as (repository, actor, contract, _):
            if actor.id != contract.recipient_citizen_id:
                raise TreasuryError(TreasuryErrorReason.NOT_CONTRACT_PARTY)
            evidence = _text(evidence, 500)
            if contract.submitted_at is not None and contract.evidence == evidence:
                return contract
            self._require_status(contract, ContractStatus.RESERVED)
            now = self._clock()
            self._check_deadline(contract, now)
            submitted = replace(contract, status=ContractStatus.SUBMITTED, evidence=evidence, submitted_at=now)
            await repository.update_contract(submitted)
            return submitted

    async def pay_contract(
        self, guild_id: int | None, user_id: int, contract_id: int, *, reason: str
    ) -> StateContract:
        async with self._change(guild_id, user_id, contract_id, "contract.pay", approver=True) as (
            repository, actor, contract, recipient,
        ):
            reason = _text(reason, 200)
            if contract.status == ContractStatus.PAID:
                return contract
            self._require_status(contract, ContractStatus.SUBMITTED)
            self._require_recipient(recipient)
            budget = await self._original_budget(repository, contract)
            treasury = await repository.ensure_account(contract.guild_id, AccountKind.TREASURY)
            if contract.recipient_company_id is None:
                destination = await repository.ensure_account(
                    contract.guild_id, AccountKind.PERSONAL, contract.recipient_citizen_id
                )
            else:
                destination = await repository.ensure_company_account(contract.guild_id, contract.recipient_company_id)
            await repository.lock_treasury_available(contract.guild_id, treasury.id, recipient_account_id=destination.id)
            await repository.release_contract_reservation(budget, contract.amount, spent=True)
            posting = await repository.post(
                contract.guild_id, TransactionKind.STATE_CONTRACT_PAYOUT, f"state_contract_payout:{contract.id}",
                {treasury.id: -contract.amount, destination.id: contract.amount}, actor_citizen_id=actor.id, reason=reason,
            )
            paid = replace(
                contract, status=ContractStatus.PAID, closed_at=self._clock(), closed_by_citizen_id=actor.id,
                close_reason=reason, payout_transaction_id=posting.transaction_id,
            )
            await repository.update_contract(paid)
            return paid

    async def cancel_contract(
        self, guild_id: int | None, user_id: int, contract_id: int, *, reason: str
    ) -> StateContract:
        async with self._change(guild_id, user_id, contract_id, "contract.cancel", approver=True) as (
            repository, actor, contract, _,
        ):
            reason = _text(reason, 200)
            if contract.status == ContractStatus.CANCELLED:
                return contract
            self._require_status(contract, ContractStatus.PROPOSED, ContractStatus.RESERVED, ContractStatus.SUBMITTED)
            if contract.budget_period_id is not None:
                budget = await self._original_budget(repository, contract)
                treasury = await repository.ensure_account(contract.guild_id, AccountKind.TREASURY)
                await repository.lock_treasury_available(contract.guild_id, treasury.id)
                await repository.release_contract_reservation(budget, contract.amount, spent=False)
            cancelled = replace(
                contract, status=ContractStatus.CANCELLED, closed_at=self._clock(),
                closed_by_citizen_id=actor.id, close_reason=reason,
            )
            await repository.update_contract(cancelled)
            return cancelled

    async def get_contract(self, guild_id: int | None, user_id: int, contract_id: int) -> StateContract:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            actor = await self._citizen(repository, user_id)
            contract = await self._contract(repository, actor.guild_id, contract_id)
            if not self._is_staff(user_id) and actor.id not in (contract.recipient_citizen_id, contract.proposer_citizen_id):
                raise TreasuryError(TreasuryErrorReason.NOT_CONTRACT_PARTY)
            return contract

    async def list_contracts(self, guild_id: int | None, user_id: int) -> tuple[StateContract, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            actor = await self._citizen(repository, user_id)
            return await repository.list_contracts(actor.guild_id, actor.id, include_all=self._is_staff(user_id), limit=_LIST_LIMIT)

    @asynccontextmanager
    async def _change(
        self, guild_id: int | None, user_id: int, contract_id: int, action: str, *, approver: bool = False
    ) -> AsyncIterator[tuple[TreasuryRepository, Citizen, StateContract, Citizen | None]]:
        async with self._audit_denial(guild_id, user_id, action, contract_id):
            self._authorize(guild_id, user_id)
            if approver:
                self._require_approver(user_id)
            async with self._unit_of_work() as repository:
                actor = await self._citizen(repository, user_id)
                contract = await self._contract(repository, actor.guild_id, contract_id, lock=True)
                citizens = await repository.get_citizens(
                    self._guild_id, (user_id, contract.recipient_discord_user_id), lock=True
                )
                verified = {citizen.id: citizen for citizen in citizens if citizen.status == CitizenStatus.VERIFIED}
                actor = verified.get(actor.id)
                if actor is None:
                    raise TreasuryError(TreasuryErrorReason.CITIZEN_NOT_VERIFIED)
                if approver and actor.id in (contract.recipient_citizen_id, contract.proposer_citizen_id):
                    raise TreasuryError(TreasuryErrorReason.CONFLICT_OF_INTEREST)
                yield repository, actor, contract, verified.get(contract.recipient_citizen_id)

    @asynccontextmanager
    async def _audit_denial(
        self, guild_id: int | None, user_id: int, action: str, contract_id: int | None = None
    ) -> AsyncIterator[None]:
        try:
            yield
        except (CitizenshipAccessError, TreasuryError, LedgerError) as error:
            if guild_id == self._guild_id and is_discord_id(user_id):
                subject = contract_id if contract_id is not None and _valid_id(contract_id) else None
                async with self._unit_of_work() as repository:
                    await repository.append_contract_denial(
                        self._guild_id, user_id, action, subject, error.reason.value, self._clock()
                    )
            raise

    async def _citizen(self, repository: TreasuryRepository, user_id: int, *, lock: bool = False) -> Citizen:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=lock)
        if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
            raise TreasuryError(TreasuryErrorReason.CITIZEN_NOT_VERIFIED)
        return citizens[0]

    async def _recipient(
        self, repository: TreasuryRepository, guild_id: int, user_id: int | None, company_id: int | None
    ) -> Citizen:
        if company_id is not None:
            company = await repository.get_company(guild_id, company_id) if _valid_id(company_id) else None
            if company is None:
                raise TreasuryError(TreasuryErrorReason.COMPANY_NOT_FOUND)
            user_id = company.owner_discord_user_id
        if user_id is None or not is_discord_id(user_id):
            raise TreasuryError(TreasuryErrorReason.RECIPIENT_NOT_VERIFIED)
        try:
            return await self._citizen(repository, user_id)
        except TreasuryError as error:
            raise TreasuryError(TreasuryErrorReason.RECIPIENT_NOT_VERIFIED) from error

    @staticmethod
    async def _contract(
        repository: TreasuryRepository, guild_id: int, contract_id: int, *, lock: bool = False
    ) -> StateContract:
        contract = await repository.get_contract(guild_id, contract_id, lock=lock) if _valid_id(contract_id) else None
        if contract is None:
            raise TreasuryError(TreasuryErrorReason.CONTRACT_NOT_FOUND)
        return contract

    @staticmethod
    async def _original_budget(repository: TreasuryRepository, contract: StateContract) -> ContractBudget:
        if contract.budget_period_id is None:
            raise TreasuryError(TreasuryErrorReason.BUDGET_NOT_ACTIVE)
        budget = await repository.get_contract_budget_by_period(contract.guild_id, contract.budget_period_id, lock=True)
        if budget is None:
            raise TreasuryError(TreasuryErrorReason.BUDGET_NOT_ACTIVE)
        return budget

    def _require_approver(self, user_id: int) -> None:
        if user_id not in self._approver_user_ids:
            raise TreasuryError(TreasuryErrorReason.APPROVER_REQUIRED)

    def _is_staff(self, user_id: int) -> bool:
        return user_id in self._initiator_user_ids or user_id in self._approver_user_ids

    @staticmethod
    def _require_recipient(recipient: Citizen | None) -> None:
        if recipient is None:
            raise TreasuryError(TreasuryErrorReason.RECIPIENT_NOT_VERIFIED)

    @staticmethod
    def _require_status(contract: StateContract, *allowed: ContractStatus) -> None:
        if contract.status not in allowed:
            raise TreasuryError(TreasuryErrorReason.INVALID_STATUS)

    @staticmethod
    def _check_deadline(contract: StateContract, now: datetime) -> None:
        if now >= contract.deadline:
            raise TreasuryError(TreasuryErrorReason.DEADLINE_PASSED)
