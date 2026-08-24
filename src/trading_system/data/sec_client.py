"""Rate-limited SEC EDGAR client with retry and local-response hooks."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class SecResourceNotFound(Exception):
    """An expected absence of one specific SEC resource, not an infrastructure failure."""

    def __init__(self, resource_type: str, cik: str | None, url: str) -> None:
        self.resource_type = resource_type
        self.cik = cik
        self.url = url
        super().__init__(
            f"SEC {resource_type} resource not found"
            + (f" for CIK {cik}" if cik is not None else "")
        )


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
        self._request_counts: Counter[str] = Counter()

    @property
    def request_counts(self) -> dict[str, int]:
        """Return actual HTTP attempts, including retries, grouped by resource."""

        return dict(self._request_counts)

    def _get(self, url: str, *, resource_type: str, cik: str | None = None) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            wait = self.interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self.sleeper(wait)
            try:
                self._request_counts[resource_type] += 1
                response = self.session.get(url, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                if response.status_code == 404:
                    raise SecResourceNotFound(resource_type, cik, url)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(
                        f"retryable SEC status {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response
            except SecResourceNotFound:
                raise
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise
                delay = min(2**attempt, 16)
                LOGGER.warning(
                    "SEC request retry resource=%s url=%s delay=%s error=%s",
                    resource_type,
                    url,
                    delay,
                    exc,
                )
                self.sleeper(delay)
        raise RuntimeError("unreachable")

    def _get_json(self, url: str, *, resource_type: str, cik: str | None = None) -> dict[str, Any]:
        response = self._get(url, resource_type=resource_type, cik=cik)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SEC response was not a JSON object")
        return payload

    def _get_text(self, url: str, *, resource_type: str) -> str:
        return self._get(url, resource_type=resource_type).text

    def company_facts(self, cik: str | int) -> dict[str, Any]:
        normalized = f"{int(cik):010d}"
        return self._get_json(
            f"{self.DATA_BASE}/api/xbrl/companyfacts/CIK{normalized}.json",
            resource_type="companyfacts",
            cik=normalized,
        )

    def submissions(self, cik: str | int) -> dict[str, Any]:
        normalized = f"{int(cik):010d}"
        return self._get_json(
            f"{self.DATA_BASE}/submissions/CIK{normalized}.json",
            resource_type="submissions",
            cik=normalized,
        )

    def company_tickers(self) -> dict[str, Any]:
        return self._get_json(
            f"{self.ARCHIVES_BASE}/files/company_tickers.json",
            resource_type="ticker_map",
        )

    def filing_index(self, year: int, quarter: int, *, current: bool) -> str:
        if quarter not in {1, 2, 3, 4}:
            raise ValueError("quarter must be within 1..4")
        path = (
            "edgar/full-index/xbrl.idx"
            if current
            else f"edgar/full-index/{year}/QTR{quarter}/xbrl.idx"
        )
        return self._get_text(f"{self.ARCHIVES_BASE}/Archives/{path}", resource_type="filing_index")

    def daily_master_index_directory(self, year: int, quarter: int) -> dict[str, Any]:
        """Return SEC metadata for one daily-index quarter through the shared transport."""

        if year < 1994:
            raise ValueError("daily master indexes are unavailable before 1994")
        if quarter not in {1, 2, 3, 4}:
            raise ValueError("quarter must be within 1..4")
        return self._get_json(
            f"{self.ARCHIVES_BASE}/Archives/edgar/daily-index/{year}/QTR{quarter}/index.json",
            resource_type="daily_index_directory",
        )

    def daily_master_index(self, year: int, quarter: int, filing_date: str) -> str:
        """Fetch one explicitly discovered SEC daily master index."""

        if year < 1994:
            raise ValueError("daily master indexes are unavailable before 1994")
        if quarter not in {1, 2, 3, 4}:
            raise ValueError("quarter must be within 1..4")
        if len(filing_date) != 8 or not filing_date.isdigit():
            raise ValueError("filing_date must use YYYYMMDD")
        try:
            parsed_date = datetime.strptime(filing_date, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError("filing_date must be a valid calendar date") from exc
        if parsed_date.year != year or (parsed_date.month - 1) // 3 + 1 != quarter:
            raise ValueError("filing_date must belong to the requested year and quarter")
        return self._get_text(
            f"{self.ARCHIVES_BASE}/Archives/edgar/daily-index/"
            f"{year}/QTR{quarter}/master.{filing_date}.idx",
            resource_type="daily_master_index",
        )

    def ticker_to_cik(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.company_tickers().values():
            if isinstance(item, dict) and item.get("ticker") and item.get("cik_str") is not None:
                result[str(item["ticker"]).upper()] = f"{int(item['cik_str']):010d}"
        return result
