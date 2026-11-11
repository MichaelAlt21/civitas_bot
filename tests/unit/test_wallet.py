from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from civitas.app.citizens import CitizensService, CitizenshipAccessError
from civitas.app.wallet import WalletError, WalletErrorReason, WalletFunding, WalletRepository, WalletService
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import (
    MAX_AMOUNT,
    Account,
    AccountKind,
    HistoryEntry,
    LedgerError,
    LedgerErrorReason,
    PostResult,
    TransactionKind,
)

DISCORD_GUILD = 100
GUILD = 1
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SENDER = Citizen(1, GUILD, 200, CitizenStatus.VERIFIED, NOW, NOW)
RECIPIENT = Citizen(2, GUILD, 201, CitizenStatus.VERIFIED, NOW, NOW)
SUSPENDED = Citizen(3, GUILD, 202, CitizenStatus.SUSPENDED, NOW, NOW)
ISSUER_ACCOUNT = Account(1, GUILD, AccountKind.ISSUER, None)
TREASURY_ACCOUNT = Account(2, GUILD, AccountKind.TREASURY, None)
SENDER_ACCOUNT = Account(3, GUILD, AccountKind.PERSONAL, SENDER.id)
RECIPIENT_ACCOUNT = Account(4, GUILD, AccountKind.PERSONAL, RECIPIENT.id)


@pytest.fixture
def repository() -> AsyncMock:
    repo = AsyncMock(spec=WalletRepository)
    accounts = {
        (account.kind, account.citizen_id): account
        for account in (ISSUER_ACCOUNT, TREASURY_ACCOUNT, SENDER_ACCOUNT, RECIPIENT_ACCOUNT)
    }

    def account(guild_id: int, kind: AccountKind, citizen_id: int | None = None) -> Account:
        assert guild_id == GUILD
        return accounts[(kind, citizen_id)]

    def citizens(guild_id: int, user_ids: tuple[int, ...], *, lock: bool) -> tuple[Citizen, ...]:
        assert guild_id == DISCORD_GUILD
        assert lock
        return tuple(
            citizen
            for citizen in (SENDER, RECIPIENT, SUSPENDED)
            if citizen.discord_user_id in user_ids
        )

    repo.ensure_guild.return_value = GUILD
    repo.ensure_account.side_effect = account
    repo.get_citizens.side_effect = citizens
    repo.get_post.return_value = PostResult(90, created=False)
    repo.get_balance.return_value = 250
    repo.get_history.return_value = (HistoryEntry(91, TransactionKind.STARTER_GRANT, 250, NOW),)
    repo.post.return_value = PostResult(92, created=True)
    return repo


@pytest.fixture
def wallet(repository: AsyncMock) -> WalletService:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[WalletRepository]:
        yield repository

    citizens = CitizensService(
        unit_of_work,
        guild_id=DISCORD_GUILD,
        allowed_user_ids=[SENDER.discord_user_id, SUSPENDED.discord_user_id],
    )
    return WalletService(
        unit_of_work,
        guild_id=DISCORD_GUILD,
        authorize=citizens.authorize,
        funding=WalletFunding(starter_grant=250, treasury_genesis=8000),
    )


@pytest.mark.asyncio
async def test_explicit_genesis_and_first_wallet_access_issue_stable_postings(
    wallet: WalletService, repository: AsyncMock
) -> None:
    repository.get_post.side_effect = (None, PostResult(90, created=False), None)

    await wallet.initialize_guild()
    assert await wallet.balance(DISCORD_GUILD, SENDER.discord_user_id) == 250

    genesis, grant = repository.post.await_args_list
    assert genesis.args == (
        GUILD,
        TransactionKind.GENESIS,
        "genesis",
        {ISSUER_ACCOUNT.id: -8000, TREASURY_ACCOUNT.id: 8000},
    )
    assert grant.args == (
        GUILD,
        TransactionKind.STARTER_GRANT,
        f"starter_grant:{SENDER.id}",
        {ISSUER_ACCOUNT.id: -250, SENDER_ACCOUNT.id: 250},
    )
    repository.ensure_guild.assert_awaited_once_with(DISCORD_GUILD)


@pytest.mark.asyncio
async def test_existing_issuance_receipts_keep_original_amounts(repository: AsyncMock) -> None:
    changed_settings = WalletFunding(starter_grant=300, treasury_genesis=9000)

    assert await changed_settings.fund_registered_citizen(repository, SENDER) == SENDER_ACCOUNT

    repository.ensure_account.assert_awaited_once_with(GUILD, AccountKind.PERSONAL, SENDER.id)
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_funding_creates_accounts_without_zero_postings(repository: AsyncMock) -> None:
    repository.get_post.return_value = None
    funding = WalletFunding(starter_grant=0, treasury_genesis=0)

    assert await funding.fund_registered_citizen(repository, SENDER) == SENDER_ACCOUNT

    assert repository.ensure_account.await_count == 3
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_and_citizen_status_precede_wallet_access(
    wallet: WalletService, repository: AsyncMock
) -> None:
    with pytest.raises(CitizenshipAccessError):
        await wallet.balance(None, SENDER.discord_user_id)
    with pytest.raises(CitizenshipAccessError):
        await wallet.history(DISCORD_GUILD, RECIPIENT.discord_user_id)
    repository.get_citizens.assert_not_awaited()

    with pytest.raises(WalletError) as denied:
        await wallet.balance(DISCORD_GUILD, SUSPENDED.discord_user_id)
    assert denied.value.reason == WalletErrorReason.CITIZEN_NOT_VERIFIED
    repository.ensure_account.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_is_scoped_to_own_personal_account(
    wallet: WalletService, repository: AsyncMock
) -> None:
    history = await wallet.history(DISCORD_GUILD, SENDER.discord_user_id)

    assert history == (HistoryEntry(91, TransactionKind.STARTER_GRANT, 250, NOW),)
    repository.get_history.assert_awaited_once_with(SENDER_ACCOUNT.id, 10)
    repository.post.assert_not_awaited()


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, MAX_AMOUNT + 1])
@pytest.mark.asyncio
async def test_invalid_amount_is_rejected_before_wallet_mutation(
    wallet: WalletService, repository: AsyncMock, amount: int
) -> None:
    with pytest.raises(WalletError) as denied:
        await wallet.transfer(
            DISCORD_GUILD, SENDER.discord_user_id, RECIPIENT.discord_user_id, amount,
            idempotency_key="draft-1",
        )

    assert denied.value.reason == WalletErrorReason.INVALID_AMOUNT
    repository.get_citizens.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfers_to_unverified_or_self_are_denied(
    wallet: WalletService, repository: AsyncMock
) -> None:
    with pytest.raises(WalletError) as suspended:
        await wallet.prepare_transfer(DISCORD_GUILD, SENDER.discord_user_id, SUSPENDED.discord_user_id, 50)
    with pytest.raises(WalletError) as self_transfer:
        await wallet.prepare_transfer(DISCORD_GUILD, SENDER.discord_user_id, SENDER.discord_user_id, 50)

    assert suspended.value.reason == WalletErrorReason.RECIPIENT_NOT_VERIFIED
    assert self_transfer.value.reason == WalletErrorReason.SELF_TRANSFER
    repository.ensure_account.assert_not_awaited()
    repository.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_confirmation_reuses_the_posting_key_and_verifies_funds_in_db(
    wallet: WalletService, repository: AsyncMock
) -> None:
    repository.post.side_effect = (PostResult(92, True), PostResult(92, False))

    first = await wallet.transfer(
        DISCORD_GUILD, SENDER.discord_user_id, RECIPIENT.discord_user_id, 150,
        idempotency_key="draft-1",
    )
    repeated = await wallet.transfer(
        DISCORD_GUILD, SENDER.discord_user_id, RECIPIENT.discord_user_id, 150,
        idempotency_key="draft-1",
    )

    assert first == PostResult(92, True)
    assert repeated == PostResult(92, False)
    for posting in repository.post.await_args_list:
        assert posting.args == (
            GUILD,
            TransactionKind.TRANSFER,
            "draft-1",
            {SENDER_ACCOUNT.id: -150, RECIPIENT_ACCOUNT.id: 150},
        )
        assert posting.kwargs == {"actor_citizen_id": SENDER.id, "reason": "citizen_transfer"}
    repository.get_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_refuses_insufficient_funds_without_posting_transfer(
    wallet: WalletService, repository: AsyncMock
) -> None:
    repository.get_balance.return_value = 100

    with pytest.raises(LedgerError) as denied:
        await wallet.prepare_transfer(DISCORD_GUILD, SENDER.discord_user_id, RECIPIENT.discord_user_id, 150)

    assert denied.value.reason == LedgerErrorReason.INSUFFICIENT_FUNDS
    repository.post.assert_not_awaited()
