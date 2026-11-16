"""Discord commands for the Chief Justice and court docket."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

import discord
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.court import COURT_REASON_MAX_LENGTH, CourtError, CourtNomination, CourtService
from civitas.app.court_cases import (
    ACT_DECISION_REASON_MAX_LENGTH,
    CASE_MAX_PAGE,
    CASE_PAGE_SIZE,
    CASE_STATEMENT_MAX_LENGTH,
    AdministrativeDecision,
    AdministrativeOutcome,
    CaseSummary,
    CourtCase,
    CourtCaseError,
    CourtCaseErrorReason,
    CourtCaseService,
    MeritsDecision,
    MeritsOutcome,
)
from civitas.app.law import ActKind
from civitas.domain.court_sanctions import ARTICLE_MAX_LENGTH, CUSTODY_RANGE, FINE_RANGE, CourtSentence, SanctionTerm

from .localization import command_description, message
from .responses import send_private_lines
from .roles import safe_assignable_role, set_member_role

logger = logging.getLogger(__name__)
_CUSTODY_ROLE_INTERVAL_SECONDS = 30


async def _reconcile_custody_role(
    client: discord.Client, service: CourtCaseService, guild_id: int, role_id: int,
) -> None:
    guild = client.get_guild(guild_id)
    if guild is None or guild.unavailable:
        return
    role = safe_assignable_role(guild, role_id)
    if role is None:
        logger.warning("Проверьте иерархию и права роли игрового заключения")
        return
    assignments = await service.custody_role_assignments()
    for member in role.members:
        assignments.setdefault(member.id, False)
    for user_id, present in assignments.items():
        try:
            await set_member_role(guild, role, user_id, present=present, reason="Civitas in-game custody sync")
        except discord.HTTPException as error:
            logger.warning("Не удалось синхронизировать роль игрового заключения (HTTP %s)", error.status)


@asynccontextmanager
async def run_custody_role_sync(
    client: discord.Client, service: CourtCaseService | None, *,
    guild_id: int, get_role_id: Callable[[], Awaitable[int | None]],
) -> AsyncIterator[None]:
    if service is None:
        yield
        return

    async def run() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                role_id = await get_role_id()
                if role_id is not None:
                    await _reconcile_custody_role(client, service, guild_id, role_id)
            except Exception as error:
                logger.warning("Сверка роли игрового заключения завершилась ошибкой (%s)", type(error).__name__)
            await asyncio.sleep(_CUSTODY_ROLE_INTERVAL_SECONDS)

    task = asyncio.create_task(run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def _court_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    locale = interaction.locale
    try:
        yield
    except CitizenshipAccessError as error:
        await send_private_lines(interaction, message(locale, f"access.{error.reason.value}"))
    except CourtError as error:
        await send_private_lines(
            interaction, message(locale, f"court.error.{error.reason.value}", limit=str(COURT_REASON_MAX_LENGTH)),
        )
    except CourtCaseError as error:
        await send_private_lines(interaction, message(
            locale, f"court.case.error.{error.reason.value}", limit=str(CASE_STATEMENT_MAX_LENGTH),
            reason_limit=str(ACT_DECISION_REASON_MAX_LENGTH),
        ))


def _require_guild(interaction: discord.Interaction, guild_scope: discord.Object) -> None:
    if interaction.guild_id != guild_scope.id:
        raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)


def _nomination_details(locale: discord.Locale, nomination: CourtNomination) -> list[str]:
    lines = [
        message(locale, "court.nomination", id=str(nomination.id), candidate=f"<@{nomination.candidate_user_id}>"),
        message(locale, "court.approvals", count=str(nomination.approval_count)),
    ]
    if nomination.starts_at is not None and nomination.ends_at is not None:
        lines.append(message(
            locale, "court.term", start=discord.utils.format_dt(nomination.starts_at, style="f"),
            end=discord.utils.format_dt(nomination.ends_at, style="f"),
        ))
    return lines


def _revision_number(locale: discord.Locale, act_kind: ActKind | None, revision_id: int) -> str:
    label = "court.case.decree" if act_kind is ActKind.DECREE else "court.case.law"
    return message(locale, label, id=str(revision_id))


def _case_summary(locale: discord.Locale, record: CourtCase | CaseSummary) -> str:
    kind = message(locale, f"court.case.kind.{record.kind.value.lower()}")
    parts = [message(locale, "court.case.summary", id=str(record.id), kind=kind)]
    if record.law_revision_id is not None:
        parts.append(_revision_number(locale, record.act_kind, record.law_revision_id))
    if record.administrative_outcome is not None:
        parts.append(message(locale, f"court.case.outcome.{record.administrative_outcome.value.lower()}"))
    if record.merits_outcome is not None:
        parts.append(message(locale, f"court.case.merits.{record.merits_outcome.value.lower()}"))
    if record.hearing_at is not None:
        parts.append(message(locale, "court.case.hearing", date=discord.utils.format_dt(record.hearing_at, style="f")))
    else:
        parts.append(message(locale, "court.case.no_hearing"))
    return " · ".join(parts)


def _escaped_text_parts(value: str) -> list[str]:
    # Split before escaping so each part remains within the Discord message limit.
    return [
        discord.utils.escape_mentions(discord.utils.escape_markdown(value[start:start + 900], ignore_links=False))
        for start in range(0, len(value), 900)
    ]


def _decision_row(locale: discord.Locale, decision: AdministrativeDecision) -> str:
    return message(
        locale, "court.case.decision.row", id=str(decision.case_id),
        revision=_revision_number(locale, decision.act_kind, decision.revision_id),
        date=discord.utils.format_dt(decision.decided_at, style="f"),
    )


def _decision_details(locale: discord.Locale, decision: AdministrativeDecision | MeritsDecision) -> list[str]:
    if isinstance(decision, MeritsDecision):
        parts = [
            message(locale, "court.case.summary", id=str(decision.case_id),
                    kind=message(locale, f"court.case.kind.{decision.kind.value.lower()}")),
            message(locale, f"court.case.merits.{decision.outcome.value.lower()}"),
        ]
        if decision.revision_id is not None:
            parts.append(_revision_number(locale, ActKind.LAW, decision.revision_id))
        if decision.sentence is not None:
            sentence = decision.sentence
            article = _escaped_text_parts(sentence.article)[0]
            if sentence.fine is not None:
                fine = sentence.fine
                parts.append(message(
                    locale, "court.case.decision.fine", amount=str(fine.value), article=article,
                    minimum=str(fine.article_minimum), maximum=str(fine.article_maximum),
                ))
            if sentence.custody is not None:
                custody = sentence.custody
                until = decision.decided_at + timedelta(hours=custody.value)
                parts.append(message(
                    locale, "court.case.decision.custody", hours=str(custody.value), article=article,
                    minimum=str(custody.article_minimum), maximum=str(custody.article_maximum),
                    until=discord.utils.format_dt(until, style="f"),
                ))
        parts.append(message(locale, "court.case.decision.reason"))
        parts.extend(_escaped_text_parts(decision.reason))
        return parts
    parts = [
        _decision_row(locale, decision),
        message(locale, f"court.case.outcome.{decision.outcome.value.lower()}"),
    ]
    if decision.outcome is AdministrativeOutcome.GRANTED:
        parts.append(message(locale, f"court.case.decision.granted.{decision.act_kind.value.lower()}"))
    parts.append(message(locale, "court.case.decision.reason"))
    parts.extend(_escaped_text_parts(decision.reason))
    return parts


def _choice(key: str, value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=app_commands.locale_str(message(None, key), key=key), value=value)


def _sanction_term(value: int | None, minimum: int | None, maximum: int | None) -> SanctionTerm | None:
    if value is None and minimum is None and maximum is None:
        return None
    if value is None or minimum is None or maximum is None:
        raise CourtCaseError(CourtCaseErrorReason.INVALID_SENTENCE)
    return SanctionTerm(value, minimum, maximum)


def _respondent_id(member: discord.Member, guild_scope: discord.Object) -> int:
    if member.bot or member.guild.id != guild_scope.id:
        raise CourtCaseError(CourtCaseErrorReason.INVALID_PARTIES)
    return member.id


def _add_case_commands(court: app_commands.Group, guild_scope: discord.Object, service: CourtCaseService) -> None:
    cases = app_commands.Group(name="case", description=command_description("court.case"))

    @cases.command(name="civil", description=command_description("court.case.civil"))
    @app_commands.describe(
        statement=command_description("court.case.statement"), respondent=command_description("court.case.respondent"),
        for_state=command_description("court.case.for_state"), against_state=command_description("court.case.against_state"),
    )
    async def civil(
        interaction: discord.Interaction, statement: app_commands.Range[str, 1, CASE_STATEMENT_MAX_LENGTH],
        respondent: discord.Member | None = None, for_state: bool = False, against_state: bool = False,
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.file_civil(
                interaction.guild_id, interaction.user.id, statement=statement,
                respondent_user_id=_respondent_id(respondent, guild_scope) if respondent is not None else None,
                for_state=for_state,
                against_state=against_state, idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _case_summary(interaction.locale, record))

    @cases.command(name="charge", description=command_description("court.case.charge"))
    @app_commands.describe(
        respondent=command_description("court.case.respondent"), law_revision_id=command_description("court.case.law_revision_id"),
        occurred_at=command_description("court.case.occurred_at"), statement=command_description("court.case.statement"),
    )
    async def charge(
        interaction: discord.Interaction, respondent: discord.Member, law_revision_id: app_commands.Range[int, 1],
        occurred_at: str, statement: app_commands.Range[str, 1, CASE_STATEMENT_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.file_charge(
                interaction.guild_id, interaction.user.id, respondent_user_id=_respondent_id(respondent, guild_scope),
                law_revision_id=law_revision_id, occurred_at=occurred_at, statement=statement,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _case_summary(interaction.locale, record))

    @cases.command(name="challenge", description=command_description("court.case.challenge"))
    @app_commands.describe(
        law_revision_id=command_description("court.case.law_revision_id"), statement=command_description("court.case.standing"),
    )
    async def challenge(
        interaction: discord.Interaction, law_revision_id: app_commands.Range[int, 1],
        statement: app_commands.Range[str, 1, CASE_STATEMENT_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.file_challenge(
                interaction.guild_id, interaction.user.id, law_revision_id=law_revision_id, statement=statement,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _case_summary(interaction.locale, record))

    @cases.command(name="challenge_decree", description=command_description("court.case.challenge_decree"))
    @app_commands.describe(
        decree_revision_id=command_description("court.case.decree_revision_id"),
        statement=command_description("court.case.standing"),
    )
    async def challenge_decree(
        interaction: discord.Interaction, decree_revision_id: app_commands.Range[int, 1],
        statement: app_commands.Range[str, 1, CASE_STATEMENT_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.file_decree_challenge(
                interaction.guild_id, interaction.user.id, decree_revision_id=decree_revision_id,
                statement=statement, idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _case_summary(interaction.locale, record))

    @cases.command(name="show", description=command_description("court.case.show"))
    @app_commands.describe(case_id=command_description("court.case.id"))
    async def show(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record, can_view_statement = await service.case(interaction.guild_id, interaction.user.id, case_id)
            parts = [_case_summary(interaction.locale, record)]
            if can_view_statement and isinstance(record, CourtCase):
                parts.append(message(interaction.locale, "court.case.statement"))
                parts.extend(_escaped_text_parts(record.statement))
            await send_private_lines(interaction, *parts)

    @cases.command(name="list", description=command_description("court.case.list"))
    @app_commands.describe(page=command_description("court.case.page"))
    async def list_cases(interaction: discord.Interaction, page: app_commands.Range[int, 1, CASE_MAX_PAGE] = 1) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            records = await service.list_cases(interaction.guild_id, page=page)
            locale = interaction.locale
            if not records:
                await send_private_lines(interaction, message(locale, "court.case.list.empty", page=str(page)))
                return
            parts = [message(locale, "court.case.list.title", page=str(page))]
            parts.extend(_case_summary(locale, record) for record in records)
            if len(records) == CASE_PAGE_SIZE and page < CASE_MAX_PAGE:
                parts.append(message(locale, "court.case.list.next", page=str(page + 1)))
            await send_private_lines(interaction, *parts)

    @cases.command(name="schedule", description=command_description("court.case.schedule"))
    @app_commands.describe(case_id=command_description("court.case.id"), hearing_at=command_description("court.case.hearing_at"))
    async def schedule(interaction: discord.Interaction, case_id: app_commands.Range[int, 1], hearing_at: str) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            record = await service.schedule_hearing(
                interaction.guild_id, interaction.user.id, case_id, hearing_at=hearing_at,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _case_summary(interaction.locale, record))

    @cases.command(name="decide_challenge", description=command_description("court.case.decide_challenge"))
    @app_commands.describe(
        case_id=command_description("court.case.id"), outcome=command_description("court.case.outcome"),
        reason=command_description("court.case.public_reason"),
    )
    @app_commands.choices(outcome=[
        _choice(f"court.case.choice.outcome.{outcome.value.lower()}", outcome.value) for outcome in AdministrativeOutcome
    ])
    async def decide_challenge(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1], outcome: app_commands.Choice[str],
        reason: app_commands.Range[str, 1, ACT_DECISION_REASON_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            try:
                decision_outcome = AdministrativeOutcome(outcome.value)
            except ValueError:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_OUTCOME) from None
            result = await service.decide_challenge(
                interaction.guild_id, interaction.user.id, case_id, outcome=decision_outcome, reason=reason,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, *_decision_details(interaction.locale, result))

    @cases.command(name="decide_merits", description=command_description("court.case.decide_merits"))
    @app_commands.describe(
        case_id=command_description("court.case.id"),
        outcome=command_description("court.case.outcome"),
        reason=command_description("court.case.public_reason"),
        article=command_description("court.case.sentence.article"),
        fine=command_description("court.case.sentence.fine"),
        fine_min=command_description("court.case.sentence.fine_min"),
        fine_max=command_description("court.case.sentence.fine_max"),
        custody_hours=command_description("court.case.sentence.custody_hours"),
        custody_min=command_description("court.case.sentence.custody_min"),
        custody_max=command_description("court.case.sentence.custody_max"),
    )
    @app_commands.choices(outcome=[
        _choice(f"court.case.merits.{item.value.lower()}", item.value) for item in MeritsOutcome
    ])
    async def decide_merits(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1], outcome: app_commands.Choice[str],
        reason: app_commands.Range[str, 1, ACT_DECISION_REASON_MAX_LENGTH],
        article: app_commands.Range[str, 1, ARTICLE_MAX_LENGTH] | None = None,
        fine: app_commands.Range[int, FINE_RANGE[0], FINE_RANGE[1]] | None = None,
        fine_min: app_commands.Range[int, 0] | None = None,
        fine_max: app_commands.Range[int, 0] | None = None,
        custody_hours: app_commands.Range[int, CUSTODY_RANGE[0], CUSTODY_RANGE[1]] | None = None,
        custody_min: app_commands.Range[int, 0] | None = None,
        custody_max: app_commands.Range[int, 0] | None = None,
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            try:
                selected = MeritsOutcome(outcome.value)
            except ValueError:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_OUTCOME) from None
            sentence = None
            fine_term = _sanction_term(fine, fine_min, fine_max)
            custody_term = _sanction_term(custody_hours, custody_min, custody_max)
            if article is not None or fine_term is not None or custody_term is not None:
                if article is None:
                    raise CourtCaseError(CourtCaseErrorReason.INVALID_SENTENCE)
                try:
                    sentence = CourtSentence(article, fine=fine_term, custody=custody_term)
                except ValueError:
                    raise CourtCaseError(CourtCaseErrorReason.INVALID_SENTENCE) from None
            result = await service.decide_merits(
                interaction.guild_id, interaction.user.id, case_id, outcome=selected, reason=reason,
                idempotency_key=str(interaction.id), sentence=sentence,
            )
            await send_private_lines(interaction, *_decision_details(interaction.locale, result))

    @cases.command(name="decision", description=command_description("court.case.decision"))
    @app_commands.describe(case_id=command_description("court.case.id"))
    async def decision(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            result = await service.decision(interaction.guild_id, case_id)
            if result is None:
                await send_private_lines(interaction, message(interaction.locale, "court.case.decision.pending"))
                return
            await send_private_lines(interaction, *_decision_details(interaction.locale, result))

    @cases.command(name="fine_status", description=command_description("court.case.fine_status"))
    @app_commands.describe(case_id=command_description("court.case.id"))
    async def fine_status(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            status = await service.fine_status(interaction.guild_id, interaction.user.id, case_id)
            key = status.value if status is not None else "not_found"
            await send_private_lines(interaction, message(interaction.locale, f"court.case.fine_status.{key}"))

    @cases.command(name="returns", description=command_description("court.case.returns"))
    @app_commands.describe(
        act_kind=command_description("court.case.return_kind"), page=command_description("court.case.page"),
    )
    @app_commands.choices(act_kind=[
        _choice(f"court.case.choice.act_kind.{kind.value.lower()}", kind.value) for kind in ActKind
    ])
    async def list_returns(
        interaction: discord.Interaction, act_kind: app_commands.Choice[str], page: app_commands.Range[int, 1, CASE_MAX_PAGE] = 1,
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            try:
                kind = ActKind(act_kind.value)
            except ValueError:
                raise CourtCaseError(CourtCaseErrorReason.INVALID_REQUEST) from None
            records = await service.list_returns(interaction.guild_id, kind, page=page)
            locale = interaction.locale
            if not records:
                await send_private_lines(interaction, message(locale, "court.case.returns.empty", page=str(page)))
                return
            parts = [message(locale, f"court.case.returns.{kind.value.lower()}", page=str(page))]
            parts.extend(_decision_row(locale, record) for record in records)
            if len(records) == CASE_PAGE_SIZE and page < CASE_MAX_PAGE:
                parts.append(message(locale, "court.case.list.next", page=str(page + 1)))
            await send_private_lines(interaction, *parts)

    court.add_command(cases)


def add_court_commands(
    tree: app_commands.CommandTree, *, guild_scope: discord.Object, service: CourtService,
    case_service: CourtCaseService | None = None,
) -> None:
    court = app_commands.Group(name="court", description=command_description("court"))

    @court.command(name="nominate", description=command_description("court.nominate"))
    @app_commands.describe(candidate=command_description("court.candidate"), reason=command_description("court.reason"))
    async def nominate(
        interaction: discord.Interaction, candidate: discord.Member, reason: app_commands.Range[str, 1, COURT_REASON_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            if candidate.bot or candidate.guild.id != guild_scope.id:
                await send_private_lines(interaction, message(interaction.locale, "court.error.invalid_candidate"))
                return
            nomination = await service.nominate(
                interaction.guild_id, interaction.user.id, candidate_user_id=candidate.id, reason=reason,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, *_nomination_details(interaction.locale, nomination))

    @court.command(name="approve", description=command_description("court.approve"))
    @app_commands.describe(nomination_id=command_description("court.nomination_id"), reason=command_description("court.reason"))
    async def approve(
        interaction: discord.Interaction, nomination_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, COURT_REASON_MAX_LENGTH],
    ) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            nomination = await service.approve(interaction.guild_id, interaction.user.id, nomination_id, reason=reason)
            await send_private_lines(interaction, *_nomination_details(interaction.locale, nomination))

    @court.command(name="current", description=command_description("court.current"))
    async def current(interaction: discord.Interaction) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            nomination = await service.current(interaction.guild_id)
            if nomination is None:
                await send_private_lines(interaction, message(interaction.locale, "court.vacant"))
                return
            await send_private_lines(
                interaction, message(interaction.locale, "court.current"),
                *_nomination_details(interaction.locale, nomination),
            )

    @court.command(name="nomination", description=command_description("court.nomination"))
    @app_commands.describe(nomination_id=command_description("court.nomination_id"))
    async def nomination_details(interaction: discord.Interaction, nomination_id: app_commands.Range[int, 1]) -> None:
        async with _court_response(interaction):
            _require_guild(interaction, guild_scope)
            nomination = await service.get_nomination(interaction.guild_id, nomination_id)
            await send_private_lines(interaction, *_nomination_details(interaction.locale, nomination))

    if case_service is not None:
        _add_case_commands(court, guild_scope, case_service)
    tree.add_command(court, guild=guild_scope)
