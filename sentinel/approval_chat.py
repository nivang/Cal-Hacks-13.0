"""Native MCP approval transport. The model never supplies a Yes/No tool argument."""
import asyncio
import json
from typing import Any

from fastmcp import Context
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult

from sentinel import link_approvals, results


def response_choice(response: ElicitResult) -> str:
    """Accept only an explicit Yes; accepting a form is not itself purchase consent."""
    if response.action == "accept":
        return "yes" if (response.content or {}).get("value") == "Yes" else "no"
    return "no" if response.action == "decline" else "cancel"


async def ask(ctx: Context, row: dict[str, Any]) -> str | InputRequiredResult | None:
    """Ask the host's human UI, or return None for the authenticated browser fallback."""
    capabilities = ctx.session.client_capabilities
    if not capabilities or not capabilities.elicitation or capabilities.elicitation.form is None:
        return None
    message = (
        f"Unapproved seller. Pay {results.usdc_text(row['atomic_amount'])} test USDC on Base Sepolia once?\n"
        f"Request: {row['id']}\n"
        f"URL (untrusted): {json.dumps(row['url'])}\nRecipient: {json.dumps(row['pay_to'])}\n"
        f"Purpose (agent supplied): {json.dumps(row['reason'])}\n"
        "Yes pays this purchase only and does not trust the seller for future purchases. No cancels it. "
        "Spending rules still apply. This request expires within five minutes."
    )
    if ctx.request_context and ctx.request_context.protocol_version == "2026-07-28":
        return InputRequiredResult(
            request_state=json.dumps({"approval_request_id": row["id"]}),
            input_requests={"purchase": ElicitRequest(params=ElicitRequestFormParams(
                message=message,
                requested_schema={
                    "type": "object", "properties": {"value": {
                        "type": "string", "title": "Approve this purchase once?", "enum": ["No", "Yes"],
                    }}, "required": ["value"], "additionalProperties": False,
                },
            ))},
        )
    try:
        async with asyncio.timeout(min(float(row["seconds_left"]), link_approvals.MAX_WAIT_S)):
            answer = await ctx.elicit(message, ["No", "Yes"], response_title="Approve this purchase once?")
        if answer.action == "accept":
            return "yes" if answer.data == "Yes" else "no"
        return "no" if answer.action == "decline" else "cancel"
    except TimeoutError:
        return "cancel"
    except Exception:
        return None
