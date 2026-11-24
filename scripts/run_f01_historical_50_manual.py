#!/usr/bin/env python3
"""Manually advance one Task 9 historical SEC filing ingestion phase."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from decimal import InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag
from zipfile import BadZipFile

sys.dont_write_bytecode = True

import f01_task9_admission as review  # noqa: E402
import prepare_f01_arelle_taxonomies as taxonomy  # noqa: E402
import prepare_f01_filing_evidence as filing  # noqa: E402
import prepare_f01_historical_50_batch as d6  # noqa: E402
from bootstrap_f01_research_db import _code_evidence  # noqa: E402
from validate_f01_canonical_assurance import _check_quiescent  # noqa: E402
from validate_f01_sec_bulk_pilot import filing_documents  # noqa: E402

from trading_system.data import canonical_bootstrap as bootstrap  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.sec_sync_persistence import stage_lock  # noqa: E402

bulk = filing.bulk
pilot = filing.pilot
require = filing.require
sha = d6.sha
PARENT = d6.ARTIFACTS / "f01-historical-50-preparation-v1" / "first_batch_manifest.json"
PARENT_SHA = "072aa28953bed9fba5de5960b1c134ea13bdafe3600dd80415b91a34d5800e52"
SOURCE_ROOT = d6.RESEARCH / "sec-originals" / "pilot01-20261121-175840"
ARTIFACTS = d6.ARTIFACTS / "f01-task9-ingestion-preparation-v1"
DEFAULT = "0000320187-16-000242"
PHASES = (
    "select",
    "download",
    "verify",
    "taxonomy",
    "extract",
    "source-template",
    "accounting-template",
    "bootstrap-manifest",
    "import",
    "reader",
)
DOCUMENTS_PER_FILING = 35
GIB = 1024**3
MAX_JSON = 64 * 1024**2


def checked(path, digest=None, limit=MAX_JSON):
    raw, data = bulk.read_metadata(path.absolute(), max_bytes=limit)
    require(digest is None or sha(raw) == digest, f"INPUT_HASH_MISMATCH: {path}")
    return raw, data


def publish(path, data):
    raw = d6.json_bytes(data)
    bulk.directory(path.parent)
    if bulk.exists(path):
        existing, _ = checked(path, limit=filing.SHARD_LIMIT)
        require(raw == existing, f"IMMUTABLE_ARTIFACT_CONFLICT: {path}")
        return sha(raw)
    with tempfile.TemporaryDirectory(prefix=".task9-", dir=path.parent) as temp:
        staged = Path(temp) / "content"
        bulk.write_new(staged, raw)
        staged.rename(path)  # Windows same-volume no-replace publication.
    return sha(raw)


def load_parent(path=PARENT, expected=PARENT_SHA):
    raw, parent = checked(path, expected)
    require(
        parent["contract"] == d6.CONTRACT
        and parent["benchmark"] == "SPY"
        and parent["bindings"]["universe"]["sha256"] == d6.D6_SHA[0]
        and parent["bindings"]["sec_metadata"]["sha256"] == d6.D6_SHA[1],
        "D11_PARENT_BINDING_MISMATCH",
    )
    for name, digest in parent["artifacts_sha256"].items():
        require(filing.NAME.fullmatch(name) and ".." not in name, "D11_ARTIFACT_PATH_INVALID")
        artifact = path.parent / name
        require(bulk.plain(artifact).st_size <= MAX_JSON, "D11_ARTIFACT_TOO_LARGE")
        require(sha(artifact.read_bytes()) == digest, f"D11_ARTIFACT_HASH_MISMATCH: {name}")
    stable = {
        k: v for k, v in parent.items() if k not in {"capacity", "artifacts_sha256", "plan_sha256"}
    }
    require(sha(d6.json_bytes(stable)) == parent["plan_sha256"], "D11_PLAN_HASH_MISMATCH")
    return raw, parent


def selected_rows(parent, inventory, accessions):
    require(1 <= len(accessions) == len(set(accessions)) <= 4, "SELECT_ONE_TO_FOUR_ACCESSIONS")
    allowed = defaultdict(list)
    for row in parent["accessions"]:
        allowed[row["accession"]].append(row)
    result = []
    for accession in accessions:
        candidates = allowed.get(accession, [])
        require(len(candidates) == 1, "ACCESSION_OUTSIDE_OR_AMBIGUOUS_IN_PARENT")
        row = candidates[0]
        issuer = inventory[row["symbol"]]
        source = issuer["filings"][accession]
        require(
            row["cik"] == issuer["cik"]
            and not row["filing_index_conflicted"]
            and accession not in issuer["filing_index_conflicts"]
            and all(row[key] == source[key] for key in ("filed", "form", "acceptance"))
            and row["form"] in {"10-K", "10-Q"}
            and row["acceptance"],
            "UNRESOLVED_FILING_METADATA_OR_AMENDMENT_SCOPE",
        )
        review.instant(row["acceptance"])
        result.append(row)
    return sorted(result, key=lambda r: (r["cik"], review.instant(r["acceptance"]), r["accession"]))


def primary_documents(rows, inventory, submissions):
    issuers = {
        row["cik"]: {"cik": row["cik"], "submissions_member": f"CIK{row['cik']}.json"}
        for row in rows
    }
    bulk.plain(submissions)
    with pinned_files({"submissions": submissions}):
        members = pilot.submissions_members({"submissions": submissions}, issuers.values(), set())
    result = []
    for row in rows:
        cik, accession = row["cik"], row["accession"]
        main, digest, history = members[cik]
        original = inventory[row["symbol"]]
        history_hashes = {part["member"]: part["sha256"] for part in history}
        require(
            digest == original["submissions_member_sha256"]
            and history_hashes == original["submissions_history_member_sha256"],
            "D6_SUBMISSIONS_MEMBERS_CHANGED",
        )
        index, conflicts, _ = pilot.filing_index(main, history)
        docs, periods = filing_documents(main, history)
        require(
            accession not in conflicts
            and index.get(accession)
            == (date.fromisoformat(row["filed"]), row["form"], review.instant(row["acceptance"]))
            and len(docs[accession]) == len(periods[accession]) == 1,
            "SUBMISSIONS_FILING_OR_PRIMARY_UNVERIFIED",
        )
        report_date = next(iter(periods[accession]))
        require(
            date.fromisoformat(report_date) <= date.fromisoformat(row["filed"]),
            "INVALID_REPORT_PERIOD",
        )
        result.append(
            {
                "cik": cik,
                "symbol": row["symbol"],
                "accession": accession,
                "filed": row["filed"],
                "form": row["form"],
                "acceptance": row["acceptance"],
                "report_date": report_date,
                "primary": next(iter(docs[accession])),
                "registrant": original["registrant_in_archived_submissions"],
                "submissions_sha256": digest,
                "history_sha256": history_hashes,
                "d11_cache_status": row["physical_status"],
                "source_status": "UNREVIEWED",
                "taxonomy_status": "UNVERIFIED",
                "reporting_entity_status": "REVIEW_REQUIRED",
                "security_status": "TRADING_NOT_QUALIFIED",
                "accounting_status": "UNREVIEWED",
            }
        )
    return filing.validate_rows(result)


def capacity(count, root):
    return {
        "original_upper_bound_bytes": count * DOCUMENTS_PER_FILING * filing.LIMIT,
        "temporary_and_shards_allowance_bytes": count * (filing.SHARD_LIMIT + filing.LIMIT),
        "taxonomy_allowance_bytes": 2 * GIB,
        "request_and_sqlite_allowance_bytes": GIB,
        "reserve_bytes": 2 * GIB,
        "free_bytes": shutil.disk_usage(root).free,
        "backup": "Independent capacity and restore verification required; F: not written",
    }


def check_space(rows, job):
    budget = capacity(len(rows), job)
    require(
        budget["free_bytes"]
        >= sum(v for k, v in budget.items() if k.endswith("_bytes") and k != "free_bytes"),
        "INSUFFICIENT_TASK9_WORKING_CAPACITY",
    )


def build_selection(accessions):
    raw, parent = load_parent()
    universe, _ = checked_csv()
    meta, inventory = checked(d6.REPO / "reports/f01_historical_50_preflight.json", d6.D6_SHA[1])
    _, _, issuer_inventory, _ = d6.d6_inputs(universe, meta)
    source_raw, _ = checked(
        SOURCE_ROOT / "f01_sec_bulk_source_manifest.json",
        parent["bindings"]["historical_source_generation"],
    )
    require(
        sha(source_raw) == inventory["bindings"]["bulk_manifest_sha256"],
        "D6_BULK_SOURCE_GENERATION_MISMATCH",
    )
    rows = primary_documents(
        selected_rows(parent, issuer_inventory, accessions),
        issuer_inventory,
        SOURCE_ROOT / "submissions.zip",
    )
    code = _code_evidence()
    return {
        "contract": review.SELECTION,
        "status": "PREFLIGHT_READY",
        "authorization": "PENDING_MANUAL_APPROVAL",
        "parent_manifest_sha256": sha(raw),
        "parent_batch_id": parent["plan_sha256"],
        "universe_sha256": d6.D6_SHA[0],
        "metadata_sha256": d6.D6_SHA[1],
        "source_generation": sha(source_raw),
        "candidate": {"head": code["git_head"], "fingerprint": code["code_fingerprint"]},
        "filings": rows,
        "planned_target": str(d6.TARGET),
        "original_budget_upper_bound_bytes": len(rows) * DOCUMENTS_PER_FILING * filing.LIMIT,
    }


def checked_csv():
    path = d6.REPO / "reports/f01_historical_50_pilot.csv"
    bulk.plain(path)
    return d6.read_bound(path, d6.D6_SHA[0])


def job_path(path):
    require(
        path is not None
        and path.is_absolute()
        and path.parent == ARTIFACTS
        and path.name.startswith("f01-task9-")
        and filing.NAME.fullmatch(path.name)
        and ".." not in path.name,
        "TASK9_JOB_PATH_REQUIRED",
    )
    bulk.directory(path.parent)
    if bulk.exists(path):
        bulk.directory(path)
    return path


class PersistedSecOpener:
    """Extend the existing SEC client's in-process retry pacing across clean restarts."""

    def __init__(self, state, opener, *, clock=time.time, pause=time.sleep):
        self.state, self.opener, self.clock, self.pause = state, opener, clock, pause

    def save(self, deadline, blocked=None):
        with tempfile.TemporaryDirectory(prefix=".limiter-", dir=self.state.parent) as temp:
            stage = Path(temp) / "state.json"
            bulk.write_new(stage, d6.json_bytes({"until": deadline, "blocked": blocked}))
            if bulk.exists(self.state):
                bulk.plain(self.state)
            os.replace(stage, self.state)

    def open(self, request, *, timeout):
        if bulk.exists(self.state):
            _, state = checked(self.state)
            wait = state["until"] - self.clock()
            require(not state["blocked"] and wait <= 60, "SEC_LIMITER_MANUAL_REVIEW_REQUIRED")
            self.pause(max(0, wait))
        # An interrupted HTTP call has unknown outcome. Retain a bounded hold
        # until the response or the HTTP exception is actually accounted for.
        self.save(self.clock() + 60)
        try:
            result = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            delay = 1
            if error.code in bulk.RETRYABLE:
                header = error.headers.get("Retry-After")
                try:
                    if header and header.isdecimal() and len(header) <= 9:
                        delay = int(header)
                    elif header:
                        delay = max(0, parsedate_to_datetime(header).timestamp() - self.clock())
                    elif error.code == 429:
                        raise ValueError("missing Retry-After")
                except (TypeError, ValueError, OverflowError, OSError):
                    self.save(self.clock(), "UNVERIFIED_RETRY_AFTER")
                    raise
            self.save(self.clock() + max(1, delay))
            raise
        except bulk.TRANSPORT_ERRORS:
            # The existing client handles backoff; the persistent 60-second hold
            # covers failed or ambiguous transports across processes.
            raise
        else:
            self.save(self.clock() + 1)
            return result


@contextmanager
def network(user_agent):
    require(user_agent, "SEC_USER_AGENT_REQUIRED")
    client = bulk.SecHttp(user_agent)
    lock = ARTIFACTS / ".task9-network.lock"
    if bulk.exists(lock):
        bulk.plain(lock)
    with stage_lock(lock):
        client.opener = PersistedSecOpener(ARTIFACTS / "task9-sec-rate-state.json", client.opener)
        yield client


def source_cache(job, row):
    local = job / "originals" / row["cik"] / row["accession"]
    if bulk.exists(local):
        return local
    return filing.ROOT / row["cik"] / row["accession"]


def verify_sources(job, selection, selection_sha):
    result = {}
    for row in selection["filings"]:
        folder = source_cache(job, row)
        raw, listing = filing.cache_read(folder / "index.json", row, "index.json")
        names = dict.fromkeys(
            filing.documents(row, json.loads(raw)) + filing.taxonomy_files(json.loads(raw))
        )
        require(row["primary"] in names, "PRIMARY_DOCUMENT_NOT_VERIFIED")
        documents = [listing]
        for name in names:
            _, source = filing.cache_read(folder / name, row, name)
            documents.append(source)
        for item in documents:
            require(
                item["filed"] == row["filed"]
                and review.instant(item["acceptance"]) == review.instant(row["acceptance"]),
                "CACHE_FILING_AVAILABILITY_MISMATCH",
            )
            review.instant(item["download_completed"])
        result[review.filing_key(row)] = {"directory": str(folder), "documents": documents}
    return {
        "contract": "F01_TASK9_VERIFIED_ORIGINALS_V1",
        "status": "SOURCE_VERIFIED",
        "selection_sha256": selection_sha,
        "source_generation": selection["source_generation"],
        "filings": result,
    }


def taxonomy_seeds(job, row, source):
    from pathlib import PurePosixPath
    from urllib.parse import urlsplit

    folder = Path(source["directory"])
    seeds = set()
    for record in source["documents"]:
        name = record["filename"]
        if name == "index.json":
            continue
        raw, actual = filing.cache_read(folder / name, row, name)
        require(record == actual, "TAXONOMY_ORIGINAL_CHANGED")
        for match in taxonomy.REFERENCE.findall(raw.decode("utf-8", errors="replace")):
            url, _ = urldefrag(match)
            suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
            if (
                suffix in {".xsd", ".xml", ".dtd"}
                and urlsplit(url).netloc != "www.sec.gov"
                or taxonomy.parts(url)
            ):
                seeds.add(url)
    for diagnostic in sorted(job.glob(f"{row['accession']}-quarantine-*.json")):
        _, info = checked(diagnostic, limit=filing.SHARD_LIMIT)
        if info["source_documents"] == source["documents"]:
            seeds.update(taxonomy.issue_urls(info["issues"]))
    return seeds


def taxonomy_report(job, selection, verified, source_sha, *, client=None):
    root = bulk.directory(filing.ARELLE_ROOT)
    acquire = taxonomy.publisher(root, client) if client else None
    results = {}
    for row in selection["filings"]:
        key = review.filing_key(row)
        seeds = taxonomy_seeds(job, row, verified["filings"][key])
        results[key] = taxonomy.coverage(
            seeds, lambda url: taxonomy.bound_input(root, url), acquire
        )
    plugin = filing.verified_sec_plugin(root)
    complete = plugin and all(
        status["status"] == "BOUND" for urls in results.values() for status in urls.values()
    )
    return {
        "contract": "F01_TASK9_TAXONOMY_CLOSURE_V1",
        "source_sha256": source_sha,
        "status": "TAXONOMY_CLOSURE_READY" if complete else "TAXONOMY_PENDING",
        "arelle_version": filing.ARELLE_VERSION,
        "sec_plugin_commit": filing.EDGAR_COMMIT,
        "sec_plugin_verified": bool(plugin),
        "filings": results,
    }


def extract_filing(job, row, source, closure, code, processor):
    key = review.filing_key(row)
    binding = {"source": source, "taxonomy": closure, "code": code}
    binding_hash = sha(d6.json_bytes(binding))
    path = job / f"{row['accession']}-extraction.json"
    if bulk.exists(path):
        _, stored = checked(path, limit=filing.SHARD_LIMIT)
        require(
            stored["binding_sha256"] == binding_hash and stored["status"] == "EXTRACTION_COMPLETE",
            "EXISTING_EXTRACTION_BINDING_CHANGED",
        )
        return stored
    folder = Path(source["directory"])
    listing, _ = filing.cache_read(folder / "index.json", row, "index.json")
    observations, documents, issues, sources = filing.extract_cached(
        row,
        folder,
        json.loads(listing),
        processor,
        extended=True,
    )
    require(
        sorted(sources, key=lambda s: s["filename"])
        == sorted(source["documents"][1:], key=lambda s: s["filename"]),
        "EXTRACTION_SOURCE_SET_CHANGED",
    )
    selected = [record for record in documents if record["selected"]]
    actual_dts = [dep for record in selected for dep in record["taxonomy_documents"]]
    known = {entry["source"]["sha256"] for entry in closure.values()}
    staged = {entry["sha256"] for entry in sources}
    # Only dependencies proved by the original files or reviewed taxonomy inventory are valid.
    if not actual_dts or any(dep.get("sha256") not in known | staged for dep in actual_dts):
        issues.append({"code": "UNBOUND_ACTUAL_DTS", "reason": "Dependency hash not bound"})
    fatal = [issue for issue in issues if issue.get("category") != "INCOMPATIBLE_DOCUMENT_FORMAT"]
    complete = len(selected) == 1 and bool(observations) and not fatal
    shard = {
        "contract": "F01_TASK9_ARELLE_FILING_V1",
        "binding_sha256": binding_hash,
        "filing": key,
        "status": "EXTRACTION_COMPLETE" if complete else "TAXONOMY_PENDING",
        "runtime": processor.release,
        "source_documents": source["documents"],
        "documents": documents,
        "issues": issues,
        "observations": observations,
    }
    if not complete:
        digest = sha(d6.json_bytes(shard))
        publish(job / f"{row['accession']}-quarantine-{digest[:12]}.json", shard)
        raise bulk.Blocked(f"EXTRACTION_OR_DTS_INCOMPLETE: {key}")
    publish(path, shard)
    return shard


def auth_binding(phase, selection_hash, evidence_hash=None):
    binding = {"selection_sha256": selection_hash}
    if evidence_hash is not None:
        binding["evidence_sha256"] = evidence_hash
    return binding


def check_authorization(path, expected, phase, binding):
    require(path and expected, "OPERATION_APPROVAL_FILE_AND_HASH_REQUIRED")
    _, record = checked(path, expected)
    review.authorization(record, phase, binding)


def auth_template(phase, binding):
    return {
        "contract": review.AUTH,
        "operation": phase,
        "binding": binding,
        "decision": "UNREVIEWED",
        "reviewer": "",
        "reviewed_at": "",
    }


def original_record(job, selection_sha, source_sha):
    require(source_sha, "EXPECTED_VERIFIED_ORIGINALS_SHA256_REQUIRED")
    raw, record = checked(job / "verified-originals.json", source_sha)
    require(
        record["contract"] == "F01_TASK9_VERIFIED_ORIGINALS_V1"
        and record["selection_sha256"] == selection_sha
        and record["status"] == "SOURCE_VERIFIED",
        "VERIFIED_ORIGINALS_BINDING_MISMATCH",
    )
    return raw, record


def extraction_record(job, selection, source_sha, extraction_sha):
    require(extraction_sha, "EXPECTED_EXTRACTION_MANIFEST_SHA256_REQUIRED")
    raw, record = checked(job / "extraction-manifest.json", extraction_sha)
    require(
        record["contract"] == "F01_TASK9_EXTRACTION_MANIFEST_V1"
        and record["status"] == "EXTRACTION_COMPLETE"
        and record["source_sha256"] == source_sha
        and record["code_fingerprint"] == selection["candidate"]["fingerprint"],
        "EXTRACTION_MANIFEST_BINDING_MISMATCH",
    )
    return raw, record


def extract_phase(args, job, selection, source, source_sha):
    require(args.taxonomy_report and args.taxonomy_sha256, "EXPECTED_TAXONOMY_REPORT_REQUIRED")
    _, closure = checked(args.taxonomy_report, args.taxonomy_sha256)
    require(
        closure["contract"] == "F01_TASK9_TAXONOMY_CLOSURE_V1"
        and closure["source_sha256"] == source_sha
        and closure["status"] == "TAXONOMY_CLOSURE_READY",
        "TAXONOMY_PENDING",
    )
    if not args.execute:
        return {"status": "PREFLIGHT_READY", "phase": "extract", "source_sha256": source_sha}
    require(
        taxonomy_report(job, selection, source, source_sha) == closure, "TAXONOMY_BYTES_CHANGED"
    )
    code = selection["candidate"]["fingerprint"]
    processor = filing.ArelleProcessor(filing.ARELLE_ROOT)
    require(processor.release["version"] == filing.ARELLE_VERSION, "ARELLE_RUNTIME_CHANGED")
    records = {}
    for row in selection["filings"]:
        key = review.filing_key(row)
        print(f"Extracting or reusing {key}", file=sys.stderr, flush=True)
        shard = extract_filing(
            job, row, source["filings"][key], closure["filings"][key], code, processor
        )
        records[key] = sha(d6.json_bytes(shard))
    result = {
        "contract": "F01_TASK9_EXTRACTION_MANIFEST_V1",
        "status": "EXTRACTION_COMPLETE",
        "source_sha256": source_sha,
        "taxonomy_sha256": args.taxonomy_sha256,
        "code_fingerprint": code,
        "filings": records,
    }
    path = job / "extraction-manifest.json"
    return result | {"manifest_sha256": publish(path, result), "path": str(path)}


def review_binding(selection_sha, source_sha, extraction_sha):
    return {
        "selection_sha256": selection_sha,
        "source_sha256": source_sha,
        "extraction_sha256": extraction_sha,
    }


def reviewed_input(path, digest):
    require(path and digest, "REVIEW_FILE_AND_INDEPENDENT_HASH_REQUIRED")
    return checked(path, digest)[1]


def template(contract, binding, rows):
    return {
        "contract": contract,
        "binding": binding,
        "decision": "UNREVIEWED",
        "reviewer": "",
        "reviewed_at": "",
        "filings": rows,
    }


def load_shards(job, selection, extraction):
    shards = {}
    for row in selection["filings"]:
        key = review.filing_key(row)
        _, shard = checked(
            job / f"{row['accession']}-extraction.json",
            extraction["filings"][key],
            limit=filing.SHARD_LIMIT,
        )
        require(
            shard["filing"] == key and shard["status"] == "EXTRACTION_COMPLETE",
            "EXTRACTION_INCOMPLETE",
        )
        shards[key] = shard
    return shards


def accounting_candidates(shard, row):
    candidates = {}
    for obs in shard["observations"]:
        _, tag = filing.taxonomy(obs.get("concept"))
        metric = review.METRICS.get(tag)
        if metric is None:
            continue
        try:
            fact = review.fact_from_original(row, obs, metric)
        except (ValueError, InvalidOperation, TypeError):
            continue
        digest = review.observation_id(obs)
        candidates[digest] = {
            "decision": "UNREVIEWED",
            "metric": metric,
            "scope": "",
            "version": "",
            "fallback": "NONE",
            "expected_value": str(fact.value),
            "reference": "",
            "original": {
                "qname": obs["concept"],
                "period_end": str(fact.period_end),
                "document": obs.get("source_document"),
                "line": obs.get("source_line"),
                "decimals": obs.get("decimals"),
            },
        }
    return {"facts": candidates}


def bootstrap_manifest(selection, binding):
    scope = {
        "contract": review.IMPORT,
        "universe_sha256": selection["universe_sha256"],
        "source_generation": selection["source_generation"],
        "initial_batch": binding,
        "initial_filings": [[r["cik"], r["accession"]] for r in selection["filings"]],
        "code_fingerprint": selection["candidate"]["fingerprint"],
        "target_lineage": d6.TARGET.parent.name,
        "schema": "F01_CANON_OCC_V1",
        "storage": "isolated; budget/restore separately reviewed",
    }
    return {
        "contract": bootstrap.BOOTSTRAP_CONTRACT,
        "classification": bootstrap.CLASSIFICATION,
        "research_scope": json.dumps(scope, sort_keys=True, separators=(",", ":")),
    }


def compatible_bootstrap(provided, proposed, append):
    if not append:
        require(provided == proposed, "BOOTSTRAP_IS_NOT_BOUND_TO_THIS_BATCH")
        return ()
    require(
        provided["contract"] == bootstrap.BOOTSTRAP_CONTRACT
        and provided["classification"] == bootstrap.CLASSIFICATION,
        "BOOTSTRAP_IS_NOT_TASK9_RESEARCH",
    )
    original = json.loads(provided["research_scope"])
    candidate = json.loads(proposed["research_scope"])
    initial = original.pop("initial_filings")
    subsequent = candidate.pop("initial_filings")
    original.pop("initial_batch")
    candidate.pop("initial_batch")
    require(
        original == candidate
        and initial
        and not {tuple(key) for key in initial} & {tuple(key) for key in subsequent},
        "APPEND_REQUIRES_DISJOINT_SAME_LINEAGE_FILINGS",
    )
    return initial


def verify_originals_unchanged(job, selection, selection_sha, source):
    require(verify_sources(job, selection, selection_sha) == source, "VERIFIED_ORIGINALS_CHANGED")


def import_or_reader(args, job, selection, selection_sha, source, shards, binding):
    source_review = reviewed_input(args.source_review, args.source_review_sha256)
    accounting = reviewed_input(args.accounting_review, args.accounting_review_sha256)
    code = selection["candidate"]["fingerprint"]
    items = []
    for row in selection["filings"]:
        key = review.filing_key(row)
        shard = shards[key]
        documents = source["filings"][key]["documents"]
        require(documents == shard["source_documents"], "ADMISSION_SOURCE_SHARD_MISMATCH")
        facts = review.admitted_facts(row, shard, source_review, accounting, binding)
        request_binding = binding | {
            "source_review_sha256": args.source_review_sha256,
            "accounting_review_sha256": args.accounting_review_sha256,
            "filing": key,
            "retrieved_at": max(
                review.instant(v["download_completed"]) for v in documents
            ).isoformat(),
        }
        items.append((row, facts, request_binding))
    boot = bootstrap_manifest(
        selection,
        binding
        | {
            "source_review_sha256": args.source_review_sha256,
            "accounting_review_sha256": args.accounting_review_sha256,
        },
    )
    if args.phase == "bootstrap-manifest":
        return boot
    require(
        args.bootstrap_manifest and args.bootstrap_sha256, "EXPECTED_BOOTSTRAP_MANIFEST_REQUIRED"
    )
    _, provided = checked(args.bootstrap_manifest, args.bootstrap_sha256)
    predecessors = compatible_bootstrap(provided, boot, args.append)
    if args.phase == "import":
        operation = auth_binding("import", selection_sha, args.accounting_review_sha256) | {
            "source_review_sha256": args.source_review_sha256,
            "bootstrap_sha256": args.bootstrap_sha256,
        }
        if args.execute:
            check_authorization(args.approval, args.approval_sha256, "import", operation)
            verify_originals_unchanged(job, selection, selection_sha, source)
        try:
            report = review.execute(
                d6.TARGET,
                d6.TARGET,
                args.bootstrap_sha256,
                code,
                items,
                write=args.execute,
                predecessors=predecessors,
            )
        except ValueError as error:
            if args.execute:
                quarantine = {
                    "status": "BLOCKED",
                    "phase": "import",
                    "operation_binding": operation,
                    "reason": str(error),
                }
                digest = sha(d6.json_bytes(quarantine))
                publish(job / f"import-quarantine-{digest[:12]}.json", quarantine)
            raise
        return report | {
            "status": "IMPORT_COMPLETE" if args.execute else "IMPORT_READY",
            "authorization_template": auth_template("import", operation),
        }
    if not args.execute:
        report = review.execute(
            d6.TARGET, d6.TARGET, args.bootstrap_sha256, code, items, predecessors=predecessors
        )
        require(all(r["existing_receipt"] for r in report["receipts"]), "READER_RECEIPT_NOT_BOUND")
        return report | {"status": "PREFLIGHT_READY", "phase": "reader"}
    verify_originals_unchanged(job, selection, selection_sha, source)
    review.check_target(d6.TARGET, d6.TARGET)
    boundaries = []
    with pinned_files({"read": d6.TARGET}), review.open_target(d6.TARGET, read_only=True) as con:
        _check_quiescent(d6.TARGET)
        dataset = review.lineage(con, args.bootstrap_sha256, code)
        review.require_predecessors(con, dataset, predecessors)
        for row, facts, request_binding in items:
            found = con.execute(
                "SELECT version,payload_hash FROM ingestion_receipt WHERE request_id=?",
                (review.request_id(dataset, row),),
            ).fetchone()
            require(
                found and found[1] == sha(review.json_bytes(request_binding)),
                "READER_RECEIPT_NOT_BOUND",
            )
            boundaries.append(review.reader_boundary(con, row, facts, found[0]))
    return {
        "status": "VALIDATION_PASSED",
        "scope": "EXACT_FILING_ACCEPTANCE_BOUNDARIES",
        "dataset_id": dataset,
        "boundaries": boundaries,
    }


def run_job(args, job, selection, selection_sha):
    if args.phase == "download":
        binding = auth_binding("download", selection_sha)
        if not args.execute:
            return {
                "status": "ACQUISITION_PENDING",
                "filings": selection["filings"],
                "capacity": capacity(len(selection["filings"]), job.parent),
                "authorization_template": auth_template("download", binding),
            }
        check_authorization(args.approval, args.approval_sha256, "download", binding)
        check_space(selection["filings"], job)
        root = filing.safe_directory(job / "originals")
        with network(args.user_agent) as client:
            filing.acquire(selection["filings"], root, client)
        return {
            "status": "SOURCE_DOWNLOADED",
            "filings": len(selection["filings"]),
            "cache": str(root),
        }
    if args.phase == "verify":
        if not args.execute:
            states = {
                review.filing_key(row): str(source_cache(job, row)) for row in selection["filings"]
            }
            return {"status": "PREFLIGHT_READY", "cache_lookup": states}
        result = verify_sources(job, selection, selection_sha)
        path = job / "verified-originals.json"
        return result | {"manifest_sha256": publish(path, result), "path": str(path)}
    _, source = original_record(job, selection_sha, args.source_sha256)
    if args.phase == "taxonomy":
        binding = auth_binding("taxonomy", selection_sha, args.source_sha256)
        if args.authorize_network:
            require(args.execute, "TAXONOMY_NETWORK_REQUIRES_EXECUTE")
            check_authorization(args.approval, args.approval_sha256, "taxonomy", binding)
            verify_originals_unchanged(job, selection, selection_sha, source)
            check_space(selection["filings"], job)
            with network(args.user_agent) as client:
                result = taxonomy_report(job, selection, source, args.source_sha256, client=client)
        else:
            result = taxonomy_report(job, selection, source, args.source_sha256)
        digest = sha(d6.json_bytes(result))
        path = job / f"taxonomy-{digest[:12]}.json"
        if args.execute:
            publish(path, result)
        return result | {
            "report_sha256": digest,
            "path": str(path) if args.execute else None,
            "authorization_template": auth_template("taxonomy", binding),
        }
    if args.phase == "extract":
        return extract_phase(args, job, selection, source, args.source_sha256)
    _, extraction = extraction_record(job, selection, args.source_sha256, args.extraction_sha256)
    binding = review_binding(selection_sha, args.source_sha256, args.extraction_sha256)
    if args.phase == "source-template":
        return template(
            review.SOURCE,
            binding,
            {
                review.filing_key(row): {
                    "decision": "UNREVIEWED",
                    "issuer_identity": "UNREVIEWED",
                    "completeness": "UNREVIEWED",
                    "amendment": "UNREVIEWED",
                    "reference": "",
                    "source_memberships": [],
                }
                for row in selection["filings"]
            },
        )
    shards = load_shards(job, selection, extraction)
    if args.phase == "accounting-template":
        approved = reviewed_input(args.source_review, args.source_review_sha256)
        review.approved_review(approved, review.SOURCE, binding)
        for row in selection["filings"]:
            review.require_source(row, approved)
        return template(
            review.ACCOUNTING,
            binding
            | {
                "source_review_sha256": sha(review.json_bytes(approved)),
            },
            {
                review.filing_key(row): accounting_candidates(shards[review.filing_key(row)], row)
                for row in selection["filings"]
            },
        )
    return import_or_reader(args, job, selection, selection_sha, source, shards, binding)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--accession", action="append", help="Select one to four D.11 accessions")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--selection", type=Path)
    for option in (
        "approval",
        "taxonomy-report",
        "source-review",
        "accounting-review",
        "bootstrap-manifest",
    ):
        parser.add_argument(f"--{option}", type=Path)
    for option in (
        "selection",
        "approval",
        "source",
        "taxonomy",
        "extraction",
        "source-review",
        "accounting-review",
        "bootstrap",
    ):
        parser.add_argument(f"--{option}-sha256")
    parser.add_argument("--execute", action="store_true", help="Explicitly execute this phase only")
    parser.add_argument(
        "--append", action="store_true", help="Later disjoint batch on same lineage"
    )
    parser.add_argument(
        "--authorize-network", action="store_true", help="Taxonomy acquisition only"
    )
    parser.add_argument("--user-agent", default=os.getenv("TRAWALP_SEC_USER_AGENT"))
    args = parser.parse_args(argv)
    try:
        require(
            not args.authorize_network or args.phase == "taxonomy",
            "NETWORK_FLAG_ONLY_FOR_TAXONOMY_PHASE",
        )
        require(not args.append or args.phase in {"import", "reader"}, "APPEND_PHASE_INVALID")
        if args.phase == "select":
            require(not args.execute, "SELECTION_IS_READ_ONLY")
            result = build_selection(args.accession or [DEFAULT])
        else:
            require(
                args.selection and args.selection_sha256 and not args.accession,
                "HASH_BOUND_SELECTION_REQUIRED",
            )
            _, selection = checked(args.selection, args.selection_sha256)
            require(
                selection == build_selection([row["accession"] for row in selection["filings"]]),
                "SELECTION_OR_CODE_BINDING_CHANGED",
            )
            job = job_path(args.job)
            if args.execute:
                require(
                    args.phase in {"download", "verify", "taxonomy", "extract", "import", "reader"},
                    "PHASE_IS_READ_ONLY",
                )
                if args.phase == "download":
                    check_authorization(
                        args.approval,
                        args.approval_sha256,
                        "download",
                        auth_binding("download", args.selection_sha256),
                    )
                    bulk.SecHttp(args.user_agent)
                if args.phase in {"download", "verify"} and not bulk.exists(job):
                    job.mkdir()
                bulk.directory(job)
                lock = job / ".phase.lock"
                if bulk.exists(lock):
                    bulk.plain(lock)
                with stage_lock(lock):
                    result = run_job(args, job, selection, args.selection_sha256)
            else:
                result = run_job(args, job, selection, args.selection_sha256)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return int(result.get("status") == "TAXONOMY_PENDING") * 3
    except (ValueError, TypeError, KeyError, InvalidOperation) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 3
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
        BadZipFile,
        ET.ParseError,
    ) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("FAILED: INTERRUPTED; rerun the same phase with the same bindings", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
