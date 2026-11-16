from datetime import UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from civitas import __main__ as entrypoint
from civitas.bot.client import CivitasBot
from civitas.config import GovernmentSettings, JobsSettings, MarketSettings, Settings, TreasurySettings, WalletSettings
from civitas.domain.ledger import TransactionKind
from civitas.infrastructure.db.admission_models import AdmittedUserRow
from civitas.infrastructure.db.ledger_models import FinancialTransactionRow


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_resale_tax_bp", [None, 0], ids=["primary-only", "resale-without-issuer"])
async def test_bot_bootstrap_wires_enabled_modules_without_discord(pg_engine, monkeypatch, asset_resale_tax_bp):
    settings = Settings(
        database_url="unused-by-test",
        discord_token="test-token",
        guild_id=101,
        allowed_user_ids=frozenset({201, 202}),
        technical_operator_ids=frozenset({901}),
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
        asset_issuer_ids=frozenset({201}) if asset_resale_tax_bp is None else frozenset(),
        asset_resale_tax_bp=asset_resale_tax_bp,
        treasury=TreasurySettings(
            initiator_ids=frozenset({201}), approver_ids=frozenset({202}), budget_weekly_limit=800,
            contract_weekly_limit=250, budget_timezone=UTC,
        ),
        treasury_report_channel_id=500,
        government=GovernmentSettings(
            commissioner_ids=frozenset({201, 202}), poll_hosts=frozenset({"discord.com"}), min_eligible=10,
            quorum_bp=4000, quorum_min=4, parliament_seats=3, term_days=28, voting_hours=48,
            extension_hours=24, president_role_id=501, parliament_role_id=502,
        ),
    )
    command_names = set()
    sync = AsyncMock()

    async def start_without_network(bot, token):
        assert token == "test-token"
        monkeypatch.setattr(bot.tree, "sync", sync)
        await bot.setup_hook()
        assert bot.intents.guilds
        command_names.update(command.name for command in bot.tree.get_commands(guild=bot.guild_scope))
        treasury = bot.tree.get_command("treasury", guild=bot.guild_scope)
        assert {command.name for command in treasury.commands} == {"budget", "approve_budget", "report"}
        wallet = bot.tree.get_command("wallet", guild=bot.guild_scope)
        assert {command.name for command in wallet.commands} == {"balance", "history", "transfer", "receipt", "receipts"}
        admin = bot.tree.get_command("admin", guild=bot.guild_scope)
        assert {command.name for command in admin.commands} == {"admission", "audit", "maintenance", "setup"}
        court = bot.tree.get_command("court", guild=bot.guild_scope)
        assert {command.name for command in court.commands} >= {"nominate", "approve", "current", "case"}
        assert {command.name for command in court.get_command("case").commands} == {
            "civil", "charge", "challenge", "challenge_decree", "show", "list", "schedule",
            "decide_challenge", "decide_merits", "decision", "fine_status", "returns",
        }
        assert {command.name for command in court.get_command("appeal").commands} == {
            "file", "open", "voters", "record", "confirm", "show",
        }
        law = bot.tree.get_command("law", guild=bot.guild_scope)
        assert {command.name for command in law.commands} == {"register", "confirm", "show", "revision", "list"}
        decree = bot.tree.get_command("decree", guild=bot.guild_scope)
        assert {command.name for command in decree.commands} == {command.name for command in law.commands}
        prosecutor = bot.tree.get_command("prosecutor", guild=bot.guild_scope)
        assert {command.name for command in prosecutor.commands} == {
            "nominate", "propose_removal", "approve", "current", "motion",
        }
        assets = bot.tree.get_command("assets", guild=bot.guild_scope)
        asset_commands = {command.name for command in assets.commands}
        resale_commands = {"sell", "resale", "resale_buy", "resale_cancel"}
        assert asset_commands & resale_commands == (resale_commands if asset_resale_tax_bp is not None else set())
        assert not bot.tree.get_commands()

    monkeypatch.setattr(entrypoint, "create_db_engine", lambda _url: pg_engine)
    monkeypatch.setattr(CivitasBot, "start", start_without_network)
    await entrypoint.run_bot(settings)

    assert command_names == {
        "start", "citizen", "profile", "wallet", "jobs", "company", "market", "work", "assets", "treasury", "contracts",
        "government", "election", "court", "law", "decree", "prosecutor", "help", "admin",
    }
    assert sync.await_count == 1
    async with pg_engine.connect() as connection:
        assert set(await connection.scalars(select(AdmittedUserRow.discord_user_id))) == {201, 202}
        assert await connection.scalar(
            select(func.count()).select_from(FinancialTransactionRow).where(
                FinancialTransactionRow.kind == TransactionKind.GENESIS
            )
        ) == 1
