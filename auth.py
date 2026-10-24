"""Agent identity: issue bearer tokens and map a presented token back to the agent it belongs to.

A token is random, shown once when issued, and stored only as a sha256 hash in agents.token_hash. Both MCP
transports (stdio and HTTP) verify tokens here, so who is asking never depends on a label the agent chose for
itself. Used by mcp_server.py. Run `python auth.py issue research-agent` to create or rotate a token.
"""
import hashlib
import secrets
import sys
from pathlib import Path

from db import Connection

TOKEN_PREFIX = "snt_"
ENV_VAR = "SENTINEL_AGENT_TOKEN"


def hash_token(token: str) -> str:
    """Return the sha256 hex digest of a token. Tokens are 256 bits of randomness, so a plain hash is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(conn: Connection, agent: str) -> str:
    """Create a new token for an agent, replacing any old one, and return it. This is the only time it is visible.

    Raises LookupError if the agent does not exist.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    updated = conn.execute("UPDATE agents SET token_hash = %s WHERE name = %s", (hash_token(token), agent)).rowcount
    if updated == 0:
        raise LookupError(f"no agent named {agent}")
    return token


def agent_for_token(conn: Connection, token: str) -> str | None:
    """Return the name of the agent that owns this token, or None if it is not a valid token."""
    if not token.startswith(TOKEN_PREFIX):
        return None  # cheap reject before touching the database
    row = conn.execute("SELECT name FROM agents WHERE token_hash = %s", (hash_token(token),)).fetchone()
    return str(row["name"]) if row else None


def write_env_var(path: Path, name: str, value: str) -> None:
    """Set name=value in an env file, replacing a line or appending one. Keeps the token out of the terminal."""
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{name}=")]
    path.write_text("\n".join([*kept, f"{name}={value}"]) + "\n")


if __name__ == "__main__":
    from dotenv import load_dotenv

    import db

    load_dotenv()
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "issue":
        sys.exit("usage: python auth.py issue <agent> [--env-file .env]  (token is shown once, or written to the file)")
    with db.connection() as c:
        new_token = issue_token(c, args[1])
    db.close_pool()
    if "--env-file" in args:
        target = Path(args[args.index("--env-file") + 1])
        write_env_var(target, ENV_VAR, new_token)
        print(f"token for {args[1]} written to {target} as {ENV_VAR}")
    else:
        print(new_token)
