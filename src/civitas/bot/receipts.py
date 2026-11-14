"""Opt-in private Discord notifications for committed wallet transactions."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2s

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.receipts import ReceiptService, ReceiptUndeliverable
from civitas.domain.receipts import ReceiptDelivery

from .localization import command_description, language_for_locale, message, transaction_label

logger = logging.getLogger(__name__)
_IDLE_INTERVAL_SECONDS = 5
_UNKNOWN_USER = 10013
_NO_MENTIONS = discord.AllowedMentions.none()


def add_receipt_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: ReceiptService,
    currency_symbol: str = "₵",
) -> None:
    wallet = tree.get_command("wallet", guild=guild_scope)
    if not isinstance(wallet, app_commands.Group):
        raise RuntimeError("Receipt preferences require the guild's wallet command group")

    @wallet.command(name="receipts", description=command_description("wallet.receipts"))
    @app_commands.describe(enabled=command_description("wallet.receipts.enabled"))
    async def receipts(interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        preference = language_for_locale(locale) if enabled else None
        try:
            await service.set_preference(interaction.guild_id, interaction.user.id, locale=preference)
        except CitizenshipAccessError as error:
            key = f"access.{error.reason.value}"
        else:
            key = "wallet.receipts.enabled" if enabled else "wallet.receipts.disabled"
        await interaction.followup.send(message(locale, key), ephemeral=True, allowed_mentions=_NO_MENTIONS)

    @wallet.command(name="receipt", description=command_description("wallet.receipt"))
    @app_commands.describe(transaction_id=command_description("wallet.receipt.transaction_id"))
    async def receipt_detail(interaction: discord.Interaction, transaction_id: app_commands.Range[int, 1]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            receipt = await service.get_receipt(interaction.guild_id, interaction.user.id, transaction_id)
        except CitizenshipAccessError as error:
            reply = message(locale, f"access.{error.reason.value}")
        else:
            if receipt is None:
                reply = message(locale, "wallet.receipts.not_found")
            else:
                reference = message(
                    locale, "wallet.receipts.reference", transaction_id=str(receipt.transaction_id),
                    date=discord.utils.format_dt(receipt.created_at, style="f"),
                )
                label = transaction_label(locale, receipt.kind, receipt.delta)
                reply = f"{reference}\n{label}: {receipt.delta:+d} {currency_symbol}"
        await interaction.followup.send(reply, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _send_receipt(client: discord.Client, receipt: ReceiptDelivery) -> int:
    locale = receipt.locale
    lines = (
        message(locale, "wallet.receipts.title"),
        message(
            locale, "wallet.receipts.reference", transaction_id=str(receipt.transaction_id),
            date=discord.utils.format_dt(receipt.created_at, style="f"),
        ),
        message(locale, "wallet.receipts.details_hint", transaction_id=str(receipt.transaction_id)),
    )
    recipient_id = receipt.recipient_discord_user_id
    nonce_key = f"receipt:{recipient_id}:{receipt.id}:{receipt.transaction_id}"
    nonce = blake2s(nonce_key.encode("ascii"), digest_size=12).hexdigest()
    try:
        user = client.get_user(recipient_id)
        if user is None:
            user = await client.fetch_user(recipient_id)
        sent = await user.send("\n".join(lines), allowed_mentions=_NO_MENTIONS, nonce=nonce)
    except discord.Forbidden:
        raise ReceiptUndeliverable() from None
    except discord.NotFound as error:
        if error.code == _UNKNOWN_USER:
            raise ReceiptUndeliverable() from None
        raise
    return sent.id


@asynccontextmanager
async def run_receipt_publisher(client: discord.Client, service: ReceiptService | None) -> AsyncIterator[None]:
    if service is None:
        yield
        return

    async def send(receipt: ReceiptDelivery) -> int:
        return await _send_receipt(client, receipt)

    async def publish() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                delivered = await service.run_due(send)
            except Exception as error:
                logger.warning("Ошибка очереди приватных квитанций (%s)", type(error).__name__)
                delivered = False
            if not delivered:
                await asyncio.sleep(_IDLE_INTERVAL_SECONDS)

    task = asyncio.create_task(publish())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
