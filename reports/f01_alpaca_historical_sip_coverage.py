"""Offline-only Alpaca SIP coverage audit for the eight fixed F01 controls."""

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, time
from importlib.metadata import version
from pathlib import Path

from f01_historical_50_preflight_run import (
    BASELINE,
    NY,
    path_state,
    preceding_sessions,
    valid_bar,
)

from trading_system.data.canonical_paths import pinned_files
from trading_system.data.market_sessions import trading_sessions_between

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = Path("G:/TraWalpResearch/import-artifacts")
START, END = "2016-01-01", "2026-11-20"
SYMBOLS = ("SPY", "CAT", "LLY", "AAPL", "MSFT", "COST", "NVDA", "ACN")
D6 = ROOT / "reports/f01_historical_50_preflight.json"
ALPACA = RESEARCH / "f01-market-20261123-192923-451-alpaca.json"
MASSIVE = RESEARCH / "f01-market-20261123-195632-943-massive.json"
D7 = ROOT / "reports/f01_historical_market_feasibility.json"
EXPECTED_HASHES = {
    D6: "2c47e3da2bd21c03cc262232e85d775c0fde7190d49149656dfe72d8c3fc9752",
    ALPACA: "53cdee4bbed06bb4df802b4556647765684f5c68331332b37d57c03d332b6663",
    MASSIVE: "13fa5293be5918151f696f24a313d86f40156f36609ba93803a992093c030e7a",
}
VARIANTS = tuple((feed, adj) for feed in ("sip", "iex") for adj in ("raw", "split", "all"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def load(path):
    raw = path.read_bytes()
    digest = sha(raw)
    if path in EXPECTED_HASHES and digest != EXPECTED_HASHES[path]:
        raise ValueError(f"BOUND_SOURCE_HASH_MISMATCH: {path.name}")
    return json.loads(raw), {"path": str(path), "sha256": digest}


def calendar():
    warm = [d.isoformat() for d in preceding_sessions(date.fromisoformat(START), 300)]
    days = [
        d.isoformat()
        for d in trading_sessions_between(date.fromisoformat(START), date.fromisoformat(END))
    ]
    periods = {"WARMUP": warm}
    for year in range(2016, 2027):
        periods[str(year)] = [d for d in days if d[:4] == str(year)]
    return periods


def apply_epoch(sessions, epochs):
    """An interval is usable only with attested security and effective bounds."""
    if len(epochs) != 1 or not epochs[0].get("verified") or not epochs[0].get("security_id"):
        return sessions, None
    epoch = epochs[0]
    if not epoch.get("start") or not epoch.get("end") or epoch["start"] >= epoch["end"]:
        return sessions, None
    return [d for d in sessions if epoch["start"] <= d < epoch["end"]], epoch["security_id"]


def gaps(sessions, missing):
    group = []
    for day in sessions:
        if day in missing:
            group.append(day)
        elif group:
            yield group
            group = []
    if group:
        yield group


def coverage(sessions, seen, useful, invalid, security):
    present = sorted(set(sessions) & useful)
    missing = set(sessions) - useful
    runs = list(gaps(sessions, missing))
    status = (
        "BLOCKED"
        if not security
        else "MISSING"
        if not present
        else "PARTIAL"
        if missing or invalid
        else "QUALIFIED"
    )
    return {
        "expected_sessions": len(sessions),
        "observed_sessions": len(set(sessions) & seen),
        "usable_sessions": len(present),
        "missing_sessions": len(missing),
        "invalid_bars": invalid,
        "first_usable": present[0] if present else "",
        "last_usable": present[-1] if present else "",
        "largest_consecutive_gap": max(map(len, runs), default=0),
        "internal_missing_sessions": sum(present[0] < d < present[-1] for d in missing)
        if present
        else 0,
        "technical_coverage": "MISSING"
        if not present
        else "PARTIAL"
        if missing or invalid
        else "COMPLETE_IN_REFERENCE_CALENDAR",
        "qualification": status,
    }, runs


def populate(rows):
    digest = hashlib.sha256()
    seen, good, invalid = set(), set(), Counter()
    for row in rows:
        digest.update(encode(row) + b"\n")
        stamp = datetime.fromisoformat(row[0])
        if stamp.utcoffset() is None:
            raise ValueError("UNZONED_BAR_TIMESTAMP")
        local = stamp.astimezone(NY)
        session = local.date().isoformat()
        provenance_ok = len(row) == 6 or (
            row[6] == session
            and datetime.fromisoformat(row[7]).utcoffset() is not None
            and bool(row[8])
        )
        if session in seen:
            invalid[session] += 1
            good.discard(session)
        elif provenance_ok and local.time() == time.min and valid_bar(row[:6]):
            good.add(session)
        else:
            invalid[session] += 1
        seen.add(session)
    return seen, good, invalid, digest.hexdigest()


def read_stores(paths, dates):
    result, bindings = {}, {}
    for source, path in paths.items():
        if not path.is_file():
            bindings[source] = {"path": str(path), "status": "SOURCE_MISSING"}
            continue
        with pinned_files({"research": path}) as handles:
            before = path_state(path)
            if not os.path.samestat(os.fstat(handles["research"].fileno()), path.stat()):
                raise ValueError(f"SQLITE_FILE_ID_DRIFT: {source}")
            if before.get(path.name + "-wal", (0, 0, 0))[2] or path.name + "-journal" in before:
                raise ValueError(f"SQLITE_REQUIRES_CHECKPOINT: {source}")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                for symbol in SYMBOLS:
                    result[(source, symbol, "unknown", "unknown")] = populate(
                        db.execute(
                            """SELECT timestamp,open,high,low,close,volume FROM bars
                           WHERE symbol=? AND timeframe='1d' AND timestamp>=? AND timestamp<?
                           ORDER BY timestamp""",
                            (symbol, dates[0], "2026-11-21"),
                        )
                    )
                    if source == "legacy":
                        result[("forward", symbol, "sip", "all")] = populate(
                            db.execute(
                                """SELECT timestamp,open,high,low,close,volume,session,retrieved_at,
                                      sync_reference FROM forward_market_observations
                               WHERE symbol=? AND session BETWEEN ? AND ? AND timeframe='1d'
                                 AND provider='alpaca' AND feed='sip' AND adjustment='all'
                               ORDER BY session""",
                                (symbol, dates[0], END),
                            )
                        )
            if before != path_state(path):
                raise ValueError(f"SQLITE_CHANGED_DURING_READ: {source}")
            bindings[source] = {"path": str(path), "file_state": before, "status": "READ_ONLY"}
    return result, bindings


def probe_series(observations):
    series, examined = defaultdict(list), set()
    for item in observations:
        p, q = item["probe"], item["probe"]["query"]
        if p["kind"] != "bars" or p["symbol"] not in SYMBOLS or q.get("asof") != "-":
            continue
        key = ("probe", p["symbol"], q["feed"], q["adjustment"])
        examined.add(key)
        if item.get("http_status") != 200 or item.get("invalid_rows", 0):
            continue
        for sym, session, *row in item.get("bars", []):
            if (
                sym != p["symbol"]
                or datetime.fromisoformat(row[0]).astimezone(NY).date().isoformat() != session
            ):
                raise ValueError("PROBE_BAR_PROVENANCE_MISMATCH")
            series[key].append(row)
    return {key: populate(sorted(series[key])) for key in examined}


def retained_for_plan(stores, symbol, variant):
    # Deduplication of acquisition demand only. Source-specific coverage remains independent.
    days = set()
    for (source, label, feed, adjustment), series in stores.items():
        if source in ("probe", "forward") and label == symbol and (feed, adjustment) == variant:
            days.update(series[1])
    return days


def build_matrix(periods, stores, identities, source_ids):
    rows, missing_runs = [], []
    for symbol in SYMBOLS:
        variants = [("probe", f, a) for f, a in VARIANTS]
        variants += [
            ("legacy", "unknown", "unknown"),
            ("u0", "unknown", "unknown"),
            ("forward", "sip", "all"),
        ]
        for source, feed, adj in variants:
            key = source, symbol, feed, adj
            seen, good, invalid, digest = stores.get(key, (set(), set(), {}, ""))
            valid_warmup = len(good & set(periods["WARMUP"]))
            for year, days in periods.items():
                scoped, security = apply_epoch(days, [])
                report, runs = coverage(
                    scoped, seen, good, sum(invalid.get(d, 0) for d in days), security
                )
                blockers = ["HISTORICAL_SECURITY_INTERVAL_UNPROVEN"]
                if not digest:
                    blockers.append("SOURCE_OR_VARIANT_NOT_OBSERVED")
                if feed == "unknown":
                    blockers.append("FEED_AND_ADJUSTMENT_NOT_BOUND_PER_BAR")
                if valid_warmup < 300:
                    blockers.append("INITIAL_WARMUP_INCOMPLETE")
                if report["missing_sessions"]:
                    blockers.append("MISSING_LOCAL_BARS")
                source_path = source_ids[source]["path"]
                base = dict(
                    symbol=symbol,
                    cik=identities.get(symbol, ""),
                    security_id=security or "",
                    epoch_status="UNPROVEN",
                    source=source,
                    provider="alpaca" if feed != "unknown" else "unknown",
                    feed=feed,
                    adjustment=adj,
                    year=year,
                    artifact=source_path,
                    artifact_sha256=source_ids[source].get("sha256", ""),
                    selected_rows_sha256=digest,
                )
                rows.append(
                    base
                    | report
                    | {
                        "warmup_usable": valid_warmup,
                        "warmup_expected": 300,
                        "blockers": "|".join(blockers),
                    }
                )
                for run in runs:
                    missing_runs.append(
                        base
                        | {
                            "gap_start": run[0],
                            "gap_end": run[-1],
                            "session_count": len(run),
                            "exact_sessions": " ".join(run),
                            "reason": "LOCAL_BAR_UNAVAILABLE",
                        }
                    )
    return rows, missing_runs


def acquisition(periods, stores, alpaca, d7):
    jobs = []
    by_variant = {}
    for adjustment in ("all", "raw"):
        missing_count = 0
        batches = defaultdict(list)
        for symbol in SYMBOLS:
            retained = retained_for_plan(stores, symbol, ("sip", adjustment))
            for year, days in periods.items():
                if year == "WARMUP":
                    continue
                tier = (
                    (2 if int(year) < 2020 else 3)
                    if symbol == "SPY"
                    else (4 if int(year) < 2020 else 5)
                )
                missing = set(days) - retained
                missing_count += len(missing)
                for run in gaps(days, missing):
                    batches[(tier, year, run[0], run[-1])].append(symbol)
        for (tier, year, first, last), symbols in sorted(batches.items()):
            for i in range(0, len(symbols), 5):
                labels = symbols[i : i + 5]
                request = dict(
                    priority=tier,
                    year=year,
                    start=first,
                    end=last,
                    symbols=labels,
                    provider="alpaca",
                    feed="sip",
                    adjustment=adjustment,
                    authorization="NOT_AUTHORIZED",
                    symbol_mapping="exact_epoch_label",
                    prerequisite="TRADING_EPOCH_REVIEW_AND_FREE_STORAGE_TERMS",
                )
                jobs.append(request | {"planning_id": sha(encode(request))})
        samples = [
            o
            for o in alpaca["observations"]
            if o["probe"]["query"].get("feed") == "sip"
            and o["probe"]["query"].get("adjustment") == adjustment
            and o["probe"]["symbol"] in SYMBOLS
            and o["classification"] == "SAMPLE_COMPLETE"
        ]
        size = {}
        for key in ("normalized_jsonl_bytes", "normalized_gzip_bytes", "decoded_body_bytes"):
            values = [(o[key], o["rows_usable"]) for o in samples if key in o]
            if values:
                count = sum(n for _, n in values)
                size[key] = (missing_count * sum(b for b, _ in values) + count - 1) // count
        if adjustment == "all":
            scenario = d7["local_storage_measurement"]["capacity"]["by_feed_and_adjustment"][0]
            for key in ("normalized_jsonl_bytes", "normalized_gzip_bytes"):
                ref = (
                    missing_count * scenario["projected_bytes"][key]
                    + scenario["projected_rows"]
                    - 1
                ) // scenario["projected_rows"]
                size[key] = max(size.get(key, 0), ref)
        by_variant[adjustment] = {"missing_row_slots": missing_count, "estimated_bytes": size}
    jobs.sort(key=lambda j: (j["priority"], j["year"], j["start"], j["adjustment"], j["symbols"]))
    compact = sum(
        item["estimated_bytes"].get("normalized_gzip_bytes", 0) for item in by_variant.values()
    )
    transfer = sum(
        item["estimated_bytes"].get("decoded_body_bytes", 0) for item in by_variant.values()
    )
    manifest = 4096 * len(jobs)
    validation = 128 * sum(item["missing_row_slots"] for item in by_variant.values())
    return {
        "status": "PLAN_ONLY_NOT_AUTHORIZED",
        "priority_1": {
            "scope": "SPY pre-2016 warmup",
            "dates": periods["WARMUP"],
            "reason": "OUTSIDE_DOCUMENTED_FREE_START",
            "requests_planned": 0,
        },
        "priority_6": {
            "scope": "security and corporate-action evidence review",
            "requests_estimated": None,
        },
        "strategy": "year-bounded exact gaps; one source/variant per job; no feed substitution",
        "batch_size_max": 5,
        "request_years_max": 1,
        "jobs": jobs,
        "request_estimate": {
            "initial_pages": len(jobs),
            "planned_budget_at_two_pages": 2 * len(jobs),
            "documented_limit_per_minute": 200,
            "paced_at_30_per_min_estimated_minutes": round(len(jobs) / 30, 2),
            "scope": "stock bars only; pagination, latency and retries uncertain",
        },
        "capacity_estimate": {
            "variants": by_variant,
            "manifest_allowance_bytes": manifest,
            "validation_workspace_bytes": validation,
            "estimated_retained_compact_bytes": compact + manifest,
            "estimated_with_transfer_validation_and_one_backup_bytes": 2 * (compact + manifest)
            + validation
            + transfer,
            "excludes": [
                "pre-2016",
                "additional security sources",
                "db indexes",
                "raw protocol bytes unavailable from normalized archives",
            ],
        },
        "resume_key_fields": [
            "provider",
            "feed",
            "adjustment",
            "security_epoch",
            "symbol",
            "asof",
            "start",
            "end",
            "schema_version",
            "request_manifest_sha256",
        ],
        "commit": "validate primary and page hashes; immutable shards; atomic completion manifest",
        "minimal_next_probe": {
            "symbol": "SPY",
            "feed": "sip",
            "adjustment": "all",
            "start": "2016-01-11",
            "end": "2016-01-15",
            "request_max": 1,
            "after": ["attest free account", "verify persistent storage terms"],
            "reason": "first adjacent unobserved archived benchmark week",
            "automatic": False,
        },
    }


def security_evidence(d6, alpaca, massive):
    candidates = {r["symbol"]: r for r in d6["candidates"]}
    cases = {}
    for symbol, labels, reasons in (
        (
            "AAPL",
            {"AAPL"},
            ["PRICE_EPS_SHARES_BASIS_DATE_UNVERIFIED", "ANNOUNCEMENT_TIMESTAMP_UNBOUND"],
        ),
        ("ACN", {"ACN"}, ["CLASS_A_INTERVAL_UNPROVEN", "ISSUER_SHARES_SCOPE_UNRESOLVED"]),
        ("TWTR", {"TWTR"}, ["LAST_OBSERVED_NOT_VERIFIED_LAST_TRADE", "EXIT_PAYOFF_PIT_UNVERIFIED"]),
        (
            "JATT_ZURA",
            {"JATT", "ZURA", "JATT,ZURA"},
            ["TICKER_REUSE_CONFIRMED_POINTS", "NO_AUTOMATIC_EPOCH_OR_ALIAS_JOIN"],
        ),
    ):
        items = []
        for artifact in (alpaca, massive):
            items.extend(
                {
                    k: o[k]
                    for k in (
                        "probe",
                        "http_status",
                        "classification",
                        "records",
                        "last_observed_session",
                        "body_sha256",
                        "observed_at_utc",
                    )
                    if k in o
                }
                for o in artifact["observations"]
                if o["probe"]["symbol"] in labels
            )
        cases[symbol] = {
            "status": "BLOCKED",
            "blockers": reasons,
            "observations": items,
            "lifecycle_index": {
                label: d6["issuer_filing_inventories"][label]["lifecycle_index_events"]
                for label in sorted(labels)
                if label in d6["issuer_filing_inventories"]
            },
        }
    cases["AAPL"]["adjustment_ratios"] = alpaca["aapl_split_comparison"]
    return {
        "status": "BLOCKED",
        "qualified_intervals": [],
        "fixed_symbol_points": {
            s: {
                k: candidates[s].get(k)
                for k in ("research_cik", "dated_security_points", "historical_security_id")
            }
            for s in SYMBOLS
            if s in candidates
        },
        "benchmark_security_interval": "UNVERIFIED",
        "use_of_current_points": "planning/evidence only; no historic interval promotion",
        "cases": cases,
    }


def provider_report(alpaca, massive, d7):
    witnesses = []
    for tagged in (alpaca, massive):
        witnesses.extend(
            {
                k: r[k]
                for k in (
                    "probe",
                    "http_status",
                    "classification",
                    "rows_usable",
                    "decoded_body_bytes",
                    "body_sha256",
                    "observed_at_utc",
                )
                if k in r
            }
            for r in tagged["observations"]
        )
    return {
        "status": "PARTIAL",
        "sources": d7["documentation"],
        "account_attestations": [alpaca["account_probe"], massive["account_probe"]],
        "properties": {
            "free_tier": ["DOCUMENTED_BASIC", "ACCOUNT_ENTITLEMENT_UNVERIFIED"],
            "sip_daily_history": ["DOCUMENTED_SINCE_2016_DELAYED", "SAMPLED_DATES_ONLY"],
            "iex_daily_history": ["DOCUMENTED", "SPY_2016_2018_2019_EMPTY_SAMPLE"],
            "adjustments": ["RAW_SPLIT_ALL_DOCUMENTED", "AAPL_OVERLAP_CONFIRMED"],
            "actions": ["DOCUMENTED", "AAPL_TWTR_JATT_OBSERVED", "PIT_PUBLICATION_UNVERIFIED"],
            "inactive": [
                "ASSET_LOOKUP_DOCUMENTED",
                "TWTR_INACTIVE_OBSERVED",
                "COVERAGE_UNVERIFIED",
            ],
            "symbol_changes": ["ASOF_DOCUMENTED", "JATT_ZURA_ACTION_OBSERVED", "LINK_UNVERIFIED"],
            "rate_limit": ["200_PER_MINUTE_DOCUMENTED", "NOT_TESTED"],
            "persistent_storage": ["LICENCE_FOR_INTENDED_RETENTION_UNVERIFIED", "BLOCKED"],
        },
        "witnesses": witnesses,
    }


def publish(output, files):
    output.mkdir()
    for name, value in files.items():
        path = output / f"{output.name}_{name}"
        if name.endswith(".csv"):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(value[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(value)
            raw = stream.getvalue().encode()
        elif name.endswith(".md"):
            raw = value.encode()
        else:
            raw = (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()
        with path.open("xb") as stream:
            stream.write(raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    out = ap.parse_args().output.absolute()
    if out.parent != RESEARCH or out.exists():
        ap.error("New output directory under G:/TraWalpResearch/import-artifacts required")
    git = ["git", "-c", f"safe.directory={ROOT.as_posix()}"]
    if subprocess.check_output([*git, "branch", "--show-current"], cwd=ROOT, text=True).strip() != (
        "SEC-integration"
    ):
        ap.error("SEC-integration required")
    d6, d6_id = load(D6)
    alpaca, alpaca_id = load(ALPACA)
    massive, massive_id = load(MASSIVE)
    d7, d7_id = load(D7)
    dates = calendar()
    stores, bindings = read_stores(
        {
            "legacy": ROOT / "data/trading_system.sqlite3",
            "u0": BASELINE / "snapshot.sqlite3",
        },
        dates["WARMUP"],
    )
    stores.update(probe_series(alpaca["observations"]))
    source_ids = {
        "legacy": bindings["legacy"],
        "u0": bindings["u0"],
        "probe": alpaca_id,
        "forward": dict(path=bindings["legacy"]["path"]),
    }
    identities = {r["symbol"]: r["research_cik"] for r in d6["candidates"]}
    grid, holes = build_matrix(dates, stores, identities, source_ids)
    plan = acquisition(dates, stores, alpaca, d7)
    eras = {"2016-2019": range(2016, 2020), "2020-2026": range(2020, 2027)}
    comparison = {}
    legacy_d6 = d6["local_books"]["legacy_symbol_store"]
    for source, feed, adj, prior in (
        ("legacy", "unknown", "unknown", legacy_d6["daily"]["SPY"]),
        ("forward", "sip", "all", legacy_d6["forward_alpaca_sip_all_daily"]["SPY"]),
    ):
        for era, years in eras.items():
            expected = prior[era]["usable_xnys_sessions"]
            current = sum(
                r["usable_sessions"]
                for r in grid
                if r["symbol"] == "SPY"
                and r["source"] == source
                and r["feed"] == feed
                and r["adjustment"] == adj
                and r["year"] in {str(y) for y in years}
            )
            comparison[f"{source}_{era}"] = {
                "prior": expected,
                "current": current,
                "matches": expected == current,
            }
    readiness = {
        "benchmark": ["PARTIAL", "RAW_SAMPLE_ONLY_AND_SIP_ALL_FORWARD_ONLY"],
        "continuity": ["PARTIAL", "GAPS_IN_EACH_LOCALLY_OBSERVED_VARIANT"],
        "warmup": ["MISSING", "PRE_2016_SIP_NO_LOCAL_OBSERVATIONS"],
        "feed": ["PARTIAL", "FORWARD_AND_PROBE_BOUND_LEGACY_INDETERMINATE"],
        "adjustment": ["PARTIAL", "FORWARD_ALL_AAPL_SPLIT_SAMPLE_ONLY"],
        "corporate_actions": ["PARTIAL", "POINT_OBSERVATIONS_WITHOUT_FULL_PIT_TIMELINE"],
        "security": ["BLOCKED", "NO_VERIFIED_CONTIGUOUS_INTERVALS"],
        "persistent_storage": ["UNVERIFIED", "FREE_ACCOUNT_TERMS_NOT_ATTESTED"],
        "pit": ["BLOCKED", "MARKET_SECURITY_AND_ACTION_AVAILABILITY_UNPROVEN"],
        "historical_trading": ["BLOCKED", "WARMUP_AND_IDENTITY_AND_PROVENANCE_GAPS"],
    }
    summary = {
        "status": "HISTORICAL_SIP_COVERAGE_PARTIAL_TRADING_BLOCKED",
        "scope": [START, END],
        "controls": SYMBOLS,
        "readiness": readiness,
        "evidence": [d6_id, alpaca_id, massive_id, d7_id],
        "local_snapshots": bindings,
        "extractor_sha256": sha(Path(__file__).read_bytes()),
        "branch": "SEC-integration",
        "head": subprocess.check_output([*git, "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "calendar": "exchange_calendars XNYS, America/New_York, regular exchange sessions",
        "calendar_package_version": version("exchange-calendars"),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "reference_sessions_by_period": {k: len(v) for k, v in dates.items()},
        "warmup": {
            "first_preceding_session": dates["WARMUP"][0],
            "last_preceding_session": dates["WARMUP"][-1],
            "required": 300,
        },
        "d6_spy_comparisons": comparison,
        "d6_archived_forward_groups": legacy_d6["forward_market_observations"],
        "same_legacy_file_metadata_as_d6": bindings["legacy"].get("file_state")
        == legacy_d6["file_state"],
        "matrix_rows": len(grid),
        "gap_runs": len(holes),
        "network_requests": 0,
        "qualified_security_epochs": 0,
    }
    audit = (ROOT / "docs/f01_alpaca_historical_sip_coverage.md").read_text(encoding="utf-8-sig")
    estimates = plan["request_estimate"]
    space = plan["capacity_estimate"]
    audit += (
        "\n## Gemessener Lauf und Schätzung\n\n"
        f"- Git HEAD `{summary['head']}`. Matrixzeilen {len(grid)}, Lückenblöcke {len(holes)}.\n"
        f"- Initiale Seiten: {estimates['initial_pages']}. "
        f"Planwert mit zwei Seiten: {estimates['planned_budget_at_two_pages']}. "
        f"Erste Seiten bei 30/min: {estimates['paced_at_30_per_min_estimated_minutes']} min.\n"
        f"- Geschätzte kompakte Dateien und Manifeste: "
        f"{space['estimated_retained_compact_bytes']:,} Bytes. "
        f"Mit Transfer, Prüfbereich und einer Kopie: "
        f"{space['estimated_with_transfer_validation_and_one_backup_bytes']:,} Bytes.\n"
        "- Eingangsbindungen, Abweichungen zu D.6 und verwendete Kalender-Version stehen "
        f"in [{out.name}_summary.json]({out.name}_summary.json).\n"
    )
    publish(
        out,
        {
            "coverage.csv": grid,
            "gaps.csv": holes,
            "security_blockers.json": security_evidence(d6, alpaca, massive),
            "provider_qualification.json": provider_report(alpaca, massive, d7),
            "batch_plan.json": plan,
            "audit.md": audit,
            "summary.json": summary,
        },
    )
    print(summary["status"], out)


if __name__ == "__main__":
    main()
