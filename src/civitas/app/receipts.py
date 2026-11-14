"""Opt in to private ledger receipts and retry their delivery after commit."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from civitas.app.citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id
from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.ledger import MAX_AMOUNT
from civitas.domain.receipts import ReceiptDelivery, ReceiptDetails

_logger = logging.getLogger(__name__)
_LEASE_DURATION = timedelta(minutes=2)
_SEND_TIMEOUT = 45


class ReceiptUndeliverable(Exception):
    """Discord cannot deliver a receipt to this account."""


class ReceiptRepository(Protocol):
    async def get_citizens(
        self, guild_id: int, user_ids: tuple[int, ...], *, lock: bool = False,
    ) -> tuple[Citizen, ...]: ...

    async def set_receipt_preference(self, guild_id: int, citizen_id: int, locale: str | None) -> None: ...

    async def get_own_receipt(self, guild_id: int, citizen_id: int, transaction_id: int) -> ReceiptDetails | None: ...

    async def claim_receipts(
        self, discord_guild_id: int, *, now: datetime, lease_until: datetime, limit: int,
    ) -> tuple[ReceiptDelivery, ...]: ...

    async def can_deliver_receipt(self, receipt_id: int, lease_token: UUID, *, now: datetime) -> bool: ...

    async def mark_receipt_sent(
        self, receipt_id: int, lease_token: UUID, *, message_id: int, at: datetime,
    ) -> bool: ...

    async def reschedule_receipt(self, receipt_id: int, lease_token: UUID, *, retry_at: datetime) -> bool: ...

    async def suppress_receipt(self, receipt_id: int, lease_token: UUID, *, at: datetime) -> bool: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReceiptService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[ReceiptRepository]], *, guild_id: int,
        authorize: Callable[[int | None, int], None], clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._authorize = authorize
        self._clock = clock

    async def set_preference(self, guild_id: int | None, user_id: int, *, locale: str | None) -> None:
        self._authorize(guild_id, user_id)
        if locale not in ("ru", "en", None):
            raise ValueError("Receipt locale must be ru, en or disabled")
        async with self._unit_of_work() as repository:
            citizens = await repository.get_citizens(self._guild_id, (user_id,), lock=True)
            if not citizens or (locale is not None and citizens[0].status != CitizenStatus.VERIFIED):
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
            await repository.set_receipt_preference(citizens[0].guild_id, citizens[0].id, locale)

    async def run_due(self, send: Callable[[ReceiptDelivery], Awaitable[int]]) -> bool:
        now = self._clock()
        async with self._unit_of_work() as repository:
            claimed = await repository.claim_receipts(
                self._guild_id, now=now, lease_until=now + _LEASE_DURATION, limit=1,
            )
        if not claimed:
            return False
        (receipt,) = claimed
        async with self._unit_of_work() as repository:
            if not await repository.can_deliver_receipt(receipt.id, receipt.lease_token, now=self._clock()):
                await repository.suppress_receipt(receipt.id, receipt.lease_token, at=self._clock())
                return False
        try:
            async with asyncio.timeout(_SEND_TIMEOUT):
                message_id = await send(receipt)
                if not is_discord_id(message_id):
                    raise ValueError("Private delivery did not return a Discord message ID")
        except ReceiptUndeliverable:
            async with self._unit_of_work() as repository:
                await repository.suppress_receipt(receipt.id, receipt.lease_token, at=self._clock())
            return False
        except Exception as error:
            _logger.warning("Не удалось доставить приватную квитанцию (%s)", type(error).__name__)
            retry_delay = timedelta(seconds=min(30 * 2 ** min(max(receipt.attempts - 1, 0), 6), 1800))
            async with self._unit_of_work() as repository:
                await repository.reschedule_receipt(
                    receipt.id, receipt.lease_token, retry_at=self._clock() + retry_delay,
                )
            return False
        async with self._unit_of_work() as repository:
            return await repository.mark_receipt_sent(
                receipt.id, receipt.lease_token, message_id=message_id, at=self._clock(),
            )

    async def get_receipt(self, guild_id: int | None, user_id: int, transaction_id: int) -> ReceiptDetails | None:
        self._authorize(guild_id, user_id)
        if type(transaction_id) is not int or not 1 <= transaction_id <= MAX_AMOUNT:
            return None
        async with self._unit_of_work() as repository:
            citizens = await repository.get_citizens(self._guild_id, (user_id,))
            if not citizens or citizens[0].status != CitizenStatus.VERIFIED:
                raise CitizenshipAccessError(AccessDeniedReason.NOT_ALLOWED)
            return await repository.get_own_receipt(citizens[0].guild_id, citizens[0].id, transaction_id)
