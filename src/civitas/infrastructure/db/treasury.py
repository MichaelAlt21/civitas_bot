"""Persist state contracts, budget approvals and their shared treasury reserves."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from civitas.domain.jobs import BudgetPeriod
from civitas.domain.ledger import AccountKind
from civitas.domain.treasury import ContractBudget, ContractStatus, StateContract

from .assets import PostgresAssetRepository
from .jobs_models import BudgetArticleRow, BudgetPeriodRow
from .ledger_models import FinancialAccountRow
from .models import CitizenRow
from .treasury_models import ContractBudgetApprovalRow, StateContractRow, TreasuryAuditEventRow

_CONTRACT_ARTICLE = "state_contracts"


def _to_contract(row: StateContractRow, discord_user_id: int | Decimal) -> StateContract:
    return StateContract(
        id=row.id,
        guild_id=row.guild_id,
        title=row.title,
        terms=row.terms,
        amount=row.amount,
        deadline=row.deadline.astimezone(UTC),
        recipient_citizen_id=row.recipient_citizen_id,
        recipient_company_id=row.recipient_company_id,
        proposer_citizen_id=row.proposer_citizen_id,
        proposal_reason=row.proposal_reason,
        request_key=row.request_key,
        created_at=row.created_at.astimezone(UTC),
        status=ContractStatus(row.status),
        recipient_discord_user_id=int(discord_user_id),
        budget_period_id=row.budget_period_id,
        reserved_by_citizen_id=row.reserved_by_citizen_id,
        reserved_at=row.reserved_at.astimezone(UTC) if row.reserved_at else None,
        reserve_reason=row.reserve_reason,
        evidence=row.evidence,
        submitted_at=row.submitted_at.astimezone(UTC) if row.submitted_at else None,
        closed_by_citizen_id=row.closed_by_citizen_id,
        closed_at=row.closed_at.astimezone(UTC) if row.closed_at else None,
        close_reason=row.close_reason,
        payout_transaction_id=row.payout_transaction_id,
    )


def _contracts() -> Select:
    return select(StateContractRow, CitizenRow.discord_user_id).join(
        CitizenRow, CitizenRow.id == StateContractRow.recipient_citizen_id
    )


def _budgets() -> Select:
    return (
        select(BudgetPeriodRow, BudgetArticleRow, ContractBudgetApprovalRow)
        .join(ContractBudgetApprovalRow, ContractBudgetApprovalRow.period_id == BudgetPeriodRow.id)
        .join(BudgetArticleRow, BudgetArticleRow.period_id == BudgetPeriodRow.id)
        .where(BudgetArticleRow.code == _CONTRACT_ARTICLE)
    )


class PostgresTreasuryRepository(PostgresAssetRepository):
    async def get_contract(self, guild_id: int, contract_id: int, *, lock: bool = False) -> StateContract | None:
        statement = _contracts().where(StateContractRow.guild_id == guild_id, StateContractRow.id == contract_id)
        if lock:
            statement = statement.with_for_update(of=StateContractRow, key_share=True)
        row = (await self._session.execute(statement)).one_or_none()
        return _to_contract(*row) if row is not None else None

    async def get_contract_by_key(self, guild_id: int, key: str) -> StateContract | None:
        row = (await self._session.execute(_contracts().where(
            StateContractRow.guild_id == guild_id, StateContractRow.request_key == key,
        ))).one_or_none()
        return _to_contract(*row) if row is not None else None

    async def list_contracts(
        self, guild_id: int, citizen_id: int, *, include_all: bool, limit: int,
    ) -> tuple[StateContract, ...]:
        statement = _contracts().where(StateContractRow.guild_id == guild_id)
        if not include_all:
            statement = statement.where(or_(
                StateContractRow.proposer_citizen_id == citizen_id,
                StateContractRow.recipient_citizen_id == citizen_id,
            ))
        rows = await self._session.execute(statement.order_by(StateContractRow.id.desc()).limit(limit))
        return tuple(_to_contract(*row) for row in rows)

    async def create_contract(
        self, *, guild_id: int, title: str, terms: str, amount: int, deadline: datetime,
        recipient_citizen_id: int, recipient_discord_user_id: int, recipient_company_id: int | None,
        proposer_citizen_id: int, proposal_reason: str, request_key: str, created_at: datetime,
    ) -> StateContract | None:
        row = await self._session.scalar(
            insert(StateContractRow).values(
                guild_id=guild_id, title=title, terms=terms, amount=amount, deadline=deadline,
                recipient_citizen_id=recipient_citizen_id, recipient_company_id=recipient_company_id,
                proposer_citizen_id=proposer_citizen_id, proposal_reason=proposal_reason,
                request_key=request_key, created_at=created_at, status=ContractStatus.PROPOSED.value,
            ).on_conflict_do_nothing(constraint="uq_state_contracts_request").returning(StateContractRow)
        )
        return _to_contract(row, recipient_discord_user_id) if row is not None else None

    async def update_contract(self, contract: StateContract) -> None:
        await self._session.execute(update(StateContractRow).where(
            StateContractRow.guild_id == contract.guild_id, StateContractRow.id == contract.id,
        ).values(
            status=contract.status.value, budget_period_id=contract.budget_period_id,
            reserved_by_citizen_id=contract.reserved_by_citizen_id, reserved_at=contract.reserved_at,
            reserve_reason=contract.reserve_reason, evidence=contract.evidence, submitted_at=contract.submitted_at,
            closed_by_citizen_id=contract.closed_by_citizen_id, closed_at=contract.closed_at,
            close_reason=contract.close_reason, payout_transaction_id=contract.payout_transaction_id,
        ))

    async def _get_budget(self, statement: Select, *, lock: bool) -> ContractBudget | None:
        if lock:
            # NPC and contract spend both lock the period before touching their article.
            statement = statement.with_for_update(of=BudgetPeriodRow, key_share=True)
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        if lock:
            # READ COMMITTED may have waited for a concurrent period update after
            # taking the article snapshot. Read fresh counters under the period lock.
            row = (await self._session.execute(_budgets().where(BudgetPeriodRow.id == row[0].id)
                     .execution_options(populate_existing=True))).one()
        period, article, approval = row
        return ContractBudget(
            period_id=period.id, guild_id=period.guild_id, start_at=period.start_at.astimezone(UTC),
            end_at=period.end_at.astimezone(UTC), overall_cap=period.overall_cap,
            spent_total=period.spent_total, reserved_total=period.reserved_total,
            contracts_cap=article.cap, contracts_spent=article.spent, contracts_reserved=article.reserved,
            activated_at=approval.activated_at.astimezone(UTC),
            activated_by_citizen_id=approval.activated_by_citizen_id, activation_reason=approval.activation_reason,
        )

    async def get_contract_budget(
        self, guild_id: int, at: datetime, *, lock: bool = False,
    ) -> ContractBudget | None:
        return await self._get_budget(_budgets().where(
            BudgetPeriodRow.guild_id == guild_id, BudgetPeriodRow.start_at <= at, BudgetPeriodRow.end_at > at,
        ), lock=lock)

    async def get_contract_budget_by_period(
        self, guild_id: int, period_id: int, *, lock: bool = False,
    ) -> ContractBudget | None:
        return await self._get_budget(_budgets().where(
            BudgetPeriodRow.guild_id == guild_id, BudgetPeriodRow.id == period_id,
        ), lock=lock)

    async def activate_contract_budget(
        self, period: BudgetPeriod, cap: int, actor_id: int, at: datetime, reason: str,
    ) -> ContractBudget:
        await self._session.execute(insert(ContractBudgetApprovalRow).values(
            period_id=period.id, guild_id=period.guild_id, activated_by_citizen_id=actor_id,
            activated_at=at, activation_reason=reason,
        ).on_conflict_do_nothing(index_elements=[ContractBudgetApprovalRow.period_id]))
        await self._session.execute(insert(BudgetArticleRow).values(
            period_id=period.id, code=_CONTRACT_ARTICLE, cap=cap, personal_cap=0,
        ).on_conflict_do_nothing(constraint="uq_budget_articles_period_code"))
        budget = await self.get_contract_budget_by_period(period.guild_id, period.id)
        assert budget is not None
        return budget

    async def lock_treasury_available(
        self, guild_id: int, account_id: int, *, recipient_account_id: int | None = None,
    ) -> int:
        account_ids = {account_id} if recipient_account_id is None else {account_id, recipient_account_id}
        rows = (await self._session.execute(select(FinancialAccountRow.id, FinancialAccountRow.kind).where(
            FinancialAccountRow.guild_id == guild_id, FinancialAccountRow.id.in_(account_ids),
        ).order_by(FinancialAccountRow.id).with_for_update(key_share=True))).tuples().all()
        if len(rows) != len(account_ids) or (account_id, AccountKind.TREASURY.value) not in rows:
            raise RuntimeError("Contract cash must use accounts from its guild treasury")
        return await self.get_balance(account_id) - await self._reserved_treasury_cash(guild_id)

    async def _change_counters(self, budget: ContractBudget, *, reserved: int, spent: int) -> None:
        await self._session.execute(update(BudgetPeriodRow).where(
            BudgetPeriodRow.guild_id == budget.guild_id, BudgetPeriodRow.id == budget.period_id,
        ).values(
            reserved_total=BudgetPeriodRow.reserved_total + reserved, spent_total=BudgetPeriodRow.spent_total + spent,
        ))
        await self._session.execute(update(BudgetArticleRow).where(
            BudgetArticleRow.period_id == budget.period_id, BudgetArticleRow.code == _CONTRACT_ARTICLE,
        ).values(reserved=BudgetArticleRow.reserved + reserved, spent=BudgetArticleRow.spent + spent))

    async def record_contract_reservation(self, budget: ContractBudget, amount: int) -> None:
        await self._change_counters(budget, reserved=amount, spent=0)

    async def release_contract_reservation(self, budget: ContractBudget, amount: int, *, spent: bool) -> None:
        await self._change_counters(budget, reserved=-amount, spent=amount if spent else 0)

    async def append_contract_denial(
        self, discord_guild_id: int, actor_discord_user_id: int, action: str,
        contract_id: int | None, reason: str, at: datetime,
    ) -> None:
        self._session.add(TreasuryAuditEventRow(
            discord_guild_id=Decimal(discord_guild_id), actor_discord_user_id=Decimal(actor_discord_user_id),
            action=action, contract_id=contract_id, reason=reason, occurred_at=at,
        ))


class PostgresTreasuryUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresTreasuryRepository]:
        async with self._sessions.begin() as session:
            yield PostgresTreasuryRepository(session)
