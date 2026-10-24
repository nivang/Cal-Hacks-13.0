"""Shared pytest fixtures. Database tests run in the "test" schema of the Supabase database.

The schema is built once per session from migrations/*.sql (never dropped). Each test then runs
inside a transaction that is rolled back at the end, so tests cannot see each other's rows and the
ledger tables (which reject deletes by design) are never left dirty. Needs DATABASE_URL in .env.
"""
import os
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv

import db

load_dotenv()

TEST_SCHEMA = "test"
NETWORK = "eip155:84532"
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DATACO = "0x290E84b17Ce32cF9403f5D0218cB50ccdcc94781"
SCAMMER = "0xSca000000000000000000000000000000000dead"


@pytest.fixture(scope="session")
def test_schema() -> Iterator[str]:
    """Create the "test" schema, its tables and seed rows if they do not exist yet, then hand out its name."""
    if not os.environ.get("DATABASE_URL"):
        pytest.fail("DATABASE_URL is not set; database tests need the Supabase connection string in .env")
    with db.connection() as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        with db.transaction(c):
            db.use_schema(c, TEST_SCHEMA)
            db.apply_migrations(c)
            db.init(c)
            db.seed_sellers(
                c, [{"name": "DataCo", "pay_to": DATACO, "network": NETWORK, "asset": ASSET, "status": "approved"}]
            )
    yield TEST_SCHEMA
    db.close_pool()


@pytest.fixture
def conn(test_schema: str) -> Iterator[db.Connection]:
    """A connection inside a transaction that is rolled back after the test, pointed at the test schema."""
    with db.connection() as c, db.transaction(c, rollback=True):
        db.use_schema(c, test_schema)
        yield c


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Run async tests on asyncio only (the trio backend is not installed)."""
    return str(request.param)
