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
    ElectionCancellation,
    ElectionProtocol,
    ElectionStatus,
    Impeachment,
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
IMPEACHMENT = Impeachment(
    id=10, guild_id=1, term_id=9, election_id=OPEN.id, president_citizen_id=2,
    president_discord_user_id=CANDIDATE, eligible_citizen_ids=(201, 202, 203), quorum=2,
    opened_by_citizen_id=1, opened_at=START, request_key="impeachment",
)
IMPEACHMENT_RECORDED = replace(
    IMPEACHMENT, source_url=SOURCE, participants=3, votes_for=2,
    recorded_by_citizen_id=201, recorded_at=END, public_reason="**Review** <#123> @everyone",
)
IMPEACHMENT_CONFIRMED = replace(
    IMPEACHMENT_RECORDED, confirmed_at=END + timedelta(minutes=1), confirmed_by_citizen_id=202,
    confirmation_reason="Private audit evidence",
)


@pytest.fixture
def commands():
    service = Mock(spec=GovernmentService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD)
    add_government_commands(tree, guild_scope=scope, government_service=service)
    government = tree.get_command("government", guild=scope)
    election = tree.get_command("election", guild=scope)
    return service, tree, government, election, election.get_command("commission")


def interaction(*, guild_id=GUILD, locale=discord.Locale.russian, actor=COMMISSIONER):
    return SimpleNamespace(
        id=1001, guild_id=guild_id, user=SimpleNamespace(id=actor), locale=locale,
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
    assert {command.name for command in government.commands} == {"offices", "impeachment"}
    payload = await election.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Выборы и подтверждённые итоги"}
    commission = next(option for option in payload["options"] if option["name"] == "commission")
    by_name = {item["name"]: item for item in commission["options"]}
    assert by_name.keys() == {
        "active", "voters", "open", "record", "inspect", "certify", "resolve_crisis", "confirm_crisis", "cancel", "confirm_cancel",
        "open_impeachment", "impeachment_voters", "record_impeachment", "confirm_impeachment",
    }
    for command in ("resolve_crisis", "confirm_crisis", "cancel", "confirm_cancel"):
        assert {item["name"] for item in by_name[command]["options"] if item["required"]} == {"election_id", "reason"}
        assert "ru" in by_name[command]["description_localizations"]
    cancel_reason = next(item for item in by_name["cancel"]["options"] if item["name"] == "reason")
    assert cancel_reason["description"] == "Reason shown to the public after confirmation"
    opening = next(option for option in commission["options"] if option["name"] == "open")
    retry = next(option for option in opening["options"] if option["name"] == "retry_of_id")
    assert retry["description_localizations"] == {"ru": "Номер несостоявшихся выборов для повторного голосования"}
    assert not retry["required"]
    replacement = next(option for option in opening["options"] if option["name"] == "replacement_of_id")
    assert not replacement["required"]
    assert by_name["voters"]["description_localizations"] == {"ru": "Приватно посмотреть зафиксированный список избирателей"}
    voter_options = {option["name"]: option for option in by_name["voters"]["options"]}
    assert voter_options["election_id"]["required"] and not voter_options["page"]["required"]
    assert voter_options["page"]["min_value"] == 1
    for command in ("open_impeachment", "impeachment_voters", "record_impeachment", "confirm_impeachment"):
        assert "ru" in by_name[command]["description_localizations"]
    record_options = {option["name"]: option for option in by_name["record_impeachment"]["options"]}
    assert record_options.keys() == {"impeachment_id", "source_url", "participants", "votes_for", "reason"}
    assert all(option["required"] for option in record_options.values())
    assert record_options["votes_for"]["min_value"] == 0


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
async def test_voter_snapshot_paginates_copyable_ids_privately(commands):
    service, _, _, _, commission = commands
    voters = tuple(range(200000000000000001, 200000000000000026))
    service.list_voter_snapshot.return_value = voters
    first = interaction()
    second = interaction(locale=discord.Locale.american_english)
    outside = interaction()
    voters_command = commission.get_command("voters")

    await voters_command.callback(first, OPEN.id)
    await voters_command.callback(second, OPEN.id, 2)
    await voters_command.callback(outside, OPEN.id, 3)

    assert service.list_voter_snapshot.await_count == 3
    service.list_voter_snapshot.assert_awaited_with(GUILD, COMMISSIONER, OPEN.id)
    first_page, second_page = reply(first, private=True), reply(second, private=True)
    assert "Допущено · 25 | Страница 1 из 2" in first_page
    assert "Eligible voters · 25 | Page 2 of 2" in second_page
    assert first_page.split("```\n", 1)[1].splitlines() == [*(str(user_id) for user_id in voters[:20]), "```"]
    assert second_page.split("```\n", 1)[1].splitlines() == [*(str(user_id) for user_id in voters[20:]), "```"]
    assert reply(outside, private=True) == "На странице 3 нет ID избирателей."


@pytest.mark.asyncio
async def test_voter_snapshot_displays_commission_denial_without_exposing_ids(commands):
    service, _, _, _, commission = commands
    service.list_voter_snapshot.side_effect = GovernmentError(GovernmentErrorReason.COMMISSIONER_REQUIRED)
    command = interaction()

    await commission.get_command("voters").callback(command, OPEN.id)

    assert reply(command, private=True) == "Для этого действия нужны полномочия избирательной комиссии."


@pytest.mark.asyncio
async def test_commission_impeachment_records_each_action_privately(commands):
    service, _, _, _, commission = commands
    service.open_impeachment.return_value = IMPEACHMENT
    service.record_impeachment.return_value = IMPEACHMENT_RECORDED
    service.confirm_impeachment.return_value = IMPEACHMENT_CONFIRMED
    opening, tally = interaction(), interaction()
    confirmation = interaction(actor=OTHER_CANDIDATE, locale=discord.Locale.american_english)

    await commission.get_command("open_impeachment").callback(opening)
    await commission.get_command("record_impeachment").callback(
        tally, IMPEACHMENT.id, SOURCE, 3, 2, IMPEACHMENT_RECORDED.public_reason,
    )
    await commission.get_command("confirm_impeachment").callback(
        confirmation, IMPEACHMENT.id, IMPEACHMENT_CONFIRMED.confirmation_reason,
    )

    service.open_impeachment.assert_awaited_once_with(GUILD, COMMISSIONER, idempotency_key=str(opening.id))
    service.record_impeachment.assert_awaited_once_with(
        GUILD, COMMISSIONER, IMPEACHMENT.id, source_url=SOURCE, participants=3, votes_for=2,
        reason=IMPEACHMENT_RECORDED.public_reason,
    )
    service.confirm_impeachment.assert_awaited_once_with(
        GUILD, OTHER_CANDIDATE, IMPEACHMENT.id, reason=IMPEACHMENT_CONFIRMED.confirmation_reason,
    )
    assert "Ожидается результат публичного голосования." in reply(opening, private=True)
    recorded = reply(tally, private=True)
    assert "Участвовало · 3 | За отстранение · 2" in recorded
    assert "Ожидается независимое подтверждение." in recorded
    assert "The presidential term ended at" in reply(confirmation, private=True)


@pytest.mark.asyncio
async def test_impeachment_voter_snapshot_uses_private_numbered_pages(commands):
    service, _, _, _, commission = commands
    voters = tuple(range(200000000000000001, 200000000000000022))
    service.list_impeachment_voters.return_value = voters
    command = interaction()

    await commission.get_command("impeachment_voters").callback(command, IMPEACHMENT.id, 2)

    service.list_impeachment_voters.assert_awaited_once_with(GUILD, COMMISSIONER, IMPEACHMENT.id)
    content = reply(command, private=True)
    assert "Импичмент № 10" in content and "Допущено · 21 | Страница 2 из 2" in content
    assert str(voters[-1]) in content and str(voters[0]) not in content


@pytest.mark.asyncio
async def test_public_impeachment_shares_aggregate_evidence_without_audit_notes_or_mentions(commands):
    service, _, government, _, _ = commands
    service.get_public_impeachment.return_value = replace(
        IMPEACHMENT_CONFIRMED, eligible_citizen_ids=(), confirmation_reason=None,
    )
    command = interaction(locale=discord.Locale.american_english)
    outside = interaction(guild_id=GUILD + 1)
    callback = government.get_command("impeachment").callback

    await callback(command, IMPEACHMENT.id)
    await callback(outside, IMPEACHMENT.id)

    service.get_public_impeachment.assert_awaited_once_with(GUILD, COMMISSIONER, IMPEACHMENT.id)
    service.list_impeachment_voters.assert_not_awaited()
    content = reply(command, private=False)
    assert f"<{SOURCE}>" in content and "Votes for removal · 2" in content
    assert "Result certified." in content
    assert "Eligible voters · 0" not in content
    assert "‹#123›" in content and "@everyone" not in content
    assert IMPEACHMENT_CONFIRMED.confirmation_reason not in content
    outside.response.send_message.assert_awaited_once_with(
        "Команды Civitas доступны на игровом сервере.", ephemeral=True,
    )


@pytest.mark.asyncio
async def test_impeachment_denials_and_failed_threshold_have_localized_status(commands):
    service, _, government, _, commission = commands
    service.get_public_impeachment.return_value = replace(IMPEACHMENT_RECORDED, votes_for=1)
    service.confirm_impeachment.side_effect = GovernmentError(
        GovernmentErrorReason.IMPEACHMENT_INSUFFICIENT_SUPPORT,
    )
    service.record_impeachment.side_effect = GovernmentError(
        GovernmentErrorReason.INVALID_IMPEACHMENT_TALLY,
    )
    public, private, bad_tally = interaction(), interaction(), interaction(locale=discord.Locale.american_english)

    await government.get_command("impeachment").callback(public, IMPEACHMENT.id)
    await commission.get_command("confirm_impeachment").callback(private, IMPEACHMENT.id, "Verify result")
    await commission.get_command("record_impeachment").callback(bad_tally, IMPEACHMENT.id, SOURCE, 1, 2, "Tally")

    assert "кворум или порог голосов за отстранение не достигнут" in reply(public, private=False)
    assert "Для отстранения нужны кворум и не менее двух третей" in reply(private, private=True)
    assert "Votes for removal must be within the participant count" in reply(bad_tally, private=True)


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
@pytest.mark.parametrize("command, method, confirmed, expected", [
    ("cancel", "propose_election_cancellation", False, "awaiting confirmation by another commissioner"),
    ("confirm_cancel", "confirm_election_cancellation", True, "was cancelled"),
])
async def test_cancellation_commands_forward_reasons_and_reply_privately(commands, command, method, confirmed, expected):
    service, _, _, _, commission = commands
    action = getattr(service, method)
    cancellation = ElectionCancellation(
        election_id=OPEN.id, proposed_by_citizen_id=7, proposed_at=END, public_reason="Missing poll",
    )
    if confirmed:
        cancellation = replace(cancellation, confirmed_by_citizen_id=8, confirmed_at=END, confirmation_reason="Check")
    action.return_value = cancellation
    request = interaction(locale=discord.Locale.american_english)

    await commission.get_command(command).callback(request, OPEN.id, "Audit reason")

    action.assert_awaited_once_with(GUILD, COMMISSIONER, OPEN.id, reason="Audit reason")
    content = reply(request, private=True)
    assert expected in content and "Audit reason" not in content


@pytest.mark.asyncio
async def test_cancelled_public_result_shows_safe_reason_and_hides_unverifiable_protocol(commands):
    service, _, _, election, commission = commands
    cancellation = ElectionCancellation(
        election_id=OPEN.id, proposed_by_citizen_id=7, proposed_at=END,
        public_reason="**Poll inaccessible** @everyone <#1300>",
    )
    pending = replace(OPEN, status=ElectionStatus.PENDING_CERTIFICATION, protocol=PROTOCOL, cancellation=cancellation)
    confirmed = replace(cancellation, confirmed_by_citizen_id=8, confirmed_at=END, confirmation_reason="Private check")
    cancelled = replace(pending, cancellation=confirmed)
    service.get_election.return_value = pending
    service.get_public_result.return_value = cancelled
    service.list_results.return_value = (cancelled,)
    inspection, detail, history = [interaction() for _ in range(3)]

    await commission.get_command("inspect").callback(inspection, OPEN.id)
    await election.get_command("results").callback(detail, OPEN.id)
    await election.get_command("results").callback(history)

    private = reply(inspection, private=True)
    assert "ожидает подтверждения другим членом комиссии" in private
    assert "Poll inaccessible" in private
    public = reply(detail, private=False)
    assert "Отмена подтверждена" in public
    assert r"\*\*Poll inaccessible\*\*" in public
    assert "@everyone" not in public and "<#1300>" not in public
    assert SOURCE not in public and "Число голосов по кандидатам" not in public
    assert confirmed.confirmation_reason not in public
    assert "отменены" in reply(history, private=False)


@pytest.mark.asyncio
async def test_commission_opens_replacement_with_link_to_cancelled_election(commands):
    service, _, _, _, commission = commands
    replacement = replace(OPEN, id=4, replacement_of_id=OPEN.id)
    service.open_election.return_value = replacement
    request = interaction()

    await commission.get_command("open").callback(request, "PRESIDENT", str(CANDIDATE), "Replacement", None, OPEN.id)

    service.open_election.assert_awaited_once_with(
        GUILD, COMMISSIONER, office=OfficeKind.PRESIDENT, candidate_user_ids=(CANDIDATE,),
        reason="Replacement", idempotency_key=str(request.id), retry_of_id=None, replacement_of_id=OPEN.id,
    )
    assert f"Взамен отменённых выборов № {OPEN.id}" in reply(request, private=True)


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
        reason="Repeat", idempotency_key=str(opening.id), retry_of_id=OPEN.id, replacement_of_id=None,
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
        reason="Open", idempotency_key=str(opening.id), retry_of_id=None, replacement_of_id=None,
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
