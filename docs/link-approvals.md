# Interactive link approvals

Approved sellers pass automatically within the agent's spending rules. An
unlisted seller gets a native Yes/No form before anything is signed. Explicit
seller blocks, unsafe URLs, per payment caps, budgets, price jumps and duplicate
checks still block. An approval never adds a seller to the permanent list.

Run `python -m sentinel.db migrate` after updating, then restart Sentinel and
reconnect your MCP client so it discovers `resume_payment`.

## Chat flow

1. The agent starts a task, then calls `pay(task_id, url, reason, found_at?)` for a discovered resource.
2. Sentinel fetches the x402 quote and checks spending policy and task intent. Unknown sellers that pass
   spending checks create a `pending` payment request.
3. Sentinel closes the wallet and payment connection and releases the in flight
   slot before asking the human. The form shows the URL, actual recipient,
   amount, asset, network, purpose and request id. Agent text is labeled untrusted.
4. No records a denial. Dismissal cancels. Yes records a one purchase
   authorization, then fetches the quote again and executes automatically.
5. Changed terms invalidate the authorization. The agent must start a new request
   and obtain a new answer. Neither an old Yes nor a cached tool result can pay twice.

The quote binds the exact URL, recipient, amount, asset, network and timeout;
the original reason is also bound. Even a cheaper quote requires a new approval.
Requests expire after the smaller of the quoted timeout and five minutes, using
the database clock. The same deadline applies after Yes and before consumption.

The server supports both legacy elicitation and 2026-07-28 input-required
round trips. Legacy HTTP clients use stateful Streamable HTTP with SSE, because
stateless JSON responses cannot carry a native form and its response. Reconnect
after a server restart. Multiple replicas require sticky routing for legacy MCP
sessions; durable approvals and modern requests live independently of the process.

## Browser fallback

Clients without native forms receive `APPROVAL_REQUIRED` with an `approval_url`.
The user opens it and signs in as the request's owner. Only a same-origin,
CSRF-protected POST records Yes or No. Visiting the page never approves anything.
The Payments page also links to waiting approvals.

After a browser Yes, the agent calls `resume_payment(request_id)`. That tool
cannot grant permission: without a stored human Yes it only reports the request's
state. `payment_status` never executes a payment. Denied, cancelled and expired
requests are visible in Payments and CSV and have no journal expense.

## Trust boundary and direct instructions

Generic MCP tools receive model-written arguments, not an authenticated copy of
the user's original message. A `user_requested` flag, a reason saying "the user
asked", or a model-written approval tool call cannot establish user consent.
An unlisted seller therefore still needs the native form or signed-in approval
page even if the model says the URL was in the prompt. Approved sellers and
previously recorded one purchase authorizations do not ask again.

Skipping the prompt for direct user instructions requires a separate trusted host
integration that issues a scoped, verifiable user authorization. This release
does not pretend to infer that provenance and exposes no self-approval flag or
approval tool. The MCP host is trusted to present human input faithfully.

## Enforcement and audit

Migration 010 restores active use of pending attempts and the append-only
`approvals` audit table. A pending request reserves its duplicate key, not money
or budget. Under an agent row lock, consumption rechecks current policy and task intent and
changes `authorized` to `approved` once. The same lock serializes budget admission
for different simultaneous payments across server processes. Settlement uses the
existing atomic journal path. Failed signing consumes the authorization rather
than granting automatic retries. Expiry is applied lazily when requests are read,
decided or new payments are authorized, releasing stale duplicate locks.

Tests cover native choices on both protocol paths, actual authenticated HTTP,
browser CSRF and ownership, quote changes, expiry, replay and independent
Postgres transactions competing for one authorization or the last budget funds.

The command-line `demo.run` remains noninteractive and blocks unlisted sellers.
To try the real chat gate locally, request the unlisted `/test-site/api/intel/litigation-complete?company=Acme+Robotics` (PremiumIntel, 0.25 test
USDC) and
choose No in the native form. The unlisted `/test-site/api/intel/deep-dossier` quotes 9
test USDC, so a lower cap blocks it before any prompt, as intended. Use a funded wallet and an appropriate explicit
limit before testing Yes with a vendor you intend to pay.

## Integration with tasks

Purchase approval never overrides a revoked task, task budget, intent block or intent hold.
The pending request stores its task and discovery source. Resuming reconstructs that same task
from the stored request and rechecks intent before consuming permission. A task revoked while
the human is deciding cannot pay after Yes. Clients must reconnect to discover the task tools
as well as resume_payment. Main also requires the pinned Anthropic dependency and ANTHROPIC_API_KEY
for task extraction and intent review.

The original 010_link_approvals migration filename is retained for existing installations.
014 reconciles the status constraint with intent binding for databases upgraded from either branch.
