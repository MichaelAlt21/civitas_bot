from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock

import discord
import pytest
from discord import app_commands

from civitas.app.market import MarketError, MarketErrorReason, MarketService
from civitas.bot.localization import CommandTranslator, message
from civitas.bot.market import add_market_commands
from civitas.domain.ledger import TransactionKind
from civitas.domain.market import Company, MarketOrder, Offer, OrderQuote, OrderStatus

GUILD_ID = 123
BUYER_ID = 456
SELLER_ID = 789
ARBITER_ID = 790
NOW = datetime(2026, 11, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=48)
COMPANY = Company(1, 1, 2, SELLER_ID, "Workshop", "workshop", "Woodworking", 150, "register", NOW)
OFFER = Offer(2, 1, COMPANY.id, "Make a desk", "Deliver a custom desk.", 200, 48, "offer", NOW)
QUOTE = OrderQuote(OFFER, COMPANY, None, 1000, 20, DEADLINE)
ORDER = MarketOrder(
    id=3,
    guild_id=1,
    offer_id=OFFER.id,
    seller_company_id=COMPANY.id,
    seller_citizen_id=2,
    buyer_citizen_id=3,
    buyer_company_id=None,
    buyer_account_id=4,
    title=OFFER.title,
    terms=OFFER.terms,
    price=OFFER.price,
    tax_bp=1000,
    deadline=DEADLINE,
    created_at=NOW,
    status=OrderStatus.FUNDED,
    request_key="fund",
)


@pytest.fixture
def market_commands():
    service = Mock(spec=MarketService)
    type(service).registration_fee = PropertyMock(return_value=150)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_market_commands(tree, guild_scope=guild, market_service=service, currency_symbol="CP")
    return service, tree, tree.get_command("company", guild=guild), tree.get_command("market", guild=guild)


def interaction(*, actor=BUYER_ID, locale=discord.Locale.russian):
    return SimpleNamespace(
        id=1001,
        locale=locale,
        guild_id=GUILD_ID,
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
async def test_market_metadata_is_guild_only_and_localized(market_commands):
    _, tree, companies, market = market_commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    assert {command.name for command in companies.commands} == {"register", "info"}
    payload = await market.get_translated_payload(tree, CommandTranslator())
    assert payload["description_localizations"] == {"ru": "Рынок и заказы"}
    commands = {command["name"]: command for command in payload["options"]}
    assert set(commands) == {"list", "offer", "withdraw", "buy", "order"}
    withdrawal = commands["withdraw"]
    assert withdrawal["description_localizations"] == {"ru": "Снять предложение компании с рынка"}
    assert {option["name"] for option in withdrawal["options"] if option["required"]} == {"offer_id", "reason"}
    action_option = next(option for option in commands["order"]["options"] if option["name"] == "action")
    choices = {choice["value"]: choice["name_localizations"] for choice in action_option["choices"]}
    assert choices["resolve_refund"] == {"ru": "Разрешить спор возвратом"}


@pytest.mark.asyncio
async def test_company_registration_previews_fee_and_reuses_the_same_key_on_retry(market_commands):
    service, _, companies, _ = market_commands
    service.register_company.side_effect = [ConnectionError(), COMPANY]
    opening = interaction(actor=SELLER_ID)

    await companies.get_command("register").callback(opening, "Workshop", "Woodworking")

    opening.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    prompt = opening.edit_original_response.await_args.kwargs
    assert "Workshop" in prompt["content"]
    assert "150 CP" in prompt["content"]
    view = prompt["view"]
    assert not await view.interaction_check(interaction(actor=BUYER_ID))
    service.register_company.assert_not_awaited()
    first = interaction(actor=SELLER_ID)
    assert await view.interaction_check(first)
    with pytest.raises(ConnectionError) as raised:
        await view.confirm.callback(first)
    await view.on_error(first, raised.value, view.confirm)
    assert "Статус операции пока неизвестен" in first.followup.send.await_args.args[0]

    retry = interaction(actor=SELLER_ID)
    await view.confirm.callback(retry)

    call, repeated = service.register_company.await_args_list
    assert call == repeated
    assert call.args == (GUILD_ID, SELLER_ID)
    assert call.kwargs["expected_fee"] == 150
    assert call.kwargs["idempotency_key"]
    assert "зарегистрирована" in retry.edit_original_response.await_args.kwargs["content"]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_company_info_never_reads_balance_without_request_and_reports_denial(market_commands):
    service, _, companies, _ = market_commands
    service.company_info.return_value = COMPANY
    service.company_balance.side_effect = MarketError(MarketErrorReason.COMPANY_NOT_OWNED)
    public_card = interaction()
    denied_balance = interaction()

    await companies.get_command("info").callback(public_card, COMPANY.id)
    service.company_balance.assert_not_awaited()
    await companies.get_command("info").callback(denied_balance, COMPANY.id, True)

    assert "Деятельность: Woodworking" in private_reply(public_card)
    service.company_balance.assert_awaited_once_with(GUILD_ID, BUYER_ID, COMPANY.id)
    assert private_reply(denied_balance) == "Это действие доступно владельцу компании."


@pytest.mark.asyncio
async def test_offer_publish_uses_discord_request_key_and_lists_offer(market_commands):
    service, _, _, market = market_commands
    service.publish_offer.return_value = OFFER
    service.list_offers.return_value = (OFFER,)
    publish, browse = interaction(actor=SELLER_ID), interaction()

    await market.get_command("offer").callback(publish, COMPANY.id, OFFER.title, OFFER.terms, 200, 48)
    await market.get_command("list").callback(browse)

    service.publish_offer.assert_awaited_once_with(
        GUILD_ID, SELLER_ID, company_id=COMPANY.id, title=OFFER.title, terms=OFFER.terms,
        price=200, delivery_hours=48, idempotency_key=str(publish.id),
    )
    assert "опубликовано" in private_reply(publish)
    assert "Make a desk · 200 CP" in private_reply(browse)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale, expected", [
    (discord.Locale.russian, "Предложение № 2 снято с рынка. Текущие заказы можно завершить."),
    (discord.Locale.american_english, "Offer #2 was withdrawn. Existing orders can still be completed."),
])
async def test_offer_withdrawal_is_private_and_does_not_echo_reason(market_commands, locale, expected):
    service, _, _, market = market_commands
    service.withdraw_offer.return_value = SimpleNamespace(offer_id=OFFER.id)
    request = interaction(actor=SELLER_ID, locale=locale)
    reason = "Internal reason @everyone"

    await market.get_command("withdraw").callback(request, OFFER.id, reason)

    service.withdraw_offer.assert_awaited_once_with(GUILD_ID, SELLER_ID, OFFER.id, reason=reason)
    assert private_reply(request) == expected


@pytest.mark.asyncio
async def test_offer_withdrawal_denial_stays_private(market_commands):
    service, _, _, market = market_commands
    service.withdraw_offer.side_effect = MarketError(MarketErrorReason.COMPANY_NOT_OWNED)
    request = interaction(actor=BUYER_ID)

    await market.get_command("withdraw").callback(request, OFFER.id, "test reason")

    service.withdraw_offer.assert_awaited_once_with(GUILD_ID, BUYER_ID, OFFER.id, reason="test reason")
    assert private_reply(request) == "Это действие доступно владельцу компании."


@pytest.mark.asyncio
@pytest.mark.parametrize("buyer_company_id", [None, 9])
async def test_buy_previews_and_confirms_exact_quote(market_commands, buyer_company_id):
    service, _, _, market = market_commands
    quote = replace(QUOTE, buyer_company_id=buyer_company_id)
    service.quote_order.return_value = quote
    service.fund_order.return_value = ORDER
    opening = interaction(locale=discord.Locale.british_english)

    await market.get_command("buy").callback(opening, OFFER.id, buyer_company_id)

    opening.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    service.quote_order.assert_awaited_once_with(GUILD_ID, BUYER_ID, OFFER.id, buyer_company_id=buyer_company_id)
    service.fund_order.assert_not_awaited()
    prompt = opening.edit_original_response.await_args.kwargs
    assert "Price held in escrow: 200 CP" in prompt["content"]
    assert "Tax included: 20 CP (10%)" in prompt["content"]
    assert discord.utils.format_dt(DEADLINE, style="f") in prompt["content"]
    assert OFFER.terms in prompt["content"]
    source = "your personal wallet" if buyer_company_id is None else "company #9"
    assert source in prompt["content"]

    click = interaction()
    await prompt["view"].confirm.callback(click)

    service.fund_order.assert_awaited_once()
    assert service.fund_order.await_args.args[:2] == (GUILD_ID, BUYER_ID)
    assert service.fund_order.await_args.args[2] is quote
    assert service.fund_order.await_args.kwargs["idempotency_key"]
    assert "оплачено в эскроу" in click.edit_original_response.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_changed_quote_requires_new_confirmation(market_commands):
    service, _, _, market = market_commands
    service.quote_order.return_value = QUOTE
    service.fund_order.side_effect = MarketError(MarketErrorReason.QUOTE_CHANGED)
    opening, click = interaction(), interaction()

    await market.get_command("buy").callback(opening, OFFER.id)
    view = opening.edit_original_response.await_args.kwargs["view"]
    await view.confirm.callback(click)

    assert "Условия в подтверждении устарели" in click.edit_original_response.await_args.kwargs["content"]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_existing_purchase_confirmation_reports_offer_withdrawal(market_commands):
    service, _, _, market = market_commands
    service.quote_order.return_value = QUOTE
    service.fund_order.side_effect = MarketError(MarketErrorReason.OFFER_WITHDRAWN)
    opening, click = interaction(), interaction()

    await market.get_command("buy").callback(opening, OFFER.id)
    view = opening.edit_original_response.await_args.kwargs["view"]
    await view.confirm.callback(click)

    assert click.edit_original_response.await_args.kwargs["content"] == "Это предложение снято с рынка."
    assert view.is_finished()


@pytest.mark.asyncio
async def test_order_list_hides_dispute_reason_and_detail_is_private(market_commands):
    service, _, _, market = market_commands
    disputed = replace(ORDER, status=OrderStatus.DISPUTED, dispute_reason="Private details")
    service.list_orders.return_value = (disputed,)
    service.get_order.return_value = disputed
    listing, detail = interaction(actor=SELLER_ID), interaction(actor=SELLER_ID)

    await market.get_command("order").callback(listing)
    await market.get_command("order").callback(detail, ORDER.id)

    service.list_orders.assert_awaited_once_with(GUILD_ID, SELLER_ID)
    service.get_order.assert_awaited_once_with(GUILD_ID, SELLER_ID, ORDER.id)
    assert "Private details" not in private_reply(listing)
    assert "Private details" in private_reply(detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "method", "kwargs", "status"), [
    ("deliver", "deliver_order", {}, OrderStatus.DELIVERED),
    ("accept", "accept_order", {}, OrderStatus.COMPLETED),
    ("cancel", "cancel_order", {}, OrderStatus.REFUNDED),
    ("dispute", "dispute_order", {"reason": "Reason"}, OrderStatus.DISPUTED),
    ("resolve_refund", "resolve_order", {"settle": False, "reason": "Reason"}, OrderStatus.REFUNDED),
    ("resolve_settle", "resolve_order", {"settle": True, "reason": "Reason"}, OrderStatus.COMPLETED),
])
async def test_order_routes_private_actions_through_service(market_commands, action, method, kwargs, status):
    service, _, _, market = market_commands
    action_service = getattr(service, method)
    action_service.return_value = replace(ORDER, status=status)
    request = interaction(actor=ARBITER_ID)

    await market.get_command("order").callback(request, ORDER.id, action, kwargs.get("reason"))

    action_service.assert_awaited_once_with(GUILD_ID, ARBITER_ID, ORDER.id, **kwargs)
    reply = private_reply(request)
    if status == OrderStatus.COMPLETED:
        assert "Продавцу перечислено 180 CP, в казну — 20 CP." in reply


@pytest.mark.parametrize("kind", [
    TransactionKind.COMPANY_FEE, TransactionKind.ORDER_FUND, TransactionKind.ORDER_SETTLE, TransactionKind.ORDER_REFUND,
])
def test_wallet_history_labels_market_transactions(kind):
    key = f"wallet.history.{kind.value.lower()}"
    assert message(discord.Locale.russian, key) != message(discord.Locale.american_english, key)
