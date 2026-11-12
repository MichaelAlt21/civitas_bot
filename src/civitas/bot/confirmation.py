"""Private, actor-bound Discord confirmation for an idempotent operation."""

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

import discord

from .localization import message

logger = logging.getLogger(__name__)
_NO_MENTIONS = discord.AllowedMentions.none()


class OperationConfirmation(discord.ui.View):
    def __init__(
        self,
        *,
        guild_id: int,
        actor_id: int,
        locale: discord.Locale,
        commit: Callable[[discord.Interaction, str], Awaitable[str]],
        message_prefix: str = "confirmation",
    ) -> None:
        super().__init__(timeout=120)
        self._guild_id = guild_id
        self._actor_id = actor_id
        self._locale = locale
        self._commit = commit
        self._message_prefix = message_prefix
        self._idempotency_key = uuid4().hex
        self._processing = False
        self._submitted = False
        self._prompt: discord.InteractionMessage | None = None
        self.confirm.label = self._message(locale, "confirm")
        self.cancel.label = self._message(locale, "cancel")

    def _message(self, locale: discord.Locale, suffix: str) -> str:
        return message(locale, f"{self._message_prefix}.{suffix}")

    async def show(self, interaction: discord.Interaction, prompt: str) -> None:
        self._prompt = await interaction.edit_original_response(
            content=prompt,
            view=self,
            allowed_mentions=_NO_MENTIONS,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == self._guild_id and interaction.user.id == self._actor_id:
            return True
        await interaction.response.send_message(
            self._message(interaction.locale, "wrong_actor"), ephemeral=True
        )
        return False

    async def _begin_action(self, interaction: discord.Interaction, *, cancelling: bool) -> bool:
        if self.is_finished():
            suffix = "expired"
        elif self._processing:
            suffix = "processing"
        elif cancelling and self._submitted:
            suffix = "retry"
        else:
            self._processing = True
            return True
        await interaction.response.send_message(self._message(interaction.locale, suffix), ephemeral=True)
        return False

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._begin_action(interaction, cancelling=False):
            return
        try:
            await interaction.response.defer()
            self._submitted = True
            reply = await self._commit(interaction, self._idempotency_key)
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
                content=self._message(interaction.locale, "cancelled"), view=None
            )
            self.stop()
        finally:
            self._processing = False

    async def on_timeout(self) -> None:
        if self._prompt is None or self._processing:
            return
        try:
            await self._prompt.edit(content=self._message(self._locale, "expired"), view=None)
        except discord.HTTPException as error:
            logger.warning("Не удалось обновить подтверждение операции (HTTP %s)", error.status)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.error("Ошибка подтверждения операции (%s)", type(error).__name__)
        if self._submitted:
            reply = self._message(interaction.locale, "retry")
        else:
            reply = message(interaction.locale, "error.temporary")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(reply, ephemeral=True)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
        except discord.HTTPException as response_error:
            logger.warning("Discord не принял ответ Civitas (HTTP %s)", response_error.status)
