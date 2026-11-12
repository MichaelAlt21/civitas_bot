from datetime import UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civitas import __main__ as entrypoint
from civitas.bot.client import CivitasBot
from civitas.config import JobsSettings, MarketSettings, Settings, TreasurySettings, WalletSettings
from civitas.domain.ledger import TransactionKind
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow


@pytest.mark.asyncio
async def test_bot_bootstrap_wires_enabled_modules_without_discord(pg_engine, monkeypatch):
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
        market=MarketSettings(
            company_fee=150, company_limit_per_owner=1, sales_tax_basis_points=1000,
            arbiter_ids=frozenset({201}),
        ),
        asset_issuer_ids=frozenset({201}),
        treasury=TreasurySettings(
            initiator_ids=frozenset({201}), approver_ids=frozenset({202}), budget_weekly_limit=800,
            contract_weekly_limit=250, budget_timezone=UTC,
        ),
        treasury_report_channel_id=500,
    )
    command_names = set()
    sync = AsyncMock()

    async def start_without_network(bot, token):
        assert token == "test-token"
        monkeypatch.setattr(bot.tree, "sync", sync)
        await bot.setup_hook()
        command_names.update(command.name for command in bot.tree.get_commands(guild=bot.guild_scope))
        treasury = bot.tree.get_command("treasury", guild=bot.guild_scope)
        assert {command.name for command in treasury.commands} == {"budget", "approve_budget", "report"}
        assert not bot.tree.get_commands()

    monkeypatch.setattr(entrypoint, "create_db_engine", lambda _url: pg_engine)
    monkeypatch.setattr(CivitasBot, "start", start_without_network)
    await entrypoint.run_bot(settings)

    assert command_names == {
        "start", "citizen", "profile", "wallet", "jobs", "company", "market", "assets", "treasury", "contracts",
    }
    assert sync.await_count == 1
    async with pg_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow).where(
                FinancialTransactionRow.kind == TransactionKind.GENESIS
            )
        ) == 1
