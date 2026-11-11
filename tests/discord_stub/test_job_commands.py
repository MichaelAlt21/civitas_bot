from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.jobs import JobError, JobErrorReason, JobService
from civitas.app.wallet import WalletService
from civitas.bot.jobs import add_job_commands
from civitas.bot.localization import CommandTranslator
from civitas.bot.wallet import add_wallet_commands
from civitas.domain.jobs import JobCategory, JobClaim, JobClaimStatus, JobDetail, JobTask
from civitas.domain.ledger import HistoryEntry, TransactionKind

GUILD_ID = 123
WORKER_ID = 456
STAFF_ID = 789
TASK_ID = 42
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
TASK = JobTask(
    id=TASK_ID,
    guild_id=1,
    category=JobCategory.LOGISTICS,
    title="Move supplies",
    requirements="Use the staging area.",
    reward=30,
    expires_at=NOW + timedelta(hours=24),
    created_at=NOW,
    published_by_citizen_id=3,
)
CLAIM = JobClaim(10, 1, TASK_ID, 2, WORKER_ID, JobClaimStatus.CLAIMED, NOW)


@pytest.fixture
def job_commands():
    service = Mock(spec=JobService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_job_commands(tree, guild_scope=guild, job_service=service, currency_symbol="CP")
    jobs = tree.get_command("jobs", guild=guild)
    return service, tree, jobs, jobs.get_command("staff")


def interaction(*, actor=WORKER_ID, locale=discord.Locale.russian):
    return SimpleNamespace(
        locale=locale,
        guild_id=GUILD_ID,
        user=SimpleNamespace(id=actor),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def private_replies(command):
    command.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    replies = []
    for call in command.followup.send.await_args_list:
        assert call.kwargs["ephemeral"] is True
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert call.kwargs["suppress_embeds"] is True
        replies.append(call.args[0])
    assert replies
    return replies


@pytest.mark.asyncio
async def test_jobs_metadata_is_guild_only_and_localized(job_commands):
    _, tree, jobs, _ = job_commands
    assert tree.get_command("jobs") is None
    assert tree.get_command("jobs", guild=discord.Object(id=GUILD_ID + 1)) is None
    payload = await jobs.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Работа у NPC"}
    commands = {command["name"]: command for command in payload["options"]}
    assert set(commands) == {"staff", "list", "claim", "submit"}
    staff = {command["name"]: command for command in commands["staff"]["options"]}
    assert set(staff) == {"publish", "review", "approve", "reject"}
    publish_parameters = {item["name"]: item for item in staff["publish"]["options"]}
    categories = {choice["value"]: choice for choice in publish_parameters["category"]["choices"]}
    assert categories["logistics"]["name_localizations"] == {"ru": "Логистика"}
    assert publish_parameters["requirements"]["max_length"] == 500


@pytest.mark.asyncio
async def test_lists_jobs_and_own_status_without_echoing_proof(job_commands):
    service, _, jobs, _ = job_commands
    service.list_tasks.return_value = (TASK,)
    evidence = "Do not echo this private proof"
    service.get_task_details.return_value = JobDetail(
        TASK, replace(CLAIM, status=JobClaimStatus.SUBMITTED, evidence=evidence)
    )
    list_command = interaction(locale=discord.Locale.german)
    detail_command = interaction()

    await jobs.get_command("list").callback(list_command)
    await jobs.get_command("list").callback(detail_command, TASK_ID)

    service.list_tasks.assert_awaited_once_with(GUILD_ID, WORKER_ID)
    service.get_task_details.assert_awaited_once_with(GUILD_ID, WORKER_ID, TASK_ID)
    assert "Move supplies · Logistics · 30 CP" in private_replies(list_command)[0]
    reply = private_replies(detail_command)[0]
    assert "ожидает проверки" in reply
    assert TASK.requirements in reply
    assert evidence not in reply


@pytest.mark.asyncio
async def test_citizen_claim_and_submit_are_private(job_commands):
    service, _, jobs, _ = job_commands
    service.claim.return_value = CLAIM
    service.submit.return_value = replace(CLAIM, status=JobClaimStatus.SUBMITTED)
    claim_command, submit_command = interaction(), interaction()
    proof = "private result"

    await jobs.get_command("claim").callback(claim_command, TASK_ID)
    await jobs.get_command("submit").callback(submit_command, TASK_ID, proof)

    service.claim.assert_awaited_once_with(GUILD_ID, WORKER_ID, TASK_ID)
    service.submit.assert_awaited_once_with(GUILD_ID, WORKER_ID, TASK_ID, proof)
    assert "взято в работу" in private_replies(claim_command)[0]
    reply = private_replies(submit_command)[0]
    assert "ожидает проверки" in reply
    assert proof not in reply


@pytest.mark.asyncio
async def test_staff_publishes_with_category_and_utc_deadline(job_commands):
    service, _, _, staff = job_commands
    service.publish.return_value = TASK
    command = interaction(actor=STAFF_ID)
    before = datetime.now(UTC)

    await staff.get_command("publish").callback(command, "logistics", "Supplies", "Move supplies", 30, 24)

    after = datetime.now(UTC)
    service.publish.assert_awaited_once()
    assert service.publish.await_args.args == (GUILD_ID, STAFF_ID)
    options = service.publish.await_args.kwargs
    assert options["category"] == JobCategory.LOGISTICS
    assert options["reward"] == 30
    assert before + timedelta(hours=24) <= options["expires_at"] <= after + timedelta(hours=24)
    assert "опубликовано" in private_replies(command)[0]


@pytest.mark.asyncio
async def test_staff_reviews_queue_and_full_proof_privately(job_commands):
    service, _, _, staff = job_commands
    service.list_submitted_task_ids.return_value = (TASK_ID,)
    evidence = "😀" * 499 + "!"
    service.get_review.return_value = JobDetail(
        replace(TASK, requirements="😀" * 500),
        replace(CLAIM, status=JobClaimStatus.SUBMITTED, evidence=evidence),
    )
    queue, review = interaction(actor=STAFF_ID), interaction(actor=STAFF_ID)

    await staff.get_command("review").callback(queue)
    await staff.get_command("review").callback(review, TASK_ID)

    service.list_submitted_task_ids.assert_awaited_once_with(GUILD_ID, STAFF_ID)
    service.get_review.assert_awaited_once_with(GUILD_ID, STAFF_ID, TASK_ID)
    queue_reply = private_replies(queue)[0]
    assert "Задания на проверке: 42" in queue_reply
    assert evidence not in queue_reply
    replies = private_replies(review)
    assert any(evidence in reply for reply in replies)
    assert all(len(reply.encode("utf-16-le")) // 2 <= 2000 for reply in replies)


@pytest.mark.asyncio
async def test_staff_review_error_returns_no_proof(job_commands):
    service, _, _, staff = job_commands
    service.get_review.side_effect = JobError(JobErrorReason.STAFF_REQUIRED)
    command = interaction()

    await staff.get_command("review").callback(command, TASK_ID)

    service.get_review.assert_awaited_once_with(GUILD_ID, WORKER_ID, TASK_ID)
    assert private_replies(command) == ["Для этого действия нужны полномочия сотрудника NPC."]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "status", "reply"), [
    ("approve", JobClaimStatus.PAID, "Job #42: paid."),
    ("reject", JobClaimStatus.REJECTED, "Job #42: rejected."),
])
async def test_review_decision_forwards_actor_and_reason(job_commands, name, status, reply):
    service, _, _, staff = job_commands
    service.review.return_value = replace(CLAIM, status=status)
    command = interaction(actor=STAFF_ID, locale=discord.Locale.american_english)

    await staff.get_command(name).callback(command, TASK_ID, "Reviewed proof")

    service.review.assert_awaited_once_with(
        GUILD_ID, STAFF_ID, TASK_ID, approve=name == "approve", reason="Reviewed proof"
    )
    assert private_replies(command) == [reply]


@pytest.mark.asyncio
async def test_wallet_history_labels_npc_reward():
    service = Mock(spec=WalletService)
    service.history.return_value = (HistoryEntry(1, TransactionKind.NPC_PAYOUT, 30, NOW),)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_wallet_commands(tree, guild_scope=guild, wallet_service=service)
    command = interaction()

    await tree.get_command("wallet", guild=guild).get_command("history").callback(command)

    assert "Награда за работу у NPC: +30" in command.followup.send.await_args.args[0]
    assert command.followup.send.await_args.kwargs["ephemeral"] is True
