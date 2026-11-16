"""Run the Discord bot with explicitly configured credentials."""

import argparse
import asyncio
import logging
import os
import sys

from civitas.app.assets import AssetService
from civitas.app.admin_audit import AdminAuditService
from civitas.app.admin_operators import AdminOperatorPolicy
from civitas.app.admission import AdmissionService
from civitas.app.citizens import CitizensService
from civitas.app.court import CourtService
from civitas.app.court_appeals import CourtAppealService
from civitas.app.court_cases import CourtCaseService
from civitas.app.government import ElectionRules, GovernmentService
from civitas.app.jobs import JobService, JobsRules
from civitas.app.law import ActKind, LawService
from civitas.app.maintenance import MaintenanceService
from civitas.app.market import MarketRules, MarketService
from civitas.app.private_jobs import PrivateJobService
from civitas.app.presentation import GuildPresentation, PresentationService
from civitas.app.profile import ProfileService
from civitas.app.prosecutor import ProsecutorService
from civitas.app.receipts import ReceiptService
from civitas.app.reports import ReportService
from civitas.app.treasury import TreasuryRules, TreasuryService
from civitas.app.wallet import WalletFunding, WalletService
from civitas.bot import create_bot
from civitas.bot.admin import add_admin_commands
from civitas.bot.assets import add_asset_commands
from civitas.bot.court import add_court_commands
from civitas.bot.appeals import add_appeal_commands
from civitas.bot.government import add_government_commands, run_office_role_sync
from civitas.bot.jobs import add_job_commands
from civitas.bot.law import add_law_commands
from civitas.bot.market import add_market_commands
from civitas.bot.membership import run_membership_sync
from civitas.bot.private_jobs import add_private_job_commands
from civitas.bot.prosecutor import add_prosecutor_commands
from civitas.bot.receipts import add_receipt_commands, run_receipt_publisher
from civitas.bot.reports import add_report_commands, run_report_publisher
from civitas.bot.roles import run_role_sync
from civitas.bot.treasury import add_treasury_commands
from civitas.config import ConfigurationError, Settings, load_settings
from civitas.domain.jobs import BudgetCaps
from civitas.domain.ledger import LedgerError, LedgerErrorReason
from civitas.infrastructure.db import create_db_engine
from civitas.infrastructure.db.admin_audit import PostgresAdminAuditUnitOfWork
from civitas.infrastructure.db.admission import PostgresAdmissionUnitOfWork
from civitas.infrastructure.db.court import PostgresCourtUnitOfWork
from civitas.infrastructure.db.court_appeals import PostgresCourtAppealUnitOfWork
from civitas.infrastructure.db.court_cases import PostgresCourtCaseUnitOfWork
from civitas.infrastructure.db.law import PostgresLawUnitOfWork
from civitas.infrastructure.db.maintenance import PostgresMaintenanceUnitOfWork
from civitas.infrastructure.db.presentation import PostgresPresentationUnitOfWork
from civitas.infrastructure.db.prosecutor import PostgresProsecutorUnitOfWork
from civitas.infrastructure.db.receipts import PostgresReceiptUnitOfWork


async def run_bot(settings: Settings) -> None:
    engine = create_db_engine(settings.database_url)
    try:
        unit_of_work = PostgresReceiptUnitOfWork(engine)
        admission = AdmissionService(PostgresAdmissionUnitOfWork(engine), guild_id=settings.guild_id)
        await admission.initialize(settings.allowed_user_ids)
        government_settings = settings.government
        presentation = PresentationService(PostgresPresentationUnitOfWork(engine), guild_id=settings.guild_id)
        await presentation.initialize(GuildPresentation(
            citizen_role_id=settings.citizen_role_id,
            president_role_id=government_settings.president_role_id if government_settings else None,
            parliament_role_id=government_settings.parliament_role_id if government_settings else None,
            report_channel_id=settings.treasury_report_channel_id,
        ))

        async def citizen_role_id() -> int | None:
            return (await presentation.current()).citizen_role_id

        async def office_role_ids() -> tuple[int | None, int | None]:
            current = await presentation.current()
            return current.president_role_id, current.parliament_role_id

        async def report_channel_id() -> int | None:
            return (await presentation.current()).report_channel_id

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
            authorize_admission=admission.authorize,
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
            try:
                await wallet.initialize_guild()
            except LedgerError as error:
                if error.reason != LedgerErrorReason.ECONOMY_READ_ONLY:
                    raise
        bot = create_bot(
            citizens_service=service,
            guild_id=settings.guild_id,
            sync_roles=True,
            wallet_service=wallet,
            currency_symbol=currency_symbol,
            profile_service=ProfileService(unit_of_work, authorize=service.authorize),
            track_membership=settings.track_membership,
        )
        maintenance = audit = None
        if settings.technical_operator_ids:
            operator_policy = AdminOperatorPolicy(
                discord_guild_id=settings.guild_id, operator_user_ids=settings.technical_operator_ids,
            )
            maintenance = MaintenanceService(
                PostgresMaintenanceUnitOfWork(engine), operator_policy=operator_policy, guild_id=settings.guild_id,
            )
            audit = AdminAuditService(PostgresAdminAuditUnitOfWork(engine), operator_policy=operator_policy)
        add_admin_commands(
            bot.tree, guild_scope=bot.guild_scope, admission_service=admission, presentation_service=presentation,
            maintenance_service=maintenance, audit_service=audit,
        )
        receipts = None
        if wallet is not None:
            receipts = ReceiptService(unit_of_work, guild_id=settings.guild_id, authorize=service.authorize)
            add_receipt_commands(bot.tree, guild_scope=bot.guild_scope, service=receipts, currency_symbol=currency_symbol)
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
        if funding is not None and (settings.asset_issuer_ids or settings.asset_resale_tax_bp is not None):
            assets = AssetService(
                unit_of_work,
                guild_id=settings.guild_id,
                authorize=service.authorize,
                staff_user_ids=settings.asset_issuer_ids,
                funding=funding,
                resale_tax_bp=settings.asset_resale_tax_bp,
                use_persisted_admission=True,
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
        reports = ReportService(
            unit_of_work, guild_id=settings.guild_id, timezone=settings.report_timezone,
            get_publication_channel_id=report_channel_id,
        )
        add_report_commands(bot.tree, guild_scope=bot.guild_scope, service=reports, currency_symbol=currency_symbol)
        government = None
        if government_settings is not None:
            government = GovernmentService(
                unit_of_work, guild_id=settings.guild_id, authorize=service.authorize,
                commissioner_user_ids=government_settings.commissioner_ids,
                use_persisted_admission=True,
                rules=ElectionRules(
                    min_eligible=government_settings.min_eligible, quorum_bp=government_settings.quorum_bp,
                    quorum_min=government_settings.quorum_min, parliament_seats=government_settings.parliament_seats,
                    term_days=government_settings.term_days, voting_hours=government_settings.voting_hours,
                    extension_hours=government_settings.extension_hours, poll_hosts=government_settings.poll_hosts,
                ),
            )
            add_government_commands(bot.tree, guild_scope=bot.guild_scope, government_service=government)
            add_court_commands(
                bot.tree, guild_scope=bot.guild_scope,
                service=CourtService(PostgresCourtUnitOfWork(engine), guild_id=settings.guild_id),
                case_service=CourtCaseService(PostgresCourtCaseUnitOfWork(engine), guild_id=settings.guild_id),
            )
            add_appeal_commands(
                bot.tree, guild_scope=bot.guild_scope,
                service=CourtAppealService(
                    PostgresCourtAppealUnitOfWork(engine), guild_id=settings.guild_id,
                    commissioner_user_ids=government_settings.commissioner_ids,
                    quorum_bp=government_settings.quorum_bp, quorum_min=government_settings.quorum_min,
                    poll_hosts=government_settings.poll_hosts,
                ),
            )
            add_law_commands(
                bot.tree, guild_scope=bot.guild_scope,
                service=LawService(PostgresLawUnitOfWork(engine), guild_id=settings.guild_id),
            )
            add_law_commands(
                bot.tree, guild_scope=bot.guild_scope, act_kind=ActKind.DECREE,
                service=LawService(PostgresLawUnitOfWork(engine), guild_id=settings.guild_id, act_kind=ActKind.DECREE),
            )
            add_prosecutor_commands(
                bot.tree, guild_scope=bot.guild_scope,
                service=ProsecutorService(PostgresProsecutorUnitOfWork(engine), guild_id=settings.guild_id),
            )
        async with (
            bot,
            run_role_sync(bot, unit_of_work, guild_id=settings.guild_id, get_role_id=citizen_role_id),
            run_membership_sync(bot, unit_of_work, service, guild_id=settings.guild_id, enabled=settings.track_membership),
            run_receipt_publisher(bot, receipts),
            run_report_publisher(
                bot, reports,
                guild_id=settings.guild_id, currency_symbol=currency_symbol,
            ),
            run_office_role_sync(
                bot, government, guild_id=settings.guild_id, get_role_ids=office_role_ids,
            ),
        ):
            await bot.start(settings.discord_token)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m civitas", description="Бот Civitas для Discord.")
    parser.add_argument(
        "--check-config", action="store_true", help="Проверить переменные окружения и завершить работу.",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(os.environ)
    except ConfigurationError as error:
        raise SystemExit(str(error)) from None

    if args.check_config:
        print("Конфигурация Civitas валидна.")
        return

    logging.basicConfig(level=logging.INFO)
    # Psycopg's asynchronous connections require a selector loop on Windows.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run_bot(settings), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
