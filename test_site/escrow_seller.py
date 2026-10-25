"""The seller side of escrow for the test site: offer x402 auth-capture next to exact, and deliver only once
the money is held. A fixture, like app.py; a real seller would run the same few steps.

EscrowMiddleware sits outside x402's PaymentMiddlewareASGI. For an escrow route it:
  - adds an auth-capture offer, first, to the 402 that x402 builds (header and body), naming Sentinel's operator as
    captureAuthorizer, so the buyer can choose to pay into escrow;
  - on a request whose PAYMENT-SIGNATURE is an auth-capture payload, rebuilds the escrow's PaymentInfo from its own
    terms, reads paymentState onchain, and serves the route only when the full price is held for this seller; each
    payment is served once. It forwards such a request to PAID_PREFIX + path with ESCROW_PAID set in the ASGI scope;
    the handler there refuses anything without that mark (paid_only), so the prefix is not a free way in.
Anything else goes to x402 as before.
The seller needs no facilitator for escrow: Sentinel, as operator, sends authorize, capture and void. The operator
address comes from SENTINEL_OPERATOR_ADDRESS, else from the CDP account escrow.open_operator names (the demo shares
Sentinel's CDP project). Without either, routes offer exact only.
"""
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from x402.http.utils import safe_base64_decode, safe_base64_encode

from sentinel import escrow

log = logging.getLogger("test_site.escrow_seller")
OPERATOR_ENV = "SENTINEL_OPERATOR_ADDRESS"
HOLD_S = 1800  # capture deadline: half an hour to check the delivery
REFUND_S = 3600
TIMEOUT_S = 300
PAID_PREFIX = "/_paid"
ESCROW_PAID = "sentinel.escrow_paid"  # ASGI scope key: this request's escrow payment was checked onchain

Terms = Callable[[], tuple[str, int]]  # today's (pay_to, amount in atomic units) for a route


def escrow_option(pay_to: str, amount: int, operator: str, now: int) -> dict[str, Any]:
    """The auth-capture accepts entry for one route, in x402 v2 wire form."""
    return {
        "scheme": escrow.SCHEME, "network": f"eip155:{escrow.CHAIN_ID}", "asset": escrow.USDC, "amount": str(amount),
        "payTo": pay_to, "maxTimeoutSeconds": TIMEOUT_S,
        "extra": {
            "name": escrow.TOKEN_NAME, "version": escrow.TOKEN_VERSION, "authCaptureEscrow": escrow.ESCROW,
            "captureAuthorizer": operator, "operatorType": "delegated", "paymentFlow": "escrow",
            "captureMode": "deferred", "captureDeadline": now + HOLD_S, "refundDeadline": now + REFUND_S,
            "minFeeBps": 0, "maxFeeBps": 0, "feeRecipient": escrow.ZERO, "assetTransferMethod": "eip3009",
        },
    }


class EscrowMiddleware:
    """Escrow for the routes in terms (path -> today's payee and price); everything else passes through."""

    def __init__(self, app: ASGIApp, terms: dict[str, Terms]):
        self.app, self.terms = app, terms
        self.served: set[str] = set()
        self.operator = os.environ.get(OPERATOR_ENV) or None

    async def operator_address(self) -> str | None:
        """Sentinel's operator, looked up once from CDP when the environment does not name it."""
        if self.operator is None:
            try:
                async with escrow.open_operator() as operator:
                    self.operator = operator.address
            except Exception:
                log.warning("no escrow operator found; offering exact only", exc_info=True)
                self.operator = ""
        return self.operator or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path.startswith(PAID_PREFIX):
            scope = {**scope, ESCROW_PAID: False}  # nobody outside sets the mark
        if scope["type"] != "http" or scope.get("method") != "GET" or path not in self.terms:
            return await self.app(scope, receive, send)
        operator = await self.operator_address()
        if operator is None:
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        signature = headers.get("payment-signature")
        if signature:
            try:
                payload = json.loads(safe_base64_decode(signature))
            except ValueError:
                payload = {}
            if (payload.get("accepted") or {}).get("scheme") == escrow.SCHEME:
                problem = await self.check_hold(payload, path, operator)
                if problem:
                    return await _json(send, 402, {"error": problem})
                paid = PAID_PREFIX + path
                return await self.app({**scope, "path": paid, "raw_path": paid.encode(), ESCROW_PAID: True},
                                      receive, send)
            return await self.app(scope, receive, send)
        await self.app(scope, receive, self.offering(send, path, operator))

    def offering(self, send: Send, path: str, operator: str) -> Send:
        """Wrap send so a 402 from x402 also offers escrow, first."""
        start: dict[str, Message] = {}
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                if message["status"] != 402:
                    return await send(message)
                start["message"] = message
                return None
            if "message" not in start:
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return None
            pay_to, amount = self.terms[path]()
            option = escrow_option(pay_to, amount, operator, int(time.time()))
            head = start["message"]
            fixed = []
            for key, value in head.get("headers", []):
                if key.decode().lower() == "payment-required":
                    required = json.loads(safe_base64_decode(value.decode()))
                    required["accepts"] = [option, *required.get("accepts", [])]
                    value = safe_base64_encode(json.dumps(required)).encode()
                if key.decode().lower() != "content-length":
                    fixed.append((key, value))
            body = b"".join(chunks)
            try:
                document = json.loads(body) if body else None
            except ValueError:
                document = None
            if isinstance(document, dict) and isinstance(document.get("accepts"), list):
                document["accepts"] = [option, *document["accepts"]]
                body = json.dumps(document).encode()
            fixed.append((b"content-length", str(len(body)).encode()))
            await send({**head, "headers": fixed})
            await send({"type": "http.response.body", "body": body})

        return wrapped

    async def check_hold(self, payload: dict[str, Any], path: str, operator: str) -> str | None:
        """Why this escrow payment does not pay for path, or None when the full price is held for this seller."""
        accepted, inner = payload.get("accepted") or {}, payload.get("payload") or {}
        authorization = inner.get("authorization") or {}
        extra = accepted.get("extra") or {}
        pay_to, amount = self.terms[path]()
        if (str(accepted.get("payTo", "")).lower() != pay_to.lower() or int(accepted.get("amount", 0)) != amount
                or str(extra.get("captureAuthorizer", "")).lower() != operator.lower()):
            return "The escrow terms do not match this product."
        try:
            info = escrow.PaymentInfo(
                operator=operator, payer=authorization["from"], receiver=pay_to, token=escrow.USDC,
                max_amount=amount, pre_approval_expiry=int(authorization["validBefore"]),
                authorization_expiry=int(extra["captureDeadline"]), refund_expiry=int(extra["refundDeadline"]),
                salt=int(inner["salt"]),
            )
            payment = escrow.info_hash(info)
            state = await escrow.payment_state(payment)
        except (KeyError, TypeError, ValueError, escrow.EscrowError):
            return "The escrow payment could not be checked."
        if payment in self.served:
            return "This escrow payment was already used."
        if state.capturable < amount:
            return "The money is not held in escrow for this seller."
        self.served.add(payment)
        return None


def paid_only(scope: Scope) -> bool:
    """True for a request EscrowMiddleware forwarded after checking its escrow payment onchain."""
    return scope.get(ESCROW_PAID) is True


async def _json(send: Send, status: int, document: dict[str, Any]) -> None:
    body = json.dumps(document).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
