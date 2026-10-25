"""Actual independent Postgres transactions race on one-time approval and budget admission."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from sentinel import db, guard, link_approvals, payments

QUOTE = guard.Quote("http://seller.test/new", "0x" + "a" * 40, 500_000,
                    "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "eip155:84532", 300)
AGENT = "research-agent"
REASON = "Email benchmarks for the Q3 report."


@pytest.fixture
def race_schema(test_schema):
    # Separate committed schema: rollback-only fixtures cannot expose rows to independent connections.
    schema = "approval_race_" + uuid.uuid4().hex
    with db.connection() as conn, db.transaction(conn):
        conn.execute(f'CREATE SCHEMA "{schema}"')
        db.use_schema(conn, schema)
        db.apply_migrations(conn)
        db.init(conn)
    try:
        yield schema
    finally:
        with db.connection() as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("same_grant", [True, False])
def test_concurrent_calls_admit_only_one_payment(race_schema, same_grant):
    request_id = None
    with db.connection() as conn, db.transaction(conn):
        db.use_schema(conn, race_schema)
        if same_grant:
            with pytest.raises(payments.ApprovalRequired) as caught:
                payments.authorize(conn, AGENT, REASON, QUOTE, interactive=True)
            request_id = caught.value.attempt_id
            link_approvals.decide(conn, AGENT, request_id, "human", "yes")
        else:
            db.seed_sellers(conn, [{"name": "RaceCo", "pay_to": QUOTE.pay_to, "asset": QUOTE.asset,
                                   "network": QUOTE.network, "status": "approved"}])
            conn.execute("UPDATE agents SET weekly_budget = 0.5 WHERE name = %s", (AGENT,))
    gate = Barrier(2)

    def run(index):
        with db.connection() as conn, db.transaction(conn):
            db.use_schema(conn, race_schema)
            conn.execute("SET LOCAL lock_timeout = '10s'")
            gate.wait(timeout=10)
            quote = QUOTE if same_grant else replace(QUOTE, url=f"{QUOTE.url}/{index}")
            try:
                payments.authorize(conn, AGENT, REASON, quote, approval_id=request_id)
                return "admitted"
            except payments.PaymentRejected as exc:
                return exc.rule

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(run, [1, 2]))
    assert sorted(outcomes) == sorted(["admitted", "approval" if same_grant else "budget"])
    with db.connection() as conn, db.transaction(conn):
        db.use_schema(conn, race_schema)
        assert conn.execute("SELECT count(*) AS n FROM payment_attempts WHERE status = 'approved'").fetchone()["n"] == 1
