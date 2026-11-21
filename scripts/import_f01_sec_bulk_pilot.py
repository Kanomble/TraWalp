#!/usr/bin/env python3
"""Offline, explicitly authorized SEC Bulk import for one isolated research lineage."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True
import download_f01_sec_bulk as bulk  # noqa: E402
from prepare_f01_migration import REPO, repository_evidence  # noqa: E402

from trading_system.data import canonical_bootstrap as bootstrap  # noqa: E402
from trading_system.data.canonical_facts import (  # noqa: E402
    COLS,
    CONTRACT,
    KEY,
    MUTABLE,
    Admission,
    RequestIdentity,
    require_contract,
)
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.database import Database, _fact_rows  # noqa: E402
from trading_system.data.sec_persistence import SecRequestEnvelope, provider_cik  # noqa: E402
from trading_system.data.sec_sync_persistence import (  # noqa: E402
    IsolatedCanonicalSec,
    SecSyncPersistence,
    stage_lock,
)
from trading_system.data.xbrl_parser import TAG_ALIASES, parse_company_facts  # noqa: E402
from trading_system.models.fundamentals import CompanyIdentity  # noqa: E402

RESEARCH_ROOT = Path("G:/TraWalpResearch")
GENERATION = "pilot01-20261121-175840"
SOURCE_SHA = "d0392ebbba463b4da8485e786c9986d163e02a57532197fb84c33491b27dbc20"
PILOT_SHA = "9e7fa2b90c1b302228d4c0b12bfa75b7b910630971bd6a690dd00d134d7b6e37"
IMPORT_CONTRACT = "F01_SEC_BULK_PILOT_IMPORT_V1"
ADMISSION_CONTRACT = "F01_SEC_BULK_PILOT_ADMISSION_V1"
IMPORTED = "SEC_BULK_PILOT_IMPORTED"
REPORT = "f01_sec_bulk_pilot_report.json"
STATS = "f01_sec_bulk_pilot_issuer_stats.csv"
AUDIT = "f01_sec_bulk_pilot_import_audit.md"
QUARANTINE = "f01_sec_bulk_pilot_fact_quarantine.json"
ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
NAME = re.compile(r"f01-sec-isolated-research-[a-z0-9][a-z0-9-]{0,47}")
DATASET = re.compile(r"f01-research-[0-9a-f]{32}")
ALIASES = {pair for aliases in TAG_ALIASES.values() for pair in aliases}
TAXONOMIES = sorted({taxonomy for taxonomy, _ in ALIASES})
require = bulk.require


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def code_binding():
    branch = subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "-c",
            f"safe.directory={REPO.as_posix()}",
            "branch",
            "--show-current",
        ],
        cwd=REPO,
        text=True,
    ).strip()
    require(branch == "SEC-integration", "PILOT_REQUIRES_SEC_INTEGRATION_BRANCH")
    evidence = repository_evidence(REPO)
    return {"head": evidence["git_head"], "fingerprint": evidence["code_fingerprint"]}


def pilot_spec():
    raw, spec = bulk.read_metadata(REPO / "docs" / "f01_sec_bulk_pilot_spec.json")
    require(
        sha(raw) == PILOT_SHA
        and spec["core_count"] == len(spec["issuers"]) == 8
        and spec["identity_negative_probe"]["symbol"] == "JSM",
        "PILOT_SPEC_BINDING_MISMATCH",
    )
    return sorted(spec["issuers"], key=lambda issuer: issuer["cik"])


def admission_template(issuers):
    return {
        "contract": ADMISSION_CONTRACT,
        "manifest_sha256": SOURCE_SHA,
        "pilot_spec_sha256": PILOT_SHA,
        "reviewer": "",
        "scope": "RESEARCH_ONLY",
        "issuers": {
            item["cik"]: {
                "symbol": item["symbol"],
                "identity_decision": "PENDING",
                "identity_evidence": {"path": "", "sha256": ""},
                "memberships": {
                    t: {
                        "decision": "PENDING",
                        "canonical_anchor": False,
                        "evidence": {"path": "", "sha256": ""},
                    }
                    for t in TAXONOMIES
                },
            }
            for item in issuers
        },
    }


def reviewed_evidence(record, root, cik, symbol, taxonomy=None):
    path = Path(record["path"])
    require(
        path.is_absolute()
        and path.is_relative_to(root)
        and bulk.SHA256.fullmatch(record["sha256"]),
        "REVIEW_EVIDENCE_PATH_OR_HASH_REQUIRED",
    )
    raw, document = bulk.read_metadata(path)
    require(
        sha(raw) == record["sha256"]
        and document["contract"] == "F01_SEC_BULK_RESEARCH_EVIDENCE_V1"
        and document["cik"] == cik
        and document["symbol"] == symbol
        and document["taxonomy"] == taxonomy
        and document["scope"] == "RESEARCH_ONLY"
        and isinstance(document["source_reference"], str)
        and bool(document["source_reference"].strip()),
        "REVIEW_EVIDENCE_SHA256_MISMATCH",
    )
    return f"{path}#sha256={record['sha256']}"


def load_admission(path, expected_sha, issuers, layout):
    require(path is not None and expected_sha, "INDEPENDENT_REVIEWED_ADMISSION_REQUIRED")
    raw, plan = bulk.read_metadata(path)
    require(
        sha(raw) == expected_sha
        and plan["contract"] == ADMISSION_CONTRACT
        and plan["scope"] == "RESEARCH_ONLY"
        and plan["manifest_sha256"] == SOURCE_SHA
        and plan["pilot_spec_sha256"] == PILOT_SHA
        and isinstance(plan["reviewer"], str)
        and bool(plan["reviewer"].strip())
        and set(plan["issuers"]) == {item["cik"] for item in issuers},
        "ADMISSION_PLAN_BINDING_MISMATCH",
    )
    evidence_root = layout.paths["import_artifacts"]
    result = {}
    for item in issuers:
        cik, symbol = item["cik"], item["symbol"]
        reviewed = plan["issuers"][cik]
        require(
            reviewed["symbol"] == symbol
            and reviewed["identity_decision"] == "ADMIT_RESEARCH"
            and set(reviewed["memberships"]) == set(TAXONOMIES),
            f"RESEARCH_IDENTITY_OR_MEMBERSHIP_REVIEW_REQUIRED: {symbol}",
        )
        identity = reviewed_evidence(reviewed["identity_evidence"], evidence_root, cik, symbol)
        rules = {}
        for taxonomy, decision in reviewed["memberships"].items():
            evidence = reviewed_evidence(decision["evidence"], evidence_root, cik, symbol, taxonomy)
            if decision["decision"] == "DENY":
                require(decision["canonical_anchor"] is False, "DENIED_ANCHOR_CONFLICT")
                rules[taxonomy] = None
            else:
                require(
                    decision["decision"] == "ADMIT_RESEARCH"
                    and decision["canonical_anchor"] is True,
                    f"EXPLICIT_RESEARCH_CANONICAL_ANCHOR_REQUIRED: {symbol}:{taxonomy}",
                )
                rules[taxonomy] = Admission(True, True, evidence)
        result[cik] = (RequestIdentity(symbol, cik, identity), rules)
    return result


def source_inputs(layout, issuers):
    final = bulk.directory(layout.paths["originals"] / GENERATION)
    raw, manifest = bulk.read_metadata(
        final / bulk.MANIFEST_NAME, max_bytes=bulk.MAX_MANIFEST_BYTES
    )
    require(
        sha(raw) == SOURCE_SHA
        and manifest["contract"] == bulk.MANIFEST_CONTRACT
        and manifest["generation"] == GENERATION
        and manifest["quality"] == bulk.QUALITY
        and manifest["reviewed_census"]["pilot_spec_sha256"] == PILOT_SHA
        and manifest["trading_universe_qualified"] is False
        and manifest["qualified_research_issuers"] == [],
        "SOURCE_MANIFEST_BINDING_MISMATCH",
    )
    status = manifest["pilot_structure"]
    require(len(status) == len(issuers), "SOURCE_PILOT_SCOPE_MISMATCH")
    expected = {(item["cik"], item["symbol"]) for item in issuers}
    require(
        {(item["cik"], item["symbol"]) for item in status} == expected,
        "SOURCE_PILOT_SCOPE_MISMATCH",
    )
    for item in status:
        require(
            set(item["endpoints"]) == set(bulk.URLS)
            and all(
                value == {"member": f"CIK{item['cik']}.json", "status": "STRUCTURE_CLEAR"}
                for value in item["endpoints"].values()
            ),
            f"SOURCE_PILOT_STRUCTURE_BLOCKED: {item['symbol']}",
        )
    paths, excluded = {}, {}
    for endpoint, url in bulk.URLS.items():
        entry = manifest["archives"][endpoint]
        path = final / f"{endpoint}.zip"
        integrity = entry["integrity"]
        exclusions = integrity["excluded_members"]
        names = {item["member"] for item in exclusions}
        info = bulk.plain(path)
        require(
            entry["local_path"] == str(path)
            and entry["endpoint"] == endpoint
            and entry["archive_generation"] == f"{GENERATION}/{endpoint}"
            and entry["url"] == url
            and entry["bytes"] == info.st_size
            and integrity["status"] == "VERIFIED"
            and len(names) == len(exclusions) == integrity["excluded_member_count"]
            and all(item["import_eligible"] is False for item in exclusions),
            f"SOURCE_ARCHIVE_BINDING: {endpoint}",
        )
        paths[endpoint], excluded[endpoint] = path, names
    return manifest, paths, excluded


def zip_member(archive, name, endpoint, excluded):
    require(name not in excluded, f"QUARANTINED_SOURCE_MEMBER: {endpoint}:{name}")
    info = archive.getinfo(name)
    require(
        info.orig_filename == name
        and bulk.member_name(info, endpoint)
        and bulk.readable_member(info),
        f"UNSAFE_SOURCE_MEMBER: {endpoint}:{name}",
    )
    raw = bulk.read_zip_member(archive, info)
    # Preserve non-integral numeric lexemes exactly for the Decimal-based parser.
    payload = json.loads(raw, parse_float=str)
    require(isinstance(payload, dict), f"INVALID_SOURCE_PAYLOAD: {endpoint}:{name}")
    return payload, sha(raw)


def zone_time(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.utcoffset() is not None else None


def filing_index(main, history):
    entries = defaultdict(set)
    malformed = 0
    for columns in [main["filings"]["recent"], *(part["payload"] for part in history)]:
        ids, dates, forms = (columns.get(key) for key in ("accessionNumber", "filingDate", "form"))
        stamps = columns.get("acceptanceDateTime")
        if not (
            all(isinstance(item, list) for item in (ids, dates, forms))
            and len(ids) == len(dates) == len(forms)
            and (stamps is None or isinstance(stamps, list) and len(ids) == len(stamps))
        ):
            malformed += 1
            continue
        for index, (accession, declared, form) in enumerate(zip(ids, dates, forms, strict=True)):
            try:
                filed = date.fromisoformat(declared)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if (
                not isinstance(accession, str)
                or not ACCESSION.fullmatch(accession)
                or not (isinstance(form, str) and form)
            ):
                malformed += 1
                continue
            observed = zone_time(stamps[index]) if stamps is not None else None
            entries[accession].add((filed, form, observed))
    conflicted = {
        acc
        for acc, rows in entries.items()
        if len({(filed, form) for filed, form, _ in rows}) != 1
        or len({stamp for _, _, stamp in rows if stamp is not None}) > 1
    }
    reliable = {}
    for accession, rows in entries.items():
        if accession not in conflicted:
            filed, form, _ = next(iter(rows))
            stamp = next((value for _, _, value in rows if value is not None), None)
            reliable[accession] = (filed, form, stamp)
    return reliable, conflicted, malformed


def submissions_members(paths, issuers, exclusions):
    result = {}
    with zipfile.ZipFile(paths["submissions"]) as archive:
        for item in issuers:
            cik = item["cik"]
            main, primary_hash = zip_member(
                archive, item["submissions_member"], "submissions", exclusions
            )
            require(provider_cik(main) == cik, f"SUBMISSIONS_CIK_MISMATCH: {cik}")
            refs = bulk.submissions_filing_index(main)
            require(refs is not None, f"SUBMISSIONS_INDEX_INVALID: {cik}")
            history = {}
            for part in refs:
                name = bulk.submission_reference(part, cik)
                require(name and name not in history, f"SUBMISSIONS_REFERENCE_INVALID: {cik}")
                chunk, chunk_hash = zip_member(archive, name, "submissions", exclusions)
                require(
                    "cik" not in chunk or provider_cik(chunk) == cik,
                    f"SUBMISSIONS_HISTORY_CIK_MISMATCH: {cik}",
                )
                history[name] = {"member": name, "sha256": chunk_hash, "payload": chunk}
            result[cik] = (main, primary_hash, list(history.values()))
    return result


def raw_count(payload):
    totals = Counter()
    for taxonomy, concepts in payload["facts"].items():
        require(isinstance(concepts, dict), "RAW_TAXONOMY_INVALID")
        for tag, concept in concepts.items():
            require(
                isinstance(concept, dict) and isinstance(concept.get("units"), dict),
                "RAW_CONCEPT_UNITS_INVALID",
            )
            for observations in concept["units"].values():
                require(isinstance(observations, list), "RAW_FACTS_INVALID")
                totals["raw_facts"] += len(observations)
                if (taxonomy, tag) in ALIASES:
                    totals["mapped_raw_facts"] += len(observations)
    return totals


def classify_facts(payload, symbol, rules, filings, filing_conflicts):
    counts = raw_count(payload)
    rows = parse_company_facts(payload, symbol, preserve_observations=True)
    counts["normalized_facts"] = len(rows)
    reasons = Counter(
        {
            "OUTSIDE_READER_ALIASES": counts["raw_facts"] - counts["mapped_raw_facts"],
            "UNSUPPORTED_FORM_OR_INVALID_NORMALIZATION": counts["mapped_raw_facts"] - len(rows),
        }
    )
    require(reasons["UNSUPPORTED_FORM_OR_INVALID_NORMALIZATION"] >= 0, "RAW_COUNT_MISMATCH")
    by_key = defaultdict(list)
    for fact, row in zip(rows, _fact_rows(rows), strict=True):
        data = dict(zip(COLS, row, strict=True))
        by_key[tuple(data[field] for field in KEY)].append(
            (fact, tuple(data[field] for field in MUTABLE))
        )
    allowed, exceptions = [], []
    for key, group in by_key.items():
        if len({mutable for _, mutable in group}) != 1:
            reasons["UNORDERED_SAME_KEY_CONFLICT"] += len(group)
            counts["same_key_conflict_groups"] += 1
            exceptions.append(
                {
                    "reason": "UNORDERED_SAME_KEY_CONFLICT",
                    "key": dict(zip(KEY, key, strict=True)),
                    "observations": [fact.model_dump(mode="json") for fact, _ in group],
                }
            )
            continue
        for fact, _ in group:
            accession = fact.accession_number
            filing = filings.get(accession)
            if rules[fact.taxonomy] is None:
                reason = "TAXONOMY_DENIED_BY_REVIEW"
            elif accession in filing_conflicts:
                reason = "SUBMISSIONS_ACCESSION_CONFLICT"
            elif filing is None:
                reason = "SUBMISSIONS_ACCESSION_MISSING"
            elif (fact.filed, fact.form) != filing[:2]:
                reason = "FACT_FILING_FORM_OR_DATE_MISMATCH"
            else:
                allowed.append(fact)
                counts["acceptance_missing_for_admitted_facts"] += int(filing[2] is None)
                continue
            reasons[reason] += 1
            if reason != "TAXONOMY_DENIED_BY_REVIEW":
                exceptions.append({"reason": reason, "observation": fact.model_dump(mode="json")})
    counts["admitted_facts"] = len(allowed)
    counts["excluded_facts"] = sum(reasons.values())
    require(counts["raw_facts"] == len(allowed) + counts["excluded_facts"], "FACT_ACCOUNTING_ERROR")
    return allowed, counts, dict(sorted(reasons.items())), exceptions


@dataclass(frozen=True)
class IssuerPlan:
    request: SecRequestEnvelope
    stats: dict
    exclusions: dict
    quarantine: list
    member_hashes: dict


def issuer_plans(paths, exclusions, source, issuers, admissions, admission_sha, code):
    histories = submissions_members(paths, issuers, exclusions["submissions"])
    observed = zone_time(source["archives"]["companyfacts"]["retrieval_ended_at_utc"])
    submissions_at = zone_time(source["archives"]["submissions"]["retrieval_ended_at_utc"])
    require(observed is not None and submissions_at is not None, "SOURCE_RETRIEVAL_TIME_INVALID")
    plans = []
    with zipfile.ZipFile(paths["companyfacts"]) as archive:
        for item in issuers:
            started = time.monotonic()
            cik, symbol = item["cik"], item["symbol"]
            payload, payload_hash = zip_member(
                archive, item["companyfacts_member"], "companyfacts", exclusions["companyfacts"]
            )
            require(provider_cik(payload) == cik, f"COMPANYFACTS_CIK_MISMATCH: {symbol}")
            main, main_hash, history = histories.pop(cik)
            filings, filing_conflicts, malformed = filing_index(main, history)
            identity, rules = admissions[cik]
            allowed, counts, reasons, quarantine = classify_facts(
                payload, symbol, rules, filings, filing_conflicts
            )
            require(allowed, f"NO_ADMITTED_FACTS_FOR_PILOT_ISSUER: {symbol}")
            name = main.get("name")
            require(
                isinstance(name, str) and name.strip(), f"SUBMISSIONS_COMPANY_NAME_REQUIRED: {cik}"
            )
            company = CompanyIdentity(
                cik=cik,
                symbol=symbol,
                name=name,
                sic=str(main["sic"]) if main.get("sic") else None,
                sic_description=main.get("sicDescription") or None,
            )
            evidence = {
                "manifest_sha256": SOURCE_SHA,
                "source_quality": bulk.QUALITY,
                "companyfacts_member_sha256": payload_hash,
                "submissions_member_sha256": main_hash,
                "companyfacts_retrieved_at": observed.isoformat(),
                "submissions_retrieved_at": submissions_at.isoformat(),
                "companyfacts_float_encoding": "original_decimal_lexeme_as_string",
            }
            require("_f01_bulk_evidence" not in main, "SUBMISSIONS_RESERVED_FIELD")
            request = SecRequestEnvelope.capture(
                observation_id=sha(
                    bulk.json_bytes(
                        [
                            IMPORT_CONTRACT,
                            SOURCE_SHA,
                            PILOT_SHA,
                            admission_sha,
                            code["fingerprint"],
                            cik,
                        ]
                    )
                ),
                company=company,
                identity=identity,
                retrieved_at=observed,
                submissions=main | {"_f01_bulk_evidence": evidence | {"history": history}},
                companyfacts=payload,
                facts=allowed,
                accessions=set(filings) | filing_conflicts,
                parser_version=f"xbrl-preserve-observations:{code['fingerprint']}",
                policy_version=ADMISSION_CONTRACT + ":" + admission_sha,
                admissions={
                    (symbol, taxonomy): rules[taxonomy]
                    for taxonomy in {fact.taxonomy for fact in allowed}
                },
            )
            stats = {
                **counts,
                "symbol": symbol,
                "cik": cik,
                "submissions_accessions": len(filings),
                "submissions_amendments": sum(
                    form.endswith("/A") for _, form, _ in filings.values()
                ),
                "conflicted_accessions": len(filing_conflicts),
                "malformed_submission_rows": malformed,
                "unresolved_conflicts": counts["same_key_conflict_groups"]
                + len(filing_conflicts)
                + reasons.get("FACT_FILING_FORM_OR_DATE_MISMATCH", 0),
                "admitted_instant_facts": sum(fact.period_start is None for fact in allowed),
                "admitted_duration_facts": sum(fact.period_start is not None for fact in allowed),
                "selected_member_bytes": archive.getinfo(item["companyfacts_member"]).file_size,
                "archive_request_bytes": len(request.archive_bytes()),
                "planning_seconds": time.monotonic() - started,
            }
            plans.append(
                IssuerPlan(
                    request,
                    stats,
                    reasons,
                    quarantine,
                    {
                        "companyfacts": {item["companyfacts_member"]: payload_hash},
                        "submissions": {
                            item["submissions_member"]: main_hash,
                            **{part["member"]: part["sha256"] for part in history},
                        },
                    },
                )
            )
    return plans


def bootstrap_target(layout, path):
    require(
        path.is_absolute()
        and path.name == bootstrap.DB_NAME
        and NAME.fullmatch(path.parent.name)
        and path.parent.parent == layout.paths["research_db"]
        and path.is_relative_to(RESEARCH_ROOT),
        "ISOLATED_RESEARCH_TARGET_REQUIRED",
    )
    bulk.directory(path.parent)
    bulk.plain(path)
    _, external = bulk.read_metadata(path.parent / "bootstrap_result.json")
    raw, manifest = bulk.read_metadata(path.parent / "bootstrap_manifest.json")
    lineage = external["lineage"]
    require(
        external["status"] == "PUBLISHED_EMPTY_RESEARCH_DATASET"
        and external["database"] == str(path)
        and external["initial_ingestion_receipts"] == 0
        and bulk.SHA256.fullmatch(external["database_sha256_at_bootstrap"])
        and lineage["classification"] == manifest["classification"] == bootstrap.CLASSIFICATION
        and lineage["bootstrap_contract"] == manifest["contract"] == bootstrap.BOOTSTRAP_CONTRACT
        and lineage["initial_version"] == 1
        and lineage["dataset_origin"] == "EMPTY_CANONICAL_RESEARCH_BOOTSTRAP"
        and lineage["schema_version"] == CONTRACT
        and DATASET.fullmatch(lineage["dataset_id"])
        and lineage["manifest_sha256"] == sha(raw),
        "BOOTSTRAP_REPORT_OR_LINEAGE_MISMATCH",
    )
    return external, raw


def target_receipts(db, external, manifest, requests):
    with db.read_only() as connection:
        require_contract(connection, writer=False)
        expected = external["lineage"]
        row = connection.execute("SELECT evidence,manifest FROM research_bootstrap").fetchone()
        origin = connection.execute(
            "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1"
        ).fetchone()
        version = connection.execute(
            "SELECT current_version,schema_hash FROM fact_contract"
        ).fetchone()
        require(
            row
            and json.loads(row[0]) == expected
            and row[1] == manifest
            and origin
            and tuple(origin) == (expected["dataset_id"], expected["manifest_sha256"])
            and version[1] == external["schema_fingerprint"]
            and not connection.execute(
                """SELECT 1 FROM sqlite_master WHERE name IN
                   ('canonical_migration','canonical_migration_batch','canonical_import_archive')"""
            ).fetchone()
            and not connection.execute(
                "SELECT 1 FROM sync_state WHERE source <> 'sec_accessions'"
            ).fetchone(),
            "TARGET_RESEARCH_CONTRACT_OR_STATE_MISMATCH",
        )
        receipts = [
            tuple(row)
            for row in connection.execute(
                "SELECT request_id,version FROM ingestion_receipt ORDER BY version"
            )
        ]
        expected_receipts = [(request.request_id, i) for i, request in enumerate(requests, 2)]
        datasets = [
            tuple(row)
            for row in connection.execute(
                "SELECT dataset_id,version FROM fact_dataset WHERE version>1 ORDER BY version"
            )
        ]
        require(
            receipts == expected_receipts[: len(receipts)] == datasets
            and version[0] == len(receipts) + 1,
            "TARGET_RECEIPTS_NOT_DETERMINISTIC_PREFIX",
        )
        return len(receipts)


def sqlite_files(path):
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def check_sidecars(path, *, recover=False):
    wal, shm, journal = sqlite_files(path)
    require(not bulk.exists(wal) and not bulk.exists(shm), "UNEXPECTED_WAL_OR_SHM")
    if bulk.exists(journal):
        bulk.plain(journal)
        require(recover, "HOT_JOURNAL_REQUIRES_AUTHORIZED_RESUME")
        # The exclusive stage lock and durable, file-identity-bound import intent
        # are checked before permitting native SQLite rollback recovery.
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()


def capacity(layout, plans, target, *, current_archive_bytes=0):
    budget = layout.budget
    total_archives = sum(p.stats["archive_request_bytes"] for p in plans)
    pending_archives = max(0, total_archives - current_archive_bytes)
    staging_bytes = max(p.stats["archive_request_bytes"] for p in plans)
    require(
        total_archives + staging_bytes <= budget["request_archives"]
        and all(
            budget[key] > 0
            for key in (
                "pilot_database",
                "sqlite_sidecars",
                "request_archives",
                "import_index_work",
            )
        ),
        "PILOT_BUDGET_UNDERPROVISIONED",
    )
    locations = (
        (
            target.parent,
            budget["pilot_database"] + budget["sqlite_sidecars"] + pending_archives + staging_bytes,
        ),
        (
            layout.paths["import_artifacts"],
            budget["import_index_work"] + budget["restore_workspace"],
        ),
        (layout.paths["backup"], budget["backup_generation"]),
    )
    volumes = {}
    for folder, amount in locations:
        volume = volumes.setdefault(
            folder.stat().st_dev,
            {
                "path": str(folder),
                "required_additional_bytes": budget["safety_reserve"],
            },
        )
        volume["required_additional_bytes"] += amount
    for volume in volumes.values():
        volume["free_bytes"] = shutil.disk_usage(volume["path"]).free
        require(
            volume["free_bytes"] >= volume["required_additional_bytes"],
            f"PILOT_VOLUME_CAPACITY_BLOCKED: {volume}",
        )
    return list(volumes.values())


def archive_payload_bytes(path, requests):
    allowed = {request.request_id + ".json" for request in requests}
    total, incomplete = 0, 0
    for item in path.iterdir():
        if re.fullmatch(r"\.sec-[a-z0-9_]{8}", item.name):
            # An interrupted archive.store() can leave an exclusively created
            # staging file. That file is never loaded as a request.
            incomplete += bulk.plain(item).st_size
            continue
        require(item.name in allowed, f"UNEXPECTED_PILOT_REQUEST_ARCHIVE_ENTRY: {item}")
        total += bulk.plain(item).st_size
    return total, incomplete


class DiskSampler:
    """Sample volume usage and SQLite transient files during writer transactions."""

    def __init__(self, target):
        self.path = target
        self.baseline = shutil.disk_usage(target.parent).free
        self.minimum = self.baseline
        self.peak_sidecars = 0
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        self.minimum = min(self.minimum, shutil.disk_usage(self.path.parent).free)
        journal_bytes = 0
        for sidecar in sqlite_files(self.path):
            with suppress(FileNotFoundError):
                journal_bytes += sidecar.stat().st_size
        self.peak_sidecars = max(self.peak_sidecars, journal_bytes)

    def run(self):
        while not self.stop.wait(0.1):
            try:
                self.sample()
            except OSError as error:
                self.error = str(error)
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.sample()

    def report(self):
        return {
            "sample_interval_seconds": 0.1,
            "sampled_volume_peak_consumption_bytes": self.baseline - self.minimum,
            "sampled_sqlite_sidecars_peak_bytes": self.peak_sidecars,
            "sampling_error": self.error,
            "scope": "current invocation; other volume activity may affect the measurement",
        }


def peak_working_set():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "working",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    counters = Counters()
    counters.size = ctypes.sizeof(counters)
    check = kernel.K32GetProcessMemoryInfo
    check.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    check.restype = wintypes.BOOL
    return counters.peak if check(kernel.GetCurrentProcess(), counters, counters.size) else None


def canonical_counts(db, cik):
    with db.read_only() as connection:
        facts = connection.execute(
            "SELECT COUNT(*) FROM canonical_fact WHERE cik=?", (cik,)
        ).fetchone()[0]
        occurrences, memberships = connection.execute(
            """SELECT COALESCE(SUM(n),0), COUNT(*) FROM (
                SELECT COUNT(*) AS n FROM source_occurrence AS o
                JOIN canonical_fact AS f ON f.fact_id=o.fact_id WHERE f.cik=?
                GROUP BY o.fact_id,o.symbol,o.taxonomy
            )""",
            (cik,),
        ).fetchone()
        revisions = connection.execute(
            """SELECT COUNT(*) FROM fact_revision AS r
               JOIN canonical_fact AS f ON f.fact_id=r.fact_id WHERE f.cik=?""",
            (cik,),
        ).fetchone()[0]
    return {
        "canonical_facts": facts,
        "imported_occurrences": occurrences,
        "source_memberships": memberships,
        "revisions": revisions,
    }


def publish_results(run, binding, plans, stats, source, db, resources, elapsed):
    output = run / "result"
    if bulk.exists(output):
        bulk.directory(output)
        _, existing = bulk.read_metadata(output / REPORT, max_bytes=bulk.MAX_MANIFEST_BYTES)
        require(
            existing["status"] == IMPORTED
            and existing["binding"] == binding
            and existing["dataset_version"] == len(plans) + 1
            and set(existing["artifact_sha256"]) == {STATS, AUDIT, QUARANTINE}
            and all(
                file_sha(output / name) == expected
                for name, expected in existing["artifact_sha256"].items()
            ),
            "EXISTING_RESULT_BINDING_MISMATCH",
        )
        return {"status": IMPORTED, "report": str(output / REPORT), "existing_result": True}
    stage = run / f".result-{uuid4().hex[:12]}.incomplete"
    stage.mkdir()
    sums = {
        key: sum(item[key] for item in stats)
        for key in (
            "raw_facts",
            "admitted_facts",
            "excluded_facts",
            "canonical_facts",
            "imported_occurrences",
            "source_memberships",
            "revisions",
            "unresolved_conflicts",
            "duplicate_occurrence_reduction",
            "request_bytes",
            "acceptance_missing_for_admitted_facts",
        )
    }
    db_after = bulk.plain(db.path).st_size
    growth = max(0, db_after - binding["bootstrap_database_bytes"])
    universe = source["archives"]["companyfacts"]["integrity"]["structurally_valid_members"]
    with (stage / STATS).open("x", encoding="utf-8", newline="") as stream:
        fields = list(dict.fromkeys(key for item in stats for key in item))
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(stats)
        stream.flush()
        os.fsync(stream.fileno())
    bulk.write_new(
        stage / QUARANTINE,
        bulk.json_bytes(
            {
                "classification": "NOT_ADMITTED_OR_UNPROVEN",
                "issuers": [
                    {
                        "cik": plan.request.company.cik,
                        "excluded_counts": plan.exclusions,
                        "exceptions": plan.quarantine,
                        "member_sha256": plan.member_hashes,
                    }
                    for plan in plans
                ],
            }
        ),
    )
    text = (
        "# F01 SEC-Bulk-Pilotimport\n\n"
        f"**{IMPORTED}**\n\n"
        f"Dataset {binding['dataset_id']}, Version {len(plans) + 1}. "
        f"{sums['canonical_facts']} Canonical-Facts, "
        f"{sums['imported_occurrences']} Occurrences, "
        f"{sums['excluded_facts']} ausgeschlossene Facts.\n\n"
        "Quellenqualität CURRENT_PROVIDER_RECONSTRUCTION. Filing-Datum, nachgewiesene "
        "Acceptance-Zeit, tatsächlicher Abruf und Dataset-Version sind getrennt. "
        "G5, PIT, historisches Universe und Produktivbetrieb bleiben ohne Freigabe.\n\n"
        "Ungeordnete Same-Key-Konflikte bleiben in den gebundenen Originalen, "
        "Request-Archiven und der Quarantänedatei erhalten. Ressourcen und "
        "Hochrechnung stehen im JSON-Bericht.\n"
    )
    bulk.write_new(stage / AUDIT, text.encode())
    bulk.write_new(
        stage / REPORT,
        bulk.json_bytes(
            {
                "contract": IMPORT_CONTRACT,
                "status": IMPORTED,
                "binding": binding,
                "source_quality": bulk.QUALITY,
                "operational_qualification": False,
                "historical_pit_qualified": False,
                "g5_qualified": False,
                "trading_universe_qualified": False,
                "dataset_version": len(plans) + 1,
                "totals": sums,
                "issuers": stats,
                "resource_samples": resources,
                "database_bytes_before": binding["bootstrap_database_bytes"],
                "database_bytes_after": db_after,
                "elapsed_seconds_this_invocation": elapsed,
                "occurrences_per_second_this_invocation": sum(
                    item["imported_occurrences"] for item in stats if not item["replayed"]
                )
                / max(elapsed, 0.001),
                "pilot_storage_estimate": {
                    "scope": "linear scenario from eight nonrepresentative issuers",
                    "structurally_valid_companyfacts_members_basis": universe,
                    "database_growth_including_indexes_and_metadata_bytes": round(
                        growth * universe / len(plans)
                    ),
                    "request_archive_bytes": round(sums["request_bytes"] * universe / len(plans)),
                    "separate_budget": ["journal", "temp", "backup", "retained_source_archives"],
                },
                "artifact_sha256": {
                    name: file_sha(stage / name) for name in (STATS, AUDIT, QUARANTINE)
                },
            }
        ),
    )
    stage.rename(output)
    return {"status": IMPORTED, "report": str(output / REPORT)}


def run_import(
    layout, db, external, manifest, plans, binding, run, source, *, resume, started, progress
):
    archive_root = db.path.with_name(db.path.name + ".sec-requests")
    archive_root.mkdir(exist_ok=True)
    bulk.directory(archive_root)
    lock_path = archive_root / "stage.lock"
    if bulk.exists(lock_path):
        bulk.plain(lock_path)
    requests = [item.request for item in plans]
    with stage_lock(lock_path):
        if resume:
            bulk.directory(run)
            _, bound = bulk.read_metadata(run / "binding.json")
            require(bound == binding, "PILOT_RESUME_BINDING_MISMATCH")
            check_sidecars(db.path, recover=True)
            target_receipts(db, external, manifest, requests)
        else:
            check_sidecars(db.path)
            require(
                not bulk.exists(run)
                and set(archive_root.iterdir()) == {lock_path}
                and file_sha(db.path) == external["database_sha256_at_bootstrap"]
                and target_receipts(db, external, manifest, requests) == 0,
                "FRESH_BOOTSTRAP_REQUIRED_OR_USE_RESUME",
            )
            intent = run.with_name(f".pilot-intent-{uuid4().hex[:12]}.incomplete")
            intent.mkdir()
            bulk.write_new(intent / "binding.json", bulk.json_bytes(binding))
            intent.rename(run)
        scope = IsolatedCanonicalSec(
            db.path.parent, SecRequestEnvelope.admission_map, ADMISSION_CONTRACT
        )
        writer = SecSyncPersistence(db, scope, force=False)
        require(
            set(archive_root.iterdir()) == {lock_path, archive_root / writer.binding},
            "UNEXPECTED_PILOT_ARCHIVE_NAMESPACE",
        )
        used, incomplete_bytes = archive_payload_bytes(writer.archive.directory, requests)
        capacity(layout, plans, db.path, current_archive_bytes=used)
        stats = []
        with DiskSampler(db.path) as samples:
            for index, plan in enumerate(plans, 1):
                request = plan.request
                writer.require_target()
                prefix = target_receipts(db, external, manifest, requests)
                require(prefix >= index - 1, "PILOT_ORDER_VIOLATION")
                free = shutil.disk_usage(db.path.parent).free
                require(
                    free
                    >= layout.budget["safety_reserve"]
                    + layout.budget["sqlite_sidecars"]
                    + plan.stats["archive_request_bytes"],
                    "PILOT_DISK_RESERVE_REACHED",
                )
                progress(f"[{index}/{len(plans)}] {request.company.cik} {request.company.symbol}")
                issuer_start = time.monotonic()
                before = bulk.plain(db.path).st_size
                with DiskSampler(db.path) as issuer_samples:
                    result = writer.backend.persist(request, expected_version=index)
                require(result.dataset_version == index + 1, "PILOT_WRITER_VERSION_MISMATCH")
                facts = canonical_counts(db, request.company.cik)
                archive_size = bulk.plain(writer.archive._path(request.request_id)).st_size
                after = bulk.plain(db.path).st_size
                seconds = time.monotonic() - issuer_start
                stats.append(
                    {
                        **plan.stats,
                        **facts,
                        "dataset_version": result.dataset_version,
                        "receipt_id": result.receipt_id,
                        "replayed": result.replayed,
                        "duplicate_occurrence_reduction": (
                            plan.stats["admitted_facts"] - facts["imported_occurrences"]
                        ),
                        "acceptance_missing_for_admitted_facts": (
                            plan.stats.get("acceptance_missing_for_admitted_facts", 0)
                        ),
                        "request_bytes": archive_size,
                        "database_bytes_before_operation": before,
                        "database_bytes_after_operation": after,
                        "write_or_replay_seconds": seconds,
                        "sampled_sqlite_sidecars_peak_bytes": issuer_samples.peak_sidecars,
                        "sampled_volume_peak_consumption_bytes": (
                            issuer_samples.baseline - issuer_samples.minimum
                        ),
                        "sampled_disk_error": issuer_samples.error,
                        "occurrences_per_second": (
                            0
                            if result.replayed
                            else facts["imported_occurrences"] / max(seconds, 0.001)
                        ),
                    }
                )
        require(
            target_receipts(db, external, manifest, requests) == len(plans),
            "INCOMPLETE_PILOT_RECEIPTS",
        )
        require(code_binding() == binding["candidate"], "CANDIDATE_CODE_CHANGED_DURING_IMPORT")
        resources = samples.report() | {
            "process_peak_working_set_bytes": peak_working_set(),
            "retained_incomplete_request_archive_bytes": incomplete_bytes,
        }
        return publish_results(
            run, binding, plans, stats, source, db, resources, time.monotonic() - started
        )


def execute_or_preflight(args, issuers, code, progress):
    started = time.monotonic()
    layout_raw, _ = bulk.read_metadata(args.layout)
    layout = bulk.Layout.load(args.layout)
    require(
        all(
            layout.paths[role].is_relative_to(RESEARCH_ROOT)
            for role in (
                "originals",
                "research_db",
                "import_artifacts",
                "temporary",
            )
        ),
        "ISOLATED_RESEARCH_LAYOUT_REQUIRED",
    )
    admissions = load_admission(args.admission, args.admission_sha256, issuers, layout)
    external, bootstrap_manifest = bootstrap_target(layout, args.target)
    source, paths, exclusions = source_inputs(layout, issuers)
    db = Database(args.target)
    lineage = external["lineage"]
    run = layout.paths["import_artifacts"] / f"f01-pilot-{lineage['dataset_id']}"
    identities = {"work": db.path, **paths}
    with (
        pinned_files(identities) as pins,
        pinned_files({}, directories=(layout.paths["import_artifacts"],)),
    ):
        progress("Verifying finalized source manifest and ZIP archive bindings")
        verified = bulk.verify_generation(layout, GENERATION, SOURCE_SHA)
        require(verified["status"] == bulk.REVIEWED_STATUS, "VERIFIED_BULK_ARCHIVES_REQUIRED")
        plans = issuer_plans(
            paths, exclusions, source, issuers, admissions, args.admission_sha256, code
        )
        requests = [plan.request for plan in plans]
        target_info = os.fstat(pins["work"].fileno())
        # The initial size is preserved separately when a resumed DB has grown.
        binding = {
            "contract": IMPORT_CONTRACT,
            "generation": GENERATION,
            "dataset_id": lineage["dataset_id"],
            "target": str(db.path),
            "target_file_identity": [target_info.st_dev, target_info.st_ino],
            "bootstrap_manifest_sha256": sha(bootstrap_manifest),
            "bootstrap_database_sha256": external["database_sha256_at_bootstrap"],
            "schema_fingerprint": external["schema_fingerprint"],
            "candidate": code,
            "manifest_sha256": SOURCE_SHA,
            "pilot_spec_sha256": PILOT_SHA,
            "layout_sha256": sha(layout_raw),
            "admission_sha256": args.admission_sha256,
            "requests": [[request.request_id, request.input_hash] for request in requests],
        }
        if bulk.exists(run):
            bulk.directory(run)
            _, old = bulk.read_metadata(run / "binding.json")
            binding["bootstrap_database_bytes"] = old["bootstrap_database_bytes"]
            require(binding == old, "PILOT_RESUME_BINDING_MISMATCH")
        else:
            binding["bootstrap_database_bytes"] = target_info.st_size
        capacities = capacity(layout, plans, args.target)
        require(code_binding() == code, "CANDIDATE_CHANGED_DURING_PREFLIGHT")
        if args.execute:
            return run_import(
                layout,
                db,
                external,
                bootstrap_manifest,
                plans,
                binding,
                run,
                source,
                resume=args.resume,
                started=started,
                progress=progress,
            )
        check_sidecars(db.path)
        previous = target_receipts(db, external, bootstrap_manifest, requests)
        if not bulk.exists(run):
            require(
                previous == 0 and file_sha(db.path) == external["database_sha256_at_bootstrap"],
                "FRESH_BOOTSTRAP_REQUIRED",
            )
        return {
            "status": "PREFLIGHT_ONLY",
            "binding": binding,
            "volumes": capacities,
            "already_committed": previous,
            "requires_resume": bulk.exists(run),
            "issuers": [item.stats | {"excluded_by_reason": item.exclusions} for item in plans],
            "historical_pit_qualified": False,
            "trading_universe_qualified": False,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--admission-sha256")
    parser.add_argument("--expect-code-fingerprint")
    parser.add_argument("--admission-template", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorize-research-import", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.execute != args.authorize_research_import
        or args.resume
        and not args.execute
        or args.execute
        and not bulk.SHA256.fullmatch(args.expect_code_fingerprint or "")
        or args.admission_template
        and args.execute
        or not args.admission_template
        and (args.layout is None or args.target is None)
        or bool(args.admission) != bool(args.admission_sha256)
    ):
        parser.error("Invalid mode, admission binding or missing explicit write authorization")

    def progress(line):
        print(line, file=sys.stderr, flush=True)

    try:
        issuers = pilot_spec()
        if args.admission_template:
            print(bulk.json_bytes(admission_template(issuers)).decode(), end="")
            return 0
        code = code_binding()
        require(
            not args.expect_code_fingerprint or args.expect_code_fingerprint == code["fingerprint"],
            "CANDIDATE_CODE_FINGERPRINT_MISMATCH",
        )
        report = execute_or_preflight(args, issuers, code, progress)
        print(bulk.json_bytes(report).decode(), end="")
        return 0
    except (ValueError, KeyError, StopIteration, TypeError, zipfile.BadZipFile) as error:
        print(f"Pilot blocked: {error}", file=sys.stderr)
        return 3
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"Pilot failed: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print(
            "Pilot interrupted. Preserve the target, archive and durable run binding.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
