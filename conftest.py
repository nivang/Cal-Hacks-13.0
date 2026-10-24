"""Shared pytest fixtures: a fresh database per test with the sellers from the demo."""
import sqlite3

import pytest

import db

NETWORK = "eip155:84532"
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DATACO = "0x290E84b17Ce32cF9403f5D0218cB50ccdcc94781"
SCAMMER = "0xSca000000000000000000000000000000000dead"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """A temporary database with the chart of accounts, research-agent, and DataCo approved."""
    c = db.connect(str(tmp_path / "test.db"))
    db.init(c)
    db.seed_sellers(c, [{"name": "DataCo", "pay_to": DATACO, "network": NETWORK, "asset": ASSET, "status": "approved"}])
    return c
