"""Explicit, bounded Companyfacts-only refresh using locally verified identities."""

from __future__ import annotations

import csv
import importlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import requests

from trading_system.data.database import Database
from trading_system.models.fundamentals import CompanyIdentity

AUDIT_STEM = "f_2022_data_gap_audit_v1"
DEFAULT_MANIFEST = Path("reports") / f"{AUDIT_STEM}_sec_backfill_candidates.csv"
MAX_SYMBOLS = 15
SEC_TABLES = {"companies", "fundamental_facts", "sync_state"}


class ReadOnlyDatabase(Database):
    def connect(self):
        return self.read_only()

    def initialize(self):
        raise RuntimeError("SEC_FORCE_INITIALIZE_FORBIDDEN")


class SecWriteDatabase(ReadOnlyDatabase):
    """Existing-file connection with a table-level SQLite write allowlist."""

    def __init__(self, path):
        super().__init__(path)
        self.authorized_write_tables = set()
        self.denied_writes = 0

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")

        def authorize(action, table, column, database, trigger):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                allowed = table in SEC_TABLES and (
                    action != sqlite3.SQLITE_DELETE or table == "sync_state"
                )
                if allowed and trigger is None:
                    self.authorized_write_tables.add(table)
                    return sqlite3.SQLITE_OK
                self.denied_writes += 1
                return sqlite3.SQLITE_DENY
            if action not in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_SAVEPOINT,
                sqlite3.SQLITE_RECURSIVE,
            }:
                self.denied_writes += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def resolve_local_companies(path, symbols):
    """No ticker endpoint, alias invention, or implicit universe expansion."""
    normalized = [symbol.strip().upper() for symbol in symbols]
    if not normalized or len(normalized) > MAX_SYMBOLS or len(set(normalized)) != len(normalized):
        raise ValueError("SEC_FORCE_REQUIRES_1_TO_15_DISTINCT_SYMBOLS")
    database = ReadOnlyDatabase(path)
    assets = {asset.symbol: asset for asset in database.list_tradable_assets()}
    conflicts = database.unresolved_sec_identity_conflict_symbols()
    cached = database.sync_value("sec_reference", "ticker_to_cik") or {}
    with database.read_only() as connection:
        rows = connection.execute("SELECT * FROM companies").fetchall()
    by_symbol = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    resolved = {}
    for symbol in normalized:
        matches = by_symbol[symbol]
        if symbol not in assets or symbol in conflicts or len(matches) != 1:
            raise ValueError(f"SEC_FORCE_UNRESOLVED_OR_CONFLICTED_SYMBOL:{symbol}")
        row = matches[0]
        cik = str(row["cik"])
        if not cik.isdigit() or len(cik) != 10 or int(cik) == 0:
            raise ValueError(f"SEC_FORCE_INVALID_CIK:{symbol}")
        if cached.get(symbol) and str(cached[symbol]).zfill(10) != cik:
            raise ValueError(f"SEC_FORCE_LOCAL_CIK_CONFLICT:{symbol}")
        resolved[symbol] = CompanyIdentity(
            **{key: row[key] for key in ("cik", "symbol", "name", "sic", "sic_description")}
        )
    if len({company.cik for company in resolved.values()}) != len(resolved):
        raise ValueError("SEC_FORCE_DUPLICATE_CIK")
    return resolved


def candidate_order(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["symbol"].upper()].append(row)
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    result = []
    for symbol, entries in grouped.items():
        priority = min((row["priority"] for row in entries), key=lambda p: rank[p])
        result.append(
            {
                "symbol": symbol,
                "cik": entries[0]["cik"].zfill(10),
                "priority": priority,
                # Factor-level counts overlap. Max is a deterministic lower bound
                # on the affected-session union, never a double-counted sum.
                "affected_sessions": max(int(row["affected_sessions"]) for row in entries),
                "affected_factors": sorted({row["affected_factor"] for row in entries}),
                "missing_input_classes": sorted(
                    {
                        item
                        for row in entries
                        for item in row["relevant_missing_metric_inputs"].split("|")
                    }
                ),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            rank[row["priority"]],
            -row["affected_sessions"],
            -len(row["affected_factors"]),
            row["symbol"],
        ),
    )


def verify_session_union_ranking(ranked, rows, coverage, selected, session_count):
    """Prove that overlapping factor counts cannot change the selected top list.

    The old manifest stores counts, not individual affected dates. For scoring
    factors their union is bounded by evaluated sessions; market-cap input gaps
    precede scoring. Reject ambiguous top lists rather than inventing a union.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["symbol"].upper()].append(row)
    bounds = {}
    for candidate in ranked:
        symbol = candidate["symbol"]
        entries = grouped[symbol]
        pre = sum(
            int(r["affected_sessions"])
            for r in entries
            if r["affected_factor"] == "market_cap_input"
        )
        scoring = sum(
            int(r["affected_sessions"])
            for r in entries
            if r["affected_factor"] != "market_cap_input"
        )
        evaluated = int(
            coverage.get(symbol, {}).get("fundamentals_evaluated_session_count", session_count)
        )
        lower = candidate["affected_sessions"]
        upper = min(session_count, pre + min(scoring, evaluated))
        if upper < lower:
            raise ValueError("SEC_FORCE_INCONSISTENT_MANIFEST_SESSION_COUNTS")
        bounds[symbol] = {"lower": lower, "upper": upper}
    if any(bounds[s]["lower"] != bounds[s]["upper"] for s in selected):
        raise ValueError("SEC_FORCE_AMBIGUOUS_SELECTED_SESSION_UNION")
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

    def key(candidate, optimistic=False):
        count = bounds[candidate["symbol"]]["upper" if optimistic else "lower"]
        return (
            rank[candidate["priority"]],
            -count,
            -len(candidate["affected_factors"]),
            candidate["symbol"],
        )

    # Only a manifest-driven top selection needs proof against unselected rows;
    # an explicit symbol subset retains deterministic ordering within its scope.
    cutoff = max(key(c) for c in ranked if c["symbol"] in selected)
    if any(key(c, True) < cutoff for c in ranked if c["symbol"] not in selected):
        raise ValueError("SEC_FORCE_AMBIGUOUS_TOP_SESSION_UNION")
    return {"exact_selected_counts": True, "top_selection_proven": True, "bounds": bounds}


def prepare_plan(path, manifest=DEFAULT_MANIFEST, *, symbols=None, limit=15):
    if not 1 <= limit <= MAX_SYMBOLS:
        raise ValueError("SEC_FORCE_LIMIT_MUST_BE_1_TO_15")
    with Path(manifest).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ranked = candidate_order(rows)
    requested = [s.strip().upper() for s in symbols or []]
    if requested and (len(set(requested)) != len(requested) or len(requested) > limit):
        raise ValueError("SEC_FORCE_EXPLICIT_SYMBOL_LIST_EXCEEDS_LIMIT_OR_HAS_DUPLICATES")
    if set(requested) - {row["symbol"] for row in ranked}:
        raise ValueError("SEC_FORCE_SYMBOL_NOT_IN_AUDIT_MANIFEST")
    # The default manifest's sibling evidence is required, even for explicit symbols.
    directory = Path(manifest).parent
    universe = json.loads((directory / f"{AUDIT_STEM}_universe_identity_summary.json").read_text())
    summary = json.loads((directory / f"{AUDIT_STEM}_summary.json").read_text())
    with (directory / f"{AUDIT_STEM}_symbol_coverage.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        coverage = {row["symbol"]: row for row in csv.DictReader(handle)}
    conflicts = set(universe.get("unresolved_current_SEC_identity_conflicts", []))
    conflicts.update(
        item["symbol"] for item in universe.get("historical_current_CIK_conflicts", [])
    )
    # A historical Massive probe is not a validated historical identity. Keep all
    # such unresolved research cases out of this narrowly scoped SEC pilot.
    conflicts.update(
        universe.get("symbols_with_2022_bars_and_Massive_historical_identity_evidence", [])
    )
    database = ReadOnlyDatabase(path)
    assets = {asset.symbol: asset for asset in database.list_tradable_assets()}
    selected, skipped = [], []
    from trading_system.data.sync import classify_unmapped_asset

    for row in ranked:
        symbol = row["symbol"]
        if requested and symbol not in requested:
            continue
        reason = None
        if (
            symbol in conflicts
            or coverage.get(symbol, {}).get("identity_review_candidate") == "True"
        ):
            reason = "AUDIT_IDENTITY_REVIEW_REQUIRED"
        raw = summary.get("local_raw_cache_diagnostics", {}).get(symbol, {})
        if raw.get("taxonomy_PIT_counts", {}).get("ifrs-full", 0):
            reason = "KNOWN_IFRS_MAPPING_CASE"
        if symbol in assets and classify_unmapped_asset(assets[symbol]) in {
            "etf_or_fund",
            "warrant",
            "unit",
            "rights",
            "preferred",
        }:
            reason = "UNSUPPORTED_SECURITY_TYPE"
        try:
            company = resolve_local_companies(path, [symbol])[symbol]
            if company.cik != row["cik"]:
                reason = "AUDIT_CIK_MISMATCH"
        except ValueError as exc:
            reason = str(exc)
        if reason:
            skipped.append({"symbol": symbol, "reason": reason})
            if requested:
                raise ValueError(f"SEC_FORCE_EXPLICIT_SYMBOL_REJECTED:{symbol}:{reason}")
            continue
        with database.read_only() as connection:
            facts = connection.execute(
                "SELECT count(*),min(filed),max(filed) FROM fundamental_facts WHERE cik=?",
                (company.cik,),
            ).fetchone()
        selected.append(
            {
                **{key: value for key, value in row.items() if key != "candidate_rows"},
                "current_tradable": True,
                "local_fact_count": facts[0],
                "earliest_filed": facts[1],
                "latest_filed": facts[2],
                "in_audit_candidate_manifest": True,
                "force_sync_would_be_attempted": True,
            }
        )
        if len(selected) == (len(requested) if requested else limit):
            break
    if not selected:
        raise ValueError("SEC_FORCE_NO_VALID_CANDIDATES")
    resolve_local_companies(path, [row["symbol"] for row in selected])
    proof = verify_session_union_ranking(
        [
            r
            for r in ranked
            if (not requested or r["symbol"] in requested)
            and r["symbol"] not in {skipped_row["symbol"] for skipped_row in skipped}
        ],
        rows,
        coverage,
        {r["symbol"] for r in selected},
        summary.get("sessions", 251),
    )
    return {
        "manifest": str(Path(manifest).resolve()),
        "requested_symbols": requested or [row["symbol"] for row in selected],
        "resolved_symbols": [row["symbol"] for row in selected],
        "selected": selected,
        "skipped": skipped,
        "limit": limit,
        "ranking": "priority; max affected sessions across factors; distinct factors; symbol",
        "session_union_ranking_verification": proof,
        "network_calls": 0,
        "database_writes": 0,
    }


@contextmanager
def companyfacts_boundary(session, ciks):
    """Only the existing client's explicit Companyfacts requests can leave process."""
    urls = {f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json" for cik in ciks}
    counts = {
        "sec_requests": 0,
        "successful_responses": 0,
        "failed_responses": 0,
        "transport_errors": 0,
        "other_provider_requests": 0,
        "asset_sync_calls": 0,
        "bar_sync_calls": 0,
        "massive_calls": 0,
        "broker_calls": 0,
        "blocked_attempts": 0,
        "http_statuses": Counter(),
    }
    original = requests.sessions.Session.request

    def request(self, method, url, *args, **kwargs):
        if self is not session or method.upper() != "GET" or url not in urls:
            counts["blocked_attempts"] += 1
            raise RuntimeError("SEC_FORCE_NETWORK_OUTSIDE_COMPANYFACTS_FORBIDDEN")
        kwargs["allow_redirects"] = False
        counts["sec_requests"] += 1
        try:
            response = original(self, method, url, *args, **kwargs)
        except requests.RequestException:
            counts["transport_errors"] += 1
            raise
        counts["http_statuses"][str(response.status_code)] += 1
        counts["successful_responses" if response.ok else "failed_responses"] += 1
        if 300 <= response.status_code < 400:
            raise requests.HTTPError("SEC_FORCE_REDIRECT_FORBIDDEN", response=response)
        return response

    def deny(*args, **kwargs):
        counts["blocked_attempts"] += 1
        raise RuntimeError("SEC_FORCE_OTHER_PROVIDER_OR_SYNC_FORBIDDEN")

    from trading_system.data.sync import DataSynchronizer

    with ExitStack() as stack:
        stack.enter_context(patch.object(requests.sessions.Session, "request", request))
        for name in (
            "sync",
            "sync_full",
            "sync_assets",
            "sync_historical_bars",
            "sync_daily_history",
            "sync_intraday",
            "refresh_market",
        ):
            stack.enter_context(patch.object(DataSynchronizer, name, deny))
        for module_name in (
            "alpaca_client",
            "massive_reference",
            "massive_identity_research",
            "openfigi_research",
            "alpaca_corporate_actions",
        ):
            module = importlib.import_module("trading_system.data." + module_name)
            for name, cls in vars(module).items():
                if isinstance(cls, type) and "Client" in name and cls.__module__ == module.__name__:
                    stack.enter_context(patch.object(cls, "__init__", deny))
        yield counts


def execute_force(path, symbols, user_agent, *, evidence_callback=None):
    from trading_system.data.sec_client import SecClient
    from trading_system.data.sync import DataSynchronizer

    companies = resolve_local_companies(path, symbols)
    database = SecWriteDatabase(path)
    with requests.Session() as session:
        session.trust_env = False
        with companyfacts_boundary(
            session, [company.cik for company in companies.values()]
        ) as counts:
            client = SecClient(user_agent, session=session)
            if evidence_callback is not None:
                original = client.company_facts

                def capture(cik):
                    payload = original(cik)
                    evidence_callback(cik, payload)
                    return payload

                client.company_facts = capture
            result = DataSynchronizer(database, None, client).sync_sec_full(
                symbols, companyfacts_only=True
            )
            counts["client_request_counts"] = client.request_counts
    counts["retry_count"] = max(0, counts["sec_requests"] - result["companies_checked"])
    counts["authorized_write_tables"] = sorted(database.authorized_write_tables)
    counts["denied_database_writes"] = database.denied_writes
    return result, counts


def command(settings, args):
    plan = prepare_plan(
        settings.strategy.storage.database_path,
        args.manifest or DEFAULT_MANIFEST,
        symbols=args.symbols.split(",") if args.symbols else None,
        limit=args.limit,
    )
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return 0
    from trading_system.backtest.sec_backfill_pilot import run_pilot

    summary = run_pilot(settings, plan)
    print(json.dumps(summary, indent=2))
    return 1 if summary["pilot_symbols_failed"] else 0
