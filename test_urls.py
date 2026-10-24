"""Tests for the URL safety check. No real DNS: resolve() is patched where a host name is involved."""
import pytest

import urls


def test_plain_public_https_url_is_allowed(monkeypatch):
    monkeypatch.setattr(urls, "resolve", lambda host, port: ["93.184.216.34"])
    assert urls.validate_url("https://seller.example/benchmarks/email", local_ok=False) == "https://seller.example/benchmarks/email"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://seller.example/x", "javascript:alert(1)", "gopher://seller.example/",
])
def test_only_http_and_https_schemes(url):
    with pytest.raises(ValueError, match="http"):
        urls.validate_url(url, local_ok=False)


def test_embedded_credentials_and_missing_host_are_rejected():
    with pytest.raises(ValueError, match="credentials"):
        urls.validate_url("https://user:pass@seller.example/x", local_ok=False)
    with pytest.raises(ValueError, match="no host"):
        urls.validate_url("http:///path", local_ok=False)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1",
])
def test_private_loopback_and_metadata_addresses_are_rejected(address, monkeypatch):
    monkeypatch.setattr(urls, "resolve", lambda host, port: [address])
    with pytest.raises(ValueError, match="private or local"):
        urls.validate_url("http://anything.example/x", local_ok=False)


def test_one_private_address_among_public_ones_is_enough_to_reject(monkeypatch):
    monkeypatch.setattr(urls, "resolve", lambda host, port: ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(ValueError):
        urls.validate_url("http://rebind.example/x", local_ok=False)


@pytest.mark.parametrize("url", ["http://localhost:4000/x", "http://127.0.0.1/x", "http://2130706433/x", "http://[::1]/x"])
def test_loopback_spellings_are_rejected_without_a_patched_resolver(url):
    with pytest.raises(ValueError):
        urls.validate_url(url, local_ok=False)


def test_unresolvable_host_gives_a_clear_error(monkeypatch):
    def nope(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(urls, "resolve", nope)
    with pytest.raises(ValueError, match="Could not find"):
        urls.validate_url("http://nonexistent.invalid/x", local_ok=False)


def test_local_urls_are_allowed_only_when_the_flag_is_on(monkeypatch):
    monkeypatch.delenv("SENTINEL_ALLOW_LOCAL_URLS", raising=False)
    with pytest.raises(ValueError):
        urls.validate_url("http://localhost:4000/benchmarks/email")
    monkeypatch.setenv("SENTINEL_ALLOW_LOCAL_URLS", "1")
    assert urls.validate_url("http://localhost:4000/benchmarks/email") == "http://localhost:4000/benchmarks/email"
