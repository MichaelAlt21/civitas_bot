from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.court import COURT_REASON_MAX_LENGTH, CourtError, CourtErrorReason, CourtService
from civitas.bot.court import add_court_commands
from civitas.bot.localization import CommandTranslator


GUILD_ID = 701
ACTOR_ID = 702
CANDIDATE_ID = 703
NOMINATION_ID = 41
INTERACTION_ID = 705
NOW = datetime(2026, 11, 15, tzinfo=UTC)


@pytest.fixture
def commands():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD_ID)
    service = Mock(spec=CourtService)
    add_court_commands(tree, guild_scope=scope, service=service)
    return service, tree, tree.get_command("court", guild=scope)


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        id=INTERACTION_ID, guild_id=guild_id, user=SimpleNamespace(id=ACTOR_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def candidate(*, guild_id=GUILD_ID, bot=False):
    return SimpleNamespace(id=CANDIDATE_ID, guild=SimpleNamespace(id=guild_id), bot=bot)


def nomination(approvals):
    return SimpleNamespace(
        id=NOMINATION_ID, candidate_user_id=CANDIDATE_ID, approval_count=approvals,
        starts_at=NOW if approvals == 2 else None, ends_at=NOW + timedelta(days=28) if approvals == 2 else None,
        reason="Internal reason @everyone **example**",
    )


def private_reply(request):
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    request.followup.send.assert_awaited_once()
    reply = request.followup.send.await_args
    assert reply.kwargs["ephemeral"] is True
    assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert reply.kwargs["suppress_embeds"] is True
    assert "Internal reason" not in reply.args[0]
    return reply.args[0]


@pytest.mark.asyncio
async def test_court_commands_are_guild_scoped_and_localized(commands):
    _, tree, court = commands
    assert not tree.get_commands()
    assert not tree.get_commands(guild=discord.Object(id=GUILD_ID + 1))
    payload = await court.get_translated_payload(tree, CommandTranslator())
    entries = {item["name"]: item for item in payload["options"]}
    assert set(entries) == {"approve", "current", "nominate", "nomination"}
    assert "действующего верховного судью" in entries["current"]["description_localizations"]["ru"]
    member, reason = entries["nominate"]["options"]
    assert member["type"] == discord.AppCommandOptionType.user.value
    assert reason["max_length"] == COURT_REASON_MAX_LENGTH


@pytest.mark.asyncio
async def test_nominate_forwards_member_reason_and_request_id_without_echoing_reason(commands):
    service, _, court = commands
    service.nominate.return_value = nomination(0)
    request = interaction()

    await court.get_command("nominate").callback(request, candidate(), "Internal reason @everyone **example**")

    service.nominate.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, candidate_user_id=CANDIDATE_ID, reason="Internal reason @everyone **example**",
        idempotency_key=str(INTERACTION_ID),
    )
    reply = private_reply(request)
    assert f"№ {NOMINATION_ID} · <@{CANDIDATE_ID}>" in reply
    assert "0 из 2" in reply
    assert "Срок мандата" not in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [candidate(bot=True), candidate(guild_id=GUILD_ID + 1)])
async def test_nominate_rejects_bot_and_foreign_member_locally(commands, target):
    service, _, court = commands
    request = interaction()

    await court.get_command("nominate").callback(request, target, "Test")

    service.nominate.assert_not_awaited()
    assert "Выберите допущенного гражданина" in private_reply(request)


@pytest.mark.asyncio
async def test_approve_shows_progress_and_only_renders_term_after_appointment(commands):
    service, _, court = commands
    service.approve.side_effect = [nomination(1), nomination(2)]
    callback = court.get_command("approve").callback
    first = interaction(locale=discord.Locale.american_english)
    second = interaction(locale=discord.Locale.american_english)

    await callback(first, NOMINATION_ID, "First signature")
    await callback(second, NOMINATION_ID, "Independent signature")

    calls = service.approve.await_args_list
    assert all(call.args == (GUILD_ID, ACTOR_ID, NOMINATION_ID) for call in calls)
    assert [call.kwargs["reason"] for call in calls] == ["First signature", "Independent signature"]
    pending = private_reply(first)
    assert "1 of 2" in pending
    assert "Term" not in pending
    appointed = private_reply(second)
    assert "2 of 2" in appointed
    assert f"Term · {discord.utils.format_dt(NOW, style='f')}" in appointed


@pytest.mark.asyncio
async def test_court_queries_and_rejections_stay_private(commands):
    service, _, court = commands
    service.current.side_effect = [None, nomination(2)]
    service.get_nomination.side_effect = CourtError(CourtErrorReason.NOMINATION_NOT_FOUND)
    service.approve.side_effect = CourtError(CourtErrorReason.DEPUTY_REQUIRED)
    foreign = interaction(guild_id=GUILD_ID + 1)
    vacant, appointed, missing, denied = [interaction() for _ in range(4)]

    current = court.get_command("current").callback
    await current(foreign)
    await current(vacant)
    await current(appointed)
    await court.get_command("nomination").callback(missing, NOMINATION_ID)
    await court.get_command("approve").callback(denied, NOMINATION_ID, "Test")

    assert [call.args for call in service.current.await_args_list] == [(GUILD_ID,), (GUILD_ID,)]
    service.get_nomination.assert_awaited_once_with(GUILD_ID, NOMINATION_ID)
    assert "сервер" in private_reply(foreign)
    assert "свободна" in private_reply(vacant)
    assert "Действующий верховный судья" in private_reply(appointed)
    assert "не найдена" in private_reply(missing)
    assert "действующий сенатор" in private_reply(denied)
