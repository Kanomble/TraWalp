"""Read-only inventory for the historical F01 readiness audit."""

import csv
import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from download_f01_sec_bulk import directory, plain
from f01_alpaca_historical_sip_coverage import populate
from f01_historical_50_preflight_run import path_state

from trading_system.data.canonical_paths import pinned_files

START = "2010-01-01"
STAGE_PLAN = "c62ec7e82663363c75e838c4f2064cb859bb4af2cc4ba56cd0ddc6be4eee3565"
STAGE_DATABASE = "8e022640986af4b2844e726ead6dfa5ee0bba6be7ee8f9acc5490305116d70f1"
CLASSIFICATION = "UNVERIFIED_COMPANYFACTS_RESEARCH_STAGING"
LEGACY = ("legacy", "unknown", "unknown", "unknown")
U0 = ("u0_snapshot", "unknown", "unknown", "unknown")
SIP = ("forward", "alpaca", "sip", "all")
REQUIRED_RESEARCH_METRICS = ("shares_outstanding",)
MARKET_DAILY = """SELECT timestamp,open,high,low,close,volume FROM bars
WHERE symbol=? AND timeframe='1d' AND timestamp>=? AND timestamp<? ORDER BY timestamp"""
FORWARD_DAILY = """SELECT timestamp,open,high,low,close,volume,session,retrieved_at,sync_reference
FROM forward_market_observations
WHERE symbol=? AND timeframe='1d' AND provider=? AND feed=? AND adjustment=?
AND session BETWEEN ? AND ? ORDER BY session"""


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoding(document):
    return (json.dumps(document, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()


class Inputs:
    def __init__(self):
        self.files = {}

    def read(self, path, expected=None):
        plain(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if expected and sha != expected:
            raise ValueError(f"INPUT_HASH_MISMATCH: {path}")
        self.files[str(path)] = {"sha256": sha, "bytes": len(raw)}
        return raw

    def json(self, path, expected=None):
        return json.loads(self.read(path, expected))

    def present(self, path):
        result = {"path": str(path), "present": path.exists()}
        if result["present"]:
            result["bytes"] = plain(path).st_size
        self.files[str(path)] = result
        return result


@contextmanager
def read_database(path, bindings, expected_sha=None):
    """Deny writes while scanning and refuse a snapshot with pending WAL or rollback."""
    plain(path)
    before = path_state(path)
    if before.get(path.name + "-wal", (0, 0, 0))[2] or path.name + "-journal" in before:
        raise ValueError(f"SQLITE_UNCHECKPOINTED: {path}")
    with pinned_files({"read": path}) as pins:
        stream = pins["read"]
        if not os.path.samestat(os.fstat(stream.fileno()), path.stat()) or before != path_state(
            path
        ):
            raise ValueError(f"SQLITE_CHANGED_BEFORE_READ: {path}")
        binding = {"mode": "READ_ONLY_IMMUTABLE", "file_state": before}
        if expected_sha:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if sha != expected_sha:
                raise ValueError(f"DATABASE_HASH_MISMATCH: {path}")
            binding["sha256"] = sha
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            yield db
        if before != path_state(path):
            raise ValueError(f"SQLITE_CHANGED_DURING_READ: {path}")
        bindings[str(path)] = binding


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def staging(root, inputs, labels):
    result = inputs.json(root / "staging-result.json")
    if (
        result["status"],
        result["classification"],
        result["plan_sha256"],
        result["database_sha256"],
    ) != ("STAGING_PUBLISHED", CLASSIFICATION, STAGE_PLAN, STAGE_DATABASE):
        raise ValueError("STAGING_PUBLICATION_MISMATCH")
    inputs.json(root / "staging-plan.json", STAGE_PLAN)
    report = inputs.json(root / "preflight.json", result["preflight_sha256"])
    coverage = list(
        csv.DictReader(
            inputs.read(root / "coverage.csv", report["coverage_sha256"]).decode().splitlines()
        )
    )
    filings = inputs.json(root / "filing-review.json", report["filing_review_sha256"])
    groups = inputs.json(root / "acquisition-plan.json", report["acquisition_plan_sha256"])
    forms = {(r["cik"], r["accession"]): r["form"] for r in filings}
    cells, counts, bounds = defaultdict(Counter), {}, {}
    distinct_periods = defaultdict(set)
    with read_database(root / "companyfacts-staging.sqlite3", inputs.files, STAGE_DATABASE) as db:
        if not {"issuer", "candidate", "observation", "staging_binding"} <= tables(db):
            raise ValueError("STAGING_SCHEMA_MISMATCH")
        if db.execute("SELECT * FROM staging_binding").fetchall() != [(STAGE_PLAN, CLASSIFICATION)]:
            raise ValueError("STAGING_DATABASE_BINDING_MISMATCH")
        if dict(db.execute("SELECT cik,symbol_label FROM issuer")) != labels:
            raise ValueError("STAGING_UNIVERSE_MISMATCH")
        for table in ("issuer", "observation", "candidate"):
            counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        bounds["period_start"], bounds["period_end"], bounds["filed_start"], bounds["filed_end"] = (
            db.execute(
                "SELECT MIN(period_end),MAX(period_end),MIN(filed),MAX(filed) FROM candidate"
            ).fetchone()
        )
        for cik, metric, start, end, accession, acceptance in db.execute(
            "SELECT cik,metric,period_start,period_end,accession,acceptance_utc FROM candidate"
        ):
            cell = cells[cik, end[:4], metric]
            form = forms.get((cik, accession))
            cell["candidates"] += 1
            cell["acceptance"] += bool(acceptance)
            cell["amendments"] += bool(form and form.endswith("/A"))
            cell["annual_form_candidates"] += form in {"10-K", "20-F", "40-F"}
            cell["quarter_form_candidates"] += form == "10-Q"
            cell["instant_candidates"] += start is None
            distinct_periods[cik, end[:4], metric].add((start, end))
        for key, periods in distinct_periods.items():
            ends = {end for _, end in periods}
            cells[key]["distinct_periods"] = len(periods)
            cells[key]["distinct_period_ends"] = len(ends)
            cells[key]["first_period_end"] = min(ends)
            cells[key]["last_period_end"] = max(ends)
        shares = db.execute(
            """SELECT cik,substr(json_extract(payload_json,'$.end'),1,4),COUNT(*)
            FROM observation WHERE taxonomy='dei' AND tag='EntityCommonStockSharesOutstanding'
            GROUP BY 1,2"""
        )
        for cik, year, count in shares:
            cells[cik, year, "shares_outstanding"]["raw_tag_only"] = count
    expected = {(r["cik"], r["year"], r["metric"]): r for r in coverage}
    if len(expected) != len(coverage):
        raise ValueError("DUPLICATE_STAGING_COVERAGE_CELL")
    for key, row in expected.items():
        actual = cells[key]
        if (int(row["retrospective_candidates"]), int(row["provisional_acceptance_bound"])) != (
            actual["candidates"],
            actual["acceptance"],
        ):
            raise ValueError(f"STAGING_COVERAGE_RECONCILIATION_FAILED: {key}")
    return expected, cells, filings, groups, counts | bounds


def canonical(root, inputs):
    inventory, accessions, cells = [], set(), defaultdict(dict)
    for lineage in (
        "f01-sec-isolated-research-pilot01",
        "f01-sec-isolated-research-accounting-e11",
        "f01-sec-isolated-research-historical50-v1",
    ):
        path = root / lineage / "f01-sec-candidate.sqlite3"
        entry = {"lineage": lineage, "present": path.is_file()}
        if entry["present"]:
            with read_database(path, inputs.files) as db:
                schema = tables(db)
                entry["counts"] = {
                    table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "canonical_fact",
                        "source_occurrence",
                        "fact_dataset",
                        "ingestion_receipt",
                        "fact_revision",
                        "research_bootstrap",
                    )
                    if table in schema
                }
                if "fact_dataset" in schema:
                    entry["versions"] = list(
                        db.execute(
                            "SELECT version,dataset_id,source_hash FROM fact_dataset "
                            "ORDER BY version"
                        )
                    )
                if "canonical_fact" in schema:
                    entry["period_and_filing_bounds"] = db.execute(
                        "SELECT MIN(period_end),MAX(period_end),MIN(filed),MAX(filed) "
                        "FROM canonical_fact"
                    ).fetchone()
                    member = list(
                        db.execute(
                            "SELECT DISTINCT cik,accession_number FROM canonical_fact "
                            "ORDER BY cik,accession_number"
                        )
                    )
                    accessions.update(member)
                    entry["distinct_filing_pairs"] = len(member)
                    for cik, year, metric, count in db.execute(
                        "SELECT cik,substr(period_end,1,4),metric,COUNT(*) FROM canonical_fact "
                        "GROUP BY 1,2,3"
                    ):
                        cells[cik, year, metric][lineage] = count
        inventory.append(entry)
    return inventory, accessions, cells


def inventory_filings(filings, research, inputs):
    from prepare_f01_filing_evidence import cache_read

    jobs = sorted(
        (research / "import-artifacts/f01-task9-ingestion-preparation-v1").glob("f01-task9-*")
    )
    cache = [research / "sec-originals/filing-evidence", *(job / "originals" for job in jobs)]
    verified, dts, extracted = {}, set(), set()
    for job in jobs:
        for path in sorted(job.glob("verified-originals*.json")):
            source = inputs.json(path)
            if source.get("status") != "SOURCE_VERIFIED":
                continue
            for key, item in source["filings"].items():
                cik, accession = key.split("/")
                folder = Path(item["directory"])
                if folder not in [base / cik / accession for base in cache]:
                    raise ValueError(f"UNBOUND_ORIGINAL_CACHE: {folder}")
                try:
                    for prior in item["documents"]:
                        _, actual = cache_read(
                            folder / prior["filename"],
                            {"cik": cik, "accession": accession},
                            prior["filename"],
                        )
                        if actual != prior:
                            raise ValueError("ORIGINAL_MANIFEST_DOCUMENT_CHANGED")
                except (OSError, ValueError, RuntimeError) as exc:
                    verified.setdefault((cik, accession), {"failed": str(exc)})
                else:
                    if item["documents"]:
                        verified[(cik, accession)] = {
                            "source_sha256": inputs.files[str(path)]["sha256"],
                            "documents": len(item["documents"]),
                        }
        for path in sorted(job.glob("taxonomy-*.json")):
            report = inputs.json(path)
            if report.get("status") == "TAXONOMY_CLOSURE_READY":
                dts.update(report["filings"])
        path = job / "extraction-manifest.json"
        if path.is_file():
            report = inputs.json(path)
            if report.get("status") == "EXTRACTION_COMPLETE":
                extracted.update(report["filings"])
    rows = []
    for item in filings:
        key = item["cik"], item["accession"]
        directories = [p for base in cache if (p := base.joinpath(*key)).is_dir()]
        files = [p for p in directories if any(p.iterdir())]
        for folder in directories:
            directory(folder)
        proof = verified.get(key, {})
        status = (
            "ORIGINAL_BYTES_MATCH_PUBLISHED_MANIFEST"
            if proof.get("source_sha256")
            else "SOURCE_VERIFICATION_FAILED"
            if proof.get("failed")
            else "LOCAL_UNVERIFIED"
            if files
            else "MISSING_IN_SCANNED_CACHES"
        )
        filing_key = "/".join(key)
        rows.append(
            item
            | {
                "historical_original_status": item["original_status"],
                "original_status": status,
                "cache_directories": [str(p) for p in directories],
                "byte_check": proof,
                "published_dts_ready": filing_key in dts,
                "published_extraction_complete": filing_key in extracted,
            }
        )
    return rows


def market(stores, labels, first, end, inputs):
    data, other = {}, []
    for source, path in stores.items():
        with read_database(path, inputs.files) as db:
            schema = tables(db)
            if "bars" not in schema:
                raise ValueError(f"MARKET_SCHEMA_MISMATCH: {path}")
            if source == LEGACY and "forward_market_observations" not in schema:
                raise ValueError("FORWARD_MARKET_SCHEMA_MISMATCH")
            for symbol in labels:
                data[source, symbol] = populate(
                    db.execute(MARKET_DAILY, (symbol, first, f"{end}T23:59:59"))
                )
                for timeframe, count, earliest, latest in db.execute(
                    """SELECT timeframe,COUNT(*),MIN(timestamp),MAX(timestamp) FROM bars
                    WHERE symbol=? AND timeframe<>'1d'
                    GROUP BY timeframe ORDER BY timeframe""",
                    (symbol,),
                ):
                    other.append([source[0], symbol, timeframe, count, earliest, latest])
                if source == LEGACY:
                    variants = set(
                        db.execute(
                            """SELECT DISTINCT provider,feed,adjustment
                            FROM forward_market_observations
                        WHERE symbol=? AND timeframe='1d'""",
                            (symbol,),
                        )
                    ) | {SIP[1:]}
                    for provider, feed, adjustment in sorted(variants):
                        key = ("forward", provider, feed, adjustment)
                        data[key, symbol] = populate(
                            db.execute(
                                FORWARD_DAILY, (symbol, provider, feed, adjustment, first, end)
                            )
                        )
                    for (
                        provider,
                        feed,
                        adjustment,
                        timeframe,
                        count,
                        first_seen,
                        last_seen,
                    ) in db.execute(
                        """SELECT provider,feed,adjustment,timeframe,COUNT(*),
                            MIN(session),MAX(session) FROM forward_market_observations
                            WHERE symbol=? AND timeframe<>'1d' GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""",
                        (symbol,),
                    ):
                        other.append(
                            [
                                f"forward/{provider}/{feed}/{adjustment}",
                                symbol,
                                timeframe,
                                count,
                                first_seen,
                                last_seen,
                            ]
                        )
    inputs.files["market_daily_scoped_row_digests"] = {
        "/".join((*series, symbol)): result[3] for (series, symbol), result in sorted(data.items())
    }
    return data, other


def period_key(session):
    return date.fromisoformat(session).strftime("%Y-%m")
