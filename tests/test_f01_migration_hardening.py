"""8F.1B: exclusively generated offline fixtures, never configured data paths."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import tracemalloc

import pytest
from test_champion_consolidation import offline as offline
from test_f01_canonical_ingestion import ADMIT, COMPANY, IDENTITY, normalized, write
from test_f01_duplicate_impact import ASOF, current, inputs, ranked, scored, seeded
from test_f01_survivor_resolution import read

from trading_system.data import canonical_migration as migration
from trading_system.data.canonical_facts import KEY, atomic, lookup_query
from trading_system.data.canonical_sec_adapter import IsolatedCanonicalSecWriter
from trading_system.data.database import Database


def test_reproduce_generic_lookup_scan(tmp_path, capsys):
    db = normalized(tmp_path, [current("cash", 10)])
    with db.connect() as c:
        c.execute("DROP INDEX canonical_forward_reader")
        row = c.execute("SELECT * FROM canonical_fact").fetchone()
        plan = [
            r[3]
            for r in c.execute(
                "EXPLAIN QUERY PLAN SELECT fact_id FROM canonical_fact "
                "WHERE " + " AND ".join(k + " IS ?" for k in KEY),
                [row[k] for k in KEY],
            )
        ]
        print("BASELINE_GENERIC_LOOKUP", plan)
        assert any("SCAN canonical_fact" in r for r in plan)


def test_indexed_lookups_and_readers(tmp_path):
    from datetime import date

    db = normalized(
        tmp_path,
        [
            current("cash", 10),
            current("revenue", 20).model_copy(update={"period_start": date(2024, 10, 1)}),
        ],
    )
    with db.connect() as c:
        for row in c.execute("SELECT * FROM canonical_fact"):
            sql, args = lookup_query(row)
            plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)]
            print("LOOKUP", plan)
            index = "canonical_instant" if row["period_start"] is None else "canonical_duration"
            assert any("SEARCH" in r and index in r for r in plan)
        for role, key, value in [("legacy", "symbol", "TEST"), ("forward", "cik", "0000001234")]:
            plan = [
                r[3]
                for r in c.execute(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {role}_occ_v1 "
                    f"WHERE {key}=? AND filed<=? ORDER BY filed,period_end,id",
                    (value, "2026-01-01"),
                )
            ]
            print(role, plan)
            assert not any("SCAN o" in r or "SCAN f" in r for r in plan)
            assert not any("fact_revision" in r or "fact_dataset" in r for r in plan)


def test_owner_commit_error_preserves_original_exception_and_rolls_back():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE child(id INTEGER REFERENCES parent DEFERRABLE INITIALLY DEFERRED)")
    try:
        with (
            pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"),
            atomic(c),
        ):
            c.execute("INSERT INTO child VALUES (1)")
        assert not c.in_transaction
        assert c.execute("SELECT * FROM child").fetchall() == []
    finally:
        c.close()


def work_manifest(tmp_path, facts, *, id_shift=0):
    source = seeded(tmp_path / "source", facts)
    with source.connect() as c:
        c.execute("PRAGMA journal_mode=DELETE")
        if id_shift:
            c.execute("UPDATE fundamental_facts SET id=id+?", (id_shift,))
    backup, work = tmp_path / "backup.sqlite", tmp_path / "work.sqlite"
    shutil.copyfile(source.path, backup)
    shutil.copyfile(source.path, work)
    with source.read_only() as c:
        schema = migration.sql_schema_hash(c)
        stream = migration.fact_stream_hash(c, "fundamental_facts")
        count = c.execute("SELECT COUNT(*) FROM fundamental_facts").fetchone()[0]
        unique = {
            tuple(r) for r in c.execute("SELECT " + ",".join(KEY) + " FROM fundamental_facts")
        }
    paths = {"source": source.path, "backup": backup, "work": work}
    result = dict(
        contract="F01_CANON_OCC_V1",
        plan_id="synthetic-8f1b",
        **{k: str(p) for k, p in paths.items()},
        **{k + "_sha256": hashlib.sha256(p.read_bytes()).hexdigest() for k, p in paths.items()},
        expected_counts={"occurrences": count, "canonical": len(unique)},
        input_inventory=["synthetic"],
        consumer_inventory=["offline test readers"],
        capacity_budget={"bytes": 100_000_000},
        restore_evidence="synthetic copy",
        sql_schema_hash=schema,
        fact_stream_hash=stream,
        file_identities={k: [p.stat().st_dev, p.stat().st_ino] for k, p in paths.items()},
    )
    return result


def run_work(monkeypatch, manifest, **kwargs):
    monkeypatch.setattr(migration, "EXECUTION_ENABLED", True)
    return migration.migrate_work(manifest, **kwargs)


def protected_unchanged(manifest):
    from pathlib import Path

    for role in ["source", "backup"]:
        assert (
            hashlib.sha256(Path(manifest[role]).read_bytes()).hexdigest()
            == manifest[role + "_sha256"]
        )


def test_sparse_revisions_keep_receipts_versions_and_old_stream(tmp_path):
    a = current("cash", 10)
    db = normalized(tmp_path, [a])
    with db.connect() as c:
        write(c, [a])
        write(c, [a], request="second", version=2)
        assert c.execute("SELECT COUNT(*) FROM fact_revision").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM fact_dataset").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM ingestion_receipt").fetchone()[0] == 3
        write(c, [a.model_copy(update={"value": 20})], request="changed", version=3)
        assert c.execute("SELECT COUNT(*) FROM fact_revision").fetchone()[0] == 2
        assert [
            r[0]
            for r in c.execute("SELECT value FROM legacy_occ_versions_v1 ORDER BY dataset_version")
        ] == ["10", "10", "10", "20"]


def test_executor_complete_stream_and_initialization_parity(tmp_path, monkeypatch):
    facts = inputs() + [
        current("cash", 10),
        current("cash", 20, "B"),
        current("cash", 10),
        current("unknown", 5).model_copy(update={"symbol": "OLD"}),
    ]
    manifest = work_manifest(tmp_path, facts)
    beforedb = Database(manifest["source"])
    before = [
        read(beforedb, r, ASOF, s)
        for r, s in [("legacy", "TEST"), ("legacy", "OLD"), ("forward", "TEST")]
    ]
    batches = list(beforedb.iter_fact_batches(["TEST", "OLD"], ASOF, retain_latest_periods=2))
    assert run_work(monkeypatch, manifest, batch_size=7)["rows"] == len(facts)
    work = Database(manifest["work"])
    work.initialize()  # No legacy indexes on a view, no implicit migration.
    for (r, s), old in zip(
        [("legacy", "TEST"), ("legacy", "OLD"), ("forward", "TEST")], before, strict=True
    ):
        new = read(work, r, ASOF, s)
        assert new == old
        assert scored(new, ASOF) == scored(old, ASOF)
        assert ranked([scored(new, ASOF)[1]]) == ranked([scored(old, ASOF)[1]])
    assert list(work.iter_fact_batches(["TEST", "OLD"], ASOF, retain_latest_periods=2)) == batches
    assert run_work(monkeypatch, manifest, batch_size=19, resume=True)["state"] == "COMPLETE"
    protected_unchanged(manifest)


@pytest.mark.parametrize(
    "event",
    ["before_batch_commit", "after_batch_commit", "after_index_build", "after_publish_commit"],
)
def test_process_crash_and_resume(tmp_path, monkeypatch, event):
    facts = [
        current("cash", 10),
        current("cash", 20, "B"),
        current("cash", 10),
        current("unknown", 7),
    ]
    manifest = work_manifest(tmp_path, facts)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    script = (
        "import json,os,sys; from trading_system.data import canonical_migration as m; "
        "m.EXECUTION_ENABLED=True; "
        "m.migrate_work(json.load(open(sys.argv[1])),batch_size=2,"
        "hook=lambda event,c: os._exit(73) if event==sys.argv[2] else None)"
    )
    crash = subprocess.run(
        [sys.executable, "-c", script, str(path), event], capture_output=True, text=True, timeout=45
    )
    assert crash.returncode == 73, crash.stderr
    db = Database(manifest["work"])
    if event != "after_publish_commit":
        for action in [
            db.initialize,
            lambda: db.facts_available_as_of("TEST", ASOF),
            lambda: list(db.read_only().__enter__().execute("SELECT 1")),
        ]:
            with pytest.raises(ValueError, match="MIGRATION_NOT_COMPLETE"):
                action()
    result = run_work(monkeypatch, manifest, batch_size=3, resume=True)
    assert result == {"state": "COMPLETE", "rows": 4}
    with db.read_only() as c:
        assert c.execute("SELECT COUNT(*) FROM source_occurrence").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM canonical_fact").fetchone()[0] == 3
        assert [
            tuple(r) for r in c.execute("SELECT id,fact_id FROM source_occurrence ORDER BY id")
        ] == [(1, 1), (2, 2), (3, 1), (4, 4)]
    protected_unchanged(manifest)


@pytest.mark.parametrize(
    "drift", ["manifest", "schema", "ledger", "archive", "user_version", "state"]
)
def test_resume_rejects_drift(tmp_path, monkeypatch, drift):
    manifest = work_manifest(tmp_path, [current("cash", 10), current("cash", 10)])

    def interrupt(event, c):
        if event == "after_batch_commit":
            raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        run_work(monkeypatch, manifest, batch_size=1, hook=interrupt)
    if drift == "manifest":
        manifest = {**manifest, "plan_id": "different"}
    else:
        with sqlite3.connect(manifest["work"]) as c:
            sql = {
                "schema": "DROP INDEX canonical_instant",
                "ledger": "UPDATE canonical_migration_batch SET mapping_hash='bad'",
                "archive": "UPDATE canonical_import_archive SET value='99' WHERE id=1",
                "user_version": "PRAGMA user_version=99",
                "state": "UPDATE canonical_migration SET state='UNKNOWN'",
            }[drift]
            c.execute(sql)
    with pytest.raises(ValueError, match="DRIFT"):
        run_work(monkeypatch, manifest, resume=True)
    protected_unchanged(manifest)


def test_mandatory_pins_block_protected_writes_and_path_swap(tmp_path, monkeypatch):
    manifest = work_manifest(tmp_path, [current("cash", 10)])
    from pathlib import Path

    def attack(event, c):
        if event != "paths_pinned":
            return
        for role in ["source", "backup", "work"]:
            path = Path(manifest[role])
            with pytest.raises(OSError):
                path.rename(path.with_suffix(".moved"))
            if role != "work":
                with pytest.raises(OSError), path.open("r+b") as target:
                    target.write(b"unsafe")
                with pytest.raises(sqlite3.Error), sqlite3.connect(path, timeout=0) as unsafe:
                    unsafe.execute("CREATE TABLE forbidden(x)")
        with pytest.raises(OSError):
            Path(manifest["source"]).parent.rename(tmp_path / "replaced-parent")

    run_work(monkeypatch, manifest, hook=attack)
    protected_unchanged(manifest)


def test_isolated_sec_adapter_external_rollback(tmp_path):
    db = normalized(tmp_path, [current("cash", 10)])
    adapter = IsolatedCanonicalSecWriter(db.path)
    contract = dict(
        request_id="g5",
        payload_hash="b" * 64,
        retrieved_at="2026-11-15T00:00:00Z",
        identity=IDENTITY,
        admissions={("TEST", "us-gaap"): ADMIT},
        expected_version=1,
    )
    with adapter.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        adapter.update_company(COMPANY, [current("cash", 20)], ["A"], connection=c, **contract)
        c.rollback()
    with db.read_only() as c:
        assert c.execute("SELECT value FROM canonical_fact").fetchone()[0] == "10"
    assert adapter.update_company(COMPANY, [current("cash", 20)], ["A"], **contract) == 2
    with adapter.connect() as c, pytest.raises(sqlite3.Error):
        c.execute("CREATE TABLE bypass(x)")


def test_external_transaction_keeps_owners_work_and_original_error():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE data(x INTEGER UNIQUE)")
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO data VALUES (7)")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), atomic(c):
            c.execute("INSERT INTO data VALUES (8)")
            c.execute("INSERT INTO data VALUES (7)")
        assert c.in_transaction
        assert c.execute("SELECT * FROM data").fetchall() == [(7,)]
        c.rollback()
        assert c.execute("SELECT * FROM data").fetchall() == []
    finally:
        c.close()


@pytest.mark.parametrize("field", ["sql_schema_hash", "fact_stream_hash", "work_sha256"])
def test_initial_binding_drift_is_rejected(tmp_path, monkeypatch, field):
    manifest = work_manifest(tmp_path, [current("cash", 10)])
    with pytest.raises(ValueError, match="MISMATCH"):
        run_work(monkeypatch, {**manifest, field: "0" * 64})
    protected_unchanged(manifest)


def test_manifest_identity_blocks_substitution_before_open(tmp_path, monkeypatch):
    from pathlib import Path

    manifest = work_manifest(tmp_path, [current("cash", 10)])
    work = Path(manifest["work"])
    new = tmp_path / "substitute.sqlite"
    shutil.copyfile(work, new)  # Identical bytes, different file identity.
    work.unlink()
    new.rename(work)
    with pytest.raises(ValueError, match="FILE_IDENTITY_MISMATCH"):
        run_work(monkeypatch, manifest)
    protected_unchanged(manifest)


def test_content_conflict_never_publishes_partial_dataset(tmp_path, monkeypatch):
    manifest = work_manifest(tmp_path, [current("cash", 10), current("cash", 20)])
    with pytest.raises(ValueError, match="UNRESOLVED_CONTENT_CONFLICT"):
        run_work(monkeypatch, manifest, batch_size=1)
    with pytest.raises(ValueError, match="MIGRATION_NOT_COMPLETE"):
        Database(manifest["work"]).initialize()
    with sqlite3.connect(manifest["work"]) as c:
        assert c.execute("SELECT rows_done FROM canonical_migration").fetchone()[0] == 1
    protected_unchanged(manifest)


def test_zero_and_negative_original_ids_preserved_across_resume(tmp_path, monkeypatch):
    manifest = work_manifest(
        tmp_path, [current("cash", 10), current("cash", 20, "B"), current("cash", 10)], id_shift=-3
    )
    run_work(monkeypatch, manifest, batch_size=1)
    run_work(monkeypatch, manifest, batch_size=2, resume=True)
    with Database(manifest["work"]).read_only() as c:
        assert [
            tuple(r) for r in c.execute("SELECT id,fact_id FROM source_occurrence ORDER BY id")
        ] == [(-2, -2), (-1, -1), (0, -2)]


def test_cleanup_failure_does_not_mask_original_commit_error():
    class BrokenCleanup(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("original commit failure")

        def rollback(self):
            raise sqlite3.OperationalError("additional rollback failure")

    c = sqlite3.connect(":memory:", factory=BrokenCleanup)
    try:
        with (
            pytest.raises(sqlite3.OperationalError, match="original commit failure") as error,
            atomic(c),
        ):
            c.execute("CREATE TABLE synthetic(x)")
        assert "additional rollback failure" in error.value.__notes__[0]
    finally:
        c.close()


def _memory_bytes():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in [
                "peak_rss",
                "rss",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            ]
        ]

    counters = Counters()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), ctypes.sizeof(counters)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.rss, counters.peak_rss


@pytest.mark.parametrize("unique_count,batch_size", [(2000, 250), (8000, 1000)])
def test_bounded_synthetic_scale(tmp_path, monkeypatch, unique_count, batch_size):
    # Mixed metrics and null/duration starts; repeats must retain every occurrence.
    from datetime import date
    from pathlib import Path

    facts = []
    for i in range(unique_count):
        fact = current("cash" if i % 2 else "revenue", i, "synthetic-" + str(i))
        if i % 2 == 0:
            fact = fact.model_copy(update={"period_start": date(2024, 10, 1)})
        facts.extend(
            [fact] if i % 2 == 0 else [fact, fact.model_copy(update={"symbol": "OLD"}), fact]
        )
    manifest = work_manifest(tmp_path, facts)
    del facts
    before_rss, _ = _memory_bytes()
    tracemalloc.start()
    started = time.perf_counter()
    result = run_work(monkeypatch, manifest, batch_size=batch_size)
    seconds = time.perf_counter() - started
    _, python_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after_rss, process_peak = _memory_bytes()
    query_start = time.perf_counter()
    with Database(manifest["work"]).read_only() as c:
        rows = c.execute(
            "SELECT * FROM forward_occ_v1 WHERE cik='0000001234' "
            "AND filed<='2026-01-01' ORDER BY filed,period_end,id"
        ).fetchall()
        assert len(rows) == unique_count * 2
        page_count = c.execute("PRAGMA page_count").fetchone()[0]
        page_size = c.execute("PRAGMA page_size").fetchone()[0]
        batches = c.execute("SELECT COUNT(*) FROM canonical_migration_batch").fetchone()[0]
    query_seconds = time.perf_counter() - query_start
    assert result["rows"] == unique_count * 2
    assert python_peak < 24 * 1024 * 1024  # Executor measured, fixture construction excluded.
    metrics = dict(
        occurrences=result["rows"],
        canonical=unique_count,
        batch_size=batch_size,
        batch_commits=batches,
        seconds=seconds,
        rows_per_second=result["rows"] / seconds,
        query_seconds=query_seconds,
        python_peak_bytes=python_peak,
        rss_before_bytes=before_rss,
        rss_after_bytes=after_rss,
        process_peak_rss_bytes=process_peak,
        input_bytes=Path(manifest["source"]).stat().st_size,
        work_bytes=Path(manifest["work"]).stat().st_size,
        allocated_bytes=page_count * page_size,
        sqlite_cache_budget_bytes=8192 * 1024,
    )
    (tmp_path / "scale_metrics.json").write_text(json.dumps(metrics, indent=2))
    print("SYNTHETIC_SCALE", json.dumps(metrics, sort_keys=True))
    protected_unchanged(manifest)
