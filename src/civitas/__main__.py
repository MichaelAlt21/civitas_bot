"""Run the Discord bot with explicitly configured credentials."""

import asyncio
import logging
import os
import sys

from civitas.app.citizens import CitizensService
from civitas.bot import create_bot
from civitas.config import ConfigurationError, Settings, load_settings
from civitas.infrastructure.db import PostgresCitizenUnitOfWork, create_db_engine


async def run_bot(settings: Settings) -> None:
    engine = create_db_engine(settings.database_url)
    try:
        service = CitizensService(
            PostgresCitizenUnitOfWork(engine),
            guild_id=settings.guild_id,
            allowed_user_ids=settings.allowed_user_ids,
        )
        async with create_bot(citizens_service=service, guild_id=settings.guild_id) as bot:
            await bot.start(settings.discord_token)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        settings = load_settings(os.environ)
    except ConfigurationError as error:
        raise SystemExit(str(error)) from None

    logging.basicConfig(level=logging.INFO)
    # Psycopg's asynchronous connections require a selector loop on Windows.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run_bot(settings), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
