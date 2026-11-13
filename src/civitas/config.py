"""Environment settings for the bot process."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from civitas.app.citizens import is_discord_id


class ConfigurationError(ValueError):
    """Missing or invalid startup configuration."""


@dataclass(frozen=True, slots=True)
class WalletSettings:
    starter_grant: int
    treasury_genesis: int
    currency_symbol: str


@dataclass(frozen=True, slots=True)
class JobsSettings:
    reviewer_ids: frozenset[int]
    budget_weekly_limit: int
    npc_weekly_limit: int
    personal_weekly_limit: int
    reward_min: int
    reward_max: int
    budget_timezone: tzinfo
    cooldown_minutes: int


@dataclass(frozen=True, slots=True)
class MarketSettings:
    company_fee: int
    company_limit_per_owner: int
    sales_tax_basis_points: int
    arbiter_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class TreasurySettings:
    initiator_ids: frozenset[int]
    approver_ids: frozenset[int]
    budget_weekly_limit: int
    contract_weekly_limit: int
    budget_timezone: tzinfo
    elected_authority: bool = False


@dataclass(frozen=True, slots=True)
class GovernmentSettings:
    commissioner_ids: frozenset[int]
    poll_hosts: frozenset[str]
    min_eligible: int
    quorum_bp: int
    quorum_min: int
    parliament_seats: int
    term_days: int
    voting_hours: int
    extension_hours: int
    president_role_id: int | None
    parliament_role_id: int | None


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(repr=False)
    discord_token: str = field(repr=False)
    guild_id: int
    allowed_user_ids: frozenset[int]
    citizen_role_id: int | None = None
    wallet: WalletSettings | None = None
    jobs: JobsSettings | None = None
    market: MarketSettings | None = None
    asset_issuer_ids: frozenset[int] = frozenset()
    treasury: TreasurySettings | None = None
    treasury_report_channel_id: int | None = None
    report_timezone: tzinfo = UTC
    government: GovernmentSettings | None = None


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
    role_value = env.get("CIVITAS_CITIZEN_ROLE_ID", "").strip()
    role_id = _discord_id(role_value, "CIVITAS_CITIZEN_ROLE_ID") if role_value else None
    if role_id == guild_id:
        raise ConfigurationError("CIVITAS_CITIZEN_ROLE_ID не может указывать на @everyone.")
    wallet = _load_wallet_settings(env)
    report_channel_key = "CIVITAS_TREASURY_REPORT_CHANNEL_ID"
    report_channel = env.get(report_channel_key, "").strip()
    report_channel_id = _discord_id(report_channel, report_channel_key) if report_channel else None
    asset_issuer_ids = _discord_ids(env, "CIVITAS_ASSET_ISSUER_IDS")
    if asset_issuer_ids and wallet is None:
        raise ConfigurationError("Для продажи имущества настройте CIVITAS_STARTER_GRANT и CIVITAS_TREASURY_GENESIS.")
    government = _load_government_settings(env)
    treasury = _load_treasury_settings(env, wallet)
    if treasury is not None and treasury.elected_authority and government is None:
        raise ConfigurationError("Для CIVITAS_ELECTED_TREASURY сначала настройте выборы.")
    roles = [role for role in (role_id, government.president_role_id if government else None,
                              government.parliament_role_id if government else None) if role is not None]
    if guild_id in roles or len(roles) != len(set(roles)):
        raise ConfigurationError("Укажите отдельные ID для роли гражданина и выборных ролей.")
    return Settings(
        database_url=database_url,
        discord_token=token,
        guild_id=guild_id,
        allowed_user_ids=_discord_ids(env, "CIVITAS_ALLOWED_USER_IDS"),
        citizen_role_id=role_id,
        wallet=wallet,
        jobs=_load_jobs_settings(env, wallet),
        market=_load_market_settings(env, wallet),
        asset_issuer_ids=asset_issuer_ids,
        treasury=treasury,
        treasury_report_channel_id=report_channel_id,
        report_timezone=_budget_timezone(env) if wallet is not None or report_channel_id is not None else UTC,
        government=government,
    )


def _load_wallet_settings(env: Mapping[str, str]) -> WalletSettings | None:
    starter_grant = env.get("CIVITAS_STARTER_GRANT", "").strip()
    treasury_genesis = env.get("CIVITAS_TREASURY_GENESIS", "").strip()
    if not starter_grant and not treasury_genesis:
        return None
    currency_symbol = env.get("CIVITAS_CURRENCY_SYMBOL", "₵").strip() or "₵"
    if len(currency_symbol) > 8 or not currency_symbol.isprintable():
        raise ConfigurationError("Проверьте CIVITAS_CURRENCY_SYMBOL.")
    return WalletSettings(
        starter_grant=_money(starter_grant, "CIVITAS_STARTER_GRANT"),
        treasury_genesis=_money(treasury_genesis, "CIVITAS_TREASURY_GENESIS"),
        currency_symbol=currency_symbol,
    )


def _load_jobs_settings(env: Mapping[str, str], wallet: WalletSettings | None) -> JobsSettings | None:
    keys = (
        "CIVITAS_BUDGET_WEEKLY_LIMIT",
        "CIVITAS_NPC_WEEKLY_LIMIT",
        "CIVITAS_NPC_PERSONAL_WEEKLY_LIMIT",
        "CIVITAS_NPC_REWARD_MIN",
        "CIVITAS_NPC_REWARD_MAX",
    )
    values = tuple(env.get(key, "").strip() for key in keys)
    reviewers = _discord_ids(env, "CIVITAS_NPC_REVIEWER_IDS")
    if not reviewers and not any(values[1:]):
        return None
    if wallet is None:
        raise ConfigurationError("Для NPC-заданий настройте CIVITAS_STARTER_GRANT и CIVITAS_TREASURY_GENESIS.")
    if not reviewers:
        raise ConfigurationError("Задайте CIVITAS_NPC_REVIEWER_IDS.")

    total, npc, personal, minimum, maximum = (
        _money(value, key) for key, value in zip(keys, values, strict=True)
    )
    if not 1 <= minimum <= maximum or npc > total:
        raise ConfigurationError("Проверьте порядок размеров наград и недельных лимитов NPC.")
    cooldown_key = "CIVITAS_NPC_COOLDOWN_MINUTES"
    cooldown = _money(env.get(cooldown_key, "").strip() or "0", cooldown_key)
    return JobsSettings(
        reviewer_ids=reviewers,
        budget_weekly_limit=total,
        npc_weekly_limit=npc,
        personal_weekly_limit=personal,
        reward_min=minimum,
        reward_max=maximum,
        budget_timezone=_budget_timezone(env),
        cooldown_minutes=cooldown,
    )


def _load_treasury_settings(env: Mapping[str, str], wallet: WalletSettings | None) -> TreasurySettings | None:
    initiator_key = "CIVITAS_TREASURY_INITIATOR_IDS"
    approver_key = "CIVITAS_TREASURY_APPROVER_IDS"
    contract_key = "CIVITAS_CONTRACT_WEEKLY_LIMIT"
    overall_key = "CIVITAS_BUDGET_WEEKLY_LIMIT"
    initiators = _discord_ids(env, initiator_key)
    approvers = _discord_ids(env, approver_key)
    contract_limit = env.get(contract_key, "").strip()
    elected = _enabled(env, "CIVITAS_ELECTED_TREASURY")
    if not initiators and not approvers and not contract_limit and not elected:
        return None
    if wallet is None:
        raise ConfigurationError("Для казённых контрактов настройте CIVITAS_STARTER_GRANT и CIVITAS_TREASURY_GENESIS.")
    if not elected and (not initiators or not approvers):
        raise ConfigurationError(f"Задайте {initiator_key} и {approver_key}.")
    overall = _money(_required(env, overall_key), overall_key)
    contracts = _money(contract_limit, contract_key)
    if contracts > overall:
        raise ConfigurationError(f"{contract_key} не может превышать {overall_key}.")
    return TreasurySettings(initiators, approvers, overall, contracts, _budget_timezone(env), elected)


def _enabled(env: Mapping[str, str], key: str) -> bool:
    value = env.get(key, "").strip().lower()
    if value in ("", "false", "0"):
        return False
    if value in ("true", "1"):
        return True
    raise ConfigurationError(f"Для {key} укажите true или false.")


def _load_government_settings(env: Mapping[str, str]) -> GovernmentSettings | None:
    commissioners = _discord_ids(env, "CIVITAS_ELECTION_COMMISSIONER_IDS")
    if not commissioners:
        return None
    if len(commissioners) < 2:
        raise ConfigurationError("Для выборов задайте минимум два разных CIVITAS_ELECTION_COMMISSIONER_IDS.")
    poll_hosts_key = "CIVITAS_ELECTION_POLL_HOSTS"
    hosts = frozenset(host.strip().lower() for host in _required(env, poll_hosts_key).split(","))
    if any(not host or any(not (ch.isascii() and (ch.isalnum() or ch in ".-")) for ch in host) for host in hosts):
        raise ConfigurationError(f"Укажите имена хостов без URL в {poll_hosts_key}.")

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        key = f"CIVITAS_ELECTION_{name}"
        amount = _money(env.get(key, "").strip() or str(default), key)
        if not minimum <= amount <= maximum:
            raise ConfigurationError(f"Проверьте диапазон {key}.")
        return amount

    def role_id(key: str) -> int | None:
        value = env.get(key, "").strip()
        return _discord_id(value, key) if value else None

    return GovernmentSettings(
        commissioner_ids=commissioners, poll_hosts=hosts,
        min_eligible=integer("MIN_ELIGIBLE", 10, 1, 10_000),
        quorum_bp=integer("QUORUM_BP", 4000, 1, 10_000),
        quorum_min=integer("QUORUM_MIN", 4, 1, 10_000),
        parliament_seats=integer("PARLIAMENT_SEATS", 3, 1, 100),
        term_days=integer("TERM_DAYS", 28, 1, 3650),
        voting_hours=integer("VOTING_HOURS", 48, 1, 8760),
        extension_hours=integer("EXTENSION_HOURS", 24, 1, 8760),
        president_role_id=role_id("CIVITAS_PRESIDENT_ROLE_ID"),
        parliament_role_id=role_id("CIVITAS_PARLIAMENT_ROLE_ID"),
    )


def _budget_timezone(env: Mapping[str, str]) -> tzinfo:
    timezone_name = env.get("CIVITAS_BUDGET_TIMEZONE", "UTC").strip() or "UTC"
    try:
        return UTC if timezone_name == "UTC" else ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigurationError("Проверьте часовой пояс CIVITAS_BUDGET_TIMEZONE.") from None


def _money(value: str, name: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise ConfigurationError(f"Задайте целое неотрицательное значение {name}.")
    amount = int(value)
    if amount > 2**63 - 1:
        raise ConfigurationError(f"Значение {name} превышает диапазон BIGINT.")
    return amount


def _load_market_settings(env: Mapping[str, str], wallet: WalletSettings | None) -> MarketSettings | None:
    keys = ("CIVITAS_COMPANY_FEE", "CIVITAS_COMPANY_LIMIT", "CIVITAS_SALES_TAX_BP")
    values = tuple(env.get(key, "").strip() for key in keys)
    arbiters = _discord_ids(env, "CIVITAS_MARKET_ARBITER_IDS")
    if not arbiters and not any(values):
        return None
    if wallet is None:
        raise ConfigurationError("Для рынка настройте CIVITAS_STARTER_GRANT и CIVITAS_TREASURY_GENESIS.")
    fee, company_limit, tax_rate = (_money(value, key) for key, value in zip(keys, values, strict=True))
    if company_limit == 0:
        raise ConfigurationError("CIVITAS_COMPANY_LIMIT должен быть больше нуля.")
    if tax_rate > 10_000:
        raise ConfigurationError("CIVITAS_SALES_TAX_BP должен быть в пределах 0–10000.")
    return MarketSettings(fee, company_limit, tax_rate, arbiters)


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


def _discord_ids(env: Mapping[str, str], name: str) -> frozenset[int]:
    raw = env.get(name, "").strip()
    return frozenset(_discord_id(value.strip(), name) for value in (raw.split(",") if raw else ()))
