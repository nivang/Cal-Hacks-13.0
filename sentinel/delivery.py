"""Did the seller deliver what it sold? Checks a paid response before an escrowed payment is captured.

The seller's promise comes from its own 402 (promise_from): the resource's mime type, its description, and the
fields the x402 Bazaar discovery extension says the output has (the output schema's required keys, else the keys of
its example). check() runs the rules, first to fire wins, each a FAIL:
  http_error      the paid request did not return 200
  empty           nothing came back
  too_big         more than MAX_BODY_CHARS
  wrong_type      the content type is not the one the seller declared (e.g. an HTML error page for a JSON API)
  not_json        JSON was promised and the body does not parse, or is not an object
  missing_fields  a promised field is absent, null or empty
  injection       the body talks to AI agents (intent.INJECTION): seller text trying to steer the buyer
When every rule passes, the judge reads the body against the promise and the task (judge.check_delivery): only
"delivered" is a PASS. "not_delivered", "unsure" and a judge failure are UNSURE, which voids the escrow (the buyer is
refunded) just like a FAIL: Sentinel never pays a seller for a delivery it doubts.
Pure apart from the judge call in payments.py, so test_delivery.py is the answer key.
"""
import json
from dataclasses import dataclass
from typing import Any

from sentinel import intent

PASS, FAIL, UNSURE = "pass", "fail", "unsure"
MAX_BODY_CHARS = 100_000  # results.MAX_BODY_CHARS: more than the agent would be handed
MAX_FIELDS = 30
MAX_JUDGED_CHARS = 4_000  # how much of the body the judge reads


@dataclass(frozen=True)
class Promise:
    """What the seller said this paid URL returns. All of it is the seller's own claim."""

    mime_type: str | None = None
    fields: tuple[str, ...] = ()
    description: str | None = None
    service_name: str | None = None


@dataclass(frozen=True)
class Verdict:
    """pass (capture), fail (void) or unsure (hold for the owner), which rule decided, and why in plain English."""

    outcome: str
    rule: str
    detail: str


def promise_from(payment_required: Any) -> Promise:
    """Read the seller's promise from its 402: resource mime type and description, Bazaar output fields."""
    resource = getattr(payment_required, "resource", None)
    extensions = getattr(payment_required, "extensions", None) or {}
    return Promise(
        mime_type=_mime(getattr(resource, "mime_type", None)),
        fields=_fields(extensions.get("bazaar") if isinstance(extensions, dict) else None),
        description=intent.clip(getattr(resource, "description", None)),
        service_name=intent.clip(getattr(resource, "service_name", None), 100),
    )


def _mime(value: Any) -> str | None:
    """"application/json; charset=utf-8" -> "application/json"."""
    if not isinstance(value, str):
        return None
    return value.split(";")[0].strip().lower() or None


def _fields(bazaar: Any) -> tuple[str, ...]:
    """The output fields a Bazaar extension promises: the output schema's required keys, else its example's keys."""
    if not isinstance(bazaar, dict):
        return ()
    try:
        example_schema = bazaar["schema"]["properties"]["output"]["properties"]["example"]
        required = example_schema.get("required")
    except (KeyError, TypeError, AttributeError):
        required = None
    if not isinstance(required, list):
        output = (bazaar.get("info") or {}).get("output") if isinstance(bazaar.get("info"), dict) else None
        example = output.get("example") if isinstance(output, dict) else None
        required = list(example) if isinstance(example, dict) else []
    return tuple(str(f)[:100] for f in required if isinstance(f, str))[:MAX_FIELDS]


def check(status: int, content_type: str | None, body: bytes, promise: Promise) -> Verdict | None:
    """The rules: a FAIL verdict when one fires, None when the delivery passes them all (then the judge reads it)."""
    if status == 0:
        return Verdict(FAIL, "http_error", "The seller did not answer the paid request.")
    if status != 200:
        return Verdict(FAIL, "http_error", f"The seller answered the paid request with HTTP {status}.")
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return Verdict(FAIL, "empty", "The seller sent back nothing.")
    if len(text) > MAX_BODY_CHARS:
        return Verdict(FAIL, "too_big", f"The response is over {MAX_BODY_CHARS:,} characters.")
    got = _mime(content_type)
    if promise.mime_type and got != promise.mime_type:
        return Verdict(FAIL, "wrong_type", f"The seller promised {promise.mime_type} and sent {got or 'no type'}.")
    if (promise.mime_type or got) == "application/json":
        try:
            data = json.loads(text)
        except ValueError:
            return Verdict(FAIL, "not_json", "The seller promised JSON and sent something that does not parse.")
        if promise.fields and not isinstance(data, dict):
            return Verdict(FAIL, "not_json", "The seller promised a JSON object and sent something else.")
        missing = [f for f in promise.fields if data.get(f) in (None, "", [], {})]
        if missing:
            listed = ", ".join(missing)
            return Verdict(FAIL, "missing_fields", f"The response lacks fields the seller promised: {listed}.")
    if intent.INJECTION.search(text):
        return Verdict(FAIL, "injection", "The response contains instructions aimed at AI agents, not just data.")
    return None


def case(mandate: intent.Mandate, promise: Promise, url: str, body: bytes) -> dict[str, Any]:
    """What the judge reads: the task, the seller's promise and the start of the body, all as quoted data."""
    return {
        "task": {"goal": mandate.goal, "owner_words": mandate.task_text},
        "seller_promise": {"url": url, "service_name": promise.service_name, "description": promise.description,
                           "fields": list(promise.fields)},
        "delivered_body": body.decode("utf-8", errors="replace")[:MAX_JUDGED_CHARS],
    }


def from_judge(verdict: str, reason: str) -> Verdict:
    """The judge's opinion as a verdict: only delivered passes; anything else is refunded."""
    if verdict == "delivered":
        return Verdict(PASS, "delivered", reason)
    return Verdict(UNSURE, "not_delivered" if verdict == "not_delivered" else "judge_unsure", reason)


def unavailable() -> Verdict:
    """The judge could not be reached: refund, never capture on a guess."""
    return Verdict(UNSURE, "judge_unavailable", "The delivery check could not reach its model, so it is refunded.")


def without_task() -> Verdict:
    """No task to judge against (a payment made outside a task): the rules passed, so pay."""
    return Verdict(PASS, "rules_passed", "The response passed every delivery rule.")
