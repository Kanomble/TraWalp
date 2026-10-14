"""No network, secrets or real sleeping: enforce the free reference safety contract."""

import json
import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from trading_system.data import massive_rate_limit as rate_limit
from trading_system.data import massive_reference as massive

DUMMY_KEY = "synthetic-credential-never-from-environment"
CATALOG = [
    {"code": code, "description": description, "asset_class": "stocks", "locale": "us"}
    for code, description in (
        ("CS", "Common Stock"),
        ("ETF", "Exchange Traded Fund"),
        ("PFD", "Preferred Stock"),
        ("ADRC", "American Depository Receipt Common"),
        ("WARRANT", "Warrant"),
        ("UNIT", "Unit"),
        ("RIGHT", "Right"),
        ("ETP", "Exchange Traded Product"),
        ("ETN", "Exchange Traded Note"),
        ("FUND", "Fund"),
        ("BOND", "Bond"),
        ("OTHER", "Other"),
        ("FUTURE_CODE", "A Newly Introduced Type"),
    )
]


def ticker(symbol="A", code="CS", cik="1"):
    return {
        "ticker": symbol,
        "type": code,
        "cik": cik,
        "market": "stocks",
        "locale": "us",
        "active": True,
        "primary_exchange": "XNYS",
        "composite_figi": f"FIGI-{symbol}",
        "share_class_figi": f"CLASS-{symbol}",
        "last_updated_utc": "2026-10-01T00:00:00Z",
    }


def response(status=200, results=None, *, next_url=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    payload = {"status": "OK", "results": results if results is not None else CATALOG}
    if next_url:
        payload["next_url"] = next_url
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    if retry_after is not None:
        result.headers["Retry-After"] = str(retry_after)
    return result


def forbidden(*args, **kwargs):
    raise AssertionError("Real network, environment credentials and sleeps are forbidden")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def wall_clock(self):
        return 1_700_000_000 + self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(rate_limit.time, "sleep", forbidden)
    # Provider tests never consult a real key, even if one is configured.
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)


@pytest.fixture
def clock(monkeypatch, tmp_path):
    clock = FakeClock()
    monkeypatch.setattr(
        massive,
        "_REQUEST_LIMITER",
        massive._RollingWindowLimiter(
            clock=clock.monotonic,
            wall_clock=clock.wall_clock,
            sleeper=clock.sleep,
            state_path=tmp_path / "massive-limiter.json",
        ),
    )
    return clock


@pytest.fixture
def client_factory(monkeypatch, clock):
    clients = []

    def build(responses, *, limit=5):
        client = massive.MassiveReferenceClient(DUMMY_KEY, request_limit_per_minute=limit)
        clients.append(client)
        pending = iter(responses)
        calls = []

        def get(url, **kwargs):
            calls.append({"url": url, "time": clock.now, **kwargs})
            value = next(pending)
            if isinstance(value, BaseException):
                raise value
            return value

        monkeypatch.setattr(client._session, "get", get)
        return client, calls

    yield build
    for client in clients:
        client.close()


def assert_rolling_cap(times, limit=5):
    # Every possible violating interval contains its first and last events.
    for first in times:
        assert sum(first <= t <= first + 60 for t in times) <= limit


def test_exact_five_then_sixth_waits(clock):
    limiter = massive._REQUEST_LIMITER
    times = []
    for _ in range(5):
        limiter.transmit(lambda: times.append(clock.now))
    assert times == [0] * 5 and clock.sleeps == []
    limiter.transmit(lambda: times.append(clock.now))
    assert times[-1] == 60.5 and clock.sleeps == [60.5]
    assert_rolling_cap(times)


def test_rolling_boundary_has_no_wall_clock_burst(clock):
    times = []
    clock.now = 59
    for _ in range(5):
        massive._REQUEST_LIMITER.transmit(lambda: times.append(clock.now))
    clock.now = 60
    for _ in range(11):
        massive._REQUEST_LIMITER.transmit(lambda: times.append(clock.now))
    assert times[5] == 119.5
    assert_rolling_cap(times)


def test_staggered_requests_and_transport_delay_remain_protected(clock):
    times = []
    for _ in range(5):
        massive._REQUEST_LIMITER.transmit(lambda: times.append(clock.now))
        clock.now += 9
    for _ in range(15):

        def delayed_send():
            clock.now += 7
            times.append(clock.now)
            clock.now += 3

        massive._REQUEST_LIMITER.transmit(delayed_send)
    assert_rolling_cap(times)


def test_types_pages_retries_and_multiple_clients_share_one_limiter(client_factory):
    pages = [
        response(
            results=[ticker(str(i))],
            next_url=(
                f"https://api.massive.com/v3/reference/tickers?cursor={i + 1}" if i < 7 else None
            ),
        )
        for i in range(8)
    ]
    a, acalls = client_factory([response(), response(503), *pages])
    b, bcalls = client_factory([response()])
    a.get_ticker_types()
    b.get_ticker_types()
    assert len(list(a.iterate_stock_tickers())) == 8
    times = sorted(c["time"] for c in acalls + bcalls)
    assert len(times) == 11
    assert_rolling_cap(times)
    assert a.diagnostics == {
        "massive_requests_total": 10,
        "massive_requests_ticker_types": 1,
        "massive_requests_ticker_pages": 9,
        "massive_retries": 1,
        "configured_request_limit_per_minute": 5,
        "paid_features_used": False,
        "allowed_endpoints_only": True,
    }
    assert acalls[1]["url"].endswith("market=stocks&active=true&limit=1000&sort=ticker&order=asc")
    for call in acalls + bcalls:
        assert urlsplit(call["url"]).path in massive.FREE_REFERENCE_CONTRACT.allowed_endpoints
        assert call["headers"] == {"Authorization": f"Bearer {DUMMY_KEY}"}
        assert not call["allow_redirects"] and DUMMY_KEY not in call["url"]


@pytest.mark.parametrize("limit", [6, 100, "unlimited", None, 0, -1, True, 5.1])
def test_rate_configuration_cannot_raise_or_disable_cap(limit):
    with pytest.raises(massive.MassiveReferenceError, match="MASSIVE_RATE_LIMIT_EXCEEDS_FREE_TIER"):
        massive.MassiveReferenceClient(DUMMY_KEY, request_limit_per_minute=limit)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_lower_limits_are_supported(clock, limit):
    times = []
    for _ in range(limit + 1):
        massive._REQUEST_LIMITER.transmit(lambda: times.append(clock.now), limit=limit)
    assert_rolling_cap(times, limit)


@pytest.mark.parametrize("path", [massive.TYPES_PATH, massive.TICKERS_PATH])
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "MASSIVE_AUTH_FAILED"),
        (402, "MASSIVE_PAYMENT_OR_PLAN_REQUIRED"),
        (403, "MASSIVE_REFERENCE_ACCESS_DENIED"),
    ],
)
def test_access_errors_stop_immediately_without_retry_or_fallback(
    client_factory, status, expected, path
):
    client, calls = client_factory([response(status)])
    with pytest.raises(massive.MassiveReferenceError, match=expected):
        if path == massive.TYPES_PATH:
            client.get_ticker_types()
        else:
            list(client.iterate_stock_tickers())
    assert len(calls) == 1 and urlsplit(calls[0]["url"]).path == path
    assert client.diagnostics["massive_retries"] == 0


@pytest.mark.parametrize(
    "status,expected",
    [(429, "MASSIVE_REFERENCE_RATE_LIMITED"), (503, "MASSIVE_REFERENCE_SERVER_FAILED")],
)
def test_bounded_retry_honors_retry_after(client_factory, status, expected):
    client, calls = client_factory([response(status, retry_after=75) for _ in range(4)])
    with pytest.raises(massive.MassiveReferenceError, match=expected):
        client.get_ticker_types()
    assert [c["time"] for c in calls] == [0, 75, 150, 225]
    assert client.diagnostics["massive_retries"] == 3
    assert_rolling_cap([c["time"] for c in calls])


def test_retry_waits_for_local_window_when_retry_after_is_short(client_factory):
    client, calls = client_factory(
        [response() for _ in range(4)] + [response(429, retry_after=1), response()]
    )
    for _ in range(5):
        client.get_ticker_types()
    assert calls[5]["time"] >= 60.5
    assert_rolling_cap([c["time"] for c in calls])
    assert client.diagnostics["massive_requests_total"] == 6


def test_network_failure_counts_and_exception_cannot_leak_key(client_factory):
    client, calls = client_factory(
        [requests.Timeout(f"https://bad?apiKey={DUMMY_KEY}") for _ in range(4)]
    )
    with pytest.raises(massive.MassiveReferenceError, match="NETWORK_FAILED") as error:
        client.get_ticker_types()
    assert DUMMY_KEY not in str(error.value) and error.value.__suppress_context__
    assert len(calls) == 4 and client.diagnostics["massive_retries"] == 3


@pytest.mark.parametrize(
    "next_url",
    [
        "https://api.massive.com/v3/reference/tickers/AAPL",
        "https://api.massive.com/v3/trades/AAPL",
        "https://api.massive.com/v3/quotes/AAPL",
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
        "https://api.massive.com/v2/aggs/ticker/AAPL",
        "https://api.massive.com/v3/reference/tickers/types",
        "https://evil.example/v3/reference/tickers",
        "http://api.massive.com/v3/reference/tickers",
        "https://api.massive.com@evil.example/v3/reference/tickers",
        "https://api.massive.com/v3/reference/tickers?market=crypto",
        "https://api.massive.com/v3/reference/tickers?active=false",
        "https://api.massive.com/v3/reference/tickers?limit=1001",
        "https://api.massive.com/v3/reference/tickers?cursor=a&cursor=b",
        "https://api.massive.com/v3/reference/tickers",
        "https://api.massive.com/v3/reference/tickers?cursor=",
    ],
)
def test_unapproved_pagination_never_reaches_transport(client_factory, next_url):
    client, calls = client_factory([response(results=[ticker()], next_url=next_url)])
    with pytest.raises(massive.MassiveReferenceError, match="ENDPOINT_NOT_ALLOWED"):
        list(client.iterate_stock_tickers())
    assert len(calls) == 1
    with pytest.raises(massive.MassiveReferenceError, match="ENDPOINT_NOT_ALLOWED"):
        client._get(next_url, urlsplit(next_url).path)
    assert len(calls) == 1


def test_query_credentials_are_removed_and_redirects_refused(client_factory):
    client, calls = client_factory(
        [
            response(
                results=[ticker()],
                next_url=f"https://api.massive.com/v3/reference/tickers?cursor=next&apiKey={DUMMY_KEY}",
            ),
            response(302),
        ]
    )
    with pytest.raises(massive.MassiveReferenceError, match="HTTP_FAILED"):
        list(client.iterate_stock_tickers())
    assert len(calls) == 2
    assert parse_qs(urlsplit(calls[1]["url"]).query) == {"cursor": ["next"]}
    assert all("apiKey" not in c["url"] and DUMMY_KEY not in c["url"] for c in calls)


def test_http_date_retry_after_and_invalid_values():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 88 <= massive._retry_after(future) <= 90
    for value in (None, "invalid", "nan", "inf", "-1"):
        assert massive._retry_after(value) == 0


def test_missing_key_has_explicit_status():
    with pytest.raises(massive.MassiveReferenceError, match="MASSIVE_API_KEY_MISSING"):
        massive.MassiveReferenceClient(None)


def test_provider_error_bodies_and_urls_never_appear_in_logs(client_factory, caplog):
    caplog.set_level("DEBUG")
    bad = response(403, results={"url": f"https://api.massive.com/?apiKey={DUMMY_KEY}"})
    client, calls = client_factory([bad])
    with pytest.raises(massive.MassiveReferenceError) as exc:
        client.get_ticker_types()
    assert DUMMY_KEY not in caplog.text + str(exc.value) + json.dumps(client.diagnostics)
    assert len(calls) == 1
