from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.law import ActKind, LawError, LawErrorReason, LawRevision, LawService
from civitas.bot.law import add_law_commands
from civitas.bot.localization import CommandTranslator

GUILD_ID, ACTOR_ID, INTERACTION_ID = 101, 201, 999
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.fixture
def registries():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD_ID)
    laws = Mock(spec=LawService)
    decrees = Mock(spec=LawService)
    add_law_commands(tree, guild_scope=scope, service=laws)
    add_law_commands(tree, guild_scope=scope, service=decrees, act_kind=ActKind.DECREE)
    return tree, scope, laws, decrees


def interaction(locale):
    return SimpleNamespace(
        id=INTERACTION_ID, guild_id=GUILD_ID, user=SimpleNamespace(id=ACTOR_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def reply_text(request):
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    request.followup.send.assert_awaited_once()
    reply = request.followup.send.await_args
    assert reply.kwargs["ephemeral"] is True
    assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    return reply.args[0]


@pytest.mark.asyncio
async def test_both_registries_have_separate_guild_scoped_translated_commands(registries):
    tree, scope, _, _ = registries
    assert {group.name for group in tree.get_commands(guild=scope)} == {"decree", "law"}
    assert not tree.get_commands()
    assert not tree.get_commands(guild=discord.Object(id=GUILD_ID + 1))

    expected_commands = {"register", "confirm", "show", "revision", "list"}
    translated = {}
    for name in ("decree", "law"):
        group = tree.get_command(name, guild=scope)
        payload = await group.get_translated_payload(tree, CommandTranslator())
        assert payload["description_localizations"]["ru"]
        translated[name] = {command["name"]: command for command in payload["options"]}
        assert set(translated[name]) == expected_commands
        assert all(command["description_localizations"]["ru"] for command in payload["options"])

    decree = translated["decree"]
    assert "as president" in decree["register"]["description"]
    assert "публикацию" in decree["confirm"]["description_localizations"]["ru"]
    assert "second senator" in translated["law"]["confirm"]["description"]


@pytest.mark.asyncio
async def test_decree_register_and_confirm_route_independently_and_describe_their_status(registries):
    tree, scope, laws, decrees = registries
    law = tree.get_command("law", guild=scope)
    decree = tree.get_command("decree", guild=scope)
    draft = LawRevision(
        id=19, guild_id=1, code="ACT-01", title="Published decree", text="Full text",
        source_url="https://discord.com/channels/101/102/103", effective_at=NOW, proposer_citizen_id=1,
        proposed_at=NOW, act_kind=ActKind.DECREE,
    )
    decrees.register.return_value = draft
    decrees.confirm.return_value = replace(draft, confirmed_by_citizen_id=2, confirmed_at=NOW)
    laws.confirm.return_value = replace(draft, act_kind=ActKind.LAW, confirmed_by_citizen_id=2, confirmed_at=NOW)
    register_request = interaction(discord.Locale.russian)
    decree_request = interaction(discord.Locale.american_english)
    law_request = interaction(discord.Locale.american_english)

    await decree.get_command("register").callback(
        register_request, draft.code, draft.title, draft.text, draft.source_url, NOW.isoformat(),
    )
    await decree.get_command("confirm").callback(decree_request, draft.id)
    await law.get_command("confirm").callback(law_request, draft.id)

    decrees.register.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, code=draft.code, title=draft.title, text=draft.text, source_url=draft.source_url,
        effective_at=NOW.isoformat(), idempotency_key=str(INTERACTION_ID),
    )
    laws.register.assert_not_awaited()
    decrees.confirm.assert_awaited_once_with(GUILD_ID, ACTOR_ID, draft.id)
    laws.confirm.assert_awaited_once_with(GUILD_ID, ACTOR_ID, draft.id)
    assert "Сенатору предстоит сверить публикацию" in reply_text(register_request)
    assert "confirmed by a senator" in reply_text(decree_request)
    assert "Confirmed by two senators" in reply_text(law_request)


@pytest.mark.asyncio
async def test_decree_list_and_validation_use_the_decree_namespace(registries):
    tree, scope, laws, decrees = registries
    decree = tree.get_command("decree", guild=scope)
    decrees.list_effective.return_value = ()
    decrees.confirm.side_effect = LawError(LawErrorReason.REVISION_NOT_FOUND)
    list_request = interaction(discord.Locale.russian)
    confirm_request = interaction(discord.Locale.american_english)

    await decree.get_command("list").callback(list_request, 2)
    await decree.get_command("confirm").callback(confirm_request, 19)

    decrees.list_effective.assert_awaited_once_with(GUILD_ID, page=2)
    laws.list_effective.assert_not_awaited()
    assert "На странице 2 нет действующих указов." in reply_text(list_request)
    assert "No decree revision matches this request." in reply_text(confirm_request)
