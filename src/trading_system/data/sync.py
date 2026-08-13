"""Independent, observable synchronization stages for local screening data."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    full_history_request_window,
    latest_completed_trading_session,
)
from trading_system.data.sec_client import SecClient
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.data.xbrl_parser import VALID_FORMS, parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity

LOGGER = logging.getLogger(__name__)
RELEVANT_SEC_FORMS = VALID_FORMS


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _recent_accessions(submissions: Mapping[str, Any]) -> set[str]:
    recent = submissions.get("filings", {}).get("recent", {})
    if not isinstance(recent, Mapping):
        return set()
    accessions = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    if not isinstance(accessions, list) or not isinstance(forms, list):
        return set()
    return {
        str(accession)
        for accession, form in zip(accessions, forms, strict=False)
        if accession and form in RELEVANT_SEC_FORMS
    }


class DataSynchronizer:
    def __init__(
        self,
        database: Database,
        alpaca: AlpacaDataClient | None,
        sec: SecClient | None,
        *,
        market_data_days: int = 320,
        market_data_batch_size: int = 200,
        exclude_financials: bool = False,
        exclude_reits: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        if market_data_batch_size <= 0:
            raise ValueError("market_data_batch_size must be positive")
        self.database = database
        self.alpaca = alpaca
        self.sec = sec
        self.market_data_days = market_data_days
        self.market_data_batch_size = market_data_batch_size
        self.exclude_financials = exclude_financials
        self.exclude_reits = exclude_reits
        self.clock = clock
        self.timer = timer

    def sync(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        """Backwards-compatible complete synchronization."""

        return self.sync_full(requested_symbols)

    def sync_full(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        started = self.timer()
        assets = self.sync_assets()
        available = set(self.database.list_tradable_asset_symbols())
        symbols = sorted(available if not requested_symbols else available & set(requested_symbols))
        sec = self.sync_sec_full(symbols)
        bars = self.sync_historical_bars(symbols)
        result = {
            "mode": "full",
            "assets": assets["records_updated"],
            "symbols": len(symbols),
            "market_symbols": bars["symbols_checked"],
            "facts": sec["facts_processed"],
            "bars": bars["records_updated"],
            "errors": sec["errors"] + bars["errors"],
            "elapsed_seconds": round(self.timer() - started, 3),
            "stages": {"assets": assets, "sec": sec, "historical_bars": bars},
        }
        return result

    def sync_assets(self) -> dict[str, Any]:
        return self._run_stage("asset_universe", self._sync_assets)

    def _sync_assets(self) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for asset synchronization")
        assets = self.alpaca.list_tradable_us_equities()
        return {"records_updated": self.database.upsert_assets(assets), "errors": 0}

    def sync_sec_full(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage("sec", lambda: self._sync_sec(requested_symbols, full=True))

    def sync_sec_incremental(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage("sec", lambda: self._sync_sec(requested_symbols, full=False))

    def _sync_sec(self, requested_symbols: list[str] | None, *, full: bool) -> dict[str, Any]:
        if self.sec is None:
            raise ValueError("SEC client is required for SEC synchronization")
        available = set(self.database.list_tradable_asset_symbols())
        if not available:
            raise RuntimeError("Asset universe is empty; run sync-assets or sync --full first")
        symbols = sorted(available if not requested_symbols else available & set(requested_symbols))
        ticker_map = self.database.company_symbol_to_cik()
        missing_mappings = set(symbols) - ticker_map.keys()
        cached_ticker_map = self.database.sync_value("sec_reference", "ticker_to_cik")
        if full or not isinstance(cached_ticker_map, dict):
            remote_map = self.sec.ticker_to_cik()
            self.database.set_sync_value("sec_reference", "ticker_to_cik", remote_map)
        else:
            remote_map = {str(symbol): str(cik) for symbol, cik in cached_ticker_map.items()}
        ticker_map.update(
            remote_map
            if full
            else {
                symbol: remote_map[symbol]
                for symbol in missing_mappings
                if symbol in remote_map
            }
        )
        accession_states = self.database.sync_values("sec_accessions")
        counts = {
            "mode": "full" if full else "incremental",
            "companies_checked": 0,
            "companies_updated": 0,
            "facts_processed": 0,
            "missing_cik_mappings": 0,
            "errors": 0,
        }
        submissions_seconds = 0.0
        companyfacts_seconds = 0.0
        with self.database.connect() as write_connection:
            for symbol in symbols:
                cik = ticker_map.get(symbol)
                if cik is None:
                    counts["missing_cik_mappings"] += 1
                    continue
                counts["companies_checked"] += 1
                try:
                    outcome = self._sync_sec_company(
                        symbol,
                        cik,
                        full=full,
                        stored_accessions=accession_states.get(cik),
                        write_connection=write_connection,
                    )
                    write_connection.commit()
                    counts["facts_processed"] += outcome["facts_processed"]
                    counts["companies_updated"] += outcome["companies_updated"]
                    submissions_seconds += outcome["submissions_seconds"]
                    companyfacts_seconds += outcome["companyfacts_seconds"]
                    if outcome["accessions"] is not None:
                        accession_states[cik] = outcome["accessions"]
                except Exception:
                    write_connection.rollback()
                    counts["errors"] += 1
                    LOGGER.exception("Failed SEC update symbol=%s cik=%s", symbol, cik)
        counts["submissions_seconds"] = round(submissions_seconds, 3)
        counts["companyfacts_seconds"] = round(companyfacts_seconds, 3)
        return counts

    def _sync_sec_company(
        self,
        symbol: str,
        cik: str,
        *,
        full: bool,
        stored_accessions: Any,
        write_connection: Any,
    ) -> dict[str, Any]:
        if self.sec is None:  # Narrowed by _sync_sec; retained for direct testability.
            raise ValueError("SEC client is required for SEC synchronization")
        request_started = self.timer()
        submissions = self.sec.submissions(cik)
        submissions_seconds = self.timer() - request_started
        current_accessions = _recent_accessions(submissions)
        initializing_state = stored_accessions is None
        cached_submissions = (
            self.database.cached_sec_payload(cik, "submissions", max_age=None)
            if initializing_state
            else None
        )
        previous_accessions = (
            set(stored_accessions)
            if stored_accessions is not None
            else (
                _recent_accessions(cached_submissions or {})
                | self.database.known_accession_numbers(cik)
            )
        )
        needs_facts = (
            full
            or (initializing_state and not self.database.has_fundamental_facts(cik))
            or bool(current_accessions - previous_accessions)
        )
        new_state = sorted(previous_accessions | current_accessions)
        facts_processed = 0
        companyfacts_seconds = 0.0
        if needs_facts:
            company = CompanyIdentity(
                cik=cik,
                symbol=symbol,
                name=str(submissions.get("name") or symbol),
                sic=str(submissions["sic"]) if submissions.get("sic") else None,
                sic_description=submissions.get("sicDescription"),
            )
            request_started = self.timer()
            payload = self.sec.company_facts(cik)
            companyfacts_seconds = self.timer() - request_started
            facts = parse_company_facts(payload, symbol)
            facts_processed = self.database.upsert_sec_company_update(
                company,
                facts,
                new_state,
                connection=write_connection,
            )
        elif initializing_state:
            self.database.set_sync_value(
                "sec_accessions", cik, new_state, connection=write_connection
            )
        return {
            "facts_processed": facts_processed,
            "companies_updated": int(needs_facts),
            "submissions_seconds": submissions_seconds,
            "companyfacts_seconds": companyfacts_seconds,
            "accessions": new_state if needs_facts or initializing_state else None,
        }

    def sync_historical_bars(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage(
            "historical_bars", lambda: self._sync_historical_bars(requested_symbols)
        )

    def _sync_historical_bars(self, requested_symbols: list[str] | None) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for historical-bar synchronization")
        available = {company.symbol for company in self.database.list_tradable_companies()}
        symbols = sorted(available if not requested_symbols else available & set(requested_symbols))
        completed_session = latest_completed_trading_session()
        full_start, end = full_history_request_window(completed_session, self.market_data_days)
        latest = self.database.latest_bar_timestamps(symbols)
        starts: dict[datetime, list[str]] = defaultdict(list)
        for symbol in symbols:
            starts[latest[symbol] - timedelta(days=7) if symbol in latest else full_start].append(
                symbol
            )
        counts = {"symbols_checked": len(symbols), "records_updated": 0, "errors": 0}
        for start, grouped_symbols in starts.items():
            for batch in _chunks(grouped_symbols, self.market_data_batch_size):
                try:
                    bars = self.alpaca.daily_bars(batch, start, end)
                    counts["records_updated"] += self.database.upsert_bars(bars)
                except Exception:
                    counts["errors"] += 1
                    LOGGER.exception(
                        "Failed Alpaca bar batch symbols=%d first=%s last=%s",
                        len(batch),
                        batch[0],
                        batch[-1],
                    )
        return counts

    def refresh_market(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage(
            "market_snapshot", lambda: self._refresh_market(requested_symbols)
        )

    def _refresh_market(self, requested_symbols: list[str] | None) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for market refresh")
        symbols = self._screenable_symbols(requested_symbols)
        counts = {
            "symbols_requested": len(symbols),
            "symbols_updated": 0,
            "missing_symbols": 0,
            "errors": 0,
        }
        for batch in _chunks(symbols, self.market_data_batch_size):
            try:
                snapshots = self.alpaca.stock_snapshots(batch)
                counts["symbols_updated"] += self.database.upsert_market_snapshots(snapshots)
                counts["missing_symbols"] += len(set(batch) - {item.symbol for item in snapshots})
            except Exception:
                counts["errors"] += 1
                LOGGER.exception(
                    "Failed Alpaca snapshot batch symbols=%d first=%s last=%s",
                    len(batch),
                    batch[0],
                    batch[-1],
                )
        return counts

    def _screenable_symbols(self, requested_symbols: list[str] | None) -> list[str]:
        companies = self.database.list_tradable_companies()
        symbols = [
            company.symbol
            for company in companies
            if not (self.exclude_reits and is_reit(company.sic))
            and not (self.exclude_financials and is_financial_or_reit(company.sic))
        ]
        if requested_symbols:
            symbols = sorted(set(symbols) & {symbol.upper() for symbol in requested_symbols})
        return symbols

    def _run_stage(self, dataset: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started_at = self.clock()
        started = self.timer()
        previous = self.database.sync_value("dataset", dataset)
        previous = previous if isinstance(previous, dict) else {}
        self.database.set_sync_value(
            "dataset",
            dataset,
            {
                **previous,
                "last_started_at": started_at.isoformat(),
                "status": "running",
            },
        )
        try:
            result = operation()
        except Exception as exc:
            elapsed = round(self.timer() - started, 3)
            self.database.set_sync_value(
                "dataset",
                dataset,
                {
                    **previous,
                    "last_started_at": started_at.isoformat(),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": elapsed,
                },
            )
            raise
        elapsed = round(self.timer() - started, 3)
        result = {**result, "elapsed_seconds": elapsed}
        status = "partial" if result.get("errors", 0) else "success"
        state = {
            **previous,
            "last_started_at": started_at.isoformat(),
            "last_success_at": (
                self.clock().isoformat() if status == "success" else previous.get("last_success_at")
            ),
            "status": status,
            **result,
        }
        self.database.set_sync_value("dataset", dataset, state)
        LOGGER.info("Stage %s completed status=%s elapsed=%.3fs", dataset, status, elapsed)
        return result
