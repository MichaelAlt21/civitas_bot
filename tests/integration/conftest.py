import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateSchema, DropSchema

from civitas.infrastructure.db import create_db_engine


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


def _upgrade(connection: Connection) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    database_url = os.getenv("CIVITAS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set CIVITAS_TEST_DATABASE_URL for the PostgreSQL integration tests")
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        pytest.fail("CIVITAS_TEST_DATABASE_URL must use postgresql+psycopg")

    schema = f"civitas_test_{uuid4().hex}"
    admin_engine = create_db_engine(url)
    test_engine = create_db_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        try:
            async with test_engine.begin() as connection:
                await connection.run_sync(_upgrade)
            yield test_engine
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
    finally:
        await admin_engine.dispose()
