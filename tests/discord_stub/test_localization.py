import discord
import pytest
from discord import app_commands

from civitas.bot.localization import CommandTranslator, message


@pytest.mark.parametrize("locale", [discord.Locale.russian, "ru"])
def test_russian_locale_renders_russian_text(locale):
    assert message(locale, "profile.status", status=message(locale, "status.verified")) == (
        "Статус гражданства: подтверждено."
    )


@pytest.mark.parametrize(
    "locale",
    [discord.Locale.american_english, discord.Locale.british_english, discord.Locale.german, None],
)
def test_all_other_locales_fall_back_to_english(locale):
    assert message(locale, "profile.status", status=message(locale, "status.verified")) == (
        "Citizenship status: verified."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "english", "russian", "location"),
    [
        (
            "command.start.description",
            "Get started in Civitas",
            "Как начать игру в Civitas",
            app_commands.TranslationContextLocation.command_description,
        ),
        (
            "command.citizen.description",
            "Civitas citizenship",
            "Гражданство Civitas",
            app_commands.TranslationContextLocation.group_description,
        ),
        (
            "command.register.description",
            "Register for citizenship",
            "Оформить гражданство",
            app_commands.TranslationContextLocation.command_description,
        ),
        (
            "command.profile.description",
            "Check your citizenship status",
            "Проверить свой гражданский статус",
            app_commands.TranslationContextLocation.command_description,
        ),
    ],
)
async def test_russian_command_metadata_overrides_english_default(key, english, russian, location):
    translator = CommandTranslator()
    description = app_commands.locale_str(message(discord.Locale.american_english, key), key=key)
    context = app_commands.TranslationContext(location, None)

    assert description.message == english
    assert await translator.translate(description, discord.Locale.russian, context) == russian
    assert await translator.translate(description, discord.Locale.german, context) is None


@pytest.mark.asyncio
async def test_slash_command_name_has_no_localization():
    context = app_commands.TranslationContext(
        app_commands.TranslationContextLocation.command_name, None
    )
    translation = await CommandTranslator().translate(
        app_commands.locale_str("start"), discord.Locale.russian, context
    )

    assert translation is None
