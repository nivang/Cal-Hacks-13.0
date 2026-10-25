"""Users of the sign in site: their rows, their own wallet and books, and the identity an MCP call carries.

A user is one row in users, keyed by the lowercase email_key. The first sign in gives them a CDP server account
named by their uuid (wallets.open_user_wallet with create=True), then, in one transaction, their own agent
"user-<uuid>" and cash account "1010-<agent id>" ("USDC Cash <address>"), with the default limits and the shared
6100 expense account. A user has a wallet and books together or neither, so nothing is written unless CDP answered.

Identity is what a valid OAuth access token resolves to: the user, their agent and their wallet address. oauth.py
puts it into the token claims and mcp_server.py reads it back with Identity.from_claims, which refuses anything
malformed. The site (web/app.py) uses the rest: find_or_create_user on sign in, session_user to check a session's
epoch, account_view for the account page and sign_out_everywhere to end every session and grant at once.

This module does not import web/auth.py; sessions reach it as plain (user id, epoch) values.
"""
import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from typing import Any

from x402.mechanisms.evm import ClientEvmSigner

from sentinel import attempts, db, guard, wallets

AGENT_PREFIX = "user-"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")  # always used with fullmatch: "$" alone lets a trailing newline in
EXPENSE_ACCOUNT = "6100"  # every user's payments are research data expense, as for research-agent
ATOMIC = "* 1000000"  # SQL: whole USDC (numeric) to integer atomic units

Connect = Callable[[], AbstractContextManager[db.Connection]]
OpenSigner = Callable[..., AbstractAsyncContextManager[ClientEvmSigner]]  # open_user_wallet(user_id, *, create)

log = logging.getLogger(__name__)

USER_COLUMNS = (
    "id::text AS id, email, email_key, session_epoch, wallet_address, agent_id, wallet_created_at, created_at,"
    " last_sign_in_at"
)


def canonical_id(user_id: object) -> str | None:
    """Return user_id if it is a uuid in canonical lowercase form, else None.

    Checked before any SQL, so a malformed id is simply not found instead of a cast error that would abort the
    surrounding transaction.
    """
    if not isinstance(user_id, str):
        return None
    try:
        return user_id if str(uuid.UUID(user_id)) == user_id else None
    except ValueError:
        return None


def _require_id(user_id: str) -> str:
    """Return the canonical user id, or raise ValueError (without echoing the value, in case it is an email)."""
    uid = canonical_id(user_id)
    if uid is None:
        raise ValueError("A user id is a uuid in canonical lowercase form.")
    return uid


def agent_name(user_id: str) -> str:
    """The agent name for a user: "user-<uuid>". No email ever goes into a name, key or log."""
    return AGENT_PREFIX + _require_id(user_id)


@dataclass(frozen=True)
class Identity:
    """Who an MCP call acts for: the user, their agent and their wallet address, as an access token resolves them."""

    user_id: str  # canonical lowercase uuid string
    agent: str  # "user-<user_id>"
    wallet: str  # 0x address as stored

    def claims(self, issuer: str) -> dict[str, str]:
        """The token claims for this identity, as oauth.py puts them on an access token."""
        return {"iss": issuer, "sub": self.user_id, "agent": self.agent, "wallet": self.wallet}

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any] | None) -> "Identity":
        """Rebuild the identity from token claims, or raise ValueError when anything is missing or malformed.

        sub must be a canonical uuid, agent exactly "user-" + sub, and wallet a 0x address of 40 hex digits.
        """
        if not isinstance(claims, Mapping) or not claims:
            raise ValueError("no identity claims")
        sub, agent, wallet = claims.get("sub"), claims.get("agent"), claims.get("wallet")
        uid = canonical_id(sub)
        if uid is None:
            raise ValueError("the sub claim is not a user id")
        if agent != AGENT_PREFIX + uid:
            raise ValueError("the agent claim does not belong to the user")
        if not isinstance(wallet, str) or not ADDRESS_RE.fullmatch(wallet):
            raise ValueError("the wallet claim is not an address")
        return cls(uid, agent, wallet)


def find_or_create_user(conn: db.Connection, email: str, email_key: str) -> dict[str, Any]:
    """Return the user with this email key, creating it on first sign in, and record the sign in time.

    An existing user keeps the address on file (mail goes there); only last_sign_in_at changes. The key is
    compared lowercase, so a caller that forgot to lower it still finds the same user.
    """
    row = conn.execute(
        "INSERT INTO users (email, email_key, last_sign_in_at) VALUES (%s, %s, now())"
        " ON CONFLICT (email_key) DO UPDATE SET last_sign_in_at = now()"
        " RETURNING id::text AS id, email, email_key, session_epoch, wallet_address, agent_id",
        (email, email_key.lower()),
    ).fetchone()
    if row is None:
        raise RuntimeError("upsert returned no user")
    return row


def stored_email(conn: db.Connection, email_key: str) -> str | None:
    """The address on file for an existing user with this key (sign in mail goes there), or None."""
    row = conn.execute("SELECT email FROM users WHERE email_key = %s", (email_key.lower(),)).fetchone()
    return row["email"] if row else None


def load_user(conn: db.Connection, user_id: str) -> dict[str, Any] | None:
    """Return the user's row (id as text), or None when the id is malformed or unknown."""
    uid = canonical_id(user_id)
    if uid is None:
        return None
    return conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s", (uid,)).fetchone()


def session_user(conn: db.Connection, user_id: str, epoch: int) -> dict[str, Any] | None:
    """Return the user a session names if it exists and the session's epoch is current, else None.

    "Sign out everywhere" bumps the epoch, so every session signed before it stops here.
    """
    uid = canonical_id(user_id)
    if uid is None or not isinstance(epoch, int) or isinstance(epoch, bool):
        return None
    return conn.execute(
        f"SELECT {USER_COLUMNS} FROM users WHERE id = %s AND session_epoch = %s", (uid, epoch)
    ).fetchone()


def create_books(conn: db.Connection, user_id: str, address: str) -> int:
    """Give the user their agent and cash account and store their wallet address, once. Returns the agent id.

    Runs in one transaction with the user's row locked, so a retry or a second tab waits and then finds the books
    already made: the same address returns the existing agent id, a different one raises RuntimeError (a user has
    exactly one wallet). Otherwise inserts agent "user-<id>" with the default limits and the 6100 expense account,
    its cash account "1010-<agent id>" named "USDC Cash <address>", and links them. ValueError for a malformed id
    or address, LookupError for an unknown user.
    """
    uid = _require_id(user_id)
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
        raise ValueError("A wallet address is 0x followed by 40 hex digits.")
    with db.transaction(conn):
        user = conn.execute(
            "SELECT agent_id, wallet_address FROM users WHERE id = %s FOR UPDATE", (uid,)
        ).fetchone()
        if user is None:
            raise LookupError(f"no user {uid}")
        if user["wallet_address"] is not None:
            if user["wallet_address"].lower() != address.lower():
                raise RuntimeError(f"user {uid} already has a different wallet")
            return int(user["agent_id"])
        aid = db.insert(
            conn,
            "INSERT INTO agents (name, per_payment_cap, weekly_budget, expense_account_id) VALUES (%s, %s, %s, %s)",
            (
                agent_name(uid), db.usdc(db.DEFAULT_PER_PAYMENT_CAP), db.usdc(db.DEFAULT_WEEKLY_BUDGET),
                db.account_id(conn, EXPENSE_ACCOUNT),
            ),
        )
        cash_id = db.insert(
            conn,
            "INSERT INTO accounts (code, name, type) VALUES (%s, %s, 'asset')",
            (f"1010-{aid}", f"USDC Cash {address}"),
        )
        conn.execute("UPDATE agents SET cash_account_id = %s WHERE id = %s", (cash_id, aid))
        conn.execute(
            "UPDATE users SET agent_id = %s, wallet_address = %s, wallet_created_at = now() WHERE id = %s",
            (aid, address, uid),
        )
    log.info("created books for user %s: agent %s", uid, aid)
    return aid


def _load_in_thread(connect: Connect, user_id: str) -> dict[str, Any] | None:
    """load_user on its own connection (runs in a worker thread)."""
    with connect() as conn:
        return load_user(conn, user_id)


def _books_in_thread(connect: Connect, user_id: str, address: str) -> str:
    """create_books on its own connection (runs in a worker thread); returns the stored address."""
    with connect() as conn:
        create_books(conn, user_id, address)
        user = load_user(conn, user_id)
    if user is None or user["wallet_address"] is None:
        raise RuntimeError(f"user {user_id} has no wallet after create_books")
    return str(user["wallet_address"])


async def ensure_wallet(connect: Connect, user_id: str, open_signer: OpenSigner = wallets.open_user_wallet) -> str:
    """Make sure the user has a CDP wallet and books, creating both on first sign in. Returns the wallet address.

    Fast path: the stored address, with no CDP call. Otherwise gets or creates the CDP account named by the user's
    uuid, with no database connection held while CDP answers, then writes the books in a worker thread. Any CDP
    failure or timeout propagates and nothing is written; the caller keeps the user signed in and offers a retry.
    ValueError for a malformed id, LookupError for an unknown user (both before any CDP call).
    """
    uid = _require_id(user_id)
    user = await asyncio.to_thread(_load_in_thread, connect, uid)
    if user is None:
        raise LookupError(f"no user {uid}")
    if user["wallet_address"] is not None:
        return str(user["wallet_address"])
    async with open_signer(uid, create=True) as signer:
        address = str(signer.address)
    return await asyncio.to_thread(_books_in_thread, connect, uid, address)


def identity_for(conn: db.Connection, user_id: str) -> Identity | None:
    """The identity an access token for this user carries, or None until the wallet and books exist.

    Requires the user's agent to be "user-<id>" with its own cash account, so a half built or foreign agent never
    becomes an identity.
    """
    uid = canonical_id(user_id)
    if uid is None:
        return None
    row = conn.execute(
        "SELECT u.wallet_address, a.name FROM users u JOIN agents a ON a.id = u.agent_id"
        " WHERE u.id = %s AND a.cash_account_id IS NOT NULL",
        (uid,),
    ).fetchone()
    if row is None or row["name"] != agent_name(uid) or not ADDRESS_RE.fullmatch(row["wallet_address"] or ""):
        return None
    return Identity(uid, row["name"], row["wallet_address"])


def account_view(conn: db.Connection, user_id: str, include_spend: bool = True) -> dict[str, Any]:
    """What the account pages show: email, wallet_address, agent, agent_id, per_payment_cap, weekly_budget, week_spend.

    Amounts are atomic units; week_spend counts settled and in flight payments over the guard's budget window.
    Lazy sections can skip the spending query with include_spend=False.
    Before the wallet exists wallet_address, agent, agent_id and the limits are None and week_spend is 0.
    LookupError for an unknown user.
    """
    uid = canonical_id(user_id)
    row = None
    if uid is not None:
        row = conn.execute(
            f"SELECT u.email, u.wallet_address, a.id AS aid, a.name AS agent,"
            f" (a.per_payment_cap {ATOMIC})::bigint AS per_payment_cap,"
            f" (a.weekly_budget {ATOMIC})::bigint AS weekly_budget"
            " FROM users u LEFT JOIN agents a ON a.id = u.agent_id WHERE u.id = %s",
            (uid,),
        ).fetchone()
    if row is None:
        raise LookupError("no such user")
    aid = row.pop("aid")
    row["agent_id"] = aid
    row["week_spend"] = (
        attempts.week_spend(conn, aid, guard.POLICY["budget_window_days"])
        if aid is not None and include_spend else 0
    )
    return row


def sign_out_everywhere(conn: db.Connection, user_id: str) -> None:
    """End every session and every grant of this user at once, in one transaction.

    Revokes each of the user's grants not revoked yet (all their tokens stop with it) and bumps session_epoch, so
    every session cookie signed before now is refused. Other users are untouched. LookupError for an unknown user.
    """
    uid = _require_id(user_id)
    with db.transaction(conn):
        bumped = conn.execute(
            "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s RETURNING id", (uid,)
        ).fetchone()
        if bumped is None:
            raise LookupError(f"no user {uid}")
        conn.execute(
            "UPDATE oauth_codes SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (uid,)
        )
    log.info("signed out everywhere: user %s", uid)
