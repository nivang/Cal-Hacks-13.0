"""Tests for sentinel/live.py, the Live page's checks in progress, on a fake connection (no database).

A check opens and closes around the with block, steps reach only the check of the current context, nothing happens
without one, and a failing write never reaches the caller: progress must never stop a payment.
"""
from contextlib import contextmanager

import pytest

from sentinel import live


class FakeConn:
    def __init__(self, fail=False):
        self.sql, self.fail = [], fail

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql, params=()):
        if self.fail:
            raise RuntimeError("database down")
        self.sql.append((sql.split()[0], params))
        return self

    def fetchone(self):
        return {"id": 7}


def test_a_check_opens_steps_and_closes():
    conn = FakeConn()
    with live.watching(conn, "user-a", 3, "http://seller.test/x", "Asking the seller for its price"):
        live.step("Policy guard", pay_to="0xabc", amount=500_000)
    kinds = [kind for kind, _ in conn.sql]
    assert kinds == ["WITH", "UPDATE", "DELETE"]
    assert conn.sql[1][1][:3] == ("Policy guard", "0xabc", 500_000)
    assert conn.sql[2][1] == (7,)


def test_a_step_outside_a_check_does_nothing():
    live.step("Policy guard")  # no check open in this context: no connection, no error


def test_a_failing_write_never_reaches_the_payment():
    conn = FakeConn(fail=True)
    with live.watching(conn, "user-a", None, "http://seller.test/x", "Checking a listing", "search"):
        live.step("Policy guard")
        answer = 42
    assert answer == 42


def test_the_check_is_closed_even_when_the_payment_raises():
    conn = FakeConn()
    with pytest.raises(ValueError), live.watching(conn, "user-a", 3, "http://seller.test/x", "start"):
        raise ValueError("blocked")
    assert conn.sql[-1][0] == "DELETE"
    live.step("after")  # the context was reset
    assert conn.sql[-1][0] == "DELETE"
