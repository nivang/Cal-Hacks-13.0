"""Texting the agent (texting.py): linking a handle, the agent's tools and turns, approvals by link, the relay routes.

The agent's model is a scripted fake (FakeClient); payments go through the pay spy, so the real guard, intent review
and ledger run on the test connection.
"""
import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import TEST_BASE_URL, form_value, provision_user, sign_in
from fastmcp.utilities.asgi_transport import run_asgi_lifespan

from sentinel import link_approvals, texting
from sentinel.web import users

PHONE = "+15551234567"
SECRET = "r" * 32
REQUEST = "Pull Acme Robotics' funding history for my meeting next week. Budget $1."
UNKNOWN = "0x" + "b" * 40


class Block(SimpleNamespace):
    """A content block as the SDK returns it: attributes plus to_dict()."""

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def say(text: str) -> list[Block]:
    return [Block(type="text", text=text)]


def use(name: str, arguments: dict[str, Any], call_id: str = "call") -> list[Block]:
    return [Block(type="tool_use", id=call_id, name=name, input=arguments)]


def last_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The JSON of the newest tool result in the history."""
    return json.loads(messages[-1]["content"][0]["content"])


class FakeClient:
    """Stands in for AsyncAnthropic: each create() answers with the next scripted step (blocks, or a function of the
    history returning blocks) and records the request."""

    def __init__(self, *steps: list[Block] | Callable[[list[dict[str, Any]]], list[Block]]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **request: Any) -> SimpleNamespace:
        self.requests.append({**request, "messages": copy.deepcopy(request["messages"])})
        step = self.steps.pop(0)
        content = step(request["messages"]) if callable(step) else step
        stop = "tool_use" if any(block.type == "tool_use" for block in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop)


@pytest.fixture
def make_desk(settings, connect, fake_wallets, pay_spy, fake_judge) -> Callable[..., texting.TextDesk]:
    def make(*steps: Any) -> texting.TextDesk:
        return texting.TextDesk(settings.base_url, settings.key("phone-link"), SECRET, connect=connect,
                                judge_=fake_judge, client=FakeClient(*steps), pay_fn=pay_spy)
    return make


def sent(desk: texting.TextDesk) -> list[str]:
    """Everything queued for the relay so far (and clear it)."""
    texts = [m.text for m in desk.outbox]
    desk.outbox.clear()
    return texts


async def linked_user(connect, desk: texting.TextDesk, email: str = "ada@example.com") -> users.Identity:
    identity = await provision_user(connect, email)
    with connect() as conn:
        code = texting.issue_code(conn, desk.link_key, identity.user_id)
    await desk.handle(PHONE, code)
    assert sent(desk) == [texting.LINKED]
    return identity


# Pure helpers


@pytest.mark.parametrize("raw,expected", [
    ("+1 (555) 123-4567", PHONE), ("5551234567", PHONE), ("15551234567", PHONE), ("+447700900123", "+447700900123"),
    ("Ada@Example.com", "ada@example.com"), ("12345", None), ("", None), (None, None), ("not a handle", None),
])
def test_handles_are_normalized(raw, expected):
    assert texting.normalize_handle(raw) == expected


def test_codes_use_only_the_alphabet_and_a_linked_user_must_send_the_exact_code():
    assert texting.find_code("my code is abcd-efgh thanks") == "ABCDEFGH"
    assert texting.find_code("APPROVED") is None  # O is not in the alphabet
    assert texting.find_code("STRENGTH") == "STRENGTH"  # an unlinked handle may send it without the dash
    assert texting.find_code("STRENGTH", exact=True) is None  # a linked user's word is never taken for a code
    assert texting.find_code("stre-ngth", exact=True) == "STRENGTH"


def test_long_replies_split_at_line_breaks():
    parts = texting.chunks("a" * 1000 + "\n\n" + "b" * 1000, size=1500)
    assert parts == ["a" * 1000, "b" * 1000]
    assert texting.chunks("x" * 3100, size=1500) == ["x" * 1500, "x" * 1500, "x" * 100]


def test_trimming_keeps_tool_calls_with_their_results():
    turn = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{}]},
            {"role": "user", "content": [{"type": "tool_result"}]}, {"role": "assistant", "content": [{}]}]
    kept = texting.trim(turn * 3, limit=6)
    assert kept == turn  # cut only before an owner text


# Links


def test_a_code_links_once_and_relinking_replaces(conn, settings):
    key = settings.key("phone-link")
    ada = users.find_or_create_user(conn, "ada@example.com", "ada@example.com")["id"]
    bob = users.find_or_create_user(conn, "bob@example.com", "bob@example.com")["id"]
    code = texting.issue_code(conn, key, ada).replace("-", "")
    assert texting.link(conn, key, PHONE, code) == ada
    assert texting.link(conn, key, PHONE, code) is None  # spent
    assert texting.linked_user(conn, PHONE) == ada and texting.linked_handle(conn, ada) == PHONE
    assert texting.link(conn, key, PHONE, texting.issue_code(conn, key, bob).replace("-", "")) == bob
    assert texting.linked_handle(conn, ada) is None  # one owner per handle
    texting.unlink(conn, bob)
    assert texting.linked_user(conn, PHONE) is None


def test_a_new_code_cancels_the_old_one_and_codes_expire(conn, settings):
    key = settings.key("phone-link")
    ada = users.find_or_create_user(conn, "ada@example.com", "ada@example.com")["id"]
    first = texting.issue_code(conn, key, ada).replace("-", "")
    second = texting.issue_code(conn, key, ada).replace("-", "")
    assert texting.link(conn, key, PHONE, first) is None
    conn.execute("UPDATE phone_link_codes SET expires_at = now() - interval '1 second'")
    assert texting.link(conn, key, PHONE, second) is None


def test_sign_out_everywhere_unlinks_the_phone(conn, settings):
    key = settings.key("phone-link")
    ada = users.find_or_create_user(conn, "ada@example.com", "ada@example.com")["id"]
    texting.link(conn, key, PHONE, texting.issue_code(conn, key, ada).replace("-", ""))
    users.sign_out_everywhere(conn, ada)
    assert texting.linked_user(conn, PHONE) is None


# The desk


@pytest.mark.anyio
async def test_an_unlinked_handle_only_gets_linking_instructions(make_desk):
    desk = make_desk()
    await desk.handle(PHONE, "Buy me the Acme funding data")
    assert sent(desk) == [texting.NOT_LINKED.format(base=TEST_BASE_URL)]
    assert desk.client.requests == []  # the agent never ran


@pytest.mark.anyio
async def test_wrong_codes_lock_the_handle_out(make_desk, connect):
    desk = make_desk()
    identity = await provision_user(connect, "ada@example.com")
    for _ in range(texting.LINK_TRIES):
        await desk.handle(PHONE, "WXYZ-WXYZ")
    with connect() as conn:
        code = texting.issue_code(conn, desk.link_key, identity.user_id)
    await desk.handle(PHONE, code)  # even the right code waits out the window
    assert sent(desk)[-1] == texting.TOO_MANY_TRIES.format(base=TEST_BASE_URL)
    with connect() as conn:
        assert texting.linked_user(conn, PHONE) is None


@pytest.mark.anyio
async def test_the_agent_has_only_sentinel_tools_and_no_web_access(make_desk, connect):
    desk = make_desk(say("Hello."))
    await linked_user(connect, desk)
    await desk.handle(PHONE, "hi there")
    request = desk.client.requests[0]
    assert {tool["name"] for tool in request["tools"]} == {"start_task", "find_services", "quote", "pay",
                                                           "resume_payment"}
    assert all("type" not in tool or tool["type"] == "custom" for tool in request["tools"])  # no server web tools
    assert "no web search" in request["system"] and "Cite the source URL" in request["system"]
    assert sent(desk) == ["Hello."]


@pytest.mark.anyio
async def test_a_text_becomes_a_task_in_the_owners_words_and_a_paid_answer(make_desk, connect, pay_spy, fake_judge):
    def pay_step(messages):
        task_id = last_result(messages)["task_id"]
        return use("pay", {"task_id": task_id, "url": "http://dataco.test/funding",
                           "reason": "The briefing needs Acme's funding rounds; DataCo lists them."}, "c2")

    desk = make_desk(use("start_task", {"budget_usd": "1.00"}, "c1"), pay_step,
                     say("Acme raised a Series B.\nhttp://dataco.test/funding\nPaid $0.50 to DataCo."))
    identity = await linked_user(connect, desk)
    await desk.handle(PHONE, REQUEST)
    assert fake_judge.tasks == [REQUEST]  # the owner's own text, not anything the model wrote
    with connect() as conn:
        task = conn.execute("SELECT task_text FROM mandates ORDER BY id DESC LIMIT 1").fetchone()
    assert task["task_text"] == REQUEST
    assert [call[2] for call in pay_spy.calls] == [identity.agent]
    texts = sent(desk)
    assert texts[0].startswith("On it: ") and "Budget $1" in texts[0]
    assert texts[-1].startswith("Acme raised a Series B.")


@pytest.mark.anyio
async def test_a_new_seller_texts_an_approval_link_and_resume_pays_only_after_yes(make_desk, connect, pay_spy):
    def pay_step(messages):
        task_id = last_result(messages)["task_id"]
        return use("pay", {"task_id": task_id, "url": "http://new.test/funding",
                           "reason": "The briefing needs Acme's funding rounds; this seller lists them.",
                           "found_at": "owner"}, "c2")

    desk = make_desk(use("start_task", {}, "c1"), pay_step, say("Waiting for your approval."))
    identity = await linked_user(connect, desk)
    pay_spy.pay_to = UNKNOWN
    await desk.handle(PHONE, REQUEST)
    for task in list(desk.background):  # the approval watcher; the test answers for it below
        task.cancel()
    texts = sent(desk)
    approval = next(t for t in texts if "/account/approvals/" in t)
    request_id = int(approval.rsplit("/", 1)[1])
    assert approval.startswith("Approve $0.50 to new.test?")
    assert last_result(desk.conversations[PHONE].messages[:-1])["status"] == "APPROVAL_REQUIRED"

    pending = await desk.resume(identity, PHONE, request_id)
    assert pending.status == "APPROVAL_REQUIRED" and len(pay_spy.calls) == 1  # not authorized, nothing paid
    with connect() as conn:
        link_approvals.decide(conn, identity.agent, request_id, identity.user_id, "yes")
    paid = await desk.resume(identity, PHONE, request_id)
    assert paid.status == "SETTLED" and len(pay_spy.calls) == 2


@pytest.mark.anyio
async def test_the_watcher_tells_the_agent_once_the_owner_answers(make_desk, connect, pay_spy, monkeypatch):
    monkeypatch.setattr(texting, "APPROVAL_POLL_S", 0.01)

    def pay_step(messages):
        task_id = last_result(messages)["task_id"]
        return use("pay", {"task_id": task_id, "url": "http://new.test/funding",
                           "reason": "The briefing needs Acme's funding rounds; this seller lists them.",
                           "found_at": "owner"}, "c2")

    def resume_step(messages):
        note = messages[-1]["content"]
        assert note.startswith("Sentinel: the owner approved request")
        return use("resume_payment", {"request_id": int(note.split("request ")[1].split(" ")[0])}, "c3")

    desk = make_desk(use("start_task", {}, "c1"), pay_step, say("Waiting for your approval."), resume_step,
                     say("Got it: Acme raised a Series B."))
    identity = await linked_user(connect, desk)
    pay_spy.pay_to = UNKNOWN
    await desk.handle(PHONE, REQUEST)
    request_id = int(next(t for t in sent(desk) if "/account/approvals/" in t).rsplit("/", 1)[1])
    with connect() as conn:
        link_approvals.decide(conn, identity.agent, request_id, identity.user_id, "yes")
    await asyncio.wait_for(asyncio.gather(*desk.background), 5)
    assert sent(desk)[-1] == "Got it: Acme raised a Series B."
    assert len(pay_spy.calls) == 2


@pytest.mark.anyio
async def test_a_failed_turn_is_undone_and_the_owner_is_told(make_desk, connect):
    def broken(_messages):
        raise RuntimeError("the API is down")

    desk = make_desk(broken)
    await linked_user(connect, desk)
    await desk.handle(PHONE, "hello agent")
    assert sent(desk) == [texting.BROKE]
    assert desk.conversations[PHONE].messages == []


@pytest.mark.anyio
async def test_pay_needs_a_task_of_this_user(make_desk, connect, pay_spy):
    desk = make_desk(use("pay", {"task_id": 999999, "url": "http://dataco.test/x",
                                 "reason": "Trying a task that is not mine at all."}, "c1"),
                     lambda messages: say(last_result(messages)["status"]))
    await linked_user(connect, desk)
    await desk.handle(PHONE, "pay that")
    assert sent(desk) == ["BLOCKED"] and pay_spy.calls == []


@pytest.mark.anyio
async def test_stop_unlinks(make_desk, connect):
    desk = make_desk()
    await linked_user(connect, desk)
    await desk.handle(PHONE, "STOP")
    assert sent(desk) == [texting.UNLINKED]
    with connect() as conn:
        assert texting.linked_user(conn, PHONE) is None


# Routes


@pytest.fixture
async def texting_app(settings, connect, mailer, fake_wallets, pay_spy, fake_judge) -> AsyncIterator[Any]:
    from sentinel.web.app import create_app

    desk = texting.TextDesk(settings.base_url, settings.key("phone-link"), SECRET, connect=connect,
                            judge_=fake_judge, client=FakeClient(), pay_fn=pay_spy)
    application = create_app(settings, connect=connect, mailer=mailer, pay_fn=pay_spy, judge_=fake_judge,
                             texts=desk)

    async def with_state(scope: Any, receive: Any, send: Any) -> None:
        await application({**scope, "state": {}}, receive, send)

    async with run_asgi_lifespan(with_state):
        yield application, desk


def client_for(application: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application, client=("203.0.113.10", 50000)),
                             base_url=TEST_BASE_URL, follow_redirects=False, headers={"origin": TEST_BASE_URL})


@pytest.mark.anyio
async def test_relay_routes_need_the_secret(texting_app):
    application, desk = texting_app
    async with client_for(application) as client:
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": SECRET}):
            assert (await client.post("/texting/inbound", json={"from": PHONE, "text": "hi"},
                                      headers=headers)).status_code == 404
            assert (await client.get("/texting/outbox?wait=0", headers=headers)).status_code == 404
        good = {"authorization": f"Bearer {SECRET}"}
        assert (await client.post("/texting/inbound", json={"from": "nope", "text": "hi"},
                                  headers=good)).status_code == 400
        assert (await client.post("/texting/inbound", json={"from": "(555) 123-4567", "text": "hi"},
                                  headers=good)).status_code == 202
        await asyncio.wait_for(asyncio.gather(*desk.background), 5)
        box = (await client.get("/texting/outbox?wait=1", headers=good)).json()
    assert box == {"messages": [{"to": "(555) 123-4567", "text": texting.NOT_LINKED.format(base=TEST_BASE_URL)}]}


@pytest.mark.anyio
async def test_texting_routes_are_off_without_a_desk(app, browser):
    client = browser()
    response = await client.post("/texting/inbound", json={"from": PHONE, "text": "hi"},
                                 headers={"authorization": f"Bearer {SECRET}"})
    assert response.status_code == 404


@pytest.mark.anyio
async def test_the_account_page_links_a_phone(texting_app, mailer):
    application, desk = texting_app
    async with client_for(application) as client:
        await sign_in(client, mailer, "ada@example.com")
        page = await client.get("/account/texting")
        assert page.status_code == 200 and "Get a code" in page.text
        shown = await client.post("/account/texting/code", data={"csrf": form_value(page.text, "csrf")})
        code = shown.text.split('select-all">')[1].split("<")[0]
        await desk.handle(PHONE, code)
        assert sent(desk) == [texting.LINKED]
        page = await client.get("/account/texting")
        assert "•••4567" in page.text
        unlinked = await client.post("/account/texting/unlink", data={"csrf": form_value(page.text, "csrf")})
        assert unlinked.status_code == 303
    with desk.connect() as conn:
        assert texting.linked_user(conn, PHONE) is None


def test_from_env_is_off_without_a_secret_and_refuses_a_short_one(settings):
    key = settings.key("phone-link")
    assert texting.from_env({}, settings.base_url, key) is None
    with pytest.raises(ValueError):
        texting.from_env({texting.SECRET_VAR: "short"}, settings.base_url, key)

