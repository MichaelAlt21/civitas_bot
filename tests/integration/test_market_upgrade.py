from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from civitas.app.citizens import CitizensService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.domain.ledger import TransactionKind
from civitas.infrastructure.db.citizens import PostgresCitizenUnitOfWork
from civitas.infrastructure.db.ledger import PostgresLedgerUnitOfWork


@pytest.mark.asyncio
async def test_market_migration_preserves_existing_accounts_and_postings(pg_engine):
    def migrate(connection, *, upgrade):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        if upgrade:
            command.upgrade(config, "head")
        else:
            command.downgrade(config, "0003_npc_jobs")

    async with pg_engine.begin() as connection:
        await connection.run_sync(migrate, upgrade=False)
    citizens = CitizensService(PostgresCitizenUnitOfWork(pg_engine), guild_id=101, allowed_user_ids={201})
    citizen = (await citizens.register(101, 201)).citizen

    async with pg_engine.begin() as connection:
        account_ids = {}
        for kind in ("ISSUER", "TREASURY", "PERSONAL"):
            account_ids[kind] = await connection.scalar(text(
                "INSERT INTO financial_accounts (guild_id, kind, citizen_id) "
                "VALUES (:guild_id, :kind, :citizen_id) RETURNING id"
            ), {"guild_id": citizen.guild_id, "kind": kind, "citizen_id": citizen.id if kind == "PERSONAL" else None})
        for kind, key, target, amount in (
            ("GENESIS", "genesis", "TREASURY", 8000),
            ("STARTER_GRANT", f"starter_grant:{citizen.id}", "PERSONAL", 250),
        ):
            posting_id = await connection.scalar(text(
                "INSERT INTO financial_transactions (guild_id, kind, idempotency_key, reason, entry_count) "
                "VALUES (:guild_id, :kind, :key, 'pre-market fixture', 2) RETURNING id"
            ), {"guild_id": citizen.guild_id, "kind": kind, "key": key})
            await connection.execute(text(
                "INSERT INTO ledger_entries (guild_id, transaction_id, account_id, delta) "
                "VALUES (:guild_id, :transaction_id, :account_id, :delta)"
            ), [
                {"guild_id": citizen.guild_id, "transaction_id": posting_id, "account_id": account_ids[owner], "delta": delta}
                for owner, delta in (("ISSUER", -amount), (target, amount))
            ])

    async with pg_engine.begin() as connection:
        await connection.run_sync(migrate, upgrade=True)
    wallet = WalletService(
        PostgresLedgerUnitOfWork(pg_engine), guild_id=101, authorize=citizens.authorize,
        funding=WalletFunding(starter_grant=300, treasury_genesis=9000),
    )
    await wallet.initialize_guild()
    assert await wallet.balance(101, 201) == 250
    assert [(entry.kind, entry.delta) for entry in await wallet.history(101, 201)] == [
        (TransactionKind.STARTER_GRANT, 250)
    ]
