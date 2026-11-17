from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
import pytest
from discord import app_commands

from civitas.app.court import CourtService
from civitas.app.court_cases import (
    ACT_DECISION_REASON_MAX_LENGTH,
    CASE_PAGE_SIZE,
    CASE_STATEMENT_MAX_LENGTH,
    AdministrativeDecision,
    AdministrativeOutcome,
    CaseKind,
    CaseSummary,
    CourtCase,
    CourtCaseError,
    CourtCaseErrorReason,
    CourtCaseService,
    CourtFineStatus,
    MeritsDecision,
    MeritsOutcome,
)
from civitas.app.law import ActKind
from civitas.bot.court import _reconcile_custody_role, add_court_commands
from civitas.bot.localization import CommandTranslator
from civitas.domain.court_sanctions import CourtSentence, SanctionTerm


GUILD_ID, ACTOR_ID, RESPONDENT_ID = (101_000_000_000_000_000 + offset for offset in range(1, 4))
CASE_ID, LAW_REVISION_ID, INTERACTION_ID = 41, 18, 999
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
HEARING = datetime(2026, 11, 17, 12, tzinfo=UTC)
STATEMENT = f"Private facts **shown** @everyone <@{RESPONDENT_ID}>"
REASON = f"Published grounds **with markup** @everyone <@{RESPONDENT_ID}>"


@pytest.fixture
def commands():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD_ID)
    service = Mock(spec=CourtCaseService)
    add_court_commands(tree, guild_scope=scope, service=Mock(spec=CourtService), case_service=service)
    court = tree.get_command("court", guild=scope)
    return service, tree, court, court.get_command("case")


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        id=INTERACTION_ID, guild_id=guild_id, user=SimpleNamespace(id=ACTOR_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def member(*, guild_id=GUILD_ID, bot=False):
    return SimpleNamespace(id=RESPONDENT_ID, guild=SimpleNamespace(id=guild_id), bot=bot)


def record():
    return CourtCase(
        id=CASE_ID, guild_id=1, kind=CaseKind.CRIMINAL, filer_citizen_id=2, filer_user_id=ACTOR_ID,
        respondent_citizen_id=3, respondent_user_id=RESPONDENT_ID, for_state=True, against_state=False,
        law_revision_id=LAW_REVISION_ID, occurred_at=NOW, statement=STATEMENT, filed_at=NOW,
        hearing_at=HEARING, scheduled_by_citizen_id=4,
    )


def summary():
    return CaseSummary(
        id=CASE_ID, kind=CaseKind.CRIMINAL, law_revision_id=LAW_REVISION_ID, filed_at=NOW, hearing_at=HEARING,
    )


def verdict(*, act_kind=ActKind.LAW, outcome=AdministrativeOutcome.GRANTED):
    return AdministrativeDecision(
        guild_id=1, case_id=CASE_ID, revision_id=LAW_REVISION_ID, act_kind=act_kind, outcome=outcome,
        reason=REASON, judge_citizen_id=4, decided_at=HEARING,
    )


def private_pages(request):
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert request.followup.send.await_count
    contents = []
    for reply in request.followup.send.await_args_list:
        assert reply.kwargs["ephemeral"] is True
        assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert reply.kwargs["suppress_embeds"] is True
        content = reply.args[0]
        assert len(content.encode("utf-16-le")) // 2 <= 2000
        contents.append(content)
    return contents


@pytest.mark.asyncio
async def test_custody_role_adds_current_and_removes_expired_and_manual_assignments(commands, monkeypatch):
    service, _, _, _ = commands
    service.custody_role_assignments.return_value = {11: True, 22: False}
    role = SimpleNamespace(members=[SimpleNamespace(id=33)])
    guild = SimpleNamespace(unavailable=False)
    client = Mock(get_guild=Mock(return_value=guild))
    setter = AsyncMock()
    monkeypatch.setattr("civitas.bot.court.safe_assignable_role", Mock(return_value=role))
    monkeypatch.setattr("civitas.bot.court.set_member_role", setter)
    await _reconcile_custody_role(client, service, GUILD_ID, 99)
    assert setter.await_args_list == [
        call(guild, role, user_id, present=present, reason="Civitas in-game custody sync")
        for user_id, present in ((11, True), (22, False), (33, False))
    ]


@pytest.mark.asyncio
async def test_decide_merits_passes_fine_and_shows_recorded_terms(commands):
    service, _, _, cases = commands
    request = interaction()
    article = "7 § **б** @everyone"
    sentence = CourtSentence(article, fine=SanctionTerm(35, 20, 120))
    service.decide_merits.return_value = MeritsDecision(
        1, CASE_ID, CaseKind.CRIMINAL, LAW_REVISION_ID, MeritsOutcome.GUILTY, REASON, 4, HEARING, sentence,
    )
    await cases.get_command("decide_merits").callback(
        request, CASE_ID, app_commands.Choice(name="Guilty", value="GUILTY"), REASON,
        article=article, fine=35, fine_min=20, fine_max=120,
    )
    service.decide_merits.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, CASE_ID, outcome=MeritsOutcome.GUILTY, reason=REASON,
        idempotency_key=str(INTERACTION_ID), sentence=sentence,
    )
    rendered = "\n".join(private_pages(request))
    assert "Штраф 35 ₵" in rendered
    assert "20–120 ₵" in rendered
    assert "@everyone" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{}, {"fine_min": 40, "fine_max": 120}])
async def test_decide_merits_rejects_missing_or_inconsistent_article_limits(commands, limits):
    service, _, _, cases = commands
    request = interaction(locale=discord.Locale.american_english)
    await cases.get_command("decide_merits").callback(
        request, CASE_ID, app_commands.Choice(name="Guilty", value="GUILTY"), REASON, article="7", fine=35, **limits,
    )
    service.decide_merits.assert_not_awaited()
    assert "A sanction requires a guilty verdict" in private_pages(request)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_fine", [False, True])
async def test_decide_merits_records_custody_and_shows_scheduled_end(commands, with_fine):
    service, _, _, cases = commands
    request = interaction(locale=discord.Locale.american_english)
    fine = SanctionTerm(35, 20, 120) if with_fine else None
    fine_options = {"fine": 35, "fine_min": 20, "fine_max": 120} if with_fine else {}
    sentence = CourtSentence("7", fine=fine, custody=SanctionTerm(4, 2, 8))
    service.decide_merits.return_value = MeritsDecision(
        1, CASE_ID, CaseKind.CRIMINAL, LAW_REVISION_ID, MeritsOutcome.GUILTY, REASON, 4, HEARING, sentence,
    )
    await cases.get_command("decide_merits").callback(
        request, CASE_ID, app_commands.Choice(name="Guilty", value="GUILTY"), REASON,
        article="7", custody_hours=4, custody_min=2, custody_max=8, **fine_options,
    )
    service.decide_merits.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, CASE_ID, outcome=MeritsOutcome.GUILTY, reason=REASON,
        idempotency_key=str(INTERACTION_ID), sentence=sentence,
    )
    rendered = "\n".join(private_pages(request))
    assert "Custody 4 h" in rendered
    assert "Article limits 2–8 h" in rendered
    assert discord.utils.format_dt(HEARING + timedelta(hours=4), style="f") in rendered
    assert ("Fine 35 ₵" in rendered) == with_fine


@pytest.mark.asyncio
async def test_decide_merits_rejects_partial_custody_even_with_complete_fine(commands):
    service, _, _, cases = commands
    request = interaction()
    await cases.get_command("decide_merits").callback(
        request, CASE_ID, app_commands.Choice(name="Guilty", value="GUILTY"), REASON,
        article="7", fine=35, fine_min=20, fine_max=120, custody_min=2,
    )
    service.decide_merits.assert_not_awaited()
    assert "оба предела" in private_pages(request)[0]


@pytest.mark.asyncio
async def test_docket_commands_are_guild_scoped_and_localized(commands):
    _, tree, court, _ = commands
    assert not tree.get_commands()
    assert not tree.get_commands(guild=discord.Object(id=GUILD_ID + 1))
    payload = await court.get_translated_payload(tree, CommandTranslator())
    case_group = next(entry for entry in payload["options"] if entry["name"] == "case")
    assert case_group["type"] == discord.AppCommandOptionType.subcommand_group.value
    entries = {entry["name"]: entry for entry in case_group["options"]}
    assert set(entries) == {
        "charge", "challenge", "challenge_decree", "civil", "decide_challenge", "decide_merits", "decision", "list", "returns",
        "fine_status", "schedule", "show", "nominate_judge", "approve_judge", "nominate_prosecutor", "approve_prosecutor", "recuse", "substitute",
    }
    assert entries["civil"]["description"] == "File a civil claim"
    assert entries["civil"]["description_localizations"]["ru"] == "Подать гражданский иск"
    charge = {option["name"]: option for option in entries["charge"]["options"]}
    assert charge["respondent"]["type"] == discord.AppCommandOptionType.user.value
    assert charge["statement"]["max_length"] == CASE_STATEMENT_MAX_LENGTH
    decree = entries["challenge_decree"]
    assert decree["description_localizations"]["ru"] == "Оспорить зарегистрированный указ президента"
    assert decree["options"][0]["name"] == "decree_revision_id"
    seal = {option["name"]: option for option in entries["decide_challenge"]["options"]}
    assert seal["reason"]["max_length"] == ACT_DECISION_REASON_MAX_LENGTH
    assert "Публичное обоснование" in seal["reason"]["description_localizations"]["ru"]
    outcomes = {choice["value"]: choice for choice in seal["outcome"]["choices"]}
    assert outcomes["GRANTED"]["name_localizations"]["ru"] == "Удовлетворить жалобу"
    assert outcomes["DENIED"]["name"] == "Deny challenge"
    merits = {option["name"]: option for option in entries["decide_merits"]["options"]}
    assert merits["fine"]["min_value"] == 10 and merits["fine"]["max_value"] == 100
    assert merits["custody_hours"]["min_value"] == 1 and merits["custody_hours"]["max_value"] == 24
    assert merits["article"]["description_localizations"]["ru"].startswith("Статья опубликованного закона")
    returns = {choice["value"]: choice for choice in entries["returns"]["options"][0]["choices"]}
    assert returns["LAW"]["name"] == "Senate laws"
    assert returns["DECREE"]["name_localizations"]["ru"] == "Указы президента"


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "label"), [
    ("JUDGE", "Кандидат во временные судьи"),
    ("PROSECUTOR", "Кандидат в представители государства"),
])
async def test_substitute_details_are_private_and_do_not_ping(commands, role, label):
    service, _, _, cases = commands
    request = interaction()
    service.substitute_details.return_value = (CASE_ID, RESPONDENT_ID, 1)
    await cases.get_command("substitute").callback(
        request, 12, app_commands.Choice(name=role, value=role),
    )
    service.substitute_details.assert_awaited_once_with(GUILD_ID, ACTOR_ID, 12, role=role)
    result = "\n".join(private_pages(request))
    assert label in result and f"<@{RESPONDENT_ID}>" in result and "1 из 2" in result


@pytest.mark.asyncio
async def test_fine_status_receives_caller_id_and_replies_privately(commands):
    service, _, _, cases = commands
    request = interaction()
    service.fine_status.return_value = CourtFineStatus.REFUND_PENDING
    await cases.get_command("fine_status").callback(request, CASE_ID)
    service.fine_status.assert_awaited_once_with(GUILD_ID, ACTOR_ID, CASE_ID)
    assert private_pages(request) == ["Решение отменено. Ваш штраф ожидает возврата."]


@pytest.mark.asyncio
async def test_show_returns_only_summary_unless_the_service_allows_statement(commands):
    service, _, _, cases = commands
    service.case.side_effect = [(summary(), False), (record(), True)]
    ordinary = interaction()
    authorized = interaction(locale=discord.Locale.american_english)

    await cases.get_command("show").callback(ordinary, CASE_ID)
    await cases.get_command("show").callback(authorized, CASE_ID)

    assert [call.args for call in service.case.await_args_list] == [(GUILD_ID, ACTOR_ID, CASE_ID)] * 2
    ordinary_content = private_pages(ordinary)[0]
    assert f"Дело № {CASE_ID} · Уголовное" in ordinary_content
    assert f"Редакция закона № {LAW_REVISION_ID}" in ordinary_content
    assert f"Заседание {discord.utils.format_dt(HEARING, style='f')}" in ordinary_content
    assert "Private facts" not in ordinary_content
    assert str(ACTOR_ID) not in ordinary_content and str(RESPONDENT_ID) not in ordinary_content
    authorized_content = private_pages(authorized)[0]
    assert f"Case #{CASE_ID} · Criminal" in authorized_content
    assert "Case statement" in authorized_content
    assert r"Private facts \*\*shown\*\*" in authorized_content
    assert "@everyone" not in authorized_content and f"<@{RESPONDENT_ID}>" not in authorized_content


@pytest.mark.asyncio
async def test_private_statement_handles_discord_page_limits(commands):
    service, _, _, cases = commands
    service.case.return_value = (replace(record(), statement="*😎" * (CASE_STATEMENT_MAX_LENGTH // 2)), True)
    request = interaction()

    await cases.get_command("show").callback(request, CASE_ID)

    pages = private_pages(request)
    assert len(pages) > 1
    assert sum(page.count("😎") for page in pages) == CASE_STATEMENT_MAX_LENGTH // 2


@pytest.mark.asyncio
async def test_list_paginates_summaries_without_the_case_statement(commands):
    service, _, _, cases = commands
    service.list_cases.return_value = tuple(replace(summary(), id=CASE_ID + i) for i in range(CASE_PAGE_SIZE))
    request = interaction()

    await cases.get_command("list").callback(request, 2)

    service.list_cases.assert_awaited_once_with(GUILD_ID, page=2)
    content = "\n".join(private_pages(request))
    assert f"Дело № {CASE_ID}" in content
    assert "Следующая страница · 3" in content
    assert "Private facts" not in content
    assert str(ACTOR_ID) not in content and str(RESPONDENT_ID) not in content


@pytest.mark.asyncio
async def test_civil_and_challenge_forward_filing_options_and_use_summary(commands):
    service, _, _, cases = commands
    service.file_civil.return_value = replace(record(), kind=CaseKind.CIVIL, law_revision_id=None, hearing_at=None)
    service.file_challenge.return_value = replace(record(), kind=CaseKind.ADMINISTRATIVE)
    civil, challenge = interaction(), interaction()

    await cases.get_command("civil").callback(civil, STATEMENT, against_state=True)
    await cases.get_command("challenge").callback(challenge, LAW_REVISION_ID, STATEMENT)

    service.file_civil.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, statement=STATEMENT, respondent_user_id=None, for_state=False,
        against_state=True, idempotency_key=str(INTERACTION_ID),
    )
    service.file_challenge.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, law_revision_id=LAW_REVISION_ID, statement=STATEMENT, idempotency_key=str(INTERACTION_ID),
    )
    assert "Гражданское" in private_pages(civil)[0]
    assert "Административное" in private_pages(challenge)[0]
    assert "Private facts" not in civil.followup.send.await_args.args[0]
    assert "Private facts" not in challenge.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_charge_forwards_role_check_to_service_and_rejects_foreign_members(commands):
    service, _, _, cases = commands
    service.file_charge.side_effect = CourtCaseError(CourtCaseErrorReason.PROSECUTOR_REQUIRED)
    charge = cases.get_command("charge").callback
    bad = (member(bot=True), member(guild_id=GUILD_ID + 1))
    for target in bad:
        request = interaction()
        await charge(request, target, LAW_REVISION_ID, NOW.isoformat(), STATEMENT)
        assert "ответчиком" in private_pages(request)[0]
    service.file_charge.assert_not_awaited()

    request = interaction()
    await charge(request, member(), LAW_REVISION_ID, NOW.isoformat(), STATEMENT)

    service.file_charge.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, respondent_user_id=RESPONDENT_ID, law_revision_id=LAW_REVISION_ID,
        occurred_at=NOW.isoformat(), statement=STATEMENT, idempotency_key=str(INTERACTION_ID),
    )
    assert "действующему прокурору" in private_pages(request)[0]


@pytest.mark.asyncio
async def test_decree_challenge_calls_its_service_route_and_keeps_the_statement_private(commands):
    service, _, _, cases = commands
    service.file_decree_challenge.side_effect = [
        replace(record(), kind=CaseKind.ADMINISTRATIVE, act_kind=ActKind.DECREE),
        CourtCaseError(CourtCaseErrorReason.INVALID_DECREE),
    ]
    challenge = cases.get_command("challenge_decree").callback
    allowed = interaction()
    rejected = interaction(locale=discord.Locale.american_english)

    await challenge(allowed, LAW_REVISION_ID, STATEMENT)
    await challenge(rejected, LAW_REVISION_ID + 1, STATEMENT)

    service.file_decree_challenge.assert_any_await(
        GUILD_ID, ACTOR_ID, decree_revision_id=LAW_REVISION_ID, statement=STATEMENT,
        idempotency_key=str(INTERACTION_ID),
    )
    service.file_challenge.assert_not_awaited()
    content = private_pages(allowed)[0]
    assert f"Редакция указа № {LAW_REVISION_ID}" in content
    assert "Private facts" not in content
    assert "Enter a confirmed decree revision number." in private_pages(rejected)[0]


@pytest.mark.asyncio
async def test_schedule_keeps_case_statement_private_and_rejects_foreign_guild(commands):
    service, _, _, cases = commands
    service.schedule_hearing.return_value = record()
    schedule = cases.get_command("schedule").callback
    foreign = interaction(guild_id=GUILD_ID + 1)
    allowed = interaction()

    await schedule(foreign, CASE_ID, HEARING.isoformat())
    await schedule(allowed, CASE_ID, HEARING.isoformat())

    service.schedule_hearing.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, CASE_ID, hearing_at=HEARING.isoformat(), idempotency_key=str(INTERACTION_ID),
    )
    assert "сервер" in private_pages(foreign)[0]
    reply = private_pages(allowed)[0]
    assert f"Заседание {discord.utils.format_dt(HEARING, style='f')}" in reply
    assert "Private facts" not in reply


@pytest.mark.asyncio
async def test_decide_challenge_forwards_idempotency_key_and_escapes_the_public_reason(commands):
    service, _, _, cases = commands
    service.decide_challenge.return_value = verdict()
    decide = cases.get_command("decide_challenge").callback
    request = interaction()
    chosen = app_commands.Choice(name="Grant", value="GRANTED")

    await decide(request, CASE_ID, chosen, REASON)

    service.decide_challenge.assert_awaited_once_with(
        GUILD_ID, ACTOR_ID, CASE_ID, outcome=AdministrativeOutcome.GRANTED, reason=REASON,
        idempotency_key=str(INTERACTION_ID),
    )
    reply = "\n".join(private_pages(request))
    assert "Жалоба удовлетворена" in reply and "вернул акт в Сенат" in reply
    assert "Публичное обоснование" in reply and "Published grounds" in reply
    assert "**with markup**" not in reply and "@everyone" not in reply
    assert f"<@{RESPONDENT_ID}>" not in reply and "Private facts" not in reply
    assert f"Дело № {CASE_ID}" in reply and f"Редакция закона № {LAW_REVISION_ID}" in reply


@pytest.mark.asyncio
async def test_decide_challenge_rejects_invalid_outcome_and_localizes_a_hearing_error(commands):
    service, _, _, cases = commands
    service.decide_challenge.side_effect = CourtCaseError(CourtCaseErrorReason.HEARING_REQUIRED)
    decide = cases.get_command("decide_challenge").callback
    bad_outcome = app_commands.Choice(name="Invalid", value="INVALID")
    chosen = app_commands.Choice(name="Grant", value="GRANTED")
    invalid = interaction(locale=discord.Locale.american_english)
    foreign = interaction(guild_id=GUILD_ID + 1)
    premature = interaction()

    await decide(invalid, CASE_ID, bad_outcome, REASON)
    await decide(foreign, CASE_ID, chosen, REASON)
    service.decide_challenge.assert_not_awaited()
    await decide(premature, CASE_ID, chosen, REASON)

    assert "Choose an outcome that applies" in private_pages(invalid)[0]
    assert "сервер" in private_pages(foreign)[0]
    assert "времени заседания" in private_pages(premature)[0]


@pytest.mark.asyncio
async def test_decision_and_docket_draw_the_outcome_without_revealing_case_materials(commands):
    service, _, _, cases = commands
    service.decision.side_effect = [None, verdict(act_kind=ActKind.DECREE, outcome=AdministrativeOutcome.DENIED)]
    service.case.return_value = (
        replace(summary(), kind=CaseKind.ADMINISTRATIVE, administrative_outcome=AdministrativeOutcome.DENIED),
        False,
    )
    decision = cases.get_command("decision").callback
    undecided = interaction(locale=discord.Locale.american_english)
    decided = interaction(locale=discord.Locale.american_english)
    docket = interaction()

    await decision(undecided, CASE_ID)
    await decision(decided, CASE_ID)
    await cases.get_command("show").callback(docket, CASE_ID)

    assert "No decision" in private_pages(undecided)[0]
    reply = "\n".join(private_pages(decided))
    assert "Challenge denied" in reply and f"Decree revision #{LAW_REVISION_ID}" in reply
    assert "returned" not in reply and "Private facts" not in reply
    assert "В удовлетворении жалобы отказано" in private_pages(docket)[0]
    assert "Published grounds" not in docket.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_returns_filters_by_act_and_lists_revision_numbers_without_public_reasons(commands):
    service, _, _, cases = commands
    service.list_returns.side_effect = [(verdict(act_kind=ActKind.DECREE),), ()]
    returns = cases.get_command("returns").callback
    decree = app_commands.Choice(name="Decree", value="DECREE")
    law = app_commands.Choice(name="Law", value="LAW")
    president = interaction()
    senate = interaction(locale=discord.Locale.american_english)

    await returns(president, decree)
    await returns(senate, law, 2)

    service.list_returns.assert_any_await(GUILD_ID, ActKind.DECREE, page=1)
    service.list_returns.assert_any_await(GUILD_ID, ActKind.LAW, page=2)
    reply = "\n".join(private_pages(president))
    assert "Акты, возвращённые президенту" in reply and f"Редакция указа № {LAW_REVISION_ID}" in reply
    assert "Published grounds" not in reply and "Private facts" not in reply
    assert "No returned acts on page 2" in private_pages(senate)[0]
