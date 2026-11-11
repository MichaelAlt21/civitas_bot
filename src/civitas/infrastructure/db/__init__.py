from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .citizens import PostgresCitizenUnitOfWork

__all__ = ["PostgresCitizenUnitOfWork", "create_db_engine"]


def create_db_engine(database_url: str | URL) -> AsyncEngine:
    return create_async_engine(
        database_url,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        hide_parameters=True,
    )
