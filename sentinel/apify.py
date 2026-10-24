"""Apify Actor details for the agent: what an Actor costs and what input it needs, without running anything.

actor_details() reads two public Apify API records, the Actor (pricing, stats, example input) and its default build
(input schema, readme), and returns one ActorDetails the agent can plan a run from. The owner's APIFY_TOKEN is sent
when it is set, so private and rented Actors resolve too; the token is never put in a URL or returned. Actor ids are
checked against ACTOR_ID before any request, so an agent cannot point Sentinel at another Apify API path. Everything
the Actor's author wrote (titles, descriptions, readme) is third party text and is cleaned and cut short.
"""
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from sentinel.discovery import Fetch, clean, event_price, usdc_text

API = "https://api.apify.com/v2/acts/"
ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}[/~][A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_INPUTS = 30
MAX_README = 600
MAX_EXAMPLE_CHARS = 2000


class ActorEvent(BaseModel):
    """One thing a pay per event Actor charges for, at the free plan price."""

    event: str
    title: str
    price_usdc: str | None
    primary: bool = False


class ActorInput(BaseModel):
    """One field of the Actor's input."""

    name: str
    type: str | None = None
    title: str | None = None
    required: bool = False
    default: Any = None
    description: str | None = None


class ActorDetails(BaseModel):
    """What apify_actor returns."""

    status: str
    message: str
    actor: str
    title: str | None = None
    url: str | None = None
    description: str | None = None
    pricing_model: str | None = None
    events: list[ActorEvent] = []
    min_max_charge_usd: str | None = None
    users_30d: int | None = None
    rating: float | None = None
    success_rate_30d: float | None = None
    example_input: Any = None
    inputs: list[ActorInput] = []
    readme_summary: str | None = None
    pay_with: str | None = None


def normalize_id(actor: str) -> str:
    """The Actor id as username/name, or ValueError with a plain English reason."""
    actor = actor.strip()
    if actor.startswith("https://apify.com/"):
        actor = actor.removeprefix("https://apify.com/").split("?")[0].strip("/")
    if not ACTOR_ID.fullmatch(actor):
        raise ValueError("That is not an Apify Actor id. Use username/name, for example compass/crawler-google-places.")
    return actor.replace("~", "/")


def headers_for(url: str) -> dict[str, str]:
    """The owner's token as a bearer header, only for https://api.apify.com and only when APIFY_TOKEN is set."""
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    parts = urlsplit(url)
    if not token or parts.scheme != "https" or parts.hostname != "api.apify.com":
        return {}
    return {"Authorization": f"Bearer {token}"}


def has_credit() -> bool:
    """True when the owner set APIFY_TOKEN, so Actors can run on their Apify credit."""
    return bool((os.environ.get("APIFY_TOKEN") or "").strip())


def parse_example(record: Any) -> Any:
    """The Actor's example input: the JSON body when it parses, else None. Cut when it is very large."""
    body = record.get("body") if isinstance(record, dict) else None
    if not isinstance(body, str) or len(body) > MAX_EXAMPLE_CHARS:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def parse_inputs(build: Any) -> list[ActorInput]:
    """The input schema's fields, required first, at most MAX_INPUTS."""
    schema = (build or {}).get("inputSchema") if isinstance(build, dict) else None
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError:
            schema = None
    if not isinstance(schema, dict):
        return []
    required = set(schema.get("required") or [])
    fields = []
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        default = spec.get("default", spec.get("prefill"))
        if isinstance(default, (dict, list)) and len(json.dumps(default)) > 300:
            default = None
        fields.append(ActorInput(
            name=clean(name, 60), type=clean(spec.get("type"), 20) or None, title=clean(spec.get("title"), 80) or None,
            required=name in required, default=default, description=clean(spec.get("description"), 160) or None,
        ))
    fields.sort(key=lambda f: not f.required)
    return fields[:MAX_INPUTS]


def parse_actor(actor_id: str, actor: dict[str, Any], build: Any) -> ActorDetails:
    """Turn the Actor and build records into ActorDetails."""
    pricing = (actor.get("pricingInfos") or [{}])[-1] if isinstance(actor.get("pricingInfos"), list) else {}
    events = []
    charge_events = ((pricing or {}).get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    for key, charge in charge_events.items() if isinstance(charge_events, dict) else []:
        if isinstance(charge, dict):
            price = event_price(charge)
            events.append(ActorEvent(event=clean(key, 60), title=clean(charge.get("eventTitle"), 80),
                                     price_usdc=usdc_text(price) if price is not None else None,
                                     primary=bool(charge.get("isPrimaryEvent"))))
    events.sort(key=lambda e: not e.primary)
    stats = actor.get("stats") or {}
    runs = stats.get("publicActorRunStats30Days") or {}
    success = round(runs["SUCCEEDED"] / runs["TOTAL"], 3) if runs.get("TOTAL") and "SUCCEEDED" in runs else None
    # No pricing record means the Actor itself is free and only the run's compute is charged.
    model = (pricing.get("pricingModel") if isinstance(pricing, dict) else None) or "FREE"
    min_charge = pricing.get("minimalMaxTotalChargeUsd") if isinstance(pricing, dict) else None
    rental = model == "FLAT_PRICE_PER_MONTH"
    credit = has_credit()
    if rental:
        message = "This Actor is rented by the month, so it needs a subscription; pick a pay per event or free Actor."
    elif credit:
        message = ("Runs on the owner's Apify credit. Sentinel cannot start Actor runs yet: tell the owner this Actor, "
                   "the input you would use (start from example_input) and the expected cost.")
    else:
        message = "The owner has not connected an Apify account, so Actors can only be browsed."
    readme = (build or {}).get("readme") if isinstance(build, dict) else None
    inputs = parse_inputs(build)
    example = parse_example(actor.get("exampleRunInput"))
    if inputs and not (isinstance(example, dict) and set(example) & {i.name for i in inputs}):
        # The author's example does not use the real input fields; build one from the schema's prefilled values.
        example = {i.name: i.default for i in inputs if i.default is not None} or None
    return ActorDetails(
        status="OK", message=message, actor=actor_id, title=clean(actor.get("title"), 100) or None,
        url=f"https://apify.com/{actor_id}", description=clean(actor.get("description"), 400) or None,
        pricing_model=model, events=events[:15],
        min_max_charge_usd=str(min_charge) if isinstance(min_charge, (int, float)) else None,
        users_30d=stats.get("totalUsers30Days") if isinstance(stats.get("totalUsers30Days"), int) else None,
        rating=round(float(stats["actorReviewRating"]), 2) if isinstance(stats.get("actorReviewRating"), (int, float))
        else None,
        success_rate_30d=success, example_input=example, inputs=inputs,
        readme_summary=clean(actor.get("readmeSummary") or readme, MAX_README) or None,
        pay_with="apify_credit" if credit and not rental else None,
    )


async def actor_details(actor: str, fetch: Fetch) -> ActorDetails:
    """Look up one Actor. Raises ValueError for a bad id; a missing Actor comes back as status NOT_FOUND."""
    actor_id = normalize_id(actor)
    path = API + actor_id.replace("/", "~")
    try:
        record = await fetch(path, {})
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (400, 404):
            raise
        record = None
    data = (record or {}).get("data") if isinstance(record, dict) else None
    if not isinstance(data, dict):
        return ActorDetails(status="NOT_FOUND", actor=actor_id, message="There is no public Apify Actor with that id.")
    try:
        build = ((await fetch(path + "/builds/default", {})) or {}).get("data")
    except Exception:
        build = None  # the details still help without the input schema
    return parse_actor(actor_id, data, build)
