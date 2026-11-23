"""Classify measured F01 storage using existing, read-only evidence bindings."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from f01_dependency_census import put_json
from f01_storage_census import REPO, RESEARCH, csv_out

JOB = REPO / "reports/f01_copy_rehearsal/20261115_8f2a"
SCRATCH = JOB / "scratch/census.sqlite3"
PRODUCTION = REPO / "data/trading_system.sqlite3"
BASELINES = REPO / "reports/baselines"
PROTECTED = {
    "MIGRATION_EVIDENCE",
    "BASELINE_EVIDENCE",
    "PROTECTED_RESEARCH_DB",
    "SEC_BULK_ORIGINALS",
    "ORIGINAL_FILINGS",
    "ARELLE_EVIDENCE",
    "ARELLE_RUNTIME_TAXONOMY",
    "RESEARCH_BACKUP",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def bindings():
    result = {}
    manifest = JOB / "verified/f01_copy_rehearsal_manifest.json"
    data = load(manifest)
    (g3_path,) = JOB.glob("g3_runs/*/result/f01_copy_migration_g3_report.json")
    g3 = load(g3_path)["result"]
    for key, dependency in (
        ("source", "G0–G4, G6 input/reference"),
        ("backup", "G0/G6 backup/restore origin"),
        ("work", "G3 committed COMPLETE and G4/G6 reference"),
    ):
        value = (
            (g3["work_path"], g3["work_sha256"], g3_path)
            if key == "work"
            else (data[key], data[key + "_sha256"], manifest)
        )
        path, expected, evidence = value
        result[Path(path)] = (expected, str(evidence), dependency)
    restore = data["restore_evidence"]
    result[Path(restore["path"])] = (
        restore["sha256"],
        str(manifest),
        "G0 restore verification; V1 checks exact physical SHA",
    )
    for p in JOB.glob("acceptance_v2_runs/*/result/f01_acceptance_v2_g6.json"):
        report = load(p)
        if not report.get("g6"):
            continue
        g6 = report["g6"]
        for path, expected, reason in (
            (
                Path(g6["restore_path"]),
                g6["restore_physical_sha256"],
                "G6 Work restore and verified full stream",
            ),
            (
                Path(g6["reference_restore"]["path"]),
                g6["reference_restore"]["physical_sha256"],
                "G6 byte-equal verified backup restore",
            ),
            (
                p.parent.parent / "recovery_rebuild.sqlite3",
                g6["rebuild_physical_sha256"],
                "G6 failpoint witnesses and successful resumed rebuild",
            ),
        ):
            result[path] = (expected, str(p), reason)
    for p in BASELINES.glob("*/snapshot_manifest.json"):
        snapshot = load(p)
        if isinstance(snapshot.get("snapshot_sha256"), str):
            result[p.parent / "snapshot.sqlite3"] = (
                snapshot["snapshot_sha256"],
                str(p),
                "U0 historical market baseline",
            )
    pit_path = REPO / "reports/f01_historical_pit_coverage.json"
    pit = load(pit_path)["bindings"]
    e11 = (
        RESEARCH / "research-databases/f01-sec-isolated-research-accounting-e11/"
        "f01-sec-candidate.sqlite3"
    )
    result[e11] = (
        pit["database_sha256"],
        str(pit_path),
        "E.11 line and historical E.13/PIT replay",
    )
    return result


def inspect_seals():
    seal_paths = sorted(
        {
            *(JOB / "verified").glob("*seal.json"),
            *JOB.glob("*/**/result/*seal.json"),
        }
    )
    result = []
    for path in seal_paths:
        value = load(path)
        result.append(
            {
                "path": str(path),
                "sha256": digest(path.read_bytes()),
                "root_keys": sorted(value) if isinstance(value, dict) else [],
                "scope": "historical seal metadata only; DB members not rehashed",
            }
        )
    return result


def classify(row, refs, bound, unreadable):
    path = Path(row["path"])
    category = row["category"]
    if row["file_type"] == "REPARSE_NOT_TRAVERSED" or path in unreadable:
        return "BLOCKED_UNVERIFIED", "PATH_OR_CONTENTS_NOT_INSPECTABLE", "HIGH"
    if category == "PRODUCTIVE_DATA" or category == "PROTECTED_RESEARCH_DB":
        return "KEEP", "PRODUCTION_OR_ACTIVE_DATASET", "HIGH"
    if path in bound:
        return "KEEP_EVIDENCE", "EXACT_DB_HASH_BINDING", "HIGH"
    if path == SCRATCH:
        if set(refs) - {"scripts/prepare_f01_migration.py"}:
            return "BLOCKED_REFERENCED", "CENSUS_HAS_ADDITIONAL_REFERENCES", "HIGH"
        return ("DELETE_CANDIDATE", "PREPARATION_CENSUS_SCRATCH_CREATED_BY_PREP_ONLY", "MEDIUM")
    if category in PROTECTED:
        return "KEEP_EVIDENCE", "PROTECTED_EVIDENCE_OR_RESTORE_SCOPE", "HIGH"
    if category == "SEC_STAGING":
        return "BLOCKED_REFERENCED", "DOWNLOAD_CHECKPOINT_AND_FINALIZATION_HISTORY", "MEDIUM"
    if (
        row["root"] == "repo"
        and row["file_type"] == "FILE"
        and (
            re.fullmatch(r"\.ruff_cache/\d+\.\d+\.\d+/\d+", row["relative"])
            or row["relative"]
            in {".pytest_cache/v/cache/lastfailed", ".pytest_cache/v/cache/nodeids"}
        )
    ):
        if refs:
            return "BLOCKED_REFERENCED", "DETECTED_FILE_REFERENCE", "MEDIUM"
        return "DELETE_CANDIDATE", "REGENERABLE_TOOL_CACHE_FILE", "LOW"
    if category == "TEST_SCRATCH" and row["file_type"] != "DIRECTORY":
        return "BLOCKED_UNVERIFIED", "TEST_PROVENANCE_AND_REFERENCE_REVIEW_REQUIRED", "MEDIUM"
    return "KEEP", "NO_SAFE_REMOVAL_ESTABLISHED", "UNASSESSED"


def audit_storage(rows, output, measurement, dependency):
    exact = bindings()
    seals = inspect_seals()
    unreadable = {Path(row["path"]) for row in measurement["unreadable"]}
    references = defaultdict(set)
    for frm, to, kind in dependency["text_references"]:
        if kind == "TEXT_BASENAME_CANDIDATE" and Path(to).is_absolute():
            references[Path(to)].add(frm)
    stats, protected_allocated = Counter(), Counter()
    kept_ids = set()
    candidates, top_files, migrations, maintained = [], [], [], []
    for row in rows:
        path = Path(row["path"])
        category = row["category"]
        current_references = sorted(references[path])
        action, reason, risk = classify(row, current_references, exact, unreadable)
        ident = (row["volume_id"], row["file_id"])
        checksum, evidence, function = exact.get(path, ("", "", ""))
        amount = int(row["allocated_bytes"] or 0)
        candidate_bytes = (
            amount
            if action == "DELETE_CANDIDATE"
            and row["hardlink_count"] == "1"
            and row["file_id"] not in ("", "0")
            else 0
        )
        if row["file_type"] != "DIRECTORY":
            logical = int(row["logical_bytes"])
            stats[category] += logical
            if (
                amount
                and ident not in kept_ids
                and (action == "KEEP_EVIDENCE" or category == "PROTECTED_RESEARCH_DB")
            ):
                kept_ids.add(ident)
                protected_allocated[category] += amount
            top_files.append(
                {
                    "file": str(path),
                    "logical_bytes": logical,
                    "category": category,
                    "allocation_bytes": amount or None,
                    "recommendation": action,
                }
            )
        evidence_file = category in {
            "MIGRATION_EVIDENCE",
            "BASELINE_EVIDENCE",
            "PROTECTED_RESEARCH_DB",
            "PRODUCTIVE_DATA",
        }
        entry = {
            **row,
            "workflow": category,
            "proposed_action": action,
            "risk": risk,
            "reason": reason,
            "active_reference_candidates": " | ".join(current_references[:20]),
            "active_reference_candidate_count": len(current_references),
            "historical_evidence": evidence,
            "known_seal_closure": (
                str(JOB) if category == "MIGRATION_EVIDENCE" and path != SCRATCH else evidence
            ),
            "restore_dependency": function
            or ("ASSESS_BEFORE_RELEASE" if action == "DELETE_CANDIDATE" else ""),
            "expected_sha256_from_evidence": checksum,
            "requires_live_sha256_before_any_release": action == "DELETE_CANDIDATE",
            "approval": "PENDING_SEPARATE_AUTHORIZATION"
            if action == "DELETE_CANDIDATE"
            else "NOT_AUTHORIZED",
            "potential_reclaimable_bytes": candidate_bytes,
        }
        if action == "DELETE_CANDIDATE":
            candidates.append(entry)
        elif action in {"BLOCKED_REFERENCED", "BLOCKED_UNVERIFIED"} or (
            evidence_file and row["file_type"] == "SQLITE_OR_SIDECAR"
        ):
            maintained.append(entry)
        if evidence_file and row["file_type"] == "SQLITE_OR_SIDECAR":
            migrations.append(entry)
    for inaccessible in sorted(unreadable - {Path(row["path"]) for row in rows}):
        maintained.append(
            {
                "path": str(inaccessible),
                "file_type": "UNREADABLE_PATH",
                "proposed_action": "BLOCKED_UNVERIFIED",
                "approval": "NOT_AUTHORIZED",
                "reason": "UNREADABLE_PATH_OR_FILE",
            }
        )
    largest = sorted(top_files, key=lambda x: (-x["logical_bytes"], x["file"]))[:10]
    candidates.sort(key=lambda x: (-x["potential_reclaimable_bytes"], x["path"]))
    migrations.sort(key=lambda x: (-int(x["logical_bytes"]), x["path"]))
    csv_out(output / "legacy_migration_inventory.csv", migrations)
    csv_out(output / "cleanup_candidates.csv", candidates, fields=list(migrations[0]))
    summary = {
        "potential_reclaimable_bytes": sum(r["potential_reclaimable_bytes"] for r in candidates),
        "authorized_reclaimable_bytes": 0,
        "historically_protected_observed_allocated_bytes": sum(protected_allocated.values()),
        "protected_allocation_by_category": dict(protected_allocated),
        "logical_bytes_by_category": dict(stats),
        "top_10_files": largest,
        "top_10_candidates": candidates[:10],
        "candidate_paths": len(candidates),
        "database_and_sidecar_paths": len(migrations),
        "missing_bound_paths": sorted(str(p) for p in exact if not p.is_file()),
        "known_seals": seals,
    }
    put_json(
        output / "cleanup_manifest.json",
        {
            "contract": "F01_CLEANUP_REVIEW_ONLY_V1",
            "destructive_action_authorized": False,
            "candidates": candidates,
            "sensitive_or_blocked_entries": maintained,
            "future_review_required": [
                "exact absolute paths",
                "match volume and file ID and size",
                "required reviewed SHA-256",
                "no active or historical references",
                "all recoverable copies and audit seals preserved",
                "no unexpected SQLite sidecars",
                "no open or concurrent handles",
                "recheck actual free-space change",
            ],
            "storage_summary": summary,
            "size_definition": "Allocated NTFS bytes for observed single-link candidates",
        },
    )
    return summary, migrations
