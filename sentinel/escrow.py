"""The x402 auth-capture escrow on Base Sepolia: hold a payment, then pay the seller or give it back.

A payment in escrow moves in three steps, all sent by the operator (the only address the contract lets act):
  authorize  pulls the buyer's USDC into the escrow with the buyer's signed ReceiveWithAuthorization
  capture    pays the held amount to the seller (receiver)
  void       returns what is still held to the buyer
If the operator never acts, the buyer can reclaim the hold onchain after authorizationExpiry, so a held payment
can never be stuck with Sentinel. Sentinel is the operator: its own CDP account (SENTINEL_OPERATOR_ACCOUNT), which
pays the gas for every step. The seller opts in by naming that account as captureAuthorizer in its 402.

The pure half (PaymentInfo, hashes, typed data, calldata) follows the x402 auth-capture EVM binding and the
@x402/evm client (there is no Python SDK for the scheme yet, see FEEDBACK.md). The rest reads and writes the chain.
"""
import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol

import httpx
from cdp import CdpClient
from cdp.evm_transaction_types import TransactionRequestEIP1559
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from x402.mechanisms.evm import TypedDataDomain, TypedDataField

from sentinel import chain

SCHEME = "auth-capture"
CHAIN_ID = 84532  # Base Sepolia
CDP_NETWORK = "base-sepolia"
USDC = chain.USDC_BASE_SEPOLIA
TOKEN_NAME, TOKEN_VERSION = "USDC", "2"  # USDC's EIP-712 domain on Base Sepolia
ESCROW = "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c"  # AuthCaptureEscrow v1.1, the binding's default
EIP3009_COLLECTOR = "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88"  # v1.1 collector for receiveWithAuthorization
ZERO = "0x0000000000000000000000000000000000000000"
OPERATOR_ENV, OPERATOR_DEFAULT = "SENTINEL_OPERATOR_ACCOUNT", "sentinel-operator"
RECEIPT_TIMEOUT_S = 60.0
CDP_TIMEOUT_S = 15

PAYMENT_INFO_FIELDS = (
    "address operator,address payer,address receiver,address token,uint120 maxAmount,uint48 preApprovalExpiry,"
    "uint48 authorizationExpiry,uint48 refundExpiry,uint16 minFeeBps,uint16 maxFeeBps,address feeReceiver,uint256 salt"
)
PAYMENT_INFO_TYPEHASH = keccak(text=f"PaymentInfo({PAYMENT_INFO_FIELDS})")
INFO_TUPLE = "(address,address,address,address,uint120,uint48,uint48,uint48,uint16,uint16,address,uint256)"
INFO_TYPES = INFO_TUPLE.strip("()").split(",")

TypedData = tuple[TypedDataDomain, dict[str, list[TypedDataField]], str, dict[str, Any]]
RECEIVE_TYPES = {
    "ReceiveWithAuthorization": [
        TypedDataField("from", "address"), TypedDataField("to", "address"), TypedDataField("value", "uint256"),
        TypedDataField("validAfter", "uint256"), TypedDataField("validBefore", "uint256"),
        TypedDataField("nonce", "bytes32"),
    ]
}


class EscrowError(Exception):
    """An escrow call failed or reverted, or the chain could not be read."""


@dataclass(frozen=True)
class PaymentInfo:
    """The escrow's PaymentInfo struct: who pays whom, how much at most, and until when. Amounts in atomic units.

    preApprovalExpiry bounds the buyer's signature; authorizationExpiry is the capture deadline (and when the buyer
    may reclaim); refundExpiry is the refund deadline. Sentinel takes no fee, so both fee bounds are 0.
    """

    operator: str
    payer: str
    receiver: str
    token: str
    max_amount: int
    pre_approval_expiry: int
    authorization_expiry: int
    refund_expiry: int
    min_fee_bps: int = 0
    max_fee_bps: int = 0
    fee_receiver: str = ZERO
    salt: int = 0

    def as_tuple(self) -> tuple[Any, ...]:
        """The struct in ABI order, addresses checksummed."""
        return (
            to_checksum_address(self.operator), to_checksum_address(self.payer), to_checksum_address(self.receiver),
            to_checksum_address(self.token), self.max_amount, self.pre_approval_expiry, self.authorization_expiry,
            self.refund_expiry, self.min_fee_bps, self.max_fee_bps, to_checksum_address(self.fee_receiver), self.salt,
        )

    def to_json(self) -> dict[str, Any]:
        """For the escrow_payments row. The salt is a string, since it does not fit a JSON number."""
        return {**asdict(self), "salt": str(self.salt)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PaymentInfo":
        """The inverse of to_json."""
        return cls(**{**data, "salt": int(data["salt"])})


def new_salt() -> int:
    """A random salt, so two identical payments have different hashes."""
    return secrets.randbits(256)


def _same(a: Any, b: str) -> bool:
    return isinstance(a, str) and a.lower() == b.lower()


def check_terms(requirement: Any, operator: str, now: int, policy: dict[str, int]) -> str | None:
    """Why this auth-capture offer is not one Sentinel can hold safely, or None when it is.

    Sentinel must be the operator (otherwise the seller's own operator could capture whatever we think of the
    delivery), on the v1.1 escrow, paid by ReceiveWithAuthorization, with no fee and no receiver authorizer, and the
    hold must end between escrow_min_hold_s and escrow_max_hold_s from now. Pure, so the same offer always gets the
    same answer.
    """
    extra = requirement.extra or {}
    network = f"eip155:{CHAIN_ID}"
    if requirement.scheme != SCHEME or requirement.network != network or not _same(requirement.asset, USDC):
        return "It is not an auth-capture offer in Base Sepolia USDC."
    if extra.get("authCaptureEscrow") not in (None, "") and not _same(extra.get("authCaptureEscrow"), ESCROW):
        return "It names an escrow contract other than auth-capture v1.1."
    if not _same(extra.get("captureAuthorizer"), operator):
        return "Sentinel is not its operator, so Sentinel could not give the money back."
    if extra.get("operatorType", "delegated") != "delegated" or extra.get("paymentFlow", "escrow") != "escrow":
        return "It asks for an operator type or payment flow Sentinel does not use."
    if extra.get("assetTransferMethod", "eip3009") != "eip3009":
        return "It asks for a transfer method other than eip3009."
    if extra.get("receiverAuthorizer") not in (None, "", ZERO) or extra.get("policy") not in (None, "", ZERO):
        return "It binds a receiver authorizer or policy, which Sentinel does not support."
    if extra.get("minFeeBps") != 0 or extra.get("maxFeeBps") != 0 or extra.get("feeRecipient") not in (None, ZERO):
        return "It charges a fee on capture."
    capture, refund = extra.get("captureDeadline"), extra.get("refundDeadline")
    if not isinstance(capture, int) or not isinstance(refund, int) or refund < capture:
        return "Its capture and refund deadlines are missing or out of order."
    if capture < now + policy["escrow_min_hold_s"] or capture > now + policy["escrow_max_hold_s"]:
        return (f"Its capture deadline is not between {policy['escrow_min_hold_s'] // 60} and "
                f"{policy['escrow_max_hold_s'] // 60} minutes from now.")
    if now + int(requirement.max_timeout_seconds) > capture:
        return "Its signature would outlive the capture deadline."
    return None


def payment_info(requirement: Any, payer: str, now: int, salt: int) -> PaymentInfo:
    """The PaymentInfo for an offer that passed check_terms, paid by payer."""
    extra = requirement.extra
    return PaymentInfo(
        operator=extra["captureAuthorizer"], payer=payer, receiver=requirement.pay_to, token=requirement.asset,
        max_amount=int(requirement.amount), pre_approval_expiry=now + int(requirement.max_timeout_seconds),
        authorization_expiry=extra["captureDeadline"], refund_expiry=extra["refundDeadline"], salt=salt,
    )


def info_hash(info: PaymentInfo) -> str:
    """The escrow's getHash(paymentInfo): the id of this payment onchain, as 0x hex."""
    inner = keccak(encode(["bytes32", *INFO_TYPES], [PAYMENT_INFO_TYPEHASH, *info.as_tuple()]))
    return "0x" + keccak(encode(["uint256", "address", "bytes32"], [CHAIN_ID, ESCROW, inner])).hex()


def signature_nonce(info: PaymentInfo) -> str:
    """The ReceiveWithAuthorization nonce: the payment's hash with payer zero, which the collector recomputes."""
    return info_hash(replace(info, payer=ZERO))


def receive_typed_data(info: PaymentInfo) -> TypedData:
    """The EIP-712 ReceiveWithAuthorization the buyer signs: move max_amount to the collector, valid until
    pre_approval_expiry, under the payment's nonce. Pass straight to ClientEvmSigner.sign_typed_data."""
    domain = TypedDataDomain(name=TOKEN_NAME, version=TOKEN_VERSION, chain_id=CHAIN_ID,
                             verifying_contract=to_checksum_address(info.token))
    message = {
        "from": to_checksum_address(info.payer), "to": to_checksum_address(EIP3009_COLLECTOR),
        "value": info.max_amount, "validAfter": 0, "validBefore": info.pre_approval_expiry,
        "nonce": bytes.fromhex(signature_nonce(info).removeprefix("0x")),
    }
    return domain, RECEIVE_TYPES, "ReceiveWithAuthorization", message


def _call_data(signature: str, types: list[str], values: list[Any]) -> str:
    return "0x" + keccak(text=signature)[:4].hex() + encode(types, values).hex()


def authorize_data(info: PaymentInfo, amount: int, buyer_signature: str) -> str:
    """Calldata for authorize(paymentInfo, amount, tokenCollector, collectorData)."""
    return _call_data(f"authorize({INFO_TUPLE},uint256,address,bytes)", [INFO_TUPLE, "uint256", "address", "bytes"],
                      [info.as_tuple(), amount, to_checksum_address(EIP3009_COLLECTOR),
                       bytes.fromhex(buyer_signature.removeprefix("0x"))])


def capture_data(info: PaymentInfo, amount: int) -> str:
    """Calldata for capture(paymentInfo, amount, feeAmount 0, feeReceiver zero). v1.1 takes a fee amount, not bps."""
    return _call_data(f"capture({INFO_TUPLE},uint256,uint256,address)", [INFO_TUPLE, "uint256", "uint256", "address"],
                      [info.as_tuple(), amount, 0, ZERO])


def void_data(info: PaymentInfo) -> str:
    """Calldata for void(paymentInfo)."""
    return _call_data(f"void({INFO_TUPLE})", [INFO_TUPLE], [info.as_tuple()])


@dataclass(frozen=True)
class State:
    """paymentState(hash): whether the buyer's funds were collected, what is still held, what could be refunded."""

    collected: bool
    capturable: int
    refundable: int


async def _rpc(session: httpx.AsyncClient, method: str, params: list[object]) -> Any:
    response = await session.post(chain.RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise EscrowError(f"{method}: {body['error'].get('message', body['error'])}")
    return body.get("result")


async def _eth_call(data: str) -> bytes:
    async with httpx.AsyncClient(timeout=chain.TIMEOUT_S) as session:
        result = await _rpc(session, "eth_call", [{"to": ESCROW, "data": data}, "latest"])
    return bytes.fromhex(str(result).removeprefix("0x"))


async def payment_state(payment_hash: str) -> State:
    """Read paymentState for a payment hash. Raises EscrowError when the chain cannot be read."""
    try:
        raw = await _eth_call(_call_data("paymentState(bytes32)", ["bytes32"],
                                         [bytes.fromhex(payment_hash.removeprefix("0x"))]))
    except httpx.HTTPError as e:
        raise EscrowError("could not read the escrow") from e
    collected, capturable, refundable = decode(["bool", "uint120", "uint120"], raw)
    return State(bool(collected), int(capturable), int(refundable))


async def onchain_hash(info: PaymentInfo) -> str:
    """The escrow contract's own getHash(paymentInfo), to check info_hash against the deployed contract."""
    raw = await _eth_call(_call_data(f"getHash({INFO_TUPLE})", [INFO_TUPLE], [info.as_tuple()]))
    return "0x" + raw.hex()


async def wait_for(tx_hash: str, timeout_s: float = RECEIPT_TIMEOUT_S) -> None:
    """Wait for a transaction to be mined. Raises EscrowError if it reverted or did not land in time."""
    async with httpx.AsyncClient(timeout=chain.TIMEOUT_S) as session:
        async with asyncio.timeout(timeout_s):
            while True:
                receipt = await _rpc(session, "eth_getTransactionReceipt", [tx_hash])
                if receipt:
                    if int(receipt.get("status", "0x0"), 16) != 1:
                        raise EscrowError(f"transaction {tx_hash} reverted")
                    return
                await asyncio.sleep(1)


class EscrowOperator(Protocol):
    """What payments.py needs from an operator: its address, and the three escrow calls. Operator is the real one."""

    address: str

    async def authorize(self, info: PaymentInfo, buyer_signature: str) -> str:
        """Hold the buyer's money; returns the tx hash."""
        ...

    async def capture(self, info: PaymentInfo, amount: int) -> str:
        """Pay the seller; returns the tx hash."""
        ...

    async def void(self, info: PaymentInfo) -> str:
        """Give the hold back; returns the tx hash."""
        ...


class Operator:
    """Sentinel's operator account: the one address that can authorize, capture and void. Pays the gas."""

    def __init__(self, cdp: CdpClient, address: str):
        self._cdp, self.address = cdp, address

    async def send(self, data: str) -> str:
        """Send one call to the escrow from the operator and wait for it. Returns the tx hash."""
        try:
            tx_hash = await self._cdp.evm.send_transaction(
                address=self.address, transaction=TransactionRequestEIP1559(to=ESCROW, data=data),
                network=CDP_NETWORK,
            )
        except Exception as e:  # CDP raises its own API errors, a revert on gas estimation among them
            raise EscrowError(f"the escrow call was not sent: {e}") from e
        await wait_for(tx_hash)
        return tx_hash

    async def authorize(self, info: PaymentInfo, buyer_signature: str) -> str:
        """Move the buyer's max_amount into the escrow. Returns the tx hash."""
        return await self.send(authorize_data(info, info.max_amount, buyer_signature))

    async def capture(self, info: PaymentInfo, amount: int) -> str:
        """Pay amount of the hold to the seller. Returns the tx hash."""
        return await self.send(capture_data(info, amount))

    async def void(self, info: PaymentInfo) -> str:
        """Return what is still held to the buyer. Returns the tx hash."""
        return await self.send(void_data(info))


def operator_account() -> str:
    """The CDP account name of the operator (SENTINEL_OPERATOR_ACCOUNT, default sentinel-operator)."""
    return os.environ.get(OPERATOR_ENV) or OPERATOR_DEFAULT


@asynccontextmanager
async def open_operator(create: bool = False) -> AsyncIterator[Operator]:
    """Open the operator account. Looks it up only, unless create (the spike and first setup)."""
    async with CdpClient() as cdp:
        async with asyncio.timeout(CDP_TIMEOUT_S):
            if create:
                account = await cdp.evm.get_or_create_account(name=operator_account())
            else:
                account = await cdp.evm.get_account(name=operator_account())
        yield Operator(cdp, account.address)
