"""Independent, observable synchronization stages for local screening data."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import requests

from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    full_history_request_window,
    latest_completed_trading_session,
)
from trading_system.data.sec_client import SecClient, SecResourceNotFound
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.data.xbrl_parser import VALID_FORMS, parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset

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


@dataclass(frozen=True)
class FilingIndexSnapshot:
    last_data_received: date
    accessions_by_cik: dict[str, set[str]]


@dataclass(frozen=True)
class SecIdentityConflict:
    """A proposed SEC identity that contradicts a persisted canonical identity."""

    symbol: str
    proposed_cik: str
    existing_cik: str
    existing_symbol: str
    source: str


@dataclass(frozen=True)
class SecIdentityResolution:
    ticker_map: dict[str, str]
    alias_mappings: dict[str, str]
    canonical_symbols: dict[str, str]
    conflicts: tuple[SecIdentityConflict, ...]


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


def _security_symbol_priority(symbol: str) -> tuple[int, str]:
    structural = (".WS", ".RT", ".U", ".PR")
    return (int(any(marker in symbol for marker in structural)), symbol)


def _resolve_sec_identities(
    symbols: set[str],
    persisted: Mapping[str, str],
    current_sec: Mapping[str, str],
) -> SecIdentityResolution:
    """Resolve current SEC identities without silently moving persisted symbols or CIKs."""

    ticker_map: dict[str, str] = {}
    conflicts: list[SecIdentityConflict] = []
    for symbol in sorted(symbols):
        persisted_cik = persisted.get(symbol)
        current_cik = current_sec.get(symbol)
        if persisted_cik is not None and current_cik is not None and persisted_cik != current_cik:
            conflicts.append(
                SecIdentityConflict(
                    symbol=symbol,
                    proposed_cik=current_cik,
                    existing_cik=persisted_cik,
                    existing_symbol=symbol,
                    source="exact_sec_ticker",
                )
            )
            ticker_map[symbol] = persisted_cik
        elif current_cik is not None:
            ticker_map[symbol] = current_cik
        elif persisted_cik is not None:
            ticker_map[symbol] = persisted_cik

    alias_mappings = {
        symbol: current_sec[symbol.replace(".", "-")]
        for symbol in symbols - ticker_map.keys()
        if "." in symbol and symbol.replace(".", "-") in current_sec
    }
    ticker_map.update(alias_mappings)

    cik_symbols: dict[str, list[str]] = defaultdict(list)
    for symbol, cik in sorted(ticker_map.items()):
        cik_symbols[cik].append(symbol)
    existing_by_cik = {cik: symbol for symbol, cik in persisted.items()}
    canonical_symbols: dict[str, str] = {}
    for cik, candidates in cik_symbols.items():
        existing_symbol = existing_by_cik.get(cik)
        if existing_symbol is None:
            canonical_symbols[cik] = min(candidates, key=_security_symbol_priority)
        elif existing_symbol in candidates:
            canonical_symbols[cik] = existing_symbol
        else:
            proposed_symbol = min(candidates, key=_security_symbol_priority)
            conflicts.append(
                SecIdentityConflict(
                    symbol=proposed_symbol,
                    proposed_cik=cik,
                    existing_cik=cik,
                    existing_symbol=existing_symbol,
                    source=(
                        "dot_hyphen_alias"
                        if proposed_symbol in alias_mappings
                        else "exact_sec_ticker"
                    ),
                )
            )

    return SecIdentityResolution(
        ticker_map=ticker_map,
        alias_mappings=alias_mappings,
        canonical_symbols=canonical_symbols,
        conflicts=tuple(conflicts),
    )


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        if market_data_batch_size <= 0:
            raise ValueError("market_data_batch_size must be positive")
        if companyfacts_unavailable_ttl <= timedelta(0):
            raise ValueError("companyfacts_unavailable_ttl must be positive")
        self.database = database
        self.alpaca = alpaca
        self.sec = sec
        self.market_data_days = market_data_days
        self.market_data_batch_size = market_data_batch_size
        self.exclude_financials = exclude_financials
        self.exclude_reits = exclude_reits
        self.companyfacts_unavailable_ttl = companyfacts_unavailable_ttl
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
        identity = _resolve_sec_identities(symbols, local_map, remote_map)
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
        change_detection_requests = 0
        index_through: date | None = None
        negative_cache_hits = 0
        if full:
            candidate_ciks = set(canonical_symbols)
        else:
            detection_started = self.timer()
            snapshots = self._filing_index_snapshots()
            change_detection_seconds = self.timer() - detection_started
            change_detection_requests = len(snapshots)
            index_through = max(snapshot.last_data_received for snapshot in snapshots)
            for snapshot in snapshots:
                for cik, accessions in snapshot.accessions_by_cik.items():
                    if cik in canonical_symbols:
                        change_accessions[cik].update(accessions)
            candidate_ciks = {
                cik
                for cik in canonical_symbols
                if change_accessions.get(cik, set()) - set(accession_states.get(cik, []))
                or cik not in accession_states
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
            "filing_index": change_detection_requests,
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
            if not full and counts["errors"] == 0 and index_through is not None:
                self.database.set_sync_value(
                    "sec_change_detection",
                    "xbrl_index",
                    {"last_data_received": index_through.isoformat()},
                    connection=write_connection,
                )
                write_connection.commit()
        counts["submissions_seconds"] = round(submissions_seconds, 3)
        counts["companyfacts_seconds"] = round(companyfacts_seconds, 3)
        counts["change_detection_seconds"] = round(change_detection_seconds, 3)
        counts["parse_and_persist_seconds"] = round(parse_and_persist_seconds, 3)
        request_counts = self._request_count_delta(request_counts_before, logical_requests)
        counts.update(
            {
                "sec_requests_total": sum(request_counts.values()),
                "ticker_map_requests": request_counts.get("ticker_map", 0),
                "change_detection_requests": request_counts.get("filing_index", 0),
                "submissions_requests": request_counts.get("submissions", 0),
                "companyfacts_requests": request_counts.get("companyfacts", 0),
            }
        )
        return counts

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

    def _filing_index_snapshots(self) -> list[FilingIndexSnapshot]:
        if self.sec is None:
            raise ValueError("SEC client is required for SEC synchronization")
        today = self.clock().date()
        raw_cursor = self.database.sync_value("sec_change_detection", "xbrl_index")
        cursor: date | None = None
        if isinstance(raw_cursor, dict) and raw_cursor.get("last_data_received"):
            try:
                cursor = date.fromisoformat(str(raw_cursor["last_data_received"]))
            except ValueError as exc:
                raise ValueError("Invalid SEC XBRL-index cursor") from exc
            if cursor > today:
                raise ValueError("SEC XBRL-index cursor is in the future")
        quarters = _quarters_between(cursor or today, today)
        current_key = (today.year, (today.month - 1) // 3 + 1)
        snapshots = [
            parse_filing_index(
                self.sec.filing_index(year, quarter, current=(year, quarter) == current_key)
            )
            for year, quarter in quarters
        ]
        if cursor is not None and max(item.last_data_received for item in snapshots) < cursor:
            raise ValueError("SEC XBRL index regressed behind the saved cursor")
        return snapshots

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
        return self._run_stage("market_snapshot", lambda: self._refresh_market(requested_symbols))

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
