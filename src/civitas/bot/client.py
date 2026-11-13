from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from civitas.app.citizens import CitizensService, CitizenshipAccessError

from .localization import CommandTranslator, command_description, message
from .wallet import add_wallet_commands

if TYPE_CHECKING:
    from civitas.app.profile import ProfileService
    from civitas.app.wallet import WalletService

logger = logging.getLogger(__name__)


class CivitasBot(discord.Client):
    def __init__(
        self,
        *,
        citizens_service: CitizensService,
        guild_id: int,
        sync_roles: bool = False,
        wallet_service: WalletService | None = None,
        currency_symbol: str = "₵",
        profile_service: ProfileService | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = sync_roles
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.guild_scope = discord.Object(id=guild_id)
        self.tree = app_commands.CommandTree(self)
        self._add_citizen_commands(citizens_service, profile_service)
        if wallet_service is not None:
            add_wallet_commands(
                self.tree,
                guild_scope=self.guild_scope,
                wallet_service=wallet_service,
                currency_symbol=currency_symbol,
            )

    async def setup_hook(self) -> None:
        await self.tree.set_translator(CommandTranslator())
        await self.tree.sync(guild=self.guild_scope)

    def _add_citizen_commands(self, service: CitizensService, profile_service: ProfileService | None) -> None:
        citizen = app_commands.Group(name="citizen", description=command_description("citizen"))
        self.tree.add_command(citizen, guild=self.guild_scope)

        @self.tree.command(
            name="start", description=command_description("start"), guild=self.guild_scope
        )
        async def start(interaction: discord.Interaction) -> None:
            try:
                service.authorize(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.response.send_message(
                    message(interaction.locale, f"access.{error.reason.value}"), ephemeral=True
                )
                return
            await interaction.response.send_message(
                message(interaction.locale, "start.welcome"),
                ephemeral=True,
            )

        @citizen.command(name="register", description=command_description("register"))
        async def register(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await service.register(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.followup.send(
                    message(interaction.locale, f"access.{error.reason.value}"), ephemeral=True
                )
                return

            locale = interaction.locale
            await interaction.followup.send(
                message(
                    locale,
                    "registration.result",
                    outcome=message(locale, f"registration.{result.outcome.value}"),
                    status=message(locale, f"status.{result.citizen.status.value.lower()}"),
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="profile", description=command_description("profile"), guild=self.guild_scope
        )
        async def profile(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                details = None
                if profile_service is None:
                    record = await service.get_profile(interaction.guild_id, interaction.user.id)
                else:
                    details = await profile_service.get_profile(interaction.guild_id, interaction.user.id)
                    record = details.citizen if details else None
            except CitizenshipAccessError as error:
                await interaction.followup.send(
                    message(interaction.locale, f"access.{error.reason.value}"), ephemeral=True
                )
                return

            locale = interaction.locale
            if record is None:
                reply = message(locale, "profile.missing")
            else:
                status = message(locale, f"status.{record.status.value.lower()}")
                reply = message(locale, "profile.status", status=status)
            if details is not None:
                sections = (
                    ("offices", tuple(message(locale, f"profile.office.{office.value.lower()}") for office in details.offices)),
                    ("companies", details.companies), ("assets", details.assets),
                )
                for key, values in sections:
                    shown = ", ".join(discord.utils.escape_markdown(name) for name in values) or message(locale, "profile.none")
                    reply += "\n" + message(locale, f"profile.{key}", values=shown)
            await interaction.followup.send(reply, ephemeral=True)

        @self.tree.command(name="help", description=command_description("help"), guild=self.guild_scope)
        async def help_command(interaction: discord.Interaction) -> None:
            if interaction.guild_id != self.guild_scope.id:
                await interaction.response.send_message(message(interaction.locale, "access.wrong_guild"), ephemeral=True)
                return
            visible = []
            for command in self.tree.walk_commands(guild=self.guild_scope):
                if isinstance(command, app_commands.Command) and command.name != "help":
                    if not ({"staff", "commission"} & set(command.qualified_name.split())):
                        visible.append(f"`/{command.qualified_name}`")
            await interaction.response.send_message(
                message(interaction.locale, "help.commands", commands=", ".join(visible)), ephemeral=True,
            )

        @self.tree.error
        async def report_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            original = getattr(error, "original", error)
            logger.error("Ошибка выполнения команды Civitas (%s)", type(original).__name__)
            reply = message(interaction.locale, "error.temporary")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(reply, ephemeral=True)
                else:
                    await interaction.response.send_message(reply, ephemeral=True)
            except discord.HTTPException as response_error:
                logger.warning("Discord не принял ответ Civitas (HTTP %s)", response_error.status)


def create_bot(
    *,
    citizens_service: CitizensService,
    guild_id: int,
    sync_roles: bool = False,
    wallet_service: WalletService | None = None,
    currency_symbol: str = "₵",
    profile_service: ProfileService | None = None,
) -> CivitasBot:
    return CivitasBot(
        citizens_service=citizens_service,
        guild_id=guild_id,
        sync_roles=sync_roles,
        wallet_service=wallet_service,
        currency_symbol=currency_symbol,
        profile_service=profile_service,
    )
