"""Private, guild-scoped operator commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from civitas.app.admin_audit import ADMIN_AUDIT_MAX_PAGE, ADMIN_AUDIT_PAGE_SIZE, AdminAuditError
from civitas.app.admission import ADMISSION_MAX_PAGE, ADMISSION_PAGE_SIZE, ADMISSION_REASON_MAX_LENGTH, AdmissionError
from civitas.app.citizens import CitizenshipAccessError
from civitas.app.maintenance import MAINTENANCE_REASON_MAX_LENGTH, MaintenanceError
from civitas.app.presentation import PresentationBinding, PresentationError

from .localization import command_description, message
from .responses import send_private_lines
from .roles import safe_assignable_role

if TYPE_CHECKING:
    from civitas.app.admin_audit import AdminAuditEntry, AdminAuditService
    from civitas.app.admission import AdmissionService
    from civitas.app.maintenance import MaintenanceService
    from civitas.app.presentation import PresentationService


_ROLE_BINDINGS = {
    "Citizen": PresentationBinding.CITIZEN_ROLE,
    "President": PresentationBinding.PRESIDENT_ROLE,
    "Senator": PresentationBinding.PARLIAMENT_ROLE,
}


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


def _error_message(
    locale: discord.Locale,
    error: AdminAuditError | AdmissionError | MaintenanceError | CitizenshipAccessError | PresentationError,
) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    if isinstance(error, AdmissionError):
        return message(locale, f"admin.admission.error.{error.reason.value}", limit=str(ADMISSION_REASON_MAX_LENGTH))
    if isinstance(error, PresentationError):
        return message(locale, f"admin.setup.error.{error.reason.value}")
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


async def _set_admission(
    interaction: discord.Interaction, service: AdmissionService, user_id: int, *, allowed: bool, reason: str = "",
) -> None:
    locale = interaction.locale
    try:
        changed = await service.set_allowed(
            interaction.guild_id, interaction.user.id, administrator=interaction.permissions.administrator,
            target_user_id=user_id, allowed=allowed, reason=reason,
        )
    except (AdmissionError, CitizenshipAccessError) as error:
        await send_private_lines(interaction, _error_message(locale, error))
        return

    action = "add" if allowed else "remove"
    outcome = "changed" if changed else "unchanged"
    await send_private_lines(interaction, message(locale, f"admin.admission.{action}.{outcome}", user_id=str(user_id)))


def _add_admission(admin: app_commands.Group, service: AdmissionService) -> None:
    admission = app_commands.Group(name="admission", description=command_description("admin.admission"))

    @admission.command(name="add", description=command_description("admin.admission.add"))
    @app_commands.describe(member=command_description("admin.admission.add.member"))
    async def add(interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if member.bot or member.guild.id != interaction.guild_id:
            await send_private_lines(interaction, message(interaction.locale, "admin.admission.error.invalid_target"))
            return
        await _set_admission(interaction, service, member.id, allowed=True)

    @admission.command(name="remove", description=command_description("admin.admission.remove"))
    @app_commands.describe(
        member=command_description("admin.admission.remove.member"),
        user_id=command_description("admin.admission.remove.user_id"),
        reason=command_description("admin.admission.remove.reason"),
    )
    async def remove(
        interaction: discord.Interaction, reason: app_commands.Range[str, 1, ADMISSION_REASON_MAX_LENGTH],
        member: discord.Member | None = None, user_id: app_commands.Range[str, 1, 20] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if member is not None and user_id is None:
            target = member.id
        elif member is None and user_id is not None and user_id.isascii() and user_id.isdecimal():
            target = int(user_id)
        else:
            await send_private_lines(interaction, message(interaction.locale, "admin.admission.error.remove_target"))
            return
        await _set_admission(interaction, service, target, allowed=False, reason=reason)

    @admission.command(name="list", description=command_description("admin.admission.list"))
    @app_commands.describe(page=command_description("admin.admission.list.page"))
    async def list_allowed(
        interaction: discord.Interaction, page: app_commands.Range[int, 1, ADMISSION_MAX_PAGE] = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            user_ids = await service.list_allowed(
                interaction.guild_id, interaction.user.id, administrator=interaction.permissions.administrator, page=page,
            )
        except (AdmissionError, CitizenshipAccessError) as error:
            await send_private_lines(interaction, _error_message(locale, error))
            return
        if not user_ids:
            await send_private_lines(interaction, message(locale, "admin.admission.list.empty", page=str(page)))
            return

        parts = [message(locale, "admin.admission.list.title", page=str(page))]
        parts.extend(f"<@{user_id}> · `{user_id}`" for user_id in user_ids)
        if len(user_ids) == ADMISSION_PAGE_SIZE and page < ADMISSION_MAX_PAGE:
            parts.append(message(locale, "admin.admission.list.next", page=str(page + 1)))
        await send_private_lines(interaction, *parts)

    admin.add_command(admission)


def _binding_line(locale: discord.Locale, binding: PresentationBinding, target_id: int | None) -> str:
    label = message(locale, f"admin.setup.binding.{binding.value}")
    if target_id is None:
        target = message(locale, "admin.setup.unset")
    elif binding is PresentationBinding.REPORT_CHANNEL:
        target = f"<#{target_id}>"
    else:
        target = f"<@&{target_id}>"
    return f"{label} · {target}"


async def _set_binding(
    interaction: discord.Interaction, service: PresentationService, binding: PresentationBinding, value: int | None,
) -> None:
    locale = interaction.locale
    try:
        changed = await service.set_binding(
            interaction.guild_id, interaction.user.id, administrator=interaction.permissions.administrator,
            binding=binding, value=value,
        )
    except (AdmissionError, CitizenshipAccessError, PresentationError) as error:
        await send_private_lines(interaction, _error_message(locale, error))
        return

    key = "admin.setup.changed" if changed else "admin.setup.unchanged"
    parts = [message(locale, key), _binding_line(locale, binding, value)]
    if changed and binding is not PresentationBinding.REPORT_CHANNEL:
        parts.append(message(locale, "admin.setup.previous_role"))
    await send_private_lines(interaction, *parts)


def _add_setup(admin: app_commands.Group, service: PresentationService) -> None:
    setup = app_commands.Group(name="setup", description=command_description("admin.setup"))

    @setup.command(name="status", description=command_description("admin.setup.status"))
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        try:
            settings = await service.status(
                interaction.guild_id, interaction.user.id, administrator=interaction.permissions.administrator,
            )
        except (AdmissionError, CitizenshipAccessError, PresentationError) as error:
            await send_private_lines(interaction, _error_message(locale, error))
            return

        await send_private_lines(
            interaction, message(locale, "admin.setup.title"),
            *(_binding_line(locale, binding, getattr(settings, binding.value)) for binding in PresentationBinding),
        )

    @setup.command(name="role", description=command_description("admin.setup.role"))
    @app_commands.describe(
        kind=command_description("admin.setup.role.kind"), role=command_description("admin.setup.role.role"),
    )
    @app_commands.choices(kind=[app_commands.Choice(name=name, value=binding.value) for name, binding in _ROLE_BINDINGS.items()])
    async def set_role(interaction: discord.Interaction, kind: app_commands.Choice[str], role: discord.Role | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if role is not None and (
            interaction.guild is None or role.guild.id != interaction.guild_id
            or safe_assignable_role(interaction.guild, role.id) is None
        ):
            await send_private_lines(interaction, message(interaction.locale, "admin.setup.error.unsafe_role"))
            return
        await _set_binding(interaction, service, PresentationBinding(kind.value), role.id if role is not None else None)

    @setup.command(name="report_channel", description=command_description("admin.setup.report_channel"))
    @app_commands.describe(channel=command_description("admin.setup.report_channel.channel"))
    async def report_channel(interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        locale = interaction.locale
        if channel is not None:
            bot_member = interaction.guild.me if interaction.guild is not None else None
            if channel.guild.id != interaction.guild_id or bot_member is None:
                await send_private_lines(interaction, message(locale, "admin.setup.error.invalid_channel"))
                return
            permissions = channel.permissions_for(bot_member)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                await send_private_lines(interaction, message(locale, "admin.setup.error.channel_permissions"))
                return
        await _set_binding(
            interaction, service, PresentationBinding.REPORT_CHANNEL, channel.id if channel is not None else None,
        )

    admin.add_command(setup)


def add_admin_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object,
    maintenance_service: MaintenanceService | None = None,
    audit_service: AdminAuditService | None = None,
    admission_service: AdmissionService | None = None,
    presentation_service: PresentationService | None = None,
) -> None:
    if all(service is None for service in (maintenance_service, audit_service, admission_service, presentation_service)):
        return

    admin = app_commands.Group(name="admin", description=command_description("admin"))
    if maintenance_service is not None:
        _add_maintenance(admin, maintenance_service)
    if audit_service is not None:
        _add_audit(admin, audit_service)
    if admission_service is not None:
        _add_admission(admin, admission_service)
    if presentation_service is not None:
        _add_setup(admin, presentation_service)
    tree.add_command(admin, guild=guild_scope)
