from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from civitas.domain.citizens import Citizen, CitizenStatus
from civitas.domain.government import (
    CandidateTally, Election, ElectionProtocol, ElectionStatus, OfficeKind, OfficeRoleAssignment,
    OfficeTerm,
)

from .citizens import _to_citizen
from .government_models import (
    ElectionCertificationRow, ElectionProtocolRow, ElectionRow, PublicOfficeRow,
)
from .models import CitizenRow, GuildRow
from .private_jobs import PostgresPrivateJobRepository


def _protocol(row: ElectionProtocolRow | None) -> ElectionProtocol | None:
    if row is None:
        return None
    return ElectionProtocol(
        source_url=row.source_url,
        participants=row.participants,
        tallies=tuple(CandidateTally(*item) for item in row.tallies),
        winners=tuple(row.winners),
        recorded_by_citizen_id=row.recorded_by_citizen_id,
        recorded_at=row.recorded_at.astimezone(UTC),
        reason=row.reason,
    )


def _election(
    row: ElectionRow,
    evidence: ElectionProtocolRow | None,
    certificate: ElectionCertificationRow | None,
) -> Election:
    return Election(
        id=row.id, guild_id=row.guild_id, office=OfficeKind(row.office),
        seats=row.seats, term_days=row.term_days, quorum=row.quorum,
        opens_at=row.opens_at.astimezone(UTC), closes_at=row.closes_at.astimezone(UTC),
        opened_by_citizen_id=row.opened_by_citizen_id, reason=row.reason, request_key=row.request_key,
        eligible_citizen_ids=tuple(row.eligible_citizen_ids),
        candidate_citizen_ids=tuple(row.candidate_citizen_ids), status=ElectionStatus(row.status),
        protocol=_protocol(evidence),
        extended_until=row.extended_until.astimezone(UTC) if row.extended_until else None,
        certified_by_citizen_id=certificate.certified_by_citizen_id if certificate else None,
        certified_at=certificate.certified_at.astimezone(UTC) if certificate else None,
        certification_reason=certificate.reason if certificate else None,
        runoff_of_id=row.runoff_of_id, extension_hours=row.extension_hours,
        candidate_discord_user_ids=tuple(int(user_id) for user_id in row.candidate_discord_user_ids),
    )


def _term(row: PublicOfficeRow, discord_user_id: int) -> OfficeTerm:
    return OfficeTerm(
        election_id=row.election_id, office=OfficeKind(row.office), seat=row.seat,
        holder_citizen_id=row.holder_citizen_id, holder_discord_user_id=discord_user_id,
        starts_at=row.starts_at.astimezone(UTC), ends_at=row.ends_at.astimezone(UTC),
    )


class PostgresGovernmentRepository(PostgresPrivateJobRepository):
    async def list_verified_citizens(self, guild_id: int) -> tuple[Citizen, ...]:
        rows = await self._session.scalars(
            select(CitizenRow).where(
                CitizenRow.guild_id == guild_id, CitizenRow.status == CitizenStatus.VERIFIED.value
            ).order_by(CitizenRow.id)
        )
        return tuple(_to_citizen(row) for row in rows)

    async def _election_with_evidence(self, row: ElectionRow) -> Election:
        evidence = await self._session.scalar(
            select(ElectionProtocolRow).where(ElectionProtocolRow.election_id == row.id)
            .order_by(ElectionProtocolRow.round_number.desc()).limit(1)
        )
        certificate = await self._session.get(ElectionCertificationRow, row.id)
        return _election(row, evidence, certificate)

    async def _get_election(
        self, guild_id: int, criterion: ColumnElement[bool], *, lock: bool = False
    ) -> Election | None:
        statement = select(ElectionRow).where(ElectionRow.guild_id == guild_id, criterion)
        if lock:
            statement = statement.with_for_update(key_share=True)
        row = await self._session.scalar(statement)
        return await self._election_with_evidence(row) if row is not None else None

    async def get_election(self, guild_id: int, election_id: int, *, lock: bool = False) -> Election | None:
        return await self._get_election(guild_id, ElectionRow.id == election_id, lock=lock)

    async def get_election_by_key(self, guild_id: int, key: str) -> Election | None:
        return await self._get_election(guild_id, ElectionRow.request_key == key)

    async def create_election(self, election: Election) -> Election | None:
        row = await self._session.scalar(
            pg_insert(ElectionRow).values(
                guild_id=election.guild_id, office=election.office.value,
                seats=election.seats, term_days=election.term_days, quorum=election.quorum,
                opens_at=election.opens_at, closes_at=election.closes_at,
                opened_by_citizen_id=election.opened_by_citizen_id, reason=election.reason,
                request_key=election.request_key, eligible_citizen_ids=list(election.eligible_citizen_ids),
                candidate_citizen_ids=list(election.candidate_citizen_ids),
                candidate_discord_user_ids=[Decimal(user_id) for user_id in election.candidate_discord_user_ids],
                status=election.status.value, runoff_of_id=election.runoff_of_id,
                extension_hours=election.extension_hours,
            ).on_conflict_do_nothing().returning(ElectionRow)
        )
        return _election(row, None, None) if row is not None else None

    async def update_election(self, election: Election) -> None:
        row = (
            await self._session.scalars(
                select(ElectionRow).where(
                    ElectionRow.id == election.id, ElectionRow.guild_id == election.guild_id
                )
            )
        ).one()
        previous = ElectionStatus(row.status)
        if previous != election.status:
            await self._session.execute(
                update(ElectionRow).where(ElectionRow.id == election.id).values(
                    status=election.status.value, extended_until=election.extended_until
                )
            )
        if election.protocol is not None and previous in (ElectionStatus.OPEN, ElectionStatus.EXTENDED):
            protocol = election.protocol
            await self._session.execute(insert(ElectionProtocolRow).values(
                guild_id=election.guild_id, election_id=election.id,
                round_number=1 if previous is ElectionStatus.OPEN else 2,
                source_url=protocol.source_url, participants=protocol.participants,
                tallies=[
                    [item.candidate_citizen_id, item.votes, item.candidate_discord_user_id]
                    for item in protocol.tallies
                ],
                winners=list(protocol.winners), recorded_by_citizen_id=protocol.recorded_by_citizen_id,
                recorded_at=protocol.recorded_at, reason=protocol.reason,
            ))
        if election.certified_by_citizen_id is not None:
            await self._session.execute(insert(ElectionCertificationRow).values(
                guild_id=election.guild_id, election_id=election.id,
                certified_by_citizen_id=election.certified_by_citizen_id,
                certified_at=election.certified_at, reason=election.certification_reason,
            ))

    async def apply_election(self, election: Election) -> tuple[OfficeTerm, ...]:
        assert election.certified_at is not None and election.protocol is not None
        await self._session.scalar(
            select(GuildRow.id).where(GuildRow.id == election.guild_id).with_for_update(key_share=True)
        )
        latest_end = await self._session.scalar(
            select(func.max(PublicOfficeRow.ends_at)).where(
                PublicOfficeRow.guild_id == election.guild_id, PublicOfficeRow.office == election.office.value
            )
        )
        starts_at = max(election.certified_at, latest_end) if latest_end is not None else election.certified_at
        ends_at = starts_at + timedelta(days=election.term_days)
        await self.update_election(election)
        await self._session.execute(insert(PublicOfficeRow), [
            {
                "guild_id": election.guild_id, "election_id": election.id, "office": election.office.value,
                "seat": seat, "holder_citizen_id": citizen_id, "starts_at": starts_at, "ends_at": ends_at,
            }
            for seat, citizen_id in enumerate(election.protocol.winners, start=1)
        ])
        return await self.list_election_terms(election.guild_id, election.id)

    async def list_election_terms(self, guild_id: int, election_id: int) -> tuple[OfficeTerm, ...]:
        rows = await self._session.execute(
            select(PublicOfficeRow, CitizenRow.discord_user_id)
            .join(CitizenRow, CitizenRow.id == PublicOfficeRow.holder_citizen_id)
            .where(PublicOfficeRow.guild_id == guild_id, PublicOfficeRow.election_id == election_id)
            .order_by(PublicOfficeRow.seat)
        )
        return tuple(_term(row, int(user_id)) for row, user_id in rows)

    async def list_offices(self, guild_id: int, at: datetime) -> tuple[OfficeTerm, ...]:
        rows = await self._session.execute(
            select(PublicOfficeRow, CitizenRow.discord_user_id)
            .join(CitizenRow, CitizenRow.id == PublicOfficeRow.holder_citizen_id)
            .where(
                PublicOfficeRow.guild_id == guild_id,
                PublicOfficeRow.starts_at <= at, PublicOfficeRow.ends_at > at,
                CitizenRow.status == CitizenStatus.VERIFIED.value,
            ).order_by(PublicOfficeRow.office, PublicOfficeRow.seat)
        )
        return tuple(_term(row, int(user_id)) for row, user_id in rows)

    async def has_active_office(
        self, guild_id: int, citizen_id: int, office: OfficeKind, at: datetime
    ) -> bool:
        active = select(PublicOfficeRow.id).join(CitizenRow, CitizenRow.id == PublicOfficeRow.holder_citizen_id).where(
            PublicOfficeRow.guild_id == guild_id, PublicOfficeRow.holder_citizen_id == citizen_id,
            PublicOfficeRow.office == office.value, PublicOfficeRow.starts_at <= at, PublicOfficeRow.ends_at > at,
            CitizenRow.status == CitizenStatus.VERIFIED.value,
        )
        return bool(await self._session.scalar(select(active.exists())))

    async def list_office_role_assignments(
        self, discord_guild_id: int, at: datetime
    ) -> tuple[OfficeRoleAssignment, ...]:
        rows = await self._session.execute(
            select(
                PublicOfficeRow.office, CitizenRow.discord_user_id,
                func.bool_or(
                    (PublicOfficeRow.starts_at <= at) & (PublicOfficeRow.ends_at > at)
                    & (CitizenRow.status == CitizenStatus.VERIFIED.value)
                ),
            )
            .join(CitizenRow, CitizenRow.id == PublicOfficeRow.holder_citizen_id)
            .join(GuildRow, GuildRow.id == PublicOfficeRow.guild_id)
            .where(GuildRow.discord_guild_id == Decimal(discord_guild_id))
            .group_by(PublicOfficeRow.office, CitizenRow.discord_user_id)
            .order_by(PublicOfficeRow.office, CitizenRow.discord_user_id)
        )
        return tuple(OfficeRoleAssignment(OfficeKind(office), int(user_id), active) for office, user_id, active in rows)

    async def list_results(self, guild_id: int, limit: int) -> tuple[Election, ...]:
        rows = (
            await self._session.scalars(
                select(ElectionRow)
                .join(ElectionCertificationRow, ElectionCertificationRow.election_id == ElectionRow.id)
                .where(ElectionRow.guild_id == guild_id)
                .order_by(ElectionCertificationRow.certified_at.desc(), ElectionRow.id.desc()).limit(limit)
            )
        ).all()
        return tuple([await self._election_with_evidence(row) for row in rows])


class PostgresGovernmentUnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[PostgresGovernmentRepository]:
        async with self._sessions.begin() as session:
            yield PostgresGovernmentRepository(session)
