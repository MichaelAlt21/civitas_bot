"""Discord commands for registering and reading revisions of RP laws and decrees."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.law import (
    LAW_CODE_MAX_LENGTH,
    LAW_MAX_PAGE,
    LAW_PAGE_SIZE,
    LAW_TEXT_MAX_LENGTH,
    LAW_TITLE_MAX_LENGTH,
    ActKind,
    LawError,
    LawErrorReason,
    LawRevision,
    LawService,
)

from .localization import command_description, message
from .responses import send_private_lines


_REGISTRY_ERRORS = {
    LawErrorReason.INVALID_CODE,
    LawErrorReason.INVALID_TITLE,
    LawErrorReason.INVALID_TEXT,
    LawErrorReason.REVISION_NOT_FOUND,
    LawErrorReason.INDEPENDENT_CONFIRMATION,
    LawErrorReason.OCCUPIED_EFFECTIVE_AT,
}


@asynccontextmanager
async def _law_response(interaction: discord.Interaction, *, registry: str) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    locale = interaction.locale
    try:
        yield
    except CitizenshipAccessError as error:
        await send_private_lines(interaction, message(locale, f"access.{error.reason.value}"))
    except LawError as error:
        namespace = registry if error.reason in _REGISTRY_ERRORS else "law"
        await send_private_lines(interaction, message(locale, f"{namespace}.error.{error.reason.value}"))


def _require_guild(interaction: discord.Interaction, guild_scope: discord.Object) -> None:
    if interaction.guild_id != guild_scope.id:
        raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)


def _escape(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def _revision_row(locale: discord.Locale, revision: LawRevision) -> str:
    return message(
        locale, "law.row", code=_escape(revision.code), title=_escape(revision.title), revision_id=str(revision.id),
    )


def _revision_header(locale: discord.Locale, revision: LawRevision, *, registry: str) -> tuple[str, ...]:
    state = "confirmed" if revision.confirmed_at is not None else "pending"
    return (
        _revision_row(locale, revision),
        message(locale, f"{registry}.state.{state}"),
        message(locale, "law.effective_at", date=discord.utils.format_dt(revision.effective_at, style="f")),
        message(locale, "law.source", link=f"<{revision.source_url}>"),
    )


async def _send_revision(interaction: discord.Interaction, revision: LawRevision, *, registry: str) -> None:
    # Keep each part under Discord's length limit even if every character is escaped or non-BMP.
    text_parts = (_escape(revision.text[start:start + 900]) for start in range(0, len(revision.text), 900))
    await send_private_lines(interaction, *_revision_header(interaction.locale, revision, registry=registry), *text_parts)


def add_law_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: LawService, act_kind: ActKind = ActKind.LAW,
) -> None:
    registry = act_kind.value.lower()
    group = app_commands.Group(name=registry, description=command_description(registry))

    @group.command(name="register", description=command_description(f"{registry}.register"))
    @app_commands.describe(
        code=command_description(f"{registry}.code"), title=command_description(f"{registry}.title"),
        text=command_description("law.text"), source_url=command_description(f"{registry}.source_url"),
        effective_at=command_description("law.effective_at"),
    )
    async def register(
        interaction: discord.Interaction, code: app_commands.Range[str, 1, LAW_CODE_MAX_LENGTH],
        title: app_commands.Range[str, 1, LAW_TITLE_MAX_LENGTH], text: app_commands.Range[str, 1, LAW_TEXT_MAX_LENGTH],
        source_url: str, effective_at: str,
    ) -> None:
        async with _law_response(interaction, registry=registry):
            _require_guild(interaction, guild_scope)
            revision = await service.register(
                interaction.guild_id, interaction.user.id, code=code, title=title, text=text, source_url=source_url,
                effective_at=effective_at, idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, *_revision_header(interaction.locale, revision, registry=registry))

    @group.command(name="confirm", description=command_description(f"{registry}.confirm"))
    @app_commands.describe(revision_id=command_description(f"{registry}.revision_id"))
    async def confirm(interaction: discord.Interaction, revision_id: app_commands.Range[int, 1]) -> None:
        async with _law_response(interaction, registry=registry):
            _require_guild(interaction, guild_scope)
            revision = await service.confirm(interaction.guild_id, interaction.user.id, revision_id)
            await send_private_lines(interaction, *_revision_header(interaction.locale, revision, registry=registry))

    @group.command(name="show", description=command_description(f"{registry}.show"))
    @app_commands.describe(code=command_description(f"{registry}.code"), at=command_description(f"{registry}.at"))
    async def show(
        interaction: discord.Interaction, code: app_commands.Range[str, 1, LAW_CODE_MAX_LENGTH], at: str | None = None,
    ) -> None:
        async with _law_response(interaction, registry=registry):
            _require_guild(interaction, guild_scope)
            await _send_revision(interaction, await service.show(interaction.guild_id, code, at=at), registry=registry)

    @group.command(name="revision", description=command_description(f"{registry}.revision"))
    @app_commands.describe(revision_id=command_description(f"{registry}.revision_id"))
    async def revision(interaction: discord.Interaction, revision_id: app_commands.Range[int, 1]) -> None:
        async with _law_response(interaction, registry=registry):
            _require_guild(interaction, guild_scope)
            record = await service.revision(interaction.guild_id, interaction.user.id, revision_id)
            await _send_revision(interaction, record, registry=registry)

    @group.command(name="list", description=command_description(f"{registry}.list"))
    @app_commands.describe(page=command_description("law.page"))
    async def list_effective(interaction: discord.Interaction, page: app_commands.Range[int, 1, LAW_MAX_PAGE] = 1) -> None:
        async with _law_response(interaction, registry=registry):
            _require_guild(interaction, guild_scope)
            revisions = await service.list_effective(interaction.guild_id, page=page)
            locale = interaction.locale
            if not revisions:
                await send_private_lines(interaction, message(locale, f"{registry}.list.empty", page=str(page)))
                return
            lines = [message(locale, f"{registry}.list.title", page=str(page))]
            lines.extend(_revision_row(locale, record) for record in revisions)
            if len(revisions) == LAW_PAGE_SIZE and page < LAW_MAX_PAGE:
                lines.append(message(locale, f"{registry}.list.next", page=str(page + 1)))
            await send_private_lines(interaction, *lines)

    tree.add_command(group, guild=guild_scope)
