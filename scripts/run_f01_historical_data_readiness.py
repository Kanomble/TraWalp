#!/usr/bin/env python3
"""Offline, read-only F01/C1 coverage audit and Task-9.6 planning contract."""

import argparse
import csv
import shutil
import sys
from collections import Counter
from datetime import date
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
sys.path[:0] = [str(ROOT / "src"), str(REPORTS)]

from f01_alpaca_historical_sip_coverage import (  # noqa: E402
    ALPACA,
    EXPECTED_HASHES,
    MASSIVE,
    probe_series,
)
from f01_historical_50_preflight_run import BASELINE  # noqa: E402
from f01_readiness_coverage import (  # noqa: E402
    PIT_BLOCKERS,
    PRIORITY_METRICS,
    filing_gaps,
    fundamental_rows,
    gap_row,
    market_gaps,
    market_proxy,
    market_rows,
    readiness_rows,
    security_rows,
)
from f01_readiness_inventory import (  # noqa: E402
    LEGACY,
    START,
    U0,
    Inputs,
    canonical,
    digest,
    encoding,
    inventory_filings,
    market,
    staging,
)
from f01_readiness_requirements import requirements  # noqa: E402
from f01_research_evidence import repository_evidence  # noqa: E402
from prepare_f01_historical_50_batch import D6_SHA  # noqa: E402

from trading_system.config import load_strategy_config  # noqa: E402
from trading_system.data.canonical_facts import fingerprint  # noqa: E402
from trading_system.data.market_sessions import (  # noqa: E402
    latest_completed_trading_session,
    trading_sessions_between,
)

RESEARCH = Path("G:/TraWalpResearch")
NAME = "f01-task95-historical-data-readiness-v2"
PREVIOUS_NAME = "f01-task95-historical-data-readiness-v1"
D8 = "f01-alpaca-sip-coverage-2016-2026-v1"
CONFIG = ROOT / "config/strategy.yaml"
STAGING_NAME = "f01-companyfacts-staging-historical50-v1"
PERIOD_PRIOR_FUNDAMENTALS = "Fundamental load envelope; verify fiscal periods and share freshness"


def load_controls(inputs, artifacts):
    raw = inputs.read(REPORTS / "f01_historical_50_pilot.csv", D6_SHA[0])
    roster = list(csv.DictReader(raw.decode().splitlines()))
    labels = {r["planning_cik"]: r["symbol_label"] for r in roster}
    d6 = inputs.json(REPORTS / "f01_historical_50_preflight.json", D6_SHA[1])
    cohort = [
        r | {"cik": r["research_cik"], "blocking_reasons": r["reasons"]} for r in d6["candidates"]
    ]
    if (
        len(roster) != 50
        or len(labels) != 50
        or len(set(labels.values())) != 50
        or "SPY" in labels.values()
        or {r["cik"]: r["symbol"] for r in cohort} != labels
    ):
        raise ValueError("D6_COHORT_BINDING_MISMATCH")
    d8 = inputs.json(artifacts / D8 / f"{D8}_security_blockers.json")
    provider = inputs.json(artifacts / D8 / f"{D8}_provider_qualification.json")
    e5 = inputs.json(
        REPORTS / "f01_historical_pit_coverage.json", d6["bindings"]["approved_pit_audit_sha256"]
    )
    return labels, d6, cohort, d8, provider, e5


def other_sources(research, inputs, d6):
    bulk = research / "sec-originals/pilot01-20261121-175840"
    manifest = inputs.json(
        bulk / "f01_sec_bulk_source_manifest.json", d6["bindings"]["bulk_manifest_sha256"]
    )
    archives = {}
    for key, doc in manifest["archives"].items():
        info = inputs.present(bulk / f"{key}.zip")
        archives[key] = info | {
            "manifest_sha256_of_archive": doc.get("sha256"),
            "published_integrity": doc["integrity"]["status"],
            "size_matches_manifest": info.get("bytes") == doc["bytes"],
            "retrieval_ended_at_utc": doc.get("retrieval_ended_at_utc"),
        }
    arelle = research / "arelle/2.47.5"
    original = research / "sec-originals/filing-evidence"
    existing = {
        path.name: {
            key: value
            for key, value in inputs.json(path).items()
            if key in {"status", "contract", "version", "filing_count", "source_generation"}
        }
        for path in sorted(
            (
                *arelle.glob("taxonomy-*.json"),
                *original.glob("f01-extended-cache-*.json"),
                *original.glob("f01-extended-*/manifest.json"),
            )
        )
    }
    return {
        "sec_bulk": archives,
        "arelle_version": arelle.name,
        "arelle_local_cache_present": (arelle / "cache").is_dir(),
        "published_original_and_taxonomy_evidence": existing,
        "d8_provider_seal": "Historical local probe, account and request scope only",
    }


def provider_plan(d8, inputs):
    return {
        "live_verification_performed": False,
        "documented_d8_evidence": d8,
        "current_costs_entitlements_rate_limits_retention_and_licence": "VERIFICATION_REQUIRED",
        "sec_edgar": {
            "endpoint": "/Archives/edgar/data/{CIK}/{accession_digits}/index.json",
            "implementation": [
                "scripts/download_f01_sec_bulk.py::SecHttp",
                "scripts/prepare_f01_filing_evidence.py",
            ],
            "local_client_policy": "Conservative serial pacing; contact User-Agent; Retry-After",
            "cost_and_licence": "VERIFICATION_REQUIRED; document use and retention",
            "resume": "Cache by CIK/accession/document; persist limiter and verified SHA receipts",
        },
        "alpaca": {
            "endpoint": "Stocks Historical bars /v2/stocks/bars; /v1/corporate-actions",
            "earliest_dated_local_product_evidence": "2016; pre-2016 provision unresolved",
            "resume": "Persist token and complete-session checkpoint per feed/adjustment",
            "limits": "VERIFICATION_REQUIRED; SIP entitlement and retention rights",
            "implementation": [
                "src/trading_system/data/alpaca_corporate_actions.py",
                "reports/f01_historical_market_provider_probe.py",
            ],
        },
        "massive": {
            "endpoint": "/v3/reference/tickers/{ticker} with date; split/dividend reference",
            "use": "Dated identity/actions; 2018/2022 historical bar probes returned HTTP 403",
            "resume": "Persist endpoint/date/page and raw evidence digest",
            "limits": "VERIFICATION_REQUIRED; no substitution for Alpaca SIP/all",
            "implementation": ["src/trading_system/data/massive_identity_research.py"],
        },
        "evidence_bindings": {
            path: record
            for path, record in inputs.files.items()
            if path.endswith(("provider_qualification.json", "alpaca.json", "massive.json"))
        },
    }


def storage(d6, sec_count, market_count, research):
    gib = 1024**3
    reference = d6["estimated_capacity"]
    denominator = reference["reference_filing_count"]
    output = {
        "original_cache": round(
            sec_count * reference["reference_original_cache_bytes"] / denominator
        ),
        "unpacked_extraction_shards": round(
            sec_count * reference["reference_shard_bytes"] / denominator
        ),
        "compressed_transport_or_request_spool": round(
            sec_count * reference["reference_original_cache_bytes"] / denominator
        ),
        "daily_bar_transport_and_indices": market_count * 1024,
        "taxonomy_placeholder_pending_dts": gib,
        "new_fundamental_database_and_receipt_allowance": round(
            sec_count
            * reference["projected_new_database_and_request_archive_bytes"]
            / reference["missing_distinct_periodic_accessions"]
        ),
        "temporary_and_sqlite_sidecars": 4 * gib,
        "safety_reserve": 20 * gib,
    }
    capacity = {}
    for drive in (Path(research.anchor), Path("F:/")):
        try:
            total, _, free = shutil.disk_usage(drive)
        except OSError:
            capacity[str(drive)] = {"status": "UNAVAILABLE"}
        else:
            # Whole-GiB conservative floor prevents audit output itself from shifting the snapshot.
            capacity[str(drive)] = {"total_bytes": total, "free_bytes_floor_gib": free // gib * gib}
    return {
        "volumes": capacity,
        "assumptions": output,
        "estimated_working_bytes": sum(output.values()),
        "backup_copy_bytes_scenario": sum(output.values())
        - output["safety_reserve"]
        - output["temporary_and_sqlite_sidecars"],
        "basis": "D.6 339-filing mean; 1 KiB per Daily gap; sidecar and DTS allowances",
        "requires_new_free_space_check": True,
        "f_drive_backup_restore_and_separation_unverified": True,
    }


def acquisition(security, market_blocks, filing_blocks, bindings, stats):
    local = [b for b in filing_blocks if b["category"] == "LOCAL_REUSE_AND_REVIEW"]
    sec = [b for b in filing_blocks if b["category"].startswith(("P1_SEC_", "P2_SEC_"))]
    blocked = [b for b in filing_blocks if b["category"] == "METADATA_OR_AMENDMENT_REVIEW"]
    sections = [
        {"step": 1, "operation": "LOCAL_EVIDENCE_REUSE", "blocks": local},
        {
            "step": 2,
            "operation": "BENCHMARK_DAILY_AND_WARMUP",
            "blocks": [b for b in market_blocks if b["symbol"] == "SPY"],
        },
        {
            "step": 3,
            "operation": "SECURITY_EPOCH_ACTION_AND_EXIT_EVIDENCE",
            "blocks": [
                row
                | {
                    "block_id": fingerprint(row),
                    "provider": "MASSIVE_ALPACA_SEC",
                    "status": "EVIDENCE_AND_ENTITLEMENT_REQUIRED",
                }
                for row in security
                if "UNRESOLVED" in row["status"]
            ],
        },
        {
            "step": 4,
            "operation": "ISSUER_DAILY_AFTER_SECURITY_EPOCHS",
            "blocks": [b for b in market_blocks if b["symbol"] != "SPY"],
        },
        {"step": 5, "operation": "SEC_PERIODIC_ORIGINALS_AFTER_CACHE_RECHECK", "blocks": sec},
        {
            "step": 6,
            "operation": "OUTSTANDING_AMENDMENT_MAPPING_AND_FISCAL_REVIEW",
            "blocks": blocked,
        },
    ]
    for section in sections:
        section["depends_on"] = {
            1: [],
            2: [1],
            3: [1],
            4: [2, 3],
            5: [1],
            6: [1, 5],
        }[section["step"]]
        section["authorization"] = "PLAN_ONLY_SEPARATE_HASH_BOUND_AUTHORIZATION_REQUIRED"
    sections.append(
        {
            "step": 7,
            "operation": "PRE2010_FUNDAMENTALS_AND_FISCAL_GAPS",
            "depends_on": [1, 5],
            "authorization": "DISCOVERY_ONLY",
            "blocks": [
                {
                    "status": "SCOPE_DISCOVERY_REQUIRED",
                    "provider": "LOCAL_SUBMISSIONS_THEN_SEC",
                    "start": "2005-12-29",
                    "end": "2009-12-31",
                    "resume": "Bind dated periods and local accessions before authorization",
                }
            ],
        }
    )
    eligible = sum(len(b["accessions"]) for b in sec)
    pages = sum(b["provider"] == "ALPACA_SIP" for b in market_blocks)
    contract = {
        "contract": "F01_TASK96_ACQUISITION_PLAN_V1",
        "status": "PLAN_ONLY",
        "authorization": {"network": False, "import": False, "trading": False},
        "input_bindings": bindings,
        "sequence": sections,
        "request_estimates": {
            "sec_accessions": eligible,
            "sec_planning_http_requests_low": 2 * eligible,
            "sec_planning_http_requests_high": 35 * eligible,
            "sec_time_hours_at_serial_one_second_pacing_excluding_retries": [
                round(2 * eligible / 3600, 2),
                round(35 * eligible / 3600, 2),
            ],
            "alpaca_single_symbol_reference_windows_one_page_each": pages,
            "alpaca_notes": "Reference-window scenario only. "
            "Clamp to approved epochs; verify pagination, rate and time.",
            "security_reference_and_actions": "VERIFY_THEN_ESTIMATE: dated endpoint and pagination",
        },
        "execution_gates": {
            "shared": "Recheck SHA-256, approval per block, provider entitlements, "
            "cost, rights, disk, cache reuse and explicit symbol resolution.",
            "sec": "Bind current metadata, listing and document set, use SEC throttle, "
            "checkpoint verified document receipts; Arelle and Accounting are separate.",
            "market": "Bind provider/feed/adjustment/epoch and raw page provenance, "
            "page token and validated sessions; retain separate action evidence.",
            "pre_2016": "Source, entitlement and archival rights unresolved.",
        },
        "coverage_counts": dict(stats),
    }
    return contract | {"plan_fingerprint": fingerprint(contract)}


def csv_output(path, rows):
    rows = iter(rows)
    first = next(rows)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, first, lineterminator="\n")
        writer.writeheader()
        writer.writerow(first)
        writer.writerows(rows)


def technical_summary(proxies, sessions, symbols):
    return {
        name: {
            "earliest_any_proxy": next((d for d in sessions if d >= START and count[d]), None),
            "earliest_all_50_proxy": next(
                (d for d in sessions if d >= START and count[d] == len(symbols)), None
            ),
            "maximum_concurrent_labels": max(count.values()),
            "proxy_sessions": sum(n > 0 for n in count.values()),
        }
        for name, count in proxies.items()
    }


def audit(research, target):
    inputs = Inputs()
    artifacts = research / "import-artifacts"
    prior_manifest = artifacts / PREVIOUS_NAME / "manifest.json"
    if prior_manifest.is_file():
        inputs.json(prior_manifest)
    labels, d6, cohort, d8, d8_provider, e5 = load_controls(inputs, artifacts)
    databases = research / "research-databases"
    config = load_strategy_config(CONFIG)
    req = requirements(config)
    for path in {CONFIG, *(ROOT / p for row in req["inputs"] for p in row["consumers"])}:
        inputs.read(path)
    base = inputs.json(BASELINE / "scope.json")
    base_qualification = inputs.json(BASELINE / "run_a/data_qualification.json")
    inputs.json(BASELINE / "snapshot_manifest.json")
    e13 = inputs.json(artifacts / "f01-e13-reader-validation-20261122-211700.json")
    source_inventory = other_sources(research, inputs, d6)
    print("Audit: Task-9.4-Staging und Canonical-Bestände lesen", flush=True)
    published, cells, filings, groups, stage_stats = staging(
        databases / STAGING_NAME, inputs, labels
    )
    lineages, canonical_pairs, canonical_cells = canonical(databases, inputs)
    filing_inventory = inventory_filings(filings, research, inputs)
    filing_ledger, filing_plan, filing_stats = filing_gaps(
        filing_inventory, groups, canonical_pairs
    )
    print("Audit: Daily- und Forward-Serien im gebundenen Scope lesen", flush=True)
    by_symbol = {symbol: cik for cik, symbol in labels.items()} | {"SPY": ""}
    census_labels = {label for row in cohort for label in row["alternative_labels_for_census_only"]}
    candidate_end = latest_completed_trading_session().isoformat()
    first = req["start_2010_warmup"]
    stores, intraday = market(
        {LEGACY: ROOT / "data/trading_system.sqlite3", U0: BASELINE / "snapshot.sqlite3"},
        sorted(set(by_symbol) | census_labels),
        first,
        candidate_end,
        inputs,
    )
    probe_path = artifacts / ALPACA.name
    alpaca = inputs.json(probe_path, EXPECTED_HASHES[ALPACA])
    inputs.json(artifacts / MASSIVE.name, EXPECTED_HASHES[MASSIVE])
    for (_, symbol, feed, adjustment), series in probe_series(alpaca["observations"]).items():
        stores[("probe", "alpaca", feed, adjustment), symbol] = series
    official = [
        d.isoformat()
        for d in trading_sessions_between(
            date.fromisoformat(first), date.fromisoformat(candidate_end)
        )
    ]
    reference_spy = set().union(*(series[1] for (_, s), series in stores.items() if s == "SPY"))
    last = max(reference_spy.intersection(official))
    sessions = [day for day in official if day <= last]
    fund = list(
        fundamental_rows(labels, range(2010, int(last[:4]) + 1), published, cells, canonical_cells)
    )
    security = list(security_rows(cohort, d8, START, last))
    market_ledger, market_plan, market_stats = market_gaps(
        stores,
        sessions,
        by_symbol,
        req["minimum_market_history"],
    )
    proxies = market_proxy(stores, sessions, list(labels.values()), req["minimum_market_history"])
    readiness = list(readiness_rows(proxies, sessions, base, base_qualification))
    ledger = (
        market_ledger
        + filing_ledger
        + [
            gap_row(
                "P0",
                "security",
                row["status"],
                cik=row["cik"],
                symbol=row["symbol"],
                item="PIT identity, listing and execution",
                start=START,
                end=last,
                provider="MASSIVE_REFERENCE_ALPACA_ACTIONS_SEC",
                detail=row["reference"],
            )
            for row in security
            if "UNRESOLVED" in row["status"]
        ]
    )
    ledger.extend(
        gap_row(
            "P0"
            if row["metric"] in {"shares_outstanding", "operating_cash_flow"}
            else "P1"
            if row["metric"] in PRIORITY_METRICS
            else "P2",
            "fundamental_period",
            row["status"],
            cik=row["cik"],
            symbol=row["symbol"],
            item=row["metric"],
            start=f"{row['year']}-01-01",
            end=min(last, f"{row['year']}-12-31"),
            provider="SEC_EDGAR",
            detail="Fiscal-period expectation needs listing, reporting and applicability evidence",
        )
        for row in fund
        if not row["research_usable"]
    )
    ledger.append(
        gap_row(
            "P0",
            "fundamental_warmup",
            "PRE2010_FUNDAMENTALS_OUTSIDE_STAGING",
            item="Fiscal TTM/current-prior, shares age and score lookback",
            start=req["nominal_fundamental_load_start_2010"],
            end="2009-12-31",
            provider="SEC_EDGAR",
            detail=PERIOD_PRIOR_FUNDAMENTALS,
        )
    )
    count_pit = sum(row["historical_pit_qualified"] for row in fund)
    plan = acquisition(
        security,
        market_plan,
        filing_plan,
        {
            "universe_sha256": D6_SHA[0],
            "d6_preflight_sha256": D6_SHA[1],
            "task94_staging_plan": str(databases / STAGING_NAME / "staging-plan.json"),
            "task94_staging_plan_sha256": inputs.files[
                str(databases / STAGING_NAME / "staging-plan.json")
            ]["sha256"],
            "task94_acquisition": inputs.files[
                str(databases / STAGING_NAME / "acquisition-plan.json")
            ],
            "market_evidence_fingerprint": fingerprint(
                inputs.files["market_daily_scoped_row_digests"]
            ),
        },
        market_stats,
    )
    req["u0_published_scope"] = {
        "scope": base,
        "qualification": base_qualification,
        "checked_current_snapshot": True,
        "backtest_rerun": False,
    }
    fund_stats = Counter(row["metric"] for row in fund if row["research_usable"])
    ranges = technical_summary(proxies, sessions, labels)
    summary = {
        "stage": stage_stats,
        "staging_pit_verified_rows": count_pit,
        "filing_cache": dict(filing_stats),
        "market": dict(market_stats),
        "ranges": ranges,
        "sessions_from_2010": sum(s >= START for s in sessions),
        "first_preroll": first,
        "end": last,
        "fundamental_cells_per_metric_with_candidate": dict(fund_stats),
        "census_alternatives_not_merged": sorted(census_labels),
        "fundamental_cells": len(fund),
        "historical_task94_group_count": len(groups),
        "canonical_lineages": lineages,
        "current_market_series": [
            {
                "source": "/".join(k),
                "symbol": s,
                "observed": len(v[0]),
                "valid": len(v[1]),
                "first": min(v[1], default=None),
                "last": max(v[1], default=None),
                "invalid_or_duplicate_rows": sum(v[2].values()),
            }
            for (k, s), v in sorted(stores.items())
        ],
        "observed_non_daily": intraday,
        "local_sources": source_inventory,
        "expanded_earliest_full_technical": None,
        "expanded_earliest_pit_qualified": None,
        "frozen_earliest_pit_qualified": None,
        "blocking_reasons": PIT_BLOCKERS,
        "prior_e5_contract": e5.get("contract"),
        "prior_e13_status": e13.get("status"),
        "prior_e5_status": e5.get("status"),
        "evidenced_canonical_filing_pairs_all_lineages": len(canonical_pairs),
        "coverage_definitions": {
            "raw_available": "observed row; may be invalid or non-admissible",
            "research_usable": "valid series row or unambiguous retrospective candidate",
            "historical_pit_qualified": "requires source, time and historical identity binding",
        },
    }
    providers = provider_plan(d8_provider, inputs)
    space = storage(
        d6,
        filing_stats["MISSING_IN_SCANNED_CACHES"],
        market_stats["spy_missing_reference"] + market_stats["issuer_missing_reference"],
        research,
    )
    print("Audit: Ergebnisartefakte veröffentlichen", flush=True)
    with TemporaryDirectory(prefix=f".{NAME}-", dir=target.parent) as temp:
        folder = Path(temp)
        for name, obj in (
            ("champion_data_requirements.json", req),
            ("acquisition_plan.json", plan),
            ("provider_feasibility.json", providers),
            ("storage_estimate.json", space),
            ("inventory_summary.json", summary),
        ):
            (folder / name).write_bytes(encoding(obj))
        for name, rows in (
            ("fundamental_coverage.csv", fund),
            (
                "market_coverage.csv",
                market_rows(
                    stores,
                    sessions,
                    by_symbol | {symbol: "" for symbol in census_labels if symbol not in by_symbol},
                ),
            ),
            ("security_coverage.csv", security),
            ("champion_readiness.csv", readiness),
            (
                "historical_data_gaps.csv",
                sorted(
                    ledger,
                    key=lambda r: (
                        r["priority"],
                        r["category"],
                        r["symbol"],
                        r["start"],
                        r["item"],
                        r["gap_id"],
                    ),
                ),
            ),
        ):
            csv_output(folder / name, rows)
        (folder / "readiness_report.md").write_text(
            report_text(summary, req, plan, space), encoding="utf-8", newline="\n"
        )
        repo = repository_evidence(ROOT)
        manifest = {
            "contract": "F01_TASK95_READINESS_AUDIT_V1",
            "status": "AUDIT_COMPLETE_PLAN_ONLY",
            "issuer_count": 50,
            "benchmark": "SPY",
            "calendar_revision": "EXACT_STRICTLY_PRECEDING_XNYS_SESSIONS",
            "supersedes_result_manifest": (
                inputs.files.get(str(prior_manifest)) if target != prior_manifest.parent else None
            ),
            "git_head": repo["git_head"],
            "candidate_fingerprint": repo["code_fingerprint"],
            "exchange_calendars_version": version("exchange-calendars"),
            "input_bindings": inputs.files,
            "network_calls": 0,
            "database_writes": 0,
            "outputs": {p.name: digest(p) for p in sorted(folder.iterdir())},
        }
        (folder / "manifest.json").write_bytes(encoding(manifest))
        folder.rename(target)
    return {
        "result": str(target),
        "manifest_sha256": digest(target / "manifest.json"),
        "technical_summary": ranges,
        "market": dict(market_stats),
        "filings": dict(filing_stats),
    }


def report_text(s, r, p, capacity):
    stats, filings, market = s["stage"], s["filing_cache"], s["market"]
    u0, c = r["u0_published_scope"]["scope"], r["u0_published_scope"]["qualification"]
    e = p["request_estimates"]
    free = capacity["volumes"].get(str(Path(RESEARCH.anchor)), {}).get("free_bytes_floor_gib")
    free_gib = "nicht belegt" if free is None else f"{free / 1024**3:.0f} GiB"
    missing = filings.get("MISSING_IN_SCANNED_CACHES", 0)
    local = filings.get("LOCAL_UNVERIFIED", 0)
    verified = filings.get("ORIGINAL_BYTES_MATCH_PUBLISHED_MANIFEST", 0)
    pages = e["alpaca_single_symbol_reference_windows_one_page_each"]
    return f"""# Task 9.5 · Historische Datenabdeckung und Beschaffungsplan

## Datenbestand und Fundamentalqualität

Der Audit hat die gebundene D.6-Kohorte mit 50 Emittenten und SPY separat geprüft. Der
XNYS-Referenzkalender reicht ab 2010 bis {s["end"]} mit {s["sessions_from_2010"]:,} regulären
Sessions. Die letzte Session ist durch einen lokal gültigen SPY-Bar belegt. Vollständigkeit
über das gesamte Universum bleibt separat zu prüfen.

Das unveränderte Task-9.4-Staging enthält {stats["observation"]:,} in SQLite gespeicherte
Rohbeobachtungen und {stats["candidate"]:,} retrospektive Kandidaten. Das Periodenende liegt
zwischen
{stats["period_start"]} und {stats["period_end"]}. Die Rohzahl des alten Laufberichts erfasst
auch gelesene Beobachtungen außerhalb des gespeicherten Periodenfilters. Die Matrix trennt
Rohvorkommen, fachlich normalisierbare Kandidaten und {s["staging_pit_verified_rows"]} im Staging
originalverifizierte PIT-Zellen. Acceptance-Zeitpunkte für Firmenmeldungen qualifizieren
retrospektive Werte weiterhin nur provisorisch. Form-Zählungen sind Kandidaten aus Annual-
beziehungsweise Quarterly-Forms. Geschäftsjahresabschluss, einzelne Quartale und YTD-Ableitungen
brauchen ihre jeweiligen Perioden- und Scope-Belege.

Wesentliche Lücken betreffen Original/PIT-Facts für OCF und Issuer Shares sowie die
parallel benötigten Quality-/Valuation-Komponenten, Zeitreihen und Peer-Daten. Markt-
und Security-Provenienz sind separate Gates. Die Original- und E.5/E.13-Witnesses gelten
innerhalb ihrer gebundenen Prüfbereiche. Die Staging-Daten decken den nominalen
Fundamentalvorlauf ab {r["nominal_fundamental_load_start_2010"]} für einen Start 2010 nicht ab.

## Champion-Baseline und Expanded Historical 50

Die publizierte U0-Rekonstruktion umfasst {u0["historical_start"]} bis {u0["historical_end"]}.
Ihre bestehende Startqualifikation nennt {c["screenable_symbol_count_at_start"]} im damaligen
U0-Scope screenbare Symbole. Der Audit prüfte die lokale Snapshot-Datei und scoped Daily-Bars.
Zeitpunktgetreue historische Mitgliedschaft und Feed/Adjustment pro Legacy-Bar sind weiterhin
unbelegt. Es wurde kein Strategie-Replay ausgeführt.

Für die D.6-Erweiterung ist **kein vollständig technischer oder historisch PIT-qualifizierter
Backtest-Start** nachgewiesen. Die Matrix weist Monats- und Jahreswerte für einen
Marktdaten-Proxy aus. Dieser erfordert dieselbe lokale Quellvariante, je
{r["minimum_market_history"]}
vorherige lückenlose Sessions für SPY und das Symbol und einen gültigen aktuellen Bar.
Die geladenen {r["daily_load_preroll"]} Sessions beginnen für den Start 2010 am
{r["start_2010_warmup"]}. Der eingefrorene Loader liefert {r["frozen_loader_start_2010"]}.
Der Audit verwendet exakt die vorangehenden XNYS-Sessions. Symbolproxies gelten bis zur
Epoch-, Adjustment-, Execution- und
Fundamentalprüfung als technische Teilabdeckung. Der früheste Legacy-Marktproxy ist
{s["ranges"]["legacy"]["earliest_any_proxy"]}, der früheste Forward-SIP/all-Proxy
{s["ranges"]["forward"]["earliest_any_proxy"]}. Null qualifizierte D.6-Zeitfenster.

## Konkrete Lücken und Task-9.6-Reihenfolge

- Originale im Task-9.4-Inventar nach erneuter lokaler Prüfung. {missing:,}
  fehlen in den geprüften Caches, {local:,} sind lokal und noch byteunverifiziert,
  {verified:,} wurden gegen publizierte Dateihashes geprüft.
  DTS-, Extraction- und Canonical-Hinweise bleiben in der Planung separat erhalten.
- SIP/all-Referenzlücken. {market.get("issuer_missing_reference", 0):,} Emittenten-Symbolsessions
  und
  {market.get("spy_missing_reference", 0):,} SPY-Sessions fehlen nach Anrechnung belegter gleicher
  Varianten.
  Es fehlen {market.get("spy_warmup_300_missing", 0)} der 300 Mindest-Vorlaufsessions und
  {market.get("spy_preroll_320_missing", 0)} der 320 Sessions im vollst?ndigen Ladevorlauf.
  Die Emittentenzahl ist ein XNYS-Planungsrahmen. Vorlistings, Exits und Symbolwechsel müssen je
  Security-Epoche
  vor einer Bestellmenge ausgeschieden werden. Probe-Hits bleiben als eigene Quelle ausgewiesen.
- Reihenfolge. Lokale Originale und passende SIP-Bars nutzen. Danach SPY und Warm-up klären,
  historische
  Security-Epochen und Actions belegen und erst anschließend die Daily-Lücken je Security
  eingrenzen.
  Kritische periodische SEC-Originale können nach lokalem Reuse parallel vorbereitet werden.
  Amendments und
  weitere Fundamentalabdeckung brauchen eigene Review-Schritte.
- Anbieter. Alpaca SIP/all ist ab 2016 punktuell lokal belegt. Massive liefert einzelne datierte
  Security-/Action-Belege. Frühere Bars, die Kontoberechtigungen, Preise, archivierungsfähige
  Rechte und die
  endgültigen API-Limits bleiben VERIFICATION_REQUIRED. Für die Zeit vor 2016 ist keine Quelle
  freigegeben.
- Umfangsszenario. {e["sec_accessions"]:,} SEC-Accessions in beschaffbaren Planblöcken,
  {e["sec_planning_http_requests_low"]:,}–{e["sec_planning_http_requests_high"]:,} SEC-Requests
  bei
  2–35 Requests je Filing und {e["sec_time_hours_at_serial_one_second_pacing_excluding_retries"]}
  Stunden bei serieller Sekunden-Taktung ohne Retries. {pages:,}
  Alpaca-Symbolfenster im Ein-Seiten-Szenario vor Epoch-Clamping. Geschätzter Arbeitsspeicherplatz
  {capacity["estimated_working_bytes"] / 1024**3:.1f} GiB inklusive Reserve, G: frei {free_gib}.
  Backup-Szenario und F:-Kapazität stehen im Storage-Report. API-Zeit für Marktdaten ist nach
  Tarifprüfung zu schätzen.
- Offen bleiben möglicherweise historische Vor-2016-SIP-Ersetzung mit bestätigtem Nutzungsrecht,
  durchgängige Handels-/Exit-Epochen für historische Sonderfälle und die damals tatsächlich
  gültige
  Accounting-/Adjustment-Basis. Der Plan erteilt keine Download-, Import- oder Trading-Freigabe.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit",))
    parser.add_argument(
        "--output",
        type=Path,
        default=RESEARCH / "import-artifacts" / NAME,
        help="New result directory under the local Research import-artifacts folder",
    )
    args = parser.parse_args(argv)
    target = args.output.absolute()
    allowed = (RESEARCH / "import-artifacts").resolve(strict=True)
    if target.parent != allowed or target.exists() or target.is_symlink():
        parser.error("a fresh direct subdirectory of Research import-artifacts is required")
    print(encoding(audit(RESEARCH, target)).decode(), end="")


if __name__ == "__main__":
    main()
