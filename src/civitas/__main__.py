"""Run the Discord bot with explicitly configured credentials."""

import asyncio
import logging
import os
import sys

from civitas.app.assets import AssetService
from civitas.app.citizens import CitizensService
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.jobs import JobService, JobsRules
from civitas.app.market import MarketRules, MarketService
from civitas.app.private_jobs import PrivateJobService
from civitas.app.profile import ProfileService
from civitas.app.reports import ReportService
from civitas.app.treasury import TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.bot import create_bot
from civitas.bot.assets import add_asset_commands
from civitas.bot.government import add_government_commands, run_office_role_sync
from civitas.bot.jobs import add_job_commands
from civitas.bot.market import add_market_commands
from civitas.bot.private_jobs import add_private_job_commands
from civitas.bot.reports import add_report_commands, run_report_publisher
from civitas.bot.roles import run_role_sync
from civitas.bot.treasury import add_treasury_commands
from civitas.config import ConfigurationError, Settings, load_settings
from civitas.domain.jobs import BudgetCaps
from civitas.infrastructure.db import create_db_engine
from civitas.infrastructure.db.government import PostgresGovernmentUnitOfWork


async def run_bot(settings: Settings) -> None:
    engine = create_db_engine(settings.database_url)
    try:
        unit_of_work = PostgresGovernmentUnitOfWork(engine)
        wallet_settings = settings.wallet
        currency_symbol = wallet_settings.currency_symbol if wallet_settings else "₵"
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
        government_settings = settings.government
        president_role = government_settings.president_role_id if government_settings else None
        parliament_role = government_settings.parliament_role_id if government_settings else None
        bot = create_bot(
            citizens_service=service,
            guild_id=settings.guild_id,
            sync_roles=any(role is not None for role in (settings.citizen_role_id, president_role, parliament_role)),
            wallet_service=wallet,
            currency_symbol=currency_symbol,
            profile_service=ProfileService(unit_of_work, authorize=service.authorize),
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
                currency_symbol=currency_symbol,
            )
        market_settings = settings.market
        if market_settings is not None and funding is not None:
            market = MarketService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                arbiter_user_ids=market_settings.arbiter_ids,
                rules=MarketRules(
                    registration_fee=market_settings.company_fee,
                    sales_tax_bp=market_settings.sales_tax_basis_points,
                    company_limit=market_settings.company_limit_per_owner,
                ),
                funding=funding,
            )
            add_market_commands(
                bot.tree,
                guild_scope=bot.guild_scope,
                market_service=market,
                currency_symbol=currency_symbol,
            )
            private_jobs = PrivateJobService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                arbiter_user_ids=market_settings.arbiter_ids,
            )
            add_private_job_commands(
                bot.tree,
                guild_scope=bot.guild_scope,
                private_jobs_service=private_jobs,
                currency_symbol=currency_symbol,
            )
        if settings.asset_issuer_ids and funding is not None:
            assets = AssetService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                staff_user_ids=settings.asset_issuer_ids,
                funding=funding,
            )
            add_asset_commands(
                bot.tree, guild_scope=bot.guild_scope, asset_service=assets, currency_symbol=currency_symbol,
            )
        treasury_settings = settings.treasury
        if treasury_settings is not None:
            treasury = TreasuryService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                initiator_user_ids=treasury_settings.initiator_ids,
                approver_user_ids=treasury_settings.approver_ids,
                rules=TreasuryRules(
                    caps=BudgetCaps(
                        overall=treasury_settings.budget_weekly_limit,
                        npc=job_settings.npc_weekly_limit if job_settings else 0,
                        per_citizen=job_settings.personal_weekly_limit if job_settings else 0,
                    ),
                    contracts_weekly_limit=treasury_settings.contract_weekly_limit,
                    budget_timezone=treasury_settings.budget_timezone,
                ),
                elected_authority=treasury_settings.elected_authority,
            )
            add_treasury_commands(
                bot.tree, guild_scope=bot.guild_scope, treasury_service=treasury, currency_symbol=currency_symbol,
            )
        publication_channel = settings.treasury_report_channel_id
        reports = None
        if wallet_settings is not None or publication_channel is not None:
            reports = ReportService(
                unit_of_work, guild_id=settings.guild_id, timezone=settings.report_timezone,
                publication_channel_id=publication_channel,
            )
            add_report_commands(
                bot.tree, guild_scope=bot.guild_scope, service=reports, currency_symbol=currency_symbol,
            )
        government = None
        if government_settings is not None:
            government = GovernmentService(
                unit_of_work, guild_id=settings.guild_id, authorize=service.authorize,
                commissioner_user_ids=government_settings.commissioner_ids,
                rules=ElectionRules(
                    min_eligible=government_settings.min_eligible, quorum_bp=government_settings.quorum_bp,
                    quorum_min=government_settings.quorum_min, parliament_seats=government_settings.parliament_seats,
                    term_days=government_settings.term_days, voting_hours=government_settings.voting_hours,
                    extension_hours=government_settings.extension_hours, poll_hosts=government_settings.poll_hosts,
                ),
            )
            add_government_commands(bot.tree, guild_scope=bot.guild_scope, government_service=government)
        async with (
            bot,
            run_role_sync(bot, unit_of_work, guild_id=settings.guild_id, role_id=settings.citizen_role_id),
            run_report_publisher(
                bot, reports if publication_channel is not None else None,
                guild_id=settings.guild_id, currency_symbol=currency_symbol,
            ),
            run_office_role_sync(
                bot, government, guild_id=settings.guild_id,
                president_role_id=president_role, parliament_role_id=parliament_role,
            ),
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
