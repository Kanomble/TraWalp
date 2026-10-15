"""Only synthetic responses and clocks; no provider, credentials or DB mutations."""

import json
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_massive_reference import DUMMY_KEY, response
from test_massive_reference import clock as clock
from test_massive_reference import offline as offline

from trading_system.data import massive_identity_research as research
from trading_system.data import massive_reference as massive


@pytest.fixture
def make_client(monkeypatch, clock):
    monkeypatch.setattr(research, "_REQUEST_LIMITER", massive._REQUEST_LIMITER)
    clients = []

    def build(responses):
        client = research.MassiveIdentityResearchClient(DUMMY_KEY)
        clients.append(client)
        calls, pending = [], iter(responses)

        def get(url, **kwargs):
            calls.append({"url": url, "time": clock.now, **kwargs})
            return next(pending)

        monkeypatch.setattr(client._session, "get", get)
        return client, calls

    yield build
    for client in clients:
        client.close()


def test_details_are_date_bounded_and_keep_historical_identifiers(make_client):
    fields = {
        "ticker": "XYZ",
        "cik": "0000000001",
        "share_class_figi": "CLASS1",
        "composite_figi": "FIGI1",
        "type": "CS",
        "active": False,
        "list_date": "2020-01-01",
        "delisted_utc": "2021-01-01T00:00:00Z",
    }
    client, calls = make_client([response(results=fields)])
    result = client.historical_ticker_details("XYZ", date(2020, 5, 1))
    assert result["results"] == fields and result["http_status"] == 200
    assert "verified" not in result and "effective_start" not in result
    parts = urlsplit(calls[0]["url"])
    assert parts.netloc == "api.massive.com" and parts.path == "/v3/reference/tickers/XYZ"
    assert parse_qs(parts.query) == {"date": ["2020-05-01"]}
    assert calls[0]["headers"] == {"Authorization": f"Bearer {DUMMY_KEY}"}
    assert calls[0]["timeout"] == (10, 30) and not calls[0]["allow_redirects"]
    assert not client._session.trust_env
    assert DUMMY_KEY not in json.dumps(result)


def test_events_keep_provider_semantics_without_creating_intervals(make_client):
    event = {"date": "2025-01-01", "type": "ticker_change", "ticker_change": {"ticker": "NEW"}}
    client, calls = make_client([response(results={"events": [event]})])
    result = client.ticker_events("BBG000000001")
    assert result["results"] == {"events": [event]}
    assert parse_qs(urlsplit(calls[0]["url"]).query) == {"types": ["ticker_change"]}
    assert "epochs" not in result


@pytest.mark.parametrize(
    "identifier", ["XYZ?apiKey=secret", "../XYZ", "XYZ/other", "https://evil.test"]
)
def test_research_cannot_change_host_path_or_add_queries(make_client, identifier):
    client, calls = make_client([])
    with pytest.raises(ValueError, match="RESEARCH_EXACT_IDENTIFIER_REQUIRED"):
        client.ticker_events(identifier)
    assert calls == []


def test_access_denial_is_reported_without_response_body_or_retry(make_client):
    denied = response(403)
    denied._content = f'{{"message":"{DUMMY_KEY}"}}'.encode()
    client, calls = make_client([denied])
    result = client.historical_ticker_details("XYZ", date(2021, 1, 1))
    assert result["http_status"] == 403
    assert result["error"] == "MASSIVE_REFERENCE_ACCESS_DENIED" and len(calls) == 1
    assert DUMMY_KEY not in json.dumps(result)


def test_shared_budget_and_retry_after_remain_enforced(make_client, clock):
    # Normal reference requests and research reserve slots from the SAME limiter.
    for _ in range(5):
        massive._REQUEST_LIMITER.transmit(lambda: None)
    client, calls = make_client([response(429, retry_after=10), response(results={"events": []})])
    assert client.ticker_events("XYZ")["http_status"] == 200
    assert calls[0]["time"] == 60.5 and calls[1]["time"] >= 70.5
    assert client.diagnostics["research_http_attempts"] == 2
    assert client.diagnostics["research_retries"] == 1
    assert clock.sleeps == [60.5, 10.0]


def test_production_allowlist_is_not_extended():
    with pytest.raises(massive.MassiveReferenceError, match="ENDPOINT_NOT_ALLOWED"):
        massive._safe_url(
            "https://api.massive.com/v3/reference/tickers/XYZ?date=2020-01-01",
            "/v3/reference/tickers/XYZ",
        )


def test_guard_failure_stays_fail_closed_before_any_http_attempt(make_client, monkeypatch):
    client, calls = make_client([])

    def blocked(*args, **kwargs):
        raise massive.MassiveReferenceError("MASSIVE_RATE_LIMIT_CLOCK_ANOMALY")

    monkeypatch.setattr(research, "_REQUEST_LIMITER", SimpleNamespace(transmit=blocked))
    with pytest.raises(massive.MassiveReferenceError, match="CLOCK_ANOMALY"):
        client.ticker_events("XYZ")
    assert calls == [] and client.diagnostics["research_http_attempts"] == 0
