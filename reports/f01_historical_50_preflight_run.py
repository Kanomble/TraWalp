"""Read-only, offline historical market and 50-issuer planning preflight."""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import import_f01_sec_bulk_pilot as pilot  # noqa: E402
import prepare_f01_accounting_handoff as handoff  # noqa: E402

from trading_system.config import load_strategy_config  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.market_sessions import (  # noqa: E402
    required_daily_warmup_sessions,
    trading_sessions_between,
)

REPORTS = REPO / "reports"
BASELINE = REPORTS / "baselines/f_u0_reconstructed_2022-01-03_2026-08-12_68f7ecfc1e78a582"
EPOCHS = BASELINE / "pit_security_type/versions/multi_source_backfill_v3"
PREFIX = "f_2022_historical_universe_audit_v1"
PERIODS = {
    "2016-2019": (date(2016, 1, 1), date(2019, 12, 31)),
    "2020-2026": (date(2020, 1, 1), date(2026, 11, 20)),
}
PIT_SHA = "7ec50b15c5a1827e9fc288d7c5f773beb324bad2a70ad16178390393591b75ac"
NY = ZoneInfo("America/New_York")
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
STANDARD = (
    "ABT",
    "AMD",
    "ALK",
    "AMT",
    "BWA",
    "CLX",
    "CVX",
    "DE",
    "ED",
    "EXC",
    "FDX",
    "GIS",
    "HD",
    "USB",
    "KR",
    "NEE",
    "NKE",
    "ORCL",
    "PG",
    "VZ",
    "TXN",
    "WMT",
)
OUTSIDE = ["QRVO", "DOMO", "ALOT", "NCSM", "WBD"]
SPECIAL = {
    "GOOGL": ("0001652044", "SHARE_CLASSES", ("GOOG",)),
    "FOXA": ("0001754301", "SHARE_CLASSES", ("FOX",)),
    "HEI": ("0000046619", "SHARE_CLASSES", ("HEI.A", "HEI-A")),
    "DSAC": ("0001823466", "TICKER_REUSE", ("NOTE",)),
    "JATT": ("0001855644", "TICKER_REUSE", ("ZURA",)),
    "BG": ("0001144519", "REPORTING_ENTITY_TRANSITION", ()),
}
FORMER = {
    "SAIL": "0001627857",
    "BBBY": "0000886158",
    "TWTR": "0001418091",
    "ATVI": "0000718877",
    "SGEN": "0001060736",
    "SPLK": "0001353283",
    "PXD": "0001038357",
    "HES": "0000004447",
    "ANSS": "0001013462",
    "CTXS": "0000877890",
}
require = pilot.require
bulk = pilot.bulk


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def market_paths(path):
    return [path.with_name(path.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")]


def path_state(path):
    return {
        name.name: [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        for name in market_paths(path)
        if bulk.exists(name)
        for stat in (bulk.plain(name),)
    }


def annual_symbol_bars(db):
    annual = {
        str(year): {"rows": 0, "symbols": 0, "first": None, "last": None}
        for year in range(2016, 2027)
    }
    # Seek to each distinct leading index key without reading large OHLCV table pages.
    symbols = db.execute(
        """WITH RECURSIVE labels(symbol) AS (
               SELECT MIN(symbol) FROM bars
               UNION ALL
               SELECT (SELECT MIN(b.symbol) FROM bars b WHERE b.symbol > labels.symbol)
               FROM labels WHERE labels.symbol IS NOT NULL
           ) SELECT symbol FROM labels WHERE symbol IS NOT NULL"""
    )
    for (symbol,) in symbols:
        for year, count, first, last in db.execute(
            """SELECT SUBSTR(timestamp, 1, 4), COUNT(*), MIN(timestamp), MAX(timestamp)
               FROM bars INDEXED BY ix_bars_symbol_timeframe_timestamp
               WHERE symbol=? AND timeframe='1d' AND timestamp>=? AND timestamp<?
               GROUP BY SUBSTR(timestamp, 1, 4)""",
            (symbol, "2016-01-01", "2026-11-21"),
        ):
            row = annual[year]
            row["rows"] += count
            row["symbols"] += 1
            row["first"] = min(row["first"] or first, first)
            row["last"] = max(row["last"] or last, last)
    return annual


def valid_bar(row):
    try:
        opening, high, low, close, volume = (Decimal(str(value)) for value in row[1:])
    except InvalidOperation:
        return False
    return (
        all(item.is_finite() for item in (opening, high, low, close, volume))
        and low > 0
        and low <= min(opening, close) <= max(opening, close) <= high
        and volume > 0
    )


def bar_projection(rows):
    seen, valid = set(), set()
    invalid = 0
    for row in rows:
        stamp = datetime.fromisoformat(row[0])
        require(stamp.utcoffset() is not None, "UNZONED_DAILY_TIMESTAMP")
        day = stamp.astimezone(NY).date()
        seen.add(day)
        if valid_bar(row):
            valid.add(day)
        else:
            invalid += 1
    return seen, valid, invalid


def preceding_sessions(start, count):
    candidates = trading_sessions_between(
        start - timedelta(days=count * 2), start - timedelta(days=1)
    )
    require(len(candidates) >= count, "INSUFFICIENT_REFERENCE_CALENDAR_HISTORY")
    return candidates[-count:]


def bar_coverage(rows, calendars):
    seen, valid, _ = bar_projection(rows)
    coverage = {}
    for era, (sessions, warmup) in calendars.items():
        start, end = PERIODS[era]
        selected = {item for item in seen if start <= item <= end}
        useful = valid & sessions
        first_usable, last_usable = min(useful, default=None), max(useful, default=None)
        coverage[era] = {
            "observed_dates": len(selected),
            "usable_xnys_sessions": len(useful),
            "xnys_reference_sessions": len(sessions),
            "dates_with_only_invalid_bars_on_xnys": len((selected & sessions) - valid),
            "first_date": min(selected, default=None),
            "last_date": max(selected, default=None),
            "warmup_usable_sessions": len(valid & warmup),
            "warmup_expected": len(warmup),
            "early_gap_sessions": sum(d < first_usable for d in sessions) if useful else None,
            "internal_gap_sessions": sum(
                first_usable <= d <= last_usable for d in sessions - useful
            )
            if useful
            else None,
            "trailing_gap_sessions": sum(d > last_usable for d in sessions) if useful else None,
        }
    return coverage


def market_inventory(path, symbols, warmups):
    before = path_state(path)
    require(
        path.name in before
        and not bulk.exists(path.with_name(path.name + "-journal"))
        and before.get(path.name + "-wal", (0, 0, 0))[2] == 0,
        f"IMMUTABLE_CENSUS_REQUIRES_NO_UNCHECKPOINTED_DATA: {path}",
    )
    with ExitStack() as stack:
        pins = stack.enter_context(pinned_files({"research": path}))
        require(
            os.path.samestat(os.fstat(pins["research"].fileno()), bulk.plain(path)),
            "MARKET_SOURCE_FILE_ID_DRIFT",
        )
        require(path_state(path) == before, "MARKET_SOURCE_CHANGED_BEFORE_SNAPSHOT")
        db = stack.enter_context(
            closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True))
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        print(f"Reading indexed Daily coverage: {path}", file=sys.stderr, flush=True)
        annual = annual_symbol_bars(db)
        companies, coverage, sip_coverage = {}, {}, {}
        calendars = {
            era: (set(trading_sessions_between(start, end)), set(warmups[era]))
            for era, (start, end) in PERIODS.items()
        }
        earliest = min(warmup[0] for warmup in warmups.values()).isoformat()
        for symbol in sorted(symbols):
            company = db.execute(
                "SELECT cik,name,sic FROM companies WHERE symbol=?", (symbol,)
            ).fetchone()
            companies[symbol] = dict(company) if company else None
            bars = db.execute(
                """SELECT timestamp,open,high,low,close,volume FROM bars
                    WHERE symbol=? AND timeframe='1d' AND timestamp>=? AND timestamp<?""",
                (symbol, earliest, "2026-11-21"),
            )
            coverage[symbol] = bar_coverage(bars, calendars)
            if "forward_market_observations" in names:
                sip = db.execute(
                    """SELECT timestamp,open,high,low,close,volume FROM forward_market_observations
                       WHERE symbol=? AND session>=? AND session<? AND timeframe='1d'
                       AND provider='alpaca' AND feed='sip' AND adjustment='all'""",
                    (symbol, earliest, "2026-11-21"),
                )
                sip_coverage[symbol] = bar_coverage(sip, calendars)
        counts = {
            name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            if name in names
            else None
            for name in (
                "symbol_identity_epochs",
                "security_corporate_actions",
                "security_reference",
            )
        }
        actions = []
        if "security_corporate_actions" in names:
            actions = [dict(row) for row in db.execute("SELECT * FROM security_corporate_actions")]
        forward = []
        if "forward_market_observations" in names:
            forward = [
                dict(row)
                for row in db.execute(
                    """SELECT provider,feed,adjustment,timeframe,COUNT(*) AS rows,
                          COUNT(DISTINCT symbol) AS symbols,MIN(timestamp) AS first,
                          MAX(timestamp) AS last
                     FROM forward_market_observations GROUP BY provider,feed,adjustment,timeframe"""
                )
            ]
        require(path_state(path) == before, f"MARKET_SOURCE_CHANGED_DURING_CENSUS: {path}")
    return {
        "source": str(path),
        "file_state": before,
        "annual_raw_daily_bars": annual,
        "company_snapshot": companies,
        "daily": coverage,
        "reference_table_counts": counts,
        "stored_corporate_actions": actions,
        "forward_market_observations": forward,
        "forward_alpaca_sip_all_daily": sip_coverage,
    }


def candidate_plan(pit, reference_symbols):
    dated_current = {
        row["symbol"]: row
        for row in csv_rows(REPORTS / f"{PREFIX}_current_identity_review.csv")
        if row["first_2022_bar"]
    }
    dated_outside = {
        row["symbol"]: row for row in csv_rows(REPORTS / f"{PREFIX}_symbol_manifest.csv")
    }
    epoch_points = defaultdict(list)
    for row in csv_rows(EPOCHS / "epochs.csv"):
        if row["historical_status"] == "PROVEN_POINT":
            epoch_points[row["symbol"]].append(row)
    snapshot = {row["company"]["symbol"]: row["company"] for row in reference_symbols}
    rows = []

    def add(symbol, cik, category, aliases=()):
        require(re.fullmatch(r"[0-9]{10}", cik), f"MISSING_CANDIDATE_CIK: {symbol}")
        historical = (dated_outside | dated_current).get(symbol)
        points = [
            {
                k: item[k]
                for k in (
                    "valid_from",
                    "valid_to",
                    "identity_id",
                    "security_type",
                    "primary_source",
                    "evidence_hash",
                    "epoch_id",
                )
            }
            for item in epoch_points[symbol]
            if item["identity_id"] == cik
        ]
        rows.append(
            {
                "symbol": symbol,
                "research_cik": cik,
                "category": category,
                "alternative_labels_for_census_only": aliases,
                "dated_security_points": points,
                "observed_2022_candidate": (
                    {
                        k: historical[k]
                        for k in (
                            "first_2022_bar",
                            "last_2022_bar",
                            "daily_bars_2022",
                            "current_source_member",
                            "historical_cik",
                            "local_cik",
                            "identity_class",
                            "evidence_strength",
                        )
                    }
                    if historical
                    else None
                ),
            }
        )

    for item in pit["fixed_pilot"]:
        add(item["symbol"], item["cik"], "E11_CONTROL")
    for symbol in STANDARD:
        add(symbol, snapshot[symbol]["cik"], "DATED_MARKET_AND_ISSUER_CANDIDATE")
    for symbol in OUTSIDE:
        require(symbol in dated_outside, f"UNDATED_OUTSIDE_CASE: {symbol}")
        add(symbol, dated_outside[symbol]["local_cik"], "OUTSIDE_CURRENT_SOURCE_2022")
    for symbol, (cik, category, aliases) in SPECIAL.items():
        add(symbol, cik, category, aliases)
    for symbol, cik in FORMER.items():
        add(symbol, cik, "FORMER_REGISTRANT_LIFECYCLE")
    require(len(rows) == len({row["research_cik"] for row in rows}) == 50, "PILOT_SCOPE_NOT_50")
    return rows


def public_float(payload, indexed):
    units = payload.get("facts", {}).get("dei", {}).get("EntityPublicFloat", {}).get("units", {})
    candidates = defaultdict(list)
    for item in units.get("USD", []):
        if (
            item.get("accn") not in indexed
            or item.get("form") != "10-K"
            or not ("2021-01-01" <= item.get("end", "") <= item.get("filed", "") <= "2022-12-31")
        ):
            continue
        filed, form, observed = indexed[item["accn"]]
        if observed is None or filed.isoformat() != item["filed"] or form != item["form"]:
            continue
        try:
            value = Decimal(str(item["val"]))
        except (InvalidOperation, KeyError):
            continue
        if value.is_finite() and value > 0:
            candidates[(item["end"], item["filed"])].append((value, item["accn"], observed))
    if not candidates:
        return None
    period, filed = max(candidates)
    observations = set(candidates[period, filed])
    if len(observations) != 1:
        return None
    value, accession, acceptance = next(iter(observations))
    return {
        "reported_public_float_usd": value,
        "period": period,
        "filed": filed,
        "accession": accession,
        "acceptance": acceptance,
        "planning_size_bucket": "UNDER_2B"
        if value < 2_000_000_000
        else "2B_TO_10B"
        if value < 10_000_000_000
        else "AT_LEAST_10B",
    }


def filing_scope(candidates, paths, exclusions, manifest, era_warmups):
    archived = {(item["cik"], item["accession"]): item for item in manifest["filings"]}
    index_inputs = [
        {"cik": row["research_cik"], "submissions_member": f"CIK{row['research_cik']}.json"}
        for row in candidates
    ]
    submissions = pilot.submissions_members(paths, index_inputs, exclusions["submissions"])
    result = {}
    with ZipFile(paths["companyfacts"]) as z:
        members = set(z.namelist())
        for row in candidates:
            symbol, cik = row["symbol"], row["research_cik"]
            main, primary_sha, historical = submissions.pop(cik)
            filed, conflicts, malformed = pilot.filing_index(main, historical)
            source_name = f"CIK{cik}.json"
            provider_float, cf_sha = None, None
            if source_name not in members:
                fact_state = "COMPANYFACTS_MEMBER_MISSING"
            elif source_name in exclusions["companyfacts"]:
                fact_state = "COMPANYFACTS_MEMBER_QUARANTINED"
            else:
                payload, cf_sha = pilot.zip_member(
                    z,
                    source_name,
                    "companyfacts",
                    exclusions["companyfacts"],
                )
                require(pilot.provider_cik(payload) == cik, f"COMPANYFACTS_CIK_DRIFT: {symbol}")
                provider_float = public_float(payload, filed)
                fact_state = "LOCAL_PROVIDER_SNAPSHOT_PRESENT"
            relevant = {
                accession: {"filed": day, "form": form, "acceptance": stamp}
                for accession, (day, form, stamp) in sorted(filed.items())
                if min(era_warmups.values()) <= day <= PERIODS["2020-2026"][1]
                and (form in FORMS or form in {"25", "25-NSE", "15-12G", "15-12B", "15-15D"})
            }
            counts = {}
            for era, minimum in era_warmups.items():
                stop = PERIODS[era][1]
                required = [
                    accession
                    for accession, rec in relevant.items()
                    if minimum <= rec["filed"] <= stop and rec["form"] in FORMS
                ]
                counts[era] = {
                    "periodic_filing_candidates": len(required),
                    "original_shard_keys_in_manifest": sum(
                        (cik, acc) in archived for acc in required
                    ),
                    "missing_original_shard_keys": [
                        acc for acc in required if (cik, acc) not in archived
                    ],
                    "without_bound_submissions_acceptance": sum(
                        relevant[acc]["acceptance"] is None for acc in required
                    ),
                }
            lifecycle = [
                rec | {"accession": accession}
                for accession, rec in relevant.items()
                if rec["form"].startswith(("15-", "25"))
            ]
            if row["category"] == "FORMER_REGISTRANT_LIFECYCLE":
                require(
                    not main.get("tickers")
                    and any(item["form"] == "25-NSE" for item in lifecycle)
                    and any(item["form"].startswith("15-") for item in lifecycle),
                    f"FORMER_REGISTRANT_EVIDENCE_MISSING: {symbol}",
                )
            result[symbol] = {
                "cik": cik,
                "registrant_in_archived_submissions": main["name"],
                "sic_descriptive_snapshot_only": main.get("sic"),
                "sic_description_descriptive_only": main.get("sicDescription"),
                "fiscal_year_end_snapshot_mmdd": main.get("fiscalYearEnd"),
                "snapshot_tickers": main.get("tickers"),
                "companyfacts_availability": fact_state,
                "companyfacts_member_sha256": cf_sha,
                "size_observation": provider_float,
                "submissions_member_sha256": primary_sha,
                "submissions_history_member_sha256": {
                    item["member"]: item["sha256"] for item in historical
                },
                "filing_index_conflicts": sorted(conflicts),
                "malformed_filing_indexes": malformed,
                "filings": relevant,
                "period_counts": counts,
                "lifecycle_index_events": lifecycle,
            }
    return result


def qualify_candidate(row, filings, market, snapshots):
    symbol, cik = row["symbol"], row["research_cik"]
    company = market["company_snapshot"].get(symbol)
    category = row["category"]
    coverage = market["daily"][symbol]
    reasons = [
        "VERIFIED_SECURITY_INTERVAL_AND_TRADABILITY_REQUIRED",
        "PRICE_EPS_SHARES_ADJUSTMENT_UNVERIFIED",
    ]
    if company is None:
        reasons.append("NO_LOCAL_COMPANY_SYMBOL_MAPPING")
    elif company["cik"] != cik:
        reasons.append("LOCAL_SYMBOL_POINTS_TO_DIFFERENT_CIK")
    if not any(r["observed_dates"] for r in coverage.values()):
        reasons.append("NO_LOCAL_DAILY_BARS_FOR_PLANNING_SYMBOL")
    if category == "FORMER_REGISTRANT_LIFECYCLE":
        reasons.append("FORM_25_AND_15_FILED_SECURITY_AND_EFFECTIVE_EXIT_REQUIRE_REVIEW")
    if category == "OUTSIDE_CURRENT_SOURCE_2022":
        reasons.append("OUTSIDE_CURRENT_SOURCE_FLAG_IS_NOT_DELISTING_PROOF")
    if category in {"TICKER_REUSE", "REPORTING_ENTITY_TRANSITION", "SHARE_CLASSES"}:
        reasons.append(category + "_SECURITY_LEVEL_EPOCH_REQUIRED")
    if filings["companyfacts_availability"] != "LOCAL_PROVIDER_SNAPSHOT_PRESENT":
        reasons.append(filings["companyfacts_availability"])
    if filings["period_counts"]["2020-2026"]["missing_original_shard_keys"]:
        reasons.append("PERIODIC_ORIGINAL_SHARD_GAPS")
    observation = snapshots["daily"][symbol]["2020-2026"]
    dated = []
    if row["observed_2022_candidate"]:
        dated.append({"source": "2022_HISTORICAL_UNIVERSE_AUDIT", **row["observed_2022_candidate"]})
    if row["dated_security_points"]:
        dated.append({"source": "FROZEN_V3_PROVEN_POINT", "points": row["dated_security_points"]})
    if category in {"FORMER_REGISTRANT_LIFECYCLE", "REPORTING_ENTITY_TRANSITION", "TICKER_REUSE"}:
        dated.append(
            {
                "source": "ARCHIVED_SEC_SUBMISSIONS_FORM_INDEX",
                "events": filings["lifecycle_index_events"],
            }
        )
    if category == "E11_CONTROL":
        dated.append({"source": "BOUND_E11_E13_PIT_CONTROL"})
    if observation["first_date"]:
        dated.append(
            {
                "source": "DATED_U0_DAILY_SNAPSHOT_SYMBOL_ONLY",
                "first_date": observation["first_date"],
                "last_date": observation["last_date"],
            }
        )
    elif coverage["2020-2026"]["first_date"]:
        dated.append(
            {
                "source": "DATED_LEGACY_DAILY_STORE_SYMBOL_ONLY",
                "first_date": coverage["2020-2026"]["first_date"],
                "last_date": coverage["2020-2026"]["last_date"],
            }
        )
    require(dated, f"MISSING_DATED_COHORT_WITNESS: {symbol}")
    return row | {
        "dated_selection_evidence": dated,
        "historical_security_id": None,
        "local_current_mapping": company,
        "status": "HISTORICAL_TRADING_NOT_QUALIFIED",
        "reasons": reasons,
    }


def capacity_plan(filings, root, manifests):
    missing = {
        (row["cik"], acc)
        for row in filings.values()
        for era in row["period_counts"].values()
        for acc in era["missing_original_shard_keys"]
    }
    samples_count = len(manifests[0]["filings"])
    raw_bytes = 2_698_898_082  # E.4 measured CIK cache including source metadata
    evidence_bytes = sum(entry["bytes"] for m in manifests for entry in m["filings"])
    # Fixed comparison based on the previous seven issuers; no extrapolation of their dossiers.
    originals_projection = (raw_bytes + evidence_bytes) * len(missing) // samples_count
    db_request_projection = (16_789_504 + 50_508_110) * 50 // 7
    reserve, staging = 20 * 1024**3, 2 * 1024**3
    extra_working = originals_projection + db_request_projection + staging + reserve
    return {
        "estimate_status": "PILOT_MEAN_SCENARIO_NOT_CAPACITY_APPROVAL",
        "missing_distinct_periodic_accessions": len(missing),
        "reference_filing_count": samples_count,
        "reference_original_cache_bytes": raw_bytes,
        "reference_shard_bytes": evidence_bytes,
        "projected_additional_original_and_evidence_bytes": originals_projection,
        "projected_new_database_and_request_archive_bytes": db_request_projection,
        "temporary_workspace_planning_allowance_bytes": staging,
        "existing_safety_reserve_bytes": reserve,
        "projected_working_bytes_including_reserve": extra_working,
        "projected_with_one_new_copy_backup_on_same_volume_bytes": extra_working
        + originals_projection
        + db_request_projection,
        "current_g_drive_free_bytes": shutil.disk_usage(root).free,
        "recommended_start_filing_batch_count": 20,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot-list", type=Path, required=True)
    args = parser.parse_args()
    out, csv_out = args.output.absolute(), args.pilot_list.absolute()
    require(out != csv_out, "DISTINCT_OUTPUTS_REQUIRED")
    for target in (out, csv_out):
        bulk.directory(target.parent)
        require(not bulk.exists(target), "NEW_OUTPUTS_REQUIRED")
    code = pilot.code_binding()
    pit = handoff.read_bound(REPORTS / "f01_historical_pit_coverage.json", PIT_SHA)
    require(
        pit["bindings"]["reader_candidate"]["fingerprint"] == code["fingerprint"],
        "APPROVED_READER_CODE_FINGERPRINT_DRIFT",
    )
    paths_for_bindings = (
        REPORTS / f"{PREFIX}_symbol_manifest.csv",
        REPORTS / f"{PREFIX}_current_identity_review.csv",
        REPORTS / f"{PREFIX}_identity_evidence.json",
        EPOCHS / "epochs.csv",
        EPOCHS / "manifest.json",
        BASELINE / "scope.json",
        BASELINE / "snapshot_manifest.json",
        BASELINE / "run_a/universe_snapshot.json",
    )
    hashes = {str(path.relative_to(REPO)): pilot.file_sha(path) for path in paths_for_bindings}
    snapshot_symbols = json.loads(paths_for_bindings[-1].read_bytes())
    candidates = candidate_plan(pit, snapshot_symbols)
    config = load_strategy_config(REPO / "config" / "strategy.yaml")
    warmup_bars = required_daily_warmup_sessions(config)
    warmups = {era: preceding_sessions(start, warmup_bars) for era, (start, _) in PERIODS.items()}
    # Copy the existing HistoricalFeatureScreenSource planning look-back, without changing it.
    issuer_warmups = {era: start - timedelta(days=4 * 366) for era, (start, _) in PERIODS.items()}
    symbols = (
        {row["symbol"] for row in candidates}
        | {"SPY"}
        | {label for row in candidates for label in row["alternative_labels_for_census_only"]}
    )
    print(
        "Reading local daily market coverage and dated symbol observations",
        file=sys.stderr,
        flush=True,
    )
    market = market_inventory(REPO / "data/trading_system.sqlite3", symbols, warmups)
    snapshot = market_inventory(BASELINE / "snapshot.sqlite3", symbols, warmups)
    snapshot_manifest = json.loads((BASELINE / "snapshot_manifest.json").read_bytes())
    require(
        snapshot["file_state"]["snapshot.sqlite3"][2] == snapshot_manifest["snapshot_size"],
        "U0_SNAPSHOT_SIZE_MISMATCH",
    )
    research = pilot.RESEARCH_ROOT
    layout = bulk.Layout.load(research / "import-artifacts/f01-sec-bulk-layout-v1.json")
    _, source_paths, excluded = pilot.source_inputs(layout, pilot.pilot_spec())
    filing_root = research / "sec-originals/filing-evidence"
    bundle = f"f01-extended-filing-evidence-{handoff.RUN}"
    filing_manifest = handoff.read_bound(filing_root / bundle / "manifest.json", handoff.FILING_SHA)
    accounting_bundle = f"f01-extended-accounting-evidence-{handoff.RUN}"
    accounting_manifest = handoff.read_bound(
        research / "import-artifacts" / accounting_bundle / "manifest.json",
        handoff.ACCOUNTING_SHA,
    )
    print(
        "Reading bounded SEC ZIP members and checking historical filing inventory",
        file=sys.stderr,
        flush=True,
    )
    with pinned_files(source_paths):
        filings = filing_scope(candidates, source_paths, excluded, filing_manifest, issuer_warmups)
    candidates = [
        qualify_candidate(row, filings[row["symbol"]], market, snapshot) for row in candidates
    ]
    epoch = json.loads((EPOCHS / "manifest.json").read_bytes())
    require(
        all(
            pilot.file_sha(path) == hashes[str(path.relative_to(REPO))]
            for path in paths_for_bindings
        )
        and pilot.code_binding() == code,
        "PREFLIGHT_SOURCE_ARTIFACT_OR_CODE_DRIFT",
    )
    data = {
        "contract": "F01_HISTORICAL_SECURITY_50_ISSUER_PREFLIGHT_V1",
        "status": "PILOT_50_PLANNED_HISTORICAL_TRADING_BLOCKED",
        "candidate": code,
        "periods": PERIODS,
        "bindings": {
            "static_artifacts_sha256": hashes,
            "approved_pit_audit_sha256": PIT_SHA,
            "bulk_manifest_sha256": pilot.SOURCE_SHA,
            "original_filing_manifest_sha256": handoff.FILING_SHA,
            "accounting_manifest_sha256": handoff.ACCOUNTING_SHA,
            "preflight_script_sha256": pilot.file_sha(Path(__file__)),
        },
        "cohort_counts": dict(Counter(row["category"] for row in candidates)),
        "historical_trading_qualified": 0,
        "coverage_semantics": "symbol_dated_bar_presence_only; no security_epoch_or_feed_inference",
        "market_warmup_sessions": warmup_bars,
        "market_warmup_start": {era: sessions[0] for era, sessions in warmups.items()},
        "fundamental_warmup_nominal_lower_bound": issuer_warmups,
        "candidates": candidates,
        "issuer_filing_inventories": filings,
        "local_books": {"legacy_symbol_store": market, "sealed_u0_snapshot": snapshot},
        "e11_market_tables": pit["universe_table_counts"],
        "v3_historical_type_scope": {
            key: epoch[key]
            for key in (
                "final_status",
                "required_identities",
                "required_identity_sessions",
                "covered_identity_sessions",
                "fully_covered_identities",
                "coverage_pct",
            )
        },
        "estimated_capacity": capacity_plan(
            filings, research, (filing_manifest, accounting_manifest)
        ),
    }
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer, lineterminator="\n")
    writer.writerow(
        (
            "symbol_label",
            "planning_cik",
            "category",
            "registrant_from_submissions",
            "sic_snapshot",
            "fiscal_year_end_snapshot",
            "public_float_2022_bucket",
            "historic_security_id",
            "status",
            "blocking_reasons",
        )
    )
    for row in candidates:
        rec = filings[row["symbol"]]
        writer.writerow(
            (
                row["symbol"],
                row["research_cik"],
                row["category"],
                rec["registrant_in_archived_submissions"],
                rec["sic_descriptive_snapshot_only"],
                rec["fiscal_year_end_snapshot_mmdd"],
                (rec["size_observation"] or {}).get("planning_size_bucket", ""),
                row["historical_security_id"],
                row["status"],
                ";".join(row["reasons"]),
            )
        )
    bulk.write_new(
        out, (json.dumps(data, default=str, ensure_ascii=True, indent=2) + "\n").encode()
    )
    bulk.write_new(csv_out, csv_buffer.getvalue().encode("utf-8"))
    print(data["status"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"HISTORICAL_50_PREFLIGHT_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
