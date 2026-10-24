# Feedback for Coinbase

## Phase 2
- **x402 Python SDK has no manual-flow doc.** The examples use the auto-paying httpx wrapper (`x402HttpxClient`), which signs with no gap for a guard or human approval. We had to read the SDK source to find `x402HTTPClient.get_payment_required_response`, `x402Client.create_payment_payload` and `encode_payment_signature_header`. Workaround: payments.py drives these directly. Coinbase should document a "bring your own control flow" path and a pre-sign hook that can pause.
- **No way to sign one chosen requirement.** `create_payment_payload` picks from `accepts` itself, so we narrow `accepts` to one entry with `model_copy`. Coinbase should accept an explicit requirement or index, as the TS `signX402Payment(paymentRequired, index)` does.
- **CDP account needs a wrapper for x402.** `EvmLocalAccount(account)` then `EthAccountSigner` is required and undocumented together.
- **Quote expiry is `max_timeout_seconds` (300 here) on the requirement.** It is not tied to a human approval window, which matters for Phase 6.
- **x402 server typing:** `ExactEvmServerScheme` fails mypy against `x402ResourceServer.register` (`payment_flows` is typed `dict[str, dict[str, Sequence[str]]]` but expected `Mapping[str, PaymentFlowConfig]`). It works at runtime. We silence it with a targeted `type: ignore`. Coinbase should align the annotations.
