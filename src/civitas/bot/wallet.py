"""Guild-scoped Discord commands for a citizen's private wallet."""

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.wallet import WalletError, WalletService
from civitas.domain.ledger import LedgerError

from .confirmation import OperationConfirmation
from .localization import command_description, message, transaction_label

_WALLET_ERRORS = (CitizenshipAccessError, WalletError, LedgerError)
_HISTORY_LIMIT = 10


def _error_reply(
    locale: discord.Locale, error: CitizenshipAccessError | WalletError | LedgerError
) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    return message(locale, f"wallet.error.{error.reason.value}")


def add_wallet_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    wallet_service: WalletService,
    currency_symbol: str = "₵",
) -> None:
    wallet = app_commands.Group(name="wallet", description=command_description("wallet"))
    tree.add_command(wallet, guild=guild_scope)

    @wallet.command(name="balance", description=command_description("wallet.balance"))
    async def balance(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            amount = await wallet_service.balance(interaction.guild_id, interaction.user.id)
        except _WALLET_ERRORS as error:
            reply = _error_reply(locale, error)
        else:
            reply = message(locale, "wallet.balance", amount=f"{amount} {currency_symbol}")
        await interaction.followup.send(reply, ephemeral=True)

    @wallet.command(name="history", description=command_description("wallet.history"))
    async def history(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            entries = await wallet_service.history(interaction.guild_id, interaction.user.id)
        except _WALLET_ERRORS as error:
            reply = _error_reply(locale, error)
        else:
            lines = []
            for entry in entries[:_HISTORY_LIMIT]:
                label = transaction_label(locale, entry.kind, entry.delta)
                date = discord.utils.format_dt(entry.created_at, style="f")
                lines.append(f"• #{entry.transaction_id} · {date} — {label}: {entry.delta:+d} {currency_symbol}")
            if lines:
                reply = "\n".join([f"**{message(locale, 'wallet.history.title')}**", *lines])
            else:
                reply = message(locale, "wallet.history.empty")
        await interaction.followup.send(reply, ephemeral=True)

    @wallet.command(name="transfer", description=command_description("wallet.transfer"))
    @app_commands.describe(
        recipient=command_description("wallet.transfer.recipient"),
        amount=command_description("wallet.transfer.amount"),
    )
    async def transfer(
        interaction: discord.Interaction, recipient: discord.Member, amount: app_commands.Range[int, 1]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await wallet_service.prepare_transfer(
                interaction.guild_id, interaction.user.id, recipient.id, amount
            )
        except _WALLET_ERRORS as error:
            await interaction.followup.send(_error_reply(interaction.locale, error), ephemeral=True)
            return

        amount_label = f"{amount} {currency_symbol}"
        recipient_label = f"<@{recipient.id}>"

        async def commit(click: discord.Interaction, idempotency_key: str) -> str:
            try:
                await wallet_service.transfer(
                    click.guild_id, click.user.id, recipient.id, amount, idempotency_key=idempotency_key
                )
            except _WALLET_ERRORS as error:
                return _error_reply(click.locale, error)
            return message(
                click.locale, "wallet.transfer.done", amount=amount_label, recipient=recipient_label
            )

        confirmation = OperationConfirmation(
            guild_id=guild_scope.id,
            actor_id=interaction.user.id,
            locale=interaction.locale,
            commit=commit,
            message_prefix="wallet.transfer",
        )
        await confirmation.show(
            interaction,
            message(
                interaction.locale, "wallet.transfer.preview", amount=amount_label, recipient=recipient_label
            ),
        )
