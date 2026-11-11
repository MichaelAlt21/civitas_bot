"""Guild-scoped Discord commands for a citizen's private wallet."""

import logging
from uuid import uuid4

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.wallet import WalletError, WalletService
from civitas.domain.ledger import LedgerError, TransactionKind

from .localization import command_description, message

logger = logging.getLogger(__name__)

_WALLET_ERRORS = (CitizenshipAccessError, WalletError, LedgerError)
_HISTORY_LIMIT = 10
_NO_MENTIONS = discord.AllowedMentions.none()


def _error_reply(
    locale: discord.Locale, error: CitizenshipAccessError | WalletError | LedgerError
) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    return message(locale, f"wallet.error.{error.reason.value}")


class TransferConfirmation(discord.ui.View):
    def __init__(
        self,
        *,
        wallet_service: WalletService,
        guild_id: int,
        sender_id: int,
        recipient_id: int,
        amount: int,
        currency_symbol: str,
        locale: discord.Locale,
    ) -> None:
        super().__init__(timeout=120)
        self._wallet_service = wallet_service
        self._guild_id = guild_id
        self._sender_id = sender_id
        self._recipient_id = recipient_id
        self._amount = amount
        self._amount_label = f"{amount} {currency_symbol}"
        self._locale = locale
        self._idempotency_key = uuid4().hex
        self._processing = False
        self._submitted = False
        self._prompt: discord.InteractionMessage | None = None
        self.confirm.label = message(locale, "wallet.transfer.confirm")
        self.cancel.label = message(locale, "wallet.transfer.cancel")

    async def show(self, interaction: discord.Interaction) -> None:
        self._prompt = await interaction.edit_original_response(
            content=message(
                self._locale,
                "wallet.transfer.preview",
                amount=self._amount_label,
                recipient=f"<@{self._recipient_id}>",
            ),
            view=self,
            allowed_mentions=_NO_MENTIONS,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == self._guild_id and interaction.user.id == self._sender_id:
            return True
        await interaction.response.send_message(
            message(interaction.locale, "wallet.transfer.wrong_actor"), ephemeral=True
        )
        return False

    async def _begin_action(self, interaction: discord.Interaction, *, cancelling: bool) -> bool:
        if self.is_finished():
            key = "wallet.transfer.expired"
        elif self._processing:
            key = "wallet.transfer.processing"
        elif cancelling and self._submitted:
            key = "wallet.transfer.retry"
        else:
            self._processing = True
            return True
        await interaction.response.send_message(message(interaction.locale, key), ephemeral=True)
        return False

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._begin_action(interaction, cancelling=False):
            return
        locale = interaction.locale
        try:
            await interaction.response.defer()
            self._submitted = True
            try:
                await self._wallet_service.transfer(
                    interaction.guild_id,
                    interaction.user.id,
                    self._recipient_id,
                    self._amount,
                    idempotency_key=self._idempotency_key,
                )
            except _WALLET_ERRORS as error:
                reply = _error_reply(locale, error)
            else:
                reply = message(
                    locale,
                    "wallet.transfer.done",
                    amount=self._amount_label,
                    recipient=f"<@{self._recipient_id}>",
                )
            await interaction.edit_original_response(content=reply, view=None, allowed_mentions=_NO_MENTIONS)
            self.stop()
        finally:
            self._processing = False

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._begin_action(interaction, cancelling=True):
            return
        try:
            await interaction.response.edit_message(
                content=message(interaction.locale, "wallet.transfer.cancelled"), view=None
            )
            self.stop()
        finally:
            self._processing = False

    async def on_timeout(self) -> None:
        if self._prompt is None or self._processing:
            return
        try:
            await self._prompt.edit(content=message(self._locale, "wallet.transfer.expired"), view=None)
        except discord.HTTPException as error:
            logger.warning("Не удалось обновить подтверждение перевода (HTTP %s)", error.status)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.error("Ошибка подтверждения перевода (%s)", type(error).__name__)
        key = "wallet.transfer.retry" if self._submitted else "error.temporary"
        reply = message(interaction.locale, key)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(reply, ephemeral=True)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
        except discord.HTTPException as response_error:
            logger.warning("Discord не принял ответ Civitas (HTTP %s)", response_error.status)


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
                kind = entry.kind.value.lower()
                if entry.kind == TransactionKind.TRANSFER:
                    kind = "transfer_in" if entry.delta > 0 else "transfer_out"
                label = message(locale, f"wallet.history.{kind}")
                date = discord.utils.format_dt(entry.created_at, style="f")
                lines.append(f"• {date} — {label}: {entry.delta:+d} {currency_symbol}")
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

        confirmation = TransferConfirmation(
            wallet_service=wallet_service,
            guild_id=guild_scope.id,
            sender_id=interaction.user.id,
            recipient_id=recipient.id,
            amount=amount,
            currency_symbol=currency_symbol,
            locale=interaction.locale,
        )
        await confirmation.show(interaction)
