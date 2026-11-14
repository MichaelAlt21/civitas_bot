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
from civitas.app.maintenance import MAINTENANCE_REASON_MAX_LENGTH, MaintenanceError, MaintenanceErrorReason, MaintenanceService
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


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=OPERATOR_ID),
        locale=locale,
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
