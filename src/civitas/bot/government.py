"""Discord views of manual election protocols and current public offices."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError, is_discord_id
from civitas.app.government import GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.domain.government import CrisisResolution, Election, ElectionCancellation, Impeachment, OfficeKind, OfficeTerm

from .localization import command_description, message
from .responses import proportion_bar, send_lines, send_private_lines
from .roles import safe_assignable_role, set_member_role

logger = logging.getLogger(__name__)
_SYNC_INTERVAL_SECONDS = 60
_CANDIDATE_ID = re.compile(r"(?:([0-9]{1,20})|<@!?([0-9]{1,20})>)")
_VOTE_COUNT = re.compile(r"[0-9]{1,10}")
_VOTER_PAGE_SIZE = 20


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


def _election_outcome(election: Election) -> str | None:
    if not election.failed:
        return None
    if election.opens_political_crisis:
        return "crisis" if election.is_political_crisis_open else "crisis_resolved"
    return "retry"


def _election_row(locale: discord.Locale, election: Election, *, summarize_outcome: bool = False) -> str:
    status = "cancelled" if election.is_cancelled else election.status.value.lower()
    row = message(
        locale, "election.results.row", id=str(election.id),
        office=message(locale, f"government.office.{election.office.value.lower()}"),
        status=message(locale, f"election.status.{status}"),
    )
    outcome = _election_outcome(election) if summarize_outcome and not election.is_cancelled else None
    return f"{row} · {message(locale, f'election.outcome.{outcome}.short')}" if outcome else row


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
    if election.retry_of_id is not None:
        rows.append(message(locale, "election.detail.retry", id=str(election.retry_of_id)))
    if election.replacement_of_id is not None:
        rows.append(message(locale, "election.detail.replacement", id=str(election.replacement_of_id)))

    cancellation = election.cancellation
    if cancellation is not None and cancellation.confirmed_at is not None:
        rows.extend((
            message(locale, "election.cancel.confirmed_at", date=discord.utils.format_dt(cancellation.confirmed_at, style="f")),
            _cancellation_reason(locale, cancellation),
        ))
        return rows

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
        for tally in sorted(protocol.tallies, key=lambda item: -item.votes):
            share = proportion_bar(tally.votes, protocol.participants)
            rows.append(f"<@{tally.candidate_discord_user_id}> · {tally.votes} · {share}")
        abstentions = protocol.participants - sum(tally.votes for tally in protocol.tallies)
        rows.append(message(locale, "election.detail.abstained", count=str(abstentions)))
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
    outcome = _election_outcome(election)
    if outcome is not None:
        rows.append(message(locale, f"election.outcome.{outcome}.detail"))
    return rows


def _crisis_resolution_reply(locale: discord.Locale, resolution: CrisisResolution) -> str:
    status = "confirmed" if resolution.confirmed_at is not None else "pending"
    return message(locale, f"election.crisis.resolution_{status}", id=str(resolution.election_id))


def _cancellation_reply(locale: discord.Locale, cancellation: ElectionCancellation) -> str:
    status = "confirmed" if cancellation.confirmed_at is not None else "pending"
    return message(locale, f"election.cancel.{status}", id=str(cancellation.election_id))


def _public_reason(reason: str) -> str:
    flattened = " ".join(reason.split()).replace("<", "‹").replace(">", "›")
    return discord.utils.escape_mentions(discord.utils.escape_markdown(flattened, ignore_links=False))


def _cancellation_reason(locale: discord.Locale, cancellation: ElectionCancellation) -> str:
    return message(locale, "election.cancel.public_reason", reason=_public_reason(cancellation.public_reason))


def _impeachment_details(locale: discord.Locale, record: Impeachment) -> list[str]:
    rows = [
        message(locale, "impeachment.title", id=str(record.id), president=f"<@{record.president_discord_user_id}>"),
        message(locale, "impeachment.opened", date=discord.utils.format_dt(record.opened_at, style="f")),
        message(locale, "impeachment.threshold", quorum=str(record.quorum)),
    ]
    if record.eligible_citizen_ids:
        rows.append(message(locale, "impeachment.eligible", count=str(len(record.eligible_citizen_ids))))
    if record.source_url is None:
        rows.append(message(locale, "impeachment.awaiting_record"))
        return rows

    source = record.source_url.replace("<", "%3C").replace(">", "%3E")
    rows.extend((
        message(locale, "election.detail.source", url=source),
        message(locale, "impeachment.totals", participants=str(record.participants), votes_for=str(record.votes_for)),
    ))
    if record.public_reason:
        rows.append(message(locale, "impeachment.public_reason", reason=_public_reason(record.public_reason)))
    if record.confirmed_at is not None:
        rows.append(message(locale, "impeachment.confirmed", date=discord.utils.format_dt(record.confirmed_at, style="f")))
    elif record.has_support:
        rows.append(message(locale, "impeachment.awaiting_confirmation"))
    else:
        rows.append(message(locale, "impeachment.insufficient_support"))
    return rows


async def _send_voter_page(
    interaction: discord.Interaction, snapshot: tuple[int, ...], *, page: int, record_id: int, title_key: str,
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
        message(locale, title_key, id=str(record_id), count=str(len(snapshot)), page=str(page), pages=str(pages)),
        "```\n" + "\n".join(str(user_id) for user_id in ids) + "\n```",
    )


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
            interaction, message(locale, "election.results.title"),
            *(_election_row(locale, record, summarize_outcome=True) for record in records),
            message(locale, "election.results.inspect_hint"), ephemeral=False,
        )

    @commission.command(name="active", description=command_description("election.commission.active"))
    async def active_elections(interaction: discord.Interaction) -> None:
        async with _commission_response(interaction):
            locale = interaction.locale
            records = await government_service.list_unfinished_elections(interaction.guild_id, interaction.user.id)
            if not records:
                await send_private_lines(interaction, message(locale, "election.active.empty"))
                return
            await send_private_lines(
                interaction, message(locale, "election.active.title"),
                *(message(
                    locale, "election.active.row", election=_election_row(locale, record),
                    end=discord.utils.format_dt(record.extended_until or record.closes_at, style="f"),
                ) for record in records), message(locale, "election.active.inspect_hint"),
            )

    @commission.command(name="voters", description=command_description("election.commission.voters"))
    @app_commands.describe(
        election_id=command_description("election.id"), page=command_description("election.voters.page"),
    )
    async def voters(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1], page: app_commands.Range[int, 1] = 1,
    ) -> None:
        async with _commission_response(interaction):
            snapshot = await government_service.list_voter_snapshot(interaction.guild_id, interaction.user.id, election_id)
            await _send_voter_page(interaction, snapshot, page=page, record_id=election_id, title_key="election.voters.title")

    @commission.command(name="open", description=command_description("election.commission.open"))
    @app_commands.choices(office=_office_choices())
    @app_commands.describe(
        office=command_description("election.office"), candidates=command_description("election.candidates"),
        reason=command_description("election.reason"), retry_of_id=command_description("election.retry_of_id"),
        replacement_of_id=command_description("election.replacement_of_id"),
    )
    async def open_election(
        interaction: discord.Interaction,
        office: str,
        candidates: app_commands.Range[str, 1, 4000],
        reason: app_commands.Range[str, 1, 200],
        retry_of_id: app_commands.Range[int, 1] | None = None,
        replacement_of_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        async with _commission_response(interaction):
            record = await government_service.open_election(
                interaction.guild_id, interaction.user.id, office=OfficeKind(office),
                candidate_user_ids=_candidates(candidates), reason=reason,
                idempotency_key=str(interaction.id), retry_of_id=retry_of_id,
                replacement_of_id=replacement_of_id,
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
            locale = interaction.locale
            rows = _election_details(locale, record)
            resolution = record.crisis_resolution
            if resolution is not None and resolution.confirmed_at is None:
                rows.append(_crisis_resolution_reply(locale, resolution))
                proposal = discord.utils.escape_mentions(discord.utils.escape_markdown(resolution.proposal_reason))
                rows.append(message(locale, "election.crisis.proposal_reason", reason=proposal))
            cancellation = record.cancellation
            if cancellation is not None and cancellation.confirmed_at is None:
                rows.extend((_cancellation_reply(locale, cancellation), _cancellation_reason(locale, cancellation)))
            await send_private_lines(interaction, *rows)

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

    @commission.command(name="resolve_crisis", description=command_description("election.commission.resolve_crisis"))
    @app_commands.describe(election_id=command_description("election.id"), reason=command_description("election.reason"))
    async def resolve_crisis(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            resolution = await government_service.propose_crisis_resolution(
                interaction.guild_id, interaction.user.id, election_id, reason=reason,
            )
            await send_private_lines(interaction, _crisis_resolution_reply(interaction.locale, resolution))

    @commission.command(name="confirm_crisis", description=command_description("election.commission.confirm_crisis"))
    @app_commands.describe(election_id=command_description("election.id"), reason=command_description("election.reason"))
    async def confirm_crisis(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            resolution = await government_service.confirm_crisis_resolution(
                interaction.guild_id, interaction.user.id, election_id, reason=reason,
            )
            await send_private_lines(interaction, _crisis_resolution_reply(interaction.locale, resolution))

    @commission.command(name="cancel", description=command_description("election.commission.cancel"))
    @app_commands.describe(
        election_id=command_description("election.id"), reason=command_description("election.cancel.public_reason"),
    )
    async def cancel_election(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            cancellation = await government_service.propose_election_cancellation(
                interaction.guild_id, interaction.user.id, election_id, reason=reason,
            )
            await send_private_lines(interaction, _cancellation_reply(interaction.locale, cancellation))

    @commission.command(name="confirm_cancel", description=command_description("election.commission.confirm_cancel"))
    @app_commands.describe(election_id=command_description("election.id"), reason=command_description("election.reason"))
    async def confirm_cancellation(
        interaction: discord.Interaction, election_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            cancellation = await government_service.confirm_election_cancellation(
                interaction.guild_id, interaction.user.id, election_id, reason=reason,
            )
            await send_private_lines(interaction, _cancellation_reply(interaction.locale, cancellation))

    _add_impeachment_commands(government, commission, guild_scope, government_service)


def _add_impeachment_commands(
    government: app_commands.Group, commission: app_commands.Group, guild_scope: discord.Object,
    service: GovernmentService,
) -> None:
    @government.command(name="impeachment", description=command_description("government.impeachment"))
    @app_commands.describe(impeachment_id=command_description("impeachment.id"))
    async def public_impeachment(
        interaction: discord.Interaction, impeachment_id: app_commands.Range[int, 1],
    ) -> None:
        if not await _in_guild(interaction, guild_scope):
            return
        await interaction.response.defer(thinking=True)
        locale = interaction.locale
        try:
            record = await service.get_public_impeachment(interaction.guild_id, interaction.user.id, impeachment_id)
        except GovernmentError as error:
            if error.reason != GovernmentErrorReason.IMPEACHMENT_NOT_FOUND:
                raise
            await send_lines(interaction, message(locale, "election.error.impeachment_not_found"), ephemeral=False)
            return
        await send_lines(interaction, *_impeachment_details(locale, record), ephemeral=False)

    @commission.command(name="open_impeachment", description=command_description("election.commission.open_impeachment"))
    async def open_impeachment(interaction: discord.Interaction) -> None:
        async with _commission_response(interaction):
            record = await service.open_impeachment(
                interaction.guild_id, interaction.user.id, idempotency_key=str(interaction.id),
            )
            locale = interaction.locale
            await send_private_lines(
                interaction, *_impeachment_details(locale, record),
                message(locale, "impeachment.public_record_hint", id=str(record.id)),
            )

    @commission.command(
        name="impeachment_voters", description=command_description("election.commission.impeachment_voters"),
    )
    @app_commands.describe(
        impeachment_id=command_description("impeachment.id"), page=command_description("election.voters.page"),
    )
    async def impeachment_voters(
        interaction: discord.Interaction, impeachment_id: app_commands.Range[int, 1],
        page: app_commands.Range[int, 1] = 1,
    ) -> None:
        async with _commission_response(interaction):
            snapshot = await service.list_impeachment_voters(interaction.guild_id, interaction.user.id, impeachment_id)
            await _send_voter_page(
                interaction, snapshot, page=page, record_id=impeachment_id, title_key="impeachment.voters.title",
            )

    @commission.command(
        name="record_impeachment", description=command_description("election.commission.record_impeachment"),
    )
    @app_commands.describe(
        impeachment_id=command_description("impeachment.id"), source_url=command_description("impeachment.source_url"),
        participants=command_description("election.participants"), votes_for=command_description("impeachment.votes_for"),
        reason=command_description("impeachment.public_reason"),
    )
    async def record_impeachment(
        interaction: discord.Interaction, impeachment_id: app_commands.Range[int, 1],
        source_url: app_commands.Range[str, 1, 500], participants: app_commands.Range[int, 0],
        votes_for: app_commands.Range[int, 0], reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            record = await service.record_impeachment(
                interaction.guild_id, interaction.user.id, impeachment_id,
                source_url=source_url, participants=participants, votes_for=votes_for, reason=reason,
            )
            await send_private_lines(interaction, *_impeachment_details(interaction.locale, record))

    @commission.command(
        name="confirm_impeachment", description=command_description("election.commission.confirm_impeachment"),
    )
    @app_commands.describe(
        impeachment_id=command_description("impeachment.id"), reason=command_description("election.reason"),
    )
    async def confirm_impeachment(
        interaction: discord.Interaction, impeachment_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _commission_response(interaction):
            record = await service.confirm_impeachment(
                interaction.guild_id, interaction.user.id, impeachment_id, reason=reason,
            )
            await send_private_lines(interaction, *_impeachment_details(interaction.locale, record))


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
            logger.warning("Проверьте ID, иерархию и права роли %s на сервере и в каналах", office.value)
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
    president_role_id: int | None = None,
    parliament_role_id: int | None = None,
    get_role_ids: Callable[[], Awaitable[tuple[int | None, int | None]]] | None = None,
) -> AsyncIterator[None]:
    if service is None or (president_role_id is None and parliament_role_id is None and get_role_ids is None):
        yield
        return

    async def sync_periodically() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                current_president, current_parliament = (
                    await get_role_ids() if get_role_ids is not None else (president_role_id, parliament_role_id)
                )
                role_ids = {
                    office: role_id for office, role_id in (
                        (OfficeKind.PRESIDENT, current_president), (OfficeKind.PARLIAMENT, current_parliament),
                    ) if role_id is not None
                }
                if role_ids:
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
