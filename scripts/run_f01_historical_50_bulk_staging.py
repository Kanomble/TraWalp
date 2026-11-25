#!/usr/bin/env python3
"""Offline D.6 Companyfacts staging. Writes only a separately authorized research target."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date
from pathlib import Path
from zipfile import BadZipFile, ZipFile

sys.dont_write_bytecode = True

import f01_bulk_staging_rules as rules  # noqa: E402
import import_f01_sec_bulk_pilot as pilot  # noqa: E402
import prepare_f01_filing_evidence as filing  # noqa: E402
import prepare_f01_historical_50_batch as d6  # noqa: E402
from bootstrap_f01_research_db import _code_evidence  # noqa: E402
from run_f01_historical_50_manual import SOURCE_ROOT, checked, publish  # noqa: E402

from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.sec_sync_persistence import stage_lock  # noqa: E402

bulk = pilot.bulk
require = bulk.require
CONTRACT = "F01_D6_COMPANYFACTS_STAGING_V1"
CLASSIFICATION = "UNVERIFIED_COMPANYFACTS_RESEARCH_STAGING"
ROOT = d6.RESEARCH / "research-databases"
TARGET = ROOT / "f01-companyfacts-staging-historical50-v1"
GIB = 1024**3
BATCH_SIZE = 20
DB = "companyfacts-staging.sqlite3"
SCHEMA = """
CREATE TABLE staging_binding (plan_sha256 TEXT NOT NULL, classification TEXT NOT NULL);
CREATE TABLE issuer (
    cik TEXT PRIMARY KEY, symbol_label TEXT NOT NULL, companyfacts_member_sha256 TEXT NOT NULL,
    observations INTEGER NOT NULL, retrospective INTEGER NOT NULL, acceptance INTEGER NOT NULL
);
CREATE TABLE candidate (
    cik TEXT NOT NULL REFERENCES issuer, candidate_id INTEGER NOT NULL,
    metric TEXT NOT NULL, unit TEXT NOT NULL, period_start TEXT, period_end TEXT NOT NULL,
    filed TEXT NOT NULL, accession TEXT NOT NULL, taxonomy TEXT NOT NULL, tag TEXT NOT NULL,
    value_decimal TEXT NOT NULL, frame TEXT, acceptance_utc TEXT, occurrence_count INTEGER NOT NULL,
    PRIMARY KEY(cik,candidate_id)
);
CREATE TABLE observation (
    cik TEXT NOT NULL REFERENCES issuer, taxonomy TEXT NOT NULL, tag TEXT NOT NULL,
    unit TEXT NOT NULL, member_unit_ordinal INTEGER NOT NULL, payload_json TEXT NOT NULL,
    metric TEXT, disposition TEXT NOT NULL, candidate_id INTEGER,
    PRIMARY KEY(cik,taxonomy,tag,unit,member_unit_ordinal),
    FOREIGN KEY(cik,candidate_id) REFERENCES candidate(cik,candidate_id)
);
CREATE INDEX candidate_period ON candidate(cik,metric,period_end,unit);
CREATE VIEW retrospective_fundamentals AS SELECT candidate.*,
    'UNVERIFIED_COMPANYFACTS_RESEARCH_STAGING' AS classification,
    'UNVERIFIED_HISTORICAL_ORIGINAL' AS original_pit_status FROM candidate;
"""


def inputs():
    universe, _ = d6.read_bound(d6.REPO / "reports/f01_historical_50_pilot.csv", d6.D6_SHA[0])
    raw, inventory = checked(d6.REPO / "reports/f01_historical_50_preflight.json", d6.D6_SHA[1])
    cohort, _, expected, _ = d6.d6_inputs(universe, raw)
    source_sha = inventory["bindings"]["bulk_manifest_sha256"]
    manifest_path = SOURCE_ROOT / bulk.MANIFEST_NAME
    _, source = checked(manifest_path, source_sha)
    require(
        source["contract"] == bulk.MANIFEST_CONTRACT
        and source["generation"] == SOURCE_ROOT.name
        and source["trading_universe_qualified"] is False,
        "BULK_GENERATION_INVALID",
    )
    paths, exclusions = {}, {}
    for endpoint, url in bulk.URLS.items():
        record = source["archives"][endpoint]
        path = SOURCE_ROOT / f"{endpoint}.zip"
        integrity = record["integrity"]
        require(
            record["local_path"] == str(path)
            and record["url"] == url
            and record["bytes"] == bulk.plain(path).st_size
            and integrity["status"] == "VERIFIED",
            f"BULK_ARCHIVE_BINDING_MISMATCH: {endpoint}",
        )
        paths[endpoint] = path
        exclusions[endpoint] = {item["member"] for item in integrity["excluded_members"]}
    code = _code_evidence()
    return (
        cohort,
        expected,
        paths,
        exclusions,
        {
            "contract": CONTRACT,
            "classification": CLASSIFICATION,
            "source_manifest_sha256": source_sha,
            "universe_sha256": d6.D6_SHA[0],
            "inventory_sha256": d6.D6_SHA[1],
            "historical_period_start": rules.START.isoformat(),
            "candidate": {"head": code["git_head"], "fingerprint": code["code_fingerprint"]},
            "snapshot_retrieval_ended_utc": source["archives"]["companyfacts"][
                "retrieval_ended_at_utc"
            ],
            "issuer_count": 50,
            "benchmark_excluded": "SPY",
            "target": str(TARGET),
            "network_authorized": False,
            "canonical_import_authorized": False,
        },
    )


def coverage_rows(symbol, cik, aggregate, final_year):
    for year in range(rules.START.year, final_year + 1):
        for metric in rules.METRICS:
            counts = aggregate.get((year, metric), {})
            raw = counts.get("observations", 0)
            available = counts.get("retrospective", 0)
            stamp = counts.get("with_acceptance", 0)
            yield {
                "symbol_label": symbol,
                "cik": cik,
                "year": year,
                "metric": metric,
                "observed": raw,
                "retrospective_candidates": available,
                "provisional_acceptance_bound": stamp,
                "original_pit_verified": 0,
                "ambiguous": counts.get("ambiguous", 0),
                "excluded": counts.get("excluded", 0),
                "quality_score_0_to_100": round(100 * (available + stamp) / (2 * raw))
                if raw
                else 0,
                "status": "PROVISIONAL" if available else "NO_UNAMBIGUOUS_CANDIDATE",
            }


def cache_roots():
    yield filing.ROOT
    for job in sorted(
        d6.ARTIFACTS.joinpath("f01-task9-ingestion-preparation-v1").glob("f01-task9-*")
    ):
        if bulk.exists(job / "originals"):
            yield bulk.directory(job / "originals")


def filing_report(filings, cik, symbol, roots):
    for accession, item in sorted(filings.items()):
        paths = [
            str(root / cik / accession) for root in roots if bulk.exists(root / cik / accession)
        ]
        yield {
            **item,
            "problems": sorted(item["problems"]),
            "cik": cik,
            "symbol_label": symbol,
            "cache_directories": paths,
            "original_status": "LOCAL_DIRECTORY_UNVERIFIED"
            if paths
            else "MISSING_IN_SCANNED_CACHES",
        }


def batch_groups(filings):
    eligible = [
        row
        for row in filings
        if row["original_status"] == "MISSING_IN_SCANNED_CACHES"
        and row["acceptance_utc"]
        and row["form"] in {"10-K", "10-Q", "20-F", "40-F"}
        and not any(p.startswith("SUBMISSIONS") for p in row["problems"])
    ]
    groups = []
    eligible.sort(key=lambda row: (row["filed"], row["cik"], row["accession"]))
    for index in range(0, len(eligible), BATCH_SIZE):
        selected = eligible[index : index + BATCH_SIZE]
        groups.append(
            {
                "batch_id": d6.sha(d6.json_bytes(selected)),
                "status": "PLAN_ONLY",
                "accessions": selected,
            }
        )
    return groups


def scan(*, inputs_record=None, consume=None):
    cohort, expected, paths, exclusions, plan = inputs_record or inputs()
    roots = list(cache_roots())
    all_coverage, all_filings, issuer_reports = [], [], []
    totals = Counter()
    rows = sorted(cohort.values(), key=lambda row: row["planning_cik"])
    member_names = [
        {"cik": r["planning_cik"], "submissions_member": f"CIK{r['planning_cik']}.json"}
        for r in rows
    ]
    last_year = date.fromisoformat(plan["snapshot_retrieval_ended_utc"][:10]).year
    with pinned_files(paths), ZipFile(paths["companyfacts"]) as archive:
        submissions = pilot.submissions_members(paths, member_names, exclusions["submissions"])
        for row in rows:
            symbol, cik = row["symbol_label"], row["planning_cik"]
            binding = expected[symbol]
            payload, digest = pilot.zip_member(
                archive, f"CIK{cik}.json", "companyfacts", exclusions["companyfacts"]
            )
            main, submissions_sha, history = submissions.pop(cik)
            require(
                pilot.provider_cik(payload) == cik
                and digest == binding["companyfacts_member_sha256"]
                and submissions_sha == binding["submissions_member_sha256"]
                and {x["member"]: x["sha256"] for x in history}
                == binding["submissions_history_member_sha256"],
                f"D6_ISSUER_MEMBER_BINDING_MISMATCH: {cik}",
            )
            reliable, conflicts, malformed = pilot.filing_index(main, history)
            observations, facts, coverage, filings, counts = rules.process(
                payload,
                reliable,
                conflicts,
            )
            counts["submissions_malformed_rows"] = malformed
            counts["submissions_conflict_accessions"] = len(conflicts)
            totals.update(counts)
            issuer_reports.append(
                {
                    "cik": cik,
                    "symbol_label": symbol,
                    "member_sha256": digest,
                    "submissions_member_sha256": submissions_sha,
                    "archived_observations": len(observations),
                    "quality_counts": dict(counts),
                }
            )
            all_coverage.extend(coverage_rows(symbol, cik, coverage, last_year))
            all_filings.extend(filing_report(filings, cik, symbol, roots))
            if consume is not None:
                consume(cik, symbol, digest, observations, facts)
    groups = batch_groups(all_filings)
    report = {
        "status": "BULK_PREFLIGHT_READY",
        "plan": plan,
        "binding_sha256": d6.sha(d6.json_bytes(plan)),
        "issuers": issuer_reports,
        "totals": dict(totals),
        "filing_status_counts": dict(Counter(row["original_status"] for row in all_filings)),
        "acquisition_groups": len(groups),
        "coverage_rows": len(all_coverage),
        "original_pit_status": "UNVERIFIED",
        "acquisition_executor": (
            "PLAN_ONLY; authorize separately; reuse SEC HTTP limiter and filing cache resume "
            "after explicitly binding this broader selection to acquisition"
        ),
    }
    return report, all_coverage, all_filings, groups


def save_reports(folder, bundle):
    report, coverage, filings, groups = bundle
    coverage_path = folder / "coverage.csv"
    publish_csv(coverage_path, coverage)
    manifest = report | {
        "coverage_sha256": d6.sha(coverage_path.read_bytes()),
        "filing_review_sha256": publish(folder / "filing-review.json", filings),
        "acquisition_plan_sha256": publish(folder / "acquisition-plan.json", groups),
    }
    publish(folder / "preflight.json", manifest)
    return manifest


def publish_csv(path, rows):
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix=".coverage-", dir=path.parent) as temporary:
        staged = Path(temporary) / "coverage.csv"
        d6.write_csv(staged, rows, tuple(rows[0]))
        if bulk.exists(path):
            require(
                bulk.plain(path).st_size == staged.stat().st_size
                and path.read_bytes() == staged.read_bytes(),
                "COVERAGE_CONFLICT",
            )
        else:
            staged.rename(path)


def write_issuer(connection, cik, symbol, digest, observations, facts):
    retained = connection.execute("SELECT * FROM issuer WHERE cik=?", (cik,)).fetchone()
    summary = (cik, symbol, digest, len(observations), len(facts), sum(bool(f[-2]) for f in facts))
    if retained:
        require(retained == summary, f"STAGING_RESUME_MISMATCH: {cik}")
        require(
            connection.execute(
                "SELECT * FROM candidate WHERE cik=? ORDER BY candidate_id", (cik,)
            ).fetchall()
            == [(cik, i, *fact) for i, fact in enumerate(facts, 1)]
            and connection.execute(
                "SELECT * FROM observation WHERE cik=? "
                "ORDER BY taxonomy,tag,unit,member_unit_ordinal",
                (cik,),
            ).fetchall()
            == [(cik, *record) for record in observations],
            f"STAGING_RESUME_CONTENT_MISMATCH: {cik}",
        )
        return
    with connection:
        connection.execute("INSERT INTO issuer VALUES (?,?,?,?,?,?)", summary)
        connection.executemany(
            "INSERT INTO candidate VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((cik, number, *fact) for number, fact in enumerate(facts, 1)),
        )
        connection.executemany(
            "INSERT INTO observation VALUES (?,?,?,?,?,?,?,?,?)",
            ((cik, *record) for record in observations),
        )


def stage_database(path, expected):
    if not bulk.exists(path):
        bulk.write_new(path, b"")
    info = bulk.plain(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if not info.st_size:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            connection.execute(
                "INSERT INTO staging_binding VALUES (?,?)", (expected, CLASSIFICATION)
            )
            connection.commit()
        require(
            connection.execute("SELECT * FROM staging_binding").fetchall()
            == [(expected, CLASSIFICATION)],
            "STAGING_DATABASE_BINDING_MISMATCH",
        )
    except BaseException:
        connection.close()
        raise
    return connection


def execute_import(record, *, expected, resume, target):
    plan = record[-1]
    require(target == TARGET and target.is_absolute(), "EXACT_ISOLATED_STAGING_TARGET_REQUIRED")
    require(expected == d6.sha(d6.json_bytes(plan)), "STAGING_PREFLIGHT_SHA_MISMATCH")
    bulk.directory(target.parent)
    lock = target.parent / f".{target.name}.lock"
    if bulk.exists(lock):
        bulk.plain(lock)
    with stage_lock(lock):
        require(not bulk.exists(target), "STAGING_TARGET_ALREADY_EXISTS")
        stage = target.with_name(f".{target.name}.incomplete")
        if not bulk.exists(stage):
            require(not resume, "NO_STAGING_RUN_TO_RESUME")
            require(shutil.disk_usage(target.parent).free >= GIB, "STAGING_CAPACITY_REQUIRED")
            stage.mkdir()
        else:
            require(resume, "STAGING_REQUIRES_EXPLICIT_RESUME")
            bulk.directory(stage)
        publish(stage / "staging-plan.json", plan)
        with (
            pinned_files({}, directories=(stage,)),
            closing(stage_database(stage / DB, expected)) as connection,
        ):
            bundle = scan(
                inputs_record=record, consume=lambda *args: write_issuer(connection, *args)
            )
            require(
                connection.execute("SELECT COUNT(*) FROM issuer").fetchone() == (50,)
                and not connection.execute("PRAGMA foreign_key_check").fetchone()
                and connection.execute("PRAGMA quick_check").fetchone() == ("ok",),
                "STAGING_INTEGRITY_FAILURE",
            )
        manifest = save_reports(stage, bundle)
        require(
            all(not bulk.exists(stage / (DB + suffix)) for suffix in ("-wal", "-shm", "-journal")),
            "STAGING_SIDECAR_PRESENT",
        )
        final = {
            "status": "STAGING_PUBLISHED",
            "plan_sha256": expected,
            "database_sha256": pilot.file_sha(stage / DB),
            "target": str(target / DB),
            "preflight_sha256": d6.sha((stage / "preflight.json").read_bytes()),
            "classification": CLASSIFICATION,
            "issuers": len(manifest["issuers"]),
        }
        publish(stage / "staging-result.json", final)
        stage.rename(target)
        return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "import"))
    parser.add_argument("--output", type=Path, help="New report directory under import-artifacts")
    parser.add_argument("--target", type=Path, default=TARGET)
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--authorize-staging-import", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.phase == "preflight":
            require(
                not args.authorize_staging_import and not args.resume and args.target == TARGET,
                "INVALID_PREFLIGHT_ARGUMENTS",
            )
            bundle = scan()
            report = bundle[0]
            if args.output:
                output = args.output
                require(
                    output.is_absolute()
                    and output.parent == d6.ARTIFACTS
                    and output.name.startswith("f01-task94-")
                    and ".." not in output.name,
                    "NEW_TASK94_ARTIFACT_DIRECTORY_REQUIRED",
                )
                bulk.directory(output.parent)
                output.mkdir()
                report = save_reports(output, bundle)
        else:
            require(
                args.authorize_staging_import and args.expect_plan_sha256 and not args.output,
                "SEPARATE_STAGING_IMPORT_AUTHORIZATION_REQUIRED",
            )
            report = execute_import(
                inputs(), expected=args.expect_plan_sha256, resume=args.resume, target=args.target
            )
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, BadZipFile) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
