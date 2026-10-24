"""Tests for the wallet seam: which wallet is picked, what it needs, and that any x402 signer plugs in.

No network: the CDP wallet is never opened here. The pluggability test uses a throwaway key that exists only
inside the test, to show that a wallet other than CDP meets the same interface the payment flow uses, opened the
one way production code opens a wallet (open_user_wallet). Per user CDP accounts are tested in test_user_wallets.py.
"""
import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import ASSET, DATACO
from eth_account import Account
from eth_account.messages import encode_typed_data
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.types import TypedDataDomain, TypedDataField

from sentinel import wallets

# The shape of what x402 asks a wallet to sign for a USDC payment (EIP-3009 transfer authorization).
FIELDS = [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"),
          ("validBefore", "uint256"), ("nonce", "bytes32")]
DOMAIN = {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": ASSET}


def test_cdp_is_the_default_and_needs_the_cdp_keys(monkeypatch):
    monkeypatch.delenv(wallets.ENV_VAR, raising=False)
    assert wallets.selected() == "cdp"
    assert wallets.required_env() == ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET")


def test_an_unknown_wallet_is_refused_with_the_choices(monkeypatch):
    monkeypatch.setenv(wallets.ENV_VAR, "nope")
    with pytest.raises(ValueError, match="choose from: cdp"):
        wallets.required_env()
    with pytest.raises(ValueError, match="choose from: cdp"):
        wallets.open_user_wallet(uuid.uuid4())


@pytest.mark.anyio
async def test_another_wallet_plugs_in_and_signs_a_payment_as_itself(monkeypatch):
    account = Account.create()  # throwaway key, test only
    opened = []

    @asynccontextmanager
    async def local_wallet(name, create):
        opened.append((name, create))
        yield EthAccountSigner(account)

    monkeypatch.setitem(wallets.WALLETS, "local", (local_wallet, ()))
    monkeypatch.setenv(wallets.ENV_VAR, "local")
    user_id = uuid.uuid4()
    message = {"from": account.address, "to": DATACO, "value": 500_000, "validAfter": 0,
               "validBefore": 2**32, "nonce": bytes(32)}

    assert wallets.required_env() == ()
    async with wallets.open_user_wallet(user_id) as signer:
        signature = signer.sign_typed_data(
            TypedDataDomain(DOMAIN["name"], DOMAIN["version"], DOMAIN["chainId"], DOMAIN["verifyingContract"]),
            {"TransferWithAuthorization": [TypedDataField(name, type_) for name, type_ in FIELDS]},
            "TransferWithAuthorization",
            message,
        )

    assert opened == [(str(user_id), False)]  # the opener got the user's uuid as the account name, lookup only
    assert signer.address == account.address
    assert len(signature) == 65
    types = {"TransferWithAuthorization": [{"name": name, "type": type_} for name, type_ in FIELDS]}
    signed = encode_typed_data(domain_data=DOMAIN, message_types=types, message_data=message)
    assert Account.recover_message(signed, signature=signature) == account.address
