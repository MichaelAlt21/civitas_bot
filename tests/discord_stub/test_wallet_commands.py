import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.wallet import WalletService
from civitas.bot.localization import CommandTranslator
from civitas.bot.wallet import add_wallet_commands
from civitas.domain.ledger import HistoryEntry, LedgerError, LedgerErrorReason, TransactionKind

GUILD_ID = 123
SENDER_ID = 456
RECIPIENT_ID = 789


@pytest.fixture
def wallet_commands():
    service = Mock(spec=WalletService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_wallet_commands(tree, guild_scope=guild, wallet_service=service, currency_symbol="CP")
    wallet = tree.get_command("wallet", guild=guild)
    return service, tree, wallet


def interaction(*, locale=discord.Locale.russian, user_id=SENDER_ID, guild_id=GUILD_ID):
    return SimpleNamespace(
        locale=locale,
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            defer=AsyncMock(),
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
            is_done=Mock(return_value=True),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )


async def open_transfer(wallet, command):
    await wallet.get_command("transfer").callback(command, discord.Object(id=RECIPIENT_ID), 42)
    return command.edit_original_response.await_args.kwargs["view"]


@pytest.mark.asyncio
async def test_wallet_group_and_options_are_localized_and_guild_only(wallet_commands):
    _, tree, wallet = wallet_commands
    assert tree.get_command("wallet") is None
    assert tree.get_command("wallet", guild=discord.Object(id=GUILD_ID + 1)) is None
    payload = await wallet.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Ваш кошелёк Civitas"}
    commands = {command["name"]: command for command in payload["options"]}
    assert set(commands) == {"balance", "history", "transfer"}
    assert commands["transfer"]["description_localizations"]["ru"] == "Перевести деньги гражданину"
    amount_option = next(option for option in commands["transfer"]["options"] if option["name"] == "amount")
    assert amount_option["min_value"] == 1
    assert amount_option["description_localizations"]["ru"] == "Сумма в целых единицах"


@pytest.mark.asyncio
async def test_wallet_balance_is_private_and_uses_request_actor(wallet_commands):
    service, _, wallet = wallet_commands
    service.balance.return_value = 250
    command = interaction(locale=discord.Locale.german)

    await wallet.get_command("balance").callback(command)

    command.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    service.balance.assert_awaited_once_with(GUILD_ID, SENDER_ID)
    command.followup.send.assert_awaited_once_with("Your balance: 250 CP.", ephemeral=True)


@pytest.mark.asyncio
async def test_private_history_limits_rows_and_labels_both_directions(wallet_commands):
    service, _, wallet = wallet_commands
    created = datetime(2026, 11, 11, tzinfo=UTC)
    service.history.return_value = tuple(
        HistoryEntry(index, TransactionKind.TRANSFER, -42 if index == 1 else 55, created)
        for index in range(1, 13)
    )
    command = interaction()

    await wallet.get_command("history").callback(command)

    service.history.assert_awaited_once_with(GUILD_ID, SENDER_ID)
    reply = command.followup.send.await_args
    assert reply.kwargs == {"ephemeral": True}
    text = reply.args[0]
    assert text.count("\n•") == 10
    assert "Исходящий перевод: -42 CP" in text
    assert "Входящий перевод: +55 CP" in text


@pytest.mark.asyncio
async def test_preflight_denial_sends_no_confirmation(wallet_commands):
    service, _, wallet = wallet_commands
    service.prepare_transfer.side_effect = LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
    command = interaction()

    await wallet.get_command("transfer").callback(command, discord.Object(id=RECIPIENT_ID), 42)

    command.followup.send.assert_awaited_once_with("Недостаточно средств.", ephemeral=True)
    command.edit_original_response.assert_not_awaited()
    service.transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_transfer_checks_actor_and_ignores_parallel_confirmation(wallet_commands):
    service, _, wallet = wallet_commands
    command = interaction()
    view = await open_transfer(wallet, command)
    command.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    service.prepare_transfer.assert_awaited_once_with(GUILD_ID, SENDER_ID, RECIPIENT_ID, 42)
    service.transfer.assert_not_awaited()
    assert command.edit_original_response.await_args.kwargs["content"] == "Перевести 42 CP получателю <@789>?"
    assert view.confirm.label == "Подтвердить"

    unauthorized = interaction(user_id=RECIPIENT_ID)
    assert not await view.interaction_check(unauthorized)
    unauthorized.response.send_message.assert_awaited_once()
    assert not await view.interaction_check(interaction(guild_id=GUILD_ID + 1))

    confirming = interaction()
    duplicate = interaction()
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def commit(*args, **kwargs):
        entered.set()
        await resume.wait()

    service.transfer.side_effect = commit
    assert await view.interaction_check(confirming)
    task = asyncio.create_task(view.confirm.callback(confirming))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await view.confirm.callback(duplicate)
        duplicate.response.send_message.assert_awaited_once_with(
            "Этот перевод уже обрабатывается.", ephemeral=True
        )
    finally:
        resume.set()
        await task

    service.transfer.assert_awaited_once()
    assert service.transfer.await_args.args == (GUILD_ID, SENDER_ID, RECIPIENT_ID, 42)
    assert service.transfer.await_args.kwargs["idempotency_key"]
    assert confirming.edit_original_response.await_args.kwargs["view"] is None
    assert view.is_finished()


@pytest.mark.asyncio
async def test_uncertain_commit_reuses_same_id_and_cannot_be_cancelled(wallet_commands):
    service, _, wallet = wallet_commands
    view = await open_transfer(wallet, interaction(locale=discord.Locale.american_english))
    another_view = await open_transfer(wallet, interaction())
    service.transfer.side_effect = [ConnectionError(), None, None]
    first_click = interaction()
    with pytest.raises(ConnectionError) as failure:
        await view.confirm.callback(first_click)
    await view.on_error(first_click, failure.value, view.confirm)
    assert "Статус перевода пока неизвестен" in first_click.followup.send.await_args.args[0]
    cancel_click = interaction()
    await view.cancel.callback(cancel_click)
    cancel_click.response.edit_message.assert_not_awaited()

    await view.confirm.callback(interaction())
    await another_view.confirm.callback(interaction())

    first_id, retry_id, separate_id = [
        call.kwargs["idempotency_key"] for call in service.transfer.await_args_list
    ]
    assert first_id == retry_id
    assert separate_id != first_id


@pytest.mark.asyncio
async def test_cancel_and_timeout_remove_buttons_without_posting(wallet_commands):
    service, _, wallet = wallet_commands
    cancel_view = await open_transfer(wallet, interaction())
    cancel = interaction(locale=discord.Locale.british_english)
    await cancel_view.cancel.callback(cancel)
    cancel.response.edit_message.assert_awaited_once_with(content="Transfer cancelled.", view=None)
    assert cancel_view.is_finished()

    timeout_command = interaction(locale=discord.Locale.british_english)
    timeout_view = await open_transfer(wallet, timeout_command)
    await timeout_view.on_timeout()
    assert timeout_view.timeout == 120
    assert timeout_command.edit_original_response.return_value.edit.await_args.kwargs["view"] is None
    service.transfer.assert_not_awaited()
