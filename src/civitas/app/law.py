"""Register immutable versions of acts already published in RP."""

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import OfficeKind
from civitas.domain.ledger import MAX_AMOUNT, MAX_IDEMPOTENCY_KEY_LENGTH

from .citizens import AccessDeniedReason, CitizenshipAccessError, is_discord_id

LAW_CODE_MAX_LENGTH = 32
LAW_TITLE_MAX_LENGTH = 120
LAW_TEXT_MAX_LENGTH = 4000
LAW_PAGE_SIZE = 10
LAW_MAX_PAGE = 1000

_LAW_CODE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_DISCORD_MESSAGE_URL = re.compile(r"https://discord\.com/channels/([0-9]{1,20})/([0-9]{1,20})/([0-9]{1,20})")


class ActKind(StrEnum):
    LAW = "LAW"
    DECREE = "DECREE"


@dataclass(frozen=True, slots=True)
class LawRevision:
    id: int
    guild_id: int
    code: str
    title: str
    text: str
    source_url: str
    effective_at: datetime
    proposer_citizen_id: int
    proposed_at: datetime
    confirmed_by_citizen_id: int | None = None
    confirmed_at: datetime | None = None
    act_kind: ActKind = ActKind.LAW


class LawErrorReason(StrEnum):
    DEPUTY_REQUIRED = "deputy_required"
    PRESIDENT_REQUIRED = "president_required"
    INVALID_CODE = "invalid_code"
    INVALID_TITLE = "invalid_title"
    INVALID_TEXT = "invalid_text"
    INVALID_SOURCE = "invalid_source"
    INVALID_EFFECTIVE_AT = "invalid_effective_at"
    INVALID_REQUEST = "invalid_request"
    REVISION_NOT_FOUND = "revision_not_found"
    INDEPENDENT_CONFIRMATION = "independent_confirmation"
    OCCUPIED_EFFECTIVE_AT = "occupied_effective_at"


class LawError(Exception):
    def __init__(self, reason: LawErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class LawRepository(Protocol):
    async def get_profile(self, guild_id: int, user_id: int) -> Citizen | None: ...
    async def lock_election_cycle(self, guild_id: int) -> None: ...
    async def is_allowed(self, discord_guild_id: int, user_id: int) -> bool: ...
    async def has_active_office(self, guild_id: int, citizen_id: int, office: OfficeKind, at: datetime) -> bool: ...

    async def find_law_revision(self, guild_id: int, request_key: str) -> LawRevision | None: ...
    async def add_law_revision(self, revision: LawRevision, request_key: str) -> LawRevision: ...
    async def get_law_revision(self, discord_guild_id: int, revision_id: int) -> LawRevision | None: ...
    async def confirm_law_revision(self, revision: LawRevision, confirmer_citizen_id: int, *, at: datetime) -> LawRevision: ...
    async def has_confirmed_law_slot(
        self, guild_id: int, code: str, effective_at: datetime, *, act_kind: ActKind = ActKind.LAW,
    ) -> bool: ...
    async def get_effective_law(
        self, discord_guild_id: int, code: str, at: datetime, *, act_kind: ActKind = ActKind.LAW,
    ) -> LawRevision | None: ...
    async def list_effective_laws(
        self, discord_guild_id: int, at: datetime, *, limit: int, offset: int, act_kind: ActKind = ActKind.LAW,
    ) -> tuple[LawRevision, ...]: ...


def _law_code(value: str) -> str:
    if not isinstance(value, str):
        raise LawError(LawErrorReason.INVALID_CODE)
    value = value.strip()
    if len(value) > LAW_CODE_MAX_LENGTH or _LAW_CODE.fullmatch(value) is None:
        raise LawError(LawErrorReason.INVALID_CODE)
    return value.upper()


def _law_text(value: str, *, limit: int, reason: LawErrorReason, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise LawError(reason)
    if multiline:
        value = value.replace("\r\n", "\n")
    value = value.strip()
    whitespace = "\n\t" if multiline else ""
    if not value or len(value) > limit or any(not ch.isprintable() and ch not in whitespace for ch in value):
        raise LawError(reason)
    return value


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise LawError(LawErrorReason.INVALID_EFFECTIVE_AT)
    try:
        timestamp = datetime.fromisoformat(value.strip())
        if timestamp.utcoffset() is None:
            raise ValueError("Time zone required")
        return timestamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise LawError(LawErrorReason.INVALID_EFFECTIVE_AT) from None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LawService:
    def __init__(
        self, unit_of_work: Callable[[], AbstractAsyncContextManager[LawRepository]], *,
        guild_id: int, clock: Callable[[], datetime] = _utcnow, act_kind: ActKind = ActKind.LAW,
    ) -> None:
        if not is_discord_id(guild_id):
            raise ValueError("guild_id must be a Discord snowflake")
        if not isinstance(act_kind, ActKind):
            raise ValueError("act_kind must be an ActKind")
        self._unit_of_work = unit_of_work
        self._guild_id = guild_id
        self._clock = clock
        self._act_kind = act_kind
        self._proposing_office = OfficeKind.PRESIDENT if act_kind is ActKind.DECREE else OfficeKind.PARLIAMENT

    async def register(
        self, guild_id: int | None, user_id: int, *, code: str, title: str, text: str, source_url: str,
        effective_at: str, idempotency_key: str,
    ) -> LawRevision:
        self._require_guild(guild_id)
        code = _law_code(code)
        title = _law_text(title, limit=LAW_TITLE_MAX_LENGTH, reason=LawErrorReason.INVALID_TITLE)
        text = _law_text(text, limit=LAW_TEXT_MAX_LENGTH, reason=LawErrorReason.INVALID_TEXT, multiline=True)
        source = self._source(source_url)
        effective = _timestamp(effective_at)
        if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH or not idempotency_key.isprintable()):
            raise LawError(LawErrorReason.INVALID_REQUEST)

        async with self._unit_of_work() as repository:
            actor, now = await self._lock_for_office(repository, user_id, self._proposing_office)
            existing = await repository.find_law_revision(actor.guild_id, idempotency_key)
            if existing is not None:
                expected = (self._act_kind, actor.id, code, title, text, source, effective)
                if (existing.act_kind, existing.proposer_citizen_id, existing.code, existing.title, existing.text,
                        existing.source_url, existing.effective_at) != expected:
                    raise LawError(LawErrorReason.INVALID_REQUEST)
                return existing
            if await repository.has_confirmed_law_slot(actor.guild_id, code, effective, act_kind=self._act_kind):
                raise LawError(LawErrorReason.OCCUPIED_EFFECTIVE_AT)
            return await repository.add_law_revision(LawRevision(
                id=0, guild_id=actor.guild_id, code=code, title=title, text=text, source_url=source,
                effective_at=effective, proposer_citizen_id=actor.id, proposed_at=now, act_kind=self._act_kind,
            ), idempotency_key)

    async def confirm(self, guild_id: int | None, user_id: int, revision_id: int) -> LawRevision:
        self._require_guild(guild_id)
        self._validate_revision_id(revision_id)
        async with self._unit_of_work() as repository:
            actor, now = await self._lock_for_office(repository, user_id, OfficeKind.PARLIAMENT)
            revision = await self._registered_revision(repository, revision_id)
            if revision.proposer_citizen_id == actor.id:
                raise LawError(LawErrorReason.INDEPENDENT_CONFIRMATION)
            if revision.confirmed_at is not None:
                return revision
            if await repository.has_confirmed_law_slot(
                revision.guild_id, revision.code, revision.effective_at, act_kind=self._act_kind,
            ):
                raise LawError(LawErrorReason.OCCUPIED_EFFECTIVE_AT)
            return await repository.confirm_law_revision(revision, actor.id, at=now)

    async def revision(self, guild_id: int | None, user_id: int, revision_id: int) -> LawRevision:
        self._require_guild(guild_id)
        self._validate_revision_id(revision_id)
        async with self._unit_of_work() as repository:
            revision = await self._registered_revision(repository, revision_id)
            if revision.confirmed_at is None and not await self._can_read_pending(repository, user_id, revision):
                raise LawError(LawErrorReason.REVISION_NOT_FOUND)
            return revision

    async def show(self, guild_id: int | None, code: str, *, at: str | None = None) -> LawRevision:
        self._require_guild(guild_id)
        code = _law_code(code)
        effective_at = self._clock() if at is None else _timestamp(at)
        async with self._unit_of_work() as repository:
            revision = await repository.get_effective_law(
                self._guild_id, code, effective_at, act_kind=self._act_kind,
            )
            if revision is None:
                raise LawError(LawErrorReason.REVISION_NOT_FOUND)
            return revision

    async def list_effective(self, guild_id: int | None, *, page: int = 1) -> tuple[LawRevision, ...]:
        self._require_guild(guild_id)
        if type(page) is not int or not 1 <= page <= LAW_MAX_PAGE:
            raise LawError(LawErrorReason.INVALID_REQUEST)
        async with self._unit_of_work() as repository:
            return await repository.list_effective_laws(
                self._guild_id, self._clock(), limit=LAW_PAGE_SIZE, offset=(page - 1) * LAW_PAGE_SIZE,
                act_kind=self._act_kind,
            )

    async def _registered_revision(self, repository: LawRepository, revision_id: int) -> LawRevision:
        revision = await repository.get_law_revision(self._guild_id, revision_id)
        if revision is None or revision.act_kind is not self._act_kind:
            raise LawError(LawErrorReason.REVISION_NOT_FOUND)
        return revision

    async def _can_read_pending(self, repository: LawRepository, user_id: int, revision: LawRevision) -> bool:
        actor = await self._eligible_actor(repository, user_id)
        if actor is None:
            return False
        now = self._clock()
        if await repository.has_active_office(actor.guild_id, actor.id, OfficeKind.PARLIAMENT, now):
            return True
        return (
            self._act_kind is ActKind.DECREE and actor.id == revision.proposer_citizen_id
            and await repository.has_active_office(actor.guild_id, actor.id, OfficeKind.PRESIDENT, now)
        )

    async def _lock_for_office(
        self, repository: LawRepository, user_id: int, office: OfficeKind,
    ) -> tuple[Citizen, datetime]:
        required = LawErrorReason.PRESIDENT_REQUIRED if office is OfficeKind.PRESIDENT else LawErrorReason.DEPUTY_REQUIRED
        if not is_discord_id(user_id):
            raise LawError(required)
        actor = await repository.get_profile(self._guild_id, user_id)
        if actor is None:
            raise LawError(required)
        await repository.lock_election_cycle(actor.guild_id)
        now = self._clock()
        actor = await self._eligible_actor(repository, user_id)
        if actor is None or not await repository.has_active_office(actor.guild_id, actor.id, office, now):
            raise LawError(required)
        return actor, now

    async def _eligible_actor(self, repository: LawRepository, user_id: int) -> Citizen | None:
        if not is_discord_id(user_id):
            return None
        actor = await repository.get_profile(self._guild_id, user_id)
        if actor is None or actor.status is not CitizenStatus.VERIFIED:
            return None
        return actor if await repository.is_allowed(self._guild_id, user_id) else None

    def _source(self, value: str) -> str:
        matched = _DISCORD_MESSAGE_URL.fullmatch(value.strip()) if isinstance(value, str) else None
        if matched is None:
            raise LawError(LawErrorReason.INVALID_SOURCE)
        guild_id, channel_id, message_id = map(int, matched.groups())
        if guild_id != self._guild_id or not is_discord_id(channel_id) or not is_discord_id(message_id):
            raise LawError(LawErrorReason.INVALID_SOURCE)
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    def _require_guild(self, guild_id: int | None) -> None:
        if not is_discord_id(guild_id) or guild_id != self._guild_id:
            raise CitizenshipAccessError(AccessDeniedReason.WRONG_GUILD)

    @staticmethod
    def _validate_revision_id(revision_id: int) -> None:
        if type(revision_id) is not int or not 1 <= revision_id <= MAX_AMOUNT:
            raise LawError(LawErrorReason.REVISION_NOT_FOUND)
