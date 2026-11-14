import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import discord
import pytest
from discord import app_commands

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError
from civitas.app.receipts import ReceiptService, ReceiptUndeliverable
from civitas.bot.localization import CommandTranslator
from civitas.bot.receipts import _send_receipt, add_receipt_commands, run_receipt_publisher
from civitas.domain.ledger import TransactionKind
from civitas.domain.receipts import ReceiptDelivery, ReceiptDetails

GUILD_ID = 5000
USER_ID = 5001
NOW = datetime(2026, 11, 11, tzinfo=UTC)
DELIVERY = ReceiptDelivery(1, USER_ID, "ru", -123456, TransactionKind.TRANSFER, 2, NOW, uuid4(), 1)


@pytest.fixture
def commands():
    service = Mock(spec=ReceiptService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    scope = discord.Object(id=GUILD_ID)
    wallet = app_commands.Group(name="wallet", description="Wallet")
    tree.add_command(wallet, guild=scope)
    add_receipt_commands(tree, guild_scope=scope, service=service, currency_symbol="CP")
    return service, tree, wallet


def interaction(*, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        guild_id=guild_id, user=SimpleNamespace(id=USER_ID), locale=locale,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def private_reply(interaction):
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response = interaction.followup.send.await_args
    assert response.kwargs["ephemeral"] is True
    assert response.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    return response.args[0]


@pytest.mark.asyncio
async def test_receipt_commands_are_localized_and_registered_only_for_guild(commands):
    _, tree, wallet = commands
    assert tree.get_commands() == []
    payload = await wallet.get_translated_payload(tree, CommandTranslator())
    commands = {command["name"]: command for command in payload["options"]}
    assert set(commands) == {"receipt", "receipts"}
    assert commands["receipt"]["options"][0]["min_value"] == 1
    assert "квитанции" in commands["receipts"]["description_localizations"]["ru"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("locale", "enabled", "preference", "part"), [
    (discord.Locale.russian, True, "ru", "Civitas будет отправлять"),
    (discord.Locale.french, True, "en", "Civitas will send receipts"),
    (discord.Locale.russian, False, None, "отключены"),
])
async def test_receipt_preference_is_private(commands, locale, enabled, preference, part):
    service, _, wallet = commands
    invocation = interaction(locale=locale)

    await wallet.get_command("receipts").callback(invocation, enabled)

    service.set_preference.assert_awaited_once_with(GUILD_ID, USER_ID, locale=preference)
    assert part in private_reply(invocation)


@pytest.mark.asyncio
async def test_receipt_detail_checks_actor_and_returns_signed_amount_privately(commands):
    service, _, wallet = commands
    service.get_receipt.return_value = ReceiptDetails(2, -123456, TransactionKind.TRANSFER, NOW)
    invocation = interaction()

    await wallet.get_command("receipt").callback(invocation, 2)

    service.get_receipt.assert_awaited_once_with(GUILD_ID, USER_ID, 2)
    reply = private_reply(invocation)
    assert "Операция № 2" in reply
    assert "Исходящий перевод: -123456 CP" in reply


@pytest.mark.asyncio
async def test_receipt_detail_hides_missing_or_inaccessible_transaction(commands):
    service, _, wallet = commands
    service.get_receipt.side_effect = [None, CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)]
    missing = interaction()
    wrong_guild = interaction(guild_id=None)
    receipt = wallet.get_command("receipt")

    await receipt.callback(missing, 2)
    await receipt.callback(wrong_guild, 2)

    assert private_reply(missing) == "В вашем кошельке нет операции с таким номером."
    assert private_reply(wrong_guild) == "Команды Civitas доступны на игровом сервере."


@pytest.mark.asyncio
async def test_dm_is_neutral_and_nonce_stable_on_retry():
    user = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=9999)))
    client = SimpleNamespace(get_user=Mock(side_effect=[None, user]), fetch_user=AsyncMock(return_value=user))

    assert await _send_receipt(client, DELIVERY) == 9999
    assert await _send_receipt(client, replace(DELIVERY, lease_token=uuid4(), attempts=2)) == 9999

    client.fetch_user.assert_awaited_once_with(USER_ID)
    assert [call.args for call in client.get_user.call_args_list] == [(USER_ID,), (USER_ID,)]
    first, retry = user.send.await_args_list
    assert first == retry
    body = first.args[0]
    assert "Операция № 2" in body
    assert "/wallet receipt transaction_id:2" in body
    assert "123456" not in body
    assert "перевод" not in body
    assert str(USER_ID) not in body
    assert len(first.kwargs["nonce"]) <= 25
    assert first.kwargs["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(("error_type", "status", "code", "exception"), [
    (discord.Forbidden, 403, 50007, ReceiptUndeliverable),
    (discord.NotFound, 404, 10013, ReceiptUndeliverable),
    (discord.NotFound, 404, 10003, discord.NotFound),
    (discord.HTTPException, 503, 0, discord.HTTPException),
])
async def test_dm_errors_classify_terminal_and_retryable_failures(error_type, status, code, exception):
    failure = error_type(SimpleNamespace(status=status, reason="error"), {"code": code, "message": "error"})
    user = SimpleNamespace(send=AsyncMock(side_effect=failure))
    client = SimpleNamespace(get_user=Mock(return_value=user))

    with pytest.raises(exception):
        await _send_receipt(client, DELIVERY)


@pytest.mark.asyncio
async def test_receipt_worker_delivers_after_ready_and_stops_with_context():
    delivered = asyncio.Event()
    user = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=9999)))
    client = SimpleNamespace(
        get_user=Mock(return_value=user), wait_until_ready=AsyncMock(), is_closed=Mock(return_value=False),
    )

    async def run_due(send):
        assert await send(DELIVERY) == 9999
        delivered.set()
        return False

    service = SimpleNamespace(run_due=run_due)
    async with run_receipt_publisher(client, service):
        await asyncio.wait_for(delivered.wait(), timeout=1)

    client.wait_until_ready.assert_awaited_once()
    user.send.assert_awaited_once()
