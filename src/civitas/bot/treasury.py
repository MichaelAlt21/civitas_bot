"""Guild-scoped treasury and state contract commands."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import discord
from discord import app_commands

from civitas.app.citizens import CitizenshipAccessError
from civitas.app.treasury import TreasuryError, TreasuryService
from civitas.domain.ledger import LedgerError, LedgerErrorReason
from civitas.domain.treasury import ContractBudget, StateContract

from .confirmation import OperationConfirmation
from .localization import command_description, message
from .responses import send_private_lines

_TREASURY_ERRORS = (CitizenshipAccessError, TreasuryError, LedgerError)


def _error_reply(locale: discord.Locale, error: CitizenshipAccessError | TreasuryError | LedgerError) -> str:
    if isinstance(error, CitizenshipAccessError):
        return message(locale, f"access.{error.reason.value}")
    if isinstance(error, TreasuryError):
        return message(locale, f"treasury.error.{error.reason.value}")
    if error.reason == LedgerErrorReason.INSUFFICIENT_FUNDS:
        return message(locale, "treasury.error.treasury_insufficient_funds")
    return message(locale, f"wallet.error.{error.reason.value}")


@asynccontextmanager
async def _treasury_response(interaction: discord.Interaction) -> AsyncIterator[None]:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        yield
    except _TREASURY_ERRORS as error:
        await send_private_lines(interaction, _error_reply(interaction.locale, error))


def _budget_rows(locale: discord.Locale, budget: ContractBudget, currency_symbol: str) -> tuple[str, ...]:
    return (
        message(
            locale,
            "treasury.budget.interval",
            start=discord.utils.format_dt(budget.start_at, style="f"),
            end=discord.utils.format_dt(budget.end_at, style="f"),
        ),
        message(locale, "treasury.budget.active", date=discord.utils.format_dt(budget.activated_at, style="f")),
        message(
            locale,
            "treasury.budget.overall",
            cap=f"{budget.overall_cap} {currency_symbol}",
            spent=f"{budget.spent_total} {currency_symbol}",
            reserved=f"{budget.reserved_total} {currency_symbol}",
            available=f"{budget.overall_cap - budget.spent_total - budget.reserved_total} {currency_symbol}",
        ),
        message(
            locale,
            "treasury.budget.contracts",
            cap=f"{budget.contracts_cap} {currency_symbol}",
            spent=f"{budget.contracts_spent} {currency_symbol}",
            reserved=f"{budget.contracts_reserved} {currency_symbol}",
            available=f"{budget.contracts_cap - budget.contracts_spent - budget.contracts_reserved} {currency_symbol}",
        ),
    )


def _title(contract: StateContract) -> str:
    return discord.utils.escape_markdown(" ".join(contract.title.split()))


def _recipient(locale: discord.Locale, contract: StateContract) -> str:
    if contract.recipient_company_id is not None:
        return message(locale, "contracts.recipient.company", id=str(contract.recipient_company_id))
    return f"<@{contract.recipient_discord_user_id}>"


def _contract_row(locale: discord.Locale, contract: StateContract, currency_symbol: str) -> str:
    return message(
        locale,
        "contracts.row",
        id=str(contract.id),
        title=_title(contract),
        status=message(locale, f"contracts.status.{contract.status.value.lower()}"),
        amount=f"{contract.amount} {currency_symbol}",
    )


def _contract_details(locale: discord.Locale, contract: StateContract, currency_symbol: str) -> list[str]:
    rows = [
        _contract_row(locale, contract, currency_symbol),
        message(locale, "contracts.recipient.label", recipient=_recipient(locale, contract)),
        message(locale, "contracts.deadline", date=discord.utils.format_dt(contract.deadline, style="f")),
        message(locale, "contracts.terms", terms=discord.utils.escape_markdown(contract.terms)),
    ]
    if contract.evidence:
        rows.append(message(locale, "contracts.proof", proof=discord.utils.escape_markdown(contract.evidence)))
    if contract.close_reason:
        rows.append(message(locale, "contracts.reason", reason=discord.utils.escape_markdown(contract.close_reason)))
    return rows


def add_treasury_commands(
    tree: app_commands.CommandTree,
    *,
    guild_scope: discord.Object,
    treasury_service: TreasuryService,
    currency_symbol: str = "₵",
) -> None:
    treasury = app_commands.Group(name="treasury", description=command_description("treasury"))
    contracts = app_commands.Group(name="contracts", description=command_description("contracts"))
    tree.add_command(treasury, guild=guild_scope)
    tree.add_command(contracts, guild=guild_scope)

    @treasury.command(name="budget", description=command_description("treasury.budget"))
    async def budget(interaction: discord.Interaction) -> None:
        locale = interaction.locale
        async with _treasury_response(interaction):
            current = await treasury_service.current_budget(interaction.guild_id, interaction.user.id)
            if current is None:
                await send_private_lines(interaction, message(locale, "treasury.budget.none"))
                return
            await send_private_lines(interaction, *_budget_rows(locale, current, currency_symbol))

    @treasury.command(name="approve_budget", description=command_description("treasury.approve_budget"))
    @app_commands.describe(reason=command_description("treasury.reason"))
    async def approve_budget(
        interaction: discord.Interaction, reason: app_commands.Range[str, 1, 200]
    ) -> None:
        async with _treasury_response(interaction):
            current = await treasury_service.activate_budget(interaction.guild_id, interaction.user.id, reason=reason)
            await send_private_lines(interaction, *_budget_rows(interaction.locale, current, currency_symbol))

    @contracts.command(name="create", description=command_description("contracts.create"))
    @app_commands.describe(
        title=command_description("contracts.title"),
        terms=command_description("contracts.terms"),
        amount=command_description("contracts.amount"),
        hours=command_description("contracts.hours"),
        reason=command_description("treasury.reason"),
        citizen=command_description("contracts.citizen"),
        company_id=command_description("contracts.company_id"),
    )
    async def create(
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 80],
        terms: app_commands.Range[str, 1, 500],
        amount: app_commands.Range[int, 1],
        hours: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
        citizen: discord.Member | None = None,
        company_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        locale = interaction.locale
        async with _treasury_response(interaction):
            try:
                deadline = interaction.created_at + timedelta(hours=hours)
            except OverflowError:
                await send_private_lines(interaction, message(locale, "treasury.error.invalid_deadline"))
                return
            draft = await treasury_service.propose_contract(
                interaction.guild_id,
                interaction.user.id,
                recipient_user_id=None if citizen is None else citizen.id,
                recipient_company_id=company_id,
                title=title,
                terms=terms,
                amount=amount,
                deadline=deadline,
                reason=reason,
                idempotency_key=str(interaction.id),
            )
            await send_private_lines(interaction, _contract_row(locale, draft, currency_symbol))

    @contracts.command(name="list", description=command_description("contracts.list"))
    @app_commands.describe(contract_id=command_description("contracts.id"))
    async def list_contracts(
        interaction: discord.Interaction, contract_id: app_commands.Range[int, 1] | None = None
    ) -> None:
        locale = interaction.locale
        guild_id, user_id = interaction.guild_id, interaction.user.id
        async with _treasury_response(interaction):
            if contract_id is not None:
                contract = await treasury_service.get_contract(guild_id, user_id, contract_id)
                await send_private_lines(interaction, *_contract_details(locale, contract, currency_symbol))
                return
            records = await treasury_service.list_contracts(guild_id, user_id)
            if not records:
                await send_private_lines(interaction, message(locale, "contracts.list.empty"))
                return
            await send_private_lines(
                interaction,
                message(locale, "contracts.list.title"),
                *(_contract_row(locale, contract, currency_symbol) for contract in records),
            )

    async def confirm_contract_action(
        interaction: discord.Interaction, contract_id: int, reason: str, *, action: str
    ) -> None:
        locale = interaction.locale
        apply = treasury_service.reserve_contract if action == "reserve" else treasury_service.pay_contract
        async with _treasury_response(interaction):
            contract = await treasury_service.get_contract(interaction.guild_id, interaction.user.id, contract_id)
            prompt = message(
                locale,
                f"contracts.{action}.preview",
                id=str(contract.id),
                title=_title(contract),
                recipient=_recipient(locale, contract),
                amount=f"{contract.amount} {currency_symbol}",
            )

            async def commit(click: discord.Interaction, _key: str) -> str:
                try:
                    updated = await apply(click.guild_id, click.user.id, contract.id, reason=reason)
                except _TREASURY_ERRORS as error:
                    return _error_reply(click.locale, error)
                return _contract_row(click.locale, updated, currency_symbol)

            confirmation = OperationConfirmation(
                guild_id=guild_scope.id, actor_id=interaction.user.id, locale=locale, commit=commit
            )
            await confirmation.show(interaction, prompt)

    @contracts.command(name="reserve", description=command_description("contracts.reserve"))
    @app_commands.describe(contract_id=command_description("contracts.id"), reason=command_description("treasury.reason"))
    async def reserve(
        interaction: discord.Interaction,
        contract_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await confirm_contract_action(interaction, contract_id, reason, action="reserve")

    @contracts.command(name="submit", description=command_description("contracts.submit"))
    @app_commands.describe(contract_id=command_description("contracts.id"), proof=command_description("contracts.proof"))
    async def submit(
        interaction: discord.Interaction,
        contract_id: app_commands.Range[int, 1],
        proof: app_commands.Range[str, 1, 500],
    ) -> None:
        async with _treasury_response(interaction):
            contract = await treasury_service.submit_contract(
                interaction.guild_id, interaction.user.id, contract_id, evidence=proof
            )
            await send_private_lines(interaction, _contract_row(interaction.locale, contract, currency_symbol))

    @contracts.command(name="accept", description=command_description("contracts.accept"))
    @app_commands.describe(contract_id=command_description("contracts.id"), reason=command_description("treasury.reason"))
    async def accept(
        interaction: discord.Interaction,
        contract_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        await confirm_contract_action(interaction, contract_id, reason, action="accept")

    @contracts.command(name="cancel", description=command_description("contracts.cancel"))
    @app_commands.describe(contract_id=command_description("contracts.id"), reason=command_description("treasury.reason"))
    async def cancel(
        interaction: discord.Interaction,
        contract_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 1, 200],
    ) -> None:
        async with _treasury_response(interaction):
            contract = await treasury_service.cancel_contract(
                interaction.guild_id, interaction.user.id, contract_id, reason=reason
            )
            await send_private_lines(interaction, _contract_row(interaction.locale, contract, currency_symbol))
