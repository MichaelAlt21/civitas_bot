from sqlalchemy import event
from sqlalchemy.engine import ExceptionContext, URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from civitas.domain.ledger import LedgerError, LedgerErrorReason

from .citizens import PostgresCitizenUnitOfWork

__all__ = ["PostgresCitizenUnitOfWork", "create_db_engine"]


def create_db_engine(database_url: str | URL) -> AsyncEngine:
    engine = create_async_engine(
        database_url,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        hide_parameters=True,
    )
    event.listen(engine.sync_engine, "handle_error", _translate_economy_read_only)
    return engine


def _translate_economy_read_only(context: ExceptionContext) -> LedgerError | None:
    if getattr(context.original_exception, "sqlstate", None) == "CV001":
        return LedgerError(LedgerErrorReason.ECONOMY_READ_ONLY)
    return None
