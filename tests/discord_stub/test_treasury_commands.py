from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from civitas.app.treasury import TreasuryError, TreasuryErrorReason, TreasuryService
from civitas.bot.localization import CommandTranslator, message
from civitas.bot.treasury import add_treasury_commands
from civitas.domain.ledger import LedgerError, LedgerErrorReason, TransactionKind
from civitas.domain.treasury import ContractBudget, ContractStatus, StateContract

GUILD_ID = 101
PROPOSER_ID = 201
RECIPIENT_ID = 202
APPROVER_ID = 203
OUTSIDER_ID = 204
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
DRAFT = StateContract(
    id=30, guild_id=1, title="Bridge repair", terms="Repair a fictional bridge", amount=200,
    deadline=NOW + timedelta(hours=1), recipient_citizen_id=2, recipient_company_id=None,
    proposer_citizen_id=1, proposal_reason="Bridge damaged", request_key="1001", created_at=NOW,
    status=ContractStatus.PROPOSED, recipient_discord_user_id=RECIPIENT_ID,
)
BUDGET = ContractBudget(
    period_id=10, guild_id=1, start_at=NOW, end_at=NOW + timedelta(days=7),
    overall_cap=800, spent_total=100, reserved_total=50, contracts_cap=250, contracts_spent=0,
    contracts_reserved=50, activated_at=NOW, activated_by_citizen_id=3, activation_reason="Weekly vote",
)


@pytest.fixture
def commands():
    service = Mock(spec=TreasuryService)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    guild = discord.Object(id=GUILD_ID)
    add_treasury_commands(tree, guild_scope=guild, treasury_service=service, currency_symbol="CP")
    return service, tree, tree.get_command("treasury", guild=guild), tree.get_command("contracts", guild=guild)


def interaction(*, actor=APPROVER_ID, locale=discord.Locale.russian, guild_id=GUILD_ID):
    return SimpleNamespace(
        id=1001, created_at=NOW, guild_id=guild_id, locale=locale, user=SimpleNamespace(id=actor),
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
async def test_treasury_groups_and_command_options_are_guild_only_and_translated(commands):
    _, tree, treasury, contracts = commands
    assert tree.get_commands() == []
    assert tree.get_commands(guild=discord.Object(id=GUILD_ID + 1)) == []
    for group, names in [
        (treasury, {"budget", "approve_budget"}),
        (contracts, {"create", "list", "reserve", "submit", "accept", "cancel"}),
    ]:
        payload = await group.get_translated_payload(tree, CommandTranslator())
        assert payload["description_localizations"]["ru"] != payload["description"]
        assert {command["name"] for command in payload["options"]} == names
        for command in payload["options"]:
            assert command["description_localizations"]["ru"] != command["description"]
            for option in command.get("options", []):
                assert option["description_localizations"]["ru"] != option["description"]


@pytest.mark.asyncio
async def test_budget_displays_available_after_spending_and_reserve(commands):
    service, _, treasury, _ = commands
    service.current_budget.return_value = BUDGET
    service.activate_budget.return_value = BUDGET
    viewing = interaction(actor=OUTSIDER_ID, locale=discord.Locale.american_english)
    approving = interaction()

    await treasury.get_command("budget").callback(viewing)
    await treasury.get_command("approve_budget").callback(approving, "Weekly vote")

    service.current_budget.assert_awaited_once_with(GUILD_ID, OUTSIDER_ID)
    service.activate_budget.assert_awaited_once_with(GUILD_ID, APPROVER_ID, reason="Weekly vote")
    english, russian = private_reply(viewing), private_reply(approving)
    for amount in ("650 CP", "200 CP", "50 CP"):
        assert amount in english and amount in russian
    assert "Budget week" in english and "Бюджетная неделя" in russian


@pytest.mark.asyncio
async def test_missing_budget_explains_approval_instead_of_displaying_empty_caps(commands):
    service, _, treasury, _ = commands
    service.current_budget.return_value = None
    request = interaction()

    await treasury.get_command("budget").callback(request)

    assert private_reply(request) == message(request.locale, "treasury.budget.none")


@pytest.mark.parametrize(("citizen", "company_id"), [(SimpleNamespace(id=RECIPIENT_ID), None), (None, 12)])
@pytest.mark.asyncio
async def test_creation_replay_uses_original_timestamp_and_request_id(commands, citizen, company_id):
    service, _, _, contracts = commands
    service.propose_contract.return_value = DRAFT
    opening, repeated = interaction(actor=PROPOSER_ID), interaction(actor=PROPOSER_ID)
    for request in (opening, repeated):
        await contracts.get_command("create").callback(
            request, "Bridge repair", "Repair a fictional bridge", 200, 1, "Bridge damaged", citizen, company_id,
        )
    first, second = service.propose_contract.await_args_list
    assert first == second
    assert first.args == (GUILD_ID, PROPOSER_ID)
    assert first.kwargs["deadline"] == NOW + timedelta(hours=1)
    assert first.kwargs["idempotency_key"] == str(opening.id)
    assert first.kwargs["recipient_user_id"] == (citizen.id if citizen else None)
    assert first.kwargs["recipient_company_id"] == company_id
    assert "черновик" in private_reply(opening)
    assert private_reply(opening) == private_reply(repeated)


@pytest.mark.asyncio
async def test_proof_is_omitted_from_list_and_shown_only_in_private_authorized_detail(commands):
    service, _, _, contracts = commands
    record = replace(DRAFT, status=ContractStatus.SUBMITTED, evidence="Private proof @everyone https://example.org")
    service.list_contracts.return_value = (record,)
    service.get_contract.side_effect = [record, TreasuryError(TreasuryErrorReason.NOT_CONTRACT_PARTY)]
    listing, detail = interaction(actor=RECIPIENT_ID), interaction(actor=RECIPIENT_ID)
    outsider = interaction(actor=OUTSIDER_ID, locale=discord.Locale.american_english)

    await contracts.get_command("list").callback(listing)
    await contracts.get_command("list").callback(detail, DRAFT.id)
    await contracts.get_command("list").callback(outsider, DRAFT.id)

    assert "Private proof" not in private_reply(listing)
    assert "Private proof" in private_reply(detail)
    denial = private_reply(outsider)
    assert denial == message(outsider.locale, "treasury.error.not_contract_party")
    assert DRAFT.title not in denial and "Private proof" not in denial


@pytest.mark.parametrize(("command_name", "preview_action", "method", "before", "after"), [
    ("reserve", "reserve", "reserve_contract", ContractStatus.PROPOSED, ContractStatus.RESERVED),
    ("accept", "pay", "pay_contract", ContractStatus.SUBMITTED, ContractStatus.PAID),
])
@pytest.mark.asyncio
async def test_confirmation_retry_keeps_same_contract_and_only_authorized_actor(commands, command_name, preview_action, method, before, after):
    service, _, _, contracts = commands
    service.preview_contract_action.return_value = replace(DRAFT, status=before)
    apply = getattr(service, method)
    apply.return_value = replace(DRAFT, status=after, payout_transaction_id=99 if command_name == "accept" else None)
    opening = interaction()
    await contracts.get_command(command_name).callback(opening, DRAFT.id, "Reviewed")

    service.preview_contract_action.assert_awaited_once_with(GUILD_ID, APPROVER_ID, DRAFT.id, action=preview_action)
    apply.assert_not_awaited()
    opening.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    prompt = opening.edit_original_response.await_args.kwargs
    assert "200 CP" in prompt["content"]
    assert prompt["allowed_mentions"].to_dict() == {"parse": []}
    view = prompt["view"]
    outsider = interaction(actor=OUTSIDER_ID)
    assert await view.interaction_check(outsider) is False
    assert outsider.response.send_message.await_args.kwargs["ephemeral"] is True
    apply.assert_not_awaited()

    failed_reply = interaction()
    failed_reply.edit_original_response.side_effect = RuntimeError("connection dropped after commit")
    with pytest.raises(RuntimeError, match="connection dropped") as failure:
        await view.confirm.callback(failed_reply)
    await view.on_error(failed_reply, failure.value, view.confirm)
    failed_reply.followup.send.assert_awaited_once_with(message(failed_reply.locale, "confirmation.retry"), ephemeral=True)
    retry = interaction(locale=discord.Locale.american_english)
    await view.confirm.callback(retry)
    assert apply.await_count == 2
    first, second = apply.await_args_list
    assert first == second and first.args == (GUILD_ID, APPROVER_ID, DRAFT.id) and first.kwargs == {"reason": "Reviewed"}
    assert message(retry.locale, f"contracts.status.{after.value.lower()}") in retry.edit_original_response.await_args.kwargs["content"]
    await view.confirm.callback(interaction())
    assert apply.await_count == 2


@pytest.mark.asyncio
async def test_expense_preflight_denial_has_no_preview_or_commit(commands):
    service, _, _, contracts = commands
    service.preview_contract_action.side_effect = TreasuryError(TreasuryErrorReason.APPROVER_REQUIRED)
    opening = interaction(actor=OUTSIDER_ID)

    await contracts.get_command("reserve").callback(opening, DRAFT.id, "Attempted approval")

    service.preview_contract_action.assert_awaited_once_with(GUILD_ID, OUTSIDER_ID, DRAFT.id, action="reserve")
    opening.edit_original_response.assert_not_awaited()
    service.reserve_contract.assert_not_awaited()
    assert private_reply(opening) == message(opening.locale, "treasury.error.approver_required")


@pytest.mark.asyncio
async def test_payment_rechecks_and_localizes_ledger_denial_after_confirmation(commands):
    service, _, _, contracts = commands
    service.preview_contract_action.return_value = replace(DRAFT, status=ContractStatus.SUBMITTED)
    service.pay_contract.side_effect = LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)
    opening, click = interaction(), interaction()

    await contracts.get_command("accept").callback(opening, DRAFT.id, "Accepted")
    view = opening.edit_original_response.await_args.kwargs["view"]
    await view.confirm.callback(click)

    assert click.edit_original_response.await_args.kwargs["content"] == message(click.locale, "treasury.error.treasury_insufficient_funds")
    assert view.is_finished()


@pytest.mark.asyncio
async def test_submission_and_cancellation_use_private_status_without_echoing_proof(commands):
    service, _, _, contracts = commands
    service.submit_contract.return_value = replace(DRAFT, status=ContractStatus.SUBMITTED, evidence="Private evidence")
    service.cancel_contract.return_value = replace(DRAFT, status=ContractStatus.CANCELLED)
    submission, cancellation = interaction(actor=RECIPIENT_ID), interaction()

    await contracts.get_command("submit").callback(submission, DRAFT.id, "Private evidence")
    await contracts.get_command("cancel").callback(cancellation, DRAFT.id, "Deadline passed")

    service.submit_contract.assert_awaited_once_with(GUILD_ID, RECIPIENT_ID, DRAFT.id, evidence="Private evidence")
    service.cancel_contract.assert_awaited_once_with(GUILD_ID, APPROVER_ID, DRAFT.id, reason="Deadline passed")
    assert "работа сдана" in private_reply(submission)
    assert "Private evidence" not in private_reply(submission)
    assert "отменён" in private_reply(cancellation)


def test_wallet_history_localizes_state_contract_payment():
    key = f"wallet.history.{TransactionKind.STATE_CONTRACT_PAYOUT.value.lower()}"
    assert message(discord.Locale.american_english, key) == "State contract payment"
    assert message(discord.Locale.russian, key) == "Выплата по государственному контракту"
