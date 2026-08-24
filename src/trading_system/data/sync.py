"""Independent, observable synchronization stages for local screening data."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import requests

from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.daily_history import boundary_integrity_check
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    full_history_request_window,
    is_regular_session_timestamp,
    latest_completed_trading_session,
    trading_sessions_between,
)
from trading_system.data.sec_client import SecClient, SecResourceNotFound
from trading_system.data.sec_identity import (
    SecIdentityResolution,
    identity_conflict_is_resolved,
    resolve_sec_identities,
)
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.data.xbrl_parser import VALID_FORMS, parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, TradableAsset

LOGGER = logging.getLogger(__name__)
# The change detector must match the forms that parse_company_facts can persist.
# XBRL 8-K/6-K filings are intentionally ignored until the parser supports them;
# treating them as candidates would trigger thousands of no-op Company Facts reads.
RELEVANT_SEC_FORMS = VALID_FORMS
UNMAPPED_CATEGORIES = (
    "etf_or_fund",
    "warrant",
    "unit",
    "rights",
    "preferred",
    "depositary_or_foreign",
    "unclassified",
)
SEC_IDENTITY_CONFLICT_SOURCE = "sec_identity_conflicts"
SEC_CHANGE_DETECTION_SOURCE = "sec_change_detection"
DAILY_MASTER_INDEX_CURSOR_KEY = "daily_master_index"
LEGACY_XBRL_INDEX_CURSOR_KEY = "xbrl_index"
# Re-read a small calendar window so late/corrected SEC directory publication is observable.
# Accession state makes this deliberately repeated work idempotent.
DAILY_INDEX_OVERLAP_DAYS = 7
DAILY_MASTER_INDEX_NAME = re.compile(r"^master\.(\d{8})\.idx$")


@dataclass(frozen=True)
class FilingIndexSnapshot:
    last_data_received: date
    accessions_by_cik: dict[str, set[str]]


@dataclass(frozen=True, order=True)
class DailyMasterIndexResource:
    index_date: date
    year: int
    quarter: int
    filename: str


@dataclass(frozen=True)
class DailyMasterIndexEntry:
    cik: str
    company_name: str
    form: str
    filed: date
    filename: str
    accession: str


@dataclass(frozen=True)
class DailyMasterIndexSnapshot:
    index_date: date
    entries: tuple[DailyMasterIndexEntry, ...]
    accessions_by_cik: dict[str, set[str]]


@dataclass(frozen=True)
class DailyMasterIndexDiscovery:
    cursor_before: date | None
    legacy_cursor_bootstrap: bool
    directories_checked: int
    indexes_discovered: int
    indexes_new: int
    snapshots: tuple[DailyMasterIndexSnapshot, ...]


class SecCompanySyncError(Exception):
    def __init__(self, phase: str, resource: str, elapsed: float, cause: Exception) -> None:
        self.phase = phase
        self.resource = resource
        self.elapsed = elapsed
        self.cause = cause
        super().__init__(f"{phase} failure for {resource}: {cause}")


def parse_filing_index(payload: str) -> FilingIndexSnapshot:
    """Parse an official EDGAR XBRL index and reject incomplete/malformed input."""

    header = re.search(r"^Last Data Received:\s+(.+?)\s*$", payload, flags=re.MULTILINE)
    if header is None or "CIK|Company Name|Form Type|Date Filed|Filename" not in payload:
        raise ValueError("SEC XBRL index is missing required headers")
    try:
        last_data_received = datetime.strptime(header.group(1), "%B %d, %Y").date()
    except ValueError as exc:
        raise ValueError("SEC XBRL index has an invalid Last Data Received date") from exc
    accessions_by_cik: dict[str, set[str]] = defaultdict(set)
    for line in payload.splitlines():
        fields = line.split("|")
        if len(fields) != 5 or not fields[0].isdigit():
            continue
        cik, _name, form, _filed, filename = fields
        if form not in RELEVANT_SEC_FORMS:
            continue
        accession = PurePosixPath(filename).stem
        if accession:
            accessions_by_cik[cik.zfill(10)].add(accession)
    return FilingIndexSnapshot(last_data_received, dict(accessions_by_cik))


def parse_daily_index_directory(
    payload: Mapping[str, Any], year: int, quarter: int
) -> tuple[DailyMasterIndexResource, ...]:
    """Parse an SEC daily-index directory, accepting only exact daily master names."""

    if quarter not in {1, 2, 3, 4}:
        raise ValueError("quarter must be within 1..4")
    directory = payload.get("directory")
    if not isinstance(directory, Mapping):
        raise ValueError("SEC daily-index directory JSON is missing directory metadata")
    items = directory.get("item")
    if not isinstance(items, list):
        raise ValueError("SEC daily-index directory JSON is missing its item list")

    by_date: dict[date, DailyMasterIndexResource] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("SEC daily-index directory contains a malformed item")
        name = item.get("name")
        if not isinstance(name, str):
            raise ValueError("SEC daily-index directory item is missing a filename")
        if item.get("type") not in {None, "file"}:
            continue
        match = DAILY_MASTER_INDEX_NAME.fullmatch(name)
        if match is None:
            continue
        try:
            index_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        expected_quarter = (index_date.month - 1) // 3 + 1
        if index_date.year != year or expected_quarter != quarter:
            continue
        resource = DailyMasterIndexResource(index_date, year, quarter, name)
        previous = by_date.get(index_date)
        if previous is None or resource.filename < previous.filename:
            by_date[index_date] = resource
    return tuple(by_date[item] for item in sorted(by_date))


def parse_daily_master_index(payload: str, index_date: date) -> DailyMasterIndexSnapshot:
    """Parse relevant rows from one daily master index, failing closed after its header."""

    lines = payload.splitlines()
    expected_header = ("cik", "companyname", "formtype", "datefiled", "filename")

    def normalized_header(line: str) -> tuple[str, ...]:
        return tuple(
            re.sub(r"\s+", "", field).casefold()
            for field in line.lstrip("\ufeff").split("|")
        )

    header_index = next(
        (index for index, line in enumerate(lines) if normalized_header(line) == expected_header),
        None,
    )
    if header_index is None:
        payload_start = payload.lstrip("\ufeff\r\n\t ")[:256].casefold()
        if payload_start.startswith(("<!doctype html", "<html")):
            raise ValueError(
                f"SEC daily master index {index_date.isoformat()} returned HTML "
                "instead of index data"
            )
        raise ValueError(
            f"SEC daily master index {index_date.isoformat()} is missing its delimited header"
        )

    entries: list[DailyMasterIndexEntry] = []
    accessions_by_cik: dict[str, set[str]] = defaultdict(set)
    filing_path = re.compile(r"^edgar/data/(\d+)/([A-Za-z0-9][A-Za-z0-9-]*)\.txt$")
    for line_number, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
        stripped = line.strip()
        if not stripped or set(stripped) == {"-"}:
            continue
        fields = line.split("|")
        if len(fields) != 5:
            raise ValueError(f"Malformed SEC daily master row at line {line_number}")
        raw_cik, company_name, form, raw_filed, filename = (field.strip() for field in fields)
        if not raw_cik.isdigit() or len(raw_cik) > 10 or not company_name or not form:
            raise ValueError(f"Malformed SEC daily master row at line {line_number}")
        try:
            filed = date.fromisoformat(raw_filed)
        except ValueError as exc:
            raise ValueError(f"Malformed SEC daily master row at line {line_number}") from exc
        if form not in RELEVANT_SEC_FORMS:
            continue
        path_match = filing_path.fullmatch(filename)
        if path_match is None or int(path_match.group(1)) != int(raw_cik):
            raise ValueError(f"Malformed SEC daily master row at line {line_number}")
        cik = raw_cik.zfill(10)
        accession = path_match.group(2)
        entry = DailyMasterIndexEntry(cik, company_name, form, filed, filename, accession)
        entries.append(entry)
        accessions_by_cik[cik].add(accession)
    return DailyMasterIndexSnapshot(index_date, tuple(entries), dict(accessions_by_cik))


def classify_unmapped_asset(asset: TradableAsset) -> str:
    """Classify only when local Alpaca symbol/name evidence is explicit."""

    symbol = asset.symbol.upper()
    name = asset.name.upper()
    if re.search(r"\b(ETF|ETN|FUND|PORTFOLIO)\b", name) or any(
        marker in name
        for marker in (
            "ISHARES",
            "PROSHARES",
            "DIREXION",
            "SPDR ",
            "WISDOMTREE",
            "VANECK",
            "GLOBAL X ",
            "FIRST TRUST ",
        )
    ):
        return "etf_or_fund"
    if symbol.endswith(".WS") or re.search(r"\bWARRANTS?\b", name):
        return "warrant"
    if symbol.endswith(".U") or re.search(r"\bUNITS?\b", name):
        return "unit"
    if symbol.endswith(".RT") or re.search(r"\bRIGHTS?\b", name):
        return "rights"
    if ".PR" in symbol or re.search(r"\b(PREFERRED|PREFERENCE|PFD)\b", name):
        return "preferred"
    if re.search(r"\b(ADR|ADS|DEPOSITARY|DEPOSITORY|COMMON SHARES)\b", name):
        return "depositary_or_foreign"
    return "unclassified"


def _quarters_between(start: date, end: date) -> list[tuple[int, int]]:
    year, quarter = start.year, (start.month - 1) // 3 + 1
    end_key = (end.year, (end.month - 1) // 3 + 1)
    output = []
    while (year, quarter) <= end_key:
        output.append((year, quarter))
        if quarter == 4:
            year, quarter = year + 1, 1
        else:
            quarter += 1
    return output


def _classify_sync_failure(counts: dict[str, Any], error: SecCompanySyncError) -> None:
    if error.phase == "parse":
        counts["parse_failures"] += 1
        return
    if error.phase == "database":
        counts["database_failures"] += 1
        return
    if error.phase != "request":
        counts["other_failures"] += 1
        return
    counts["request_failures"] += 1
    cause = error.cause
    if isinstance(cause, requests.Timeout):
        counts["timeout_failures"] += 1
    elif isinstance(cause, requests.ConnectionError):
        counts["connection_failures"] += 1
    elif isinstance(cause, requests.HTTPError) and cause.response is not None:
        status = cause.response.status_code
        if status == 429:
            counts["rate_limit_failures"] += 1
        elif status >= 500:
            counts["server_failures"] += 1
        else:
            counts["other_failures"] += 1
    elif isinstance(cause, (requests.JSONDecodeError, ValueError)):
        counts["json_failures"] += 1
    else:
        counts["other_failures"] += 1


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _time_windows(
    start: datetime, end: datetime, window_days: int
) -> Iterable[tuple[datetime, datetime]]:
    current = start
    step = timedelta(days=window_days)
    while current < end:
        following = min(current + step, end)
        yield current, following
        current = following


def _incremental_bar_ranges(
    database: Database,
    symbol: str,
    timeframe: BarTimeframe,
    start: datetime,
    end: datetime,
    overlap_bars: int,
) -> list[tuple[datetime, datetime]]:
    earliest, latest = database.bar_bounds(
        symbol, timeframe, start=start, end=end
    )
    return _bar_edge_ranges(earliest, latest, timeframe, start, end, overlap_bars)


def _bar_edge_ranges(
    earliest: datetime | None,
    latest: datetime | None,
    timeframe: BarTimeframe,
    start: datetime,
    end: datetime,
    overlap_bars: int,
    *,
    previously_verified: bool = False,
) -> list[tuple[datetime, datetime]]:
    """Plan missing leading/trailing coverage while retaining a correction overlap."""

    overlap = timeframe.duration * overlap_bars
    if previously_verified:
        if earliest is None or latest is None or not overlap_bars:
            return []
        correction_start = max(start, end - overlap)
        return [(correction_start, end)] if correction_start < end else []
    if earliest is None or latest is None:
        return [(start, end)]
    ranges: list[tuple[datetime, datetime]] = []
    if earliest > start and not previously_verified:
        ranges.append((start, min(end, earliest + overlap)))
    following = latest + timeframe.duration
    if following < end:
        tail_start = max(start, following - overlap)
        ranges.append((tail_start, end))
    elif overlap_bars:
        tail_start = max(start, following - overlap)
        if tail_start < end:
            ranges.append((tail_start, end))
    if not ranges:
        return []
    ranges.sort()
    merged = [ranges[0]]
    for range_start, range_end in ranges[1:]:
        previous_start, previous_end = merged[-1]
        if range_start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, range_end))
        else:
            merged.append((range_start, range_end))
    return merged


def _enum_text(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _daily_range_verified(
    value: Any,
    start: datetime,
    end: datetime,
    feed: str | None,
    adjustment: str | None,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("feed") != feed or value.get("adjustment") != adjustment:
        return False
    try:
        checked_start = datetime.fromisoformat(str(value["start"]))
        checked_end = datetime.fromisoformat(str(value["end_exclusive"]))
    except (KeyError, TypeError, ValueError):
        return False
    return checked_start <= start and checked_end >= end


def _daily_coverage_value(
    previous: Any,
    start: datetime,
    end: datetime,
    feed: str | None,
    adjustment: str | None,
    checked_at: str,
) -> dict[str, str | None]:
    if isinstance(previous, Mapping) and (
        previous.get("feed") == feed and previous.get("adjustment") == adjustment
    ):
        try:
            previous_start = datetime.fromisoformat(str(previous["start"]))
            previous_end = datetime.fromisoformat(str(previous["end_exclusive"]))
            if previous_start <= end and start <= previous_end:
                start = min(start, previous_start)
                end = max(end, previous_end)
        except (KeyError, TypeError, ValueError):
            pass
    return {
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "feed": feed,
        "adjustment": adjustment,
        "checked_at": checked_at,
    }


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
        companyfacts_unavailable_ttl: timedelta = timedelta(days=7),
        intraday_enabled: bool = False,
        intraday_timeframes: Iterable[BarTimeframe | str] = (BarTimeframe.MINUTES_15,),
        intraday_extended_hours: bool = False,
        intraday_incremental: bool = True,
        intraday_overlap_bars: int = 2,
        intraday_symbol_batch_size: int = 25,
        intraday_request_window_days: int = 7,
        daily_overlap_bars: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        if market_data_batch_size <= 0:
            raise ValueError("market_data_batch_size must be positive")
        if companyfacts_unavailable_ttl <= timedelta(0):
            raise ValueError("companyfacts_unavailable_ttl must be positive")
        if intraday_overlap_bars < 0:
            raise ValueError("intraday_overlap_bars cannot be negative")
        if daily_overlap_bars < 0:
            raise ValueError("daily_overlap_bars cannot be negative")
        self.database = database
        self.alpaca = alpaca
        self.sec = sec
        self.market_data_days = market_data_days
        self.market_data_batch_size = market_data_batch_size
        self.exclude_financials = exclude_financials
        self.exclude_reits = exclude_reits
        self.companyfacts_unavailable_ttl = companyfacts_unavailable_ttl
        self.intraday_enabled = intraday_enabled
        self.intraday_timeframes = tuple(BarTimeframe(item) for item in intraday_timeframes)
        self.intraday_extended_hours = intraday_extended_hours
        self.intraday_incremental = intraday_incremental
        self.intraday_overlap_bars = intraday_overlap_bars
        self.intraday_symbol_batch_size = intraday_symbol_batch_size
        self.intraday_request_window_days = intraday_request_window_days
        self.daily_overlap_bars = daily_overlap_bars
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
        intraday = None
        if self.intraday_enabled:
            completed = latest_completed_trading_session(self.clock())
            intraday_start, intraday_end = full_history_request_window(
                completed, self.market_data_days
            )
            intraday = self.sync_intraday(
                symbols,
                self.intraday_timeframes,
                intraday_start,
                intraday_end,
            )
        result = {
            "mode": "full",
            "assets": assets["records_updated"],
            "symbols": len(symbols),
            "market_symbols": bars["symbols_checked"],
            "facts": sec["facts_processed"],
            "bars": bars["records_updated"],
            "errors": sec["errors"] + bars["errors"] + (intraday or {}).get("errors", 0),
            "elapsed_seconds": round(self.timer() - started, 3),
            "stages": {
                "assets": assets,
                "sec": sec,
                "historical_bars": bars,
                **({"intraday_bars": intraday} if intraday is not None else {}),
            },
        }
        return result

    def sync_assets(self) -> dict[str, Any]:
        return self._run_stage("asset_universe", self._sync_assets)

    def _sync_assets(self) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for asset synchronization")
        assets = self.alpaca.list_tradable_us_equities()
        return {**self.database.reconcile_assets(assets), "errors": 0}

    def sync_sec_full(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage("sec", lambda: self._sync_sec(requested_symbols, full=True))

    def sync_sec_incremental(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage("sec", lambda: self._sync_sec(requested_symbols, full=False))

    def _sync_sec(self, requested_symbols: list[str] | None, *, full: bool) -> dict[str, Any]:
        if self.sec is None:
            raise ValueError("SEC client is required for SEC synchronization")
        assets = self.database.list_tradable_assets()
        if not assets:
            raise RuntimeError("Asset universe is empty; run sync-assets or sync --full first")
        requested = {symbol.upper() for symbol in requested_symbols or []}
        selected_assets = [asset for asset in assets if not requested or asset.symbol in requested]
        symbols = {asset.symbol for asset in selected_assets}
        request_counts_before = self._sec_request_counts()
        remote_map = {
            str(symbol).upper(): str(cik).zfill(10)
            for symbol, cik in self.sec.ticker_to_cik().items()
        }
        self.database.set_sync_value("sec_reference", "ticker_to_cik", remote_map)
        local_map = self.database.company_symbol_to_cik()
        identity = resolve_sec_identities(symbols, local_map, remote_map)
        self._persist_identity_conflicts(identity, symbols, local_map, remote_map)
        ticker_map = identity.ticker_map
        alias_mappings = identity.alias_mappings
        unmapped = [asset for asset in selected_assets if asset.symbol not in ticker_map]
        classification = defaultdict(int)
        for asset in unmapped:
            classification[classify_unmapped_asset(asset)] += 1
        canonical_symbols = identity.canonical_symbols
        accession_states = self.database.sync_values("sec_accessions")
        companyfacts_statuses = self.database.sync_values("sec_companyfacts_status")
        submissions_statuses = self.database.sync_values("sec_submissions_status")
        change_accessions: dict[str, set[str]] = defaultdict(set)
        change_detection_seconds = 0.0
        discovery: DailyMasterIndexDiscovery | None = None
        cursor_after: date | None = None
        negative_cache_hits = 0
        if full:
            candidate_ciks = set(canonical_symbols)
        else:
            detection_started = self.timer()
            discovery = self._daily_master_index_snapshots()
            change_detection_seconds = self.timer() - detection_started
            if discovery.snapshots:
                cursor_after = max(
                    discovery.cursor_before or discovery.snapshots[0].index_date,
                    discovery.snapshots[-1].index_date,
                )
            elif not discovery.legacy_cursor_bootstrap:
                cursor_after = discovery.cursor_before
            for snapshot in discovery.snapshots:
                for cik, accessions in snapshot.accessions_by_cik.items():
                    if cik in canonical_symbols:
                        change_accessions[cik].update(accessions)
            candidate_ciks = {
                cik
                for cik in canonical_symbols
                if change_accessions.get(cik, set()) - set(accession_states.get(cik, []))
                or self._negative_cache_expired(companyfacts_statuses.get(cik))
                or self._negative_cache_expired(submissions_statuses.get(cik))
            }
            suppressed = {
                cik
                for cik in canonical_symbols
                if not (change_accessions.get(cik, set()) - set(accession_states.get(cik, [])))
                and (
                    self._negative_cache_fresh(companyfacts_statuses.get(cik))
                    or self._negative_cache_fresh(submissions_statuses.get(cik))
                )
            }
            candidate_ciks -= suppressed
            negative_cache_hits = len(suppressed)
        counts = {
            "mode": "full" if full else "incremental",
            "universe_symbols": len(selected_assets),
            "sec_mapped_symbols": len(selected_assets) - len(unmapped),
            "sec_mapped_ciks": len(canonical_symbols),
            "sec_ticker_alias_symbols": len(alias_mappings),
            "sec_unmapped_symbols": len(unmapped),
            "missing_cik_mappings": len(unmapped),
            "unmapped_otc_exchange": sum(asset.exchange == "OTC" for asset in unmapped),
            **{
                f"unmapped_{category}": classification[category] for category in UNMAPPED_CATEGORIES
            },
            "change_candidates": len(candidate_ciks),
            "companies_checked": 0,
            "companies_updated": 0,
            "facts_processed": 0,
            "companyfacts_unavailable": 0,
            "submissions_unavailable": 0,
            "identity_conflicts": len(identity.conflicts),
            "identity_conflict_sample": sorted({item.symbol for item in identity.conflicts})[:10],
            "negative_cache_hits": negative_cache_hits,
            "change_detection_source": None if full else "sec_daily_master_index",
            "cursor_before": (
                discovery.cursor_before.isoformat()
                if discovery and discovery.cursor_before
                else None
            ),
            "cursor_after": cursor_after.isoformat() if cursor_after else None,
            "legacy_cursor_bootstrap": bool(discovery and discovery.legacy_cursor_bootstrap),
            "daily_index_overlap_days": DAILY_INDEX_OVERLAP_DAYS if discovery else 0,
            "daily_index_directories_checked": discovery.directories_checked if discovery else 0,
            "daily_indexes_discovered": discovery.indexes_discovered if discovery else 0,
            "daily_indexes_scanned": len(discovery.snapshots) if discovery else 0,
            "daily_indexes_new": discovery.indexes_new if discovery else 0,
            "daily_index_first_scanned": (
                discovery.snapshots[0].index_date.isoformat()
                if discovery and discovery.snapshots
                else None
            ),
            "daily_index_last_scanned": (
                discovery.snapshots[-1].index_date.isoformat()
                if discovery and discovery.snapshots
                else None
            ),
            "relevant_filings_detected": (
                sum(len(snapshot.entries) for snapshot in discovery.snapshots) if discovery else 0
            ),
            "request_failures": 0,
            "rate_limit_failures": 0,
            "server_failures": 0,
            "timeout_failures": 0,
            "connection_failures": 0,
            "json_failures": 0,
            "parse_failures": 0,
            "database_failures": 0,
            "other_failures": 0,
            "errors": 0,
        }
        submissions_seconds = 0.0
        companyfacts_seconds = 0.0
        parse_and_persist_seconds = 0.0
        logical_requests = {
            "ticker_map": 1,
            "daily_index_directory": discovery.directories_checked if discovery else 0,
            "daily_master_index": len(discovery.snapshots) if discovery else 0,
            "submissions": 0,
            "companyfacts": 0,
        }
        for conflict in identity.conflicts:
            LOGGER.warning(
                "SEC identity conflict symbol=%s proposed_cik=%s existing_cik=%s "
                "existing_symbol=%s source=%s; update skipped",
                conflict.symbol,
                conflict.proposed_cik,
                conflict.existing_cik,
                conflict.existing_symbol,
                conflict.source,
            )
        with self.database.connect() as write_connection:
            for cik in sorted(candidate_ciks):
                symbol = canonical_symbols[cik]
                detected = change_accessions.get(cik, set())
                unavailable_status = companyfacts_statuses.get(cik)
                submissions_status = submissions_statuses.get(cik)
                counts["companies_checked"] += 1
                logical_requests["submissions"] += 1
                try:
                    outcome = self._sync_sec_company(
                        symbol,
                        cik,
                        full=full,
                        stored_accessions=accession_states.get(cik),
                        detected_accessions=detected,
                        retry_unavailable=(
                            self._negative_cache_expired(unavailable_status)
                            or self._negative_cache_expired(submissions_status)
                        ),
                        write_connection=write_connection,
                    )
                    write_connection.commit()
                    counts["facts_processed"] += outcome["facts_processed"]
                    counts["companies_updated"] += outcome["companies_updated"]
                    counts["companyfacts_unavailable"] += outcome["companyfacts_unavailable"]
                    counts["submissions_unavailable"] += outcome["submissions_unavailable"]
                    submissions_seconds += outcome["submissions_seconds"]
                    companyfacts_seconds += outcome["companyfacts_seconds"]
                    parse_and_persist_seconds += outcome["parse_and_persist_seconds"]
                    logical_requests["companyfacts"] += outcome["companyfacts_requests"]
                    if outcome["accessions"] is not None:
                        accession_states[cik] = outcome["accessions"]
                except SecCompanySyncError as exc:
                    write_connection.rollback()
                    if exc.phase in {"parse", "database"}:
                        parse_and_persist_seconds += exc.elapsed
                    elif exc.resource == "submissions":
                        submissions_seconds += exc.elapsed
                    elif exc.resource == "companyfacts":
                        companyfacts_seconds += exc.elapsed
                        logical_requests["companyfacts"] += 1
                    _classify_sync_failure(counts, exc)
                    counts["errors"] += 1
                    LOGGER.exception(
                        "SEC update failed symbol=%s cik=%s phase=%s resource=%s",
                        symbol,
                        cik,
                        exc.phase,
                        exc.resource,
                        exc_info=exc.cause,
                    )
            if not full and counts["errors"] == 0 and cursor_after is not None:
                self.database.set_sync_value(
                    SEC_CHANGE_DETECTION_SOURCE,
                    DAILY_MASTER_INDEX_CURSOR_KEY,
                    {"last_processed_date": cursor_after.isoformat()},
                    connection=write_connection,
                )
                write_connection.commit()
            elif not full and counts["errors"]:
                counts["cursor_after"] = counts["cursor_before"]
        counts["submissions_seconds"] = round(submissions_seconds, 3)
        counts["companyfacts_seconds"] = round(companyfacts_seconds, 3)
        counts["change_detection_seconds"] = round(change_detection_seconds, 3)
        counts["parse_and_persist_seconds"] = round(parse_and_persist_seconds, 3)
        request_counts = self._request_count_delta(request_counts_before, logical_requests)
        counts.update(
            {
                "sec_requests_total": sum(request_counts.values()),
                "ticker_map_requests": request_counts.get("ticker_map", 0),
                "daily_index_directory_requests": request_counts.get(
                    "daily_index_directory", 0
                ),
                "daily_master_index_requests": request_counts.get("daily_master_index", 0),
                "change_detection_requests": (
                    request_counts.get("daily_index_directory", 0)
                    + request_counts.get("daily_master_index", 0)
                ),
                "submissions_requests": request_counts.get("submissions", 0),
                "companyfacts_requests": request_counts.get("companyfacts", 0),
            }
        )
        return counts

    def _persist_identity_conflicts(
        self,
        identity: SecIdentityResolution,
        selected_symbols: set[str],
        persisted: Mapping[str, str],
        current_sec: Mapping[str, str],
    ) -> None:
        previous = self.database.sync_values(SEC_IDENTITY_CONFLICT_SOURCE)
        active = {conflict.symbol: conflict for conflict in identity.conflicts}
        observed_at = self.clock().isoformat()
        with self.database.connect() as connection:
            for symbol, conflict in active.items():
                old = previous.get(symbol)
                detected_at = (
                    old.get("detected_at") or observed_at
                    if isinstance(old, dict) and old.get("status") == "unresolved"
                    else observed_at
                )
                self.database.set_sync_value(
                    SEC_IDENTITY_CONFLICT_SOURCE,
                    symbol,
                    {
                        "symbol": symbol,
                        "existing_cik": conflict.existing_cik,
                        "existing_symbol": conflict.existing_symbol,
                        "proposed_cik": conflict.proposed_cik,
                        "source": conflict.source,
                        "status": "unresolved",
                        "detected_at": detected_at,
                        "last_seen_at": observed_at,
                    },
                    connection=connection,
                )
            for symbol in (selected_symbols & previous.keys()) - active.keys():
                if identity_conflict_is_resolved(symbol, persisted, current_sec):
                    self.database.delete_sync_value(
                        SEC_IDENTITY_CONFLICT_SOURCE,
                        symbol,
                        connection=connection,
                    )

    def _sync_sec_company(
        self,
        symbol: str,
        cik: str,
        *,
        full: bool,
        stored_accessions: Any,
        detected_accessions: set[str],
        retry_unavailable: bool,
        write_connection: Any,
    ) -> dict[str, Any]:
        if self.sec is None:  # Narrowed by _sync_sec; retained for direct testability.
            raise ValueError("SEC client is required for SEC synchronization")
        request_started = self.timer()
        try:
            submissions = self.sec.submissions(cik)
        except SecResourceNotFound:
            elapsed = self.timer() - request_started
            accessions = sorted(set(stored_accessions or []) | detected_accessions)
            self._record_unavailable("sec_submissions_status", cik, accessions, write_connection)
            LOGGER.info("SEC submissions unavailable symbol=%s cik=%s", symbol, cik)
            return {
                "facts_processed": 0,
                "companies_updated": 0,
                "companyfacts_unavailable": 0,
                "submissions_unavailable": 1,
                "submissions_seconds": elapsed,
                "companyfacts_seconds": 0.0,
                "parse_and_persist_seconds": 0.0,
                "companyfacts_requests": 0,
                "accessions": accessions,
            }
        except Exception as exc:
            raise SecCompanySyncError(
                "request", "submissions", self.timer() - request_started, exc
            ) from exc
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
            or retry_unavailable
            or (initializing_state and not self.database.has_fundamental_facts(cik))
            or bool((current_accessions | detected_accessions) - previous_accessions)
        )
        new_state = sorted(previous_accessions | current_accessions | detected_accessions)
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
            try:
                payload = self.sec.company_facts(cik)
            except SecResourceNotFound:
                companyfacts_seconds = self.timer() - request_started
                self._record_unavailable(
                    "sec_companyfacts_status", cik, new_state, write_connection
                )
                self.database.delete_sync_value(
                    "sec_submissions_status", cik, connection=write_connection
                )
                LOGGER.info("SEC companyfacts unavailable symbol=%s cik=%s", symbol, cik)
                return {
                    "facts_processed": 0,
                    "companies_updated": 0,
                    "companyfacts_unavailable": 1,
                    "submissions_unavailable": 0,
                    "submissions_seconds": submissions_seconds,
                    "companyfacts_seconds": companyfacts_seconds,
                    "parse_and_persist_seconds": 0.0,
                    "companyfacts_requests": 1,
                    "accessions": new_state,
                }
            except Exception as exc:
                raise SecCompanySyncError(
                    "request", "companyfacts", self.timer() - request_started, exc
                ) from exc
            companyfacts_seconds = self.timer() - request_started
            processing_started = self.timer()
            try:
                facts = parse_company_facts(payload, symbol)
            except Exception as exc:
                raise SecCompanySyncError(
                    "parse", "companyfacts", self.timer() - processing_started, exc
                ) from exc
            try:
                self.database.delete_sync_value(
                    "sec_submissions_status", cik, connection=write_connection
                )
                facts_processed = self.database.upsert_sec_company_update(
                    company,
                    facts,
                    new_state,
                    connection=write_connection,
                )
            except Exception as exc:
                raise SecCompanySyncError(
                    "database", "structured_facts", self.timer() - processing_started, exc
                ) from exc
            parse_and_persist_seconds = self.timer() - processing_started
        elif initializing_state:
            self.database.set_sync_value(
                "sec_accessions", cik, new_state, connection=write_connection
            )
        return {
            "facts_processed": facts_processed,
            "companies_updated": int(needs_facts),
            "companyfacts_unavailable": 0,
            "submissions_unavailable": 0,
            "submissions_seconds": submissions_seconds,
            "companyfacts_seconds": companyfacts_seconds,
            "parse_and_persist_seconds": parse_and_persist_seconds if needs_facts else 0.0,
            "companyfacts_requests": int(needs_facts),
            "accessions": new_state if needs_facts or initializing_state else None,
        }

    def _daily_master_index_snapshots(self) -> DailyMasterIndexDiscovery:
        """Discover and parse available daily master files around the saved cursor."""

        if self.sec is None:
            raise ValueError("SEC client is required for SEC synchronization")
        today = self.clock().date()
        raw_cursor = self.database.sync_value(
            SEC_CHANGE_DETECTION_SOURCE, DAILY_MASTER_INDEX_CURSOR_KEY
        )
        cursor: date | None = None
        legacy_cursor_bootstrap = False
        if raw_cursor is not None:
            if not isinstance(raw_cursor, Mapping) or not raw_cursor.get("last_processed_date"):
                raise ValueError("Invalid SEC daily-master-index cursor")
            try:
                cursor = date.fromisoformat(str(raw_cursor["last_processed_date"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid SEC daily-master-index cursor") from exc
        else:
            legacy = self.database.sync_value(
                SEC_CHANGE_DETECTION_SOURCE, LEGACY_XBRL_INDEX_CURSOR_KEY
            )
            if isinstance(legacy, Mapping) and legacy.get("last_data_received"):
                try:
                    cursor = date.fromisoformat(str(legacy["last_data_received"]))
                except (TypeError, ValueError):
                    LOGGER.warning("Ignoring invalid legacy SEC XBRL-index cursor")
                else:
                    legacy_cursor_bootstrap = True
        if cursor is not None and cursor > today:
            raise ValueError("SEC daily-master-index cursor is in the future")

        discovery_start = (cursor or today) - timedelta(days=DAILY_INDEX_OVERLAP_DAYS)
        resources_by_date: dict[date, DailyMasterIndexResource] = {}
        quarters = _quarters_between(discovery_start, today)
        for year, quarter in quarters:
            payload = self.sec.daily_master_index_directory(year, quarter)
            for resource in parse_daily_index_directory(payload, year, quarter):
                if discovery_start <= resource.index_date <= today:
                    resources_by_date[resource.index_date] = resource

        resources = tuple(resources_by_date[item] for item in sorted(resources_by_date))
        snapshots = tuple(
            parse_daily_master_index(
                self.sec.daily_master_index(
                    resource.year,
                    resource.quarter,
                    resource.index_date.strftime("%Y%m%d"),
                ),
                resource.index_date,
            )
            for resource in resources
        )
        return DailyMasterIndexDiscovery(
            cursor_before=cursor,
            legacy_cursor_bootstrap=legacy_cursor_bootstrap,
            directories_checked=len(quarters),
            indexes_discovered=len(resources),
            indexes_new=sum(cursor is None or item.index_date > cursor for item in resources),
            snapshots=snapshots,
        )

    def _negative_cache_fresh(self, status: Any) -> bool:
        if not isinstance(status, dict) or status.get("status") != "unavailable":
            return False
        try:
            checked = datetime.fromisoformat(str(status["last_checked_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return self.clock() - checked.astimezone(UTC) < self.companyfacts_unavailable_ttl

    def _negative_cache_expired(self, status: Any) -> bool:
        return (
            isinstance(status, dict)
            and status.get("status") == "unavailable"
            and not self._negative_cache_fresh(status)
        )

    def _record_unavailable(
        self,
        source: str,
        cik: str,
        accessions: list[str],
        connection: Any,
    ) -> None:
        self.database.set_sync_value("sec_accessions", cik, accessions, connection=connection)
        self.database.set_sync_value(
            source,
            cik,
            {
                "status": "unavailable",
                "last_checked_at": self.clock().isoformat(),
                "last_submission_accession": accessions[-1] if accessions else None,
                "last_http_status": 404,
            },
            connection=connection,
        )

    def _sec_request_counts(self) -> dict[str, int] | None:
        counts = getattr(self.sec, "request_counts", None)
        return dict(counts) if isinstance(counts, Mapping) else None

    def _request_count_delta(
        self, before: dict[str, int] | None, logical: dict[str, int]
    ) -> dict[str, int]:
        after = self._sec_request_counts()
        if before is None or after is None:
            return logical
        return {key: after.get(key, 0) - before.get(key, 0) for key in set(before) | set(after)}

    def sync_historical_bars(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage(
            "historical_bars", lambda: self._sync_historical_bars(requested_symbols)
        )

    def _sync_historical_bars(self, requested_symbols: list[str] | None) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for historical-bar synchronization")
        available = {company.symbol for company in self.database.list_tradable_companies()}
        selected = available if not requested_symbols else available & set(requested_symbols)
        identity_conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        skipped = sorted(selected & identity_conflicts)
        symbols = sorted(selected - identity_conflicts)
        completed_session = latest_completed_trading_session()
        full_start, end = full_history_request_window(completed_session, self.market_data_days)
        latest = self.database.latest_bar_timestamps(symbols)
        starts: dict[datetime, list[str]] = defaultdict(list)
        for symbol in symbols:
            starts[latest[symbol] - timedelta(days=7) if symbol in latest else full_start].append(
                symbol
            )
        counts = {
            "symbols_checked": len(symbols),
            "identity_conflicts_skipped": len(skipped),
            "identity_conflict_sample": skipped[:10],
            "records_updated": 0,
            "errors": 0,
        }
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

    def sync_daily_history(
        self,
        requested_symbols: Iterable[str] | None,
        start: date,
        end: date,
        *,
        incremental: bool = True,
        include_benchmark: bool = True,
    ) -> dict[str, Any]:
        """Backfill an explicit inclusive Daily range with bidirectional edge gaps."""

        if start > end:
            raise ValueError("Daily history start must not be after end")
        return self._run_stage(
            "daily_history",
            lambda: self._sync_daily_history(
                requested_symbols,
                start,
                end,
                incremental=incremental,
                include_benchmark=include_benchmark,
            ),
        )

    def _sync_daily_history(
        self,
        requested_symbols: Iterable[str] | None,
        start: date,
        end: date,
        *,
        incremental: bool,
        include_benchmark: bool,
    ) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for Daily-history synchronization")
        selected = (
            {company.symbol for company in self.database.list_tradable_companies()}
            if requested_symbols is None
            else {symbol.strip().upper() for symbol in requested_symbols if symbol.strip()}
        )
        if include_benchmark:
            selected.add("SPY")
        identity_conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        skipped = sorted((selected - {"SPY"}) & identity_conflicts)
        symbols = sorted(selected - set(skipped))
        if not symbols:
            raise ValueError("Daily-history sync symbol selection is empty")

        start_at = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        end_at = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        expected_sessions = trading_sessions_between(start, end)
        if not expected_sessions:
            raise ValueError("Daily-history sync range contains no XNYS sessions")
        feed = _enum_text(getattr(self.alpaca, "feed", None))
        adjustment = _enum_text(getattr(self.alpaca, "adjustment", None))
        file_size_before = self.database.path.stat().st_size if self.database.path.exists() else 0
        bars_before = self.database.bar_count(BarTimeframe.DAY_1)
        started = self.timer()
        download_seconds = 0.0
        database_write_seconds = 0.0
        bounds_before = self.database.bar_bounds_by_symbol(
            symbols,
            BarTimeframe.DAY_1,
            start=start_at,
            end=end_at,
        )
        coverage_state = self.database.sync_values("daily_history_coverage")
        grouped_ranges: dict[tuple[datetime, datetime], list[str]] = defaultdict(list)
        planned_ranges: Counter[str] = Counter()
        for symbol in symbols:
            earliest, latest = bounds_before.get(symbol, (None, None))
            verified = _daily_range_verified(
                coverage_state.get(symbol), start_at, end_at, feed, adjustment
            )
            ranges = (
                _bar_edge_ranges(
                    earliest,
                    latest,
                    BarTimeframe.DAY_1,
                    start_at,
                    end_at,
                    self.daily_overlap_bars,
                    previously_verified=True,
                )
                if incremental and verified
                else [(start_at, end_at)]
            )
            for requested_range in ranges:
                grouped_ranges[requested_range].append(symbol)
                planned_ranges[symbol] += 1

        total_batches = sum(
            (len(items) + self.market_data_batch_size - 1) // self.market_data_batch_size
            for items in grouped_ranges.values()
        )
        successful_ranges: Counter[str] = Counter()
        symbols_received: set[str] = set()
        first_received: datetime | None = None
        last_received: datetime | None = None
        counts: dict[str, Any] = {
            "timeframe": BarTimeframe.DAY_1.value,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "incremental": incremental,
            "feed": feed,
            "adjustment": adjustment,
            "symbols_requested": len(symbols),
            "identity_conflicts_skipped": len(skipped),
            "identity_conflict_sample": skipped[:10],
            "symbols_with_data": 0,
            "symbols_without_data": 0,
            "symbols_without_older_data": 0,
            "bars_before": bars_before,
            "bars_received": 0,
            "bars_inserted": 0,
            "bars_updated": 0,
            "bars_unchanged": 0,
            "duplicate_bars": 0,
            "invalid_bars": 0,
            "request_batches": 0,
            "sqlite_write_batches": 0,
            "errors": 0,
        }
        LOGGER.info(
            "DAILY HISTORY BACKFILL requested=%s -> %s symbols=%d feed=%s adjustment=%s",
            start,
            end,
            len(symbols),
            feed,
            adjustment,
        )
        progress = 0
        for (range_start, range_end), grouped_symbols in sorted(grouped_ranges.items()):
            for batch in _chunks(grouped_symbols, self.market_data_batch_size):
                progress += 1
                counts["request_batches"] += 1
                try:
                    download_started = self.timer()
                    downloaded = self.alpaca.daily_bars(batch, range_start, range_end)
                    download_seconds += self.timer() - download_started
                    counts["bars_received"] += len(downloaded)
                    symbols_received.update(bar.symbol for bar in downloaded)
                    diagnostics = getattr(self.alpaca, "last_bar_diagnostics", {})
                    counts["invalid_bars"] += int(diagnostics.get("invalid_bars", 0))
                    if downloaded:
                        batch_first = min(bar.timestamp for bar in downloaded)
                        batch_last = max(bar.timestamp for bar in downloaded)
                        first_received = (
                            batch_first
                            if first_received is None
                            else min(first_received, batch_first)
                        )
                        last_received = (
                            batch_last if last_received is None else max(last_received, batch_last)
                        )
                    write_started = self.timer()
                    write_counts = self.database.upsert_bars_with_stats(downloaded)
                    database_write_seconds += self.timer() - write_started
                    counts["sqlite_write_batches"] += 1
                    for key in (
                        "bars_inserted",
                        "bars_updated",
                        "bars_unchanged",
                        "duplicate_bars",
                        "invalid_bars",
                    ):
                        counts[key] += write_counts[key]
                    successful_ranges.update(batch)
                except Exception:
                    counts["errors"] += 1
                    LOGGER.exception(
                        "Failed Daily-history batch symbols=%d first=%s last=%s start=%s end=%s",
                        len(batch),
                        batch[0],
                        batch[-1],
                        range_start.isoformat(),
                        range_end.isoformat(),
                    )
                LOGGER.info(
                    "DAILY HISTORY progress [%d/%d] batches bars=%d inserted=%d "
                    "updated=%d errors=%d",
                    progress,
                    total_batches,
                    counts["bars_received"],
                    counts["bars_inserted"],
                    counts["bars_updated"],
                    counts["errors"],
                )

        completed_symbols = {
            symbol
            for symbol in symbols
            if planned_ranges[symbol] == successful_ranges[symbol]
        }
        checked_at = self.clock().isoformat()
        self.database.set_sync_values(
            "daily_history_coverage",
            {
                symbol: _daily_coverage_value(
                    coverage_state.get(symbol),
                    start_at,
                    end_at,
                    feed,
                    adjustment,
                    checked_at,
                )
                for symbol in completed_symbols
            },
        )
        bounds_after = self.database.bar_bounds_by_symbol(
            symbols,
            BarTimeframe.DAY_1,
            start=start_at,
            end=end_at,
        )
        global_first, global_last = self.database.bar_bounds(
            "SPY", BarTimeframe.DAY_1
        )
        daily_first, daily_last = self.database.bar_date_bounds(
            timeframe=BarTimeframe.DAY_1
        )
        first_expected = expected_sessions[0]
        without_older = [
            symbol
            for symbol, (first, _last) in bounds_after.items()
            if first.date() > first_expected
        ]
        boundary_counts = Counter(
            first.date()
            for first, _last in bounds_before.values()
            if first > start_at
        )
        integrity = []
        for boundary, _occurrences in boundary_counts.most_common(3):
            boundary_symbols = [
                symbol
                for symbol, (first, _last) in bounds_before.items()
                if first.date() == boundary
            ]
            integrity.append(
                boundary_integrity_check(self.database, boundary_symbols, boundary)
            )
        elapsed = max(self.timer() - started, 1e-12)
        file_size_after = self.database.path.stat().st_size if self.database.path.exists() else 0
        bars_after = self.database.bar_count(BarTimeframe.DAY_1)
        counts.update(
            {
                "symbols_with_data": len(bounds_after),
                "symbols_without_data": len(set(symbols) - set(bounds_after)),
                "symbols_without_older_data": len(without_older),
                "symbols_without_older_data_sample": without_older[:20],
                "symbols_received": len(symbols_received),
                "bars_after": bars_after,
                "records_updated": counts["bars_inserted"] + counts["bars_updated"],
                "first_received_timestamp": (
                    first_received.isoformat() if first_received else None
                ),
                "last_received_timestamp": last_received.isoformat() if last_received else None,
                "first_daily_timestamp": daily_first.isoformat() if daily_first else None,
                "last_daily_timestamp": daily_last.isoformat() if daily_last else None,
                "spy_first_timestamp": global_first.isoformat() if global_first else None,
                "spy_last_timestamp": global_last.isoformat() if global_last else None,
                "download_seconds": round(download_seconds, 3),
                "database_write_seconds": round(database_write_seconds, 3),
                "total_seconds": round(elapsed, 3),
                "bars_per_second": round(counts["bars_received"] / elapsed, 3),
                "database_size_before": file_size_before,
                "database_size_after": file_size_after,
                "database_size_delta_bytes": file_size_after - file_size_before,
                "boundary_integrity": integrity,
            }
        )
        return counts

    def sync_intraday(
        self,
        requested_symbols: Iterable[str],
        timeframes: Iterable[BarTimeframe | str],
        start: datetime,
        end: datetime,
        *,
        incremental: bool | None = None,
        extended_hours: bool | None = None,
    ) -> dict[str, Any]:
        normalized_timeframes = tuple(dict.fromkeys(BarTimeframe(item) for item in timeframes))
        if not normalized_timeframes or any(not item.intraday for item in normalized_timeframes):
            raise ValueError("Intraday sync requires one or more of: 5m, 15m, 1h")
        return self._run_stage(
            "intraday_bars",
            lambda: self._sync_intraday(
                requested_symbols,
                normalized_timeframes,
                start,
                end,
                incremental=self.intraday_incremental if incremental is None else incremental,
                extended_hours=(
                    self.intraday_extended_hours
                    if extended_hours is None
                    else extended_hours
                ),
            ),
        )

    def _sync_intraday(
        self,
        requested_symbols: Iterable[str],
        timeframes: tuple[BarTimeframe, ...],
        start: datetime,
        end: datetime,
        *,
        incremental: bool,
        extended_hours: bool,
    ) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for intraday synchronization")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Intraday sync boundaries must be timezone-aware")
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if start >= end:
            raise ValueError("Intraday sync start must be before end")
        symbols = sorted({symbol.upper() for symbol in requested_symbols if symbol.strip()})
        if not symbols:
            raise ValueError(
                "Intraday sync requires explicit symbols; use --universe all intentionally"
            )
        file_size_before = self.database.path.stat().st_size if self.database.path.exists() else 0
        started = self.timer()
        download_seconds = 0.0
        database_write_seconds = 0.0
        symbols_with_data: set[str] = set()
        first_timestamp: datetime | None = None
        last_timestamp: datetime | None = None
        counts: dict[str, Any] = {
            "timeframes": [item.value for item in timeframes],
            "extended_hours": extended_hours,
            "incremental": incremental,
            "symbols_requested": len(symbols),
            "symbols_with_data": 0,
            "symbols_without_data": 0,
            "bars_downloaded": 0,
            "bars_received": 0,
            "bars_inserted": 0,
            "bars_updated": 0,
            "bars_unchanged": 0,
            "duplicate_bars": 0,
            "invalid_bars": 0,
            "request_batches": 0,
            "sqlite_write_batches": 0,
            "errors": 0,
        }
        for timeframe in timeframes:
            grouped_ranges: dict[tuple[datetime, datetime], list[str]] = defaultdict(list)
            for symbol in symbols:
                ranges = (
                    _incremental_bar_ranges(
                        self.database,
                        symbol,
                        timeframe,
                        start,
                        end,
                        self.intraday_overlap_bars,
                    )
                    if incremental
                    else [(start, end)]
                )
                for requested_range in ranges:
                    grouped_ranges[requested_range].append(symbol)
            total_groups = sum(
                (len(items) + self.intraday_symbol_batch_size - 1)
                // self.intraday_symbol_batch_size
                for items in grouped_ranges.values()
            )
            progress = 0
            LOGGER.info(
                "INTRADAY SYNC timeframe=%s symbols=%d start=%s end=%s extended_hours=%s",
                timeframe.value,
                len(symbols),
                start.isoformat(),
                end.isoformat(),
                extended_hours,
            )
            for (range_start, range_end), grouped_symbols in sorted(grouped_ranges.items()):
                for batch in _chunks(grouped_symbols, self.intraday_symbol_batch_size):
                    progress += 1
                    batch_failed = False
                    for window_start, window_end in _time_windows(
                        range_start, range_end, self.intraday_request_window_days
                    ):
                        counts["request_batches"] += 1
                        try:
                            download_started = self.timer()
                            downloaded = self.alpaca.bars(
                                batch,
                                window_start,
                                window_end,
                                timeframe=timeframe,
                                batch_size=len(batch),
                            )
                            download_seconds += self.timer() - download_started
                            counts["bars_downloaded"] += len(downloaded)
                            diagnostics = getattr(self.alpaca, "last_bar_diagnostics", {})
                            counts["invalid_bars"] += int(diagnostics.get("invalid_bars", 0))
                            selected = (
                                downloaded
                                if extended_hours
                                else [
                                    bar
                                    for bar in downloaded
                                    if is_regular_session_timestamp(bar.timestamp)
                                ]
                            )
                            counts["bars_received"] += len(selected)
                            symbols_with_data.update(bar.symbol for bar in selected)
                            if selected:
                                local_first = min(bar.timestamp for bar in selected)
                                local_last = max(bar.timestamp for bar in selected)
                                first_timestamp = (
                                    local_first
                                    if first_timestamp is None
                                    else min(first_timestamp, local_first)
                                )
                                last_timestamp = (
                                    local_last
                                    if last_timestamp is None
                                    else max(last_timestamp, local_last)
                                )
                            write_started = self.timer()
                            write_counts = self.database.upsert_bars_with_stats(selected)
                            database_write_seconds += self.timer() - write_started
                            counts["sqlite_write_batches"] += 1
                            for key in (
                                "bars_inserted",
                                "bars_updated",
                                "bars_unchanged",
                                "duplicate_bars",
                                "invalid_bars",
                            ):
                                counts[key] += write_counts[key]
                        except Exception:
                            counts["errors"] += 1
                            batch_failed = True
                            LOGGER.exception(
                                "Failed intraday batch timeframe=%s symbols=%s start=%s end=%s",
                                timeframe.value,
                                ",".join(batch),
                                window_start.isoformat(),
                                window_end.isoformat(),
                            )
                            # Do not jump past a failed time window. Keeping the local
                            # high-water mark before the failure makes the next incremental
                            # run resume from the last durable batch without a silent hole.
                            break
                    LOGGER.info(
                        "INTRADAY SYNC progress timeframe=%s [%d/%d] symbol_batches bars=%d",
                        timeframe.value,
                        progress,
                        total_groups,
                        counts["bars_received"],
                    )
                    if batch_failed:
                        continue
        elapsed = max(self.timer() - started, 1e-12)
        file_size_after = self.database.path.stat().st_size if self.database.path.exists() else 0
        counts.update(
            {
                "symbols_with_data": len(symbols_with_data),
                "symbols_without_data": len(set(symbols) - symbols_with_data),
                "first_timestamp": first_timestamp.isoformat() if first_timestamp else None,
                "last_timestamp": last_timestamp.isoformat() if last_timestamp else None,
                "records_updated": counts["bars_inserted"] + counts["bars_updated"],
                "download_seconds": round(download_seconds, 3),
                "database_write_seconds": round(database_write_seconds, 3),
                "bars_per_second": round(counts["bars_received"] / elapsed, 3),
                "database_size_delta_bytes": file_size_after - file_size_before,
                "sqlite_query_count": len(symbols) * len(timeframes)
                + counts["sqlite_write_batches"] * 4,
            }
        )
        LOGGER.info(
            "INTRADAY SYNC COMPLETE timeframes=%s bars=%d inserted=%d updated=%d errors=%d",
            ",".join(item.value for item in timeframes),
            counts["bars_received"],
            counts["bars_inserted"],
            counts["bars_updated"],
            counts["errors"],
        )
        return counts

    def refresh_market(self, requested_symbols: list[str] | None = None) -> dict[str, Any]:
        return self._run_stage("market_snapshot", lambda: self._refresh_market(requested_symbols))

    def _refresh_market(self, requested_symbols: list[str] | None) -> dict[str, Any]:
        if self.alpaca is None:
            raise ValueError("Alpaca client is required for market refresh")
        candidates = self._screenable_symbols(requested_symbols)
        identity_conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        skipped = sorted(set(candidates) & identity_conflicts)
        symbols = [symbol for symbol in candidates if symbol not in identity_conflicts]
        counts = {
            "symbols_requested": len(symbols),
            "identity_conflicts_skipped": len(skipped),
            "identity_conflict_sample": skipped[:10],
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
        status = (
            "partial"
            if result.get("errors", 0) or result.get("identity_conflicts", 0)
            else "success"
        )
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
