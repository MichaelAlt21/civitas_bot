import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from civitas.app.citizens import CitizensService
from civitas.domain.ledger import (
    AccountKind,
    LedgerError,
    LedgerErrorReason,
    PostResult,
    TransactionKind,
)
from civitas.infrastructure.db.ledger import PostgresLedgerUnitOfWork
from civitas.infrastructure.db.ledger_models import (
    FinancialTransactionRow,
    LedgerEntryRow,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class Wallets:
    guild: int
    buyer: int
    buyer_citizen: int
    seller: int
    another_recipient: int
    treasury: int
    issuer: int


async def _open_wallets(unit_of_work: PostgresLedgerUnitOfWork, amount: int) -> Wallets:
    guild_id = 100
    citizen_service = CitizensService(
        unit_of_work, guild_id=guild_id, allowed_user_ids={1, 2, 3}
    )
    buyer, seller, another = [
        (await citizen_service.register(guild_id, user_id)).citizen for user_id in (1, 2, 3)
    ]
    guild = buyer.guild_id
    async with unit_of_work() as repository:
        issuer = await repository.ensure_account(guild, AccountKind.ISSUER)
        treasury = await repository.ensure_account(guild, AccountKind.TREASURY)
        source = await repository.ensure_account(guild, AccountKind.PERSONAL, buyer.id)
        receiver = await repository.ensure_account(guild, AccountKind.PERSONAL, seller.id)
        second = await repository.ensure_account(guild, AccountKind.PERSONAL, another.id)
        await repository.post(
            guild,
            TransactionKind.STARTER_GRANT,
            f"starter_grant:{buyer.id}",
            {issuer.id: -amount, source.id: amount},
            reason="test funding",
        )
    return Wallets(guild, source.id, buyer.id, receiver.id, second.id, treasury.id, issuer.id)


async def test_three_entry_settlement_replay_and_reconciliation(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresLedgerUnitOfWork(pg_engine)
    wallets = await _open_wallets(unit_of_work, 250)
    entries = {wallets.buyer: -200, wallets.seller: 180, wallets.treasury: 20}
    async with unit_of_work() as repository:
        first = await repository.post(
            wallets.guild, TransactionKind.TRANSFER, "split", entries,
            actor_citizen_id=wallets.buyer_citizen, reason="settlement test",
        )
    assert first.created

    async with unit_of_work() as repository:
        replay = await repository.post(
            wallets.guild, TransactionKind.TRANSFER, "split", entries,
            actor_citizen_id=wallets.buyer_citizen, reason="settlement test",
        )
        history = await repository.get_history(wallets.buyer, 10)
        balances = [
            await repository.get_balance(account)
            for account in (wallets.buyer, wallets.seller, wallets.treasury, wallets.issuer)
        ]
    assert replay == PostResult(transaction_id=first.transaction_id, created=False)
    assert balances == [50, 180, 20, -250]
    assert sum(balances) == 0
    assert [entry.delta for entry in history] == [-200, 250]

    # Altering either the postings or audit metadata conflicts with the existing key.
    for proposed_entries, reason in [
        ({wallets.buyer: -20, wallets.seller: 18, wallets.treasury: 2}, "settlement test"),
        (entries, "different reason"),
    ]:
        with pytest.raises(LedgerError) as failure:
            async with unit_of_work() as repository:
                await repository.post(
                    wallets.guild, TransactionKind.TRANSFER, "split", proposed_entries,
                    actor_citizen_id=wallets.buyer_citizen, reason=reason,
                )
        assert failure.value.reason == LedgerErrorReason.IDEMPOTENCY_CONFLICT


async def test_insufficient_funds_and_concurrent_debits(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresLedgerUnitOfWork(pg_engine)
    wallets = await _open_wallets(unit_of_work, 100)
    with pytest.raises(LedgerError) as failure:
        async with unit_of_work() as repository:
            await repository.post(
                wallets.guild, TransactionKind.TRANSFER, "too-large",
                {wallets.buyer: -150, wallets.seller: 150}, reason="overspend",
            )
    assert failure.value.reason == LedgerErrorReason.INSUFFICIENT_FUNDS
    async with unit_of_work() as repository:
        assert await repository.get_balance(wallets.buyer) == 100
        assert await repository.get_balance(wallets.seller) == 0
        assert await repository.get_post(wallets.guild, TransactionKind.TRANSFER, "too-large") is None

    barrier = asyncio.Barrier(2)

    async def transfer(recipient: int) -> PostResult:
        async with unit_of_work() as repository:
            await barrier.wait()
            return await repository.post(
                wallets.guild, TransactionKind.TRANSFER, f"concurrent:{recipient}",
                {wallets.buyer: -80, recipient: 80}, reason="concurrent",
            )

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            transfer(wallets.seller), transfer(wallets.another_recipient), return_exceptions=True
        ),
        timeout=10,
    )
    successes = [outcome for outcome in outcomes if isinstance(outcome, PostResult)]
    errors = [outcome.reason for outcome in outcomes if isinstance(outcome, LedgerError)]
    assert len(successes) == 1
    assert errors == [LedgerErrorReason.INSUFFICIENT_FUNDS]
    async with unit_of_work() as repository:
        assert await repository.get_balance(wallets.buyer) == 20
        assert sorted([
            await repository.get_balance(wallets.seller),
            await repository.get_balance(wallets.another_recipient),
        ]) == [0, 80]


@pytest.mark.parametrize("failure_case", ["missing_entries", "unbalanced", "negative"])
async def test_database_rejects_bad_raw_posting(
    pg_engine: AsyncEngine, failure_case: str
) -> None:
    unit_of_work = PostgresLedgerUnitOfWork(pg_engine)
    wallets = await _open_wallets(unit_of_work, 100)
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            transaction_id = await connection.scalar(
                insert(FinancialTransactionRow)
                .values(
                    guild_id=wallets.guild,
                    kind=TransactionKind.TRANSFER.value,
                    idempotency_key="invalid",
                    entry_count=2,
                    reason="direct invalid transaction",
                )
                .returning(FinancialTransactionRow.id)
            )
            if failure_case != "missing_entries":
                debit, credit = (-10, 9) if failure_case == "unbalanced" else (-101, 101)
                await connection.execute(
                    insert(LedgerEntryRow),
                    [
                        {
                            "transaction_id": transaction_id,
                            "guild_id": wallets.guild,
                            "account_id": account,
                            "delta": delta,
                        }
                        for account, delta in ((wallets.buyer, debit), (wallets.seller, credit))
                    ],
                )
    async with unit_of_work() as repository:
        assert await repository.get_balance(wallets.buyer) == 100
        assert await repository.get_balance(wallets.seller) == 0
        assert await repository.get_post(wallets.guild, TransactionKind.TRANSFER, "invalid") is None


async def test_database_rejects_edits_and_later_entries(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresLedgerUnitOfWork(pg_engine)
    wallets = await _open_wallets(unit_of_work, 100)
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            await connection.execute(
                update(LedgerEntryRow)
                .where(LedgerEntryRow.account_id == wallets.buyer)
                .values(delta=1)
            )
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as connection:
            transaction_id = await connection.scalar(select(FinancialTransactionRow.id))
            await connection.execute(
                insert(LedgerEntryRow),
                [
                    {
                        "transaction_id": transaction_id,
                        "guild_id": wallets.guild,
                        "account_id": account,
                        "delta": delta,
                    }
                    for account, delta in ((wallets.seller, 1), (wallets.treasury, -1))
                ],
            )
    async with pg_engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(LedgerEntryRow)) == 2


async def test_rejects_unauthorized_issuer_postings(pg_engine: AsyncEngine) -> None:
    unit_of_work = PostgresLedgerUnitOfWork(pg_engine)
    wallets = await _open_wallets(unit_of_work, 100)
    for kind, key in [
        (TransactionKind.TRANSFER, "pretend-transfer"),
        (TransactionKind.STARTER_GRANT, "second-grant"),
    ]:
        with pytest.raises(LedgerError) as failure:
            async with unit_of_work() as repository:
                await repository.post(
                    wallets.guild, kind, key, {wallets.issuer: -10, wallets.buyer: 10},
                    reason="disallowed issue",
                )
        assert failure.value.reason == LedgerErrorReason.INVALID_POSTING
