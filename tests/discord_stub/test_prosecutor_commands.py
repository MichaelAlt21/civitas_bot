from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.prosecutor import (
    PROSECUTOR_REASON_MAX_LENGTH,
    ProsecutorAction,
    ProsecutorError,
    ProsecutorErrorReason,
    ProsecutorService,
)
from civitas.bot.localization import CommandTranslator
from civitas.bot.prosecutor import add_prosecutor_commands


GUILD_ID = 601
ACTOR_ID = 602
SUBJECT_ID = 603
INTERACTION_ID = 604
MOTION_ID = 41
NOW = datetime(2026, 11, 16, tzinfo=UTC)
PRIVATE_REASON = "Private reason @everyone"


@pytest.fixture
def commands():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    service = Mock(spec=ProsecutorService)
    add_prosecutor_commands(tree, guild_scope=guild, service=service)
    return service, tree, tree.get_command("prosecutor", guild=guild)


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        id=INTERACTION_ID, guild_id=guild_id, user=SimpleNamespace(id=ACTOR_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def candidate(*, guild_id=GUILD_ID, bot=False):
    return SimpleNamespace(id=SUBJECT_ID, guild=SimpleNamespace(id=guild_id), bot=bot)


def motion(action, approval_count=0):
    return SimpleNamespace(
        id=MOTION_ID, subject_user_id=SUBJECT_ID, action=action, approval_count=approval_count,
        applied_at=NOW if approval_count == 2 else None, reason=PRIVATE_REASON,
    )


def private_reply(request):
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    request.followup.send.assert_awaited_once()
    reply = request.followup.send.await_args
    assert reply.kwargs["ephemeral"] is True
    assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert reply.kwargs["suppress_embeds"] is True
    assert PRIVATE_REASON not in reply.args[0]
    return reply.args[0]


@pytest.mark.asyncio
async def test_prosecutor_commands_are_guild_scoped_and_localized(commands):
    _, tree, prosecutor = commands
    assert not tree.get_commands()
    assert not tree.get_commands(guild=discord.Object(id=GUILD_ID + 1))
    payload = await prosecutor.get_translated_payload(tree, CommandTranslator())
    entries = {item["name"]: item for item in payload["options"]}
    assert set(entries) == {"approve", "current", "motion", "nominate", "propose_removal"}
    assert all(item["description_localizations"]["ru"] for item in entries.values())
    member, reason = entries["nominate"]["options"]
    assert member["type"] == discord.AppCommandOptionType.user.value
    assert reason["max_length"] == PROSECUTOR_REASON_MAX_LENGTH


@pytest.mark.asyncio
async def test_prosecutor_appointment_and_removal_proposals_forward_the_kind(commands):
    service, _, prosecutor = commands
    service.propose.side_effect = [motion(ProsecutorAction.APPOINT), motion(ProsecutorAction.REMOVE)]
    appointment, removal = interaction(), interaction(locale=discord.Locale.american_english)

    await prosecutor.get_command("nominate").callback(appointment, candidate(), PRIVATE_REASON)
    await prosecutor.get_command("propose_removal").callback(removal, PRIVATE_REASON)

    calls = service.propose.await_args_list
    assert [call.args for call in calls] == [(GUILD_ID, ACTOR_ID), (GUILD_ID, ACTOR_ID)]
    assert calls[0].kwargs == {
        "action": ProsecutorAction.APPOINT, "candidate_user_id": SUBJECT_ID,
        "reason": PRIVATE_REASON, "idempotency_key": str(INTERACTION_ID),
    }
    assert calls[1].kwargs == {
        "action": ProsecutorAction.REMOVE, "reason": PRIVATE_REASON, "idempotency_key": str(INTERACTION_ID),
    }
    assert "Кандидатура прокурора" in private_reply(appointment)
    assert "Prosecutor removal proposal" in private_reply(removal)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [candidate(bot=True), candidate(guild_id=GUILD_ID + 1)])
async def test_prosecutor_nomination_rejects_invalid_target(commands, target):
    service, _, prosecutor = commands
    request = interaction()

    await prosecutor.get_command("nominate").callback(request, target, PRIVATE_REASON)

    service.propose.assert_not_awaited()
    assert "Выберите допущенного гражданина" in private_reply(request)


@pytest.mark.asyncio
async def test_approval_reports_signatures_and_only_applied_removal_has_an_effective_date(commands):
    service, _, prosecutor = commands
    service.approve.side_effect = [motion(ProsecutorAction.REMOVE, 1), motion(ProsecutorAction.REMOVE, 2)]
    first, second = interaction(locale=discord.Locale.american_english), interaction()
    approve = prosecutor.get_command("approve").callback

    await approve(first, MOTION_ID, "First signature")
    await approve(second, MOTION_ID, "Second independent signature")

    assert all(call.args == (GUILD_ID, ACTOR_ID, MOTION_ID) for call in service.approve.await_args_list)
    first_reply = private_reply(first)
    assert "1 of 2" in first_reply and "Removed from office" not in first_reply
    second_reply = private_reply(second)
    assert "2 из 2" in second_reply
    assert f"Освобождение вступило в силу · {discord.utils.format_dt(NOW, style='f')}" in second_reply


@pytest.mark.asyncio
async def test_prosecutor_queries_and_role_rejections_remain_private(commands):
    service, _, prosecutor = commands
    service.current.side_effect = [None, motion(ProsecutorAction.APPOINT, 2)]
    service.motion.return_value = motion(ProsecutorAction.REMOVE, 1)
    service.approve.side_effect = ProsecutorError(ProsecutorErrorReason.SENATOR_REQUIRED)
    foreign = interaction(guild_id=GUILD_ID + 1)
    vacant, appointed, pending, unauthorized = [interaction() for _ in range(4)]

    await prosecutor.get_command("current").callback(foreign)
    await prosecutor.get_command("current").callback(vacant)
    await prosecutor.get_command("current").callback(appointed)
    await prosecutor.get_command("motion").callback(pending, MOTION_ID)
    await prosecutor.get_command("approve").callback(unauthorized, MOTION_ID, PRIVATE_REASON)

    assert [call.args for call in service.current.await_args_list] == [(GUILD_ID,), (GUILD_ID,)]
    service.motion.assert_awaited_once_with(GUILD_ID, MOTION_ID)
    assert "сервер" in private_reply(foreign)
    assert "свободна" in private_reply(vacant)
    assert "Действующий прокурор" in private_reply(appointed)
    assert "1 из 2" in private_reply(pending)
    assert "действующий сенатор" in private_reply(unauthorized)
