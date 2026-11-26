"""Reuse Task 9 DTS, extraction and accounting gates for a defined filing set."""

import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from copy import copy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import f01_task9_auto_admission as auto
import run_f01_historical_50_manual as task9
from f01_readiness_inventory import STAGE_DATABASE, digest, read_database
from f01_task96_contract import RESEARCH, STAGING, current_code, key, publish, require
from f01_task96_evidence import METADATA_EXCEPTIONS, checked_original
from f01_task96_taxonomy import CACHE, Resolver, references, xml
from f01_task96_taxonomy_acquisition import acquisition_plan

from trading_system.data.canonical_facts import fingerprint

SHARD_ROOT = RESEARCH / "arelle-evidence/f01-task96-taxonomy-recovery-v1"
BLOCKERS = {
    "QUARANTINED",
    "ORIGINAL_MISSING",
    "LOCAL_ORIGINAL_REVIEW_REQUIRED",
    "ARCHIVED_SUBMISSIONS_IDENTITY_OR_PERIOD_UNRESOLVED",
}


def progress_for(block_id, rows, evidence, *, process=False, taxonomy_cache=CACHE, shard_root=None):
    code = current_code()
    produced = []
    resolver = None
    shard_root = shard_root or SHARD_ROOT
    if process:
        shard_root.parent.mkdir(exist_ok=True)
        task9.bulk.directory(shard_root.parent)
        task9.filing.safe_directory(shard_root)
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
                if resolver is None:
                    resolver = Resolver(root=taxonomy_cache)
                result = extract(row, source, block_id, code, resolver, db, shard_root=shard_root)
                produced.append(result)
            except (*METADATA_EXCEPTIONS, ET.ParseError, subprocess.TimeoutExpired) as exc:
                produced.append(
                    {
                        "cik": pair[0],
                        "accession": pair[1],
                        "status": "QUARANTINED",
                        "reason": str(exc),
                        "pit": "PIT_EVIDENCE_INCOMPLETE",
                    }
                )
    gaps = {uri for item in produced for uri in item.get("missing_taxonomy_uris", [])}
    return {
        "contract": "F01_TASK96_OFFLINE_FILING_EVIDENCE_V1",
        "block_id": block_id,
        "code_fingerprint": code,
        "filings": produced,
        "counts": dict(Counter(r["status"] for r in produced)),
        "taxonomy_acquisition": acquisition_plan(gaps),
        "canonical_import_authorized": False,
        "pit_trading_authorized": False,
    }


def original_references(row, source):
    folder = Path(source["directory"])
    seeds = defaultdict(set)
    for record in source["documents"]:
        filename = record["filename"]
        if not filename.lower().endswith((".xml", ".xsd")):
            continue
        data, actual = task9.filing.cache_read(folder / filename, row, filename)
        require(record == actual, "TAXONOMY_ORIGINAL_CHANGED")
        for uri, namespace in references(data, record["url"]):
            namespaces = seeds[uri]
            if namespace:
                namespaces.add(namespace)
    return seeds


def closure_for(job, row, source, resolver):
    originals = {item["url"]: item for item in source["documents"]}
    seeds = original_references(row, source)
    discovered = defaultdict(set)
    for prior in [job, *sorted(job.glob("dts-*"))]:
        hints, actual = task9.taxonomy_seeds(prior, row, source)
        for uri in hints:
            seeds.setdefault(uri, set())
        for uri, hashes in actual.items():
            discovered[uri].update(hashes - {None})
            seeds.setdefault(uri, set())
    closure = resolver.closure(seeds, originals)
    for uri, hashes in discovered.items():
        entry = closure.get(uri, {})
        if hashes and entry.get("status") == "BOUND" and hashes != {entry["source"]["sha256"]}:
            entry.update(status="QUARANTINED", reason="DISCOVERED_DTS_HASH_MISMATCH")
    return closure


def verified_worker_output(run, **request):
    result = run(**request)
    if result["issues"]:
        return result
    facts = result["observations"]
    if any(fact.get("evidence_issue") for fact in facts):
        result["issues"].append({"code": "XBRL_FACT_CONTEXT_OR_UNIT_UNRESOLVED"})
    entry = Path(request["document"])
    if entry.suffix.lower() == ".xml":
        instance = xml(entry.read_bytes())
        expected = Counter(
            (node.tag, node.get("contextRef"), node.get("unitRef"), node.get("id"))
            for node in instance.iter()
            if node.get("contextRef")
        )
        actual = Counter(
            (fact["concept"], fact["context_id"], fact["unit_id"], fact["fact_id"])
            for fact in facts
            if fact["context_id"]
        )
        if expected != actual:
            result["issues"].append({"code": "XBRL_INSTANCE_FACT_IDENTITY_MISMATCH"})
    return result


def extract(row, source, block_id, code, resolver, db, *, shard_root=None):
    shard_root = shard_root or SHARD_ROOT
    filing_id = task9.review.filing_key(row)
    scope = {"block": block_id, "source": source, "row": row, "code": code}
    job = task9.filing.safe_directory(shard_root / fingerprint(scope))
    source_sha = fingerprint(source)
    receipt = {"cik": row["cik"], "accession": row["accession"], "source_sha256": source_sha}
    plugin = task9.filing.verified_sec_plugin(resolver.arelle)
    processor = None
    for attempt in range(2):
        closure = closure_for(job, row, source, resolver)
        gaps = sorted(uri for uri, entry in closure.items() if entry["status"] != "BOUND")
        report = {
            "contract": "F01_TASK961_DTS_CLOSURE_V1",
            "source_sha256": source_sha,
            "status": "TAXONOMY_CLOSURE_READY"
            if plugin and closure and not gaps
            else "TAXONOMY_PENDING",
            "arelle_version": task9.filing.ARELLE_VERSION,
            "sec_plugin_verified": bool(plugin),
            "filings": {filing_id: closure},
        }
        closure_path = job / f"taxonomy-{fingerprint(report)}.json"
        closure_sha = publish(closure_path, report)
        if report["status"] != "TAXONOMY_CLOSURE_READY":
            return receipt | {
                "status": "DTS_INCOMPLETE",
                "closure_path": str(closure_path),
                "closure_sha256": closure_sha,
                "missing_taxonomy_uris": gaps,
                "pit": "PIT_EVIDENCE_INCOMPLETE",
            }
        if processor is None:
            processor = task9.filing.ArelleProcessor(resolver.arelle)
            require(
                processor.release["version"] == task9.filing.ARELLE_VERSION,
                "ARELLE_RUNTIME_CHANGED",
            )
        extraction_job = task9.filing.safe_directory(job / f"dts-{closure_sha[:12]}")
        try:
            with TemporaryDirectory(prefix="offline-", dir=shard_root) as temp:
                isolated = copy(processor)
                isolated.directory = Path(temp)
                isolated.cache = isolated.directory / "cache"
                isolated.packages = []  # Resolve only through the verified URI overlay.
                isolated.run = partial(verified_worker_output, isolated.run)
                resolver.materialize(isolated.directory, closure)
                shard = task9.extract_filing(extraction_job, row, source, closure, code, isolated)
        except task9.bulk.Blocked as exc:
            if not str(exc).startswith("EXTRACTION_OR_DTS_INCOMPLETE"):
                raise
            if attempt:
                return receipt | {
                    "status": "DTS_INCOMPLETE",
                    "reason": str(exc),
                    "closure_path": str(closure_path),
                    "closure_sha256": closure_sha,
                    "offline_evidence_path": str(extraction_job),
                    "missing_taxonomy_uris": [],
                    "pit": "PIT_EVIDENCE_INCOMPLETE",
                }
        else:
            break
    job = extraction_job
    index, _ = task9.filing.cache_read(Path(source["directory"]) / "index.json", row, "index.json")
    entrypoints = task9.filing.documents(row, task9.json.loads(index))
    decision = auto.evaluate_filing(row, source, shard, closure, entrypoints)
    bindings = {
        "scope": scope,
        "closure_sha256": closure_sha,
        "shard_sha256": digest(job / f"{row['accession']}-extraction.json"),
    }
    report = auto.batch_report({filing_id: decision}, bindings)
    accounting_path = job / f"accounting-{fingerprint(report)}.json"
    report_sha = publish(accounting_path, report)
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
        "closure_path": str(closure_path),
        "closure_sha256": closure_sha,
        "original_matches_sha256": join_sha,
        "shard_sha256": bindings["shard_sha256"],
        "runtime": shard.get("runtime"),
        "facts_extracted": len(shard["observations"]),
        "accounting_sha256": report_sha,
        "accounting_path": str(accounting_path),
        "v1_admitted_candidates": report["summary"]["facts_admitted"],
        "pit": "PIT_EVIDENCE_INCOMPLETE",
    }
