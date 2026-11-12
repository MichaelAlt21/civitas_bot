from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.assets import ASSET_PAGE_SIZE, AssetError, AssetErrorReason, AssetService
from civitas.bot.assets import add_asset_commands
from civitas.bot.localization import CommandTranslator, message
from civitas.domain.assets import Asset, AssetKind, AssetPurchase, AssetQuote, AssetStatus
from civitas.domain.ledger import TransactionKind
from civitas.domain.market import Company

GUILD_ID = 123
BUYER_ID = 456
ISSUER_ID = 789
NOW = datetime(2026, 11, 12, tzinfo=UTC)
ASSET = Asset(
    id=10,
    guild_id=1,
    kind=AssetKind.OFFICE,
    name="Studio",
    purpose="Use the studio for company meetings.",
    price=200,
    status=AssetStatus.FOR_SALE,
    issued_by_citizen_id=2,
    issue_reason="Operator note",
    issue_key="issue",
    created_at=NOW,
)
COMPANY = Company(20, 1, 3, BUYER_ID, "Workshop", "workshop", "Woodworking", 150, "register", NOW)
RECEIPT = AssetPurchase(30, 1, ASSET.id, 3, None, 4, 200, "purchase", 5, NOW)


@pytest.fixture
def asset_commands():
    service = Mock(spec=AssetService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_asset_commands(tree, guild_scope=guild, asset_service=service, currency_symbol="CP")
    group = tree.get_command("assets", guild=guild)
    return service, tree, group


def interaction(*, actor=BUYER_ID, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        id=1001,
        locale=locale,
        guild_id=guild_id,
        user=SimpleNamespace(id=actor),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )


def private_reply(command):
    command.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    reply = command.followup.send.await_args
    assert reply.kwargs["ephemeral"] is True
    assert reply.kwargs["suppress_embeds"] is True
    assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    return reply.args[0]


@pytest.mark.asyncio
async def test_asset_commands_are_guild_only_and_localized(asset_commands):
    _, tree, assets = asset_commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    payload = await assets.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Каталог госимущества и владение"}
    commands = {command["name"]: command for command in payload["options"]}
    assert set(commands) == {"catalog", "mine", "buy", "staff"}
    for name in ("catalog", "mine"):
        page = next(option for option in commands[name]["options"] if option["name"] == "page")
        assert page["min_value"] == 1
        assert page["required"] is False
    issue = commands["staff"]["options"][0]
    kind = next(option for option in issue["options"] if option["name"] == "kind")
    labels = {choice["value"]: choice["name_localizations"] for choice in kind["choices"]}
    assert labels["office"] == {"ru": "Офис"}
    assert labels["equipment"] == {"ru": "Оборудование"}


@pytest.mark.asyncio
async def test_catalog_shows_sale_price_and_purpose_but_not_operator_notes(asset_commands):
    service, _, assets = asset_commands
    service.catalog.return_value = (ASSET,)
    service.asset_info.return_value = ASSET
    listing = interaction(locale=discord.Locale.german)
    detail = interaction()

    await assets.get_command("catalog").callback(listing)
    await assets.get_command("catalog").callback(detail, ASSET.id, 3)

    service.catalog.assert_awaited_once_with(GUILD_ID, BUYER_ID, page=1)
    service.asset_info.assert_awaited_once_with(GUILD_ID, BUYER_ID, ASSET.id)
    assert "Studio · Office · 200 CP" in private_reply(listing)
    assert ASSET.purpose in private_reply(detail)
    assert ASSET.issue_reason not in private_reply(detail)


@pytest.mark.asyncio
async def test_mine_reads_actor_ownership_and_hides_purchase_price(asset_commands):
    service, _, assets = asset_commands
    service.list_owned_assets.return_value = (
        replace(ASSET, status=AssetStatus.SOLD, owner_company_id=COMPANY.id),
    )
    command = interaction()

    await assets.get_command("mine").callback(command, COMPANY.id)

    service.list_owned_assets.assert_awaited_once_with(GUILD_ID, BUYER_ID, company_id=COMPANY.id, page=1)
    reply = private_reply(command)
    assert "Компания № 20" in reply
    assert "200 CP" not in reply


@pytest.mark.asyncio
async def test_catalog_and_mine_forward_page_and_show_next_page_hint(asset_commands):
    service, _, assets = asset_commands
    listings = tuple(replace(ASSET, id=asset_id) for asset_id in range(1, ASSET_PAGE_SIZE + 1))
    service.catalog.return_value = listings
    service.list_owned_assets.return_value = tuple(
        replace(asset, status=AssetStatus.SOLD, owner_company_id=COMPANY.id) for asset in listings
    )
    catalog, mine = interaction(), interaction(locale=discord.Locale.american_english)

    await assets.get_command("catalog").callback(catalog, page=2)
    await assets.get_command("mine").callback(mine, COMPANY.id, 3)

    service.catalog.assert_awaited_once_with(GUILD_ID, BUYER_ID, page=2)
    service.list_owned_assets.assert_awaited_once_with(GUILD_ID, BUYER_ID, company_id=COMPANY.id, page=3)
    assert "Страница 2" in private_reply(catalog)
    assert "Следующая страница: `/assets catalog page:3`" in private_reply(catalog)
    assert "Page 3" in private_reply(mine)
    assert "Next page: `/assets mine company_id:20 page:4`" in private_reply(mine)


@pytest.mark.asyncio
async def test_empty_later_page_and_invalid_page_are_distinct(asset_commands):
    service, _, assets = asset_commands
    service.catalog.side_effect = [(), AssetError(AssetErrorReason.INVALID_PAGE)]
    empty, invalid = interaction(), interaction()

    await assets.get_command("catalog").callback(empty, page=3)
    await assets.get_command("catalog").callback(invalid, page=0)

    assert private_reply(empty) == "На странице 3 нет объектов."
    assert private_reply(invalid) == "Укажите допустимый положительный номер страницы."


@pytest.mark.asyncio
async def test_staff_issue_forwards_audit_reason_and_reports_denial(asset_commands):
    service, _, assets = asset_commands
    service.issue_asset.side_effect = [ASSET, AssetError(AssetErrorReason.STAFF_REQUIRED)]
    issue = assets.get_command("staff").get_command("issue")
    authorized = interaction(actor=ISSUER_ID)
    denied = interaction()

    await issue.callback(authorized, "office", ASSET.name, ASSET.purpose, ASSET.price, ASSET.issue_reason)
    await issue.callback(denied, "office", ASSET.name, ASSET.purpose, ASSET.price, ASSET.issue_reason)

    call = service.issue_asset.await_args_list[0]
    assert call.args == (GUILD_ID, ISSUER_ID)
    assert call.kwargs == {
        "kind": AssetKind.OFFICE,
        "name": ASSET.name,
        "purpose": ASSET.purpose,
        "price": ASSET.price,
        "reason": ASSET.issue_reason,
        "idempotency_key": str(authorized.id),
    }
    assert "выставлен на продажу" in private_reply(authorized)
    assert "нужны полномочия" in private_reply(denied)


@pytest.mark.asyncio
@pytest.mark.parametrize("company", [None, COMPANY])
async def test_purchase_confirms_exact_quote_and_reuses_key_after_uncertain_result(asset_commands, company):
    service, _, assets = asset_commands
    company_id = company.id if company else None
    quote = AssetQuote(ASSET, company)
    service.quote_asset.return_value = quote
    service.buy_asset.side_effect = [ConnectionError(), replace(RECEIPT, buyer_company_id=company_id)]
    opening = interaction()

    await assets.get_command("buy").callback(opening, ASSET.id, company_id)

    opening.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    service.quote_asset.assert_awaited_once_with(GUILD_ID, BUYER_ID, ASSET.id, buyer_company_id=company_id)
    service.buy_asset.assert_not_awaited()
    preview = opening.edit_original_response.await_args.kwargs
    assert "за 200 CP" in preview["content"]
    owner = "Workshop (компания № 20)" if company else "вы"
    assert f"Собственник после покупки: {owner}" in preview["content"]
    view = preview["view"]
    assert not await view.interaction_check(interaction(actor=ISSUER_ID))
    assert not await view.interaction_check(interaction(guild_id=GUILD_ID + 1))
    first_click = interaction()
    assert await view.interaction_check(first_click)
    with pytest.raises(ConnectionError):
        await view.confirm.callback(first_click)
    retry = interaction()
    await view.confirm.callback(retry)

    call, retry_call = service.buy_asset.await_args_list
    assert call == retry_call
    assert call.args[:2] == (GUILD_ID, BUYER_ID)
    assert call.args[2] is quote
    assert call.kwargs["idempotency_key"]
    assert "куплен за 200 CP" in retry.edit_original_response.await_args.kwargs["content"]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_concurrent_sale_is_reported_without_purchase_receipt(asset_commands):
    service, _, assets = asset_commands
    service.quote_asset.return_value = AssetQuote(ASSET, None)
    service.buy_asset.side_effect = AssetError(AssetErrorReason.ASSET_SOLD)
    opening, click = interaction(), interaction()

    await assets.get_command("buy").callback(opening, ASSET.id)
    view = opening.edit_original_response.await_args.kwargs["view"]
    await view.confirm.callback(click)

    assert click.edit_original_response.await_args.kwargs["content"] == "Этот объект уже продан."
    assert view.is_finished()


def test_wallet_history_label_for_asset_purchase():
    key = f"wallet.history.{TransactionKind.ASSET_PURCHASE.value.lower()}"
    assert message(discord.Locale.russian, key) == "Покупка имущества"
    assert message(discord.Locale.american_english, key) == "Asset purchase"
