import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from civitas.infrastructure.db import (  # noqa: F401
    asset_models, government_models, jobs_models, ledger_models, market_models,
    private_job_models, report_models, treasury_models,
)
from civitas.infrastructure.db.models import Base


def _database_url() -> str:
    url = os.getenv("CIVITAS_DATABASE_URL")
    if not url:
        raise RuntimeError("Set CIVITAS_DATABASE_URL before running migrations")
    return url


def _run_online(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(
            url=_database_url(), target_metadata=Base.metadata, literal_binds=True
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run_online(connection)
        return

    engine = create_engine(
        _database_url(), poolclass=pool.NullPool, hide_parameters=True
    )
    try:
        with engine.connect() as connection:
            _run_online(connection)
    finally:
        engine.dispose()


run_migrations()
