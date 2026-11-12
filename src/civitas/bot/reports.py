"""Public aggregate treasury reports and the Discord delivery gateway."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2s
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .localization import command_description, message

if TYPE_CHECKING:
    from civitas.app.reports import ReportService
    from civitas.domain.reports import ReportDelivery, TreasuryReport

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 60
_NO_MENTIONS = discord.AllowedMentions.none()


def _report_embed(
    report: TreasuryReport, currency_symbol: str, locale: discord.Locale, *, bilingual: bool = False,
) -> discord.Embed:
    def label(key: str) -> str:
        key = f"report.{key}"
        if bilingual:
            return f"{message(discord.Locale.russian, key)} / {message(discord.Locale.american_english, key)}"
        return message(locale, key)

    def money_line(key: str, amount: int) -> str:
        return f"{label(key)} · {amount} {currency_symbol}"

    starts_at = discord.utils.format_dt(report.window.starts_at, style="f")
    as_of = discord.utils.format_dt(report.as_of, style="f")
    lines = [
        f"{starts_at} → {as_of}",
        "",
        money_line("opening", report.opening_balance),
        money_line("closing", report.closing_balance),
        money_line("available", report.available_cash),
        money_line("reserved", report.cash_reserved),
        money_line("escrow", report.open_escrow),
        "",
        f"**{money_line('income', report.total_income)}**",
        money_line("fees", report.company_fees),
        money_line("tax", report.sales_tax),
        money_line("assets", report.asset_sales),
        money_line("treasury_issuance", report.treasury_issuance),
        money_line("other_income", report.other_income),
        f"**{money_line('expenses', report.total_expenses)}**",
        money_line("npc", report.npc_payouts),
        money_line("contracts", report.contract_payouts),
        money_line("other_expenses", report.other_expenses),
        "",
        money_line("total_issued", report.total_issued),
        money_line("starter_grants", report.starter_grants),
        f"{label('transactions')} · {report.transaction_count}",
        f"{label('orders')} · {report.settled_orders}",
        f"{label('asset_count')} · {report.asset_purchases}",
    ]
    if report.budget_limit is not None:
        lines.extend(("", f"**{money_line('budget', report.budget_limit)}**", label("budget_columns")))
        for article in report.budget_articles:
            key = {
                "npc_work": "npc", "state_contracts": "contracts",
            }.get(article.code, "budget_other")
            lines.append(f"{label(key)} · {article.cap} / {article.spent} / {article.reserved} {currency_symbol}")
    return discord.Embed(title=label("title"), description="\n".join(lines))


def add_report_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: ReportService, currency_symbol: str,
) -> None:
    treasury = tree.get_command("treasury", guild=guild_scope)
    if treasury is None:
        treasury = app_commands.Group(name="treasury", description=command_description("treasury"))
        tree.add_command(treasury, guild=guild_scope)
    if not isinstance(treasury, app_commands.Group):
        raise RuntimeError("The treasury command must be a group")

    @treasury.command(name="report", description=command_description("treasury.report"))
    @app_commands.describe(previous=command_description("treasury.report.previous"))
    async def report(interaction: discord.Interaction, previous: bool = False) -> None:
        if interaction.guild_id != guild_scope.id:
            await interaction.response.send_message(message(interaction.locale, "access.wrong_guild"), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        view = await service.get_report(interaction.guild_id, previous=previous)
        await interaction.followup.send(
            embed=_report_embed(view, currency_symbol, interaction.locale), allowed_mentions=_NO_MENTIONS,
        )


async def _send_report(
    client: discord.Client, delivery: ReportDelivery, *, guild_id: int, currency_symbol: str,
) -> int:
    channel = client.get_channel(delivery.channel_id)
    if channel is None:
        channel = await client.fetch_channel(delivery.channel_id)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
        raise ValueError("Treasury report channel must be a text channel in the configured guild")
    nonce_key = f"treasury:{guild_id}:{channel.id}:{delivery.report.window.week_start.isoformat()}"
    nonce = blake2s(nonce_key.encode("ascii"), digest_size=12).hexdigest()
    sent = await channel.send(
        embed=_report_embed(delivery.report, currency_symbol, discord.Locale.russian, bilingual=True),
        allowed_mentions=_NO_MENTIONS, nonce=nonce,
    )
    return sent.id


@asynccontextmanager
async def run_report_publisher(
    client: discord.Client, service: ReportService | None, *, guild_id: int, currency_symbol: str,
) -> AsyncIterator[None]:
    if service is None:
        yield
        return

    async def publish(delivery: ReportDelivery) -> int:
        return await _send_report(client, delivery, guild_id=guild_id, currency_symbol=currency_symbol)

    async def run() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await service.run_due(publish)
            except Exception as error:
                logger.warning("Ошибка еженедельного отчёта казны (%s)", type(error).__name__)
            await asyncio.sleep(_INTERVAL_SECONDS)

    task = asyncio.create_task(run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
