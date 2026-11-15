"""Few small synthetic fixtures only. Never run documented real paths."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_champion_consolidation import offline as offline
from test_f01_duplicate_impact import current, seeded

SPEC = importlib.util.spec_from_file_location(
    "prepare_f01_migration",
    Path(__file__).resolve().parents[1] / "scripts/prepare_f01_migration.py",
)
prep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prep
SPEC.loader.exec_module(prep)
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows mandatory sharing locks")


def fixture(tmp_path):
    facts = [
        current("cash", 10),
        current("cash", 10),
        current("cash", 10).model_copy(update={"symbol": "OLD"}),
        current("debt_current", 20, "B"),
    ]
    db = seeded(tmp_path / "origin", facts)
    # Keep the original WAL-mode header but close all SQLite connections.
    Path(str(db.path) + "-wal").write_bytes(b"")
    Path(str(db.path) + "-shm").write_bytes(bytes(32768))
    return prep.Paths(db.path, tmp_path / "job")


def review():
    repo = prep.repository_evidence(prep.REPO)
    return repo, dict(
        git_head=repo["git_head"],
        code_fingerprint=repo["code_fingerprint"],
        reviewer="synthetic-test",
        external_inventory_complete=True,
        inputs_complete=True,
        repository_consumers_reviewed=True,
        unknown_consumers=[],
        external_files=[],
    )


@pytest.mark.parametrize("flags", [[], ["--prepare"], ["--confirm-no-writers"]])
def test_flags_cannot_start_preparation(tmp_path, flags):
    paths = fixture(tmp_path)
    before = {p: p.read_bytes() for p in paths.origin.parent.iterdir() if p.is_file()}
    assert prep.main(flags, paths=paths) == 0
    assert not paths.job.exists()
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_complete_small_prepare_preserves_origin_and_validates(tmp_path):
    paths = fixture(tmp_path)
    before = {p: p.read_bytes() for p in paths.origin.parent.iterdir() if p.is_file()}
    baseline = dict(
        occurrences=4,
        instant=4,
        duration=0,
        canonical=2,
        canonical_instant=2,
        canonical_duration=0,
        alias_groups=1,
        content_conflicts=0,
    )
    repo, evidence = review()
    manifest = prep.prepare(paths, repo, evidence, baseline=baseline)
    assert manifest["expected_counts"]["source_memberships"] == 3
    assert prep.migration.validate_work_manifest(manifest) == paths.files()["work"]
    assert manifest["backup_sha256"] == manifest["work_sha256"]
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert prep.migration.EXECUTION_ENABLED is False
    final = paths.job / "verified"
    seal = json.loads((final / "seal.json").read_text(encoding="utf-8"))
    import hashlib

    assert all(
        hashlib.sha256((final / name).read_bytes()).hexdigest() == value
        for name, value in seal.items()
    )
    with sqlite3.connect(paths.files()["work"]) as c:
        assert (
            c.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_fact'").fetchone() is None
        )
    with pytest.raises(prep.Stop, match="existiert bereits"):
        prep.prepare(paths, repo, evidence, baseline=baseline)


@pytest.mark.parametrize("unsafe", ["wal", "handle", "space", "evidence"])
def test_unsafe_preparation_stops_without_outputs(tmp_path, monkeypatch, unsafe):
    paths = fixture(tmp_path)
    if unsafe == "wal":
        Path(str(paths.origin) + "-wal").write_bytes(b"nonempty")
    elif unsafe == "handle":
        opened = sqlite3.connect(paths.origin)
        # An open read/write SQLite handle is incompatible with the no-sharing probe.
    elif unsafe == "space":
        monkeypatch.setattr(prep, "available", lambda _: 0)
    try:
        if unsafe == "evidence":
            assert prep.main(["--prepare", "--confirm-no-writers"], paths=paths) == 2
        else:
            with pytest.raises((prep.Stop, OSError)):
                prep.preflight(paths)
        assert not paths.job.exists()
    finally:
        if unsafe == "handle":
            opened.close()


def test_ctrl_c_retains_failed_artifacts_without_ready(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    repo, evidence = review()

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(prep, "backup_export", interrupt)
    with pytest.raises(KeyboardInterrupt):
        prep.prepare(paths, repo, evidence)
    assert not (paths.job / "verified").exists()
    assert (
        json.loads((paths.job / "failure.json").read_text(encoding="utf-8"))["status"] == "ABORTED"
    )


@pytest.mark.parametrize("reason", ["conflict", "count_drift"])
def test_failed_evidence_never_publishes_ready(tmp_path, reason):
    paths = fixture(tmp_path)
    if reason == "conflict":
        # Generated fixture only, before any preparation locks exist.
        for suffix in ("-wal", "-shm"):
            Path(str(paths.origin) + suffix).unlink()
        with sqlite3.connect(paths.origin) as c:
            c.execute("UPDATE fundamental_facts SET value='99' WHERE id=2")
        c.close()
    repo, evidence = review()
    with pytest.raises(prep.Stop):
        prep.prepare(paths, repo, evidence)
    assert not (paths.job / "verified").exists()
    if reason == "conflict":
        rows = (paths.job / "content_conflicts.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(rows) == 3
    else:
        assert (paths.job / "count_differences.json").exists()
    assert (
        json.loads((paths.job / "failure.json").read_text(encoding="utf-8"))["status"] == "BLOCKED"
    )
