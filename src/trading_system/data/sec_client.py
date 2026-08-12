"""Rate-limited SEC EDGAR client with retry and local-response hooks."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class SecClient:
    DATA_BASE = "https://data.sec.gov"
    ARCHIVES_BASE = "https://www.sec.gov"

    def __init__(
        self,
        user_agent: str,
        *,
        request_interval_seconds: float = 0.11,
        timeout_seconds: float = 30,
        max_retries: int = 4,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError("SEC user agent must include a contact email")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.interval = request_interval_seconds
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.sleeper = sleeper
        self._last_request_at = 0.0

    def _get_json(self, url: str) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            wait = self.interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self.sleeper(wait)
            try:
                response = self.session.get(url, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(
                        f"retryable SEC status {response.status_code}", response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("SEC response was not a JSON object")
                return payload
            except (requests.RequestException, ValueError) as exc:
                if (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code == 404
                ):
                    LOGGER.warning("SEC resource not found url=%s; not retrying", url)
                    raise
                if attempt >= self.max_retries:
                    LOGGER.error("SEC request failed url=%s attempts=%d", url, attempt + 1)
                    raise
                delay = min(2**attempt, 16)
                LOGGER.warning("SEC request retry url=%s delay=%s error=%s", url, delay, exc)
                self.sleeper(delay)
        raise RuntimeError("unreachable")

    def company_facts(self, cik: str | int) -> dict[str, Any]:
        return self._get_json(f"{self.DATA_BASE}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

    def submissions(self, cik: str | int) -> dict[str, Any]:
        return self._get_json(f"{self.DATA_BASE}/submissions/CIK{int(cik):010d}.json")

    def company_tickers(self) -> dict[str, Any]:
        return self._get_json(f"{self.ARCHIVES_BASE}/files/company_tickers.json")

    def ticker_to_cik(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.company_tickers().values():
            if isinstance(item, dict) and item.get("ticker") and item.get("cik_str") is not None:
                result[str(item["ticker"]).upper()] = f"{int(item['cik_str']):010d}"
        return result
