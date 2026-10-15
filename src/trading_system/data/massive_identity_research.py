"""Bounded read-only probes; never imported by sync, selection or epoch ingestion.

The production reference allowlist stays unchanged. Research uses the same
credential/session setup, durable limiter, timeouts and bounded retry rules.
Results are observations, not verified security intervals.
"""

import hashlib
import re
from datetime import UTC, date, datetime

import requests

from trading_system.data.massive_reference import (
    _ORIGIN,
    _REQUEST_LIMITER,
    MassiveReferenceClient,
    _retry_after,
)

DETAIL_FIELDS = (
    "ticker",
    "cik",
    "share_class_figi",
    "composite_figi",
    "type",
    "active",
    "market",
    "locale",
    "primary_exchange",
    "list_date",
    "delisted_utc",
    "last_updated_utc",
)


class MassiveIdentityResearchClient(MassiveReferenceClient):
    """Single-identifier probes; these methods never paginate, persist or promote."""

    @property
    def diagnostics(self):
        return {
            "research_http_attempts": self._counts["massive_requests_total"],
            "research_retries": self._counts["massive_retries"],
            "configured_request_limit_per_minute": self._limit,
            "scope": "READ_ONLY_IDENTITY_RESEARCH;ENTITLEMENTS_NOT_ASSUMED",
        }

    def historical_ticker_details(self, ticker, as_of):
        if type(as_of) is not date:
            raise ValueError("RESEARCH_AS_OF_DATE_REQUIRED")
        return self._probe(ticker, "details", as_of)

    def ticker_events(self, identifier):
        return self._probe(identifier, "events")

    def _probe(self, identifier, operation, as_of=None):
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Z0-9][A-Z0-9.-]{0,31}", identifier
        ):
            raise ValueError("RESEARCH_EXACT_IDENTIFIER_REQUIRED")
        if operation == "details" and type(as_of) is date:
            path = f"/v3/reference/tickers/{identifier}"
            query = f"date={as_of}"
        elif operation == "events" and as_of is None:
            path = f"/vX/reference/tickers/{identifier}/events"
            query = "types=ticker_change"
        else:
            raise ValueError("RESEARCH_ENDPOINT_NOT_ALLOWED")
        observation = {
            "identifier": identifier,
            "operation": operation,
            "as_of": str(as_of) if as_of else None,
            "path": path,
            "query": query,
            "observed_at": datetime.now(UTC).isoformat(),
            "http_status": None,
        }
        not_before = 0
        for attempt in range(4):

            def send(current_attempt=attempt):
                self._counts["massive_requests_total"] += 1
                self._counts["massive_retries"] += bool(current_attempt)
                return self._session.get(
                    f"{_ORIGIN}{path}?{query}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=(10, 30),
                    allow_redirects=False,
                )

            try:
                response = _REQUEST_LIMITER.transmit(send, limit=self._limit, not_before=not_before)
            except requests.RequestException:
                if attempt == 3:
                    return {**observation, "error": "MASSIVE_REFERENCE_NETWORK_FAILED"}
                not_before = _REQUEST_LIMITER._clock() + 2 ** (attempt + 1)
                continue
            try:
                status = response.status_code
                observation["http_status"] = status
                observation["response_sha256"] = hashlib.sha256(response.content).hexdigest()
                if (status == 429 or 500 <= status <= 599) and attempt < 3:
                    delay = max(
                        2 ** (attempt + 1), _retry_after(response.headers.get("Retry-After"))
                    )
                    not_before = _REQUEST_LIMITER._clock() + delay
                    continue
                if status != 200:
                    return {
                        **observation,
                        "error": {
                            401: "MASSIVE_AUTH_FAILED",
                            402: "MASSIVE_PAYMENT_OR_PLAN_REQUIRED",
                            403: "MASSIVE_REFERENCE_ACCESS_DENIED",
                            404: "MASSIVE_REFERENCE_NOT_FOUND",
                            429: "MASSIVE_REFERENCE_RATE_LIMITED",
                        }.get(status, "MASSIVE_REFERENCE_HTTP_FAILED"),
                    }
                try:
                    payload = response.json()
                except (ValueError, requests.RequestException):
                    return {**observation, "error": "MASSIVE_REFERENCE_INVALID_RESPONSE"}
                if not isinstance(payload, dict) or payload.get("status") != "OK":
                    return {**observation, "error": "MASSIVE_REFERENCE_INVALID_RESPONSE"}
                result = payload.get("results")
                if not isinstance(result, dict):
                    return {**observation, "error": "MASSIVE_REFERENCE_INVALID_RESPONSE"}
                fields = DETAIL_FIELDS if operation == "details" else ("name", "events")
                return {
                    **observation,
                    "request_id": payload.get("request_id"),
                    "result_fields": sorted(result),
                    "results": {k: result[k] for k in fields if k in result},
                }
            finally:
                response.close()
        raise AssertionError("Unreachable bounded retry loop")
