from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.court import CourtService
from civitas.app.court_appeals import AppealOutcome, AppealTally, CourtAppeal, CourtAppealError, CourtAppealErrorReason, CourtAppealService
from civitas.bot.appeals import add_appeal_commands
from civitas.bot.court import add_court_commands
from civitas.bot.localization import CommandTranslator


GUILD_ID = 101_000_000_000_000_001
ACTOR_ID = GUILD_ID + 1
ROSTER = tuple(range(GUILD_ID + 100, GUILD_ID + 123))
CASE_ID = 41
FILED = datetime(2026, 11, 16, 12, tzinfo=UTC)
OPENED = FILED + timedelta(hours=1)
CLOSES = OPENED + timedelta(hours=48)
SOURCE = f"https://discord.com/channels/{GUILD_ID}/101000000000000004/101000000000000005"


@pytest.fixture
def commands():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD_ID)
    service = Mock(spec=CourtAppealService)
    add_court_commands(tree, guild_scope=scope, service=Mock(spec=CourtService))
    add_appeal_commands(tree, guild_scope=scope, service=service)
    court = tree.get_command("court", guild=scope)
    return service, tree, court, court.get_command("appeal")


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        guild_id=guild_id, user=SimpleNamespace(id=ACTOR_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def appeal_record():
    return CourtAppeal(
        guild_id=1, case_id=CASE_ID, appellant_citizen_id=4, filed_at=FILED,
        opened_by_citizen_id=5, opened_at=OPENED, source_url=SOURCE, eligible_user_ids=ROSTER,
        eligible_count=len(ROSTER), quorum=10, closes_at=CLOSES,
    )


def reply(request):
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    contents = []
    for call in request.followup.send.await_args_list:
        assert call.kwargs["ephemeral"] is True
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert call.kwargs["suppress_embeds"] is True
        assert len(call.args[0].encode("utf-16-le")) // 2 <= 2000
        contents.append(call.args[0])
    assert contents
    return "\n".join(contents)


@pytest.mark.asyncio
async def test_appeal_commands_are_guild_scoped_and_localized(commands):
    _, tree, court, _ = commands
    assert not tree.get_commands()
    assert not tree.get_commands(guild=discord.Object(id=GUILD_ID + 1))
    payload = await court.get_translated_payload(tree, CommandTranslator())
    group = next(option for option in payload["options"] if option["name"] == "appeal")
    assert group["type"] == discord.AppCommandOptionType.subcommand_group.value
    entries = {entry["name"]: entry for entry in group["options"]}
    assert set(entries) == {"file", "open", "voters", "record", "confirm", "show"}
    assert entries["confirm"]["description_localizations"]["ru"] == "Подтвердить итоги публичного голосования вторым членом комиссии"
    options = {entry["name"]: entry for entry in entries["record"]["options"]}
    assert options["round_number"]["max_value"] == 2
    assert options["participants"]["min_value"] == 0
    assert "including abstentions" in options["participants"]["description"]
    assert "воздержавшихся" in options["participants"]["description_localizations"]["ru"]


@pytest.mark.asyncio
async def test_file_and_open_forward_inputs_keep_voter_roster_private(commands):
    service, _, _, appeal = commands
    service.file.return_value = CourtAppeal(guild_id=1, case_id=CASE_ID, appellant_citizen_id=4, filed_at=FILED)
    service.open.return_value = appeal_record()
    filing, opening = interaction(), interaction(locale=discord.Locale.american_english)

    await appeal.get_command("file").callback(filing, CASE_ID)
    await appeal.get_command("open").callback(opening, CASE_ID, SOURCE)

    service.file.assert_awaited_once_with(GUILD_ID, ACTOR_ID, CASE_ID)
    service.open.assert_awaited_once_with(GUILD_ID, ACTOR_ID, CASE_ID, source_url=SOURCE)
    assert "Комиссии предстоит открыть" in reply(filing)
    text = reply(opening)
    assert f"<{SOURCE}>" in text and "Eligible voters 23 · Quorum 10" in text
    assert all(str(voter) not in text for voter in ROSTER)


@pytest.mark.asyncio
async def test_commission_voter_roster_is_paginated_and_private(commands):
    service, _, _, appeal = commands
    service.voters.return_value = ROSTER
    second, empty = interaction(), interaction()

    await appeal.get_command("voters").callback(second, CASE_ID, 2)
    await appeal.get_command("voters").callback(empty, CASE_ID, 3)

    assert all(call.args == (GUILD_ID, ACTOR_ID, CASE_ID) for call in service.voters.await_args_list)
    text = reply(second)
    assert "Страница 2/2" in text
    assert all(str(voter) not in text for voter in ROSTER[:20])
    assert all(str(voter) in text for voter in ROSTER[20:])
    assert "<@" not in text
    assert "На странице 3 нет ID избирателей." in reply(empty)


@pytest.mark.asyncio
async def test_record_and_confirm_keep_unsigned_and_certified_totals_separate(commands):
    service, _, _, appeal = commands
    service.record.return_value = AppealTally(
        guild_id=1, case_id=CASE_ID, round_number=1, participants=7, votes_to_cancel=4,
        recorded_by_citizen_id=7, recorded_at=CLOSES,
    )
    service.confirm.return_value = replace(
        appeal_record(), closes_at=CLOSES + timedelta(hours=24), round_number=2, participants=7, votes_to_cancel=4,
    )
    service.show.side_effect = [
        appeal_record(),
        replace(appeal_record(), outcome=AppealOutcome.OVERTURNED, resolved_at=CLOSES, participants=18, votes_to_cancel=10),
    ]
    recording, confirmation, during_review, final = (interaction() for _ in range(4))

    await appeal.get_command("record").callback(recording, CASE_ID, 1, 7, 4)
    await appeal.get_command("show").callback(during_review, CASE_ID)
    await appeal.get_command("confirm").callback(confirmation, CASE_ID, 1)
    await appeal.get_command("show").callback(final, CASE_ID)

    service.record.assert_awaited_once_with(GUILD_ID, ACTOR_ID, CASE_ID, round_number=1, participants=7, votes_to_cancel=4)
    service.confirm.assert_awaited_once_with(GUILD_ID, ACTOR_ID, CASE_ID, round_number=1)
    assert service.show.await_count == 2
    assert "Итог должен подтвердить второй член комиссии" in reply(recording)
    assert "За отмену" not in reply(during_review)
    assert "Голосование продлено один раз" in reply(confirmation)
    text = reply(final)
    assert "Апелляция отменила решение суда" in text and "Участников 18 · За отмену 10" in text
    assert all(str(voter) not in text for voter in ROSTER)


@pytest.mark.asyncio
async def test_foreign_guild_and_commission_errors_remain_private(commands):
    service, _, _, appeal = commands
    service.confirm.side_effect = CourtAppealError(CourtAppealErrorReason.INDEPENDENT_CONFIRMATION)
    foreign, denied = interaction(guild_id=GUILD_ID + 1), interaction(locale=discord.Locale.american_english)

    await appeal.get_command("show").callback(foreign, CASE_ID)
    await appeal.get_command("confirm").callback(denied, CASE_ID, 1)

    service.show.assert_not_awaited()
    assert "сервер" in reply(foreign)
    assert "A different commissioner must confirm the tally." in reply(denied)
