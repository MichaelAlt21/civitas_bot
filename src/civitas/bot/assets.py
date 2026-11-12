"""Discord commands for the primary state asset catalog."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from discord import app_commands

from civitas.app.assets import ASSET_PAGE_SIZE, AssetError, AssetService
from civitas.app.citizens import CitizenshipAccessError
from civitas.domain.assets import Asset, AssetKind
from civitas.domain.ledger import LedgerError
from civitas.domain.market import Company

from .confirmation import OperationConfirmation
from .localization import command_description, message
from .responses import send_private_lines

_ASSET_ERRORS = (CitizenshipAccessError, AssetError, LedgerError)


def _error_reply(locale: discord.Locale, error: CitizenshipAccessError | AssetError | LedgerError) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    if isinstance(error, AssetError):
        return message(locale, f"assets.error.{error.reason.value}")
    return message(locale, f"wallet.error.{error.reason.value}")


@asynccontextmanager
async def _asset_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except _ASSET_ERRORS as error:
        await send_private_lines(interaction, _error_reply(interaction.locale, error))


def _label(value: str) -> str:
    return discord.utils.escape_markdown(" ".join(value.split()))


def _catalog_row(locale: discord.Locale, asset: Asset, currency_symbol: str) -> str:
    return message(
        locale,
        "assets.catalog.row",
        id=str(asset.id),
        name=_label(asset.name),
        kind=message(locale, f"assets.kind.{asset.kind.value}"),
        price=f"{asset.price} {currency_symbol}",
    )


def _owned_row(locale: discord.Locale, asset: Asset) -> str:
    if asset.owner_company_id is None:
        owner = message(locale, "assets.owner.personal")
    else:
        owner = message(locale, "assets.owner.company", id=str(asset.owner_company_id))
    return message(
        locale,
        "assets.mine.row",
        id=str(asset.id),
        name=_label(asset.name),
        kind=message(locale, f"assets.kind.{asset.kind.value}"),
        owner=owner,
    )


def _owner_label(locale: discord.Locale, company: Company | None) -> str:
    if company is None:
        return message(locale, "assets.buy.personal")
    return message(locale, "assets.buy.company", id=str(company.id), name=_label(company.name))


def _kind_choices() -> list[app_commands.Choice[str]]:
    choices = []
    for kind in AssetKind:
        key = f"assets.kind.{kind.value}"
        name = app_commands.locale_str(message(None, key), key=key)
        choices.append(app_commands.Choice(name=name, value=kind.value))
    return choices


async def _show_page(
    interaction: discord.Interaction,
    *,
    section: str,
    page: int,
    command: str,
    rows: list[str],
    hint: str | None = None,
) -> None:
    locale = interaction.locale
    if not rows:
        if page == 1:
            reply = message(locale, f"assets.{section}.empty")
        else:
            reply = message(locale, "assets.page.empty", page=str(page))
        await send_private_lines(interaction, reply)
        return

    title = message(locale, f"assets.{section}.title")
    lines = [message(locale, "assets.page.title", title=title, page=str(page)), *rows]
    if hint:
        lines.append(hint)
    if len(rows) == ASSET_PAGE_SIZE:
        lines.append(message(locale, "assets.page.next", command=command, page=str(page + 1)))
    await send_private_lines(interaction, *lines)


def add_asset_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    asset_service: AssetService,
    currency_symbol: str = "₵",
) -> None:
    assets = app_commands.Group(name="assets", description=command_description("assets"))
    staff = app_commands.Group(name="staff", description=command_description("assets.staff"))
    assets.add_command(staff)
    tree.add_command(assets, guild=guild_scope)

    @assets.command(name="catalog", description=command_description("assets.catalog"))
    @app_commands.describe(
        asset_id=command_description("assets.asset_id"), page=command_description("assets.page")
    )
    async def catalog(
        interaction: discord.Interaction,
        asset_id: app_commands.Range[int, 1] | None = None,
        page: app_commands.Range[int, 1] = 1,
    ) -> None:
        locale = interaction.locale
        async with _asset_response(interaction):
            if asset_id is not None:
                asset = await asset_service.asset_info(interaction.guild_id, interaction.user.id, asset_id)
                await send_private_lines(
                    interaction,
                    _catalog_row(locale, asset, currency_symbol),
                    discord.utils.escape_markdown(asset.purpose),
                )
                return

            listings = await asset_service.catalog(interaction.guild_id, interaction.user.id, page=page)
            await _show_page(
                interaction,
                section="catalog",
                page=page,
                command="/assets catalog",
                rows=[_catalog_row(locale, asset, currency_symbol) for asset in listings],
                hint=message(locale, "assets.catalog.details"),
            )

    @assets.command(name="mine", description=command_description("assets.mine"))
    @app_commands.describe(
        company_id=command_description("assets.company_id"), page=command_description("assets.page")
    )
    async def mine(
        interaction: discord.Interaction,
        company_id: app_commands.Range[int, 1] | None = None,
        page: app_commands.Range[int, 1] = 1,
    ) -> None:
        locale = interaction.locale
        async with _asset_response(interaction):
            owned = await asset_service.list_owned_assets(
                interaction.guild_id, interaction.user.id, company_id=company_id, page=page
            )
            command = "/assets mine" if company_id is None else f"/assets mine company_id:{company_id}"
            await _show_page(
                interaction,
                section="mine",
                page=page,
                command=command,
                rows=[_owned_row(locale, asset) for asset in owned],
            )

    @assets.command(name="buy", description=command_description("assets.buy"))
    @app_commands.describe(
        asset_id=command_description("assets.asset_id"),
        buyer_company_id=command_description("assets.buyer_company_id"),
    )
    async def buy(
        interaction: discord.Interaction,
        asset_id: app_commands.Range[int, 1],
        buyer_company_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        locale = interaction.locale
        async with _asset_response(interaction):
            quote = await asset_service.quote_asset(
                interaction.guild_id, interaction.user.id, asset_id, buyer_company_id=buyer_company_id
            )
            asset = quote.asset
            prompt = message(
                locale,
                "assets.buy.preview",
                id=str(asset.id),
                name=_label(asset.name),
                price=f"{asset.price} {currency_symbol}",
                owner=_owner_label(locale, quote.buyer_company),
            )

            async def commit(click: discord.Interaction, key: str) -> str:
                try:
                    receipt = await asset_service.buy_asset(
                        click.guild_id, click.user.id, quote, idempotency_key=key
                    )
                except _ASSET_ERRORS as error:
                    return _error_reply(click.locale, error)
                return message(
                    click.locale,
                    "assets.buy.done",
                    id=str(receipt.asset_id),
                    price=f"{receipt.price} {currency_symbol}",
                    owner=_owner_label(click.locale, quote.buyer_company),
                )

            confirmation = OperationConfirmation(
                guild_id=guild_scope.id, actor_id=interaction.user.id, locale=locale, commit=commit
            )
            await confirmation.show(interaction, f"{prompt}\n\n{discord.utils.escape_markdown(asset.purpose)}")

    @staff.command(name="issue", description=command_description("assets.staff.issue"))
    @app_commands.choices(kind=_kind_choices())
    @app_commands.describe(
        kind=command_description("assets.kind"),
        name=command_description("assets.name"),
        purpose=command_description("assets.purpose"),
        price=command_description("assets.price"),
        reason=command_description("assets.reason"),
    )
    async def issue(
        interaction: discord.Interaction,
        kind: str,
        name: app_commands.Range[str, 1, 80],
        purpose: app_commands.Range[str, 1, 300],
        price: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _asset_response(interaction):
            asset = await asset_service.issue_asset(
                interaction.guild_id,
                interaction.user.id,
                kind=AssetKind(kind),
                name=name,
                purpose=purpose,
                price=price,
                reason=reason,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(
                interaction, message(interaction.locale, "assets.issue.done", id=str(asset.id))
            )
