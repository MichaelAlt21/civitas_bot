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
    CrisisResolution,
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
    by_name = {item["name"]: item for item in commission["options"]}
    assert by_name.keys() == {"active", "open", "record", "inspect", "certify", "resolve_crisis", "confirm_crisis"}
    for command in ("resolve_crisis", "confirm_crisis"):
        assert {item["name"] for item in by_name[command]["options"] if item["required"]} == {"election_id", "reason"}
        assert "ru" in by_name[command]["description_localizations"]
    opening = next(option for option in commission["options"] if option["name"] == "open")
    retry = next(option for option in opening["options"] if option["name"] == "retry_of_id")
    assert retry["description_localizations"] == {"ru": "Номер несостоявшихся выборов для повторного голосования"}
    assert not retry["required"]


@pytest.mark.asyncio
async def test_active_elections_list_status_and_deadline_privately(commands):
    service, _, _, _, commission = commands
    service.list_unfinished_elections.return_value = (
        replace(OPEN, status=ElectionStatus.PENDING_CERTIFICATION, protocol=PROTOCOL),
    )
    command = interaction(locale=discord.Locale.american_english)

    await commission.get_command("active").callback(command)

    service.list_unfinished_elections.assert_awaited_once_with(GUILD, COMMISSIONER)
    content = reply(command, private=True)
    assert f"#{OPEN.id}" in content and discord.utils.format_dt(END, style="f") in content
    assert SOURCE not in content and OPEN.reason not in content


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
@pytest.mark.parametrize("locale, retry_label, crisis_label", [
    (discord.Locale.russian, "допускается одно повторное голосование", "игровой политический кризис"),
    (discord.Locale.american_english, "one repeat vote allowed", "in-game political crisis"),
])
async def test_public_history_marks_failed_votes_and_details_explain_the_in_game_crisis(
    commands, locale, retry_label, crisis_label,
):
    service, _, _, election, _ = commands
    protocol = replace(PROTOCOL, winners=(), reason="Private evidence")
    failure = replace(OPEN, status=ElectionStatus.NO_QUORUM, protocol=protocol, certified_at=END)
    retry = replace(failure, id=4, retry_of_id=failure.id, status=ElectionStatus.TIED)
    service.list_results.return_value = (retry, failure)
    service.get_public_result.return_value = retry
    history, detail = interaction(locale=locale), interaction(locale=locale)

    await election.get_command("results").callback(history)
    await election.get_command("results").callback(detail, retry.id)

    assert retry_label in reply(history, private=False)
    assert crisis_label in reply(history, private=False)
    explanation = reply(detail, private=False)
    assert ("в рамках ролевой игры" if locale == discord.Locale.russian else "for roleplay") in explanation
    assert str(failure.id) in explanation
    assert protocol.reason not in explanation


@pytest.mark.asyncio
async def test_pending_crisis_resolution_is_private_until_independently_confirmed(commands):
    service, _, _, election, commission = commands
    retry = replace(OPEN, id=4, retry_of_id=OPEN.id, status=ElectionStatus.TIED, certified_at=END)
    resolution = CrisisResolution(retry.id, 7, END, "Private proposal @everyone")
    pending = replace(retry, crisis_resolution=resolution)
    confirmed = replace(resolution, confirmed_by_citizen_id=8, confirmed_at=END, confirmation_reason="Private check")
    resolved = replace(retry, crisis_resolution=confirmed)
    service.get_election.return_value = pending
    service.get_public_result.side_effect = [pending, resolved]
    service.list_results.return_value = (resolved,)
    inspect, before, detail, history = [interaction() for _ in range(4)]

    await commission.get_command("inspect").callback(inspect, retry.id)
    await election.get_command("results").callback(before, retry.id)
    await election.get_command("results").callback(detail, retry.id)
    await election.get_command("results").callback(history)

    private = reply(inspect, private=True)
    assert "ожидает подтверждения другим членом комиссии" in private
    assert "Основание предлагаемого решения · Private proposal @\u200beveryone" in private
    assert "@everyone" not in private
    unconfirmed = reply(before, private=False)
    assert "ролевой игры" in unconfirmed and "разрешила" not in unconfirmed
    assert "Private proposal" not in unconfirmed
    outcome = reply(detail, private=False)
    assert "Комиссия разрешила провести новые выборы" in outcome
    assert resolution.proposal_reason not in outcome and confirmed.confirmation_reason not in outcome
    assert "игровой политический кризис завершён" in reply(history, private=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("command, method, confirmed, expected", [
    ("resolve_crisis", "propose_crisis_resolution", False, "awaiting confirmation by another commissioner"),
    ("confirm_crisis", "confirm_crisis_resolution", True, "New elections can be opened"),
])
async def test_crisis_commands_forward_private_resolution_reason(commands, command, method, confirmed, expected):
    service, _, _, _, commission = commands
    request = interaction(locale=discord.Locale.american_english)
    action = getattr(service, method)
    resolution = CrisisResolution(4, 7, END, "Private RP account")
    if confirmed:
        resolution = replace(resolution, confirmed_by_citizen_id=8, confirmed_at=END, confirmation_reason="Check")
    action.return_value = resolution

    await commission.get_command(command).callback(request, resolution.election_id, "My audit reason")

    action.assert_awaited_once_with(GUILD, COMMISSIONER, resolution.election_id, reason="My audit reason")
    content = reply(request, private=True)
    assert expected in content and "My audit reason" not in content and resolution.proposal_reason not in content


@pytest.mark.asyncio
async def test_crisis_confirmation_independence_error_stays_private(commands):
    service, _, _, _, commission = commands
    service.confirm_crisis_resolution.side_effect = GovernmentError(GovernmentErrorReason.INDEPENDENT_CERTIFICATION_REQUIRED)
    request = interaction()

    await commission.get_command("confirm_crisis").callback(request, 4, "My audit reason")

    assert reply(request, private=True) == "Результат должен подтвердить другой член комиссии."


@pytest.mark.asyncio
async def test_crisis_inspection_access_denial_stays_private(commands):
    service, _, _, _, commission = commands
    service.get_election.side_effect = GovernmentError(GovernmentErrorReason.COMMISSIONER_REQUIRED)
    request = interaction()

    await commission.get_command("inspect").callback(request, 4)

    assert reply(request, private=True) == "Для этого действия нужны полномочия избирательной комиссии."


@pytest.mark.asyncio
async def test_commission_forwards_failed_election_id_without_publishing_unconfirmed_crisis(commands):
    service, _, _, _, commission = commands
    retry = replace(OPEN, id=4, retry_of_id=OPEN.id)
    service.open_election.return_value = retry
    service.record_protocol.return_value = replace(retry, status=ElectionStatus.NO_QUORUM, protocol=PROTOCOL)
    opening, recording = interaction(), interaction()

    await commission.get_command("open").callback(opening, "PRESIDENT", str(CANDIDATE), "Repeat", OPEN.id)
    await commission.get_command("record").callback(recording, retry.id, SOURCE, 1, f"{CANDIDATE}=1", "Count")

    service.open_election.assert_awaited_once_with(
        GUILD, COMMISSIONER, office=OfficeKind.PRESIDENT, candidate_user_ids=(CANDIDATE,),
        reason="Repeat", idempotency_key=str(opening.id), retry_of_id=OPEN.id,
    )
    assert f"Повторное голосование по выборам № {OPEN.id}" in reply(opening, private=True)
    content = reply(recording, private=True)
    assert "кворум не достигнут" in content
    assert "кризис" not in content


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
        reason="Open", idempotency_key=str(opening.id), retry_of_id=None,
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
        fetch_member=AsyncMock(side_effect=members.get), channels=[],
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
