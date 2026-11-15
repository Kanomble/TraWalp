#!/usr/bin/env python3
"""Manual F01 G4-G6 acceptance. No database access occurs on import.

Only disposable, explicitly authorized copies may be written. The original
preparation manifest and the G3 wrapper/executor are never changed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ADDITIONS = frozenset({
    "scripts/validate_f01_migration.py",
    "docs/f01_migration_acceptance_script.md",
    "tests/test_validate_f01_migration.py",
})
GIB = 1024**3
CONTRACT = "F01_ACCEPTANCE_V1"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bound_g3_wrapper():
    # This wrapper is an originally reviewed additive input of the sealed G3
    # result. Verify bytes BEFORE execution, and execute those same bytes to
    # avoid a hash/import race. Its main remains guarded.
    path = ROOT / "scripts/run_f01_migration.py"
    raw = path.read_bytes()
    expected = "838e0028a7f234e83f0288c703a9ab0a4a73ca7d7fac61a6242bff0bd8db5cc7"
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("BOUND_G3_WRAPPER_DRIFT_BEFORE_IMPORT")
    module = types.ModuleType("_f01_acceptance_g3")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


g3 = bound_g3_wrapper()
Blocked = g3.Blocked
digest = g3.canonical_digest


@dataclass(frozen=True)
class Policy:
    root: Path = ROOT
    verified: Path = ROOT / "reports/f01_copy_rehearsal/20261115_8f2a/verified"
    approval: Path = ROOT / "reports/f01_copy_migration_additions_review.json"
    acceptance_review: Path = ROOT / "reports/f01_acceptance_additions_review.json"

    @property
    def job(self):
        return self.verified.parent


def require(ok, message):
    if not ok:
        raise Blocked(message)


def sealed(directory, seal_name, names, monitor):
    seal = g3.read_json(directory / seal_name)
    require(set(seal) == set(names), "INCOMPLETE_OR_UNKNOWN_SEAL: " + str(directory))
    for name, expected in seal.items():
        require(Path(name).name == name, "INVALID_SEAL_PATH")
        require(g3.sha(directory / name, monitor) == expected, "SEAL_DRIFT: " + name)
    return seal


def g3_result(directory, manifest_hash, monitor):
    seal = sealed(directory, "result_seal.json", {
        "f01_copy_migration_g3_report.json", "f01_copy_migration_g3_audit.md",
        "f01_copy_migration_g3_fingerprint.json",
    }, monitor)
    report = g3.read_json(directory / "f01_copy_migration_g3_report.json")
    result = dict(report.get("result") or {})
    fingerprint = result.pop("result_fingerprint", None)
    require(report.get("status") == "G3_COMPLETE" and report.get("error") is None,
            "G3_NOT_COMPLETE")
    require(fingerprint == digest(result), "G3_RESULT_FINGERPRINT_DRIFT")
    require(result.get("manifest_sha256") == manifest_hash, "G3_MANIFEST_DRIFT")
    require(result.get("source_backup_unchanged") is True
            and result.get("complete_committed") is True
            and report.get("executor_default_restored") is True, "G3_INCOMPLETE_EVIDENCE")
    fp = g3.read_json(directory / "f01_copy_migration_g3_fingerprint.json")
    require(fp == {"result_fingerprint": fingerprint, "work_sha256": result.get("work_sha256"),
                   "manifest_sha256": manifest_hash}, "G3_FINGERPRINT_FILE_DRIFT")
    return result, fingerprint, seal


def verify_gates(policy, result_dir, monitor):
    """Extend the G3 additive gate in a PRIVATE function namespace only.

    The old approval, G3 globals and sealed manifest remain untouched. The union
    approval exists only in memory, after independent review/hash verification.
    """
    original_seal = g3.read_json(policy.verified / "seal.json")
    mh = original_seal["f01_copy_rehearsal_manifest.json"]
    result, result_fp, result_seal = g3_result(result_dir, mh, monitor)
    old = g3.read_json(policy.approval)
    review = g3.read_json(policy.acceptance_review)
    require(set(old.get("files", {})) == g3.ADDITIONS, "ORIGINAL_ADDITIVE_REVIEW_DRIFT")
    require(review.get("review_status") == "REVIEWED_ADDITIVE_ONLY"
            and review.get("reviewer") and review.get("manifest_sha256") == mh
            and review.get("g3_result_fingerprint") == result_fp
            and review.get("base_git_head") == old.get("base_git_head")
            and set(review.get("files", {})) == ADDITIONS, "ACCEPTANCE_REVIEW_MISSING_OR_DRIFT")
    for name, expected in {**old["files"], **review["files"]}.items():
        require(g3.sha(g3.bound_repo_path(policy.root, name), monitor) == expected,
                "REVIEWED_ADDITION_DRIFT: " + name)
    combined = {**old, "files": {**old["files"], **review["files"]}}
    namespace = dict(vars(g3), ADDITIONS=g3.ADDITIONS | ADDITIONS)
    namespace["read_json"] = lambda path: combined if path == policy.approval else g3.read_json(path)
    for name in ("git_changes", "verify_bundle"):
        fn = getattr(g3, name)
        namespace[name] = types.FunctionType(fn.__code__, namespace, name, fn.__defaults__, fn.__closure__)
    manifest, prep, binding = namespace["verify_bundle"](policy, monitor)
    require(result.get("binding", {}).get("seal") == binding["seal"], "G3_PREPARATION_SEAL_DRIFT")
    require(result.get("binding", {}).get("reviewed_additions") == old,
            "G3_ADDITIVE_APPROVAL_DRIFT")
    require(result.get("work_path") == manifest["work"], "G3_WORK_PATH_DRIFT")
    validation = result.get("validation", {})
    for key in ("original_ids_preserved", "one_mapping_per_original",
                "executor_full_legacy_forward_stream_check", "executor_integrity_and_fk_check"):
        require(validation.get(key) is True, "MISSING_G3_PROOF: " + key)
    require(all(value == manifest["expected_counts"].get(key)
                for key, value in validation.get("counts", {}).items())
            and {"occurrences", "canonical", "source_memberships", "alias_groups"}
            <= set(validation.get("counts", {})), "G3_COUNT_AUTHORITY_DRIFT")
    binding = {
        "contract": CONTRACT, "manifest_sha256": mh, "g3_result_fingerprint": result_fp,
        "g3_seal": result_seal, "work_sha256": result["work_sha256"],
        "original_code_fingerprint": manifest["input_inventory"]["repository"]["code_fingerprint"],
        "review_sha256": g3.sha(policy.acceptance_review, monitor),
        "additions": combined["files"],
    }
    return manifest, prep, binding


def protection_paths(policy, result_dir, manifest):
    paths = {"reference_" + k: Path(manifest[k]) for k in ("source", "backup", "work")}
    paths["restore_reference"] = Path(manifest["restore_evidence"]["path"])
    files = [policy.verified / "seal.json", policy.approval, policy.acceptance_review,
             *(policy.verified / p for p in g3.SEAL_FILES),
             result_dir / "result_seal.json", *(result_dir / p for p in (
                 "f01_copy_migration_g3_report.json", "f01_copy_migration_g3_audit.md", "f01_copy_migration_g3_fingerprint.json"))]
    files += [policy.root / name for name in manifest["input_inventory"]["repository"]["files"]]
    files += [policy.root / name for name in g3.ADDITIONS | ADDITIONS]
    files += [Path(e["path"]) for e in manifest["input_inventory"]["external_review"]["external_files"]]
    for i, path in enumerate(dict.fromkeys(files)):
        paths["bound_" + str(i)] = g3.safe(path)
    return paths


def verify_files(manifest, binding, prep, monitor):
    """Never open production_origin; references must be closed, immutable files."""
    paths = [Path(manifest[k]) for k in ("source", "backup", "work")]
    identities = []
    for key, path in zip(("source", "backup", "work"), paths, strict=True):
        g3.safe(path)
        identity = [path.stat().st_dev, path.stat().st_ino]
        require(identity == manifest["file_identities"][key], "FILE_IDENTITY_DRIFT: " + key)
        identities.append(identity)
        for suffix in ("-wal", "-shm", "-journal"):
            require(not Path(str(path) + suffix).exists(), "SIDECAR_REQUIRES_INVESTIGATION: " + str(path))
        expected = binding["work_sha256"] if key == "work" else manifest[key + "_sha256"]
        monitor.phase = "Verifizierter Dateihash: " + key
        require(g3.sha(path, monitor) == expected, "REFERENCE_HASH_DRIFT: " + key)
    require(len(set(map(tuple, identities))) == 3, "ALIASED_REFERENCES")
    with reader(paths[0], monitor) as c:
        require(prep.migration.sql_schema_hash(c) == manifest["sql_schema_hash"], "SOURCE_SCHEMA_DRIFT")
    with reader(paths[2], monitor) as c:
        from trading_system.data.canonical_facts import require_contract
        require_contract(c, writer=False)
        row = c.execute("SELECT binding,state,rows_done,schema_hash FROM canonical_migration").fetchone()
        require(row is not None and row[0] == digest([prep.migration.EXECUTOR_VERSION, manifest])
                and row[1] == "COMPLETE" and row[2] == manifest["expected_counts"]["occurrences"]
                and row[3] == prep.migration.sql_schema_hash(c), "WORK_NOT_BOUND_COMPLETE")
    restored = Path(manifest["restore_evidence"]["path"])
    require(g3.sha(restored, monitor) == manifest["restore_evidence"]["sha256"], "G0_RESTORE_REFERENCE_DRIFT")
    # G3 already proved both FULL streams. Its unchanged physical Work hash binds
    # that proof, avoiding two redundant 18.7-million-row scans in G4.


class Monitor(g3.Monitor):
    def __init__(self, policy):
        super().__init__(policy)
        self.phase = "G0-G3 / unveränderte Bindungen"
        self.targets = []
        self.envelope = 0
        self.child_pid = None
        self.completed_probes = 0
        self.expected_probes = None

    def tick(self, phase=None):
        if phase:
            self.phase = phase
        self.check()

    sql_progress = g3.Monitor.progress

    def budget(self):
        if not self.prep or not self.manifest:
            return {}
        allocated = sum(p.stat().st_size for p in self.targets if p.exists())
        journal = sum(Path(str(p) + "-journal").stat().st_size for p in self.targets
                      if Path(str(p) + "-journal").exists())
        scratch = g3.tree_bytes(self.run_dir / "scratch") if self.run_dir else 0
        remaining = max(0, self.envelope - allocated - journal - scratch)
        free = self.prep.available(self.policy.job)
        temp_free = self.prep.available(self.prep.windows_temp())
        return {"free_bytes": free, "temp_free_bytes": temp_free,
                "required_remaining_bytes": remaining + 20 * GIB,
                "reserve_bytes": 20 * GIB, "copy_bytes": allocated,
                "journal_bytes": journal, "scratch_bytes": scratch,
                "existing_copies_recharged": False,
                "recovery_child_pid": self.child_pid,
                "recovery_child_memory": process_memory(self.child_pid) if self.child_pid else None}

    def sample(self):
        with self.lock:
            event = {"at": g3.stamp(), "phase": self.phase,
                     "elapsed_seconds": time.monotonic() - self.start,
                     "completed_probes": self.completed_probes, "expected_probes": self.expected_probes}
            if self.prep:
                event.update(self.prep.memory_usage())
                event["capacity"] = self.budget()
                budget = event["capacity"]
                if budget and (budget["free_bytes"] < budget["required_remaining_bytes"]
                               or budget["temp_free_bytes"] < 20 * GIB):
                    self.stop(Blocked("PHASE_CAPACITY_OR_TEMP_RESERVE_VIOLATED"))
            self.events.append(event)
            self.events = self.events[-200:]
            if self.logfile:
                self.logfile.write(json.dumps(event, ensure_ascii=True) + "\n")
                self.logfile.flush()
            resources = ""
            if event.get("capacity"):
                resources = f" RSS={event['rss_bytes'] / GIB:.2f}GiB free={event['capacity']['free_bytes'] / GIB:.2f}GiB"
                child = event["capacity"].get("recovery_child_memory") or {}
                if "rss_bytes" in child:
                    resources += f" child_RSS={child['rss_bytes'] / GIB:.2f}GiB"
            print(f"[{event['at']}] {self.phase} elapsed={event['elapsed_seconds']:.1f}s "
                  f"probes={self.completed_probes}/{self.expected_probes or '?'}{resources}", flush=True)

    def capacity(self, envelope=0, targets=()):
        self.envelope, self.targets = envelope, list(targets)
        self.sample()
        self.check()


def process_memory(pid):
    """Native child RSS, without an extra dependency or subprocess polling."""
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
                                                "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return {"status": "process_exited_or_unavailable"}
    try:
        record = Counters()
        record.cb = ctypes.sizeof(record)
        call = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        call.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not call(handle, ctypes.byref(record), record.cb):
            return {"status": "process_exited_or_unavailable"}
        return {"rss_bytes": record.rss, "peak_rss_bytes": record.peak_rss}
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def reader(path, monitor, forward=False):
    c = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    monitor.connection = c
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA cache_size=-32768")
        c.execute("PRAGMA temp_store=MEMORY")
        if forward:
            from trading_system.data.forward_market import install_forward_view
            install_forward_view(c)  # Connection-local TEMP view only.
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(monitor.progress, 10000)
        yield c
    finally:
        monitor.connection = None
        c.close()


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return {key: plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def equal(left, right, name):
    require(digest(left) == digest(right), "PARITY_DIFFERENCE: " + name)


def frozen_database(c, path, forward):
    from trading_system.data.database import Database
    from trading_system.shadow.inputs import LocalInputs

    if forward:
        db = LocalInputs(path, capture_provenance=False)
        db._connection, db._fact_ciks, db._conflict_symbols = c, None, None
        db.provenance = {"identity_inputs": {}, "fundamental_inputs": {}, "market_inputs": {}}
        return db

    class Frozen(Database):
        @contextmanager
        def connect(self):
            yield c
        read_only = connect

    return Frozen(path)


def table_evidence(c, name, monitor):
    q = '"' + name.replace('"', '""') + '"'
    sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    require(sql is not None, "MISSING_ORIGINAL_TABLE: " + name)
    columns = [tuple(r) for r in c.execute("PRAGMA table_info(" + q + ")")]
    pk = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
    order = ",".join('"' + k.replace('"', '""') + '"' for k in pk) if "WITHOUT ROWID" in sql[0].upper() else "_rowid_"
    h, count = hashlib.sha256(), 0
    for row in c.execute("SELECT * FROM " + q + " ORDER BY " + order):
        h.update((digest(tuple(row)) + "\n").encode())
        count += 1
        if count % 10000 == 0:
            monitor.check()
    return {"rows": count, "row_stream_sha256": h.hexdigest(), "columns": columns,
            "foreign_keys": [tuple(r) for r in c.execute("PRAGMA foreign_key_list(" + q + ")")]}


def bounded_sample(items, size):
    if len(items) <= size:
        return items
    return [items[i * (len(items) - 1) // (size - 1)] for i in range(size)]


def g4_phase(m, prep, monitor, sample_size):
    from trading_system.champion import champion_ranking
    from trading_system.config import load_strategy_config
    from trading_system.strategy.screener import Screener
    from trading_system.fundamentals.quality import accounting_state_as_of, analyze_fundamentals
    from trading_system.fundamentals.debug import debug_fundamentals
    from trading_system.fundamentals.metrics import MAX_SHARES_OUTSTANDING_AGE, balance_sheet_as_of

    gaps, rows, score_rows, tables, identities = [], [], [], {}, []
    config = load_strategy_config()
    with reader(Path(m["work"]), monitor) as c:
        for name, expected in sorted(m["input_inventory"]["database"]["tables"].items()):
            if name in {"fundamental_facts", "sqlite_sequence"}:
                # sqlite_sequence includes executor-added AUTOINCREMENT tables;
                # its original per-table counters are checked separately below.
                continue
            monitor.tick("G4 Nicht-Fact-Tabelle: " + name)
            actual = table_evidence(c, name, monitor)
            equal(actual, expected, "non_fact_table/" + name)
            tables[name] = actual
        # Existing sequence counters are migration inputs too. The archive was
        # renamed; only that name and new canonical counters may differ.
        source_objects = m["input_inventory"]["database"]["objects"]
        if any(r[0] == "table" and r[1] == "sqlite_sequence" for r in source_objects):
            with reader(Path(m["source"]), monitor) as source:
                for name, seq in source.execute("SELECT name,seq FROM sqlite_sequence"):
                    target_name = "canonical_import_archive" if name == "fundamental_facts" else name
                    found = c.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (target_name,)).fetchone()
                    require(found is not None and found[0] == seq, "ORIGINAL_SEQUENCE_DRIFT: " + name)
    coverage, witnesses = set(), set()
    for forward in (False, True):
        mode = "forward_cik_sip" if forward else "legacy_ticker"
        with reader(Path(m["source"]), monitor, forward) as old, reader(Path(m["work"]), monitor, forward) as new:
            dbs = [frozen_database(c, Path(m[k]), forward) for c, k in ((old, "source"), (new, "work"))]
            companies = dbs[0].list_tradable_companies()
            equal([plain(x) for x in companies], [plain(x) for x in dbs[1].list_tradable_companies()], mode + "/companies")
            conflicts = dbs[0].unresolved_sec_identity_conflict_symbols()
            equal(sorted(conflicts), sorted(dbs[1].unresolved_sec_identity_conflict_symbols()), mode + "/conflicts")
            selected = bounded_sample(sorted(companies, key=lambda x: (x.symbol, x.cik)), sample_size)
            require(selected, "G4_NO_COMPANIES")
            # Define the matrix from identity/date inputs before calculating scores.
            matrix = []
            for company in selected:
                facts = new.execute("SELECT metric,period_end,filed,accession_number,period_start,tag,unit FROM canonical_fact WHERE cik=? ORDER BY filed,period_end,fact_id", (company.cik,)).fetchall()
                days = set()
                chosen = list(bounded_sample(facts, 4))
                amendments = {}
                for f in facts:
                    key = (f[0], f[1], f[4], f[5], f[6])
                    previous = amendments.get(key)
                    if previous is not None and previous[3] != f[3] and previous[2] != f[2]:
                        chosen.extend([previous, f])
                        witnesses.add("amendment")
                        break
                    amendments[key] = f
                share = next((f for f in reversed(facts) if f[0] == "shares_outstanding"), None)
                if share:
                    chosen.append(share)
                    for anchor in (date.fromisoformat(share[1]), date.fromisoformat(share[2])):
                        days.update(anchor + MAX_SHARES_OUTSTANDING_AGE + timedelta(days=d) for d in (-1, 0, 1))
                    witnesses.add("shares_age_460")
                for f in chosen:
                    filed, end = date.fromisoformat(f[2]), date.fromisoformat(f[1])
                    days.update(filed + timedelta(days=d) for d in (-1, 0, 1))
                    days.update(end + timedelta(days=d) for d in (-1, 0, 1, 299, 300, 301, 364, 365, 366))
                    witnesses.update({"filed_before_at_after", "period_end", "prior_300"})
                matrix.append((company, sorted(days)))
            matrix_hash = digest([(plain(c), days) for c, days in matrix])
            monitor.expected_probes = sum(len(days) for _, days in matrix) * 2
            for company, days in matrix:
                for day in days:
                    monitor.tick(f"G4 {mode}: {company.symbol} {day}")
                    facts = [db.facts_available_as_of(company.symbol, day) for db in dbs]
                    equal([[plain(f) for f in stream] for stream in facts[:1]],
                          [[plain(f) for f in stream] for stream in facts[1:]], mode + "/PIT")
                    states = [accounting_state_as_of(f, day) for f in facts]
                    equal(plain(states[0]), plain(states[1]), mode + "/accounting_sources_TTM")
                    debug = [debug_fundamentals(company.symbol, f, day) for f in facts]
                    equal([plain(x) for x in debug[:1]], [plain(x) for x in debug[1:]], mode + "/source_tie_breaks")
                    for cutoff in bounded_sample(sorted({f.period_end for f in facts[0]}), 2):
                        equal(plain(balance_sheet_as_of(facts[0], day, end_on_or_before=cutoff)),
                              plain(balance_sheet_as_of(facts[1], day, end_on_or_before=cutoff)), mode + "/end_cutoff")
                    if states[0].current.revenue is not None and states[0].prior.revenue is not None:
                        witnesses.add("TTM_and_prior")
                    for key in ("cash", "total_equity", "total_debt", "shares_outstanding"):
                        if getattr(states[0].balance, key) is not None:
                            witnesses.add("selected_" + key)
                    metrics = [analyze_fundamentals(f, day, None) for f in facts]
                    equal(plain(metrics[0]), plain(metrics[1]), mode + "/metrics_missing_price")
                    coverage.update(f.metric for f in facts[0])
                    rows.append({"reader": mode, "symbol": company.symbol, "as_of": str(day),
                                 "fact_count": len(facts[0]), "fact_hash": digest([plain(f) for f in facts[0]]),
                                 "accounting_hash": digest(plain(states[0])), "source_selection_hash": digest(plain(debug[0])),
                                 "matrix_hash": matrix_hash})
                    monitor.completed_probes = len(rows)
            latest = old.execute("SELECT MAX(timestamp) FROM bars").fetchone()[0]
            if not latest:
                gaps.append(mode + ": no frozen market bars")
                continue
            session = date.fromisoformat(latest[:10])
            # Real identity edge cases are selected from frozen reference inputs,
            # independently of score outcomes. Probe unresolved labels and labels
            # sharing a CIK even when absent from the bounded score cross-section.
            mapping = dbs[0].company_symbol_to_cik()
            equal(mapping, dbs[1].company_symbol_to_cik(), mode + "/identity_mapping")
            groups = {}
            reference = old.execute("SELECT value FROM sync_state WHERE source='sec_reference' AND key='ticker_to_cik'").fetchone()
            reference_mapping = json.loads(reference[0]) if reference and reference[0] else {}
            for symbol, cik in sorted(reference_mapping.items()):
                cik = str(cik).zfill(10)
                groups.setdefault(cik, []).append(symbol)
            identity_symbols = set(sorted(conflicts)[:4])
            multi = [symbols for _, symbols in sorted(groups.items()) if len(symbols) > 1]
            for symbols in multi[:2]:
                identity_symbols.update(symbols[:2])
            if multi:
                witnesses.add("multiple_share_class_labels")
            if conflicts:
                witnesses.add("unresolved_identity")
            for company in selected:
                ciks = [r[0] for r in new.execute("SELECT DISTINCT f.cik FROM source_occurrence o JOIN canonical_fact f ON f.fact_id=o.fact_id WHERE o.symbol=? LIMIT 3", (company.symbol,))]
                if len(ciks) > 1:
                    identity_symbols.add(company.symbol)
                    witnesses.add("ticker_reuse")
            for symbol in sorted(identity_symbols):
                streams = [db.facts_available_as_of(symbol, session) for db in dbs]
                equal([plain(f) for f in streams[0]], [plain(f) for f in streams[1]], mode + "/edge_identity/" + symbol)
                if forward and symbol in conflicts:
                    require(not streams[0], "UNRESOLVED_CIK_FALLBACK")
                if forward and symbol not in mapping:
                    require(not streams[0], "UNMAPPED_SYMBOL_CIK_FALLBACK")
                identities.append({"reader": mode, "symbol": symbol, "session": str(session),
                    "cik": mapping.get(symbol), "unresolved": symbol in conflicts,
                    "facts_hash": digest([plain(f) for f in streams[0]]), "facts": len(streams[0])})
            if not forward:
                for options in ({}, {"period_end_on_or_after": session - timedelta(days=730), "retain_latest_periods": 2}):
                    streams = [[plain(f) for batch in db.iter_fact_batches([x.symbol for x in selected], session, **options) for f in batch] for db in dbs]
                    equal(streams[0], streams[1], "legacy/DENSE_RANK_batch")
                witnesses.add("DENSE_RANK")
            reports = []
            forward_rankings = []
            prepared_digests = []
            for db, c in zip(dbs, (old, new), strict=True):
                screener = Screener(db, config)
                prepared = [screener._prepare(company, session, use_market_snapshots=False, connection=c)
                            for company in selected if company.symbol not in conflicts]
                if any("analysis_error" in p.base_exclusions for p in prepared):
                    raise Blocked("G4_PRODUCTION_ANALYSIS_ERROR")
                prepared_digests.append(digest([plain(p) for p in prepared]))
                report = screener.report_from_prepared(prepared, [x for x in selected if x.symbol in conflicts],
                                                      requested_as_of=session, market_session=session)
                reports.append(report)
                if forward:
                    from trading_system.shadow.qualification import qualified_forward_screen
                    history = db.history_coverage([x.symbol for x in selected], session,
                                                  config.data_quality.min_market_history_days)
                    qualified, qualification = qualified_forward_screen(report, session, config, history_coverage=history)
                    if qualification.get("unresolved_rank_relevant_count"):
                        gaps.append(mode + ": frozen forward inputs are unresolved for ranking")
                    forward_rankings.append(qualified)
            equal(prepared_digests[0], prepared_digests[1], mode + "/prepared_market_peer_inputs")
            for label, pair in (("quality_value", reports),
                                ("F", forward_rankings if forward else [champion_ranking(r, config) for r in reports])):
                data = [r.model_dump(mode="json", exclude={"generated_at"}) for r in pair]
                equal(data[0], data[1], mode + "/scores_eligibility_ranking/" + label)
                rank1 = [r.symbol for r in pair[0].records if r.rank == 1]
                if not rank1:
                    gaps.append(mode + "/" + label + ": no rank-1 witness in fixed cross-section")
                score_rows.append({"reader": mode, "kind": label, "session": str(session),
                                   "symbols": [c.symbol for c in selected], "input_hash": prepared_digests[0],
                                   "report_hash": digest(data[0]), "eligible": pair[0].eligible_count,
                                   "rank1": rank1, "records": data[0]["records"]})
    required = {"cash", "total_equity", "shares_outstanding", "revenue", "net_income"}
    if not required <= coverage:
        gaps.append("Missing required real metric witnesses: " + str(sorted(required - coverage)))
    if not any("debt" in k for k in coverage):
        gaps.append("Missing real debt witness")
    needed = {"amendment", "shares_age_460", "filed_before_at_after", "period_end", "prior_300", "TTM_and_prior", "DENSE_RANK",
              "multiple_share_class_labels", "unresolved_identity", "ticker_reuse",
              "selected_cash", "selected_total_equity", "selected_total_debt", "selected_shares_outstanding"}
    gaps.extend("Missing real boundary witness: " + w for w in sorted(needed - witnesses))
    return {"status": "INSUFFICIENT_EVIDENCE" if gaps else "PASS", "gaps": gaps,
            "non_fact_tables": tables, "matrix": rows, "scores": score_rows,
            "config_hash": digest(plain(config)), "observed_metrics": sorted(coverage),
            "witnesses": sorted(witnesses), "identity_matrix": identities,
            "full_stream_proof": "Reused unchanged, sealed G3 physical/hash binding; both full ordered streams and original IDs",
            "historical_backtest_run": False}


def new_copy(source, target, prep, monitor, *, restore=False):
    monitor.tick("Neue isolierte Kopie: " + target.name)
    require(not target.exists(), "COPY_ALREADY_EXISTS")
    if restore:
        prep.backup_export(source, target, monitor)
    else:
        prep.copy_new(source, target, monitor)


@contextmanager
def network_denied():
    """Block Python socket connects in G5, including worker threads."""
    import socket
    old = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    def denied(*_args, **_kwargs):
        raise Blocked("G5_NETWORK_FORBIDDEN")
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = old


def g5_phase(m, prep, monitor, attempt):
    target = attempt / "g5_disposable.sqlite3"
    w = Path(m["work"]).stat().st_size
    monitor.capacity(3 * w + GIB, [target])
    new_copy(Path(m["work"]), target, prep, monitor)
    g3.safe(target)
    with network_denied():
        with prep.pinned_files({"work": target}):
            result = g5_cases(m, prep, monitor, target)
            # Production legacy mutation is attempted only through the guarded
            # primitive on this disposable canonical copy, never regular Sync.
            from trading_system.data.database import Database
            from trading_system.models.fundamentals import CompanyIdentity
            with reader(target, monitor) as c:
                row = c.execute("SELECT * FROM companies ORDER BY cik LIMIT 1").fetchone()
                company = CompanyIdentity(**{k: row[k] for k in CompanyIdentity.model_fields})
            with sqlite3.connect(target) as c:
                try:
                    Database(target).upsert_sec_company_update(company, [], [], connection=c)
                except ValueError as exc:
                    require(str(exc) == "LEGACY_WRITER_ON_CANONICAL_SCHEMA", "WRONG_LEGACY_GUARD_ERROR")
                else:
                    raise Blocked("LEGACY_WRITER_ACCEPTED_CANONICAL_SCHEMA")
            result["cases_passed"].append("legacy_writer_rejected_on_canonical_schema")
            # Canonical adapter on a new, tiny legacy-schema double must fail
            # before writing. No second full real Source copy is necessary.
            legacy_double = attempt / "g5_legacy_schema_double.sqlite3"
            require(not legacy_double.exists(), "LEGACY_DOUBLE_ALREADY_EXISTS")
            with legacy_double.open("xb"):
                pass
            with prep.pinned_files({"work": legacy_double}):
                with sqlite3.connect(legacy_double) as c:
                    c.execute("CREATE TABLE fundamental_facts(id INTEGER PRIMARY KEY)")
                    c.execute("PRAGMA foreign_keys=ON")
                    from trading_system.data.canonical_facts import install_writer_guard
                    try:
                        install_writer_guard(c)
                    except (ValueError, sqlite3.Error):
                        result["cases_passed"].append("canonical_writer_rejected_on_legacy_schema_double")
                    else:
                        raise Blocked("CANONICAL_WRITER_ACCEPTED_LEGACY_SCHEMA")
            result["disposable_sha256"] = g3.sha(target, monitor)
            return result


def g5_cases(m, prep, monitor, target):
    from trading_system.data.canonical_facts import Admission, RequestIdentity
    from trading_system.data.canonical_sec_adapter import IsolatedCanonicalSecWriter
    from trading_system.data.database import _fact_from_row
    from trading_system.models.fundamentals import CompanyIdentity

    adapter = IsolatedCanonicalSecWriter(target)
    cases, gaps = [], []
    monitor.tick("G5 Auswahl einer vormals mehrdeutigen Original-Identität")
    with reader(target, monitor) as c:
        row = c.execute("SELECT o.* FROM source_occurrence o JOIN companies co ON co.symbol=o.symbol "
                        "JOIN canonical_fact f ON f.fact_id=o.fact_id AND f.cik=co.cik "
                        "WHERE o.original_id IS NOT NULL AND EXISTS (SELECT 1 FROM source_occurrence b "
                        "WHERE b.fact_id=o.fact_id AND b.id<>o.id) ORDER BY o.id LIMIT 1").fetchone()
        require(row is not None, "G5_NO_FORMERLY_AMBIGUOUS_IDENTITY")
        fact_row = c.execute("SELECT * FROM legacy_occ_v1 WHERE id=?", (row["id"],)).fetchone()
        fact = _fact_from_row(fact_row)
        co = c.execute("SELECT * FROM companies WHERE symbol=?", (fact.symbol,)).fetchone()
        company = CompanyIdentity(**{k: co[k] for k in CompanyIdentity.model_fields})
        initial_version = c.execute("SELECT current_version FROM fact_contract").fetchone()[0]
        original_anchor = (row["id"], row["fact_id"])
        forward_anchors = [r[0] for r in c.execute("SELECT id FROM source_occurrence WHERE fact_id=? AND forward_order_admitted=1 ORDER BY id", (row["fact_id"],))]
        known = c.execute("SELECT value FROM sync_state WHERE source='sec_accessions' AND key=?", (company.cik,)).fetchone()
        known_accessions = json.loads(known[0]) if known and known[0] else []
        require(isinstance(known_accessions, list) and all(isinstance(a, str) for a in known_accessions),
                "G5_UNSUPPORTED_ACCESSION_INPUT_CONTRACT")
    fixed_time = "2026-11-15T00:00:00+00:00"
    identity = RequestIdentity(company.symbol, company.cik, "G5 isolated frozen identity")
    base_admission = Admission(True, True, "G5 explicit original membership")
    accession_inputs, committed_accessions = {}, set(known_accessions)
    accession_lock = threading.Lock()

    def version():
        with reader(target, monitor) as c:
            return c.execute("SELECT current_version FROM fact_contract").fetchone()[0]

    def call(name, facts, *, company_value=company, identity_value=identity, admissions=None, expected=None, connection=None):
        monitor.tick("G5 isolierter Adapter: " + name)
        with accession_lock:
            accessions = accession_inputs.setdefault(name, sorted(committed_accessions | {"F01_G5_" + name}
                                 | {f.accession_number for f in facts if f.accession_number}))
        result_version = adapter.update_company(company_value, facts, accessions, connection=connection,
            request_id="F01_G5_" + name, payload_hash=digest([plain(f) for f in facts]),
            retrieved_at=fixed_time, identity=identity_value,
            admissions=admissions or {(fact.symbol, fact.taxonomy): base_admission},
            expected_version=version() if expected is None else expected)
        if connection is None:
            with accession_lock:
                committed_accessions.update(accessions)
        return result_version

    v1 = call("repeat", [fact])
    equal(call("repeat", [fact], expected=initial_version), v1, "G5_receipt_retry")
    with reader(target, monitor) as c:
        equal(tuple(c.execute("SELECT id,fact_id FROM source_occurrence WHERE id=?", (original_anchor[0],)).fetchone()), original_anchor, "G5_stable_anchor")
        require(c.execute("SELECT COUNT(*) FROM fact_revision WHERE fact_id=? AND version=?", (original_anchor[1], v1)).fetchone()[0] == 0,
                "G5_UNCHANGED_VALUE_REVISION")
    cases.extend(["formerly_ambiguous_replay", "unchanged_repeat", "idempotent_receipt", "stable_original_anchor"])
    corrected = fact.model_copy(update={"value": fact.value + 1})
    vc = call("correction", [corrected])
    with reader(target, monitor) as c:
        revisions = [tuple(r) for r in c.execute("SELECT version,value FROM fact_revision WHERE fact_id=? ORDER BY version", (original_anchor[1],))]
        require(len(revisions) >= 2 and str(revisions[-1][1]) == str(corrected.value), "G5_CORRECTION_HISTORY_LOST")
        require(vc > v1, "G5_VERSION_NOT_ADVANCED")
        history_value = c.execute("SELECT value FROM legacy_occ_versions_v1 WHERE dataset_version=? AND id=?", (v1, original_anchor[0])).fetchone()[0]
        require(str(history_value) == str(fact.value), "G5_HISTORICAL_READER_CHANGED")
    cases.append("value_correction_historical_revisions")
    fresh = fact.model_copy(update={"tag": "F01G5NewIdentity", "accession_number": "F01-G5-NEW", "frame": None, "period_start": None})
    duration = fresh.model_copy(update={"tag": "F01G5Duration", "period_start": fact.period_end - timedelta(days=89)})
    call("new_accession_mixed", [fresh, duration])
    cases.append("new_accession_and_mixed_instant_duration")
    amendment = fact.model_copy(update={"accession_number": "F01-G5-AMEND", "filed": fact.filed + timedelta(days=1), "value": fact.value + 2})
    call("amendment", [amendment])
    with reader(target, monitor) as c:
        require(c.execute("SELECT COUNT(*) FROM canonical_fact WHERE cik=? AND tag=? AND accession_number=?", (fact.cik, fact.tag, amendment.accession_number)).fetchone()[0] == 1,
                "G5_AMENDMENT_IDENTITY_NOT_PRESERVED")
        require(c.execute("SELECT accession_number,filed FROM canonical_fact WHERE fact_id=?", (original_anchor[1],)).fetchone()[:] == (fact.accession_number or "", fact.filed.isoformat()),
                "G5_AMENDMENT_OVERWROTE_ORIGINAL")
    cases.append("amendment_is_distinct_from_value_correction")
    alias = corrected.model_copy(update={"symbol": "F01_G5_ALIAS"})
    call("alias", [alias], admissions={(alias.symbol, alias.taxonomy): Admission(True, False, "G5 test-only alias admission")})
    with reader(target, monitor) as c:
        require(c.execute("SELECT COUNT(*) FROM source_occurrence WHERE symbol=? AND forward_order_admitted<>0", (alias.symbol,)).fetchone()[0] == 0,
                "G5_ALIAS_FORWARD_ANCHOR_CHANGED")
        equal([r[0] for r in c.execute("SELECT id FROM source_occurrence WHERE fact_id=? AND forward_order_admitted=1 ORDER BY id", (original_anchor[1],))], forward_anchors, "G5_all_forward_anchors_unchanged")
        require(c.execute("SELECT 1 FROM legacy_occ_versions_v1 WHERE dataset_version=? AND symbol=? LIMIT 1", (v1, alias.symbol)).fetchone() is None,
                "G5_ALIAS_RETROACTIVELY_ADMITTED")
    cases.append("explicit_alias_membership_no_forward_anchor")
    # A failure in the last receipt write must undo company/fact/accession/version.
    def state():
        with reader(target, monitor) as c:
            return digest({
                "company": [tuple(r) for r in c.execute("SELECT * FROM companies WHERE cik=?", (company.cik,))],
                "fact": [tuple(r) for r in c.execute("SELECT * FROM canonical_fact WHERE fact_id=?", (original_anchor[1],))],
                "sync": [tuple(r) for r in c.execute("SELECT * FROM sync_state WHERE key=? ORDER BY source", (company.cik,))],
                "receipts": [tuple(r) for r in c.execute("SELECT * FROM ingestion_receipt WHERE request_id LIKE 'F01_G5_%' ORDER BY request_id")],
                "version": c.execute("SELECT current_version FROM fact_contract").fetchone()[0],
                "revisions": [tuple(r) for r in c.execute("SELECT * FROM fact_revision WHERE fact_id=? ORDER BY version", (original_anchor[1],))],
                "test_facts": [tuple(r) for r in c.execute("SELECT * FROM canonical_fact WHERE cik=? AND tag LIKE 'F01%' ORDER BY fact_id", (company.cik,))],
                "test_occurrences": [tuple(r) for r in c.execute("SELECT o.* FROM source_occurrence o JOIN canonical_fact f ON f.fact_id=o.fact_id WHERE f.cik=? AND f.tag LIKE 'F01%' ORDER BY o.id", (company.cik,))],
                "datasets": [tuple(r) for r in c.execute("SELECT * FROM fact_dataset WHERE version>? ORDER BY version", (initial_version,))],
            })
    before_external = state()
    before = version()
    with adapter.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        call("external_rollback", [fresh.model_copy(update={"tag": "F01G5ExternalRollback"})],
             company_value=company.model_copy(update={"name": "F01_G5_PENDING"}), connection=c, expected=before)
        c.rollback()
    equal(state(), before_external, "G5_external_company_fact_accession_receipt_rollback")
    cases.append("external_transaction_rollback")
    before_state = state()
    for site, timing, table, condition in (
        ("company", "UPDATE", "companies", ""),
        ("fact", "INSERT", "canonical_fact", ""),
        ("occurrence", "INSERT", "source_occurrence", ""),
        ("accession", "INSERT", "sync_state", "WHEN NEW.source='sec_accessions'"),
        ("status", "DELETE", "sync_state", "WHEN OLD.source='sec_companyfacts_status'"),
        ("receipt", "INSERT", "ingestion_receipt", ""),
    ):
        # Seed the status deletion case only on the disposable copy.
        if site == "status":
            with sqlite3.connect(target) as c:
                c.execute("INSERT OR REPLACE INTO sync_state VALUES ('sec_companyfacts_status',?,?,?)",
                          (company.cik, fixed_time, '{"status":"test-only"}'))
            before_state = state()
        with sqlite3.connect(target) as c:
            c.execute(f"CREATE TRIGGER f01_g5_abort BEFORE {timing} ON {table} {condition} BEGIN SELECT RAISE(ABORT,'F01_G5_INJECTED'); END")
        propagated = False
        try:
            call("injected_failure_" + site,
                 [corrected.model_copy(update={"value": corrected.value + 1}),
                  fresh.model_copy(update={"tag": "F01G5RollbackNew_" + site})],
                 company_value=company.model_copy(update={"name": "F01_G5_UNCOMMITTED"}))
        except sqlite3.Error:
            propagated = True
        finally:
            with sqlite3.connect(target) as c:
                c.execute("DROP TRIGGER f01_g5_abort")
        require(propagated, "G5_FAILURE_SWALLOWED: " + site)
        equal(state(), before_state, "G5_atomic_rollback/" + site)
        cases.append("injected_failure_" + site)
    cases.append("late_failure_atomic_rollback_and_exception_propagation")
    before = version()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call("concurrent_retry", [fresh], expected=before), range(2)))
    equal(results[0], results[1], "G5_concurrent_receipt")
    cases.append("concurrent_identical_writes")
    # Exact archived JSON, no TTL/provider fetch and no fabricated archive label.
    from trading_system.data.xbrl_parser import parse_company_facts
    with reader(target, monitor) as c:
        cache = c.execute("SELECT sql FROM sqlite_master WHERE name='raw_sec_cache'").fetchone()
        archive_inventory = [dict(r) for r in c.execute("PRAGMA table_info(raw_sec_cache)")] if cache else []
        archived = c.execute("SELECT endpoint,fetched_at,payload FROM raw_sec_cache WHERE cik=? ORDER BY endpoint", (company.cik,)).fetchall() if cache else []
    archived_evidence = []
    class ArchivedProviderDouble:
        def __init__(self, payload):
            self.payload = payload
        def companyfacts(self, cik):
            require(str(self.payload.get("cik", "")).zfill(10) == cik, "ARCHIVED_SEC_CIK_MISMATCH")
            return self.payload
    for archive in archived:
        payload = json.loads(archive[2])
        if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict):
            continue
        provider = ArchivedProviderDouble(payload)
        parsed = parse_company_facts(provider.companyfacts(company.cik), company.symbol)
        if not parsed:
            continue
        # Deterministic bounded replay across the archived parser stream, with
        # explicit admissions for every source taxonomy. No network SDK exists.
        batch = bounded_sample(parsed, 16)
        admissions = {(f.symbol, f.taxonomy): base_admission for f in batch}
        request = "archive_" + digest(payload)[:16]
        archived_version = call(request, batch, admissions=admissions)
        equal(call(request, batch, admissions=admissions, expected=0), archived_version, "G5_archived_retry_after_reopen")
        archived_evidence.append({"endpoint": archive[0], "fetched_at": archive[1],
            "payload_sha256": hashlib.sha256(archive[2].encode()).hexdigest(),
            "parsed_facts": len(parsed), "selected_facts": len(batch), "version": archived_version})
        cases.append("archived_SEC_parser_provider_double_replay_and_restart_retry")
        break
    if not archived_evidence:
        gaps.append("No parseable archived SEC companyfacts for the selected formerly ambiguous identity")
    # Explicit schema/identity/admission guards: each rejected write must leave
    # current_version unchanged and may never be interpreted as a success.
    def rejected(name, facts, **kwargs):
        before = version()
        try:
            call(name, facts, **kwargs)
        except (ValueError, sqlite3.Error):
            equal(version(), before, "G5_rejected_version/" + name)
            cases.append("rejected_" + name)
        else:
            raise Blocked("G5_INVALID_INPUT_ACCEPTED: " + name)
    rejected("wrong_cik", [fresh.model_copy(update={"cik": "9999999999"})])
    rejected("missing_admission", [fresh.model_copy(update={"taxonomy": "F01_NO_ADMISSION"})])
    rejected("stale_version", [fresh], expected=0)
    rejected("repeat", [corrected])
    rejected("unresolved_identity", [fresh], identity_value=RequestIdentity(company.symbol, company.cik, "G5 explicitly unresolved", True))
    unknown = fresh.model_copy(update={"metric": "f01_unknown_metric", "tag": "F01Unknown", "accession_number": None, "frame": None})
    call("unknown_metric_optional_nulls", [unknown])
    cases.append("unknown_metric_and_optional_nulls_preserved")
    taxonomy = corrected.model_copy(update={"taxonomy": "F01_G5_TAXONOMY"})
    call("taxonomy_membership", [taxonomy], admissions={(taxonomy.symbol, taxonomy.taxonomy): Admission(True, False, "G5 explicit taxonomy variant")})
    cases.append("taxonomy_variant_admission")
    with adapter.connect() as held:
        held.execute("BEGIN IMMEDIATE")
        with sqlite3.connect(target, timeout=0) as blocked_connection:
            rejected("lock_error", [fresh], connection=blocked_connection, expected=version())
        held.rollback()
    gaps.extend([
        "Readiness/DataSynchronizer partial/failed/last_success integration is excluded by R2 and unproven by the isolated adapter",
    ])
    monitor.completed_probes = len(cases)
    return {"status": "INSUFFICIENT_EVIDENCE", "cases_passed": cases, "gaps": gaps,
            "disposable_path": str(target), "disposable_sha256": g3.sha(target, monitor),
            "archive_columns": archive_inventory, "archived_replays": archived_evidence, "network_used": False,
            "production_sync_invoked": False, "adapter": "IsolatedCanonicalSecWriter"}


def mapping_evidence(c, engine, m):
    meta = engine.verify_progress(c, m)
    require(meta[1] == "COMPLETE", "G6_INCOMPLETE_LEDGER")
    low = c.execute("SELECT MIN(id) FROM source_occurrence").fetchone()[0]
    high = c.execute("SELECT MAX(id) FROM source_occurrence").fetchone()[0]
    return {"binding": meta[0], "state": meta[1], "occurrences": meta[3],
            "mapping_hash": engine._mapping_hash(c, min(0, low - 1) if low else 0, high or 0),
            "archive_stream_hash": engine.fact_stream_hash(c, "canonical_import_archive"),
            "batch_count": c.execute("SELECT COUNT(*) FROM canonical_migration_batch").fetchone()[0]}


def recovery_worker(request_path):
    """Private subprocess entry: only a new attempt-local clone can be written.

    No CLI/environment switch enables a writer. Parent pins all originals while
    this child deliberately exits without finally/rollback at an agreed event.
    """
    request_path = Path(request_path)
    request = g3.read_json(request_path)
    require(request["parent_pid"] == os.getppid(), "RECOVERY_PARENT_MISMATCH")
    clone = g3.safe(Path(request["clone"]))
    require(clone.parent == request_path.parent and clone.name == "recovery_rebuild.sqlite3",
            "RECOVERY_PATH_NOT_ISOLATED")
    require([clone.stat().st_dev, clone.stat().st_ino] == request["identity"], "RECOVERY_CLONE_REPLACED")
    m = request["manifest"]
    require(digest(m) == request["manifest_digest"] and clone not in [Path(m[k]) for k in ("source", "backup", "work")], "RECOVERY_BINDING_DRIFT")
    from trading_system.data import canonical_migration as engine
    from trading_system.data.canonical_paths import pinned_files
    previous = engine.EXECUTION_ENABLED
    require(previous is False, "EXECUTOR_ALREADY_ENABLED")
    event = request["crash_event"]
    marker = Path(request["marker"])
    require(marker.parent == clone.parent and not marker.exists(), "RECOVERY_MARKER_COLLISION")

    def hook(name, c):
        if name == event:
            g3.new_json(marker, {"event": name, "rows": c.execute("SELECT rows_done FROM canonical_migration").fetchone()[0]})
            os._exit(73)
        if event == "during_index_build" and name == "before_index_build":
            index_statement = [None]
            steps = [0]
            def trace(sql):
                index_statement[0] = sql if sql.upper().startswith("CREATE INDEX") else None
            def crash_in_vm():
                if index_statement[0]:
                    steps[0] += 1
                    if steps[0] >= 25:
                        g3.new_json(marker, {"event": "during_index_build", "sqlite_vm": True,
                            "statement": index_statement[0], "observed_vm_steps": steps[0]})
                        os._exit(73)
                return 0
            c.set_trace_callback(trace)
            c.set_progress_handler(crash_in_vm, 1)

    try:
        with pinned_files({"work": clone}):
            c = sqlite3.connect(clone.as_uri() + "?mode=rw", uri=True)
            try:
                c.execute("PRAGMA foreign_keys=ON")
                c.execute("PRAGMA temp_store=FILE")
                engine.EXECUTION_ENABLED = True
                result = engine.execute_batches(c, m, batch_size=request["batch_size"],
                                                resume=request["resume"], hook=hook)
            finally:
                c.close()
        g3.new_json(marker, {"event": "complete", "result": result})
    finally:
        engine.EXECUTION_ENABLED = previous


def child_recovery(clone, m, event, resume, batch_size, monitor):
    request_path = clone.parent / ("recovery_" + event + ".json")
    marker = clone.parent / ("recovery_" + event + "_evidence.json")
    request = {"parent_pid": os.getpid(), "clone": str(clone), "manifest": m,
               "manifest_digest": digest(m), "identity": [clone.stat().st_dev, clone.stat().st_ino],
               "crash_event": event, "resume": resume, "batch_size": batch_size, "marker": str(marker)}
    g3.new_json(request_path, request)
    # Windows venv python.exe is a redirector process. Launch the actual bound
    # interpreter with this parent's exact import paths/prefixes so PID ownership
    # and terminate() address the SQLite worker itself, not a redirector parent.
    runtime = json.dumps([sys.path, sys.prefix, sys.exec_prefix, sys.executable])
    code = ("import importlib.util,sys,json; sys.path[:],sys.prefix,sys.exec_prefix,sys.executable=json.loads(sys.argv[3]); "
            "s=importlib.util.spec_from_file_location('f01_recovery_child',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);m.recovery_worker(sys.argv[2])")
    monitor.tick("G6 Recovery-Prozess: " + event)
    with (clone.parent / ("recovery_" + event + ".log")).open("x", encoding="utf-8") as log:
        scratch = clone.parent / "scratch"
        scratch.mkdir(exist_ok=True)
        environment = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), SQLITE_TMPDIR=str(scratch))
        executable = getattr(sys, "_base_executable", sys.executable)
        process = subprocess.Popen([executable, "-B", "-c", code, str(Path(__file__).resolve()), str(request_path), runtime],
                                   stdout=log, stderr=subprocess.STDOUT, env=environment,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        monitor.child_pid = process.pid
        try:
            while process.poll() is None:
                monitor.check()
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
            monitor.child_pid = None
        require(process.returncode == (0 if event == "complete" else 73), "RECOVERY_CHILD_FAILURE: " + event)
    evidence = g3.read_json(marker)
    require(evidence.get("event") == event, "RECOVERY_EVENT_NOT_REACHED")
    # A normal rw open on this authorized clone alone performs hot-journal
    # recovery. Never open a hot journal using immutable=1.
    with sqlite3.connect(clone.as_uri() + "?mode=rw", uri=True) as c:
        c.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        state = tuple(c.execute("SELECT state,rows_done FROM canonical_migration").fetchone())
        if event == "before_batch_commit":
            require(state == ("BUILDING", 0), "UNCOMMITTED_BATCH_NOT_ROLLED_BACK")
        elif event == "after_batch_commit":
            require(state == ("BUILDING", min(batch_size, m["expected_counts"]["occurrences"])), "COMMITTED_BATCH_LOST")
        elif event == "during_index_build":
            require(state == ("BUILDING", m["expected_counts"]["occurrences"]), "INDEX_CRASH_PUBLISHED_INCOMPLETE_DATASET")
        if event == "before_batch_commit":
            from trading_system.data import canonical_migration as engine
            # Negative resume probes stay in a rollback transaction on this
            # disposable clone; no corrupted ledger is ever committed.
            try:
                engine.verify_progress(c, {**m, "plan_id": m["plan_id"] + "_WRONG"})
            except ValueError as exc:
                require(str(exc) == "MIGRATION_MANIFEST_DRIFT", "WRONG_MANIFEST_ERROR")
            else:
                raise Blocked("RECOVERY_WRONG_MANIFEST_ACCEPTED")
            c.execute("BEGIN")
            c.execute("UPDATE canonical_migration SET rows_done=1")
            try:
                try:
                    engine.verify_progress(c, m)
                except ValueError:
                    evidence["ledger_drift_rejected"] = True
                else:
                    raise Blocked("RECOVERY_INVALID_LEDGER_ACCEPTED")
            finally:
                c.rollback()
            c.execute("BEGIN")
            c.execute("CREATE TABLE f01_schema_drift_probe(value)")
            try:
                try:
                    engine.verify_progress(c, m)
                except ValueError as exc:
                    require(str(exc) == "MIGRATION_SCHEMA_DRIFT", "WRONG_SCHEMA_DRIFT_ERROR")
                    evidence["schema_drift_rejected"] = True
                else:
                    raise Blocked("RECOVERY_SCHEMA_DRIFT_ACCEPTED")
            finally:
                c.rollback()
    return {**evidence, "recovered_state": state, "child_returncode": process.returncode}


def g6_phase(m, prep, monitor, attempt, rebuild, batch_size):
    engine = prep.migration
    restored = attempt / "g6_restore.sqlite3"
    reference_restore = attempt / "g6_reference_restore.sqlite3"
    clone = attempt / "recovery_rebuild.sqlite3"
    w, s = Path(m["work"]).stat().st_size, Path(m["source"]).stat().st_size
    # Use the measured successful output, never an assumed deduplication saving.
    # Allow twice that physical size for the rebuild, plus journal and sort.
    rebuild_budget = max(s, 2 * w) + 2 * max(s, w) if rebuild else 0
    monitor.capacity(2 * w + s + rebuild_budget, [restored, reference_restore, clone])
    with reader(Path(m["work"]), monitor) as c:
        monitor.tick("G6 G3-Ledger und logisches Mapping")
        original = mapping_evidence(c, engine, m)
    new_copy(Path(m["backup"]), reference_restore, prep, monitor)
    reference_hash = g3.sha(reference_restore, monitor)
    equal(reference_hash, m["backup_sha256"], "G6_unchanged_reference_restore")
    with reader(reference_restore, monitor) as c:
        require([tuple(r) for r in c.execute("PRAGMA integrity_check")] == [("ok",)], "REFERENCE_RESTORE_INTEGRITY_FAILURE")
        require(c.execute("PRAGMA foreign_key_check").fetchone() is None, "REFERENCE_RESTORE_FK_FAILURE")
        equal(engine.sql_schema_hash(c), m["sql_schema_hash"], "G6_reference_restore_schema")
    new_copy(Path(m["work"]), restored, prep, monitor, restore=True)
    with reader(restored, monitor) as c:
        recovered = mapping_evidence(c, engine, m)
        monitor.tick("G6 Restore: vollständige logische Legacy-/Forward-Streams und Integrität")
        engine._validate_final(c, m)
    equal(original, recovered, "G6_restore_logical_identity")
    result = {"status": "INSUFFICIENT_EVIDENCE", "gaps": [], "original": original,
              "restore": recovered, "restore_path": str(restored),
              "restore_physical_sha256": g3.sha(restored, monitor),
              "restore_full_legacy_forward_stream_check": True,
              "reference_restore": {"path": str(reference_restore), "physical_sha256": reference_hash,
                  "byte_identical_to_verified_backup": True, "integrity_check": "ok", "foreign_key_check": []},
              "physical_hash_equality_required": False, "crash_probes": []}
    if not rebuild:
        result["gaps"].append("Real snapshot crash-before/after-batch and during-index evidence missing; a single isolated rebuild requires --rebuild-recovery")
        return result
    # One rebuild, stopped three times; never rerun the successful Work migration.
    new_copy(Path(m["source"]), clone, prep, monitor)
    g3.safe(clone)
    with prep.pinned_files({"work": clone}):
        for event, resume in (("before_batch_commit", False), ("after_batch_commit", True),
                              ("during_index_build", True), ("complete", True)):
            result["crash_probes"].append(child_recovery(clone, m, event, resume, batch_size, monitor))
        with reader(clone, monitor) as c:
            rebuilt = mapping_evidence(c, engine, m)
            # Batch-size-dependent ledger hashes may differ. Mapping and streams may not.
            equal({k: original[k] for k in ("mapping_hash", "archive_stream_hash", "occurrences", "state")},
                  {k: rebuilt[k] for k in ("mapping_hash", "archive_stream_hash", "occurrences", "state")},
                  "G6_rebuild_reproducible_logical_output")
            result["rebuilt"] = rebuilt
    result["rebuild_physical_sha256"] = g3.sha(clone, monitor)
    result["status"] = "PASS"
    return result


def assessment(phases):
    if any(p.get("status") in {"FAILED", "BLOCKED", "ABORTED"} for p in phases):
        return "REHEARSAL_NO_GO"
    if len(phases) != 3 or {p.get("phase") for p in phases} != {"g4", "g5", "g6"}:
        return "INSUFFICIENT_EVIDENCE"
    return "REHEARSAL_GO" if all(p.get("status") == "PASS" and not p.get("gaps") for p in phases) else "INSUFFICIENT_EVIDENCE"


def publish(attempt, name, report, monitor):
    monitor.tick("Atomare Ergebnisveröffentlichung")
    staging = attempt / "unpublished"
    staging.mkdir()
    g3.new_json(staging / name, report)
    audit = staging / "f01_migration_acceptance_audit.md"
    with audit.open("x", encoding="utf-8") as stream:
        stream.write("# F01 Migration Acceptance\n\nStatus: **" + report["status"] + "**\n\n")
        stream.write("Phase: " + report["phase"] + "\n\n")
        stream.write("Binding: `" + digest(report["binding"]) + "`\n\n")
        stream.write("Kein produktiver Cutover ist autorisiert. Source/Backup/erfolgreiche Work bleiben unverändert.\n\n")
        for gap in report.get("gaps", []):
            stream.write("- " + gap + "\n")
        stream.write("\nDetails und Messwerte stehen im gebundenen JSON-Bericht und Fortschrittslog.\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Freeze a complete JSONL prefix under the sampler lock. The outer live log
    # may continue; only this durable snapshot belongs to the result seal.
    with monitor.lock:
        if monitor.logfile:
            monitor.logfile.flush()
            os.fsync(monitor.logfile.fileno())
        with (staging / "progress.jsonl").open("xb") as out:
            out.write((attempt / "progress.jsonl").read_bytes())
            out.flush()
            os.fsync(out.fileno())
    g3.new_json(staging / "result_fingerprint.json", {"contract": CONTRACT, "report_fingerprint": digest(report)})
    seal = {p.name: g3.sha(p, monitor) for p in staging.iterdir()}
    g3.new_json(staging / "result_seal.json", seal)
    staging.rename(attempt / "result")
    return attempt / "result"


def read_phase(directory, phase, binding, monitor):
    g3.safe(directory, file=False)
    if (directory / "failure.json").is_file():
        failure = g3.read_json(directory / "failure.json")
        require(failure.get("binding") == binding and failure.get("phase") == phase,
                "FAILED_PHASE_BINDING_MISMATCH")
        return {"phase": phase, "status": "BLOCKED", "gaps": [failure.get("error", "Interrupted phase")],
                "failure_sha256": g3.sha(directory / "failure.json", monitor), "binding": binding}
    name = "f01_migration_acceptance_" + phase + ".json"
    sealed(directory, "result_seal.json", {name, "f01_migration_acceptance_audit.md", "progress.jsonl", "result_fingerprint.json"}, monitor)
    report = g3.read_json(directory / name)
    require(report.get("phase") == phase and report.get("binding") == binding,
            "PHASE_BINDING_MISMATCH: " + phase)
    require(report.get("references_unchanged") is True
            and report.get("executor_default_restored") is True
            and report.get("production_cutover_authorized") is False,
            "INCOMPLETE_PHASE_SAFETY_EVIDENCE: " + phase)
    require(g3.read_json(directory / "result_fingerprint.json") == {
        "contract": CONTRACT, "report_fingerprint": digest(report)}, "PHASE_FINGERPRINT_DRIFT")
    return report


def result_directory(policy, specified):
    if specified:
        return g3.safe(specified, file=False)
    candidates = sorted((policy.job / "g3_runs").glob("*/result"))
    candidates = [p for p in candidates if (p / "f01_copy_migration_g3_report.json").is_file()
                  and g3.read_json(p / "f01_copy_migration_g3_report.json").get("status") == "G3_COMPLETE"]
    require(len(candidates) == 1, "SPECIFY_UNIQUE_SEALED_G3_RESULT_WITH_--g3-result")
    return candidates[0]


def run(args, policy=None):
    policy = policy or Policy()
    monitor = Monitor(policy)
    attempt, binding = None, None
    phase = "g4" if args.g4 else "g5" if args.g5 else "g6" if args.g6 else "finalize" if args.finalize else "preflight"
    require(not args.g5 or args.authorize_g5_copy, "G5_REQUIRES_--authorize-g5-copy")
    require(not args.g6 or args.authorize_recovery_copies, "G6_REQUIRES_--authorize-recovery-copies")
    require(not args.rebuild_recovery or args.g6, "REBUILD_ONLY_WITH_G6")
    require(2 <= args.sample_size <= 64 and 1 <= args.batch_size <= 50000, "BOUNDED_MATRIX_AND_BATCH_REQUIRED")
    prior_signal = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: monitor.stop(KeyboardInterrupt()))
    monitor.begin()
    try:
        result_dir = result_directory(policy, args.g3_result)
        m, prep, binding = verify_gates(policy, result_dir, monitor)
        monitor.prep, monitor.manifest = prep, m
        # Parent pin 'reference_work' denies writers; only new disposable targets
        # use the special writable role 'work'. No gap between recheck and use.
        with ExitStack() as stack:
            stack.enter_context(prep.pinned_files(protection_paths(policy, result_dir, m)))
            m2, _, binding2 = verify_gates(policy, result_dir, monitor)
            equal((m, binding), (m2, binding2), "gate_recheck_after_native_pins")
            monitor.capacity()
            verify_files(m, binding, prep, monitor)
            if phase == "preflight":
                print("PREFLIGHT_VERIFIED: G0-G3 gebunden; keine Abnahmephase ausgeführt. Kein Rehearsal-GO.", flush=True)
                return 0
            attempts = policy.job / "acceptance_runs"
            g3.safe(attempts, file=False)
            attempts.mkdir(exist_ok=True)
            attempt = attempts / (g3.stamp().replace(":", "").replace("+", "_") + "_" + phase + "_" + uuid.uuid4().hex)
            attempt.mkdir()
            monitor.run_dir = attempt
            monitor.logfile = (attempt / "progress.jsonl").open("x", encoding="utf-8")
            stack.enter_context(prep.pinned_files({"work": attempt / "progress.jsonl"}))
            # All SQLite FILE scratch for recovery children stays on the budgeted
            # attempt volume; the independent monitor also checks the OS temp reserve.
            scratch = attempt / "scratch"
            scratch.mkdir()
            stack.enter_context(g3.execution_lock(policy.job / "f01_acceptance.lock"))
            if phase == "g4":
                result = g4_phase(m, prep, monitor, args.sample_size)
            elif phase == "g5":
                result = g5_phase(m, prep, monitor, attempt)
            elif phase == "g6":
                result = g6_phase(m, prep, monitor, attempt, args.rebuild_recovery, args.batch_size)
            else:
                phases = []
                for p in ("g4", "g5", "g6"):
                    directory = getattr(args, p + "_result")
                    if directory:
                        # Pins close races between seal verification and final use.
                        entries = {"final_" + p + "_" + f.name: f for f in directory.iterdir() if f.is_file()}
                        stack.enter_context(prep.pinned_files(entries))
                        phases.append(read_phase(directory, p, binding, monitor))
                result = {"status": assessment(phases), "phases": phases,
                          "gaps": [f"Missing {p} result" for p in ("g4", "g5", "g6")
                                   if p not in {r["phase"] for r in phases}]}
                result["gaps"] += [f"{r['phase']}: {gap}" for r in phases for gap in r.get("gaps", [])]
            monitor.capacity(0, monitor.targets)
            verify_files(m, binding, prep, monitor)
            report = {**result, "phase": phase, "binding": binding, "at": g3.stamp(),
                      "elapsed_seconds": time.monotonic() - monitor.start,
                      "resources": prep.memory_usage(), "capacity": monitor.budget(),
                      "references_unchanged": True, "production_cutover_authorized": False,
                      "executor_default_restored": prep.migration.EXECUTION_ENABLED is False}
            require(report["executor_default_restored"], "EXECUTOR_DEFAULT_NOT_RESTORED")
            name = "f01_migration_acceptance_" + (phase if phase != "finalize" else "assessment") + ".json"
            published = publish(attempt, name, report, monitor)
            print(f"{report['status']}: {published}", flush=True)
            return 0 if report["status"] in {"PASS", "REHEARSAL_GO"} else 2
    except BaseException as exc:
        status = "ABORTED" if isinstance(exc, KeyboardInterrupt) else "BLOCKED"
        error = f"{type(exc).__name__}: {exc}"
        print(f"[{g3.stamp()}] {status}: {error}", file=sys.stderr, flush=True)
        if attempt:
            # Failure is durable but never published as a successful sealed phase.
            g3.new_json(attempt / "failure.json", {"status": status, "phase": phase,
                "error": error, "binding": binding, "at": g3.stamp(), "cutover_go": False})
        return 130 if status == "ABORTED" else 2
    finally:
        monitor.close()
        if monitor.logfile:
            monitor.logfile.close()
        signal.signal(signal.SIGINT, prior_signal)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    phases = p.add_mutually_exclusive_group()
    for phase in ("g4", "g5", "g6", "finalize"):
        phases.add_argument("--" + phase, action="store_true")
    p.add_argument("--g3-result", type=Path, help="Versiegeltes G3 result-Verzeichnis; Standard: genau ein G3_COMPLETE")
    p.add_argument("--g4-result", type=Path)
    p.add_argument("--g5-result", type=Path)
    p.add_argument("--g6-result", type=Path)
    p.add_argument("--authorize-g5-copy", action="store_true")
    p.add_argument("--authorize-recovery-copies", action="store_true")
    p.add_argument("--rebuild-recovery", action="store_true", help="Ein isolierter Neubau für fehlende reale Crash-/Resume-Evidenz")
    p.add_argument("--sample-size", type=int, default=24)
    p.add_argument("--batch-size", type=int, default=5000)
    return p


if __name__ == "__main__":
    try:
        raise SystemExit(run(parser().parse_args()))
    except Blocked as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
