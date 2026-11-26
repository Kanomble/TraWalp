"""Reuse Task 9 DTS, extraction and accounting gates for a defined filing set."""

import subprocess
from collections import Counter
from pathlib import Path

import f01_task9_auto_admission as auto
import run_f01_historical_50_manual as task9
from f01_readiness_inventory import STAGE_DATABASE, digest, read_database
from f01_task96_contract import RESEARCH, STAGING, current_code, key, publish, require
from f01_task96_evidence import METADATA_EXCEPTIONS, checked_original

from trading_system.data.canonical_facts import fingerprint

SHARD_ROOT = RESEARCH / "arelle-evidence/f01-task96-acquisition-v1"
BLOCKERS = {
    "QUARANTINED",
    "ORIGINAL_MISSING",
    "LOCAL_ORIGINAL_REVIEW_REQUIRED",
    "ARCHIVED_SUBMISSIONS_IDENTITY_OR_PERIOD_UNRESOLVED",
}


def progress_for(block_id, rows, evidence, *, process=False):
    code = current_code()
    produced = []
    processor = None
    if process:
        SHARD_ROOT.parent.mkdir(exist_ok=True)
        task9.bulk.directory(SHARD_ROOT.parent)
        task9.filing.safe_directory(SHARD_ROOT)
    with read_database(STAGING / "companyfacts-staging.sqlite3", {}, STAGE_DATABASE) as db:
        for row in rows:
            pair = key(row)
            item = evidence.get(pair, {})
            status = item.get("status", "ORIGINAL_MISSING")
            existing = item.get("extraction")
            if status != "ORIGINAL_VERIFIED":
                produced.append({"cik": pair[0], "accession": pair[1], "status": status})
                continue
            if existing:
                # This published evidence keeps its own code, taxonomy and selection binding.
                produced.append(
                    {
                        "cik": pair[0],
                        "accession": pair[1],
                        "status": "EXTRACTION_REUSED",
                        "extraction": existing,
                        "accounting": item["archived_admission"],
                        "cross_lineage_witnesses": len(item["qualified_witnesses"]),
                        "pit": "PIT_EVIDENCE_INCOMPLETE",
                    }
                )
                continue
            if not process:
                produced.append(
                    {
                        "cik": pair[0],
                        "accession": pair[1],
                        "status": "ORIGINAL_VERIFIED",
                        "next": "OFFLINE_DTS",
                    }
                )
                continue
            proof = item["source"]
            try:
                source = checked_original(Path(proof["directory"]), row, proof["documents"])
                if processor is None:
                    processor = task9.filing.ArelleProcessor(task9.filing.ARELLE_ROOT)
                    require(
                        processor.release["version"] == task9.filing.ARELLE_VERSION,
                        "ARELLE_RUNTIME_CHANGED",
                    )
                produced.append(extract(row, source, block_id, code, processor, db))
            except (*METADATA_EXCEPTIONS, subprocess.TimeoutExpired) as exc:
                produced.append(
                    {
                        "cik": pair[0],
                        "accession": pair[1],
                        "status": "QUARANTINED",
                        "reason": str(exc),
                        "pit": "PIT_EVIDENCE_INCOMPLETE",
                    }
                )
    return {
        "contract": "F01_TASK96_OFFLINE_FILING_EVIDENCE_V1",
        "block_id": block_id,
        "code_fingerprint": code,
        "filings": produced,
        "counts": dict(Counter(r["status"] for r in produced)),
        "canonical_import_authorized": False,
    }


def extract(row, source, block_id, code, processor, db):
    filing_id = task9.review.filing_key(row)
    scope = {"block": block_id, "source": source, "row": row, "code": code}
    job = task9.filing.safe_directory(SHARD_ROOT / fingerprint(scope))
    source_sha = fingerprint(source)
    selections = {"filings": [row]}
    verified = {"filings": {filing_id: source}}
    receipt = {"cik": row["cik"], "accession": row["accession"], "source_sha256": source_sha}
    for attempt in range(2):
        closure = task9.taxonomy_report(job, selections, verified, source_sha)
        closure_sha = publish(job / f"taxonomy-{fingerprint(closure)}.json", closure)
        if closure["status"] != "TAXONOMY_CLOSURE_READY":
            return receipt | {
                "status": "DTS_INCOMPLETE",
                "closure_sha256": closure_sha,
                "pit": "PIT_EVIDENCE_INCOMPLETE",
            }
        try:
            shard = task9.extract_filing(
                job, row, source, closure["filings"][filing_id], code, processor
            )
        except task9.bulk.Blocked as exc:
            if attempt or not str(exc).startswith("EXTRACTION_OR_DTS_INCOMPLETE"):
                raise
        else:
            break
    index, _ = task9.filing.cache_read(Path(source["directory"]) / "index.json", row, "index.json")
    entrypoints = task9.filing.documents(row, task9.json.loads(index))
    decision = auto.evaluate_filing(row, source, shard, closure["filings"][filing_id], entrypoints)
    bindings = {
        "scope": scope,
        "closure_sha256": closure_sha,
        "shard_sha256": digest(job / f"{row['accession']}-extraction.json"),
    }
    report = auto.batch_report({filing_id: decision}, bindings)
    report_sha = publish(job / f"accounting-{fingerprint(report)}.json", report)
    facts = []
    for record in db.execute(
        """SELECT candidate_id,taxonomy,tag,unit,period_start,period_end,
        value_decimal,acceptance_utc
        FROM candidate WHERE cik=? AND accession=? ORDER BY candidate_id""",
        key(row),
    ):
        fact = dict(
            zip(
                (
                    "candidate_id",
                    "taxonomy",
                    "tag",
                    "unit",
                    "period_start",
                    "period_end",
                    "value",
                    "acceptance_utc",
                ),
                record,
                strict=True,
            )
        )
        fact["accession_number"] = row["accession"]
        facts.append(task9.filing.join(fact, shard["observations"], row["cik"]))
    join_sha = publish(job / f"companyfacts-match-{fingerprint(facts)}.json", facts)
    return receipt | {
        "status": "ACCOUNTING_ADMISSION_READY"
        if report["summary"]["facts_admitted"]
        else "ACCOUNTING_REVIEW_REQUIRED",
        "extraction_status": "EXTRACTION_COMPLETE",
        "offline_evidence_path": str(job),
        "closure_sha256": closure_sha,
        "original_matches_sha256": join_sha,
        "accounting_sha256": report_sha,
        "v1_admitted_candidates": report["summary"]["facts_admitted"],
        "pit": "PIT_EVIDENCE_INCOMPLETE",
    }
