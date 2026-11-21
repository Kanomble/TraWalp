#!/usr/bin/env python3
"""8F.3C. Default preflight reads code and JSON only. See --help for execution."""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
BASE = "27d80392828e10f42f950d98fd0318a4af9d9acf"
BASE_FP = "b4ba5b1c7ea1b477841bee6b990d5a19f2b246610cd88ceb42a75773ed4c7c93"
APPROVAL = "d1a10d358684ca2a2d3962af015e33ea3bb8adcfb66db9fcd9dff82e695a876f"
CONTRACT = "F01_G5_INTEGRATION_EVIDENCE_V1"
INPUT_CONTRACT = "F01_8F3C_REVIEWED_INPUTS_V1"
POLICY = "F01_8F3C_VERIFIED_EXISTING_SOURCE_MEMBERSHIP_V1"
PROBES = (
    "regular_full_and_incremental_sync",
    "canonical_writer_atomicity",
    "commit_failure_and_receipt_replay",
    "cursor_and_stage_recovery",
    "readiness_failure_propagation",
    "schema_and_identity_guards",
)
NEEDED = (
    "Externally reviewed JSON bundle and its independently supplied file SHA-256",
    "Quiescent disposable Canonical and Legacy targets with reviewed lineage, SHA-256 and "
    "full selected test universe; no pending requests, request archives or SQLite sidecars",
    "Archived SEC ticker map, Submissions and Companyfacts for every selected issuer, "
    "with retrieval time, provenance and byte hashes",
    "A later authorized observation with a same-key value correction and an incremental "
    "observation with new Daily Index accessions",
    "Explicit CIK/symbol/taxonomy evidence and decision for each existing Source-Membership; "
    "include authoritative identity evidence, source-row evidence and review hashes",
)


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_file(path):
    path = path.absolute()
    require(
        path.resolve(strict=True) == path and path.is_file() and path.stat().st_nlink == 1,
        f"UNSAFE_FILE: {path}",
    )
    return path


def inventory():
    def git(*args):
        return subprocess.check_output(
            ["git", "--no-optional-locks", "-c", f"safe.directory={ROOT.as_posix()}", *args],
            cwd=ROOT,
            text=True,
        ).strip()

    require(git("branch", "--show-current") == "SEC-integration", "WRONG_CANDIDATE_BRANCH")
    name = SELF.relative_to(ROOT).as_posix()
    git("merge-base", "--is-ancestor", BASE, "HEAD")
    changes = git("diff", "--name-status", "--no-renames", BASE, "--").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    require(set(changes) <= {"A\t" + name} and set(untracked) <= {name}, "CANDIDATE_GIT_DRIFT")
    files = {
        p.relative_to(ROOT).as_posix(): sha(p)
        for directory in ("src", "scripts", "config")
        for p in (ROOT / directory).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml", ".yml", ".sql", ".json"}
        and "__pycache__" not in p.parts
    }
    for filename in ("pyproject.toml", "requirements.txt", ".env"):
        path = ROOT / filename
        if path.is_file():
            files[filename] = sha(path)
    packages = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    runtime = [packages, sys.version, sqlite3.sqlite_version]
    additions = {name: files[name]}
    base = {key: value for key, value in files.items() if key != name}
    require(
        len(base) == 161 and len(packages) == 61 and digest([base, *runtime]) == BASE_FP,
        "8F3B_CODE_CONFIG_OR_RUNTIME_DRIFT",
    )
    return {
        "head": git("rev-parse", "HEAD"),
        "original_8f3b_code_fingerprint": BASE_FP,
        "reviewable_additions_sha256": additions,
        "candidate_code_fingerprint": digest([files, *runtime]),
        "files": len(files),
        "packages": len(packages),
    }


def reviewed_inputs(path, review_sha, candidate):
    require(path is not None and review_sha, "REVIEWED_REAL_INPUTS_REQUIRED")
    path = plain_file(path)
    require(path.suffix.lower() == ".json", "INPUT_REVIEW_MUST_BE_JSON")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == review_sha, "INPUT_REVIEW_HASH_DRIFT")
    plan = json.loads(raw)
    require(
        plan["contract"] == INPUT_CONTRACT
        and plan["candidate"] == candidate
        and plan["material"] == "AUTHORIZED_LOCAL_SEC_OBSERVATIONS"
        and plan["reviewer"]
        and plan["reviewed_at_utc"],
        "INPUT_REVIEW_BINDING_DRIFT",
    )
    approval = json.loads((ROOT / "docs/f01_acceptance_v2_approval.json").read_bytes())
    require(
        digest(approval) == APPROVAL and plan["historical_bindings"] == approval["bindings"],
        "HISTORICAL_APPROVAL_DRIFT",
    )
    require(
        set(plan["targets"]) == {"canonical", "legacy"}
        and all(
            plan["targets"][name]["sha256"] and plan["targets"][name]["lineage"]
            for name in plan["targets"]
        ),
        "DISPOSABLE_TARGET_LINEAGE_REQUIRED",
    )
    require(
        plan["targets"]["canonical"]["lineage"]["work_sha256"]
        == approval["bindings"]["work_sha256"],
        "CANONICAL_LINEAGE_WORK_BINDING_DRIFT",
    )
    historical = plain_file(
        ROOT / "reports/f01_copy_rehearsal/20261115_8f2a/verified/f01_copy_rehearsal_manifest.json"
    )
    historical_raw = historical.read_bytes()
    require(
        hashlib.sha256(historical_raw).hexdigest() == approval["bindings"]["manifest_sha256"]
        and plan["targets"]["legacy"]["lineage"]["source_sha256"]
        == json.loads(historical_raw)["source_sha256"],
        "LEGACY_LINEAGE_SOURCE_BINDING_DRIFT",
    )
    sources = {}
    for name, evidence in plan["evidence_files"].items():
        supplied = Path(evidence["path"])
        require(
            not supplied.is_absolute() and ".." not in supplied.parts,
            "EVIDENCE_MUST_BE_LOCAL_TO_INPUT_BUNDLE",
        )
        file = plain_file(path.parent / supplied)
        require(
            file.suffix.lower() in {".json", ".jsonl", ".csv", ".md", ".txt", ".idx"}
            and evidence["origin"],
            "INVALID_EVIDENCE_FILE",
        )
        body = file.read_bytes()
        require(hashlib.sha256(body).hexdigest() == evidence["sha256"], "LOCAL_EVIDENCE_FILE_DRIFT")
        sources[name] = body.decode("utf-8-sig")
    validate_plan(plan)
    require(
        json.loads(sources[plan["ticker_evidence"]])["ticker_map"] == plan["ticker_map"],
        "TICKER_MAP_SOURCE_DRIFT",
    )
    for batch in plan["observations"].values():
        require(
            set(batch["daily_indexes"]) == set(batch["daily_index_evidence"]),
            "DAILY_INDEX_PROVENANCE_MISSING",
        )
        for name, content in batch["daily_indexes"].items():
            require(
                sources[batch["daily_index_evidence"][name]] == content, "DAILY_INDEX_SOURCE_DRIFT"
            )
        for company in batch["companies"].values():
            recorded = json.loads(sources[company["evidence"]])
            require(
                all(
                    recorded[endpoint] == company[endpoint]
                    for endpoint in ("submissions", "companyfacts")
                ),
                "PAYLOAD_SOURCE_DRIFT",
            )
    for row in plan["memberships"]:
        identity = json.loads(sources[row["identity_evidence"]])
        membership = json.loads(sources[row["membership_evidence"]])
        require(
            any(
                item.get("status") == "VERIFIED"
                and all(item.get(key) == row[key] for key in ("cik", "symbol"))
                for item in identity["rows"]
            ),
            "IDENTITY_SOURCE_NOT_VERIFIED",
        )
        require(
            any(
                all(
                    item.get(key) == row[key]
                    for key in (
                        "cik",
                        "symbol",
                        "taxonomy",
                        "occurrence_id",
                        "source_membership",
                    )
                )
                for item in membership["rows"]
            ),
            "MEMBERSHIP_SOURCE_DRIFT",
        )
    return plan


def validate_plan(plan):
    symbols = plan["symbols"]
    require(
        len(symbols) >= 2 and symbols == sorted(set(symbols)), "NEED_TWO_OR_MORE_DISTINCT_SYMBOLS"
    )
    require(
        set(plan["ticker_map"]) == set(symbols)
        and len(set(plan["ticker_map"].values())) == len(symbols),
        "UNRESOLVED_OR_ALIASED_IDENTITY",
    )
    require(
        set(plan["observations"]) == {"baseline", "corrected", "incremental"},
        "THREE_OBSERVATION_SETS_REQUIRED",
    )
    for batch in plan["observations"].values():
        require(set(batch["companies"]) == set(symbols), "INCOMPLETE_SELECTED_UNIVERSE")
        when = datetime.fromisoformat(batch["retrieved_at"])
        require(when.utcoffset() is not None, "MISSING_RETRIEVAL_TIMEZONE")
        for symbol, company in batch["companies"].items():
            cik = plan["ticker_map"][symbol]
            require(
                company["cik"] == cik and company["evidence"] in plan["evidence_files"],
                "OBSERVATION_PROVENANCE_MISSING",
            )
            for endpoint in ("submissions", "companyfacts"):
                payload = company[endpoint]
                require(
                    str(payload["cik"]).zfill(10) == cik
                    and digest(payload) == company[endpoint + "_fingerprint"],
                    "OBSERVATION_PAYLOAD_BINDING_DRIFT",
                )
    admission_plan(plan)


def admission_plan(plan):
    entries = {}
    for row in plan["memberships"]:
        key = (row["cik"], row["symbol"], row["taxonomy"])
        require(
            key not in entries and plan["ticker_map"].get(row["symbol"]) == row["cik"],
            "DUPLICATE_OR_UNRESOLVED_MEMBERSHIP",
        )
        require(
            row["source_membership"] == "EXISTING_CANONICAL_OCCURRENCE"
            and row["identity_evidence"] in plan["evidence_files"]
            and row["membership_evidence"] in plan["evidence_files"]
            and row["reason"]
            and row["decision"] in {"ADMIT", "BLOCKED"}
            and type(row["legacy_admitted"]) is bool
            and type(row["forward_order_admitted"]) is bool,
            "MEMBERSHIP_EVIDENCE_MISSING",
        )
        entries[key] = row
    require(
        entries and {key[1] for key in entries} == set(plan["symbols"]),
        "NO_COMPLETE_ADMISSION_PLAN",
    )
    return {
        "policy": POLICY,
        "policy_fingerprint": digest([POLICY, sha(SELF)]),
        "inputs_fingerprint": digest(
            [
                plan["ticker_map"],
                plan["evidence_files"],
                plan["memberships"],
                plan["observations"],
                plan.get("targets"),
            ]
        ),
        "decisions": [entries[key] for key in sorted(entries)],
    }


def blocked_memberships(plan):
    runtime_imports()
    from trading_system.data.xbrl_parser import parse_company_facts

    decisions = {
        (row["cik"], row["symbol"], row["taxonomy"]): row["decision"]
        for row in admission_plan(plan)["decisions"]
    }
    gaps = []
    for name, batch in plan["observations"].items():
        for symbol, item in batch["companies"].items():
            keys = {
                (f.cik, f.symbol, f.taxonomy)
                for f in parse_company_facts(item["companyfacts"], symbol)
            }
            if not keys:
                gaps.append([name, item["cik"], symbol, "NO_NORMALIZED_FACTS"])
            gaps.extend([name, *key] for key in sorted(keys) if decisions.get(key) != "ADMIT")
    return gaps


def runtime_imports():
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


class RecordedSec:
    """Endpoint double serving only the exact reviewed local observations."""

    def __init__(self, plan, name):
        self.mapping = plan["ticker_map"]
        self.batch = plan["observations"][name]
        self.companies = {item["cik"]: item for item in self.batch["companies"].values()}

    def ticker_to_cik(self):
        return self.mapping

    def submissions(self, cik):
        return self.companies[cik]["submissions"]

    def company_facts(self, cik):
        return self.companies[cik]["companyfacts"]

    def daily_master_index_directory(self, year, quarter):
        return {
            "directory": {
                "item": [
                    {"name": name, "type": "file"}
                    for name in self.batch["daily_indexes"]
                    if int(name[7:11]) == year and (int(name[11:13]) - 1) // 3 + 1 == quarter
                ]
            }
        }

    def daily_master_index(self, year, quarter, filing_date):
        return self.batch["daily_indexes"]["master." + filing_date.replace("-", "") + ".idx"]


@contextmanager
def no_network():
    def deny(*args, **kwargs):
        raise RuntimeError("NETWORK_FORBIDDEN_IN_8F3C")

    with patch("socket.socket.connect", deny), patch("socket.socket.connect_ex", deny):
        yield


def require_occurrence(connection, row):
    actual = connection.execute(
        "SELECT f.cik,o.symbol,o.taxonomy FROM source_occurrence o "
        "JOIN canonical_fact f ON f.fact_id=o.fact_id WHERE o.id=?",
        (row["occurrence_id"],),
    ).fetchone()
    require(
        actual is not None and tuple(actual) == (row["cik"], row["symbol"], row["taxonomy"]),
        "SOURCE_MEMBERSHIP_TARGET_WITNESS_MISMATCH",
    )


def sync(path, plan, batch, *, mode="full", scoped=None, fault=None):
    runtime_imports()
    from trading_system.data import sec_persistence as persistence
    from trading_system.data.canonical_facts import Admission
    from trading_system.data.database import Database
    from trading_system.data.sec_force import SecWriteDatabase
    from trading_system.data.sec_sync_persistence import PENDING, IsolatedCanonicalSec
    from trading_system.data.sync import DataSynchronizer

    target = Database(path)
    scope = None
    if path.name == "f01-sec-candidate.sqlite3":
        entries = {
            (row["cik"], row["symbol"], row["taxonomy"]): row
            for row in admission_plan(plan)["decisions"]
        }

        def admit(envelope):
            decisions = {}
            with target.read_only() as c:
                for symbol, taxonomy in sorted({(f.symbol, f.taxonomy) for f in envelope.facts}):
                    row = entries.get((envelope.company.cik, symbol, taxonomy))
                    require(row and row["decision"] == "ADMIT", "MEMBERSHIP_BLOCKED")
                    require_occurrence(c, row)
                    decisions[symbol, taxonomy] = Admission(
                        row["legacy_admitted"], row["forward_order_admitted"], digest(row)
                    )
            return decisions

        scope = IsolatedCanonicalSec(path.parent, admit, POLICY)
    elif mode == "force":
        target = SecWriteDatabase(path)

    provider = RecordedSec(plan, batch)

    def clock():
        return datetime.fromisoformat(provider.batch["retrieved_at"])

    ds = DataSynchronizer(
        target,
        None,
        provider,
        clock=clock,
        isolated_canonical_sec=None if mode == "default" else scope,
    )
    original_state = target.set_sync_value
    original_persist = persistence.CanonicalSecPersistence.persist

    def stage(source, key, value, **kwargs):
        if source == PENDING and fault == "A":
            os._exit(73)
        original_state(source, key, value, **kwargs)
        if source == PENDING and fault == "B":
            os._exit(73)
        if fault == "D" and (source, key) == ("dataset", "sec") and kwargs.get("connection"):
            raise sqlite3.OperationalError("INJECTED_STAGE_FINALIZATION_FAILURE")

    def persist(self, *args, **kwargs):
        original_persist(self, *args, **kwargs)
        os._exit(73)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "injected archive capacity failure")

    with no_network(), patch.object(target, "set_sync_value", stage):
        if fault == "C":
            with patch.object(persistence.CanonicalSecPersistence, "persist", persist):
                return ds.sync_sec_full(scoped)
        if fault == "G":
            with patch.object(persistence, "NamedTemporaryFile", disk_full):
                return ds.sync_sec_full(scoped)
        if fault == "F":
            try:
                ds.sync_sec_full(scoped)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    os._exit(75)
                raise
            raise ValueError("CONCURRENT_SYNC_NOT_EXCLUDED")
        if fault in {"before_commit", "after_commit"}:

            class CommitFault(sqlite3.Connection):
                def commit(self):
                    if fault == "after_commit":
                        super().commit()
                    raise sqlite3.OperationalError("INJECTED_COMPANY_COMMIT_FAILURE")

            def connect(self):
                return sqlite3.connect(
                    self.path.as_uri() + "?mode=rw",
                    uri=True,
                    isolation_level=None,
                    factory=CommitFault,
                )

            with patch.object(persistence.CanonicalSecPersistence, "_connect", connect):
                return ds.sync_sec_full(scoped)
        if mode == "incremental":
            return ds.sync_sec_incremental(scoped)
        if mode == "composite":
            return ds.sync_full(scoped)
        return ds.sync_sec_full(scoped, companyfacts_only=mode == "force")


def child(target, plan, fault, symbols):
    # Same interpreter and immutable candidate module. A/B/C exit without finally blocks.
    launcher = (
        "import json,runpy,sys,pathlib; m=runpy.run_path(sys.argv[1]); "
        "job=json.load(sys.stdin); "
        "m['sync'](pathlib.Path(job[0]), job[1], 'baseline', scoped=job[3], fault=job[2])"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", launcher, str(SELF)],
        input=json.dumps([str(target), plan, fault, symbols]),
        text=True,
        check=False,
    )
    require(
        result.returncode == (75 if fault == "F" else 73),
        f"CRASH_BOUNDARY_{fault}_NOT_REACHED: {result.returncode}",
    )


def state(path):
    from trading_system.data.database import Database
    from trading_system.data.sec_sync_persistence import PENDING

    db = Database(path)
    with db.read_only() as c:
        c.execute("BEGIN")
        statuses = {source: {} for source in (PENDING, "dataset", "sec_change_detection")}
        for source, key, value in c.execute(
            "SELECT source,key,value FROM sync_state WHERE source IN (?,?,?)",
            tuple(statuses),
        ):
            statuses[source][key] = json.loads(value)
        pending = statuses[PENDING]
        pending_receipts = {}
        for cik, marker in pending.items():
            receipt = c.execute(
                "SELECT r.request_id,r.input_hash,r.payload_hash,r.version,"
                "d.dataset_id,d.source_hash "
                "FROM ingestion_receipt r JOIN fact_dataset d USING(version) WHERE r.request_id=?",
                (marker["request_id"],),
            ).fetchone()
            if receipt is not None:
                pending_receipts[cik] = tuple(receipt)
        tables = ("source_occurrence", "fact_revision", "ingestion_receipt")
        return {
            "version": c.execute("SELECT current_version FROM fact_contract").fetchone()[0],
            "counts": {
                table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
            },
            "pending": pending,
            "pending_receipts": pending_receipts,
            "stage": statuses["dataset"],
            "cursor": statuses["sec_change_detection"].get("daily_master_index"),
        }


def sidecars(path):
    return [suffix for suffix in ("-wal", "-shm", "-journal") if Path(str(path) + suffix).exists()]


def archive_root(path):
    return path.with_name(path.name + ".sec-requests")


def archive_entries(path):
    return sorted(archive_root(path).glob("*/*.json"))


def backup_and_restore(target, clean_target, root, plan):
    from trading_system.data.sec_sync_persistence import stage_lock

    backup = root / "backup"
    backup.mkdir()
    with stage_lock(archive_root(target) / "stage.lock"):
        require(not sidecars(target), "BACKUP_BLOCKED_BY_SQLITE_SIDECARS")
        original_state = state(target)
        require(original_state["pending_receipts"], "BACKUP_REQUIRES_COMMITTED_PENDING_RECEIPT")
        backup_db = backup / "db.sqlite3"
        shutil.copyfile(target, backup_db)
        require(sha(backup_db) == sha(target) and not sidecars(target), "BACKUP_DATABASE_DRIFT")
        archived = {}
        for path in archive_entries(target):
            relative = path.relative_to(archive_root(target))
            out = backup / "archive" / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)
            archived[relative.as_posix()] = sha(out)
            require(archived[relative.as_posix()] == sha(path), "BACKUP_REQUEST_DRIFT")
        require(
            all(
                any(name.endswith("/" + marker["request_id"] + ".json") for name in archived)
                for marker in original_state["pending"].values()
            ),
            "BACKUP_PENDING_ARCHIVE_GAP",
        )
    require(state(backup_db) == original_state, "BACKUP_LOGICAL_STATE_DRIFT")
    with closing(sqlite3.connect(backup_db.as_uri() + "?mode=ro", uri=True)) as c:
        require(
            c.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            and c.execute("PRAGMA foreign_key_check").fetchone() is None,
            "BACKUP_INTEGRITY_DRIFT",
        )
    destination = root / "f01-sec-isolated-restored"
    destination.mkdir()
    restored = destination / target.name
    shutil.copyfile(backup_db, restored)
    shutil.copytree(backup / "archive", archive_root(restored))

    def require_rejected(path):
        before = state(path)
        result = sync(path, plan, "baseline")
        after = state(path)
        require(
            result["database_failures"] > 0
            and before["pending"] == after["pending"]
            and before["version"] == after["version"]
            and before["counts"] == after["counts"],
            "RESTORE_WRONGLY_ACCEPTED_OLD_PENDING",
        )

    require_rejected(restored)
    # Independently test inode change at the same absolute path with a pending
    # request originally created on that path. Its original archive is retained.
    child(clean_target, plan, "B", None)
    require(not sidecars(clean_target), "SAME_PATH_RESTORE_SIDECARS")
    temporary = clean_target.with_name("restore.new")
    with stage_lock(archive_root(clean_target) / "stage.lock"):
        shutil.copyfile(clean_target, temporary)
        require(
            sha(clean_target) == sha(temporary)
            and clean_target.stat().st_ino != temporary.stat().st_ino,
            "SAME_PATH_RESTORE_COPY_OR_FILE_IDENTITY_DRIFT",
        )
        temporary.replace(clean_target)
    require_rejected(clean_target)
    require(state(target) == original_state, "BACKUP_SOURCE_STATE_DRIFT")
    return {
        "database_sha256": sha(backup_db),
        "archive_files_sha256": archived,
        "pending": original_state["pending"],
        "dataset_version": original_state["version"],
        "receipt_count": original_state["counts"]["ingestion_receipt"],
        "pending_receipts": original_state["pending_receipts"],
        "sidecars": [],
        "changed_path_rejected": True,
        "changed_identity_at_same_path_rejected": True,
        "operational_recovery_blocker": (
            "REVIEWED_PENDING_TRANSFER_OR_SAME_IDENTITY_RESTORE_REQUIRED"
        ),
    }


def expect_failure(operation, message, error_type=ValueError):
    try:
        operation()
    except error_type as exc:
        require(message in str(exc), f"WRONG_FAILURE: {exc}")
    else:
        raise ValueError(f"EXPECTED_FAILURE_NOT_RAISED: {message}")


def readiness_blocked(path, plan):
    from types import SimpleNamespace

    from trading_system.config import load_strategy_config
    from trading_system.data.database import Database
    from trading_system.data.market_sessions import latest_completed_trading_session
    from trading_system.models.screening import ScreenReport
    from trading_system.shadow.readiness import evaluate_shadow_input_readiness

    now = datetime.fromisoformat(plan["observations"]["baseline"]["retrieved_at"])
    db = Database(path)
    inputs = SimpleNamespace(
        sync_values=db.sync_values,
        history_coverage=lambda *_: {},
        bars_on_session=lambda *_: {},
        provenance={},
    )
    screen = SimpleNamespace(
        screen=lambda day: ScreenReport(
            as_of=day,
            generated_at=now.isoformat(),
            analyzed_count=0,
            eligible_count=0,
            records=(),
        )
    )
    result = evaluate_shadow_input_readiness(
        inputs,
        load_strategy_config(),
        now=now,
        session=latest_completed_trading_session(now),
        screen_source=screen,
    )
    require(
        not result.ready
        and {"SEC_ERRORS_PRESENT", "RANKING_UNRESOLVED"} <= set(result.details["blocking_reasons"]),
        "READINESS_DID_NOT_FAIL_CLOSED",
    )


def run_probes(canonical, legacy, root, plan):
    runtime_imports()
    from trading_system.data.database import Database
    from trading_system.data.sec_sync_persistence import PENDING, stage_lock

    db = Database(canonical)
    first = [min(plan["ticker_map"], key=plan["ticker_map"].get)]
    cik = plan["ticker_map"][first[0]]
    initial = state(canonical)
    require(not initial["pending"] and not archive_entries(canonical), "TARGET_MUST_START_CLEAN")
    clean_target = root / "f01-sec-isolated-same-path" / canonical.name
    require(not sidecars(canonical), "SEED_COPY_BLOCKED_BY_SQLITE_SIDECARS")
    clean_target.parent.mkdir()
    shutil.copyfile(canonical, clean_target)
    require(not sidecars(canonical) and sha(canonical) == sha(clean_target), "SEED_COPY_DRIFT")
    child(canonical, plan, "A", first)
    (orphan,) = archive_entries(canonical)
    require(
        not state(canonical)["pending"] and state(canonical)["counts"] == initial["counts"],
        "ARCHIVE_ORPHAN_WRONGLY_CONFIRMED",
    )
    child(canonical, plan, "B", first)
    pending = state(canonical)
    require(
        pending["counts"] == initial["counts"]
        and pending["version"] == initial["version"]
        and pending["pending"][cik]["request_id"] != orphan.stem,
        "PENDING_PRE_FACT_DRIFT",
    )
    good = pending["pending"][cik]
    for bad in ({"expected_version": pending["version"] + 1}, {"input_hash": "0" * 64}):
        # Fault injection only. Restore exactly the original reviewed fixture marker.
        db.set_sync_value(PENDING, cik, good | bad)
        failed = sync(canonical, plan, "baseline")
        require(
            failed["database_failures"] > 0
            and state(canonical)["pending"][cik] == good | bad
            and state(canonical)["counts"] == initial["counts"],
            "INVALID_PENDING_ACCEPTED",
        )
        readiness_blocked(canonical, plan)
        db.set_sync_value(PENDING, cik, good)
    result = sync(canonical, plan, "baseline")
    recovered = state(canonical)
    require(
        not result["errors"]
        and result["resumed_requests"] == 1
        and not recovered["pending"]
        and recovered["cursor"] == initial["cursor"]
        and recovered["stage"]["sec"]["status"] == "partial",
        "MIXED_REPLAY_FRESHNESS_DRIFT",
    )
    result = sync(canonical, plan, "baseline")
    fresh = state(canonical)
    require(
        not result["errors"]
        and fresh["stage"]["sec"]["status"] == "success"
        and fresh["version"] == recovered["version"] + len(plan["symbols"])
        and fresh["counts"]["source_occurrence"] == recovered["counts"]["source_occurrence"]
        and fresh["counts"]["fact_revision"] == recovered["counts"]["fact_revision"],
        "NEW_IDENTICAL_OBSERVATION_DRIFT",
    )
    global_before = fresh["stage"]["sec"]
    child(canonical, plan, "C", first)
    committed = state(canonical)
    require(
        committed["version"] == fresh["version"] + 1 and committed["pending"],
        "POST_RECEIPT_PRE_STAGE_BOUNDARY_MISSING",
    )
    backup = backup_and_restore(canonical, clean_target, root, plan)
    scoped_before = committed["stage"]["sec_scoped"]
    replay = sync(canonical, plan, "baseline", scoped=first)
    repeated = state(canonical)
    require(
        replay["replayed_receipts"] == 1
        and not repeated["pending"]
        and repeated["counts"] == committed["counts"]
        and repeated["version"] == committed["version"]
        and repeated["stage"]["sec_scoped"] == scoped_before
        and repeated["stage"]["sec"] == global_before,
        "CONFIRMED_REPLAY_DRIFT",
    )
    scoped = sync(canonical, plan, "baseline", scoped=first)
    require(
        not scoped["errors"]
        and state(canonical)["stage"]["sec_scoped"]["status"] == "success"
        and state(canonical)["stage"]["sec"] == global_before,
        "SCOPED_ATTESTED_GLOBAL_REFRESH",
    )

    old_projection = projection(canonical, state(canonical)["version"], first[0])
    before_correction = state(canonical)
    corrected = sync(canonical, plan, "corrected")
    correction = state(canonical)
    new_projection = projection(canonical, correction["version"], first[0])
    require(
        not corrected["errors"]
        and old_projection
        and new_projection
        and old_projection.keys() == new_projection.keys()
        and old_projection != new_projection
        and correction["counts"]["fact_revision"] > before_correction["counts"]["fact_revision"]
        and correction["counts"]["source_occurrence"]
        == before_correction["counts"]["source_occurrence"]
        and projection(canonical, before_correction["version"], first[0]) == old_projection,
        "CORRECTION_OR_HISTORICAL_PROJECTION_DRIFT",
    )

    before_d = state(canonical)
    expect_failure(
        lambda: sync(canonical, plan, "incremental", mode="incremental", fault="D"),
        "INJECTED_STAGE_FINALIZATION_FAILURE",
        sqlite3.OperationalError,
    )
    failed_stage = state(canonical)
    require(
        failed_stage["version"] > before_d["version"]
        and failed_stage["pending"]
        and failed_stage["cursor"] == before_d["cursor"]
        and failed_stage["stage"]["sec"]["status"] == "running",
        "NON_ATOMIC_STAGE_FINALIZATION",
    )
    resumed = sync(canonical, plan, "incremental", mode="incremental")
    require(
        resumed["replayed_receipts"] > 0 and state(canonical)["cursor"] == before_d["cursor"],
        "PREMATURE_CURSOR_AFTER_REPLAY",
    )
    clean = sync(canonical, plan, "incremental", mode="incremental")
    require(
        not clean["errors"]
        and state(canonical)["stage"]["sec"]["status"] == "success"
        and state(canonical)["cursor"] != before_d["cursor"],
        "FRESH_INCREMENTAL_RECOVERY_MISSING",
    )

    before_f = state(canonical)
    with stage_lock(archive_root(canonical) / "stage.lock"):
        child(canonical, plan, "F", first)
    require(state(canonical) == before_f, "CONCURRENT_STAGE_CHANGED_TARGET")
    full_disk = sync(canonical, plan, "baseline", scoped=first, fault="G")
    require(
        full_disk["database_failures"] > 0
        and not state(canonical)["pending"]
        and state(canonical)["counts"] == before_f["counts"],
        "ENOSPC_WRONGLY_CONFIRMED",
    )
    failed_commit = sync(canonical, plan, "baseline", scoped=first, fault="before_commit")
    require(
        failed_commit["database_failures"] > 0
        and state(canonical)["pending"]
        and state(canonical)["counts"] == before_f["counts"],
        "PRE_COMMIT_ROLLBACK_DRIFT",
    )
    require(
        not sync(canonical, plan, "baseline", scoped=first)["errors"], "PRE_COMMIT_RECOVERY_FAILED"
    )
    before_lost = state(canonical)
    lost = sync(canonical, plan, "baseline", scoped=first, fault="after_commit")
    after_lost = state(canonical)
    require(
        not lost["errors"]
        and lost["reconciled_commits"] == 1
        and not after_lost["pending"]
        and after_lost["version"] == before_lost["version"] + 1
        and after_lost["counts"]["source_occurrence"] == before_lost["counts"]["source_occurrence"],
        "LOST_COMMIT_CONFIRMATION_NOT_RECONCILED",
    )
    expect_failure(
        lambda: sync(canonical, plan, "baseline", mode="force", scoped=first),
        "SEC_CANONICAL_ISOLATED_TARGET_REQUIRED",
    )
    expect_failure(
        lambda: sync(canonical, plan, "baseline", mode="composite"),
        "SEC_CANONICAL_COMPOSITE_SYNC_FORBIDDEN",
    )
    expect_failure(
        lambda: sync(canonical, plan, "baseline", mode="default"),
        "SEC_SYNC_SCHEMA_SCOPE_MISMATCH",
    )
    before_guards = state(canonical)
    denied = deepcopy(plan)
    for row in denied["memberships"]:
        if row["symbol"] == first[0]:
            row["decision"] = "BLOCKED"
    require(blocked_memberships(denied), "STATIC_ADMISSION_GATE_MISSED_DENIAL")
    rejected = sync(canonical, denied, "baseline", scoped=first)
    require(rejected["database_failures"] == 1, "WRITER_ADMISSION_DENIAL_NOT_PROPAGATED")
    wrong_cik = deepcopy(plan)
    wrong_cik["observations"]["baseline"]["companies"][first[0]]["submissions"]["cik"] = "9999"
    rejected = sync(canonical, wrong_cik, "baseline", scoped=first)
    after_guards = state(canonical)
    require(
        rejected["parse_failures"] == 1
        and after_guards["version"] == before_guards["version"]
        and not after_guards["pending"],
        "PROVIDER_IDENTITY_DENIAL_NOT_PROPAGATED",
    )
    require(not sync(legacy, plan, "baseline")["errors"], "LEGACY_FULL_FAILED")
    legacy_before = Database(legacy).sync_value("dataset", "sec")
    force = sync(legacy, plan, "baseline", mode="force", scoped=first)
    legacy_db = Database(legacy)
    require(
        not force["errors"]
        and legacy_db.sync_value("dataset", "sec") == legacy_before
        and legacy_db.sync_value("dataset", "sec_force_companyfacts")["status"] == "success",
        "FORCE_ATTESTED_GLOBAL_REFRESH",
    )
    return {
        "crash_boundaries": dict.fromkeys("ABCDEFG", "PASS"),
        "backup_restore": backup,
        "replay_suite": "PASS",
        "scope_suite": "PASS",
        "commit_probes": {"before_commit": "PASS", "lost_confirmation": "PASS"},
        "economic_readiness_evaluated": False,
    }


def projection(path, version, symbol):
    from trading_system.data.database import Database

    with Database(path).read_only() as c:
        rows = c.execute(
            "SELECT cik,metric,tag,unit,period_start,period_end,filed,accession_number,frame,value "
            "FROM forward_occ_versions_v1 WHERE dataset_version=? AND symbol=?",
            (version, symbol),
        )
        return {tuple(row[:-1]): row[-1] for row in rows}


def fixture(root):
    runtime_imports()
    from trading_system.data.canonical_facts import build_snapshot
    from trading_system.data.database import Database
    from trading_system.data.xbrl_parser import parse_company_facts
    from trading_system.models.fundamentals import CompanyIdentity
    from trading_system.models.market_data import TradableAsset

    symbols = ["AAA", "BBB"]
    mapping = dict(zip(symbols, ["0000001234", "0000005678"], strict=True))
    canonical = root / "f01-sec-isolated-fixture" / "f01-sec-candidate.sqlite3"
    canonical.parent.mkdir()
    legacy = root / "legacy.sqlite3"

    def batch(values, extra):
        companies = {}
        for symbol, value in zip(symbols, values, strict=True):
            cik = mapping[symbol]
            submissions = {
                "cik": cik,
                "name": symbol,
                "filings": {
                    "recent": {
                        "accessionNumber": ["A", *extra],
                        "form": ["10-Q"] * (1 + len(extra)),
                    }
                },
            }
            companyfacts = {
                "cik": cik,
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2026-01-01",
                                        "end": "2026-03-31",
                                        "val": value,
                                        "accn": "A",
                                        "form": "10-Q",
                                        "filed": "2026-05-05",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
            companies[symbol] = {
                "cik": cik,
                "submissions": submissions,
                "companyfacts": companyfacts,
                "evidence": "fixture",
                "submissions_fingerprint": digest(submissions),
                "companyfacts_fingerprint": digest(companyfacts),
            }
        indexes = {}
        if extra:
            indexes["master.20261120.idx"] = "\n".join(
                [
                    "CIK|Company Name|Form Type|Date Filed|Filename",
                    *(
                        f"{int(cik)}|{symbol}|10-Q|2026-11-20|edgar/data/{int(cik)}/B.txt"
                        for symbol, cik in mapping.items()
                    ),
                ]
            )
        return {
            "retrieved_at": "2026-11-20T22:00:00+00:00",
            "companies": companies,
            "daily_indexes": indexes,
        }

    observations = {
        "baseline": batch([100, 100], []),
        "corrected": batch([120, 100], []),
        "incremental": batch([120, 100], ["B"]),
    }
    for path in (canonical, legacy):
        db = Database(path)
        db.initialize()
        db.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    exchange="NASDAQ",
                    tradable=True,
                    fractionable=True,
                )
                for symbol in symbols
            ]
        )
        for symbol, cik in mapping.items():
            db.upsert_company(CompanyIdentity(cik=cik, symbol=symbol, name=symbol))
            db.upsert_facts(
                parse_company_facts(
                    observations["baseline"]["companies"][symbol]["companyfacts"],
                    symbol,
                )
            )
        if path == canonical:
            with db.connect() as c:
                build_snapshot(c, dataset_id="fixture", source_hash="a" * 64)
        with closing(sqlite3.connect(path)) as c:
            require(
                c.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                "SYNTHETIC_QUIESCENT_FILE_REQUIRED",
            )
    rows = []
    with closing(sqlite3.connect(canonical)) as c:
        for oid, cik, symbol, taxonomy in c.execute(
            "SELECT o.id,f.cik,o.symbol,o.taxonomy FROM source_occurrence o "
            "JOIN canonical_fact f USING(fact_id)"
        ):
            rows.append(
                {
                    "occurrence_id": oid,
                    "cik": cik,
                    "symbol": symbol,
                    "taxonomy": taxonomy,
                    "source_membership": "EXISTING_CANONICAL_OCCURRENCE",
                    "decision": "ADMIT",
                    "identity_evidence": "fixture",
                    "membership_evidence": "fixture",
                    "legacy_admitted": True,
                    "forward_order_admitted": True,
                    "reason": "local fixture",
                }
            )
    plan = {
        "evidence_files": {"fixture": {"origin": "synthetic"}},
        "symbols": symbols,
        "ticker_map": mapping,
        "observations": observations,
        "memberships": rows,
    }
    validate_plan(plan)
    return canonical, legacy, plan


def target_paths(root, plan):
    root = root.absolute()
    require(
        root.resolve(strict=True) == root and root.is_dir() and root.name.startswith("f01-8f3c-"),
        "DEDICATED_REVIEWED_RUN_ROOT_REQUIRED",
    )
    require(
        not root.is_relative_to(ROOT / "reports" / "f01_copy_rehearsal")
        and not root.is_relative_to(ROOT / "data"),
        "HISTORICAL_OR_PRODUCTION_ROOT_FORBIDDEN",
    )
    require(
        not any(
            (root / name).exists()
            for name in (
                "backup",
                "g5-draft",
                "g5-package",
                "f01-sec-isolated-same-path",
                "f01-sec-isolated-restored",
            )
        ),
        "RUN_ROOT_ALREADY_USED",
    )
    targets = []
    for name in ("canonical", "legacy"):
        spec = plan["targets"][name]
        subpath = Path(spec["relative_path"])
        require(not subpath.is_absolute() and ".." not in subpath.parts, "INVALID_TARGET_PATH")
        path = plain_file(root / subpath)
        require(
            path.is_relative_to(root) and not sidecars(path) and not archive_root(path).exists(),
            "UNSAFE_OR_PREVIOUSLY_USED_TARGET",
        )
        require(sha(path) == spec["sha256"], "REVIEWED_TARGET_SHA256_DRIFT")
        targets.append(path)
    canonical, legacy = targets
    require(
        canonical.name == "f01-sec-candidate.sqlite3"
        and canonical.parent.name.startswith("f01-sec-isolated-")
        and legacy != canonical
        and legacy.name != canonical.name,
        "TARGET_SCOPE_MISMATCH",
    )
    require(
        shutil.disk_usage(root).free >= 5 * canonical.stat().st_size + 20 * 1024**3,
        "BACKUP_RESTORE_CAPACITY_BLOCKED",
    )
    runtime_imports()
    from trading_system.data.canonical_facts import require_contract
    from trading_system.data.database import Database
    from trading_system.data.sec_sync_persistence import PENDING

    for path in targets:
        db = Database(path)
        require(
            sorted(db.list_tradable_asset_symbols()) == plan["symbols"], "TARGET_UNIVERSE_DRIFT"
        )
        mapping = db.company_symbol_to_cik()
        require(
            all(mapping.get(s) == plan["ticker_map"][s] for s in plan["symbols"])
            and not set(plan["symbols"]) & db.unresolved_sec_identity_conflict_symbols(),
            "TARGET_IDENTITY_EVIDENCE_BLOCKED",
        )
        require(not db.sync_values(PENDING), "TARGET_PENDING_NOT_EMPTY")
        with db.read_only() as connection:
            if path == canonical:
                require_contract(connection, writer=False)
                for row in admission_plan(plan)["decisions"]:
                    require_occurrence(connection, row)
            else:
                require(
                    connection.execute(
                        "SELECT type FROM sqlite_master WHERE name='fundamental_facts'"
                    ).fetchone()[0]
                    == "table",
                    "LEGACY_TARGET_REQUIRED",
                )
    return targets


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True)
        stream.write("\n")


def draft(root, plan, candidate, result, review_sha):
    binding = {
        key: plan["historical_bindings"][key]
        for key in ("manifest_sha256", "g3_result_fingerprint", "work_sha256")
    }
    binding["approval_fingerprint"] = APPROVAL
    binding["candidate_code_fingerprint"] = candidate["candidate_code_fingerprint"]
    output = root / "g5-draft"
    output.mkdir()
    report = {
        "contract": CONTRACT,
        "binding": binding,
        "scope": "ISOLATED_DATA_SYNCHRONIZER_INTEGRATION",
        "adapter_only": False,
        "status": "PASS",
        "gaps": [],
        "probes": dict.fromkeys(PROBES, "PASS"),
        "candidate": candidate,
        "input_review_sha256": review_sha,
        "admission_plan": admission_plan(plan),
        "evidence": result,
        "production_cutover_authorized": False,
        "operational_release_authorized": False,
        "dimensions": {
            "QUALIFIED_FORWARD_RANKING_PARITY": "UNCHANGED_OPEN",
            "HISTORICAL_IDENTITY_EVIDENCE": "UNCHANGED_OPEN",
            "OPERATIONAL_CHAMPION_READINESS": "UNCHANGED_BLOCKED",
        },
    }
    write_json(output / "f01_g5_integration_report.json", report)
    write_json(
        output / "fingerprint.json", {"contract": CONTRACT, "report_fingerprint": digest(report)}
    )
    return output


def seal_reviewed_draft(directory, review_path, review_sha, candidate):
    directory = directory.absolute()
    require(
        directory.resolve(strict=True) == directory and directory.name == "g5-draft",
        "EXPECTED_G5_DRAFT_DIRECTORY",
    )
    require(review_path and review_sha, "INDEPENDENT_G5_REVIEW_REQUIRED")
    review_path = plain_file(review_path)
    raw_review = review_path.read_bytes()
    require(
        hashlib.sha256(raw_review).hexdigest() == review_sha, "INDEPENDENT_G5_REVIEW_HASH_DRIFT"
    )
    review = json.loads(raw_review)
    report_path = plain_file(directory / "f01_g5_integration_report.json")
    raw_report = report_path.read_bytes()
    report = json.loads(raw_report)
    fingerprint_path = plain_file(directory / "fingerprint.json")
    raw_fingerprint = fingerprint_path.read_bytes()
    require(
        report["contract"] == CONTRACT
        and report["candidate"] == candidate
        and report["binding"]["candidate_code_fingerprint"]
        == candidate["candidate_code_fingerprint"]
        and report["status"] == "PASS"
        and report["probes"] == dict.fromkeys(PROBES, "PASS")
        and not report["gaps"]
        and report["production_cutover_authorized"] is False
        and json.loads(raw_fingerprint)
        == {"contract": CONTRACT, "report_fingerprint": digest(report)},
        "G5_DRAFT_BINDING_DRIFT",
    )
    require(
        review["decision"] == "REVIEWED_ISOLATED_G5_INTEGRATION"
        and review["reviewer"]
        and review["reviewed_at_utc"]
        and review["binding"] == report["binding"]
        and review["report_fingerprint"] == digest(report)
        and review["report_sha256"] == hashlib.sha256(raw_report).hexdigest()
        and review["operational_release_authorized"] is False,
        "INDEPENDENT_G5_REVIEW_DRIFT",
    )
    output = directory.with_name("g5-package")
    require(not output.exists(), "G5_PACKAGE_ALREADY_EXISTS")
    files = {
        report_path.name: raw_report,
        fingerprint_path.name: raw_fingerprint,
        "review.json": raw_review,
    }
    with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".g5-") as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        for name, raw in files.items():
            (staging / name).write_bytes(raw)
        write_json(
            staging / "seal.json", {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
        )
        staging.rename(output)
    return output


def input_template(candidate):
    approval = json.loads((ROOT / "docs/f01_acceptance_v2_approval.json").read_bytes())
    require(digest(approval) == APPROVAL, "HISTORICAL_APPROVAL_DRIFT")
    batch = {
        "retrieved_at": "<ISO-8601 with timezone>",
        "daily_indexes": {},
        "daily_index_evidence": {},
        "companies": {
            "<symbol>": {
                "cik": "<10 digits>",
                "evidence": "<evidence_files key>",
                "submissions": {},
                "submissions_fingerprint": "<canonical JSON SHA-256>",
                "companyfacts": {},
                "companyfacts_fingerprint": "<canonical JSON SHA-256>",
            },
        },
    }
    return {
        "contract": INPUT_CONTRACT,
        "material": "AUTHORIZED_LOCAL_SEC_OBSERVATIONS",
        "reviewer": "<independent reviewer>",
        "reviewed_at_utc": "<UTC>",
        "candidate": candidate,
        "historical_bindings": approval["bindings"],
        "symbols": ["<at least two unique symbols sorted>"],
        "ticker_map": {},
        "ticker_evidence": "<evidence key: JSON object with ticker_map>",
        "targets": {
            "canonical": {
                "relative_path": "f01-sec-isolated-candidate/f01-sec-candidate.sqlite3",
                "sha256": "<copy SHA-256>",
                "lineage": {"work_sha256": approval["bindings"]["work_sha256"]},
            },
            "legacy": {
                "relative_path": "legacy.sqlite3",
                "sha256": "<copy SHA-256>",
                "lineage": {"source_sha256": "<verified preparation Source SHA-256>"},
            },
        },
        "evidence_files": {
            "<key>": {
                "path": "<relative local text file>",
                "sha256": "<file SHA-256>",
                "origin": "<documented source and retrieval provenance>",
            }
        },
        "observations": dict.fromkeys(("baseline", "corrected", "incremental"), batch),
        "memberships": [
            {
                "cik": "<10 digits>",
                "symbol": "<symbol>",
                "taxonomy": "<taxonomy>",
                "source_membership": "EXISTING_CANONICAL_OCCURRENCE",
                "occurrence_id": "<integer, stable on isolated copy>",
                "identity_evidence": "<evidence key: JSON rows with cik/symbol/status=VERIFIED>",
                "membership_evidence": "<key: JSON rows with membership and occurrence ID>",
                "decision": "BLOCKED",
                "reason": "<explicit evidence and review rationale>",
                "legacy_admitted": False,
                "forward_order_admitted": False,
            }
        ],
    }


def parser():
    cli = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Default: JSON and code preflight without SQLite. Manual execution requires "
            "--inputs, an out-of-band --review-sha256, --run-root, --execute and "
            "--authorize-isolated-copies. Run-root must contain reviewed disposable targets. "
            "Historical intake of the G5 draft requires independent review and a seal."
        ),
    )
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument(
        "--self-test", action="store_true", help="Run disposable synthetic fixtures only"
    )
    mode.add_argument(
        "--execute", action="store_true", help="Run the reviewed local offline evidence plan"
    )
    mode.add_argument("--input-template", action="store_true", help="Print the review input shape")
    mode.add_argument(
        "--seal-draft", type=Path, help="Seal a G5 draft against an independent G5 review"
    )
    cli.add_argument("--g5-review", type=Path)
    cli.add_argument("--authorize-isolated-copies", action="store_true")
    cli.add_argument("--inputs", type=Path, help="F01_8F3C_REVIEWED_INPUTS_V1 JSON")
    cli.add_argument("--review-sha256")
    cli.add_argument("--run-root", type=Path)
    return cli


def main(args):
    candidate = inventory()
    if args.input_template:
        print(json.dumps(input_template(candidate), indent=2))
        return 0
    if args.seal_draft:
        output = seal_reviewed_draft(args.seal_draft, args.g5_review, args.review_sha256, candidate)
        print(f"G5_PACKAGE: {output}")
        return 0
    if args.self_test:
        require(
            not any(
                (args.inputs, args.run_root, args.review_sha256, args.authorize_isolated_copies)
            ),
            "SELF_TEST_ACCEPTS_NO_REAL_TARGETS",
        )
        with tempfile.TemporaryDirectory(dir=ROOT / "reports", prefix="f3c-") as directory:
            root = Path(directory)
            canonical, legacy, plan = fixture(root)
            result = run_probes(canonical, legacy, root, plan)
        print(json.dumps({"status": "SYNTHETIC_PASS", "evidence": result}, indent=2))
        return 0
    if args.inputs is None and not args.execute:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "candidate": candidate,
                    "missing_or_unreviewed_inputs": NEEDED,
                },
                indent=2,
            )
        )
        return 2
    plan = reviewed_inputs(args.inputs, args.review_sha256, candidate)
    gaps = blocked_memberships(plan)
    if gaps:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "membership_gaps": gaps,
                    "admission_plan": admission_plan(plan),
                },
                indent=2,
            )
        )
        return 2
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "METADATA_PREFLIGHT_VERIFIED",
                    "candidate": candidate,
                    "admission_plan": admission_plan(plan),
                    "physical_targets_verified": False,
                },
                indent=2,
            )
        )
        return 0
    require(
        args.authorize_isolated_copies and args.run_root,
        "EXPLICIT_EXECUTION_AUTHORIZATION_REQUIRED",
    )
    root = args.run_root.absolute()
    canonical, legacy = target_paths(root, plan)
    result = run_probes(canonical, legacy, root, plan)
    require(inventory() == candidate, "DURING_RUN_CANDIDATE_DRIFT")
    output = draft(root, plan, candidate, result, args.review_sha256)
    print("G5_DRAFT_PENDING_INDEPENDENT_REVIEW", output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(parser().parse_args()))
    except Exception as error:
        print(f"BLOCKED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from None
