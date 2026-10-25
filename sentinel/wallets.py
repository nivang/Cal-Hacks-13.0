"""The wallet each payment signs with: one CDP server account per user, behind the one interface payments need.

payments.py only ever sees an x402 ClientEvmSigner: an address plus sign_typed_data. This module decides which
wallet provides it. Every user has their own CDP server account, named by the user's uuid (users.id). A uuid is 36
characters, so it fits CDP's rule that a name is 2 to 36 characters, and it can never be the shared "sentinel" test
account. The key stays inside Coinbase and every signature is a call to CDP's signing endpoint, so it never enters
this process.

open_user_wallet(user_id) is how production code opens a wallet, and the only way. A pay call only looks the
account up (create=False), so paying can never mint an empty wallet; only the first sign in creates one
(create=True). There is no default wallet: a call that cannot name its user has nothing to sign with. Scripts outside
this package that need a fixed test account open it by name through cdp_wallet.

Adding another wallet means one opener here and one entry in WALLETS; the guard, the ledger and the MCP tools do
not change. Pick one with SENTINEL_WALLET (default cdp, the only one built in).
"""
import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from x402.mechanisms.evm import ClientEvmSigner, EthAccountSigner

ENV_VAR = "SENTINEL_WALLET"
DEFAULT = "cdp"
CDP_ENV = ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET")
CDP_TIMEOUT_S = 15  # an account lookup or creation slower than this fails instead of holding the call

Opener = Callable[[str, bool], AbstractAsyncContextManager[ClientEvmSigner]]  # (account name, create)


@asynccontextmanager
async def cdp_wallet(name: str, create: bool = False) -> AsyncIterator[ClientEvmSigner]:
    """Open the CDP server account with this name and yield it as an x402 signer.

    Only looks the account up, so a missing one is an error; create=True gets or creates it instead (first sign
    in, and test scripts). The lookup is bounded by CDP_TIMEOUT_S. The CDP client stays open while the caller holds
    the wallet, since every signature is a CDP call. Keys come from the CDP_* env vars; only this process holds them.
    """
    async with CdpClient() as cdp:
        async with asyncio.timeout(CDP_TIMEOUT_S):
            if create:
                account = await cdp.evm.get_or_create_account(name=name)
            else:
                account = await cdp.evm.get_account(name=name)
        yield EthAccountSigner(EvmLocalAccount(account))


# name -> (opener, env vars it needs). A new wallet is one entry here.
WALLETS: dict[str, tuple[Opener, tuple[str, ...]]] = {"cdp": (cdp_wallet, CDP_ENV)}


def selected() -> str:
    """Return the wallet named by SENTINEL_WALLET (cdp when unset), or raise ValueError listing the choices."""
    name = os.environ.get(ENV_VAR) or DEFAULT
    if name not in WALLETS:
        raise ValueError(f"{ENV_VAR}={name} is not a known wallet; choose from: {', '.join(sorted(WALLETS))}")
    return name


def required_env() -> tuple[str, ...]:
    """The env vars the selected wallet needs, so a server can refuse to start without them."""
    return WALLETS[selected()][1]


def _account_name(user_id: str | uuid.UUID) -> str:
    """Return the account name for a user: their uuid as its canonical lowercase 36 character string.

    Raises ValueError for anything else, including an uppercase, braced or unhyphenated uuid and "sentinel", so
    exactly one name can ever reach a user's wallet. The value is left out of the message, in case it is an email.
    """
    if isinstance(user_id, uuid.UUID):
        return str(user_id)
    if isinstance(user_id, str):
        try:
            if str(uuid.UUID(user_id)) == user_id:
                return user_id
        except ValueError:
            pass
    raise ValueError("A wallet opens only for a user's uuid (users.id, canonical lowercase form).")


def open_user_wallet(user_id: str | uuid.UUID, *, create: bool = False) -> AbstractAsyncContextManager[ClientEvmSigner]:
    """Open this user's own wallet, with the opener SENTINEL_WALLET selects. Use as: async with ... as signer.

    create=False (every pay call) only looks the account up; create=True gets or creates it (first sign in only).
    Raises ValueError before anything is opened unless user_id is a uuid.UUID or its canonical lowercase string.
    """
    name = _account_name(user_id)
    return WALLETS[selected()][0](name, create)
