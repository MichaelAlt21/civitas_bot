from datetime import UTC

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


def test_citizen_role_is_optional_and_accepts_a_snowflake(env: dict[str, str]) -> None:
    assert load_settings(env).citizen_role_id is None
    env["CIVITAS_CITIZEN_ROLE_ID"] = "2000"

    assert load_settings(env).citizen_role_id == 2000


def test_wallet_requires_explicit_initial_amounts(env: dict[str, str]) -> None:
    assert load_settings(env).wallet is None
    env["CIVITAS_STARTER_GRANT"] = "250"
    with pytest.raises(ConfigurationError, match="CIVITAS_TREASURY_GENESIS"):
        load_settings(env)

    env["CIVITAS_TREASURY_GENESIS"] = "8000"
    wallet = load_settings(env).wallet
    assert wallet is not None
    assert (wallet.starter_grant, wallet.treasury_genesis, wallet.currency_symbol) == (250, 8000, "₵")


def test_jobs_are_opt_in_and_use_validated_budget_settings(env: dict[str, str]) -> None:
    assert load_settings(env).jobs is None
    env.update(
        CIVITAS_NPC_REVIEWER_IDS="12,34",
        CIVITAS_BUDGET_WEEKLY_LIMIT="800",
        CIVITAS_NPC_WEEKLY_LIMIT="300",
        CIVITAS_NPC_PERSONAL_WEEKLY_LIMIT="100",
        CIVITAS_NPC_REWARD_MIN="10",
        CIVITAS_NPC_REWARD_MAX="40",
    )
    with pytest.raises(ConfigurationError, match="CIVITAS_TREASURY_GENESIS"):
        load_settings(env)

    env.update(CIVITAS_STARTER_GRANT="250", CIVITAS_TREASURY_GENESIS="8000")
    jobs = load_settings(env).jobs
    assert jobs is not None
    assert jobs.reviewer_ids == {12, 34}
    assert (jobs.budget_weekly_limit, jobs.npc_weekly_limit, jobs.personal_weekly_limit) == (800, 300, 100)
    assert jobs.budget_timezone is UTC

    env["CIVITAS_NPC_REWARD_MAX"] = "9"
    with pytest.raises(ConfigurationError, match="лимитов NPC"):
        load_settings(env)


def test_market_rules_are_explicit_and_require_the_wallet(env: dict[str, str]) -> None:
    assert load_settings(env).market is None
    env.update(CIVITAS_COMPANY_FEE="150", CIVITAS_COMPANY_LIMIT="1", CIVITAS_SALES_TAX_BP="1000")
    with pytest.raises(ConfigurationError, match="CIVITAS_STARTER_GRANT"):
        load_settings(env)

    env.update(CIVITAS_STARTER_GRANT="250", CIVITAS_TREASURY_GENESIS="8000", CIVITAS_MARKET_ARBITER_IDS="12")
    market = load_settings(env).market
    assert market is not None
    assert (market.company_fee, market.company_limit_per_owner, market.sales_tax_basis_points) == (150, 1, 1000)
    assert market.arbiter_ids == {12}

    env["CIVITAS_SALES_TAX_BP"] = "10001"
    with pytest.raises(ConfigurationError, match="CIVITAS_SALES_TAX_BP"):
        load_settings(env)


def test_state_asset_issuers_are_explicit_and_require_the_wallet(env: dict[str, str]) -> None:
    assert load_settings(env).asset_issuer_ids == frozenset()
    env["CIVITAS_ASSET_ISSUER_IDS"] = " 12, 34 "
    with pytest.raises(ConfigurationError, match="CIVITAS_STARTER_GRANT"):
        load_settings(env)

    env.update(CIVITAS_STARTER_GRANT="250", CIVITAS_TREASURY_GENESIS="8000")
    assert load_settings(env).asset_issuer_ids == {12, 34}


def test_state_contracts_share_budget_settings_without_enabling_npc(env: dict[str, str]) -> None:
    env.update(
        CIVITAS_TREASURY_INITIATOR_IDS="12",
        CIVITAS_TREASURY_APPROVER_IDS="34",
        CIVITAS_BUDGET_WEEKLY_LIMIT="800",
        CIVITAS_CONTRACT_WEEKLY_LIMIT="250",
    )
    with pytest.raises(ConfigurationError, match="CIVITAS_STARTER_GRANT"):
        load_settings(env)
    env.update(CIVITAS_STARTER_GRANT="250", CIVITAS_TREASURY_GENESIS="8000")
    settings = load_settings(env)
    assert settings.jobs is None
    treasury = settings.treasury
    assert treasury is not None
    assert treasury.initiator_ids == {12} and treasury.approver_ids == {34}
    assert (treasury.budget_weekly_limit, treasury.contract_weekly_limit) == (800, 250)
    env["CIVITAS_CONTRACT_WEEKLY_LIMIT"] = "801"
    with pytest.raises(ConfigurationError, match="CIVITAS_CONTRACT_WEEKLY_LIMIT"):
        load_settings(env)


def test_report_publication_accepts_a_channel_snowflake_and_uses_budget_timezone(env: dict[str, str]) -> None:
    assert load_settings(env).treasury_report_channel_id is None
    env.update(CIVITAS_TREASURY_REPORT_CHANNEL_ID="18446744073709551615", CIVITAS_BUDGET_TIMEZONE="Europe/Moscow")
    settings = load_settings(env)
    assert settings.treasury_report_channel_id == 18446744073709551615
    assert str(settings.report_timezone) == "Europe/Moscow"
    env["CIVITAS_TREASURY_REPORT_CHANNEL_ID"] = "#budget"
    with pytest.raises(ConfigurationError, match="CIVITAS_TREASURY_REPORT_CHANNEL_ID"):
        load_settings(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CIVITAS_DISCORD_GUILD_ID", "0"),
        ("CIVITAS_DISCORD_GUILD_ID", "18446744073709551616"),
        ("CIVITAS_DISCORD_TOKEN", ""),
        ("CIVITAS_ALLOWED_USER_IDS", "12,,34"),
        ("CIVITAS_CITIZEN_ROLE_ID", "Citizen"),
        ("CIVITAS_CITIZEN_ROLE_ID", "1000"),
        ("CIVITAS_STARTER_GRANT", "-1"),
        ("CIVITAS_STARTER_GRANT", "9223372036854775808"),
        ("CIVITAS_DATABASE_URL", "postgresql+psycopg://localhost"),
        ("CIVITAS_DATABASE_URL", "sqlite:///civitas.db"),
    ],
)
def test_invalid_setting_names_variable(env: dict[str, str], name: str, value: str) -> None:
    env[name] = value

    with pytest.raises(ConfigurationError, match=name):
        load_settings(env)
