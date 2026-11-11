import pytest

from civitas.config import ConfigurationError, load_settings


@pytest.fixture
def env() -> dict[str, str]:
    return {
        "CIVITAS_DATABASE_URL": "postgresql+psycopg://civitas:local-password@db/civitas",
        "CIVITAS_DISCORD_TOKEN": "test-token",
        "CIVITAS_DISCORD_GUILD_ID": "1000",
        "CIVITAS_ALLOWED_USER_IDS": " 12, 34, 12, 18446744073709551615 ",
    }


def test_parses_settings_without_revealing_secrets(env: dict[str, str]) -> None:
    settings = load_settings(env)

    assert settings.guild_id == 1000
    assert settings.allowed_user_ids == {12, 34, (1 << 64) - 1}
    assert "test-token" not in repr(settings)
    assert "local-password" not in repr(settings)


def test_omitted_allowlist_admits_nobody(env: dict[str, str]) -> None:
    del env["CIVITAS_ALLOWED_USER_IDS"]

    assert not load_settings(env).allowed_user_ids


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CIVITAS_DISCORD_GUILD_ID", "0"),
        ("CIVITAS_DISCORD_GUILD_ID", "18446744073709551616"),
        ("CIVITAS_DISCORD_TOKEN", ""),
        ("CIVITAS_ALLOWED_USER_IDS", "12,,34"),
        ("CIVITAS_DATABASE_URL", "postgresql+psycopg://localhost"),
        ("CIVITAS_DATABASE_URL", "sqlite:///civitas.db"),
    ],
)
def test_invalid_setting_names_variable(env: dict[str, str], name: str, value: str) -> None:
    env[name] = value

    with pytest.raises(ConfigurationError, match=name):
        load_settings(env)
