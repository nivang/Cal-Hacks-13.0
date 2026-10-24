"""Tests for discovery.py: reading a Bazaar style catalog and ranking it against a query. No network.

The find_services and quote tools that preview each listing against a task are tested in test_mcp_server.py.
"""
import httpx
import pytest

from sentinel import discovery
from sentinel.payments import NETWORK, USDC_BASE_SEPOLIA


def item(path, name, description, tags=(), amount="250000", **extra):
    return {
        "resource": f"http://market.test{path}", "type": "http",
        "accepts": [{"scheme": "exact", "network": NETWORK, "asset": USDC_BASE_SEPOLIA, "amount": amount}],
        "metadata": {"service_name": name, "description": description, "tags": list(tags)}, **extra,
    }


CATALOG = {"x402Version": 2, "items": [
    item("/crypto", "Signals", "Crypto trading signals.", ["crypto"]),
    item("/flights", "Flights", "Flight fares to Tokyo.", ["travel", "flights"]),
    item("/weather", "Weather", "10 day forecast for any city.", ["travel", "weather"], amount="50000"),
]}


def test_parse_reads_listings_and_the_base_sepolia_price():
    listings = discovery.parse(CATALOG)
    assert [x.name for x in listings] == ["Signals", "Flights", "Weather"]
    assert listings[1].url == "http://market.test/flights" and listings[1].tags == ("travel", "flights")
    assert listings[2].listed_amount == 50_000


def test_parse_skips_malformed_items_and_clips_seller_text():
    document = {"items": [
        "junk", {"type": "http"}, item("/x", "N" * 500, "D" * 5000, ["t" * 99] * 30, amount="lots"),
        {"resource": "http://market.test/bare"},
    ]}
    listings = discovery.parse(document)
    assert [x.url for x in listings] == ["http://market.test/x", "http://market.test/bare"]
    assert len(listings[0].name) == 100 and len(listings[0].description) == 500
    assert listings[0].tags == ("t" * 40,) * 10 and listings[0].listed_amount is None
    assert listings[1].name == "" and listings[1].listed_amount is None


@pytest.mark.parametrize("document", [None, [], {"items": "nope"}, {"x402Version": 2}])
def test_a_catalog_in_the_wrong_shape_is_a_discovery_error(document):
    with pytest.raises(discovery.DiscoveryError):
        discovery.parse(document)


def test_search_ranks_matches_first_and_keeps_the_rest_up_to_the_limit():
    listings = discovery.parse(CATALOG)
    ranked = discovery.search(listings, "Tokyo flights and the weather")
    assert [x.name for x in ranked] == ["Flights", "Weather", "Signals"]
    assert [x.name for x in discovery.search(listings, "flights", limit=1)] == ["Flights"]
    assert [x.name for x in discovery.search(listings, "zzz")] == ["Signals", "Flights", "Weather"]  # catalog order


def test_the_catalog_url_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(discovery.ENV_VAR, raising=False)
    assert discovery.catalog_url() == discovery.DEFAULT_URL
    monkeypatch.setenv(discovery.ENV_VAR, " https://bazaar.example/discovery/resources ")
    assert discovery.catalog_url() == "https://bazaar.example/discovery/resources"


@pytest.mark.anyio
@pytest.mark.parametrize("response", [
    httpx.Response(200, json=CATALOG), httpx.Response(500), httpx.Response(200, content=b"not json"),
])
async def test_fetch_reads_the_catalog_or_fails_with_a_safe_message(monkeypatch, response):
    real = httpx.AsyncClient
    monkeypatch.setattr(discovery.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda request: response), **kw))
    if response.status_code == 200 and response.content.startswith(b"{"):
        assert len(await discovery.fetch("http://market.test/discovery")) == 3
    else:
        with pytest.raises(discovery.DiscoveryError, match="could not read the service catalog"):
            await discovery.fetch("http://market.test/discovery")
