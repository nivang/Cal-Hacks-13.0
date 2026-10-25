"""Credit purchases through MCP: real policy/database, fake Apify, no live spending."""
import asyncio
import json
from typing import Any

import httpx
import pytest
from fastmcp import Client
from test_mcp_server import alice, call, make_server, opener, provision, tokens

from sentinel import attempts, db, guard, mandates

# Reuse the MCP identity and server fixtures, rather than bypassing current_identity in these tests.
__all__ = ["alice", "make_server", "opener", "tokens"]
pytestmark = pytest.mark.anyio


class ErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":{"type":"invalid-input","message":"secret provider details apify_test_secret"}}'


class ApifyAPI:
    def __init__(self) -> None:
        self.starts: list[httpx.Request] = []
        self.calls: list[httpx.Request] = []
        self.start_error: int | str | None = None
        self.final_status = "SUCCEEDED"
        self.cost: float | None = 0.00321
        self.model = 'PAY_PER_EVENT'
        self.minimum = 0
        self.dataset_items = [{'title': 'Example Domain', 'url': 'https://example.com'}]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.respond), follow_redirects=False)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == 'api.apify.com' and 'token' not in request.url.params
        assert request.headers['authorization'] == 'Bearer apify_test_secret'
        if request.method == 'POST':
            self.starts.append(request)
            if self.start_error == 'timeout':
                raise httpx.ReadTimeout('secret provider details', request=request)
            if isinstance(self.start_error, int):
                return httpx.Response(self.start_error, stream=ErrorStream())
            return httpx.Response(201, json={'data': {'id': 'run123', 'status': 'RUNNING',
                                                     'defaultDatasetId': 'dataset123'}})
        if request.url.path.endswith('/items'):
            return httpx.Response(200, json=self.dataset_items)
        if '/records/OUTPUT' in request.url.path:
            return httpx.Response(200, json={'message': 'Hello world!'})
        if '/actor-runs/' in request.url.path:
            return httpx.Response(200, json={'data': {'id': 'run123', 'status': self.final_status,
                                                     'defaultDatasetId': 'dataset123', 'usageTotalUsd': self.cost,
                                                     'defaultKeyValueStoreId': 'store123'}})
        if request.url.path.endswith('/builds/default'):
            return httpx.Response(200, json={'data': {'buildNumber': '3.0.22'}})
        return httpx.Response(200, json={'data': {
            'title': 'Example data scraper', 'description': 'Extract public website text.',
            'pricingInfos': [{'pricingModel': self.model, 'minimalMaxTotalChargeUsd': self.minimum}],
        }})


@pytest.fixture
def credit_api(monkeypatch):
    monkeypatch.setenv('APIFY_TOKEN', 'apify_test_secret')
    monkeypatch.setenv('APIFY_MAX_RUN_USD', '1')
    monkeypatch.setenv('APIFY_DAILY_BUDGET_USD', '5')
    return ApifyAPI()


async def purchase(make_server: Any, credit_api: ApifyAPI, **changes: Any):
    server = make_server(apify_client_factory=credit_api.client)
    task = await call(server, 'start_task', task='Buy example website text for testing the Apify credits integration.',
                      budget_usd='1')
    args = {'task_id': task['task_id'], 'actor': 'apify/example', 'actor_input': {'url': 'https://example.com'},
            'max_charge_usd': '0.05', 'reason': 'Test the credit integration by extracting the example website text.'}
    args.update(changes)
    return server, args, await call(server, 'run_apify_actor', **args)


async def test_credit_purchase_reserves_then_reconciles_cost_and_returns_data(make_server, alice, conn, credit_api,
                                                                           fake_judge, opener):
    server, args, started = await purchase(make_server, credit_api)
    assert started['status'] == 'RUNNING' and started['max_charge_usd'] == '0.050000'
    aid = db.agent_id(conn, alice.agent)
    assert mandates.spend(conn, args['task_id']) == 50_000
    assert attempts.week_spend(conn, aid, 7) == 50_000
    request = credit_api.starts[0]
    assert request.url.params['maxTotalChargeUsd'] == '0.050000'
    assert request.url.params['timeout'] == '120' and request.url.params['restartOnError'] == 'false'
    assert request.url.params['forcePermissionLevel'] == 'LIMITED_PERMISSIONS'
    assert request.url.params['build'] == '3.0.22'
    assert json.loads(request.content) == args['actor_input']
    assert fake_judge.classified[-1]['actor_input'] == args['actor_input']
    finished = await call(server, 'apify_run_status', request_id=started['request_id'])
    assert finished['status'] == 'SUCCEEDED' and finished['cost_usd'] == '0.003210'
    assert finished['items'][0]['title'] == 'Example Domain' and not opener.calls
    assert mandates.spend(conn, args['task_id']) == 3210
    assert attempts.week_spend(conn, aid, 7) == 3210
    assert conn.execute('SELECT count(*) AS n FROM journal_entries').fetchone()['n'] == 0
    repeat = await call(server, 'run_apify_actor', **args)
    assert repeat['request_id'] == started['request_id'] and len(credit_api.starts) == 1


@pytest.mark.parametrize('failure,status,reserved', [('timeout', 'UNKNOWN', 50_000), (500, 'UNKNOWN', 50_000),
                                                    (400, 'REJECTED', 0)])
async def test_start_failures_never_retry_or_expose_provider_secrets(make_server, alice, conn, credit_api,
                                                                  failure, status, reserved):
    credit_api.start_error = failure
    server, args, first = await purchase(make_server, credit_api)
    assert first['status'] == status and 'secret provider' not in str(first)
    assert 'apify_test_secret' not in str(first)
    if failure == 400:
        assert '(invalid-input)' in first['message']
    assert mandates.spend(conn, args['task_id']) == reserved
    repeated = await call(server, 'run_apify_actor', **args)
    assert repeated['request_id'] == first['request_id'] and len(credit_api.starts) == 1


@pytest.mark.parametrize('constraint', ['missing_token', 'cap', 'task_budget', 'weekly', 'daily', 'off_task',
                                      'judge_down', 'rental', 'minimum', 'revoked'])
async def test_credit_checks_refuse_before_spending(make_server, alice, conn, credit_api, fake_judge,
                                                   monkeypatch, constraint):
    if constraint == 'missing_token':
        monkeypatch.delenv('APIFY_TOKEN')
    elif constraint == 'cap':
        monkeypatch.setenv('APIFY_MAX_RUN_USD', '0.01')
    elif constraint == 'weekly':
        conn.execute("UPDATE agents SET weekly_budget = 0.01 WHERE name = %s", (alice.agent,))
    elif constraint == 'daily':
        monkeypatch.setenv('APIFY_DAILY_BUDGET_USD', '0.01')
    elif constraint == 'off_task':
        fake_judge.fit = 'off_task'
    elif constraint == 'judge_down':
        fake_judge.fail_classify = True
    elif constraint == 'rental':
        credit_api.model = 'FLAT_PRICE_PER_MONTH'
    elif constraint == 'minimum':
        credit_api.minimum = 0.5
    changes: dict[str, Any] = {}
    if constraint in ('revoked', 'task_budget'):
        server = make_server(apify_client_factory=credit_api.client)
        task = await call(server, 'start_task', task='Test Apify credit purchases of example website text.',
                          **({'budget_usd': '0.01'} if constraint == 'task_budget' else {}))
        if constraint == 'revoked':
            mandates.revoke(conn, task['task_id'], db.agent_id(conn, alice.agent))
        changes['task_id'] = task['task_id']
    _, _, response = await purchase(make_server, credit_api, **changes)
    assert response['status'] == 'BLOCKED' and not credit_api.starts


async def test_another_user_cannot_read_or_start_under_a_task(make_server, alice, conn, credit_api, tokens):
    server, args, started = await purchase(make_server, credit_api)
    tokens.use(provision(conn, 'bob-credit@example.com'))
    fetched = await call(server, 'apify_run_status', request_id=started['request_id'])
    assert fetched['status'] == 'NOT_FOUND'
    rejected = await call(server, 'run_apify_actor', **args)
    assert rejected['status'] == 'BLOCKED' and len(credit_api.starts) == 1


async def test_failed_actor_still_reconciles_its_charge(make_server, alice, conn, credit_api):
    server, args, started = await purchase(make_server, credit_api)
    credit_api.final_status = 'FAILED'
    finished = await call(server, 'apify_run_status', request_id=started['request_id'])
    assert finished['status'] == 'FAILED' and finished['cost_usd'] == '0.003210' and finished['items'] == []
    assert mandates.spend(conn, args['task_id']) == 3210


async def test_unreported_final_cost_stays_reserved(make_server, alice, conn, credit_api):
    server, args, started = await purchase(make_server, credit_api)
    credit_api.cost = None
    finished = await call(server, 'apify_run_status', request_id=started['request_id'])
    assert finished['cost_usd'] is None and mandates.spend(conn, args['task_id']) == 50_000


async def test_actors_with_json_output_and_no_dataset_return_the_output(make_server, alice, credit_api):
    credit_api.dataset_items = []
    server, _, started = await purchase(make_server, credit_api)
    finished = await call(server, 'apify_run_status', request_id=started['request_id'])
    assert finished['output'] == {'message': 'Hello world!'} and finished['items'] == []


async def test_credit_reservation_also_restricts_x402_spending(make_server, alice, conn, credit_api):
    _, _, started = await purchase(make_server, credit_api)
    conn.execute('UPDATE agents SET weekly_budget = 0.06 WHERE name = %s', (alice.agent,))
    from test_mcp_server import ASSET, DATACO, NETWORK

    from sentinel import payments

    quote = guard.Quote('http://seller.test/data', DATACO, 20_000, ASSET, NETWORK, 300)
    with pytest.raises(payments.PaymentRejected) as rejected:
        payments.authorize(conn, alice.agent, 'Buy more data', quote)
    assert rejected.value.rule == 'budget'
    assert started['status'] == 'RUNNING'


async def test_credit_tools_require_authentication(make_server, tokens, credit_api):
    tokens.access = None
    server = make_server(apify_client_factory=credit_api.client)
    async with Client(server) as client:
        response = await client.call_tool('apify_run_status', {'request_id': 1}, raise_on_error=False)
        assert response.is_error
    assert not credit_api.calls


async def test_task_revoked_while_the_model_reviews_cannot_spend(make_server, alice, conn, credit_api, fake_judge):
    classify = fake_judge.classify

    async def revoke_during_review(case):
        opinion = await classify(case)
        conn.execute("UPDATE mandates SET status = 'revoked' WHERE agent_id = %s", (db.agent_id(conn, alice.agent),))
        return opinion

    fake_judge.classify = revoke_during_review
    _, _, response = await purchase(make_server, credit_api)
    assert response['status'] == 'BLOCKED' and not credit_api.starts


@pytest.mark.parametrize('same_input', [True, False])
async def test_inflight_reservation_prevents_double_spending(make_server, alice, conn, credit_api, same_input):
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.method == 'POST':
            entered.set()
            await release.wait()
        return credit_api.respond(request)

    server = make_server(apify_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    task = await call(server, 'start_task', task='Test credit purchases of example website text.', budget_usd='1')
    conn.execute('UPDATE agents SET weekly_budget = 0.075 WHERE name = %s', (alice.agent,))
    args = {'task_id': task['task_id'], 'actor': 'apify/example', 'actor_input': {'url': 'https://example.com'},
            'max_charge_usd': '0.05', 'reason': 'Test credits by fetching the example website text.'}
    first = asyncio.create_task(call(server, 'run_apify_actor', **args))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        second_args = args if same_input else {**args, 'actor_input': {'url': 'https://example.org'}}
        second = await call(server, 'run_apify_actor', **second_args)
    finally:
        release.set()
        started = await first
    assert len(credit_api.starts) == 1
    if same_input:
        assert second['request_id'] == started['request_id']
    else:
        assert second['status'] == 'BLOCKED' and 'weekly budget' in second['message']
