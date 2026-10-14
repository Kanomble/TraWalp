"""Only Stocks Basic Free reference GETs; no market data or account operations."""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

TICKERS_PATH = "/v3/reference/tickers"
TYPES_PATH = "/v3/reference/tickers/types"
_ORIGIN = "https://api.massive.com"


@dataclass(frozen=True)
class MassiveFreeReferenceContract:
    provider: str = "MASSIVE"
    required_access_tier: str = "STOCKS_BASIC_FREE_COMPATIBLE"
    paid_features_allowed: bool = False
    request_limit_per_minute: int = 5
    allowed_endpoints: tuple = (TICKERS_PATH, TYPES_PATH)


FREE_REFERENCE_CONTRACT = MassiveFreeReferenceContract()


class MassiveReferenceError(ValueError):
    """Fixed status only: never attach provider bodies, URLs, headers or credentials."""


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 5:
        raise MassiveReferenceError("MASSIVE_RATE_LIMIT_EXCEEDS_FREE_TIER")


class _RollingWindowLimiter:
    """Serialize transmissions, including across clients and failed attempts.

    Reserve before entering transport; conservatively move the timestamp to
    completion in finally. Thus scheduling/transport delay cannot shorten the
    protected window. Keep the lock through transport: no parallel requests.
    Production uses exactly one module-level instance. Clock/sleeper injection
    is for deterministic tests; there is no CLI/env bypass.
    """

    _expiry_seconds = 60.5

    def __init__(self, *, clock=time.monotonic, sleeper=time.sleep):
        self._clock = clock
        self._sleeper = sleeper
        self._timestamps = deque()
        self._lock = threading.Lock()

    def transmit(self, operation, *, limit=5, not_before=0):
        _validate_limit(limit)
        with self._lock:
            while True:
                now = self._clock()
                while self._timestamps and now - self._timestamps[0] >= self._expiry_seconds:
                    self._timestamps.popleft()
                due = not_before
                if len(self._timestamps) >= limit:
                    due = max(due, self._timestamps[0] + self._expiry_seconds)
                if due <= now:
                    break
                self._sleeper(due - now)
            self._timestamps.append(self._clock())
            try:
                return operation()
            finally:
                self._timestamps[-1] = self._clock()


_REQUEST_LIMITER = _RollingWindowLimiter()


def _safe_url(url, expected_path):
    """Validate every URL before transport and discard query-string credentials."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "api.massive.com"
            or parts.path not in (TICKERS_PATH, TYPES_PATH)
            or parts.path != expected_path
            or parts.fragment
        ):
            raise ValueError
        query = [(k, v) for k, v in parse_qsl(parts.query) if k.casefold() != "apikey"]
        if len({k for k, _ in query}) != len(query):
            raise ValueError
        approved = {"cursor", "market", "active", "limit", "order", "sort"}
        if expected_path == TYPES_PATH:
            approved = {"asset_class", "locale"}
        if any(k not in approved for k, _ in query):
            raise ValueError
        values = dict(query)
        if expected_path == TYPES_PATH and (
            values.get("asset_class") != "stocks" or values.get("locale") != "us"
        ):
            raise ValueError
        if (
            expected_path == TICKERS_PATH
            and not values.get("cursor")
            and (values.get("market") != "stocks" or values.get("active") != "true")
        ):
            raise ValueError
        for key, expected in {
            "market": "stocks",
            "active": "true",
            "asset_class": "stocks",
            "locale": "us",
        }.items():
            if key in values and values[key] != expected:
                raise ValueError
        if "limit" in values and not 1 <= int(values["limit"]) <= 1000:
            raise ValueError
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except (ValueError, TypeError, AttributeError):
        raise MassiveReferenceError("MASSIVE_REFERENCE_ENDPOINT_NOT_ALLOWED") from None


def _retry_after(value):
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


class MassiveReferenceClient:
    """The only public data operations are types and sequential stock tickers.

    Requests' automatic retries and redirects are disabled. Every HTTP attempt
    enters the shared limiter. Only the explicit reference-sync CLI constructs
    this client. Secrets are supplied by that command, never read here.
    """

    def __init__(self, api_key, *, request_limit_per_minute=5):
        _validate_limit(request_limit_per_minute)
        if not api_key or not api_key.strip():
            raise MassiveReferenceError("MASSIVE_API_KEY_MISSING")
        self._api_key = api_key.strip()
        self._limit = request_limit_per_minute
        self._session = requests.Session()
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self._session.trust_env = False
        self._counts = {
            "massive_requests_total": 0,
            "massive_requests_ticker_types": 0,
            "massive_requests_ticker_pages": 0,
            "massive_retries": 0,
        }

    def close(self):
        self._session.close()

    @property
    def diagnostics(self):
        return {
            **self._counts,
            "configured_request_limit_per_minute": self._limit,
            "paid_features_used": False,
            "allowed_endpoints_only": True,
        }

    def _get(self, url, path):
        url = _safe_url(url, path)
        not_before = 0
        for attempt in range(4):  # One original attempt and at most three retries.

            def send(current_attempt=attempt):
                self._counts["massive_requests_total"] += 1
                key = (
                    "massive_requests_ticker_types"
                    if path == TYPES_PATH
                    else "massive_requests_ticker_pages"
                )
                self._counts[key] += 1
                if current_attempt:
                    self._counts["massive_retries"] += 1
                return self._session.get(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=(10, 30),
                    allow_redirects=False,
                )

            try:
                response = _REQUEST_LIMITER.transmit(send, limit=self._limit, not_before=not_before)
            except requests.RequestException:
                if attempt == 3:
                    raise MassiveReferenceError("MASSIVE_REFERENCE_NETWORK_FAILED") from None
                not_before = _REQUEST_LIMITER._clock() + 2 ** (attempt + 1)
                continue
            try:
                status = response.status_code
                access_errors = {
                    401: "MASSIVE_AUTH_FAILED",
                    402: "MASSIVE_PAYMENT_OR_PLAN_REQUIRED",
                    403: "MASSIVE_REFERENCE_ACCESS_DENIED",
                }
                if status in access_errors:
                    raise MassiveReferenceError(access_errors[status])
                if status == 429 or 500 <= status <= 599:
                    if attempt == 3:
                        raise MassiveReferenceError(
                            "MASSIVE_REFERENCE_RATE_LIMITED"
                            if status == 429
                            else "MASSIVE_REFERENCE_SERVER_FAILED"
                        )
                    delay = max(
                        2 ** (attempt + 1), _retry_after(response.headers.get("Retry-After"))
                    )
                    not_before = _REQUEST_LIMITER._clock() + delay
                    continue
                if status != 200:
                    raise MassiveReferenceError("MASSIVE_REFERENCE_HTTP_FAILED")
                try:
                    payload = response.json()
                except (ValueError, requests.RequestException):
                    raise MassiveReferenceError("MASSIVE_REFERENCE_INVALID_RESPONSE") from None
                if not isinstance(payload, dict) or payload.get("status") != "OK":
                    raise MassiveReferenceError("MASSIVE_REFERENCE_INVALID_RESPONSE")
                return payload
            finally:
                response.close()
        raise AssertionError("Unreachable bounded retry loop")

    def get_ticker_types(self):
        payload = self._get(f"{_ORIGIN}{TYPES_PATH}?asset_class=stocks&locale=us", TYPES_PATH)
        results = payload.get("results")
        if isinstance(results, dict):
            results = [results]
        if not isinstance(results, list) or not results:
            raise MassiveReferenceError("MASSIVE_REFERENCE_INVALID_TYPE_CATALOG")
        return results

    def iterate_stock_tickers(self):
        url = f"{_ORIGIN}{TICKERS_PATH}?market=stocks&active=true&limit=1000&sort=ticker&order=asc"
        seen = set()
        while url:
            url = _safe_url(url, TICKERS_PATH)
            if url in seen:
                raise MassiveReferenceError("MASSIVE_REFERENCE_PAGINATION_CYCLE")
            seen.add(url)
            payload = self._get(url, TICKERS_PATH)
            records = payload.get("results")
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise MassiveReferenceError("MASSIVE_REFERENCE_INVALID_RESPONSE")
            for record in records:
                if record.get("market") != "stocks" or record.get("locale") != "us":
                    raise MassiveReferenceError("MASSIVE_REFERENCE_UNEXPECTED_UNIVERSE")
                if record.get("active") is not True:
                    raise MassiveReferenceError("MASSIVE_REFERENCE_UNEXPECTED_UNIVERSE")
                yield record
            url = payload.get("next_url")
