"""Discord commands for companies, market offers, and private order actions."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.market import MARKET_PAGE_SIZE, MarketError, MarketService
from civitas.domain.ledger import LedgerError
from civitas.domain.market import MAX_DELIVERY_HOURS, Company, MarketOrder, OrderStatus

from .confirmation import OperationConfirmation
from .localization import command_description, message
from .responses import send_private_lines as _reply

_MARKET_ERRORS = (CitizenshipAccessError, MarketError, LedgerError)
_ORDER_ACTIONS = (
    "view", "deliver", "accept", "cancel", "dispute", "resolve_refund", "resolve_settle"
)


def _error_reply(locale: discord.Locale, error: CitizenshipAccessError | MarketError | LedgerError) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    if isinstance(error, MarketError):
        return message(locale, f"market.error.{error.reason.value}")
    return message(locale, f"wallet.error.{error.reason.value}")


@asynccontextmanager
async def _market_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except _MARKET_ERRORS as error:
        await _reply(interaction, _error_reply(interaction.locale, error))


async def _show_page(
    interaction: discord.Interaction,
    *,
    page: int,
    command: str,
    title_key: str,
    empty_key: str,
    rows: list[str],
) -> None:
    locale = interaction.locale
    if not rows:
        if page == 1:
            reply = message(locale, empty_key)
        else:
            reply = message(locale, "market.page.empty", page=str(page))
        await _reply(interaction, reply)
        return

    parts = [message(locale, "market.page.title", title=message(locale, title_key), page=str(page)), *rows]
    if len(rows) == MARKET_PAGE_SIZE:
        parts.append(message(locale, "market.page.next", command=command, page=str(page + 1)))
    await _reply(interaction, *parts)


def _text(value: str) -> str:
    return discord.utils.escape_markdown(value)


def _label(value: str) -> str:
    return _text(" ".join(value.split()))


def _percent(locale: discord.Locale, basis_points: int) -> str:
    whole, hundredths = divmod(basis_points, 100)
    if not hundredths:
        return str(whole)
    separator = "," if locale == discord.Locale.russian else "."
    return f"{whole}{separator}{hundredths:02d}".rstrip("0")


def _company_card(locale: discord.Locale, company: Company) -> str:
    return message(
        locale, "company.info.card", id=str(company.id), name=_label(company.name), activity=_text(company.activity)
    )


def _order_row(locale: discord.Locale, order: MarketOrder, currency_symbol: str) -> str:
    return message(
        locale,
        "market.order.row",
        id=str(order.id),
        title=_label(order.title),
        status=message(locale, f"market.status.{order.status.value.lower()}"),
        price=f"{order.price} {currency_symbol}",
    )


def _order_details(locale: discord.Locale, order: MarketOrder, currency_symbol: str) -> list[str]:
    parts = [
        _order_row(locale, order, currency_symbol),
        message(locale, "market.order.seller", id=str(order.seller_company_id)),
        message(locale, "market.order.deadline", deadline=discord.utils.format_dt(order.deadline, style="f")),
        message(locale, "market.order.tax", tax=f"{order.tax} {currency_symbol}", rate=_percent(locale, order.tax_bp)),
    ]
    if order.status == OrderStatus.COMPLETED:
        parts.append(message(
            locale,
            "market.order.settlement",
            seller=f"{order.price - order.tax} {currency_symbol}",
            treasury=f"{order.tax} {currency_symbol}",
        ))
    parts.append(_text(order.terms))
    if order.dispute_reason:
        parts.append(message(locale, "market.order.dispute_reason", reason=_text(order.dispute_reason)))
    if order.close_reason:
        parts.append(message(locale, "market.order.close_reason", reason=_text(order.close_reason)))
    return parts


def _order_choices() -> list[app_commands.Choice[str]]:
    choices = []
    for action in _ORDER_ACTIONS:
        key = f"market.action.{action}"
        name = app_commands.locale_str(message(None, key), key=key)
        choices.append(app_commands.Choice(name=name, value=action))
    return choices


def add_market_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    market_service: MarketService,
    currency_symbol: str = "₵",
) -> None:
    companies = app_commands.Group(name="company", description=command_description("company"))
    market = app_commands.Group(name="market", description=command_description("market"))
    tree.add_command(companies, guild=guild_scope)
    tree.add_command(market, guild=guild_scope)

    @companies.command(name="register", description=command_description("company.register"))
    @app_commands.describe(name=command_description("company.name"), activity=command_description("company.activity"))
    async def register(
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 80],
        activity: app_commands.Range[str, 1, 200],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        fee = market_service.registration_fee

        async def commit(click: discord.Interaction, key: str) -> str:
            try:
                company = await market_service.register_company(
                    click.guild_id, click.user.id, name=name, activity=activity,
                    idempotency_key=key, expected_fee=fee,
                )
            except _MARKET_ERRORS as error:
                return _error_reply(click.locale, error)
            return message(click.locale, "company.register.done", id=str(company.id), name=_label(company.name))

        confirmation = OperationConfirmation(
            guild_id=guild_scope.id, actor_id=interaction.user.id, locale=interaction.locale, commit=commit
        )
        await confirmation.show(
            interaction,
            message(interaction.locale, "company.register.preview", name=_label(name), fee=f"{fee} {currency_symbol}"),
        )

    @companies.command(name="info", description=command_description("company.info"))
    @app_commands.describe(company_id=command_description("company.id"), balance=command_description("company.balance"))
    async def info(
        interaction: discord.Interaction,
        company_id: app_commands.Range[int, 1] | None = None,
        balance: bool = False,
    ) -> None:
        locale = interaction.locale
        async with _market_response(interaction):
            if company_id is None:
                if balance:
                    await _reply(interaction, message(locale, "company.info.id_required"))
                    return
                records = await market_service.list_owned_companies(interaction.guild_id, interaction.user.id)
                if not records:
                    await _reply(interaction, message(locale, "company.list.empty"))
                    return
                await _reply(interaction, message(locale, "company.list.title"), *(_company_card(locale, c) for c in records))
                return

            company = await market_service.company_info(interaction.guild_id, interaction.user.id, company_id)
            parts = [_company_card(locale, company)]
            if balance:
                amount = await market_service.company_balance(interaction.guild_id, interaction.user.id, company_id)
                parts.append(message(locale, "company.info.balance", amount=f"{amount} {currency_symbol}"))
            await _reply(interaction, *parts)

    @market.command(name="list", description=command_description("market.list"))
    @app_commands.describe(page=command_description("market.page"))
    async def list_offers(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
        locale = interaction.locale
        async with _market_response(interaction):
            offers = await market_service.list_offers(interaction.guild_id, interaction.user.id, page=page)
            await _show_page(
                interaction,
                page=page,
                command="/market list",
                title_key="market.list.title",
                empty_key="market.list.empty",
                rows=[message(
                    locale,
                    "market.list.row",
                    id=str(offer.id),
                    title=_label(offer.title),
                    price=f"{offer.price} {currency_symbol}",
                    seller=str(offer.company_id),
                ) for offer in offers],
            )

    @market.command(name="offer", description=command_description("market.offer"))
    @app_commands.describe(
        company_id=command_description("company.id"),
        title=command_description("market.title"),
        terms=command_description("market.terms"),
        price=command_description("market.price"),
        delivery_hours=command_description("market.delivery_hours"),
    )
    async def publish_offer(
        interaction: discord.Interaction,
        company_id: app_commands.Range[int, 1],
        title: app_commands.Range[str, 1, 80],
        terms: app_commands.Range[str, 1, 500],
        price: app_commands.Range[int, 1],
        delivery_hours: app_commands.Range[int, 1, MAX_DELIVERY_HOURS],
    ) -> None:
        async with _market_response(interaction):
            offer = await market_service.publish_offer(
                interaction.guild_id, interaction.user.id, company_id=company_id, title=title, terms=terms,
                price=price, delivery_hours=delivery_hours, idempotency_key=str(interaction.id),
            )
            await _reply(interaction, message(
                interaction.locale, "market.offer.published", id=str(offer.id), title=_label(offer.title)
            ))

    @market.command(name="withdraw", description=command_description("market.withdraw"))
    @app_commands.describe(
        offer_id=command_description("market.offer_id"), reason=command_description("market.withdraw.reason"),
    )
    async def withdraw_offer(
        interaction: discord.Interaction, offer_id: app_commands.Range[int, 1], reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _market_response(interaction):
            withdrawal = await market_service.withdraw_offer(
                interaction.guild_id, interaction.user.id, offer_id, reason=reason,
            )
            await _reply(interaction, message(interaction.locale, "market.offer.withdrawn", id=str(withdrawal.offer_id)))

    @market.command(name="buy", description=command_description("market.buy"))
    @app_commands.describe(
        offer_id=command_description("market.offer_id"), buyer_company_id=command_description("market.buyer_company_id")
    )
    async def buy(
        interaction: discord.Interaction,
        offer_id: app_commands.Range[int, 1],
        buyer_company_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        locale = interaction.locale
        async with _market_response(interaction):
            quote = await market_service.quote_order(
                interaction.guild_id, interaction.user.id, offer_id, buyer_company_id=buyer_company_id
            )
            prompt = message(
                locale, "market.buy.preview", title=_label(quote.offer.title), seller=_label(quote.seller.name),
                price=f"{quote.offer.price} {currency_symbol}", tax=f"{quote.tax} {currency_symbol}",
                rate=_percent(locale, quote.tax_bp), deadline=discord.utils.format_dt(quote.deadline, style="f"),
                terms=_text(quote.offer.terms),
            )
            if quote.buyer_company_id is None:
                source = message(locale, "market.buy.source.personal")
            else:
                source = message(locale, "market.buy.source.company", id=str(quote.buyer_company_id))

            async def commit(click: discord.Interaction, key: str) -> str:
                try:
                    order = await market_service.fund_order(click.guild_id, click.user.id, quote, idempotency_key=key)
                except _MARKET_ERRORS as error:
                    return _error_reply(click.locale, error)
                return _order_row(click.locale, order, currency_symbol)

            confirmation = OperationConfirmation(
                guild_id=guild_scope.id, actor_id=interaction.user.id, locale=locale, commit=commit
            )
            await confirmation.show(interaction, f"{prompt}\n\n{source}")

    @market.command(name="order", description=command_description("market.order"))
    @app_commands.choices(action=_order_choices())
    @app_commands.describe(
        order_id=command_description("market.order_id"), action=command_description("market.action"),
        reason=command_description("market.reason"), page=command_description("market.page"),
        disputes_only=command_description("market.disputes_only"),
    )
    async def order_command(
        interaction: discord.Interaction,
        order_id: app_commands.Range[int, 1] | None = None,
        action: str = "view",
        reason: app_commands.Range[str, 1, 200] | None = None,
        page: app_commands.Range[int, 1] | None = None,
        disputes_only: bool | None = None,
    ) -> None:
        locale = interaction.locale
        guild_id, user_id = interaction.guild_id, interaction.user.id
        async with _market_response(interaction):
            listing = order_id is None and action == "view"
            if not listing and (page is not None or disputes_only is not None):
                await _reply(interaction, message(locale, "market.order.list_options_only"))
                return
            if order_id is None:
                if not listing:
                    await _reply(interaction, message(locale, "market.order.id_required"))
                    return
                page_number = 1 if page is None else page
                orders = await market_service.list_orders(
                    guild_id, user_id, page=page_number, disputes_only=bool(disputes_only)
                )
                title_key = "market.order.disputes_title" if disputes_only else "market.order.list"
                empty_key = "market.order.disputes_empty" if disputes_only else "market.order.empty"
                command = "/market order disputes_only:true" if disputes_only else "/market order"
                await _show_page(
                    interaction,
                    page=page_number,
                    command=command,
                    title_key=title_key,
                    empty_key=empty_key,
                    rows=[_order_row(locale, order, currency_symbol) for order in orders],
                )
                return

            standard_actions = {
                "view": market_service.get_order,
                "deliver": market_service.deliver_order,
                "accept": market_service.accept_order,
                "cancel": market_service.cancel_order,
            }
            if action in standard_actions:
                order = await standard_actions[action](guild_id, user_id, order_id)
            elif not reason:
                await _reply(interaction, message(locale, "market.order.reason_required"))
                return
            elif action == "dispute":
                order = await market_service.dispute_order(guild_id, user_id, order_id, reason=reason)
            elif action in ("resolve_refund", "resolve_settle"):
                order = await market_service.resolve_order(
                    guild_id, user_id, order_id, settle=action == "resolve_settle", reason=reason
                )
            else:
                raise ValueError("Unknown Discord order action")

            await _reply(interaction, *_order_details(locale, order, currency_symbol))
