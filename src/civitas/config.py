"""Environment settings for the bot process."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from civitas.app.citizens import is_discord_id


class ConfigurationError(ValueError):
    """Missing or invalid startup configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(repr=False)
    discord_token: str = field(repr=False)
    guild_id: int
    allowed_user_ids: frozenset[int]


def load_settings(env: Mapping[str, str]) -> Settings:
    database_url = _required(env, "CIVITAS_DATABASE_URL")
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        raise ConfigurationError("Укажите корректный CIVITAS_DATABASE_URL.") from None
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise ConfigurationError(
            "CIVITAS_DATABASE_URL должен использовать postgresql+psycopg и содержать имя БД."
        )

    token = _required(env, "CIVITAS_DISCORD_TOKEN")
    guild_id = _discord_id(
        _required(env, "CIVITAS_DISCORD_GUILD_ID"), "CIVITAS_DISCORD_GUILD_ID"
    )
    allowed_ids = env.get("CIVITAS_ALLOWED_USER_IDS", "").strip()
    return Settings(
        database_url=database_url,
        discord_token=token,
        guild_id=guild_id,
        allowed_user_ids=frozenset(
            _discord_id(value.strip(), "CIVITAS_ALLOWED_USER_IDS")
            for value in (allowed_ids.split(",") if allowed_ids else ())
        ),
    )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Задайте {name}.")
    return value


def _discord_id(value: str, name: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ConfigurationError(f"Проверьте Discord ID в {name}.")
    discord_id = int(value)
    if not is_discord_id(discord_id):
        raise ConfigurationError(f"Проверьте Discord ID в {name}.")
    return discord_id
