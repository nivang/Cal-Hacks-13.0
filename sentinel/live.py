"""What Sentinel is checking right now, so the Live page can show a pay call or a search while it is still running.

payments.py opens a check with watching() when a pay call or a search preview starts and moves it along with step()
("Policy guard", "Asking the judge", "Locking the money in escrow", ...). The check is deleted when the call ends,
whatever the outcome; the attempt row then tells the rest. Each check lives in a context variable, so concurrent
previews (catalog.find runs several at once, one asyncio task each) never mix theirs up.

Progress only, never a decision: every write is best effort inside a savepoint, so a failure here can neither stop a
payment nor break the caller's transaction. Rows not updated for STALE_S are ignored (and cleared by the next check).
Amounts are integer atomic units.
"""
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sentinel import db

log = logging.getLogger(__name__)

STALE_S = 120
_current: ContextVar[tuple[db.Connection, int] | None] = ContextVar("live_check", default=None)


def _write(conn: db.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    """Run one progress write in a savepoint; None when it fails (logged, never raised)."""
    try:
        with conn.transaction():
            cursor = conn.execute(sql, params)
            return cursor.fetchone() if "RETURNING" in sql else None
    except Exception:
        log.debug("live check write failed", exc_info=True)
        return None


@contextmanager
def watching(conn: db.Connection, agent: str, mandate_id: int | None, url: str, stage: str,
             kind: str = "pay") -> Iterator[None]:
    """Show one check on the Live page for the length of the with block, starting at stage."""
    row = _write(
        conn,
        "WITH gone AS (DELETE FROM live_checks WHERE updated_at < now() - make_interval(secs => %s))"
        " INSERT INTO live_checks (agent_id, mandate_id, kind, url, stages)"
        " SELECT id, %s, %s, %s, ARRAY[%s] FROM agents WHERE name = %s RETURNING id",
        (STALE_S, mandate_id, kind, url, stage, agent),
    )
    token = _current.set((conn, int(row["id"])) if row else None)
    try:
        yield
    finally:
        _current.reset(token)
        if row:
            _write(conn, "DELETE FROM live_checks WHERE id = %s", (row["id"],))


def step(stage: str, *, pay_to: str | None = None, amount: int | None = None,
         attempt_id: int | None = None) -> None:
    """Move the current check (if any) to its next stage, filling in what is known by now."""
    current = _current.get()
    if current is None:
        return
    conn, check_id = current
    _write(
        conn,
        "UPDATE live_checks SET stages = array_append(stages, %s), pay_to = COALESCE(%s, pay_to),"
        " amount = COALESCE(%s, amount), attempt_id = COALESCE(%s, attempt_id), updated_at = now() WHERE id = %s",
        (stage, pay_to, amount, attempt_id, check_id),
    )


def current(conn: db.Connection, agent_id: int, mandate_id: int | None) -> list[dict[str, Any]]:
    """This agent's checks in progress under this task (or every one, for None), newest first. amount atomic."""
    return conn.execute(
        "SELECT id, kind, url, pay_to, amount, stages, attempt_id, started_at, updated_at FROM live_checks"
        " WHERE agent_id = %s AND (%s::bigint IS NULL OR mandate_id = %s::bigint)"
        " AND updated_at >= now() - make_interval(secs => %s) ORDER BY id DESC",
        (agent_id, mandate_id, mandate_id, STALE_S),
    ).fetchall()
