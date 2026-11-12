"""Guild-scoped Discord commands for one-off company jobs."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.private_jobs import PrivateJobError, PrivateJobService
from civitas.domain.ledger import LedgerError, LedgerErrorReason
from civitas.domain.private_jobs import PrivateJob

from .confirmation import OperationConfirmation
from .localization import command_description, message
from .responses import send_private_lines

_WORK_ERRORS = (CitizenshipAccessError, PrivateJobError, LedgerError)
_PAGE_SIZE = 10


def _error_reply(locale: discord.Locale, error: CitizenshipAccessError | PrivateJobError | LedgerError) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    if isinstance(error, PrivateJobError):
        return message(locale, f"work.error.{error.reason.value}")
    return message(locale, f"wallet.error.{error.reason.value}")


@asynccontextmanager
async def _work_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except _WORK_ERRORS as error:
        await send_private_lines(interaction, _error_reply(interaction.locale, error))


def _title(title: str) -> str:
    return discord.utils.escape_markdown(" ".join(title.split()))


def _job_row(locale: discord.Locale, job: PrivateJob, currency_symbol: str) -> str:
    return message(
        locale,
        "work.row",
        id=str(job.id),
        title=_title(job.title),
        company=str(job.company_id),
        amount=f"{job.amount} {currency_symbol}",
        status=message(locale, f"work.status.{job.status.value.lower()}"),
    )


def _job_details(locale: discord.Locale, job: PrivateJob, currency_symbol: str) -> list[str]:
    rows = [
        _job_row(locale, job, currency_symbol),
        message(locale, "work.deadline", date=discord.utils.format_dt(job.deadline, style="f")),
        message(locale, "work.terms", text=discord.utils.escape_markdown(job.terms)),
    ]
    if job.worker_discord_user_id is not None:
        rows.append(message(locale, "work.worker", worker=f"<@{job.worker_discord_user_id}>"))
    for key, value in (
        ("work.evidence", job.evidence), ("work.dispute_reason", job.dispute_reason),
        ("work.close_reason", job.close_reason),
    ):
        if value:
            rows.append(message(locale, key, text=discord.utils.escape_markdown(value)))
    return rows


async def _send_jobs(
    interaction: discord.Interaction, jobs: tuple[PrivateJob, ...], *, view: str, page: int, currency_symbol: str,
) -> None:
    locale = interaction.locale
    if not jobs:
        await send_private_lines(interaction, message(locale, f"work.{view}.empty"))
        return
    lines = [message(locale, f"work.{view}.title"), *(_job_row(locale, job, currency_symbol) for job in jobs)]
    if len(jobs) == _PAGE_SIZE:
        lines.append(message(locale, f"work.{view}.next", page=str(page + 1)))
    await send_private_lines(interaction, *lines)


def _decision_choice(value: str) -> app_commands.Choice[str]:
    key = f"work.decision.{value}"
    return app_commands.Choice(name=app_commands.locale_str(message(None, key), key=key), value=value)


def add_private_job_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    private_jobs_service: PrivateJobService,
    currency_symbol: str = "₵",
) -> None:
    work = app_commands.Group(name="work", description=command_description("work"))
    tree.add_command(work, guild=guild_scope)

    @work.command(name="publish", description=command_description("work.publish"))
    @app_commands.describe(
        company_id=command_description("work.company"), title=command_description("work.title"),
        terms=command_description("work.terms"), amount=command_description("work.amount"),
        hours=command_description("work.hours"), reason=command_description("work.reason"),
    )
    async def publish(
        interaction: discord.Interaction,
        company_id: app_commands.Range[int, 1],
        title: app_commands.Range[str, 1, 80],
        terms: app_commands.Range[str, 1, 500],
        amount: app_commands.Range[int, 1],
        hours: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _work_response(interaction):
            try:
                deadline = interaction.created_at + timedelta(hours=hours)
            except OverflowError:
                await send_private_lines(interaction, message(interaction.locale, "work.error.invalid_deadline"))
                return
            prompt = message(
                interaction.locale, "work.publish.preview", title=_title(title), company=str(company_id),
                amount=f"{amount} {currency_symbol}", deadline=discord.utils.format_dt(deadline, style="f"),
            )

            async def commit(click: discord.Interaction, key: str) -> str:
                try:
                    job = await private_jobs_service.publish_job(
                        click.guild_id, click.user.id, company_id=company_id, title=title, terms=terms,
                        amount=amount, deadline=deadline, reason=reason, idempotency_key=key,
                    )
                except LedgerError as error:
                    if error.reason is LedgerErrorReason.INSUFFICIENT_FUNDS:
                        return message(click.locale, "work.error.insufficient_company_funds")
                    return _error_reply(click.locale, error)
                except (CitizenshipAccessError, PrivateJobError) as error:
                    return _error_reply(click.locale, error)
                return _job_row(click.locale, job, currency_symbol)

            confirmation = OperationConfirmation(
                guild_id=guild_scope.id, actor_id=interaction.user.id, locale=interaction.locale, commit=commit,
            )
            await confirmation.show(interaction, prompt)

    @work.command(name="list", description=command_description("work.list"))
    @app_commands.describe(job_id=command_description("work.id"), page=command_description("work.page"))
    async def list_jobs(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1] | None = None,
        page: app_commands.Range[int, 1] = 1,
    ) -> None:
        async with _work_response(interaction):
            if job_id is not None:
                job = await private_jobs_service.get_job(interaction.guild_id, interaction.user.id, job_id)
                await send_private_lines(interaction, *_job_details(interaction.locale, job, currency_symbol))
                return
            jobs = await private_jobs_service.list_jobs(interaction.guild_id, interaction.user.id, page=page)
            await _send_jobs(interaction, jobs, view="list", page=page, currency_symbol=currency_symbol)

    @work.command(name="mine", description=command_description("work.mine"))
    @app_commands.describe(page=command_description("work.page"))
    async def mine(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
        async with _work_response(interaction):
            jobs = await private_jobs_service.list_my_jobs(interaction.guild_id, interaction.user.id, page=page)
            await _send_jobs(interaction, jobs, view="mine", page=page, currency_symbol=currency_symbol)

    @work.command(name="claim", description=command_description("work.claim"))
    @app_commands.describe(job_id=command_description("work.id"))
    async def claim(interaction: discord.Interaction, job_id: app_commands.Range[int, 1]) -> None:
        async with _work_response(interaction):
            job = await private_jobs_service.claim_job(interaction.guild_id, interaction.user.id, job_id)
            await send_private_lines(interaction, _job_row(interaction.locale, job, currency_symbol))

    @work.command(name="submit", description=command_description("work.submit"))
    @app_commands.describe(job_id=command_description("work.id"), evidence=command_description("work.evidence"))
    async def submit(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1], evidence: app_commands.Range[str, 1, 500],
    ) -> None:
        async with _work_response(interaction):
            job = await private_jobs_service.submit_job(interaction.guild_id, interaction.user.id, job_id, evidence=evidence)
            await send_private_lines(interaction, _job_row(interaction.locale, job, currency_symbol))

    async def apply_action(
        interaction: discord.Interaction, job_id: int, reason: str, operation: Callable[..., Awaitable[PrivateJob]],
    ) -> None:
        async with _work_response(interaction):
            job = await operation(interaction.guild_id, interaction.user.id, job_id, reason=reason)
            await send_private_lines(interaction, _job_row(interaction.locale, job, currency_symbol))

    @work.command(name="accept", description=command_description("work.accept"))
    @app_commands.describe(job_id=command_description("work.id"), reason=command_description("work.reason"))
    async def accept(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1], reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await apply_action(interaction, job_id, reason, private_jobs_service.accept_job)

    @work.command(name="cancel", description=command_description("work.cancel"))
    @app_commands.describe(job_id=command_description("work.id"), reason=command_description("work.reason"))
    async def cancel(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1], reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await apply_action(interaction, job_id, reason, private_jobs_service.cancel_job)

    @work.command(name="dispute", description=command_description("work.dispute"))
    @app_commands.describe(job_id=command_description("work.id"), reason=command_description("work.reason"))
    async def dispute(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1], reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await apply_action(interaction, job_id, reason, private_jobs_service.dispute_job)

    @work.command(name="resolve", description=command_description("work.resolve"))
    @app_commands.describe(
        job_id=command_description("work.id"), decision=command_description("work.decision"),
        reason=command_description("work.reason"),
    )
    @app_commands.choices(decision=[_decision_choice("pay"), _decision_choice("refund")])
    async def resolve(
        interaction: discord.Interaction, job_id: app_commands.Range[int, 1], decision: app_commands.Choice[str],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _work_response(interaction):
            job = await private_jobs_service.resolve_job(
                interaction.guild_id, interaction.user.id, job_id, pay=decision.value == "pay", reason=reason,
            )
            await send_private_lines(interaction, _job_row(interaction.locale, job, currency_symbol))
