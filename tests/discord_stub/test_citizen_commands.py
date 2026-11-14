from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.citizens import (
    Citizen,
    CitizensService,
    CitizenStatus,
    RegistrationOutcome,
    RegistrationResult,
)
from civitas.bot import create_bot

GUILD_ID = 123
USER_ID = 456
JOINED_AT = datetime(2026, 11, 11, tzinfo=UTC)


def fake_interaction(guild_id=GUILD_ID, user_id=USER_ID, locale=discord.Locale.russian):
    return SimpleNamespace(
        guild_id=guild_id,
        locale=locale,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=Mock(return_value=False),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def citizen_record(status):
    return Citizen(
        id=1,
        guild_id=1,
        discord_user_id=USER_ID,
        status=status,
        joined_at=JOINED_AT,
        verified_at=JOINED_AT,
    )


def get_command(bot, name):
    if name == "register":
        group = bot.tree.get_command("citizen", guild=bot.guild_scope)
        return group.get_command(name)
    return bot.tree.get_command(name, guild=bot.guild_scope)


@pytest.mark.asyncio
async def test_setup_syncs_commands_only_in_configured_guild(monkeypatch):
    bot = create_bot(citizens_service=Mock(spec=CitizensService), guild_id=GUILD_ID)
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    sync.assert_awaited_once_with(guild=bot.guild_scope)
    assert bot.tree.get_commands() == []
    assert bot.tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    assert {command.name for command in bot.tree.get_commands(guild=bot.guild_scope)} == {
        "start",
        "citizen",
        "profile",
        "help",
    }
    assert get_command(bot, "register").qualified_name == "citizen register"
    assert bot.intents.value == discord.Intents.none().value
    group = bot.tree.get_command("citizen", guild=bot.guild_scope)
    payload = await group.get_translated_payload(bot.tree, bot.tree.translator)
    assert payload["description"] == "Civitas citizenship"
    assert payload["description_localizations"] == {"ru": "Гражданство Civitas"}
    assert payload["options"][0]["name"] == "register"
    assert payload["options"][0]["description_localizations"] == {"ru": "Оформить гражданство"}


def test_role_sync_enables_only_the_guild_intent():
    bot = create_bot(citizens_service=Mock(spec=CitizensService), guild_id=GUILD_ID, sync_roles=True)

    assert bot.intents == discord.Intents(guilds=True)


@pytest.mark.asyncio
async def test_start_links_registration_and_profile():
    service = Mock(spec=CitizensService)
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    interaction = fake_interaction()

    await get_command(bot, "start").callback(interaction)

    service.authorize.assert_called_once_with(GUILD_ID, USER_ID)
    response = interaction.response.send_message.await_args
    assert response.kwargs == {"ephemeral": True}
    assert "/citizen register" in response.args[0]
    assert "/profile" in response.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [discord.Locale.american_english, discord.Locale.german])
async def test_english_registration_and_profile(locale):
    service = Mock(spec=CitizensService)
    record = citizen_record(CitizenStatus.VERIFIED)
    service.register.return_value = RegistrationResult(record, RegistrationOutcome.CREATED)
    service.get_profile.return_value = record
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)

    registration = fake_interaction(locale=locale)
    await get_command(bot, "register").callback(registration)
    assert registration.followup.send.await_args.args[0].startswith(
        "Citizenship granted. Citizenship status: verified."
    )

    profile = fake_interaction(locale=locale)
    await get_command(bot, "profile").callback(profile)
    profile.followup.send.assert_awaited_once_with("Citizenship status: verified.", ephemeral=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (RegistrationOutcome.CREATED, "Гражданство оформлено."),
        (RegistrationOutcome.RESTORED, "Гражданство восстановлено."),
        (RegistrationOutcome.EXISTING, "Запись о вашем гражданстве уже есть."),
    ],
)
async def test_register_defers_then_shows_private_outcome(outcome, expected):
    interaction = fake_interaction()
    service = Mock(spec=CitizensService)

    async def register(guild_id, user_id):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        return RegistrationResult(citizen=citizen_record(CitizenStatus.VERIFIED), outcome=outcome)

    service.register.side_effect = register
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)

    await get_command(bot, "register").callback(interaction)

    service.register.assert_awaited_once_with(GUILD_ID, USER_ID)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0].startswith(expected)
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "/citizen register"),
        (citizen_record(CitizenStatus.SUSPENDED), "приостановлено"),
    ],
)
async def test_profile_shows_current_status_or_registration_instructions(record, expected):
    service = Mock(spec=CitizensService)
    service.get_profile.return_value = record
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    interaction = fake_interaction()

    await get_command(bot, "profile").callback(interaction)

    service.get_profile.assert_awaited_once_with(GUILD_ID, USER_ID)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert expected in interaction.followup.send.await_args.args[0]
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_profile_lists_office_company_and_asset_without_a_balance():
    from civitas.app.profile import ProfileDetails
    from civitas.domain.government import OfficeKind

    citizens = Mock(spec=CitizensService)
    profile_service = Mock()
    profile_service.get_profile = AsyncMock(return_value=ProfileDetails(
        citizen_record(CitizenStatus.VERIFIED), (OfficeKind.PARLIAMENT,), ("Workshop",), ("Office",),
    ))
    bot = create_bot(citizens_service=citizens, guild_id=GUILD_ID, profile_service=profile_service)
    interaction = fake_interaction(locale=discord.Locale.american_english)
    await get_command(bot, "profile").callback(interaction)

    response = interaction.followup.send.await_args
    assert response.kwargs == {"ephemeral": True}
    assert "Public offices: Member of Parliament" in response.args[0]
    assert "Workshop" in response.args[0] and "Office" in response.args[0]
    assert "balance" not in response.args[0].lower()


@pytest.mark.asyncio
async def test_help_lists_guild_commands_in_interaction_language():
    bot = create_bot(citizens_service=Mock(spec=CitizensService), guild_id=GUILD_ID)
    interaction = fake_interaction(locale=discord.Locale.russian)
    await get_command(bot, "help").callback(interaction)

    response = interaction.response.send_message.await_args
    assert response.kwargs == {"ephemeral": True}
    assert "Команды Civitas" in response.args[0]
    assert "`/citizen register`" in response.args[0]
    assert "/wallet" not in response.args[0]


@pytest.mark.asyncio
async def test_departure_tracking_uses_only_configured_guild_and_members_intent():
    service = Mock(spec=CitizensService)
    service.mark_departed = AsyncMock(return_value=True)
    enabled = create_bot(citizens_service=service, guild_id=GUILD_ID, track_membership=True)
    disabled = create_bot(citizens_service=service, guild_id=GUILD_ID)
    user = SimpleNamespace(id=USER_ID)
    payload = SimpleNamespace(guild_id=GUILD_ID, user=user)

    await disabled.on_raw_member_remove(payload)
    await enabled.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_ID + 1, user=user))
    service.mark_departed.assert_not_awaited()
    await enabled.on_raw_member_remove(payload)

    assert enabled.intents.guilds and enabled.intents.members and not enabled.intents.message_content
    assert not disabled.intents.members
    assert service.mark_departed.await_args.args == (GUILD_ID, USER_ID)
    assert service.mark_departed.await_args.kwargs["observed_at"].tzinfo is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["start", "register", "profile"])
@pytest.mark.parametrize(
    ("guild_id", "user_id", "expected"),
    [
        (None, USER_ID, "игровом сервере"),
        (GUILD_ID + 1, USER_ID, "игровом сервере"),
        (GUILD_ID, USER_ID + 1, "пока не допущен"),
    ],
)
async def test_wrong_guild_or_denied_user_receives_private_error(
    command_name, guild_id, user_id, expected
):
    unit_of_work = Mock()
    service = CitizensService(unit_of_work, guild_id=GUILD_ID, allowed_user_ids=[USER_ID])
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    interaction = fake_interaction(guild_id, user_id)

    await get_command(bot, command_name).callback(interaction)

    send = (
        interaction.response.send_message if command_name == "start" else interaction.followup.send
    )
    send.assert_awaited_once()
    assert expected in send.await_args.args[0]
    assert send.await_args.kwargs == {"ephemeral": True}
    unit_of_work.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_error_uses_private_safe_response_and_log(caplog):
    bot = create_bot(citizens_service=Mock(spec=CitizensService), guild_id=GUILD_ID)
    interaction = fake_interaction()
    interaction.response.is_done.return_value = True
    error = app_commands.CommandInvokeError(
        get_command(bot, "register"), RuntimeError("database diagnostic contents")
    )

    await bot.tree.on_error(interaction, error)

    interaction.followup.send.assert_awaited_once_with(
        "Команда временно недоступна. Попробуйте ещё раз позже.", ephemeral=True
    )
    assert "RuntimeError" in caplog.text
    assert "database diagnostic contents" not in caplog.text
