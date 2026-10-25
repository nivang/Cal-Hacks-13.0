# Link approvals (removed)

Sentinel no longer asks a human about any single payment. It makes the final
decision itself: ALLOW (pay) or BLOCK (deny). Removed with this change:

- native MCP Yes/No forms (elicitation) and the `resume_payment` tool
- approval links (`/account/approvals/<id>`), texted or shown to the agent
- `APPROVAL_REQUIRED`, `ESCALATED` and `HELD` results
- **Allow once** on held payments, and task confirmation (tasks are live at once with their full budget)
- escrow **Pay seller** / **Refund me**: a delivery Sentinel cannot vouch for is refunded automatically

What decides instead:

- A seller on a blocked list is blocked. A seller on an approved list goes through the usual checks.
- A seller on no list is paid only through escrow (`seller_escrow`), so a delivery that fails the check is
  refunded automatically. Without an escrow offer it is blocked (`seller`).
- Every rule that used to hold a payment for the owner now blocks it, with the same rule name.
- Seller text that reads as a prompt injection is blocked (intent rule `injection`, and the local injection
  model when it is configured).

The owner still controls the policy, never single payments: seller lists, the per payment cap, the weekly
budget and auto buy on the Rules page, and **Revoke** on a whole task. Old `pending`, `authorized` and
`escalated` rows, and the `approvals` and `allowances` tables, stay in the database and read as blocked.
See [how it works](how-it-works.md).
