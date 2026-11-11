"""Use cases for personal wallets and configured one-time funding."""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Protocol

from civitas.app.citizens import is_discord_id
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import (
    MAX_AMOUNT,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    Account,
    AccountKind,
    HistoryEntry,
    LedgerError,
    LedgerErrorReason,
    PostResult,
    TransactionKind,
)

HISTORY_LIMIT = 10


class WalletErrorReason(StrEnum):
    CITIZEN_NOT_VERIFIED = "citizen_not_verified"
    RECIPIENT_NOT_VERIFIED = "recipient_not_verified"
    INVALID_AMOUNT = "invalid_amount"
    SELF_TRANSFER = "self_transfer"
    INVALID_TRANSFER_KEY = "invalid_transfer_key"


class WalletError(Exception):
    def __init__(self, reason: WalletErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class WalletRepository(Protocol):
    async def get_citizens(
        self, guild_id: int, user_ids: tuple[int, ...], *, lock: bool = False
    ) -> tuple[Citizen, ...]: ...

    async def ensure_guild(self, guild_id: int) -> int: ...

    async def ensure_account(
        self, guild_id: int, kind: AccountKind, citizen_id: int | None = None
    ) -> Account: ...

    async def get_post(
        self, guild_id: int, kind: TransactionKind, key: str
    ) -> PostResult | None: ...

    async def get_balance(self, account_id: int) -> int: ...

    async def get_history(self, account_id: int, limit: int) -> tuple[HistoryEntry, ...]: ...

    async def post(
        self,
        guild_id: int,
        kind: TransactionKind,
        key: str,
        entries: Mapping[int, int],
        *,
        actor_citizen_id: int | None = None,
        reason: str,
    ) -> PostResult: ...


class WalletFunding:
    def __init__(self, *, starter_grant: int, treasury_genesis: int) -> None:
        if any(
            type(amount) is not int or not 0 <= amount <= MAX_AMOUNT
            for amount in (starter_grant, treasury_genesis)
        ):
            raise ValueError("Funding amounts must be non-negative BIGINT integers")
        self._starter_grant = starter_grant
        self._treasury_genesis = treasury_genesis

    async def initialize_guild(self, repository: WalletRepository, guild_id: int) -> None:
        if await repository.get_post(guild_id, TransactionKind.GENESIS, "genesis") is not None:
            return
        issuer = await repository.ensure_account(guild_id, AccountKind.ISSUER)
        treasury = await repository.ensure_account(guild_id, AccountKind.TREASURY)
        if self._treasury_genesis:
            await repository.post(
                guild_id,
                TransactionKind.GENESIS,
                "genesis",
                {issuer.id: -self._treasury_genesis, treasury.id: self._treasury_genesis},
                reason="configured_genesis",
            )

    async def fund_registered_citizen(
        self, repository: WalletRepository, citizen: Citizen
    ) -> Account:
        """Open and fund the account of an authorized, locked or newly created citizen."""
        if citizen.status != CitizenStatus.VERIFIED:
            raise WalletError(WalletErrorReason.CITIZEN_NOT_VERIFIED)
        await self.initialize_guild(repository, citizen.guild_id)
        account = await repository.ensure_account(citizen.guild_id, AccountKind.PERSONAL, citizen.id)
        key = f"starter_grant:{citizen.id}"
        if self._starter_grant and await repository.get_post(
            citizen.guild_id, TransactionKind.STARTER_GRANT, key
        ) is None:
            issuer = await repository.ensure_account(citizen.guild_id, AccountKind.ISSUER)
            await repository.post(
                citizen.guild_id,
                TransactionKind.STARTER_GRANT,
                key,
                {issuer.id: -self._starter_grant, account.id: self._starter_grant},
                reason="configured_starter_grant",
            )
        return account


class WalletService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractAsyncContextManager[WalletRepository]],
        *,
        guild_id: int,
        authorize: Callable[[int | None, int], None],
        funding: WalletFunding,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._funding = funding

    async def initialize_guild(self) -> None:
        async with self._unit_of_work() as repository:
            guild_id = await repository.ensure_guild(self._guild_id)
            await self._funding.initialize_guild(repository, guild_id)

    async def fund_citizen(self, guild_id: int | None, user_id: int) -> None:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            await self._personal_account(repository, user_id)

    async def balance(self, guild_id: int | None, user_id: int) -> int:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            account = await self._personal_account(repository, user_id)
            return await repository.get_balance(account.id)

    async def history(self, guild_id: int | None, user_id: int) -> tuple[HistoryEntry, ...]:
        self._authorize(guild_id, user_id)
        async with self._unit_of_work() as repository:
            account = await self._personal_account(repository, user_id)
            return await repository.get_history(account.id, HISTORY_LIMIT)

    async def prepare_transfer(
        self, guild_id: int | None, user_id: int, recipient_user_id: int, amount: int
    ) -> None:
        self._authorize(guild_id, user_id)
        self._validate_transfer(user_id, recipient_user_id, amount)
        async with self._unit_of_work() as repository:
            source, _ = await self._transfer_accounts(repository, user_id, recipient_user_id)
            if await repository.get_balance(source.id) < amount:
                raise LedgerError(LedgerErrorReason.INSUFFICIENT_FUNDS)

    async def transfer(
        self,
        guild_id: int | None,
        user_id: int,
        recipient_user_id: int,
        amount: int,
        *,
        idempotency_key: str,
    ) -> PostResult:
        self._authorize(guild_id, user_id)
        self._validate_transfer(user_id, recipient_user_id, amount)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
            or "\0" in idempotency_key
        ):
            raise WalletError(WalletErrorReason.INVALID_TRANSFER_KEY)
        async with self._unit_of_work() as repository:
            source, target = await self._transfer_accounts(repository, user_id, recipient_user_id)
            return await repository.post(
                source.guild_id,
                TransactionKind.TRANSFER,
                idempotency_key,
                {source.id: -amount, target.id: amount},
                actor_citizen_id=source.citizen_id,
                reason="citizen_transfer",
            )

    async def _personal_account(self, repository: WalletRepository, user_id: int) -> Account:
        citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=True)
        if not citizens:
            raise WalletError(WalletErrorReason.CITIZEN_NOT_VERIFIED)
        return await self._funding.fund_registered_citizen(repository, citizens[0])

    async def _transfer_accounts(
        self, repository: WalletRepository, user_id: int, recipient_user_id: int
    ) -> tuple[Account, Account]:
        citizens = await repository.get_citizens(
            self._guild_id, (user_id, recipient_user_id), lock=True
        )
        verified = {
            citizen.discord_user_id: citizen
            for citizen in citizens
            if citizen.status == CitizenStatus.VERIFIED
        }
        source = verified.get(user_id)
        if source is None:
            raise WalletError(WalletErrorReason.CITIZEN_NOT_VERIFIED)
        target = verified.get(recipient_user_id)
        if target is None:
            raise WalletError(WalletErrorReason.RECIPIENT_NOT_VERIFIED)
        source_account = await self._funding.fund_registered_citizen(repository, source)
        target_account = await repository.ensure_account(target.guild_id, AccountKind.PERSONAL, target.id)
        return source_account, target_account

    @staticmethod
    def _validate_transfer(user_id: int, recipient_user_id: int, amount: int) -> None:
        if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
            raise WalletError(WalletErrorReason.INVALID_AMOUNT)
        if not is_discord_id(recipient_user_id):
            raise WalletError(WalletErrorReason.RECIPIENT_NOT_VERIFIED)
        if user_id == recipient_user_id:
            raise WalletError(WalletErrorReason.SELF_TRANSFER)
