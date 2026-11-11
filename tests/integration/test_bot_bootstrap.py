from datetime import UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civitas import __main__ as entrypoint
from civitas.bot.client import CivitasBot
from civitas.config import JobsSettings, Settings, WalletSettings
from civitas.domain.ledger import TransactionKind
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow


@pytest.mark.asyncio
async def test_bot_bootstrap_wires_citizens_wallet_and_jobs_without_discord(pg_engine, monkeypatch):
    settings = Settings(
        database_url="unused-by-test",
        discord_token="test-token",
        guild_id=101,
        allowed_user_ids=frozenset({201, 202}),
        wallet=WalletSettings(starter_grant=250, treasury_genesis=8000, currency_symbol="₵"),
        jobs=JobsSettings(
            reviewer_ids=frozenset({201}),
            budget_weekly_limit=800,
            npc_weekly_limit=300,
            personal_weekly_limit=100,
            reward_min=10,
            reward_max=40,
            budget_timezone=UTC,
            cooldown_minutes=0,
        ),
    )
    command_names = set()
    sync = AsyncMock()

    async def start_without_network(bot, token):
        assert token == "test-token"
        monkeypatch.setattr(bot.tree, "sync", sync)
        await bot.setup_hook()
        command_names.update(command.name for command in bot.tree.get_commands(guild=bot.guild_scope))
        assert not bot.tree.get_commands()

    monkeypatch.setattr(entrypoint, "create_db_engine", lambda _url: pg_engine)
    monkeypatch.setattr(CivitasBot, "start", start_without_network)
    await entrypoint.run_bot(settings)

    assert command_names == {"start", "citizen", "profile", "wallet", "jobs"}
    assert sync.await_count == 1
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow).where(
                FinancialTransactionRow.kind == TransactionKind.GENESIS
            )
        ) == 1
