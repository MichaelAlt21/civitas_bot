from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.admin_audit import (
    ADMIN_AUDIT_MAX_PAGE,
    ADMIN_AUDIT_PAGE_SIZE,
    AdminAuditError,
    AdminAuditErrorReason,
    AdminAuditService,
)
from civitas.app.admission import (
    ADMISSION_MAX_PAGE,
    ADMISSION_PAGE_SIZE,
    ADMISSION_REASON_MAX_LENGTH,
    AdmissionError,
    AdmissionErrorReason,
    AdmissionService,
)
from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.maintenance import MAINTENANCE_REASON_MAX_LENGTH, MaintenanceError, MaintenanceErrorReason, MaintenanceService
from civitas.app.presentation import GuildPresentation, PresentationBinding, PresentationService
from civitas.bot.admin import add_admin_commands
from civitas.bot.localization import CommandTranslator

GUILD_ID = 201
OPERATOR_ID = 202
NOW = datetime(2026, 11, 13, tzinfo=UTC)


def command_tree():
    return app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))


@pytest.fixture
def commands():
    maintenance = Mock(spec=MaintenanceService)
    audit = Mock(spec=AdminAuditService)
    tree = command_tree()
    scope = discord.Object(id=GUILD_ID)
    add_admin_commands(tree, guild_scope=scope, maintenance_service=maintenance, audit_service=audit)
    admin = tree.get_command("admin", guild=scope)
    return maintenance, audit, tree, admin


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID, administrator=False):
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=OPERATOR_ID),
        locale=locale,
        permissions=discord.Permissions(administrator=administrator),
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
    return "\n".join(replies)


def audit_entry(*, subject_id=32, action="contract.pay", reason_code="approver_required"):
    return SimpleNamespace(
        occurred_at=NOW, action=action, actor_discord_user_id=OPERATOR_ID, subject_kind="contract",
        subject_id=subject_id, result="denied", reason_code=reason_code,
    )


@pytest.mark.asyncio
async def test_admin_commands_are_registered_only_with_services_and_localized(commands):
    _, _, tree, admin = commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    payload = await admin.get_translated_payload(tree, CommandTranslator())
    options = {command["name"]: command for command in payload["options"]}
    assert set(options) == {"maintenance", "audit"}
    page = options["audit"]["options"][0]
    assert page["min_value"] == 1
    assert page["max_value"] == ADMIN_AUDIT_MAX_PAGE
    assert options["maintenance"]["description_localizations"] == {"ru": "Посмотреть или изменить режим паузы экономики"}
    reason = next(option for option in options["maintenance"]["options"] if option["name"] == "reason")
    assert reason["max_length"] == MAINTENANCE_REASON_MAX_LENGTH

    disabled_tree = command_tree()
    scope = discord.Object(id=GUILD_ID)
    add_admin_commands(disabled_tree, guild_scope=scope)
    assert disabled_tree.get_commands(guild=scope) == []


@pytest.mark.asyncio
async def test_maintenance_status_queries_service_and_replies_privately(commands):
    maintenance, _, _, admin = commands
    maintenance.status.return_value = True
    command = interaction()

    await admin.get_command("maintenance").callback(command)

    maintenance.status.assert_awaited_once_with(GUILD_ID, OPERATOR_ID)
    maintenance.set_read_only.assert_not_awaited()
    assert "Пауза экономики включена" in private_replies(command)


@pytest.mark.asyncio
async def test_maintenance_mutation_forwards_reason_and_confirms_result(commands):
    maintenance, _, _, admin = commands
    maintenance.set_read_only.side_effect = [
        SimpleNamespace(enabled=True, changed=True), SimpleNamespace(enabled=False, changed=False),
    ]
    pause = interaction()
    resume = interaction(locale=discord.Locale.american_english)
    callback = admin.get_command("maintenance").callback

    await callback(pause, True, "Check ledger")
    await callback(resume, False, "Checked")

    first, second = maintenance.set_read_only.await_args_list
    assert first.args == second.args == (GUILD_ID, OPERATOR_ID)
    assert first.kwargs == {"enabled": True, "reason": "Check ledger"}
    assert second.kwargs == {"enabled": False, "reason": "Checked"}
    assert "Режим экономики изменён" in private_replies(pause)
    assert "already in this mode" in private_replies(resume)
    assert "read-only mode is off" in private_replies(resume)


@pytest.mark.asyncio
async def test_maintenance_uses_service_denial_and_missing_reason(commands):
    maintenance, _, _, admin = commands
    maintenance.set_read_only.side_effect = [
        MaintenanceError(MaintenanceErrorReason.OPERATOR_REQUIRED),
        MaintenanceError(MaintenanceErrorReason.INVALID_REASON),
    ]
    unauthorized, missing_reason = interaction(guild_id=None), interaction()
    callback = admin.get_command("maintenance").callback

    await callback(unauthorized, True, "Check ledger")
    await callback(missing_reason, True)

    assert maintenance.set_read_only.await_args.kwargs == {"enabled": True, "reason": ""}
    assert "нужны полномочия" in private_replies(unauthorized)
    assert "укажите основание" in private_replies(missing_reason)


@pytest.mark.asyncio
async def test_audit_forwards_page_and_only_renders_private_safe_fields(commands):
    _, audit, _, admin = commands
    unsafe_code = "<@202> **bad** @everyone"
    rows = [audit_entry(subject_id=subject_id) for subject_id in range(ADMIN_AUDIT_PAGE_SIZE - 1)]
    rows.append(audit_entry(action=unsafe_code, reason_code=unsafe_code))
    audit.list_recent.return_value = tuple(rows)
    command = interaction()

    await admin.get_command("audit").callback(command, 2)

    audit.list_recent.assert_awaited_once_with(GUILD_ID, OPERATOR_ID, page=2)
    reply = private_replies(command)
    assert "Недавние события аудита · Страница 2" in reply
    assert "Оплата госконтракта" in reply
    assert "Инициатор 202 · Контракт #32 · отказано" in reply
    assert r"Код основания: approver\_required" in reply
    assert "`/admin audit page:3`" in reply
    assert "<@202>" not in reply
    assert "@everyone" not in reply
    assert "**bad**" not in reply


@pytest.mark.asyncio
async def test_audit_empty_page_and_last_page_navigation(commands):
    _, audit, _, admin = commands
    audit.list_recent.side_effect = [(), (audit_entry(),) * ADMIN_AUDIT_PAGE_SIZE]
    empty, last = interaction(), interaction()

    await admin.get_command("audit").callback(empty, 2)
    await admin.get_command("audit").callback(last, ADMIN_AUDIT_MAX_PAGE)

    assert "На странице 2 нет событий аудита" in private_replies(empty)
    assert "Следующая страница" not in private_replies(last)


@pytest.mark.asyncio
async def test_audit_access_denial_stays_private(commands):
    _, audit, _, admin = commands
    audit.list_recent.side_effect = AdminAuditError(AdminAuditErrorReason.OPERATOR_REQUIRED)
    command = interaction(locale=discord.Locale.german)

    await admin.get_command("audit").callback(command)

    audit.list_recent.assert_awaited_once_with(GUILD_ID, OPERATOR_ID, page=1)
    assert private_replies(command) == "This command requires technical operator access."


@pytest.fixture
def admission_commands():
    service = Mock(spec=AdmissionService)
    tree = command_tree()
    scope = discord.Object(id=GUILD_ID)
    add_admin_commands(tree, guild_scope=scope, admission_service=service)
    admin = tree.get_command("admin", guild=scope)
    return service, tree, admin, admin.get_command("admission")


def admission_member(*, user_id=801, bot=False, guild_id=GUILD_ID):
    return SimpleNamespace(id=user_id, bot=bot, guild=SimpleNamespace(id=guild_id))


@pytest.mark.asyncio
async def test_admission_is_registered_for_one_guild_with_localized_member_picker(admission_commands):
    _, tree, admin, _ = admission_commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    payload = await admin.get_translated_payload(tree, CommandTranslator())
    admission, = payload["options"]
    assert admission["name"] == "admission"
    assert admission["description_localizations"] == {"ru": "Управлять допуском к регистрации граждан"}
    subcommands = {command["name"]: command for command in admission["options"]}
    assert set(subcommands) == {"add", "remove", "list"}
    member, = subcommands["add"]["options"]
    assert member["type"] == discord.AppCommandOptionType.user.value
    assert member["required"] is True
    assert member["description_localizations"] == {"ru": "Участник сервера для допуска"}
    reason = next(option for option in subcommands["remove"]["options"] if option["name"] == "reason")
    assert reason["required"] is True
    assert reason["max_length"] == ADMISSION_REASON_MAX_LENGTH
    assert subcommands["list"]["options"][0]["max_value"] == ADMISSION_MAX_PAGE


@pytest.mark.asyncio
async def test_add_passes_discord_administrator_flag_and_selected_user_to_service(admission_commands):
    service, _, _, admission = admission_commands
    service.set_allowed.side_effect = [True, False]
    first = interaction(administrator=True)
    repeat = interaction(administrator=True, locale=discord.Locale.american_english)
    member = admission_member()

    await admission.get_command("add").callback(first, member)
    await admission.get_command("add").callback(repeat, member)

    for call in service.set_allowed.await_args_list:
        assert call.args == (GUILD_ID, OPERATOR_ID)
        assert call.kwargs == {"administrator": True, "target_user_id": member.id, "allowed": True, "reason": ""}
    assert "получил допуск" in private_replies(first)
    assert "already has admission" in private_replies(repeat)


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [admission_member(bot=True), admission_member(guild_id=GUILD_ID + 1)])
async def test_add_does_not_submit_bot_or_foreign_member_to_service(admission_commands, member):
    service, _, _, admission = admission_commands
    command = interaction(administrator=True)

    await admission.get_command("add").callback(command, member)

    service.set_allowed.assert_not_awaited()
    assert "Выберите игрока этого сервера" in private_replies(command)


@pytest.mark.asyncio
async def test_remove_accepts_selected_member_and_departed_members_id(admission_commands):
    service, _, _, admission = admission_commands
    service.set_allowed.side_effect = [True, False]
    first, repeat = interaction(administrator=True), interaction(administrator=True)
    member = admission_member()
    callback = admission.get_command("remove").callback

    await callback(first, "Left the game", member=member)
    await callback(repeat, "Left the game", user_id="18282892030423421001")

    first_call, second_call = service.set_allowed.await_args_list
    assert first_call.args == second_call.args == (GUILD_ID, OPERATOR_ID)
    assert first_call.kwargs == {
        "administrator": True, "target_user_id": member.id, "allowed": False, "reason": "Left the game",
    }
    assert second_call.kwargs == {
        "administrator": True, "target_user_id": 18282892030423421001, "allowed": False, "reason": "Left the game",
    }
    assert "отозван" in private_replies(first)
    assert "уже нет допуска" in private_replies(repeat)


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{}, {"user_id": "not-a-number"}, {"member": admission_member(), "user_id": "801"}])
async def test_remove_with_ambiguous_or_invalid_target_stays_private(admission_commands, fields):
    service, _, _, admission = admission_commands
    command = interaction(administrator=True)

    await admission.get_command("remove").callback(command, "Left the game", **fields)

    service.set_allowed.assert_not_awaited()
    assert "Выберите одного участника или введите один числовой ID" in private_replies(command)


@pytest.mark.asyncio
async def test_list_displays_members_privately_and_pages_through_admission(admission_commands):
    service, _, _, admission = admission_commands
    service.list_allowed.side_effect = [tuple(range(801, 801 + ADMISSION_PAGE_SIZE)), ()]
    first, empty = interaction(administrator=True), interaction(administrator=True)
    callback = admission.get_command("list").callback

    await callback(first, 2)
    await callback(empty, 3)

    assert service.list_allowed.await_args.kwargs == {"administrator": True, "page": 3}
    first_reply = private_replies(first)
    assert "Список допуска · Страница 2" in first_reply
    assert "<@801> · `801`" in first_reply
    assert "`/admin admission list page:3`" in first_reply
    assert "На странице 3 нет записей допуска" in private_replies(empty)


@pytest.mark.asyncio
async def test_admission_permissions_and_guild_errors_are_private(admission_commands):
    service, _, _, admission = admission_commands
    service.set_allowed.side_effect = AdmissionError(AdmissionErrorReason.ADMINISTRATOR_REQUIRED)
    service.list_allowed.side_effect = CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)
    non_admin = interaction()
    wrong_guild = interaction(guild_id=GUILD_ID + 1, administrator=True)

    await admission.get_command("add").callback(non_admin, admission_member())
    await admission.get_command("list").callback(wrong_guild)

    assert service.set_allowed.await_args.kwargs["administrator"] is False
    service.list_allowed.assert_awaited_once_with(GUILD_ID + 1, OPERATOR_ID, administrator=True, page=1)
    assert "нужно право администратора Discord" in private_replies(non_admin)
    assert "сервер" in private_replies(wrong_guild)


@pytest.fixture
def setup_commands():
    service = Mock(spec=PresentationService)
    tree = command_tree()
    scope = discord.Object(id=GUILD_ID)
    add_admin_commands(tree, guild_scope=scope, presentation_service=service)
    admin = tree.get_command("admin", guild=scope)
    return service, tree, admin, admin.get_command("setup")


@pytest.mark.asyncio
async def test_setup_uses_localized_role_and_text_channel_pickers(setup_commands):
    _, tree, admin, _ = setup_commands
    payload = await admin.get_translated_payload(tree, CommandTranslator())
    setup, = payload["options"]
    assert setup["description_localizations"] == {"ru": "Настроить роли сервера и отчёты казны"}
    subcommands = {command["name"]: command for command in setup["options"]}
    assert set(subcommands) == {"status", "role", "report_channel"}
    kind, role = subcommands["role"]["options"]
    assert [option["value"] for option in kind["choices"]] == ["citizen_role_id", "president_role_id", "parliament_role_id"]
    assert kind["choices"][-1]["name"] == "Senator"
    assert role["type"] == discord.AppCommandOptionType.role.value
    assert role["required"] is False
    channel, = subcommands["report_channel"]["options"]
    assert channel["type"] == discord.AppCommandOptionType.channel.value
    assert channel["channel_types"] == [discord.ChannelType.text.value, discord.ChannelType.news.value]
    assert channel["required"] is False


@pytest.mark.asyncio
async def test_setup_status_renders_private_ids_and_denial(setup_commands):
    service, _, _, setup = setup_commands
    service.status.side_effect = [
        GuildPresentation(citizen_role_id=901, parliament_role_id=902, report_channel_id=903),
        AdmissionError(AdmissionErrorReason.ADMINISTRATOR_REQUIRED),
    ]
    admin = interaction(administrator=True)
    ordinary_member = interaction(locale=discord.Locale.american_english)

    await setup.get_command("status").callback(admin)
    await setup.get_command("status").callback(ordinary_member)

    assert [call.kwargs for call in service.status.await_args_list] == [{"administrator": True}, {"administrator": False}]
    reply = private_replies(admin)
    assert "Роль гражданина · <@&901>" in reply
    assert "Роль президента · отключено" in reply
    assert "Роль сенатора · <@&902>" in reply
    assert "Канал отчётов казны · <#903>" in reply
    assert "requires the Discord Administrator permission" in private_replies(ordinary_member)


@pytest.mark.asyncio
async def test_setup_saves_safe_role_checks_hierarchy_and_allows_clearing(setup_commands):
    service, _, _, setup = setup_commands
    service.set_binding.side_effect = [True, False]
    role = SimpleNamespace(id=901, permissions=discord.Permissions.none(), is_assignable=lambda: True)
    guild = SimpleNamespace(
        id=GUILD_ID, me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True)),
        channels=[], get_role=lambda role_id: role,
    )
    role.guild = guild
    selected, clearing, unsafe = [interaction(administrator=True) for _ in range(3)]
    selected.guild = clearing.guild = unsafe.guild = guild
    kind = app_commands.Choice(name="Citizen", value=PresentationBinding.CITIZEN_ROLE.value)
    callback = setup.get_command("role").callback

    await callback(selected, kind, role)
    await callback(clearing, kind)
    role.is_assignable = lambda: False
    await callback(unsafe, kind, role)

    assert [call.kwargs for call in service.set_binding.await_args_list] == [
        {"administrator": True, "binding": PresentationBinding.CITIZEN_ROLE, "value": 901},
        {"administrator": True, "binding": PresentationBinding.CITIZEN_ROLE, "value": None},
    ]
    assert "прежняя роль" in private_replies(selected)
    assert "прежняя роль" not in private_replies(clearing)
    assert "Manage Roles" in private_replies(unsafe)


@pytest.mark.asyncio
async def test_setup_report_channel_checks_permissions_and_can_clear_channel(setup_commands):
    service, _, _, setup = setup_commands
    service.set_binding.return_value = True
    channel = SimpleNamespace(id=903, guild=SimpleNamespace(id=GUILD_ID), permissions_for=Mock())
    allowed = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    missing_embed = discord.Permissions(view_channel=True, send_messages=True)
    channel.permissions_for.side_effect = [allowed, missing_embed]
    selected, rejected, clearing = [interaction(administrator=True) for _ in range(3)]
    selected.guild = rejected.guild = clearing.guild = SimpleNamespace(me=object())
    callback = setup.get_command("report_channel").callback

    await callback(selected, channel)
    await callback(rejected, channel)
    await callback(clearing)

    assert [call.kwargs for call in service.set_binding.await_args_list] == [
        {"administrator": True, "binding": PresentationBinding.REPORT_CHANNEL, "value": 903},
        {"administrator": True, "binding": PresentationBinding.REPORT_CHANNEL, "value": None},
    ]
    channel.permissions_for.assert_called_with(selected.guild.me)
    assert "<#903>" in private_replies(selected)
    assert "Embed Links" in private_replies(rejected)
    assert "отключено" in private_replies(clearing)
