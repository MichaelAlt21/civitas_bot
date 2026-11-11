"""Run the Discord bot with explicitly configured credentials."""

import asyncio
import logging
import os
import sys

from civitas.app.citizens import CitizensService
from civitas.app.jobs import JobService, JobsRules
from civitas.app.wallet import WalletFunding, WalletService
from civitas.bot import create_bot
from civitas.bot.jobs import add_job_commands
from civitas.bot.roles import run_role_sync
from civitas.config import ConfigurationError, Settings, load_settings
from civitas.infrastructure.db import create_db_engine
from civitas.infrastructure.db.jobs import PostgresJobsUnitOfWork


async def run_bot(settings: Settings) -> None:
    engine = create_db_engine(settings.database_url)
    try:
        unit_of_work = PostgresJobsUnitOfWork(engine)
        wallet_settings = settings.wallet
        funding = None
        if wallet_settings is not None:
            funding = WalletFunding(
                starter_grant=wallet_settings.starter_grant,
                treasury_genesis=wallet_settings.treasury_genesis,
            )
        service = CitizensService(
            unit_of_work,
            guild_id=settings.guild_id,
            allowed_user_ids=settings.allowed_user_ids,
            fund_verified=funding.fund_registered_citizen if funding is not None else None,
        )
        wallet = None
        if funding is not None:
            wallet = WalletService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                funding=funding,
            )
            await wallet.initialize_guild()
        bot = create_bot(
            citizens_service=service,
            guild_id=settings.guild_id,
            sync_roles=settings.citizen_role_id is not None,
            wallet_service=wallet,
            currency_symbol=wallet_settings.currency_symbol if wallet_settings else "₵",
        )
        job_settings = settings.jobs
        if job_settings is not None:
            jobs = JobService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                staff_user_ids=job_settings.reviewer_ids,
                rules=JobsRules(
                    overall_weekly_limit=job_settings.budget_weekly_limit,
                    npc_weekly_limit=job_settings.npc_weekly_limit,
                    personal_weekly_limit=job_settings.personal_weekly_limit,
                    reward_min=job_settings.reward_min,
                    reward_max=job_settings.reward_max,
                    budget_timezone=job_settings.budget_timezone,
                    claim_cooldown_minutes=job_settings.cooldown_minutes,
                ),
            )
            add_job_commands(
                bot.tree,
                guild_scope=bot.guild_scope,
                job_service=jobs,
                currency_symbol=wallet_settings.currency_symbol if wallet_settings else "₵",
            )
        async with bot, run_role_sync(
            bot, unit_of_work, guild_id=settings.guild_id, role_id=settings.citizen_role_id
        ):
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
