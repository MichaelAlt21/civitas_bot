"""Discord commands for public court appeals and commission certification."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.court_appeals import CourtAppeal, CourtAppealError, CourtAppealService

from .localization import command_description, message
from .responses import send_private_lines

_VOTER_PAGE_SIZE = 20
_FINE_INTERVAL_SECONDS = 30
logger = logging.getLogger(__name__)


@asynccontextmanager
async def run_court_fines(client: discord.Client, service: CourtAppealService | None) -> AsyncIterator[None]:
    if service is None:
        yield
        return

    async def run() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await service.reconcile_fines()
            except Exception as error:
                logger.warning("Ошибка обработки судебных штрафов (%s)", type(error).__name__)
            await asyncio.sleep(_FINE_INTERVAL_SECONDS)

    task = asyncio.create_task(run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def _appeal_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except CitizenshipAccessError as error:
        await send_private_lines(interaction, message(interaction.locale, f"access.{error.reason.value}"))
    except CourtAppealError as error:
        await send_private_lines(interaction, message(interaction.locale, f"court.appeal.error.{error.reason.value}"))


def _require_guild(interaction: discord.Interaction, guild_scope: discord.Object) -> None:
    if interaction.guild_id != guild_scope.id:
        raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)


def _appeal_details(locale: discord.Locale, appeal: CourtAppeal) -> list[str]:
    rows = [
        message(locale, "court.appeal.title", id=str(appeal.case_id)),
        message(locale, "court.appeal.filed", date=discord.utils.format_dt(appeal.filed_at, style="f")),
    ]
    if appeal.source_url is None or appeal.closes_at is None:
        rows.append(message(locale, "court.appeal.awaiting_open"))
        return rows

    source = appeal.source_url.replace("<", "%3C").replace(">", "%3E")
    rows.extend((
        message(locale, "court.appeal.source", url=source),
        message(
            locale, "court.appeal.voting", round=str(appeal.round_number),
            date=discord.utils.format_dt(appeal.closes_at, style="f"),
        ),
        message(locale, "court.appeal.quorum", eligible=str(appeal.eligible_count), quorum=str(appeal.quorum)),
    ))
    if appeal.participants is not None and appeal.votes_to_cancel is not None:
        rows.append(message(
            locale, "court.appeal.certified_tally", participants=str(appeal.participants),
            votes=str(appeal.votes_to_cancel),
        ))
    if appeal.outcome is not None:
        rows.append(message(locale, f"court.appeal.outcome.{appeal.outcome.value.lower()}"))
    elif appeal.round_number == 2:
        rows.append(message(locale, "court.appeal.outcome.extended"))
    if appeal.resolved_at is not None:
        rows.append(message(locale, "court.appeal.resolved", date=discord.utils.format_dt(appeal.resolved_at, style="f")))
    return rows


async def _send_voter_page(
    interaction: discord.Interaction, case_id: int, snapshot: tuple[int, ...], page: int,
) -> None:
    locale = interaction.locale
    offset = (page - 1) * _VOTER_PAGE_SIZE
    ids = snapshot[offset:offset + _VOTER_PAGE_SIZE]
    if not ids:
        await send_private_lines(interaction, message(locale, "election.voters.empty", page=str(page)))
        return
    pages = (len(snapshot) + _VOTER_PAGE_SIZE - 1) // _VOTER_PAGE_SIZE
    await send_private_lines(
        interaction,
        message(locale, "court.appeal.voters", id=str(case_id), count=str(len(snapshot)), page=str(page), pages=str(pages)),
        "```\n" + "\n".join(str(user_id) for user_id in ids) + "\n```",
    )


def add_appeal_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: CourtAppealService,
) -> None:
    court = tree.get_command("court", guild=guild_scope)
    if not isinstance(court, app_commands.Group):
        raise ValueError("Register the court group before appeal commands")
    appeal = app_commands.Group(name="appeal", description=command_description("court.appeal"))

    @appeal.command(name="file", description=command_description("court.appeal.file"))
    @app_commands.describe(case_id=command_description("court.case.id"))
    async def file(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.file(interaction.guild_id, interaction.user.id, case_id)
            await send_private_lines(interaction, *_appeal_details(interaction.locale, record))

    @appeal.command(name="open", description=command_description("court.appeal.open"))
    @app_commands.describe(
        case_id=command_description("court.case.id"), source_url=command_description("court.appeal.source_url"),
    )
    async def open_vote(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1],
        source_url: app_commands.Range[str, 1, 500],
    ) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.open(interaction.guild_id, interaction.user.id, case_id, source_url=source_url)
            await send_private_lines(interaction, *_appeal_details(interaction.locale, record))

    @appeal.command(name="voters", description=command_description("court.appeal.voters"))
    @app_commands.describe(
        case_id=command_description("court.case.id"), page=command_description("election.voters.page"),
    )
    async def voters(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1], page: app_commands.Range[int, 1] = 1,
    ) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            snapshot = await service.voters(interaction.guild_id, interaction.user.id, case_id)
            await _send_voter_page(interaction, case_id, snapshot, page)

    @appeal.command(name="record", description=command_description("court.appeal.record"))
    @app_commands.describe(
        case_id=command_description("court.case.id"), round_number=command_description("court.appeal.round"),
        participants=command_description("court.appeal.participants"),
        votes_to_cancel=command_description("court.appeal.votes_to_cancel"),
    )
    async def record_tally(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1],
        round_number: app_commands.Range[int, 1, 2], participants: app_commands.Range[int, 0],
        votes_to_cancel: app_commands.Range[int, 0],
    ) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            tally = await service.record(
                interaction.guild_id, interaction.user.id, case_id, round_number=round_number,
                participants=participants, votes_to_cancel=votes_to_cancel,
            )
            await send_private_lines(interaction, message(
                interaction.locale, "court.appeal.recorded", id=str(tally.case_id), round=str(tally.round_number),
                participants=str(tally.participants), votes=str(tally.votes_to_cancel),
            ))

    @appeal.command(name="confirm", description=command_description("court.appeal.confirm"))
    @app_commands.describe(case_id=command_description("court.case.id"), round_number=command_description("court.appeal.round"))
    async def confirm(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1], round_number: app_commands.Range[int, 1, 2],
    ) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.confirm(interaction.guild_id, interaction.user.id, case_id, round_number=round_number)
            await send_private_lines(interaction, *_appeal_details(interaction.locale, record))

    @appeal.command(name="show", description=command_description("court.appeal.show"))
    @app_commands.describe(case_id=command_description("court.case.id"))
    async def show(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        async with _appeal_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.show(interaction.guild_id, case_id)
            await send_private_lines(interaction, *_appeal_details(interaction.locale, record))

    court.add_command(appeal)
