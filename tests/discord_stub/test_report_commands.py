from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.bot.reports import _send_report, add_report_commands

GUILD_ID = 101
CHANNEL_ID = 500


def _sample_report():
    starts_at = datetime(2026, 11, 9, tzinfo=UTC)
    return SimpleNamespace(
        window=SimpleNamespace(week_start=date(2026, 11, 9), starts_at=starts_at),
        as_of=datetime(2026, 11, 16, tzinfo=UTC),
        opening_balance=8000, closing_balance=8180, available_cash=8150, cash_reserved=30,
        open_escrow=10, total_income=240, company_fees=150, sales_tax=20, asset_sales=70,
        treasury_issuance=0, other_income=0, total_expenses=60, npc_payouts=30, contract_payouts=30,
        other_expenses=0, total_issued=250, starter_grants=250, transaction_count=8,
        settled_orders=1, asset_purchases=1, budget_limit=800,
        budget_articles=(SimpleNamespace(code="state_contracts", cap=250, spent=30, reserved=30),),
        private_evidence="SECRET: private proof, citizen and account IDs",
    )


def _report_command(service, *, has_existing_treasury):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guild_scope = discord.Object(id=GUILD_ID)
    if has_existing_treasury:
        group = app_commands.Group(name="treasury", description="State treasury")
        tree.add_command(group, guild=guild_scope)
    add_report_commands(tree, guild_scope=guild_scope, service=service, currency_symbol="₵")
    group = tree.get_command("treasury", guild=guild_scope)
    assert group is not None
    assert tree.get_command("treasury") is None
    return group.get_command("report")


@pytest.mark.asyncio
@pytest.mark.parametrize(("locale", "title", "label", "has_existing_treasury"), [
    (discord.Locale.russian, "Отчёт казны", "Налог с продаж", True),
    (discord.Locale.american_english, "Treasury report", "Sales tax", False),
])
async def test_treasury_report_is_public_and_aggregated(locale, title, label, has_existing_treasury):
    service = SimpleNamespace(get_report=AsyncMock(return_value=_sample_report()))
    command = _report_command(service, has_existing_treasury=has_existing_treasury)
    interaction = SimpleNamespace(
        guild_id=GUILD_ID, locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )

    await command.callback(interaction, previous=True)

    service.get_report.assert_awaited_once_with(GUILD_ID, previous=True)
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    sent = interaction.followup.send.await_args.kwargs
    assert not sent.get("ephemeral", False)
    assert sent["embed"].title == title
    assert f"{label} · 20 ₵" in sent["embed"].description
    assert "SECRET" not in sent["embed"].description
    assert sent["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
async def test_report_in_wrong_guild_returns_private_denial_without_reading_data():
    service = SimpleNamespace(get_report=AsyncMock())
    command = _report_command(service, has_existing_treasury=False)
    interaction = SimpleNamespace(
        guild_id=GUILD_ID + 1, locale=discord.Locale.american_english,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await command.callback(interaction)

    assert interaction.response.send_message.await_args.kwargs["ephemeral"]
    service.get_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_weekly_delivery_uses_bilingual_aggregates_and_reuses_nonce_after_error():
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=GUILD_ID)
    channel.id = CHANNEL_ID
    channel.send = AsyncMock(side_effect=[RuntimeError("temporary"), SimpleNamespace(id=900)])
    client = SimpleNamespace(get_channel=Mock(return_value=None), fetch_channel=AsyncMock(return_value=channel))
    delivery = SimpleNamespace(channel_id=CHANNEL_ID, report=_sample_report())

    with pytest.raises(RuntimeError, match="temporary"):
        await _send_report(client, delivery, guild_id=GUILD_ID, currency_symbol="₵")
    assert await _send_report(client, delivery, guild_id=GUILD_ID, currency_symbol="₵") == 900

    first, second = [call.kwargs for call in channel.send.await_args_list]
    assert first["nonce"] == second["nonce"] and len(first["nonce"]) <= 25
    assert second["embed"].title == "Отчёт казны / Treasury report"
    assert "Налог с продаж / Sales tax · 20 ₵" in second["embed"].description
    assert "SECRET" not in second["embed"].description


@pytest.mark.asyncio
async def test_weekly_delivery_refuses_a_channel_from_another_guild():
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=GUILD_ID + 1)
    channel.send = AsyncMock()
    client = SimpleNamespace(get_channel=Mock(return_value=channel))

    with pytest.raises(ValueError, match="configured guild"):
        await _send_report(client, SimpleNamespace(channel_id=CHANNEL_ID), guild_id=GUILD_ID, currency_symbol="₵")

    channel.send.assert_not_awaited()
