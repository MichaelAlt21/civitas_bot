"""Discord commands for proposing, approving and viewing prosecutor appointments."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.prosecutor import (
    PROSECUTOR_REASON_MAX_LENGTH,
    ProsecutorAction,
    ProsecutorError,
    ProsecutorMotion,
    ProsecutorService,
)

from .localization import command_description, message
from .responses import send_private_lines


@asynccontextmanager
async def _prosecutor_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    locale = interaction.locale
    try:
        yield
    except CitizenshipAccessError as error:
        await send_private_lines(interaction, message(locale, f"access.{error.reason.value}"))
    except ProsecutorError as error:
        await send_private_lines(interaction, message(
            locale, f"prosecutor.error.{error.reason.value}", limit=str(PROSECUTOR_REASON_MAX_LENGTH),
        ))


def _require_guild(interaction: discord.Interaction, guild_scope: discord.Object) -> None:
    if interaction.guild_id != guild_scope.id:
        raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)


def _motion_details(locale: discord.Locale, motion: ProsecutorMotion) -> list[str]:
    action = "appointment" if motion.action is ProsecutorAction.APPOINT else "removal"
    lines = [
        message(locale, f"prosecutor.motion.{action}", id=str(motion.id), subject=f"<@{motion.subject_user_id}>"),
        message(locale, "prosecutor.approvals", count=str(motion.approval_count)),
    ]
    if motion.applied_at is not None:
        lines.append(message(
            locale, f"prosecutor.applied.{action}", date=discord.utils.format_dt(motion.applied_at, style="f"),
        ))
    return lines


def add_prosecutor_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: ProsecutorService,
) -> None:
    prosecutor = app_commands.Group(name="prosecutor", description=command_description("prosecutor"))

    @prosecutor.command(name="nominate", description=command_description("prosecutor.nominate"))
    @app_commands.describe(
        candidate=command_description("prosecutor.candidate"), reason=command_description("prosecutor.reason"),
    )
    async def nominate(
        interaction: discord.Interaction, candidate: discord.Member,
        reason: app_commands.Range[str, 1, PROSECUTOR_REASON_MAX_LENGTH],
    ) -> None:
        async with _prosecutor_response(interaction):
            _require_guild(interaction, guild_scope)
            if candidate.bot or candidate.guild.id != guild_scope.id:
                await send_private_lines(interaction, message(interaction.locale, "prosecutor.error.invalid_candidate"))
                return
            motion = await service.propose(
                interaction.guild_id, interaction.user.id, action=ProsecutorAction.APPOINT,
                candidate_user_id=candidate.id, reason=reason, idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, *_motion_details(interaction.locale, motion))

    @prosecutor.command(name="propose_removal", description=command_description("prosecutor.propose_removal"))
    @app_commands.describe(reason=command_description("prosecutor.reason"))
    async def propose_removal(
        interaction: discord.Interaction, reason: app_commands.Range[str, 1, PROSECUTOR_REASON_MAX_LENGTH],
    ) -> None:
        async with _prosecutor_response(interaction):
            _require_guild(interaction, guild_scope)
            motion = await service.propose(
                interaction.guild_id, interaction.user.id, action=ProsecutorAction.REMOVE,
                reason=reason, idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, *_motion_details(interaction.locale, motion))

    @prosecutor.command(name="approve", description=command_description("prosecutor.approve"))
    @app_commands.describe(
        motion_id=command_description("prosecutor.motion_id"), reason=command_description("prosecutor.reason"),
    )
    async def approve(
        interaction: discord.Interaction, motion_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, PROSECUTOR_REASON_MAX_LENGTH],
    ) -> None:
        async with _prosecutor_response(interaction):
            _require_guild(interaction, guild_scope)
            motion = await service.approve(interaction.guild_id, interaction.user.id, motion_id, reason=reason)
            await send_private_lines(interaction, *_motion_details(interaction.locale, motion))

    @prosecutor.command(name="current", description=command_description("prosecutor.current"))
    async def current(interaction: discord.Interaction) -> None:
        async with _prosecutor_response(interaction):
            _require_guild(interaction, guild_scope)
            motion = await service.current(interaction.guild_id)
            if motion is None:
                await send_private_lines(interaction, message(interaction.locale, "prosecutor.vacant"))
                return
            await send_private_lines(
                interaction, message(interaction.locale, "prosecutor.current"), *_motion_details(interaction.locale, motion),
            )

    @prosecutor.command(name="motion", description=command_description("prosecutor.motion"))
    @app_commands.describe(motion_id=command_description("prosecutor.motion_id"))
    async def motion_details(interaction: discord.Interaction, motion_id: app_commands.Range[int, 1]) -> None:
        async with _prosecutor_response(interaction):
            _require_guild(interaction, guild_scope)
            motion = await service.motion(interaction.guild_id, motion_id)
            await send_private_lines(interaction, *_motion_details(interaction.locale, motion))

    tree.add_command(prosecutor, guild=guild_scope)
