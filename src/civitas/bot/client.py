import logging

import discord
from discord import app_commands

from civitas.app.citizens import (
    AccessDeniedReason,
    CitizensService,
    CitizenshipAccessError,
    CitizenStatus,
    RegistrationOutcome,
)

logger = logging.getLogger(__name__)

_ACCESS_MESSAGES = {
    AccessDeniedReason.WRONG_GUILD: "Команды Civitas доступны на игровом сервере.",
    AccessDeniedReason.NOT_ALLOWED: "Ваш аккаунт пока не допущен к участию в Civitas.",
}
_STATUS_LABELS = {
    CitizenStatus.PENDING: "ожидает проверки",
    CitizenStatus.VERIFIED: "подтверждено",
    CitizenStatus.SUSPENDED: "приостановлено",
    CitizenStatus.LEFT: "неактивно после выхода с сервера",
}
_REGISTRATION_MESSAGES = {
    RegistrationOutcome.CREATED: "Гражданство оформлено.",
    RegistrationOutcome.RESTORED: "Гражданство восстановлено.",
    RegistrationOutcome.EXISTING: "Запись о вашем гражданстве уже есть.",
}


class CivitasBot(discord.Client):
    def __init__(self, *, citizens_service: CitizensService, guild_id: int) -> None:
        super().__init__(
            intents=discord.Intents.none(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.guild_scope = discord.Object(id=guild_id)
        self.tree = app_commands.CommandTree(self)
        self._add_citizen_commands(citizens_service)

    async def setup_hook(self) -> None:
        await self.tree.sync(guild=self.guild_scope)

    def _add_citizen_commands(self, service: CitizensService) -> None:
        citizen = app_commands.Group(name="citizen", description="Гражданство Civitas")
        self.tree.add_command(citizen, guild=self.guild_scope)

        @self.tree.command(
            name="start", description="Как начать игру в Civitas", guild=self.guild_scope
        )
        async def start(interaction: discord.Interaction) -> None:
            try:
                service.authorize(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.response.send_message(
                    _ACCESS_MESSAGES[error.reason], ephemeral=True
                )
                return
            await interaction.response.send_message(
                "Добро пожаловать в Civitas! Оформить гражданство можно командой "
                "`/citizen register`, проверить свой статус — командой `/profile`.",
                ephemeral=True,
            )

        @citizen.command(name="register", description="Оформить гражданство")
        async def register(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await service.register(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.followup.send(_ACCESS_MESSAGES[error.reason], ephemeral=True)
                return

            outcome = _REGISTRATION_MESSAGES[result.outcome]
            status = _STATUS_LABELS[result.citizen.status]
            await interaction.followup.send(
                f"{outcome} Статус гражданства: {status}. Команда `/profile` покажет текущий статус.",
                ephemeral=True,
            )

        @self.tree.command(
            name="profile", description="Проверить свой гражданский статус", guild=self.guild_scope
        )
        async def profile(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                record = await service.get_profile(interaction.guild_id, interaction.user.id)
            except CitizenshipAccessError as error:
                await interaction.followup.send(_ACCESS_MESSAGES[error.reason], ephemeral=True)
                return

            if record is None:
                message = "Чтобы оформить гражданство, воспользуйтесь командой `/citizen register`."
            else:
                message = f"Статус гражданства: {_STATUS_LABELS[record.status]}."
            await interaction.followup.send(message, ephemeral=True)

        @self.tree.error
        async def report_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            original = getattr(error, "original", error)
            logger.error("Ошибка выполнения команды Civitas (%s)", type(original).__name__)
            message = "Команда временно недоступна. Попробуйте ещё раз позже."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException as response_error:
                logger.warning("Discord не принял ответ Civitas (HTTP %s)", response_error.status)


def create_bot(*, citizens_service: CitizensService, guild_id: int) -> CivitasBot:
    return CivitasBot(citizens_service=citizens_service, guild_id=guild_id)
