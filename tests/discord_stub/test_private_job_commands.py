from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.private_jobs import PrivateJobError, PrivateJobErrorReason, PrivateJobService
from civitas.bot.localization import CommandTranslator, message
from civitas.bot.private_jobs import add_private_job_commands
from civitas.domain.ledger import LedgerError, LedgerErrorReason
from civitas.domain.private_jobs import PrivateJob, PrivateJobStatus

GUILD_ID = 101
OWNER = 201
WORKER = 202
ARBITER = 203
OUTSIDER = 204
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
OPEN_JOB = PrivateJob(
    id=31, guild_id=1, company_id=11, owner_citizen_id=1, title="Prepare a venue",
    terms="Build the fictional stage", amount=80, deadline=NOW + timedelta(hours=6),
    request_key="existing-key", publication_reason="Private employer note", created_at=NOW,
)
CLAIMED_JOB = replace(
    OPEN_JOB, status=PrivateJobStatus.CLAIMED, worker_citizen_id=2,
    worker_discord_user_id=WORKER, claimed_at=NOW + timedelta(minutes=5),
)
SUBMITTED_JOB = replace(
    CLAIMED_JOB, status=PrivateJobStatus.SUBMITTED, submitted_at=NOW + timedelta(minutes=10),
    evidence="Private evidence @everyone https://example.org",
)


@pytest.fixture
def commands():
    service = Mock(spec=PrivateJobService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_private_job_commands(tree, guild_scope=guild, private_jobs_service=service, currency_symbol="CP")
    return service, tree, tree.get_command("work", guild=guild)


def _interaction(actor=OWNER, *, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        created_at=NOW, guild_id=guild_id, locale=locale, user=SimpleNamespace(id=actor),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )


def _private_reply(interaction):
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    sent = interaction.followup.send.await_args
    assert sent.kwargs["ephemeral"] is True
    assert sent.kwargs["suppress_embeds"] is True
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    return sent.args[0]


@pytest.mark.asyncio
async def test_work_payload_is_guild_only_and_localized(commands):
    _, tree, work = commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    payload = await work.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"]["ru"] == "Разовые оплачиваемые задания компаний"
    assert {command["name"] for command in payload["options"]} == {
        "publish", "list", "mine", "claim", "submit", "accept", "cancel", "dispute", "resolve",
    }
    for command in payload["options"]:
        assert command["description_localizations"]["ru"] != command["description"]
        for option in command["options"]:
            assert option["description_localizations"]["ru"] != option["description"]
    resolve = next(command for command in payload["options"] if command["name"] == "resolve")
    choices = next(option for option in resolve["options"] if option["name"] == "decision")["choices"]
    assert [(choice["value"], choice["name_localizations"]["ru"]) for choice in choices] == [
        ("pay", "Выплатить исполнителю"), ("refund", "Вернуть средства компании"),
    ]


@pytest.mark.asyncio
async def test_publish_confirmation_does_not_debit_before_click_and_retries_the_same_request(commands):
    service, _, work = commands
    service.publish_job.return_value = OPEN_JOB
    request = _interaction()
    await work.get_command("publish").callback(
        request, OPEN_JOB.company_id, OPEN_JOB.title, OPEN_JOB.terms, OPEN_JOB.amount, 6, OPEN_JOB.publication_reason,
    )

    service.publish_job.assert_not_awaited()
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    prompt = request.edit_original_response.await_args.kwargs
    assert "80 CP" in prompt["content"] and "эскроу" in prompt["content"]
    assert prompt["allowed_mentions"].to_dict() == {"parse": []}
    view = prompt["view"]
    for outsider in (_interaction(OUTSIDER), _interaction(guild_id=GUILD_ID + 1)):
        assert await view.interaction_check(outsider) is False
        assert outsider.response.send_message.await_args.kwargs["ephemeral"] is True

    first_click = _interaction()
    first_click.edit_original_response.side_effect = RuntimeError("lost after commit")
    with pytest.raises(RuntimeError, match="lost after commit") as failure:
        await view.confirm.callback(first_click)
    await view.on_error(first_click, failure.value, view.confirm)
    first_click.followup.send.assert_awaited_once_with(message(first_click.locale, "confirmation.retry"), ephemeral=True)
    retry = _interaction(locale=discord.Locale.american_english)
    await view.confirm.callback(retry)
    first, second = service.publish_job.await_args_list
    assert first == second
    assert first.args == (GUILD_ID, OWNER)
    arguments = dict(first.kwargs)
    key = arguments.pop("idempotency_key")
    assert len(key) == 32
    assert arguments == {
        "company_id": OPEN_JOB.company_id, "title": OPEN_JOB.title, "terms": OPEN_JOB.terms,
        "amount": OPEN_JOB.amount, "deadline": OPEN_JOB.deadline, "reason": OPEN_JOB.publication_reason,
    }
    assert "company #11" in retry.edit_original_response.await_args.kwargs["content"]
    await view.confirm.callback(_interaction())
    assert service.publish_job.await_count == 2


@pytest.mark.asyncio
async def test_publish_maps_company_balance_error_in_the_confirmation(commands):
    service, _, work = commands
    service.publish_job.side_effect = LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
    request = _interaction()
    await work.get_command("publish").callback(request, 11, "Stage", "Build", 80, 6, "Reason")
    click = _interaction(locale=discord.Locale.american_english)
    await request.edit_original_response.await_args.kwargs["view"].confirm.callback(click)

    assert click.edit_original_response.await_args.kwargs["content"] == message(
        click.locale, "work.error.insufficient_company_funds",
    )


@pytest.mark.asyncio
async def test_catalogue_and_mine_pass_page_without_exposing_proof(commands):
    service, _, work = commands
    service.list_jobs.return_value = tuple(replace(OPEN_JOB, id=i) for i in range(1, 11))
    service.list_my_jobs.return_value = (SUBMITTED_JOB,)
    catalogue, mine = _interaction(), _interaction(WORKER)
    await work.get_command("list").callback(catalogue, page=2)
    await work.get_command("mine").callback(mine, page=3)

    service.list_jobs.assert_awaited_once_with(GUILD_ID, OWNER, page=2)
    service.list_my_jobs.assert_awaited_once_with(GUILD_ID, WORKER, page=3)
    assert "/work list page:3" in _private_reply(catalogue)
    mine_reply = _private_reply(mine)
    assert "работа сдана" in mine_reply and SUBMITTED_JOB.evidence not in mine_reply
    assert OPEN_JOB.publication_reason not in mine_reply


@pytest.mark.asyncio
async def test_detail_exposes_proof_only_after_service_authorization_and_privately(commands):
    service, _, work = commands
    service.get_job.side_effect = [SUBMITTED_JOB, PrivateJobError(PrivateJobErrorReason.NOT_JOB_PARTY)]
    approved, denied = _interaction(OWNER), _interaction(OUTSIDER, locale=discord.Locale.american_english)
    for request in (approved, denied):
        await work.get_command("list").callback(request, job_id=SUBMITTED_JOB.id)

    assert "Private evidence" in _private_reply(approved)
    assert OPEN_JOB.publication_reason not in _private_reply(approved)
    assert _private_reply(denied) == message(denied.locale, "work.error.not_job_party")
    assert service.get_job.await_args_list[1].args == (GUILD_ID, OUTSIDER, SUBMITTED_JOB.id)


@pytest.mark.parametrize(("command", "method", "actor", "args", "kwargs", "result"), [
    ("claim", "claim_job", WORKER, (), {}, CLAIMED_JOB),
    ("submit", "submit_job", WORKER, ("Private proof",), {"evidence": "Private proof"}, SUBMITTED_JOB),
    ("accept", "accept_job", OWNER, ("Accepted",), {"reason": "Accepted"}, replace(SUBMITTED_JOB, status=PrivateJobStatus.PAID)),
    ("cancel", "cancel_job", WORKER, ("Unavailable",), {"reason": "Unavailable"}, replace(CLAIMED_JOB, status=PrivateJobStatus.CANCELLED)),
    ("dispute", "dispute_job", OWNER, ("Review requested",), {"reason": "Review requested"}, replace(SUBMITTED_JOB, status=PrivateJobStatus.DISPUTED)),
    ("dispute", "dispute_job", ARBITER, ("Review overdue",), {"reason": "Review overdue"}, replace(SUBMITTED_JOB, status=PrivateJobStatus.DISPUTED)),
    ("resolve", "resolve_job", ARBITER, (app_commands.Choice(name="Pay", value="pay"), "Reviewed"), {"pay": True, "reason": "Reviewed"}, replace(SUBMITTED_JOB, status=PrivateJobStatus.PAID)),
    ("resolve", "resolve_job", ARBITER, (app_commands.Choice(name="Refund", value="refund"), "Reviewed"), {"pay": False, "reason": "Reviewed"}, replace(SUBMITTED_JOB, status=PrivateJobStatus.CANCELLED)),
])
@pytest.mark.asyncio
async def test_work_lifecycle_routes_the_actor_and_reports_private_status(commands, command, method, actor, args, kwargs, result):
    service, _, work = commands
    operation = getattr(service, method)
    operation.return_value = result
    request = _interaction(actor)

    await work.get_command(command).callback(request, OPEN_JOB.id, *args)

    operation.assert_awaited_once_with(GUILD_ID, actor, OPEN_JOB.id, **kwargs)
    reply = _private_reply(request)
    assert message(request.locale, f"work.status.{result.status.value.lower()}") in reply
    assert "Private evidence" not in reply and "Private proof" not in reply


@pytest.mark.asyncio
async def test_wrong_guild_access_error_is_localized_without_public_reply(commands):
    service, _, work = commands
    service.list_jobs.side_effect = CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
    request = _interaction(guild_id=GUILD_ID + 1)
    await work.get_command("list").callback(request)
    assert _private_reply(request) == message(request.locale, "access.wrong_guild")


@pytest.mark.parametrize("locale", [discord.Locale.american_english, discord.Locale.russian])
def test_private_job_status_errors_and_ledger_kinds_have_labels(locale):
    for reason in PrivateJobErrorReason:
        assert message(locale, f"work.error.{reason.value}")
    for status in PrivateJobStatus:
        assert message(locale, f"work.status.{status.value.lower()}")
    for kind in ("private_job_fund", "private_job_pay", "private_job_refund"):
        assert message(locale, f"wallet.history.{kind}")
