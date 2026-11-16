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
from civitas.domain.ledger import LedgerError, LedgerErrorReason

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
        edit_original_response=AsyncMock(),
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

    service.authorize.assert_awaited_once_with(GUILD_ID, USER_ID)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response = interaction.edit_original_response.await_args
    assert response.kwargs["view"].register_button.label == "Стать гражданином"
    assert "/citizen register" in response.kwargs["content"]
    assert "/profile" in response.kwargs["content"]


@pytest.mark.asyncio
async def test_start_button_uses_same_registration_as_slash_command():
    service = Mock(spec=CitizensService)
    citizen = citizen_record(CitizenStatus.VERIFIED)
    service.register.side_effect = [
        RegistrationResult(citizen, RegistrationOutcome.CREATED),
        RegistrationResult(citizen, RegistrationOutcome.EXISTING),
    ]
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    opening = fake_interaction(locale=discord.Locale.american_english)
    await get_command(bot, "start").callback(opening)
    view = opening.edit_original_response.await_args.kwargs["view"]
    assert view.register_button.label == "Become a citizen"
    service.register.assert_not_awaited()

    click = fake_interaction(locale=discord.Locale.american_english)
    assert await view.interaction_check(click)
    await view.register_button.callback(click)
    slash = fake_interaction(locale=discord.Locale.american_english)
    await get_command(bot, "register").callback(slash)

    for interaction in (click, slash):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}
    assert click.followup.send.await_args.args[0].startswith("Citizenship granted.")
    assert slash.followup.send.await_args.args[0].startswith("You already have a citizenship record.")
    assert service.register.await_count == 2
    service.register.assert_awaited_with(GUILD_ID, USER_ID)


@pytest.mark.asyncio
async def test_start_button_refuses_other_users_and_guilds_and_disappears_on_timeout():
    service = Mock(spec=CitizensService)
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    opening = fake_interaction()
    await get_command(bot, "start").callback(opening)
    view = opening.edit_original_response.await_args.kwargs["view"]
    for click in (fake_interaction(user_id=USER_ID + 1), fake_interaction(guild_id=GUILD_ID + 1)):
        assert not await view.interaction_check(click)
        click.response.send_message.assert_awaited_once_with(
            "Эта кнопка регистрации доступна игроку, который вызвал `/start`.", ephemeral=True,
        )
    service.register.assert_not_awaited()

    await view.on_timeout()

    opening.edit_original_response.assert_awaited_with(view=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["button", "slash"])
async def test_registration_explains_economy_pause_privately(route):
    service = Mock(spec=CitizensService)
    service.register.side_effect = LedgerError(LedgerErrorReason.ECONOMY_READ_ONLY)
    bot = create_bot(citizens_service=service, guild_id=GUILD_ID)
    request = fake_interaction()
    if route == "button":
        opening = fake_interaction()
        await get_command(bot, "start").callback(opening)
        view = opening.edit_original_response.await_args.kwargs["view"]
        assert await view.interaction_check(request)
        await view.register_button.callback(request)
    else:
        await get_command(bot, "register").callback(request)

    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response = request.followup.send.await_args
    assert "Экономика временно работает в режиме просмотра" in response.args[0]
    assert response.kwargs == {"ephemeral": True}


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
    assert "Public offices: Senator" in response.args[0]
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

    if command_name == "start":
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.edit_original_response.assert_awaited_once()
        assert expected in interaction.edit_original_response.await_args.kwargs["content"]
    else:
        interaction.followup.send.assert_awaited_once()
        assert expected in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}
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


@pytest.mark.asyncio
async def test_tree_error_handler_explains_unhandled_economy_pause():
    bot = create_bot(citizens_service=Mock(spec=CitizensService), guild_id=GUILD_ID)
    interaction = fake_interaction()
    error = app_commands.CommandInvokeError(
        get_command(bot, "register"), LedgerError(LedgerErrorReason.ECONOMY_READ_ONLY),
    )

    await bot.tree.on_error(interaction, error)

    interaction.response.send_message.assert_awaited_once_with(
        "Экономика временно работает в режиме просмотра. Существующие записи доступны.", ephemeral=True,
    )
