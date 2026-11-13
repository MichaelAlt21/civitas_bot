from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.government import GovernmentError, GovernmentErrorReason, GovernmentService
from civitas.bot.government import _reconcile_office_roles, add_government_commands
from civitas.bot.localization import CommandTranslator
from civitas.domain.government import (
    CandidateTally,
    Election,
    ElectionProtocol,
    ElectionStatus,
    OfficeKind,
    OfficeRoleAssignment,
    OfficeTerm,
)

GUILD = 1200
COMMISSIONER = 1201
CANDIDATE = 1202
OTHER_CANDIDATE = 1203
PRESIDENT_ROLE = 1301
PARLIAMENT_ROLE = 1302
START = datetime(2026, 11, 11, tzinfo=UTC)
END = START + timedelta(days=2)
SOURCE = "https://example.org/polls/record"
OPEN = Election(
    id=3, guild_id=1, office=OfficeKind.PRESIDENT, seats=1, term_days=28, quorum=2,
    opens_at=START, closes_at=END, opened_by_citizen_id=1, reason="Private audit reason",
    request_key="open", eligible_citizen_ids=(1, 2, 3), candidate_citizen_ids=(2, 3),
    candidate_discord_user_ids=(CANDIDATE, OTHER_CANDIDATE),
)
PROTOCOL = ElectionProtocol(SOURCE, 2, (CandidateTally(2, 2, CANDIDATE), CandidateTally(3, 0, OTHER_CANDIDATE)),
                            (2,), 1, END, "Private tally reason")
CERTIFIED = replace(OPEN, status=ElectionStatus.APPLIED, protocol=PROTOCOL, certified_at=END)


@pytest.fixture
def commands():
    service = Mock(spec=GovernmentService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD)
    add_government_commands(tree, guild_scope=scope, government_service=service)
    government = tree.get_command("government", guild=scope)
    election = tree.get_command("election", guild=scope)
    return service, tree, government, election, election.get_command("commission")


def interaction(*, guild_id=GUILD, locale=discord.Locale.russian):
    return SimpleNamespace(
        id=1001, guild_id=guild_id, user=SimpleNamespace(id=COMMISSIONER), locale=locale,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def reply(command, *, private):
    if private:
        command.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    else:
        command.response.defer.assert_awaited_once_with(thinking=True)
    sent = command.followup.send.await_args
    assert sent.kwargs["ephemeral"] is private
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert sent.kwargs["suppress_embeds"] is True
    return sent.args[0]


@pytest.mark.asyncio
async def test_localized_government_commands_are_guild_only(commands):
    _, tree, government, election, _ = commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD + 1)) == []
    assert government.get_command("offices") is not None
    payload = await election.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Выборы и подтверждённые итоги"}
    commission = next(option for option in payload["options"] if option["name"] == "commission")
    assert {item["name"] for item in commission["options"]} == {"open", "record", "inspect", "certify"}


@pytest.mark.asyncio
async def test_offices_are_public_and_wrong_guild_is_denied_privately(commands):
    service, _, government, _, _ = commands
    service.list_offices.return_value = (OfficeTerm(3, OfficeKind.PRESIDENT, 1, 2, CANDIDATE, START, END),)
    public = interaction()
    outside = interaction(guild_id=None)
    callback = government.get_command("offices").callback

    await callback(public)
    await callback(outside)

    service.list_offices.assert_awaited_once_with(GUILD, COMMISSIONER)
    assert f"Президент · <@{CANDIDATE}>" in reply(public, private=False)
    outside.response.send_message.assert_awaited_once_with(
        "Команды Civitas доступны на игровом сервере.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_public_results_use_certified_service_and_hide_audit_metadata(commands):
    service, _, _, election, _ = commands
    service.get_public_result.return_value = CERTIFIED
    command = interaction()

    await election.get_command("results").callback(command, CERTIFIED.id)

    service.get_public_result.assert_awaited_once_with(GUILD, COMMISSIONER, CERTIFIED.id)
    service.get_election.assert_not_awaited()
    result = reply(command, private=False)
    assert f"<{SOURCE}>" in result
    assert f"<@{CANDIDATE}> · 2" in result
    assert "Избраны" in result
    assert OPEN.reason not in result
    assert PROTOCOL.reason not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [GovernmentErrorReason.RESULT_NOT_PUBLIC, GovernmentErrorReason.ELECTION_NOT_FOUND])
async def test_public_lookup_does_not_expose_provisional_election(commands, reason):
    service, _, _, election, _ = commands
    service.get_public_result.side_effect = GovernmentError(reason)
    command = interaction()

    await election.get_command("results").callback(command, 5)

    assert reply(command, private=False) == "Подтверждённый публичный результат этих выборов пока недоступен."
    service.get_election.assert_not_awaited()


@pytest.mark.asyncio
async def test_commission_commands_forward_aggregate_only_and_reply_privately(commands):
    service, _, _, _, commission = commands
    pending = replace(OPEN, status=ElectionStatus.PENDING_CERTIFICATION, protocol=PROTOCOL)
    service.open_election.return_value = OPEN
    service.record_protocol.return_value = pending
    service.get_election.return_value = pending
    service.certify_election.return_value = CERTIFIED
    opening, recording, inspect, certify = [interaction() for _ in range(4)]

    await commission.get_command("open").callback(opening, "PRESIDENT", f"<@{CANDIDATE}>, {OTHER_CANDIDATE}", "Open")
    await commission.get_command("record").callback(recording, OPEN.id, SOURCE, 2, f"{CANDIDATE}=2,<@!{OTHER_CANDIDATE}>=0", "Count")
    await commission.get_command("inspect").callback(inspect, OPEN.id)
    await commission.get_command("certify").callback(certify, OPEN.id, "Check")

    service.open_election.assert_awaited_once_with(
        GUILD, COMMISSIONER, office=OfficeKind.PRESIDENT, candidate_user_ids=(CANDIDATE, OTHER_CANDIDATE),
        reason="Open", idempotency_key=str(opening.id), runoff_of_id=None,
    )
    service.record_protocol.assert_awaited_once_with(
        GUILD, COMMISSIONER, OPEN.id, source_url=SOURCE, participants=2,
        tallies={CANDIDATE: 2, OTHER_CANDIDATE: 0}, reason="Count",
    )
    service.get_election.assert_awaited_once_with(GUILD, COMMISSIONER, OPEN.id)
    service.certify_election.assert_awaited_once_with(GUILD, COMMISSIONER, OPEN.id, reason="Check")
    assert "Кандидаты" in reply(opening, private=True)
    assert "ожидает независимого подтверждения" in reply(recording, private=True)
    assert SOURCE in reply(inspect, private=True)
    assert "Избраны" in reply(certify, private=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("tally", ["1202=1,1202=0", "1202=1.5", "1202=-1", "@everyone=1", "1202=1=1"])
async def test_malformed_tallies_never_reach_service(commands, tally):
    service, _, _, _, commission = commands
    command = interaction(locale=discord.Locale.german)

    await commission.get_command("record").callback(command, OPEN.id, SOURCE, 1, tally, "Record")

    service.record_protocol.assert_not_awaited()
    assert reply(command, private=True).startswith("Enter one non-negative vote total per candidate")


def fake_role(role_id, **permissions):
    return SimpleNamespace(id=role_id, is_assignable=Mock(return_value=True), permissions=discord.Permissions(**permissions))


def fake_member(role=None):
    return SimpleNamespace(get_role=Mock(return_value=role), add_roles=AsyncMock(), remove_roles=AsyncMock())


@pytest.mark.asyncio
async def test_office_sync_adds_active_role_removes_expired_role_and_skips_unsafe_role():
    active, prior, unsafe = fake_member(), fake_member(), fake_member()
    president = fake_role(PRESIDENT_ROLE)
    parliament = fake_role(PARLIAMENT_ROLE, administrator=True)
    prior.get_role.return_value = president
    members = {CANDIDATE: active, OTHER_CANDIDATE: prior, COMMISSIONER: unsafe}
    guild = SimpleNamespace(
        unavailable=False, get_role={PRESIDENT_ROLE: president, PARLIAMENT_ROLE: parliament}.get,
        me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
        fetch_member=AsyncMock(side_effect=members.get),
    )
    service = Mock(spec=GovernmentService)
    service.office_role_assignments.return_value = (
        OfficeRoleAssignment(OfficeKind.PRESIDENT, CANDIDATE, True),
        OfficeRoleAssignment(OfficeKind.PRESIDENT, OTHER_CANDIDATE, False),
        OfficeRoleAssignment(OfficeKind.PARLIAMENT, COMMISSIONER, True),
    )
    client = SimpleNamespace(get_guild=Mock(return_value=guild))

    await _reconcile_office_roles(client, service, GUILD, {
        OfficeKind.PRESIDENT: PRESIDENT_ROLE, OfficeKind.PARLIAMENT: PARLIAMENT_ROLE,
    })

    active.add_roles.assert_awaited_once_with(president, reason="Civitas public office sync")
    prior.remove_roles.assert_awaited_once_with(president, reason="Civitas public office sync")
    assert [call.args[0] for call in guild.fetch_member.await_args_list] == [CANDIDATE, OTHER_CANDIDATE]
    unsafe.add_roles.assert_not_awaited()
