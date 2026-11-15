"""G3 wrapper acceptance on tiny generated SQLite files only."""

import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from test_champion_consolidation import offline as offline
from test_f01_duplicate_impact import current, seeded

SPEC = importlib.util.spec_from_file_location(
    "run_f01_migration", Path(__file__).resolve().parents[1] / "scripts/run_f01_migration.py"
)
g3 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = g3
SPEC.loader.exec_module(g3)
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows mandatory path pins")


@pytest.fixture
def bundle(tmp_path):
    # Never reference the real verified/ directory or any real database.
    from test_prepare_f01_migration import prep

    from trading_system.data import canonical_migration as engine

    db = seeded(
        tmp_path / "seed",
        [
            current("cash", 10),
            current("cash", 10),
            current("cash", 10).model_copy(update={"symbol": "OLD"}),
            current("debt_current", 20, "B"),
        ],
    )
    with db.connect() as c:
        c.execute("PRAGMA journal_mode=DELETE")
    job = tmp_path / "job"
    for d in ("reference", "restore", "work", "verified", "scratch"):
        (job / d).mkdir(parents=True, exist_ok=True)
    files = {
        "source": job / "reference/source.sqlite3",
        "backup": job / "reference/backup.sqlite3",
        "work": job / "work/canonical_rehearsal.sqlite3",
        "restore": job / "restore/restore.sqlite3",
    }
    for p in files.values():
        shutil.copyfile(db.path, p)
    repo = prep.repository_evidence(g3.ROOT)
    # The prepared production fingerprint predates the additive wrapper.
    repo["files"] = {k: v for k, v in repo["files"].items() if k not in g3.ADDITIONS}
    repo["code_fingerprint"] = engine.fingerprint(
        [repo["files"], repo["runtime_packages"], sys.version, sqlite3.sqlite_version]
    )
    review = dict(
        git_head=repo["git_head"],
        code_fingerprint=repo["code_fingerprint"],
        reviewer="synthetic",
        repository_consumers_reviewed=True,
        external_inventory_complete=True,
        inputs_complete=True,
        unknown_consumers=[],
        external_files=[],
    )
    with db.read_only() as c:
        inv = {"sql_schema_hash": engine.sql_schema_hash(c)}
        stream_hash = engine.fact_stream_hash(c, "fundamental_facts")
    m = dict(
        contract=engine.CONTRACT,
        executor_version=engine.EXECUTOR_VERSION,
        plan_id="synthetic-wrapper",
        **{k: str(v) for k, v in files.items() if k != "restore"},
        **{k + "_sha256": g3.sha(v) for k, v in files.items() if k != "restore"},
        file_identities={
            k: [v.stat().st_dev, v.stat().st_ino] for k, v in files.items() if k != "restore"
        },
        sql_schema_hash=inv["sql_schema_hash"],
        fact_stream_hash=stream_hash,
        expected_counts=dict(
            occurrences=4,
            canonical=2,
            source_memberships=3,
            source_taxonomy_memberships=3,
            alias_groups=1,
            instant=4,
            duration=0,
            canonical_instant=2,
            canonical_duration=0,
            content_conflicts=0,
        ),
        input_inventory=dict(repository=repo, external_review=review, database=inv),
        consumer_inventory=dict(external_review=review),
        capacity_budget={"synthetic": True},
        restore_evidence=dict(
            path=str(files["restore"]),
            sha256=g3.sha(files["restore"]),
            verified_inventory_fingerprint=engine.fingerprint(inv),
        ),
    )
    v = job / "verified"
    g3.new_json(v / "f01_copy_rehearsal_manifest.json", m)
    g3.new_json(
        v / "f01_preparation_report.json",
        dict(
            status="G0_G2_READY",
            execution_enabled=False,
            migration_executed=False,
            expected_counts=m["expected_counts"],
        ),
    )
    (v / "f01_real_migration_preparation_audit.md").write_text("synthetic audit", encoding="utf-8")
    g3.new_json(v / "seal.json", {n: g3.sha(v / n) for n in g3.SEAL_FILES})
    approval = tmp_path / "addition-review.json"
    g3.new_json(
        approval,
        dict(
            review_status="REVIEWED_ADDITIVE_ONLY",
            manifest_sha256=g3.sha(v / "f01_copy_rehearsal_manifest.json"),
            base_git_head=repo["git_head"],
            reviewer="synthetic reviewer",
            files={n: g3.sha(g3.ROOT / n) for n in g3.ADDITIONS},
        ),
    )
    return g3.Policy(root=g3.ROOT, verified=v, approval=approval), m, engine


def test_preflight_then_complete_without_touching_sealed_inputs(bundle):
    policy, m, engine = bundle
    before = {Path(m[k]): g3.sha(Path(m[k])) for k in ("source", "backup", "work")}
    seal_before = {p: g3.sha(p) for p in policy.verified.iterdir()}
    result = g3.run(policy)
    assert result["status"] == "PREFLIGHT_OK"
    assert not (policy.job / "g3_runs").exists()
    assert all(g3.sha(p) == h for p, h in before.items())
    result = g3.run(policy, execute=True, batch_size=2)
    assert result["status"] == "G3_COMPLETE"
    assert result["result"]["validation"]["counts"]["source_memberships"] == 3
    assert engine.EXECUTION_ENABLED is False
    assert all(g3.sha(p) == h for p, h in seal_before.items())
    assert g3.sha(Path(m["source"])) == before[Path(m["source"])]
    assert g3.sha(Path(m["backup"])) == before[Path(m["backup"])]
    for name, h in json.loads((Path(result["artifacts"]) / "result_seal.json").read_text()).items():
        assert g3.sha(Path(result["artifacts"]) / name) == h
    assert g3.run(policy, execute=True, resume=True)["status"] == "G3_COMPLETE"


def test_interrupt_and_resume_retains_committed_batches(bundle):
    policy, m, engine = bundle

    def interrupt(name, c):
        if name == "after_batch_commit":
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        g3.run(policy, execute=True, batch_size=2, hook=interrupt)
    assert engine.EXECUTION_ENABLED is False
    with sqlite3.connect(m["work"]) as c:
        assert c.execute("SELECT rows_done,state FROM canonical_migration").fetchone() == (
            2,
            "BUILDING",
        )
    c.close()
    assert g3.run(policy, execute=True, resume=True, batch_size=1)["status"] == "G3_COMPLETE"


@pytest.mark.parametrize("drift", ["seal", "addition", "runtime", "production"])
def test_gate_drift_stops_before_any_work_change(bundle, drift, monkeypatch):
    policy, m, engine = bundle
    before = g3.sha(Path(m["work"]))
    if drift == "seal":
        (policy.verified / "f01_preparation_report.json").write_text("{}")
    elif drift == "runtime":
        monkeypatch.setattr(g3.importlib.metadata, "distributions", lambda: [])
    elif drift == "production":
        m["input_inventory"]["repository"]["files"]["src/trading_system/data/database.py"] = (
            "0" * 64
        )
        v = policy.verified
        (v / "f01_copy_rehearsal_manifest.json").write_text(json.dumps(m))
        (v / "seal.json").write_text(json.dumps({n: g3.sha(v / n) for n in g3.SEAL_FILES}))
        approval = json.loads(policy.approval.read_text())
        approval["manifest_sha256"] = g3.sha(v / "f01_copy_rehearsal_manifest.json")
        policy.approval.write_text(json.dumps(approval))
    else:
        approval = json.loads(policy.approval.read_text())
        approval["files"]["scripts/run_f01_migration.py"] = "0" * 64
        policy.approval.write_text(json.dumps(approval))
    with pytest.raises(g3.Blocked):
        g3.run(policy, execute=True)
    assert g3.sha(Path(m["work"])) == before
    assert engine.EXECUTION_ENABLED is False


def test_sampler_interrupts_long_sql_without_hooks(bundle, monkeypatch):
    policy, m, engine = bundle
    mon = g3.Monitor(policy)
    c = sqlite3.connect(":memory:")
    c.create_function("slow", 1, lambda n: (time.sleep(0.05), n)[1])
    mon.connection = c

    class Prep:
        def memory_usage(self):
            return {"rss_bytes": 1, "process_peak_rss_bytes": 1}

    mon.prep = Prep()

    def no_space():
        return {"free_bytes": 0, "required_remaining_bytes": 1}

    monkeypatch.setattr(mon, "budget", no_space)
    mon.begin()
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            c.execute(
                "WITH x(n) AS (VALUES(1),(2),(3),(4)) "
                "SELECT slow(a.n) FROM x a CROSS JOIN x b CROSS JOIN x d"
            ).fetchall()
        assert mon.cancel.is_set()
    finally:
        mon.close()
        c.close()


def test_process_exit_during_index_build_resumes_same_work(bundle):
    import subprocess

    policy, m, engine = bundle
    script = (
        "import sys,os; sys.path.insert(0,sys.argv[1]); "
        "import run_f01_migration as g; from pathlib import Path; "
        "g.run(g.Policy(verified=Path(sys.argv[2]),approval=Path(sys.argv[3])), "
        "execute=True,batch_size=2,hook=lambda event,c: os._exit(73) "
        'if event=="after_index_build" else None)'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(g3.ROOT / "scripts"),
            str(policy.verified),
            str(policy.approval),
        ],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 73, result.stderr
    assert engine.EXECUTION_ENABLED is False
    assert g3.run(policy, execute=True, resume=True)["status"] == "G3_COMPLETE"


def test_extra_membership_validation_is_inside_complete_transaction(bundle):
    policy, m, engine = bundle
    m["expected_counts"]["source_memberships"] = 99
    v = policy.verified
    (v / "f01_copy_rehearsal_manifest.json").write_text(json.dumps(m))
    report = json.loads((v / "f01_preparation_report.json").read_text())
    report["expected_counts"] = m["expected_counts"]
    (v / "f01_preparation_report.json").write_text(json.dumps(report))
    (v / "seal.json").write_text(json.dumps({name: g3.sha(v / name) for name in g3.SEAL_FILES}))
    approval = json.loads(policy.approval.read_text())
    approval["manifest_sha256"] = g3.sha(v / "f01_copy_rehearsal_manifest.json")
    policy.approval.write_text(json.dumps(approval))
    with pytest.raises(g3.Blocked, match="COUNTS_MISMATCH"):
        g3.run(policy, execute=True, batch_size=2)
    with sqlite3.connect(m["work"]) as c:
        assert c.execute("SELECT state,rows_done FROM canonical_migration").fetchone() == (
            "BUILDING",
            4,
        )
        assert (
            c.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_occ_v1'").fetchone() is None
        )
    c.close()
    assert engine.EXECUTION_ENABLED is False
