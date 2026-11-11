"""Guild-scoped Discord commands for NPC work."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.jobs import JobError, JobErrorReason, JobService
from civitas.domain.jobs import JobCategory, JobClaim, JobClaimStatus, JobTask
from civitas.domain.ledger import LedgerError, LedgerErrorReason

from .localization import command_description, message

_NO_MENTIONS = discord.AllowedMentions.none()
_MESSAGE_LIMIT = 2000


async def _reply(interaction: discord.Interaction, *parts: str) -> None:
    page = ""
    for part in parts:
        candidate = f"{page}\n{part}" if page else part
        if page and len(candidate.encode("utf-16-le")) // 2 > _MESSAGE_LIMIT:
            await _send_page(interaction, page)
            page = part
        else:
            page = candidate
    if page:
        await _send_page(interaction, page)


async def _send_page(interaction: discord.Interaction, content: str) -> None:
    await interaction.followup.send(
        content,
        ephemeral=True,
        allowed_mentions=_NO_MENTIONS,
        suppress_embeds=True,
    )


@asynccontextmanager
async def _job_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except CitizenshipAccessError as error:
        await _reply(interaction, message(interaction.locale, f"access.{error.reason.value}"))
    except JobError as error:
        await _reply(interaction, message(interaction.locale, f"jobs.error.{error.reason.value}"))
    except LedgerError as error:
        if error.reason == LedgerErrorReason.INSUFFICIENT_FUNDS:
            key = "jobs.error.treasury_insufficient_funds"
        else:
            key = "error.temporary"
        await _reply(interaction, message(interaction.locale, key))


def _summary(locale: discord.Locale, task: JobTask, currency_symbol: str) -> str:
    title = discord.utils.escape_markdown(" ".join(task.title.split()))
    return message(
        locale,
        "jobs.task.summary",
        id=str(task.id),
        title=title,
        category=message(locale, f"jobs.category.{task.category.value}"),
        reward=f"{task.reward} {currency_symbol}",
        deadline=discord.utils.format_dt(task.expires_at, style="f"),
    )


def _status(locale: discord.Locale, claim: JobClaim) -> str:
    return message(
        locale,
        "jobs.task.status",
        id=str(claim.task_id),
        status=message(locale, f"jobs.status.{claim.status.value.lower()}"),
    )


def _category_choices() -> list[app_commands.Choice[str]]:
    choices = []
    for category in JobCategory:
        key = f"jobs.category.{category.value}"
        name = app_commands.locale_str(message(None, key), key=key)
        choices.append(app_commands.Choice(name=name, value=category.value))
    return choices


def add_job_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    job_service: JobService,
    currency_symbol: str = "₵",
) -> None:
    jobs = app_commands.Group(name="jobs", description=command_description("jobs"))
    staff = app_commands.Group(name="staff", description=command_description("jobs.staff"))
    jobs.add_command(staff)
    tree.add_command(jobs, guild=guild_scope)

    @jobs.command(name="list", description=command_description("jobs.list"))
    @app_commands.describe(task_id=command_description("jobs.task_id"))
    async def list_jobs(
        interaction: discord.Interaction, task_id: app_commands.Range[int, 1] | None = None
    ) -> None:
        locale = interaction.locale
        async with _job_response(interaction):
            if task_id is None:
                tasks = await job_service.list_tasks(interaction.guild_id, interaction.user.id)
                if not tasks:
                    await _reply(interaction, message(locale, "jobs.list.empty"))
                    return
                await _reply(
                    interaction,
                    message(locale, "jobs.list.title"),
                    *(_summary(locale, task, currency_symbol) for task in tasks),
                    message(locale, "jobs.list.details"),
                )
                return

            detail = await job_service.get_task_details(
                interaction.guild_id, interaction.user.id, task_id
            )
            parts = [
                _summary(locale, detail.task, currency_symbol),
                discord.utils.escape_markdown(detail.task.requirements),
            ]
            if detail.claim is not None and detail.claim.discord_user_id == interaction.user.id:
                parts.append(_status(locale, detail.claim))
                if detail.claim.review_reason:
                    parts.append(message(
                        locale,
                        "jobs.task.reason",
                        reason=discord.utils.escape_markdown(detail.claim.review_reason),
                    ))
            await _reply(interaction, *parts)

    @jobs.command(name="claim", description=command_description("jobs.claim"))
    @app_commands.describe(task_id=command_description("jobs.task_id"))
    async def claim(interaction: discord.Interaction, task_id: app_commands.Range[int, 1]) -> None:
        async with _job_response(interaction):
            record = await job_service.claim(interaction.guild_id, interaction.user.id, task_id)
            locale = interaction.locale
            parts = [_status(locale, record)]
            if record.status == JobClaimStatus.CLAIMED:
                parts.append(message(locale, "jobs.claim.instructions", id=str(record.task_id)))
            await _reply(interaction, *parts)

    @jobs.command(name="submit", description=command_description("jobs.submit"))
    @app_commands.describe(
        task_id=command_description("jobs.task_id"),
        proof=command_description("jobs.proof"),
    )
    async def submit(
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1],
        proof: app_commands.Range[str, 1, 500],
    ) -> None:
        async with _job_response(interaction):
            record = await job_service.submit(interaction.guild_id, interaction.user.id, task_id, proof)
            await _reply(interaction, _status(interaction.locale, record))

    @staff.command(name="publish", description=command_description("jobs.staff.publish"))
    @app_commands.choices(category=_category_choices())
    @app_commands.describe(
        category=command_description("jobs.category"),
        title=command_description("jobs.title"),
        requirements=command_description("jobs.instructions"),
        reward=command_description("jobs.reward"),
        hours=command_description("jobs.hours"),
    )
    async def publish(
        interaction: discord.Interaction,
        category: str,
        title: app_commands.Range[str, 1, 80],
        requirements: app_commands.Range[str, 1, 500],
        reward: app_commands.Range[int, 1],
        hours: app_commands.Range[int, 1],
    ) -> None:
        async with _job_response(interaction):
            try:
                expires_at = datetime.now(UTC) + timedelta(hours=hours)
            except OverflowError:
                raise JobError(JobErrorReason.INVALID_DEADLINE) from None
            task = await job_service.publish(
                interaction.guild_id,
                interaction.user.id,
                category=JobCategory(category),
                title=title,
                requirements=requirements,
                reward=reward,
                expires_at=expires_at,
            )
            await _reply(interaction, message(interaction.locale, "jobs.task.published", id=str(task.id)))

    @staff.command(name="review", description=command_description("jobs.staff.review"))
    @app_commands.describe(task_id=command_description("jobs.review_task_id"))
    async def review(
        interaction: discord.Interaction, task_id: app_commands.Range[int, 1] | None = None
    ) -> None:
        locale = interaction.locale
        async with _job_response(interaction):
            if task_id is None:
                task_ids = await job_service.list_submitted_task_ids(interaction.guild_id, interaction.user.id)
                if task_ids:
                    reply = message(
                        locale, "jobs.queue.tasks", ids=", ".join(str(task_id) for task_id in task_ids)
                    )
                else:
                    reply = message(locale, "jobs.queue.empty")
                await _reply(interaction, reply)
                return

            detail = await job_service.get_review(interaction.guild_id, interaction.user.id, task_id)
            claim = detail.claim
            if claim is None:
                raise JobError(JobErrorReason.CLAIM_NOT_FOUND)
            proof = message(locale, "jobs.review.no_proof")
            if claim.evidence:
                proof = message(locale, "jobs.review.proof", proof=discord.utils.escape_markdown(claim.evidence))
            await _reply(
                interaction,
                _summary(locale, detail.task, currency_symbol),
                discord.utils.escape_markdown(detail.task.requirements),
                _status(locale, claim),
                message(locale, "jobs.review.claimant", claimant=f"<@{claim.discord_user_id}>"),
                proof,
            )

    @staff.command(name="approve", description=command_description("jobs.staff.approve"))
    @app_commands.describe(
        task_id=command_description("jobs.review_task_id"),
        reason=command_description("jobs.reason"),
    )
    async def approve(
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _job_response(interaction):
            record = await job_service.review(
                interaction.guild_id, interaction.user.id, task_id, approve=True, reason=reason
            )
            await _reply(interaction, _status(interaction.locale, record))

    @staff.command(name="reject", description=command_description("jobs.staff.reject"))
    @app_commands.describe(
        task_id=command_description("jobs.review_task_id"),
        reason=command_description("jobs.reason"),
    )
    async def reject(
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _job_response(interaction):
            record = await job_service.review(
                interaction.guild_id, interaction.user.id, task_id, approve=False, reason=reason
            )
            await _reply(interaction, _status(interaction.locale, record))
