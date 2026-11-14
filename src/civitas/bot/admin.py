"""Private, guild-scoped operator commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from civitas.app.admin_audit import ADMIN_AUDIT_MAX_PAGE, ADMIN_AUDIT_PAGE_SIZE, AdminAuditError
from civitas.app.citizens import CitizenshipAccessError
from civitas.app.maintenance import MAINTENANCE_REASON_MAX_LENGTH, MaintenanceError

from .localization import command_description, message
from .responses import send_private_lines

if TYPE_CHECKING:
    from civitas.app.admin_audit import AdminAuditEntry, AdminAuditService
    from civitas.app.maintenance import MaintenanceService


def _audit_code(value: str) -> str:
    code = " ".join(value.split())[:100].replace("<", "‹").replace(">", "›")
    return discord.utils.escape_mentions(discord.utils.escape_markdown(code))


def _audit_label(locale: discord.Locale, field: str, value: str) -> str:
    try:
        return message(locale, f"admin.audit.{field}.{value}")
    except KeyError:
        return _audit_code(value)


def _audit_row(locale: discord.Locale, entry: AdminAuditEntry) -> str:
    subject = _audit_label(locale, "subject", entry.subject_kind)
    if entry.subject_id is not None:
        subject = f"{subject} #{entry.subject_id}"

    row = message(
        locale, "admin.audit.row", date=discord.utils.format_dt(entry.occurred_at, style="f"),
        action=_audit_label(locale, "action", entry.action), actor=str(entry.actor_discord_user_id),
        subject=subject, result=_audit_label(locale, "result", entry.result),
    )
    if entry.reason_code:
        reason = message(locale, "admin.audit.reason", code=_audit_code(entry.reason_code))
        return f"{row} · {reason}"
    return row


def _error_message(locale: discord.Locale, error: AdminAuditError | MaintenanceError | CitizenshipAccessError) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    return message(locale, f"admin.error.{error.reason.value}", limit=str(MAINTENANCE_REASON_MAX_LENGTH))


def _add_maintenance(admin: app_commands.Group, service: MaintenanceService) -> None:
    @admin.command(name="maintenance", description=command_description("admin.maintenance"))
    @app_commands.describe(
        enabled=command_description("admin.maintenance.enabled"), reason=command_description("admin.maintenance.reason"),
    )
    async def maintenance(
        interaction: discord.Interaction, enabled: bool | None = None,
        reason: app_commands.Range[str, 1, MAINTENANCE_REASON_MAX_LENGTH] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        guild_id, user_id = interaction.guild_id, interaction.user.id
        try:
            if enabled is None:
                active = await service.status(guild_id, user_id)
                parts = []
            else:
                result = await service.set_read_only(guild_id, user_id, enabled=enabled, reason=reason or "")
                active = result.enabled
                key = "admin.maintenance.changed" if result.changed else "admin.maintenance.unchanged"
                parts = [message(locale, key)]
        except (MaintenanceError, CitizenshipAccessError) as error:
            await send_private_lines(interaction, _error_message(locale, error))
            return

        key = "admin.maintenance.enabled" if active else "admin.maintenance.disabled"
        parts.append(message(locale, key))
        await send_private_lines(interaction, *parts)


def _add_audit(admin: app_commands.Group, service: AdminAuditService) -> None:
    @admin.command(name="audit", description=command_description("admin.audit"))
    @app_commands.describe(page=command_description("admin.audit.page"))
    async def audit(
        interaction: discord.Interaction, page: app_commands.Range[int, 1, ADMIN_AUDIT_MAX_PAGE] = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            records = await service.list_recent(interaction.guild_id, interaction.user.id, page=page)
        except (AdminAuditError, CitizenshipAccessError) as error:
            await send_private_lines(interaction, _error_message(locale, error))
            return
        if not records:
            await send_private_lines(interaction, message(locale, "admin.audit.empty", page=str(page)))
            return

        parts = [message(locale, "admin.audit.title", page=str(page))]
        parts.extend(_audit_row(locale, entry) for entry in records)
        if len(records) == ADMIN_AUDIT_PAGE_SIZE and page < ADMIN_AUDIT_MAX_PAGE:
            parts.append(message(locale, "admin.audit.next", page=str(page + 1)))
        await send_private_lines(interaction, *parts)


def add_admin_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object,
    maintenance_service: MaintenanceService | None = None,
    audit_service: AdminAuditService | None = None,
) -> None:
    if maintenance_service is None and audit_service is None:
        return

    admin = app_commands.Group(name="admin", description=command_description("admin"))
    if maintenance_service is not None:
        _add_maintenance(admin, maintenance_service)
    if audit_service is not None:
        _add_audit(admin, audit_service)
    tree.add_command(admin, guild=guild_scope)
