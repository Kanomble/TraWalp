#!/usr/bin/env python3
"""Isolated G3 wrapper. Default is a read-only preflight; --execute is required.

Only the existing execute_batches implementation migrates data. The sealed G0-G2
manifest is never edited, extended or reserialized for executor binding.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GIB = 1024**3
ADDITIONS = frozenset(
    {
        "scripts/run_f01_migration.py",
        "docs/f01_migration_rehearsal_script.md",
        "tests/test_run_f01_migration.py",
    }
)
SEAL_FILES = frozenset(
    {
        "f01_copy_rehearsal_manifest.json",
        "f01_preparation_report.json",
        "f01_real_migration_preparation_audit.md",
    }
)


class Blocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    root: Path = ROOT
    verified: Path = ROOT / "reports/f01_copy_rehearsal/20261115_8f2a/verified"
    approval: Path = ROOT / "reports/f01_copy_migration_additions_review.json"

    @property
    def job(self):
        return self.verified.parent


def stamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def safe(path, *, file=True):
    if not path.is_absolute() or ".." in path.parts:
        raise Blocked("ABSOLUTE_PATH_REQUIRED: " + str(path))
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise Blocked("LINKED_PATH: " + str(part))
    if file and (not path.is_file() or path.stat().st_nlink != 1):
        raise Blocked("MISSING_OR_LINKED_FILE: " + str(path))
    return path


def sha(path, monitor=None):
    safe(path)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if monitor:
                monitor.check()
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    safe(path)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def new_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def bound_repo_path(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise Blocked("INVALID_REPOSITORY_BINDING: " + relative)
    return safe(root / rel)


def verify_review(review):
    if (
        not isinstance(review, dict)
        or not review.get("reviewer")
        or any(
            review.get(k) is not True
            for k in (
                "repository_consumers_reviewed",
                "external_inventory_complete",
                "inputs_complete",
            )
        )
        or review.get("unknown_consumers") != []
        or not isinstance(review.get("external_files"), list)
    ):
        raise Blocked("INCOMPLETE_CONSUMER_INPUT_EVIDENCE")


def git_changes(root, base):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "safe.directory=" + root.as_posix(), *args],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout

    git("cat-file", "-e", base + "^{commit}")
    records = git("diff", "--name-status", "--no-renames", "-z", base, "--").decode().split("\0")
    changed = []
    for index in range(0, len(records) - 1, 2):
        status, path = records[index : index + 2]
        if status != "A" or path not in ADDITIONS:
            raise Blocked(f"NON_ADDITIVE_GIT_DRIFT: {status} {path}")
        changed.append(path)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    for path in filter(None, untracked):
        if path not in ADDITIONS:
            raise Blocked("UNREVIEWED_ADDITION: " + path)
        changed.append(path)
    return sorted(set(changed)), git("rev-parse", "HEAD").decode().strip()


def verify_bundle(policy, monitor):
    """Only small JSON/text/code inputs here. No database is opened by this gate."""
    if policy.verified.name != "verified":
        raise Blocked("UNPUBLISHED_PREPARATION_DIRECTORY")
    seal = read_json(policy.verified / "seal.json")
    if set(seal) != SEAL_FILES:
        raise Blocked("INCOMPLETE_OR_UNKNOWN_SEAL_ENTRIES")
    for name, expected in seal.items():
        if sha(policy.verified / name, monitor) != expected:
            raise Blocked("SEAL_HASH_MISMATCH: " + name)
    manifest = read_json(policy.verified / "f01_copy_rehearsal_manifest.json")
    report = read_json(policy.verified / "f01_preparation_report.json")
    if (
        report.get("status") != "G0_G2_READY"
        or report.get("execution_enabled") is not False
        or report.get("migration_executed") is not False
        or report.get("expected_counts") != manifest.get("expected_counts")
    ):
        raise Blocked("G0_G2_READY_EVIDENCE_MISMATCH")
    if (
        manifest.get("contract") != "F01_CANON_OCC_V1"
        or manifest.get("executor_version") != "F01_BATCH_EXECUTOR_V1"
    ):
        raise Blocked("INCOMPATIBLE_EXECUTOR_CONTRACT")
    for field in (
        "sql_schema_hash",
        "fact_stream_hash",
        "expected_counts",
        "file_identities",
        "capacity_budget",
        "restore_evidence",
        "input_inventory",
        "consumer_inventory",
    ):
        if not manifest.get(field):
            raise Blocked("MISSING_MANIFEST_EVIDENCE: " + field)
    counts = manifest["expected_counts"]
    for field in (
        "occurrences",
        "canonical",
        "source_memberships",
        "source_taxonomy_memberships",
        "alias_groups",
        "instant",
        "duration",
        "canonical_instant",
        "canonical_duration",
        "content_conflicts",
    ):
        if type(counts.get(field)) is not int or counts[field] < 0:
            raise Blocked("MISSING_COUNT_AUTHORITY: " + field)
    if counts["content_conflicts"] != 0:
        raise Blocked("UNRESOLVED_CONTENT_CONFLICTS")
    expected_paths = {
        "source": policy.job / "reference/source.sqlite3",
        "backup": policy.job / "reference/backup.sqlite3",
        "work": policy.job / "work/canonical_rehearsal.sqlite3",
    }
    for key, path in expected_paths.items():
        if Path(manifest[key]) != path:
            raise Blocked("UNDOCUMENTED_DATABASE_PATH: " + key)
    if Path(manifest["restore_evidence"]["path"]) != policy.job / "restore/restore.sqlite3":
        raise Blocked("UNDOCUMENTED_RESTORE_PATH")
    bound = manifest["input_inventory"]["repository"]
    for name, expected in bound["files"].items():
        if sha(bound_repo_path(policy.root, name), monitor) != expected:
            raise Blocked("BOUND_PRODUCTION_OR_CONFIG_DRIFT: " + name)
    packages = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    if (
        packages != bound["runtime_packages"]
        or canonical_digest([bound["files"], packages, sys.version, sqlite3.sqlite_version])
        != bound["code_fingerprint"]
    ):
        raise Blocked("BOUND_RUNTIME_OR_FINGERPRINT_DRIFT")
    reviews = [
        manifest["input_inventory"]["external_review"],
        manifest["consumer_inventory"]["external_review"],
    ]
    if reviews[0] != reviews[1]:
        raise Blocked("CONSUMER_INPUT_REVIEW_DISAGREEMENT")
    for review in reviews[:1]:
        verify_review(review)
        if (
            review.get("git_head") != bound["git_head"]
            or review.get("code_fingerprint") != bound["code_fingerprint"]
        ):
            raise Blocked("ORIGINAL_REVIEW_BINDING_MISMATCH")
        for entry in review["external_files"]:
            if sha(Path(entry["path"]), monitor) != entry["sha256"]:
                raise Blocked("BOUND_EXTERNAL_INPUT_DRIFT: " + entry["path"])
    if manifest["restore_evidence"]["verified_inventory_fingerprint"] != canonical_digest(
        manifest["input_inventory"]["database"]
    ):
        raise Blocked("RESTORE_INVENTORY_BINDING_MISMATCH")
    approval = read_json(policy.approval)
    if (
        approval.get("review_status") != "REVIEWED_ADDITIVE_ONLY"
        or approval.get("manifest_sha256") != seal["f01_copy_rehearsal_manifest.json"]
        or approval.get("base_git_head") != bound["git_head"]
        or set(approval.get("files", {})) != ADDITIONS
        or not approval.get("reviewer")
    ):
        raise Blocked("ADDITIVE_REVIEW_MISSING_OR_MISMATCH")
    for name, expected in approval["files"].items():
        if name in bound["files"] or any(
            Path(e["path"]) == policy.root / name for e in reviews[0]["external_files"]
        ):
            raise Blocked("ADDITION_WOULD_REPLACE_BOUND_INPUT: " + name)
        if sha(bound_repo_path(policy.root, name), monitor) != expected:
            raise Blocked("ADDITIVE_REVIEW_HASH_MISMATCH: " + name)
    # Existing fingerprint rules discover *all* currently present source/config files.
    spec = importlib.util.spec_from_file_location(
        "_f01_g3_prep_tools", policy.root / "scripts/prepare_f01_migration.py"
    )
    prep = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = prep
    spec.loader.exec_module(prep)
    current = prep.repository_evidence(policy.root)
    if set(current["files"]) - set(bound["files"]) - ADDITIONS:
        raise Blocked("UNREVIEWED_NEW_PRODUCTION_OR_INPUT_FILE")
    changed, current_head = git_changes(policy.root, bound["git_head"])
    engine = prep.migration
    if engine.EXECUTION_ENABLED is not False:
        raise Blocked("EXECUTOR_ALREADY_ENABLED")
    return (
        manifest,
        prep,
        dict(
            seal=seal,
            original_head=bound["git_head"],
            current_head=current_head,
            reviewed_additions=approval,
            git_additions=changed,
        ),
    )


def tree_bytes(path):
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            raise Blocked("LINKED_SCRATCH_PATH")
        if p.is_file():
            total += p.stat().st_size
    return total


class Monitor:
    """Sampler is independent of SQLite VM callbacks, including during index builds."""

    def __init__(self, policy):
        self.policy = policy
        self.start = time.monotonic()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.error = None
        self.connection = None
        self.prep = None
        self.manifest = None
        self.phase = "G0-G2 / Code-/Manifest-Bindung"
        self.rows = self.batches = 0
        self.state = "INITIAL"
        self.initial_rows = 0
        self.import_started = None
        self.logfile = None
        self.run_dir = None
        self.events = []
        self.thread = None
        self.lock = threading.Lock()

    def stop(self, error):
        if self.error is None:
            self.error = error
        self.cancel.set()
        c = self.connection
        if c is not None:
            with suppress(sqlite3.Error):
                c.interrupt()

    def check(self):
        if self.cancel.is_set():
            raise self.error or KeyboardInterrupt()

    def progress(self):
        return int(self.cancel.is_set())

    def budget(self):
        m = self.manifest
        if not m:
            return {}
        work = Path(m["work"])
        source_size = Path(m["source"]).stat().st_size
        size = work.stat().st_size
        journal = Path(str(work) + "-journal")
        journal_size = journal.stat().st_size if journal.exists() else 0
        temp_size = tree_bytes(self.run_dir / "scratch") if self.run_dir else 0
        # Prior copies and census scratch are ALREADY allocated, not charged again.
        existing = {k: Path(m[k]).stat().st_size for k in ("source", "backup", "work")}
        existing["restore"] = Path(m["restore_evidence"]["path"]).stat().st_size
        existing["preparation_census_scratch"] = tree_bytes(self.policy.job / "scratch")
        growth = 0 if self.state == "COMPLETE" else max(0, 8 * source_size - size)
        journal_more = 0 if self.state == "COMPLETE" else max(0, 2 * source_size - journal_size)
        temp_more = 0 if self.state == "COMPLETE" else max(0, 2 * source_size - temp_size)
        required = growth + journal_more + temp_more + 20 * GIB
        free = self.prep.available(work.parent)
        return dict(
            free_bytes=free,
            required_remaining_bytes=required,
            reserve_bytes=20 * GIB,
            remaining_work_growth_bytes=growth,
            remaining_journal_bytes=journal_more,
            remaining_sort_bytes=temp_more,
            work_bytes=size,
            journal_bytes=journal_size,
            current_sort_bytes=temp_size,
            already_allocated=existing,
        )

    def sample(self):
        with self.lock:
            elapsed = time.monotonic() - self.start
            total = self.manifest["expected_counts"]["occurrences"] if self.manifest else 0
            import_elapsed = time.monotonic() - self.import_started if self.import_started else 0
            speed = max(0, self.rows - self.initial_rows) / import_elapsed if import_elapsed else 0
            event = dict(
                at=stamp(),
                phase=self.phase,
                elapsed_seconds=elapsed,
                imported_occurrences=self.rows,
                expected_occurrences=total,
                batch_commits=self.batches,
                percent=100 * self.rows / total if total else 0,
                rows_per_second=speed,
                eta_seconds=(total - self.rows) / speed if speed else None,
            )
            if self.prep:
                event.update(self.prep.memory_usage())
                event["capacity"] = self.budget()
                if (
                    event["capacity"]
                    and event["capacity"]["free_bytes"]
                    < event["capacity"]["required_remaining_bytes"]
                ):
                    self.stop(Blocked("REMAINING_CAPACITY_BUDGET_VIOLATED"))
            self.events.append(event)
            # Bound memory: the durable JSONL retains all samples.
            self.events = self.events[-200:]
            if self.logfile:
                self.logfile.write(json.dumps(event, ensure_ascii=True) + "\n")
                self.logfile.flush()
            eta = "?" if event["eta_seconds"] is None else f"{event['eta_seconds']:.0f}s"
            resources = (
                (
                    f" RSS={event['rss_bytes'] / GIB:.2f}GiB free="
                    f"{event['capacity']['free_bytes'] / GIB:.2f}GiB"
                )
                if event.get("capacity")
                else ""
            )
            print(
                f"[{event['at']}] {self.phase}: {self.rows}/{total} ({event['percent']:.2f}%) "
                f"commits={self.batches} {speed:.1f}/s ETA={eta}{resources}",
                flush=True,
            )

    def begin(self):
        def worker():
            while not self.done.wait(1):
                try:
                    self.sample()
                except BaseException as exc:
                    self.stop(exc)

        self.thread = threading.Thread(target=worker, name="f01-g3-monitor", daemon=True)
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join()


class CheckedReader:
    def __init__(self, stream, monitor):
        self.stream, self.monitor = stream, monitor

    def seek(self, *args):
        return self.stream.seek(*args)

    def read(self, *args):
        self.monitor.check()
        return self.stream.read(*args)


@contextmanager
def read_db(path, monitor):
    c = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    monitor.connection = c
    try:
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(monitor.progress, 10000)
        yield c
    finally:
        monitor.connection = None
        c.close()


def attach_monitor(c, monitor):
    monitor.connection = c
    c.set_progress_handler(monitor.progress, 10000)


def complete_checks(c, m, engine):
    """Validation only; migration, views and transaction commits remain in executor."""
    expected = m["expected_counts"]
    if (
        c.execute("SELECT state FROM canonical_migration WHERE singleton=1").fetchone()[0]
        != "COMPLETE"
    ):
        raise Blocked("COMPLETE_STATUS_MISSING")
    checks = {
        "occurrences": "SELECT COUNT(*) FROM source_occurrence",
        "canonical": "SELECT COUNT(*) FROM canonical_fact",
        "source_memberships": (
            "SELECT COUNT(*) FROM (SELECT fact_id,symbol FROM "
            "source_occurrence GROUP BY fact_id,symbol)"
        ),
        "source_taxonomy_memberships": (
            "SELECT COUNT(*) FROM (SELECT fact_id,symbol,taxonomy "
            "FROM source_occurrence GROUP BY fact_id,symbol,taxonomy)"
        ),
        "alias_groups": (
            "SELECT COUNT(*) FROM (SELECT fact_id FROM source_occurrence "
            "GROUP BY fact_id HAVING COUNT(DISTINCT symbol)>1)"
        ),
        "canonical_instant": "SELECT COUNT(*) FROM canonical_fact WHERE period_start IS NULL",
        "canonical_duration": "SELECT COUNT(*) FROM canonical_fact WHERE period_start IS NOT NULL",
        "instant": (
            "SELECT COUNT(*) FROM source_occurrence o JOIN canonical_fact f "
            "USING(fact_id) WHERE f.period_start IS NULL"
        ),
        "duration": (
            "SELECT COUNT(*) FROM source_occurrence o JOIN canonical_fact f "
            "USING(fact_id) WHERE f.period_start IS NOT NULL"
        ),
    }
    actual = {key: c.execute(sql).fetchone()[0] for key, sql in checks.items()}
    if any(actual[k] != expected[k] for k in actual):
        raise Blocked("FINAL_MANIFEST_COUNTS_MISMATCH: " + json.dumps(actual))
    if c.execute("SELECT 1 FROM source_occurrence WHERE original_id IS NOT id LIMIT 1").fetchone():
        raise Blocked("ORIGINAL_ID_MAPPING_DRIFT")
    for query in (
        "SELECT a.id FROM canonical_import_archive a LEFT JOIN source_occurrence o "
        "ON o.original_id=a.id WHERE o.id IS NULL LIMIT 1",
        "SELECT o.id FROM source_occurrence o LEFT JOIN canonical_import_archive a "
        "ON a.id=o.original_id WHERE a.id IS NULL LIMIT 1",
        "SELECT original_id FROM source_occurrence GROUP BY original_id HAVING COUNT(*)<>1 LIMIT 1",
    ):
        if c.execute(query).fetchone():
            raise Blocked("MISSING_OR_DUPLICATE_ORIGINAL_MAPPING")
    engine.verify_progress(c, m)
    return dict(
        counts=actual,
        original_ids_preserved=True,
        one_mapping_per_original=True,
        executor_full_legacy_forward_stream_check=True,
        executor_integrity_and_fk_check=True,
    )


def inspect_work(c, m, resume, engine, monitor):
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_migration'").fetchone()
    if resume:
        if not exists:
            raise Blocked("RESUME_LEDGER_MISSING; no Work deletion or reset permitted")
        meta = engine.verify_progress(c, m)
        monitor.state, monitor.rows = meta[1], meta[3]
        monitor.batches = c.execute("SELECT COUNT(*) FROM canonical_migration_batch").fetchone()[0]
    else:
        if exists:
            raise Blocked("EXPLICIT_RESUME_REQUIRED")
        if engine.sql_schema_hash(c) != m["sql_schema_hash"]:
            raise Blocked("WORK_SCHEMA_DRIFT")


@contextmanager
def execution_lock(path):
    """Persistent empty lock file; never overwrite/delete it on a later Resume."""
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        safe(path)
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise Blocked("ANOTHER_G3_PROCESS_OR_INCOMPATIBLE_LOCK")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def output_report(monitor, status, error=None, result=None):
    if not monitor.run_dir:
        return
    value = dict(
        status=status,
        error=error,
        result=result,
        elapsed_seconds=time.monotonic() - monitor.start,
        recent_progress=monitor.events,
        g4_g6_go=False,
        cutover_go=False,
        executor_default_restored=True,
    )
    pending = monitor.run_dir / "result.pending"
    pending.mkdir()
    new_json(pending / "f01_copy_migration_g3_report.json", value)
    if result:
        new_json(
            pending / "f01_copy_migration_g3_fingerprint.json",
            {k: result[k] for k in ("result_fingerprint", "work_sha256", "manifest_sha256")},
        )
    with (pending / "f01_copy_migration_g3_audit.md").open("x", encoding="utf-8") as out:
        out.write(
            "# F01 copy migration G3\n\nStatus: **" + status + "**\n\n"
            "Valid only in this attempt/result with matching result_seal.json. "
            "result.pending is not publication.\n\n"
            "No G4-G6 or productive cutover GO. Only the Work copy may be migrated.\n\n"
            + (("Error: " + error + "\n\n") if error else "")
            + "Elapsed seconds: "
            + str(value["elapsed_seconds"])
            + "\n\n"
            "Detailed binding, validation and result fingerprint: JSON report.\n"
        )
        out.flush()
        os.fsync(out.fileno())
    new_json(pending / "result_seal.json", {p.name: sha(p) for p in pending.iterdir()})
    pending.rename(monitor.run_dir / "result")


def run(policy, *, execute=False, resume=False, batch_size=10000, hook=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 50000:
        raise Blocked("BATCH_SIZE_MUST_BE_1_TO_50000")
    monitor = Monitor(policy)
    monitor.begin()
    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: monitor.stop(KeyboardInterrupt()))
    old_env = {k: os.environ.get(k) for k in ("TMP", "TEMP")}
    engine = None
    try:
        if execute:
            safe(policy.job, file=False)
            if not policy.job.is_dir():
                raise Blocked("PREPARATION_JOB_DIRECTORY_MISSING")
            runs = policy.job / "g3_runs"
            safe(runs, file=False)
            runs.mkdir(exist_ok=True)
            attempt = runs / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex)
            attempt.mkdir(exist_ok=False)
            monitor.run_dir = attempt
            (attempt / "scratch").mkdir()
            monitor.logfile = (attempt / "progress.jsonl").open("x", encoding="utf-8")
        m, prep, binding = verify_bundle(policy, monitor)
        monitor.prep, monitor.manifest = prep, m
        engine = prep.migration
        if not execute:
            for suffix in ("-wal", "-shm", "-journal"):
                if Path(m["work"] + suffix).exists():
                    raise Blocked(
                        "READ_ONLY_PREFLIGHT_CANNOT_RECOVER_WORK; "
                        "use explicit --execute --resume for hot-journal recovery"
                    )
        static = {
            str(policy.verified / "seal.json"): policy.verified / "seal.json",
            str(policy.approval): policy.approval,
        }
        static.update({str(policy.verified / n): policy.verified / n for n in SEAL_FILES})
        static.update(
            {
                str(policy.root / n): policy.root / n
                for n in m["input_inventory"]["repository"]["files"]
            }
        )
        static.update(
            {
                e["path"]: Path(e["path"])
                for e in m["input_inventory"]["external_review"]["external_files"]
            }
        )
        static.update({str(policy.root / n): policy.root / n for n in ADDITIONS})
        databases = {k: Path(m[k]) for k in ("source", "backup", "work")}
        databases["restore"] = Path(m["restore_evidence"]["path"])
        with ExitStack() as stack:
            if execute:
                stack.enter_context(execution_lock(policy.job / "g3_execution.lock"))
            stack.enter_context(prep.pinned_files(static))
            # Repeat gates after holding immutable code/metadata pins (TOCTOU).
            pinned_manifest, _, pinned_binding = verify_bundle(policy, monitor)
            if canonical_digest(pinned_manifest) != canonical_digest(m) or canonical_digest(
                pinned_binding
            ) != canonical_digest(binding):
                raise Blocked("SEALED_METADATA_CHANGED_WHILE_ACQUIRING_PINS")
            for path in databases.values():
                prep.exclusive_probe(path)
            pins = stack.enter_context(prep.pinned_files(databases))
            engine.validate_work_manifest(m, resume=resume)
            for role, stream in pins.items():
                stat = os.fstat(stream.fileno())
                if (
                    role in m["file_identities"]
                    and [stat.st_dev, stat.st_ino] != m["file_identities"][role]
                ):
                    raise Blocked("DATABASE_FILE_IDENTITY_DRIFT: " + role)
                if role != "work" or not resume:
                    monitor.phase = "Datei-Hash: " + role
                    expected = (
                        m["restore_evidence"]["sha256"]
                        if role == "restore"
                        else m[role + "_sha256"]
                    )
                    if engine.file_hash(CheckedReader(stream, monitor)) != expected:
                        raise Blocked("DATABASE_HASH_DRIFT: " + role)
            monitor.phase = "Source Schema-/Fact-Stream-Bindung"
            with read_db(databases["source"], monitor) as source:
                if (
                    engine.sql_schema_hash(source) != m["sql_schema_hash"]
                    or engine.fact_stream_hash(source, "fundamental_facts") != m["fact_stream_hash"]
                ):
                    raise Blocked("SOURCE_SCHEMA_OR_STREAM_DRIFT")
            if m["source_sha256"] != m["backup_sha256"]:
                raise Blocked("PREPARED_SOURCE_BACKUP_NOT_BYTE_IDENTICAL")
            # Initial Work is byte-identical to this verified Source/Backup; thus
            # the full fact-stream binding follows without a redundant scan.
            if execute:
                work = sqlite3.connect(databases["work"].as_uri() + "?mode=rw", uri=True, timeout=0)
                stack.callback(work.close)
                attach_monitor(work, monitor)
                work.execute("PRAGMA foreign_keys=ON")
                work.execute("PRAGMA journal_mode=DELETE")
                work.execute("PRAGMA synchronous=FULL")
                work.execute("PRAGMA cache_size=-8192")
                work.execute("PRAGMA temp_store=FILE")
                os.environ["TMP"] = os.environ["TEMP"] = str(monitor.run_dir / "scratch")
                if prep.windows_temp().resolve() != (monitor.run_dir / "scratch").resolve():
                    raise Blocked("TEMP_DIRECTORY_BINDING_FAILED")
                inspect_work(work, m, resume, engine, monitor)
            else:
                with read_db(databases["work"], monitor) as work:
                    inspect_work(work, m, resume, engine, monitor)
                monitor.sample()
                monitor.check()
                return dict(
                    status="PREFLIGHT_OK",
                    resume=resume,
                    binding=binding,
                    capacity=monitor.budget(),
                    migration_executed=False,
                )
            monitor.sample()
            monitor.check()
            final_validation = {}

            def event(name, c):
                if c is not None and name in {
                    "prepared",
                    "after_batch_commit",
                    "before_index_build",
                    "before_publish_commit",
                    "after_publish_commit",
                }:
                    monitor.rows = c.execute(
                        "SELECT rows_done FROM canonical_migration"
                    ).fetchone()[0]
                    monitor.batches = c.execute(
                        "SELECT COUNT(*) FROM canonical_migration_batch"
                    ).fetchone()[0]
                if name in {"before_index_build", "after_index_build", "before_publish_commit"}:
                    monitor.phase = name
                elif name in {"prepared", "after_batch_commit"}:
                    monitor.phase = "Import / bestaetigte Batches"
                monitor.check()
                if name == "before_publish_commit":
                    monitor.phase = "G3 Validierung vor atomarem COMPLETE-Commit"
                    final_validation.update(complete_checks(c, m, engine))
                if hook:
                    hook(name, c)

            monitor.phase = "Batch-/Resume-Executor"
            monitor.initial_rows = monitor.rows
            monitor.import_started = time.monotonic()
            engine.EXECUTION_ENABLED = True
            try:
                engine.execute_batches(work, m, batch_size=batch_size, resume=resume, hook=event)
            finally:
                engine.EXECUTION_ENABLED = False
            if not final_validation:
                final_validation.update(complete_checks(work, m, engine))
            if work.execute("SELECT state FROM canonical_migration").fetchone()[0] != "COMPLETE":
                raise Blocked("COMPLETE_NOT_COMMITTED")
            monitor.state = "COMPLETE"
            monitor.phase = "Abschliessende Datei-Fingerprints"
            monitor.connection = None
            work.close()
            with prep.pinned_files({"work_result": databases["work"]}):
                work_hash = sha(databases["work"], monitor)
                for role in ("source", "backup"):
                    if engine.file_hash(CheckedReader(pins[role], monitor)) != m[role + "_sha256"]:
                        raise Blocked("REFERENCE_HASH_DRIFT_AFTER_MIGRATION")
                verify_bundle(policy, monitor)
                monitor.check()
                result = dict(
                    binding=binding,
                    validation=final_validation,
                    work_path=m["work"],
                    work_sha256=work_hash,
                    manifest_sha256=binding["seal"]["f01_copy_rehearsal_manifest.json"],
                    source_backup_unchanged=True,
                    complete_committed=True,
                )
                result["result_fingerprint"] = engine.fingerprint(result)
                monitor.close()
                output_report(monitor, "G3_COMPLETE", result=result)
            return dict(
                status="G3_COMPLETE", result=result, artifacts=str(monitor.run_dir / "result")
            )
    except BaseException as exc:
        if engine is not None:
            engine.EXECUTION_ENABLED = False
        actual = monitor.error or exc
        monitor.close()
        status = "ABORTED" if isinstance(actual, KeyboardInterrupt) else "BLOCKED"
        try:
            output_report(monitor, status, error=str(actual) or "Strg+C")
        except OSError as report_error:
            print(f"[{stamp()}] Report nicht publiziert: {report_error}", file=sys.stderr)
        if isinstance(actual, KeyboardInterrupt):
            raise KeyboardInterrupt() from None
        raise Blocked(str(actual)) from exc
    finally:
        monitor.close()
        monitor.connection = None
        if monitor.logfile:
            monitor.logfile.close()
        signal.signal(signal.SIGINT, old_handler)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        result = run(Policy(), execute=args.execute, resume=args.resume, batch_size=args.batch_size)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps(
                dict(status="ABORTED", message="Work behalten; expliziter Resume erforderlich.")
            )
        )
        return 130
    except (Blocked, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(status="BLOCKED", error=str(exc)), ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
