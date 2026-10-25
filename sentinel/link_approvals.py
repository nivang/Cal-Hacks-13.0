"""Durable purchase approvals. Only authenticated human channels call decide().

Pending approvals hold a duplicate lock, but do not reserve budget. Budget is
checked under the agent lock when an authorization is consumed by payments.py.
No approval adds a seller or overrides a blocked seller or spending rule.
"""
from typing import Any

from sentinel import db

WAITING = ("pending", "authorized")
MAX_WAIT_S = 300


def expire(conn: db.Connection, agent: str) -> None:
    """Release abandoned requests, using the database clock rather than the client's."""
    conn.execute(
        "UPDATE payment_attempts SET status = 'expired', rule_fired = 'approval: Approval expired.'"
        " WHERE agent_id = (SELECT id FROM agents WHERE name = %s)"
        " AND status = ANY(%s) AND approval_expires_at <= clock_timestamp()",
        (agent, list(WAITING)),
    )


def get(conn: db.Connection, agent: str, request_id: int) -> dict[str, Any] | None:
    """Read only this agent's approval; include the immutable quote snapshot."""
    expire(conn, agent)
    return conn.execute(
        "SELECT p.*, (p.amount * 1000000)::bigint AS atomic_amount,"
        " GREATEST(EXTRACT(EPOCH FROM p.approval_expires_at - clock_timestamp()), 0) AS seconds_left"
        " FROM payment_attempts p"
        " JOIN agents a ON a.id = p.agent_id WHERE p.id = %s AND a.name = %s",
        (request_id, agent),
    ).fetchone()


def decide(conn: db.Connection, agent: str, request_id: int, approver: str, choice: str) -> dict[str, Any] | None:
    """Record one human decision, atomically. Repeated clicks cannot authorize again.

    Callers derive agent and approver from a verified session or MCP identity;
    neither comes from a form or tool argument. No agent-callable approval tool exists.
    """
    if choice not in ("yes", "no", "cancel"):
        raise ValueError("Choose Yes or No.")
    with db.transaction(conn):
        conn.execute("SELECT id FROM agents WHERE name = %s FOR UPDATE", (agent,)).fetchone()
        expire(conn, agent)
        row = get(conn, agent, request_id)
        if row is None or row["status"] != "pending":
            return row
        status = {"yes": "authorized", "no": "denied", "cancel": "cancelled"}[choice]
        conn.execute(
            "INSERT INTO approvals (attempt_id, approver, decision, note) VALUES (%s, %s, %s, %s)",
            (request_id, approver, "approved" if choice == "yes" else "denied", f"One purchase: {choice}"),
        )
        conn.execute(
            "UPDATE payment_attempts SET status = %s, rule_fired = %s WHERE id = %s",
            (status, f"approval: User chose {choice} for this purchase only.", request_id),
        )
        return get(conn, agent, request_id)


def invalidate(conn: db.Connection, agent: str, request_id: int, detail: str) -> None:
    """A disappeared or unsupported quote cannot leave a reusable authorization behind."""
    with db.transaction(conn):
        conn.execute("SELECT id FROM agents WHERE name = %s FOR UPDATE", (agent,)).fetchone()
        conn.execute(
            "UPDATE payment_attempts SET status = 'blocked', decision = 'BLOCK', rule_fired = %s"
            " WHERE id = %s AND agent_id = (SELECT id FROM agents WHERE name = %s) AND status = 'authorized'",
            (f"quote_changed: {detail}", request_id, agent),
        )
