"""Small generated SQLite fixtures only; never invoke the real acceptance CLI."""
import importlib.util
import shutil
import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path

import pytest
from test_f01_duplicate_impact import current, seeded

SPEC = importlib.util.spec_from_file_location(
    "validate_f01_migration", Path(__file__).resolve().parents[1] / "scripts/validate_f01_migration.py"
)
accept = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = accept
SPEC.loader.exec_module(accept)


class TinyMonitor:
    connection = None
    phase = "synthetic"
    lock = threading.Lock()

    def check(self):
        pass

    def tick(self, phase=None):
        self.phase = phase or self.phase

    def progress(self):
        return 0

    sql_progress = progress

    def capacity(self, *args):
        pass


@pytest.fixture
def tiny(tmp_path):
    from trading_system.data import canonical_migration as engine

    db = seeded(tmp_path / "seed", [current("cash", 10), current("cash", 10),
                current("cash", 10).model_copy(update={"symbol": "OLD"})])
    from test_champion_consolidation import bar
    from trading_system.models.market_data import TradableAsset
    db.upsert_assets([TradableAsset(symbol="TEST", name="Tiny", tradable=True, fractionable=True)])
    db.upsert_bars([bar("TEST", date(2025, 3, 3))])
    db.cache_sec_payload("0000001234", "companyfacts", {"cik": 1234, "facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{"val": 10, "end": "2024-12-31",
        "filed": "2025-02-09", "form": "10-K", "accn": "A", "fy": 2024, "fp": "FY"}]}}
    }}})
    with db.connect() as c:
        c.execute("PRAGMA journal_mode=DELETE")
    source, backup = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    shutil.copyfile(db.path, source)
    shutil.copyfile(db.path, backup)
    with db.connect() as c:
        prep = accept.load_module("_tiny_inventory_prep", accept.ROOT / "scripts/prepare_f01_migration.py")
        m = dict(plan_id="tiny-acceptance", source_sha256=accept.g3.sha(source),
                 source=str(source), backup=str(backup), work=str(db.path),
                 backup_sha256=accept.g3.sha(backup),
                 sql_schema_hash=engine.sql_schema_hash(c),
                 fact_stream_hash=engine.fact_stream_hash(c, "fundamental_facts"),
                 expected_counts={"occurrences": 3, "canonical": 1},
                 input_inventory={"database": prep.inventory(c, TinyMonitor())})
        engine.execute_batches(c, m, batch_size=2)
    return db, m, engine


def test_assessment_never_infers_missing_or_failed_evidence():
    assert accept.assessment([]) == "INSUFFICIENT_EVIDENCE"
    phases = [{"phase": p, "status": "PASS", "gaps": []} for p in ("g4", "g5", "g6")]
    assert accept.assessment(phases) == "REHEARSAL_GO"
    phases[1]["gaps"] = ["missing sync proof"]
    assert accept.assessment(phases) == "INSUFFICIENT_EVIDENCE"
    phases[1]["status"] = "BLOCKED"
    assert accept.assessment(phases) == "REHEARSAL_NO_GO"


def test_immutable_reader_and_logical_mapping(tiny):
    db, m, engine = tiny
    before = db.path.read_bytes()  # Only this generated tiny fixture.
    with accept.reader(db.path, TinyMonitor()) as c:
        evidence = accept.mapping_evidence(c, engine, m)
        assert evidence["occurrences"] == 3
        assert evidence["archive_stream_hash"] == m["fact_stream_hash"]
        with pytest.raises(sqlite3.OperationalError):
            c.execute("UPDATE companies SET name='forbidden'")
    assert db.path.read_bytes() == before
    assert not Path(str(db.path) + "-wal").exists()


def test_g3_seal_and_fingerprint_tampering(tmp_path):
    d = tmp_path / "result"
    d.mkdir()
    result = {"manifest_sha256": "manifest", "work_sha256": "work",
              "source_backup_unchanged": True, "complete_committed": True}
    fp = accept.digest(result)
    result["result_fingerprint"] = fp
    accept.g3.new_json(d / "f01_copy_migration_g3_report.json", {
        "status": "G3_COMPLETE", "error": None, "result": result,
        "executor_default_restored": True})
    accept.g3.new_json(d / "f01_copy_migration_g3_fingerprint.json", {
        "result_fingerprint": fp, "work_sha256": "work", "manifest_sha256": "manifest"})
    (d / "f01_copy_migration_g3_audit.md").write_text("tiny", encoding="utf-8")
    seal = {p.name: accept.g3.sha(p) for p in d.iterdir()}
    accept.g3.new_json(d / "result_seal.json", seal)
    assert accept.g3_result(d, "manifest", TinyMonitor())[1] == fp
    (d / "f01_copy_migration_g3_report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(accept.Blocked, match="SEAL_DRIFT"):
        accept.g3_result(d, "manifest", TinyMonitor())


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows path pins")
def test_g5_adapter_matrix_on_tiny_copy(tiny, tmp_path):
    db, m, engine = tiny
    prep = accept.load_module("_tiny_acceptance_prep", accept.ROOT / "scripts/prepare_f01_migration.py")
    attempt = tmp_path / "g5"
    attempt.mkdir()
    m["work"] = str(db.path)
    result = accept.g5_phase(m, prep, TinyMonitor(), attempt)
    assert "concurrent_identical_writes" in result["cases_passed"]
    assert "late_failure_atomic_rollback_and_exception_propagation" in result["cases_passed"]
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert any("Readiness" in gap for gap in result["gaps"])
    assert result["archived_replays"]
    with db.read_only() as c:
        assert c.execute("SELECT current_version FROM fact_contract").fetchone()[0] == 1


def test_missing_copy_authorization_stops_before_gates(monkeypatch):
    monkeypatch.setattr(accept, "verify_gates", lambda *_: pytest.fail("gate must not run"))
    with pytest.raises(accept.Blocked, match="authorize-g5-copy"):
        accept.run(accept.parser().parse_args(["--g5"]))
    with pytest.raises(accept.Blocked, match="authorize-recovery-copies"):
        accept.run(accept.parser().parse_args(["--g6"]))


def test_g4_real_functions_on_small_fixture_never_claim_missing_inputs(tiny):
    db, m, _ = tiny
    prep = accept.load_module("_tiny_g4_prep", accept.ROOT / "scripts/prepare_f01_migration.py")
    result = accept.g4_phase(m, prep, TinyMonitor(), sample_size=2)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["matrix"]
    assert result["scores"]
    assert any("forward" in gap and "market" in gap for gap in result["gaps"])
    assert result["historical_backtest_run"] is False
    with db.connect() as c:
        c.execute("UPDATE companies SET name='synthetic drift'")
    with pytest.raises(accept.Blocked, match="non_fact_table/companies"):
        accept.g4_phase(m, prep, TinyMonitor(), sample_size=2)


def test_phase_publication_and_tamper_rejection(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    log = (attempt / "progress.jsonl").open("x", encoding="utf-8")
    try:
        monitor = TinyMonitor()
        monitor.logfile = log
        report = {"phase": "g4", "status": "PASS", "gaps": [], "binding": {"fixture": True},
                  "references_unchanged": True, "executor_default_restored": True,
                  "production_cutover_authorized": False}
        published = accept.publish(attempt, "f01_migration_acceptance_g4.json", report, monitor)
        assert accept.read_phase(published, "g4", report["binding"], monitor) == report
        (published / "f01_migration_acceptance_g4.json").write_text("{}", encoding="utf-8")
        with pytest.raises(accept.Blocked, match="SEAL_DRIFT"):
            accept.read_phase(published, "g4", report["binding"], monitor)
        with pytest.raises(FileExistsError):
            accept.g3.new_json(published / "result_seal.json", {})
    finally:
        log.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows path pins")
def test_g6_one_rebuild_crash_sequence_and_restore(tiny, tmp_path):
    db, m, engine = tiny
    prep = accept.load_module("_tiny_recovery_prep", accept.ROOT / "scripts/prepare_f01_migration.py")
    attempt = tmp_path / "recovery"
    attempt.mkdir()
    before = accept.g3.sha(db.path)
    result = accept.g6_phase(m, prep, TinyMonitor(), attempt, rebuild=True, batch_size=2)
    assert result["status"] == "PASS"
    assert [p["event"] for p in result["crash_probes"]] == ["before_batch_commit", "after_batch_commit", "during_index_build", "complete"]
    assert result["crash_probes"][0]["ledger_drift_rejected"]
    assert result["reference_restore"]["byte_identical_to_verified_backup"]
    assert accept.g3.sha(db.path) == before
    assert engine.EXECUTION_ENABLED is False

