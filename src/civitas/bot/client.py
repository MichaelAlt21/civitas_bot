from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.domain.ledger import LedgerError, LedgerErrorReason

from .guide import show_guide
from .localization import CommandTranslator, command_description, message
from .wallet import add_wallet_commands

if TYPE_CHECKING:
    from civitas.app.profile import ProfileService
    from civitas.app.wallet import WalletService

logger = logging.getLogger(__name__)


async def _report_interaction_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, LedgerError) and error.reason in (LedgerErrorReason.ECONOMY_READ_ONLY, LedgerErrorReason.IN_CUSTODY):
        reply = message(interaction.locale, f"wallet.error.{error.reason.value}")
    else:
        logger.error("Ошибка выполнения команды Civitas (%s)", type(error).__name__)
        reply = message(interaction.locale, "error.temporary")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(reply, ephemeral=True)
        else:
            await interaction.response.send_message(reply, ephemeral=True)
    except discord.HTTPException as response_error:
        logger.warning("Discord не принял ответ Civitas (HTTP %s)", response_error.status)


async def _register_citizen(service: CitizensService, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    locale = interaction.locale
    try:
        result = await service.register(interaction.guild_id, interaction.user.id)
    except CitizenshipAccessError as error:
        reply = message(locale, f"access.{error.reason.value}")
    except LedgerError as error:
        if error.reason != LedgerErrorReason.ECONOMY_READ_ONLY:
            raise
        reply = message(locale, "wallet.error.economy_read_only")
    else:
        reply = message(
            locale,
            "registration.result",
            outcome=message(locale, f"registration.{result.outcome.value}"),
            status=message(locale, f"status.{result.citizen.status.value.lower()}"),
        )
    await interaction.followup.send(reply, ephemeral=True)


class _RegistrationView(discord.ui.View):
    def __init__(self, service: CitizensService, opening: discord.Interaction, open_guide) -> None:
        super().__init__(timeout=120)
        self._service = service
        self._opening = opening
        self._open_guide = open_guide
        self.guide_button.label = message(opening.locale, "guide.title")
        self.register_button.label = message(opening.locale, "start.register_button")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == self._opening.guild_id and interaction.user.id == self._opening.user.id:
            return True
        await interaction.response.send_message(message(interaction.locale, "start.registration_owner_only"), ephemeral=True)
        return False

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _register_citizen(self._service, interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def guide_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open_guide(interaction)

    async def on_timeout(self) -> None:
        try:
            await self._opening.edit_original_response(view=None)
        except discord.HTTPException as error:
            logger.warning("Не удалось скрыть кнопку регистрации (HTTP %s)", error.status)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await _report_interaction_error(interaction, error)


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
        track_membership: bool = False,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = sync_roles or track_membership
        intents.members = track_membership
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.guild_scope = discord.Object(id=guild_id)
        self._citizens_service = citizens_service
        self.tree = app_commands.CommandTree(self)
        self._command_ids: dict[str, int] = {}
        self.guide_staff: dict[str, frozenset[int]] = {}
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
        synced = await self.tree.sync(guild=self.guild_scope)
        self._command_ids = {command.name: command.id for command in synced}

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        if not self.intents.members or payload.guild_id != self.guild_scope.id:
            return
        await self._citizens_service.mark_departed(
            payload.guild_id, payload.user.id, observed_at=datetime.now(UTC),
            actor_discord_user_id=self.user.id if self.user is not None else None,
        )

    def _add_citizen_commands(self, service: CitizensService, profile_service: ProfileService | None) -> None:
        citizen = app_commands.Group(name="citizen", description=command_description("citizen"))
        self.tree.add_command(citizen, guild=self.guild_scope)

        @self.tree.command(
            name="start", description=command_description("start"), guild=self.guild_scope
        )
        async def start(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await service.authorize(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.edit_original_response(
                    content=message(interaction.locale, f"access.{error.reason.value}"),
                )
                return
            await interaction.edit_original_response(
                content=message(interaction.locale, "start.welcome"),
                view=_RegistrationView(service, interaction, help_command.callback),
            )

        @citizen.command(name="register", description=command_description("register"))
        async def register(interaction: discord.Interaction) -> None:
            await _register_citizen(service, interaction)

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
            await show_guide(interaction, self.tree, self.guild_scope, self._command_ids, self.guide_staff)

        @self.tree.error
        async def report_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            await _report_interaction_error(interaction, getattr(error, "original", error))


def create_bot(
    *,
    citizens_service: CitizensService,
    guild_id: int,
    sync_roles: bool = False,
    wallet_service: WalletService | None = None,
    currency_symbol: str = "₵",
    profile_service: ProfileService | None = None,
    track_membership: bool = False,
) -> CivitasBot:
    return CivitasBot(
        citizens_service=citizens_service,
        guild_id=guild_id,
        sync_roles=sync_roles,
        wallet_service=wallet_service,
        currency_symbol=currency_symbol,
        profile_service=profile_service,
        track_membership=track_membership,
    )
