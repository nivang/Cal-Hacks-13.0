"""Sentinel: the MCP server that holds the wallet, guards every payment and keeps the books."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"  # loaded by absolute path so the server works from any working directory (Claude Desktop)
