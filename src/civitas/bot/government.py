"""Discord views of manual election protocols and current public offices."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError, is_discord_id
from civitas.app.government import GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.domain.government import Election, OfficeKind, OfficeTerm

from .localization import command_description, message
from .responses import send_lines, send_private_lines
from .roles import safe_assignable_role, set_member_role

logger = logging.getLogger(__name__)
_SYNC_INTERVAL_SECONDS = 60
_CANDIDATE_ID = re.compile(r"(?:([0-9]{1,20})|<@!?([0-9]{1,20})>)")
_VOTE_COUNT = re.compile(r"[0-9]{1,10}")


def _candidate_id(text: str, reason: GovernmentErrorReason) -> int:
    match = _CANDIDATE_ID.fullmatch(text.strip())
    if match is None:
        raise GovernmentError(reason)
    identifier = int(match.group(1) or match.group(2))
    if not is_discord_id(identifier):
        raise GovernmentError(reason)
    return identifier


def _candidates(text: str) -> tuple[int, ...]:
    reason = GovernmentErrorReason.INVALID_CANDIDATES
    candidates = tuple(_candidate_id(part, reason) for part in text.split(","))
    if len(set(candidates)) != len(candidates):
        raise GovernmentError(reason)
    return candidates


def _tallies(text: str) -> dict[int, int]:
    tallies = {}
    reason = GovernmentErrorReason.INVALID_TALLY
    for entry in text.split(","):
        candidate, _, votes = entry.partition("=")
        identifier = _candidate_id(candidate, reason)
        votes = votes.strip()
        if identifier in tallies or _VOTE_COUNT.fullmatch(votes) is None:
            raise GovernmentError(reason)
        tallies[identifier] = int(votes)
    return tallies


def _office_choices() -> list[app_commands.Choice[str]]:
    choices = []
    for office in OfficeKind:
        key = f"government.office.{office.value.lower()}"
        name = app_commands.locale_str(message(None, key), key=key)
        choices.append(app_commands.Choice(name=name, value=office.value))
    return choices


def _office_row(locale: discord.Locale, term: OfficeTerm) -> str:
    if term.office == OfficeKind.PARLIAMENT:
        office = message(locale, "government.office.parliament_seat", seat=str(term.seat))
    else:
        office = message(locale, "government.office.president")
    return message(
        locale, "government.offices.row", office=office, holder=f"<@{term.holder_discord_user_id}>",
        start=discord.utils.format_dt(term.starts_at, style="f"),
        end=discord.utils.format_dt(term.ends_at, style="f"),
    )


def _election_row(locale: discord.Locale, election: Election) -> str:
    return message(
        locale, "election.results.row", id=str(election.id),
        office=message(locale, f"government.office.{election.office.value.lower()}"),
        status=message(locale, f"election.status.{election.status.value.lower()}"),
    )


def _election_details(locale: discord.Locale, election: Election) -> list[str]:
    rows = [
        _election_row(locale, election),
        message(
            locale, "election.detail.window", start=discord.utils.format_dt(election.opens_at, style="f"),
            end=discord.utils.format_dt(election.extended_until or election.closes_at, style="f"),
        ),
        message(
            locale, "election.detail.eligible", count=str(len(election.eligible_citizen_ids)),
            quorum=str(election.quorum),
        ),
    ]
    if election.runoff_of_id is not None:
        rows.append(message(locale, "election.detail.runoff", id=str(election.runoff_of_id)))

    protocol = election.protocol
    if protocol is None:
        if election.candidate_discord_user_ids:
            rows.append(message(locale, "election.detail.candidates"))
            rows.extend(f"<@{user_id}>" for user_id in election.candidate_discord_user_ids)
    else:
        source = protocol.source_url.replace("<", "%3C").replace(">", "%3E")
        rows.extend((
            message(locale, "election.detail.source", url=source),
            message(locale, "election.detail.participants", count=str(protocol.participants)),
            message(locale, "election.detail.tally"),
        ))
        rows.extend(f"<@{tally.candidate_discord_user_id}> · {tally.votes}" for tally in protocol.tallies)
        winners = set(protocol.winners)
        if winners and election.certified_at is not None:
            rows.append(message(locale, "election.detail.winners"))
            rows.extend(
                f"<@{tally.candidate_discord_user_id}>" for tally in protocol.tallies
                if tally.candidate_citizen_id in winners
            )

    if election.certified_at is not None:
        rows.append(message(
            locale, "election.detail.certified", date=discord.utils.format_dt(election.certified_at, style="f")
        ))
    return rows


@asynccontextmanager
async def _commission_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except CitizenshipAccessError as error:
        await send_private_lines(interaction, message(interaction.locale, f"access.{error.reason.value}"))
    except GovernmentError as error:
        await send_private_lines(interaction, message(interaction.locale, f"election.error.{error.reason.value}"))


async def _in_guild(interaction: discord.Interaction, guild_scope: discord.Object) -> bool:
    if interaction.guild_id == guild_scope.id:
        return True
    await interaction.response.send_message(message(interaction.locale, "access.wrong_guild"), ephemeral=True)
    return False


def add_government_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, government_service: GovernmentService,
) -> None:
    government = app_commands.Group(name="government", description=command_description("government"))
    election = app_commands.Group(name="election", description=command_description("election"))
    commission = app_commands.Group(name="commission", description=command_description("election.commission"))
    election.add_command(commission)
    tree.add_command(government, guild=guild_scope)
    tree.add_command(election, guild=guild_scope)

    @government.command(name="offices", description=command_description("government.offices"))
    async def offices(interaction: discord.Interaction) -> None:
        if not await _in_guild(interaction, guild_scope):
            return
        await interaction.response.defer(thinking=True)
        locale = interaction.locale
        terms = await government_service.list_offices(interaction.guild_id, interaction.user.id)
        if not terms:
            await send_lines(interaction, message(locale, "government.offices.empty"), ephemeral=False)
            return
        await send_lines(
            interaction, message(locale, "government.offices.title"),
            *(_office_row(locale, term) for term in terms), ephemeral=False,
        )

    @election.command(name="results", description=command_description("election.results"))
    @app_commands.describe(election_id=command_description("election.id"))
    async def results(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        if not await _in_guild(interaction, guild_scope):
            return
        await interaction.response.defer(thinking=True)
        locale = interaction.locale
        if election_id is not None:
            try:
                record = await government_service.get_public_result(interaction.guild_id, interaction.user.id, election_id)
            except GovernmentError as error:
                if error.reason not in (GovernmentErrorReason.ELECTION_NOT_FOUND, GovernmentErrorReason.RESULT_NOT_PUBLIC):
                    raise
                await send_lines(interaction, message(locale, "election.error.result_not_public"), ephemeral=False)
                return
            await send_lines(interaction, *_election_details(locale, record), ephemeral=False)
            return

        records = await government_service.list_results(interaction.guild_id, interaction.user.id)
        if not records:
            await send_lines(interaction, message(locale, "election.results.empty"), ephemeral=False)
            return
        await send_lines(
            interaction, message(locale, "election.results.title"), *(_election_row(locale, record) for record in records),
            message(locale, "election.results.inspect_hint"), ephemeral=False,
        )

    @commission.command(name="open", description=command_description("election.commission.open"))
    @app_commands.choices(office=_office_choices())
    @app_commands.describe(
        office=command_description("election.office"), candidates=command_description("election.candidates"),
        reason=command_description("election.reason"), runoff_of_id=command_description("election.runoff_of_id"),
    )
    async def open_election(
        interaction: discord.Interaction,
        office: str,
        candidates: app_commands.Range[str, 1, 4000],
        reason: app_commands.Range[str, 1, 200],
        runoff_of_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        async with _commission_response(interaction):
            record = await government_service.open_election(
                interaction.guild_id, interaction.user.id, office=OfficeKind(office),
                candidate_user_ids=_candidates(candidates), reason=reason,
                idempotency_key=str(interaction.id), runoff_of_id=runoff_of_id,
            )
            await send_private_lines(interaction, *_election_details(interaction.locale, record))

    @commission.command(name="record", description=command_description("election.commission.record"))
    @app_commands.describe(
        election_id=command_description("election.id"), source_url=command_description("election.source_url"),
        participants=command_description("election.participants"), tally=command_description("election.tally"),
        reason=command_description("election.reason"),
    )
    async def record_protocol(
        interaction: discord.Interaction,
        election_id: app_commands.Range[int, 1],
        source_url: app_commands.Range[str, 1, 500],
        participants: app_commands.Range[int, 0],
        tally: app_commands.Range[str, 1, 4000],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            record = await government_service.record_protocol(
                interaction.guild_id, interaction.user.id, election_id, source_url=source_url,
                participants=participants, tallies=_tallies(tally), reason=reason,
            )
            await send_private_lines(interaction, *_election_details(interaction.locale, record))

    @commission.command(name="inspect", description=command_description("election.commission.inspect"))
    @app_commands.describe(election_id=command_description("election.id"))
    async def inspect_election(interaction: discord.Interaction, election_id: app_commands.Range[int, 1]) -> None:
        async with _commission_response(interaction):
            record = await government_service.get_election(interaction.guild_id, interaction.user.id, election_id)
            await send_private_lines(interaction, *_election_details(interaction.locale, record))

    @commission.command(name="certify", description=command_description("election.commission.certify"))
    @app_commands.describe(election_id=command_description("election.id"), reason=command_description("election.reason"))
    async def certify_election(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            record = await government_service.certify_election(
                interaction.guild_id, interaction.user.id, election_id, reason=reason,
            )
            await send_private_lines(interaction, *_election_details(interaction.locale, record))


async def _reconcile_office_roles(
    client: discord.Client, service: GovernmentService, guild_id: int, role_ids: dict[OfficeKind, int],
) -> None:
    guild = client.get_guild(guild_id)
    if guild is None or guild.unavailable:
        logger.warning("Сервер Discord недоступен для синхронизации должностей")
        return

    roles = {}
    for office, role_id in role_ids.items():
        role = safe_assignable_role(guild, role_id)
        if role is None:
            logger.warning("Проверьте ID роли, иерархию и права Discord для должности %s", office.value)
            continue
        roles[office] = role
    if not roles:
        return

    for assignment in await service.office_role_assignments():
        role = roles.get(assignment.office)
        if role is None:
            continue
        try:
            await set_member_role(
                guild, role, assignment.discord_user_id, present=assignment.present,
                reason="Civitas public office sync",
            )
        except discord.HTTPException as error:
            logger.warning("Не удалось синхронизировать роль должности (HTTP %s)", error.status)


@asynccontextmanager
async def run_office_role_sync(
    client: discord.Client, service: GovernmentService | None, *, guild_id: int,
    president_role_id: int | None, parliament_role_id: int | None,
) -> AsyncIterator[None]:
    role_ids = {
        office: role_id for office, role_id in (
            (OfficeKind.PRESIDENT, president_role_id), (OfficeKind.PARLIAMENT, parliament_role_id),
        ) if role_id is not None
    }
    if service is None or not role_ids:
        yield
        return

    async def sync_periodically() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await _reconcile_office_roles(client, service, guild_id, role_ids)
            except Exception as error:
                logger.warning("Сверка ролей должностей завершилась ошибкой (%s)", type(error).__name__)
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)

    task = asyncio.create_task(sync_periodically())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
