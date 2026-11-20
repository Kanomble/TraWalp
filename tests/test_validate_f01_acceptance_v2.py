"""Synthetic metadata and guard probes for the scoped bridge."""

import copy
import importlib.util
import json
import socket
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "f01_acceptance_v2", Path(__file__).resolve().parents[1] / "scripts/validate_f01_acceptance_v2.py"
)
v2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = v2
SPEC.loader.exec_module(v2)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected database access or network connection")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected)


def write_seal(directory, name, members):
    seal = {member: v2.g3.sha(directory / member) for member in members}
    (directory / name).write_text(json.dumps(seal), encoding="utf-8")


def test_original_report_rejects_tampering_and_resealed_rebinding(tmp_path):
    monitor = SimpleNamespace(check=lambda: None)
    binding = {"work_sha256": "a" * 64}
    base = {"binding": binding, "phase": "g4", "status": v2.INSUFFICIENT,
            "references_unchanged": True, "executor_default_restored": True,
            "production_cutover_authorized": False}
    expected = v2.digest(base)
    name = "f01_migration_acceptance_g4.json"
    members = {name, "f01_migration_acceptance_audit.md", "result_fingerprint.json", "progress.jsonl"}
    for other in ("f01_migration_acceptance_audit.md", "progress.jsonl"):
        (tmp_path / other).touch()

    def write_report(report, digest=None):
        (tmp_path / name).write_text(json.dumps(report), encoding="utf-8")
        (tmp_path / "result_fingerprint.json").write_text(json.dumps({
            "contract": v2.old.CONTRACT, "report_fingerprint": digest or v2.digest(report),
        }), encoding="utf-8")

    write_report(base)
    write_seal(tmp_path, "result_seal.json", members)
    assert v2.load_historical(tmp_path, "g4", binding, expected, monitor) == base
    write_report({**base, "gaps": ["rewritten"]}, expected)
    with pytest.raises(v2.Blocked, match="SEAL_DRIFT"):
        v2.load_historical(tmp_path, "g4", binding, expected, monitor)
    write_seal(tmp_path, "result_seal.json", members)
    with pytest.raises(v2.Blocked, match="PHASE_FINGERPRINT_DRIFT"):
        v2.load_historical(tmp_path, "g4", binding, expected, monitor)
    write_report({**base, "gaps": ["rewritten"]})
    write_seal(tmp_path, "result_seal.json", members)
    with pytest.raises(v2.Blocked, match="UNAPPROVED_HISTORICAL_REPORT"):
        v2.load_historical(tmp_path, "g4", binding, expected, monitor)
    write_report({**base, "binding": {"work_sha256": "b" * 64}})
    write_seal(tmp_path, "result_seal.json", members)
    with pytest.raises(v2.Blocked, match="PHASE_BINDING_MISMATCH"):
        v2.load_historical(tmp_path, "g4", binding, expected, monitor)


def test_review_union_and_historical_bindings_are_separate():
    roles = (v2.g3.ADDITIONS, v2.old.ADDITIONS, v2.supp.ADDITIONS, v2.ADDITIONS)
    reviews = [({"review_status": "REVIEWED_ADDITIVE_ONLY", "reviewer": "fixture",
                 "manifest_sha256": "m", "base_git_head": "base",
                 "files": dict.fromkeys(paths, "a")}, paths) for paths in roles]
    expanded = v2.merge_reviews(reviews, "base", "m")
    authority = {
        "manifest_sha256": "m", "g3_result_fingerprint": "g3", "work_sha256": "w",
        "original_code_fingerprint": "code", "g4_report_fingerprint": "g4",
        "historical_reviews_sha256": {"g4": "g4-review", "supplement": "supp-review"},
    }
    g4, supp = v2.historical_bindings(authority, expanded, {"g3": "seal"}, {"g4": "seal"})
    assert set(g4["additions"]) == roles[0] | roles[1]
    assert set(supp["additions"]) == roles[0] | roles[1] | roles[2]
    assert supp["original_g4_binding"] == g4
    duplicate, paths = copy.deepcopy(reviews[0])
    assert v2.merge_reviews([*reviews, (duplicate, paths)], "base", "m") == expanded
    duplicate["files"][next(iter(paths))] = "b"
    with pytest.raises(v2.Blocked, match="CONFLICTING_ADDITION"):
        v2.merge_reviews([*reviews, (duplicate, paths)], "base", "m")
    duplicate["files"]["unreviewed.py"] = "a"
    with pytest.raises(v2.Blocked, match="INCOMPLETE_OR_UNBOUND"):
        v2.merge_reviews([*reviews, (duplicate, paths)], "base", "m")


def test_snapshot_assessment_keeps_b_c_d_and_every_gate_open_or_blocked():
    dimensions = dict(zip("ABCD", (
        "STORAGE_AND_SEMANTIC_PARITY", "QUALIFIED_FORWARD_RANKING_PARITY",
        "HISTORICAL_IDENTITY_EVIDENCE", "OPERATIONAL_CHAMPION_READINESS",
    ), strict=True))
    approval = {"dimensions": dimensions,
                "bindings": {"manifest_sha256": "m", "g3_result_fingerprint": "g3",
                             **{p + "_report_fingerprint": p for p in ("g4", "ranking", "identity")}},
                "coverage": {"occurrences": 1, "pit_accounting_comparisons": 1,
                             "non_fact_tables": 1, "ranking_companies": 1, "identity_symbols": 1,
                             "unresolved_forward_candidates": 1}}
    missing = {"status": v2.INSUFFICIENT, "gaps": ["unchanged gap"]}
    tables = {"companies": {"rows": 1}}
    manifest = {"input_inventory": {"database": {"tables": tables}}}
    universe = {"companies": [{"symbol": "FIXTURE"}], "session": "2026-11-06",
                "selection": "ALL_PRODUCTION_TRADABLE_COMPANIES"}
    legacy = {"ranking_valid": True, "f_ranking": [{"rank": 1}], "quality_value_ranking": [{"rank": 1}]}
    forward = {"ranking_valid": False, "qualification": {"unresolved_rank_relevant_count": 1},
               "f_ranking": [], "quality_value_ranking": []}
    modes = {mode: {"universe": universe, "universe_fingerprint": v2.digest(universe),
                    "parity": True, "evidence": value}
             for mode, value in (("legacy_ticker", legacy), ("forward_cik_sip", forward))}
    evidence = v2.Evidence(
        manifest, None, approval, {},
        g4={**missing, "matrix": [{}], "non_fact_tables": tables, "full_stream_proof": "fixture",
            "scores": [{"reader": mode, "session": universe["session"]} for mode in modes]},
        ranking={**missing, "modes": modes, "strategy_changed": False, "selection_uses_work_scores": False},
        identity={
            **missing, "census": [{"symbol": "FIXTURE", "classification": "SINGLE_CIK_PROVENANCE",
                                   "fact_provenance": [{"occurrences": 1}]}],
            "classification_counts": {"SINGLE_CIK_PROVENANCE": 1},
            "reader_witnesses": [{}], "census_source_equivalence": "fixture",
            "identity_epochs": [], "real_reuse_witness_count": 0,
            "synthetic_regression": {"status": "PASS", "material": "SYNTHETIC_IN_MEMORY_ONLY",
                                     "real_reuse_credit": False, "legacy_forward_parity": True,
                                     "no_cik_merge_or_fallback": True},
        },
    )
    report = v2.assess(evidence)
    assert [report["dimensions"][name]["status"] for name in dimensions.values()] == [
        "EVIDENCE_SATISFIED_FOR_BOUND_SNAPSHOT", v2.INSUFFICIENT, "NOT_ESTABLISHED", "NOT_RELEASED",
    ]
    assert report["historical_results"]["g0_g2"]["status"] == "G0_G2_READY"
    assert report["historical_results"]["g3"]["status"] == "G3_COMPLETE"
    assert all(report["historical_results"][p]["status"] == v2.INSUFFICIENT
               for p in ("g4", "ranking", "identity"))
    assert all(report[key] is False for key in (
        "legacy_gate_override", "rehearsal_go", "operational_release_authorized", "production_cutover_authorized",
    ))
    evidence.identity["synthetic_regression"]["real_reuse_credit"] = True
    with pytest.raises(v2.Blocked, match="SYNTHETIC_IDENTITY_PROOF_MISSING"):
        v2.assess(evidence)


def test_g6_confirmation_and_both_capacity_reserves(tmp_path):
    parser = v2.parser()
    for flags in (["--g6"], ["--rebuild-recovery"], ["--authorize-recovery-copies"],
                  ["--g6", "--authorize-recovery-copies", "--batch-size", "50001"]):
        with pytest.raises(v2.Blocked):
            v2.run(parser.parse_args(flags))
    v2.authorize(parser.parse_args(["--g6", "--authorize-recovery-copies", "--rebuild-recovery"]))
    job, temp = tmp_path / "job", tmp_path / "temp"
    reserve, envelope = 20 * v2.old.GIB, 2 * v2.old.GIB
    for free, temp_free in ((reserve + envelope - 1, reserve), (reserve + envelope, reserve - 1),
                            (reserve + envelope, reserve)):
        monitor = v2.old.Monitor(SimpleNamespace(job=job))
        monitor.manifest = {"fixture": True}
        slots = {job: free, temp: temp_free}
        monitor.prep = SimpleNamespace(available=slots.__getitem__, windows_temp=lambda: temp,
                                       memory_usage=lambda: {"rss_bytes": 0})
        if free < reserve + envelope or temp_free < reserve:
            with pytest.raises(v2.Blocked, match="PHASE_CAPACITY_OR_TEMP_RESERVE_VIOLATED"):
                monitor.capacity(envelope)
        else:
            monitor.capacity(envelope)
            assert monitor.budget()["required_remaining_bytes"] == reserve + envelope


def test_g6_pins_locks_and_rechecks_before_sealed_publication(tmp_path, monkeypatch):
    pins, operations = [], []
    locked = False

    @contextmanager
    def pin(paths):
        pins.append(set(paths))
        try:
            yield
        finally:
            pins.pop()

    @contextmanager
    def lock(path):
        assert path == tmp_path / "f01_acceptance.lock"
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    refs = {"reference_source", "reference_backup", "reference_work", "restore_reference"}

    def verify_files(*args):
        assert locked and refs in pins
        operations.append("verify")

    def recover(manifest, prep, monitor, attempt, rebuild, batch_size):
        assert locked and pins == [{"metadata"}, refs, {"work"}]
        assert rebuild and batch_size == 5000
        assert operations == ["verify"]
        assert not (attempt / "result").exists()
        operations.append("recover")
        return {"status": "PASS", "gaps": [], "crash_probes": [{"event": name} for name in (
            "before_batch_commit", "after_batch_commit", "during_index_build", "complete",
        )]}

    manifest = {role: str(tmp_path / (role + ".stub")) for role in ("source", "backup", "work")}
    manifest["restore_evidence"] = {"path": str(tmp_path / "restore.stub")}
    for path in (*[manifest[role] for role in ("source", "backup", "work")], manifest["restore_evidence"]["path"]):
        Path(path).touch()
    manifest["input_inventory"] = {"repository": {"files": {
        "src/trading_system/data/canonical_paths.py": "fixture",
    }}}
    policy = SimpleNamespace(job=tmp_path, verified=tmp_path, contract_approval=tmp_path, root=v2.ROOT)
    prep = SimpleNamespace(pinned_files=pin, available=lambda _: 100 * v2.old.GIB, windows_temp=lambda: tmp_path,
                           memory_usage=lambda: {"rss_bytes": 0},
                           migration=SimpleNamespace(EXECUTION_ENABLED=False))
    evidence = SimpleNamespace(manifest=manifest, prep=prep, binding={"original_g4_binding": {}})
    approval = {"bindings": {"manifest_sha256": "fixture"}}
    monkeypatch.setattr(v2, "APPROVAL_DIGEST", v2.digest(approval))
    monkeypatch.setattr(v2.g3, "read_json", lambda *_: approval)
    monkeypatch.setattr(v2, "verified_metadata_file", lambda *_: manifest)
    monkeypatch.setattr(v2, "metadata_paths", lambda *_: {"metadata": tmp_path})
    monkeypatch.setattr(v2, "bound_module", lambda *_: SimpleNamespace(pinned_files=pin))
    monkeypatch.setattr(v2, "verify_metadata", lambda *_: evidence)
    monkeypatch.setattr(v2, "assess", lambda _: {"production_cutover_authorized": False})
    monkeypatch.setattr(v2.g3, "execution_lock", lock)
    monkeypatch.setattr(v2.old, "verify_files", verify_files)
    monkeypatch.setattr(v2.old, "g6_phase", recover)

    assert v2.run(v2.parser().parse_args([]), policy) == 0
    assert operations == []
    assert not (tmp_path / "acceptance_v2_runs").exists()
    args = v2.parser().parse_args(["--g6", "--authorize-recovery-copies", "--rebuild-recovery"])
    assert v2.run(args, policy) == 0
    assert operations == ["verify", "recover", "verify"]
    assert not locked and not pins
    result, = (tmp_path / "acceptance_v2_runs").glob("*/result")
    payload = json.loads((result / "f01_acceptance_v2_g6.json").read_text(encoding="utf-8"))
    seal = json.loads((result / "seal.json").read_text(encoding="utf-8"))
    assert payload["status"] == "PASS" and payload["production_cutover_authorized"] is False
    assert seal == {name: v2.g3.sha(result / name) for name in (
        "f01_acceptance_v2_g6.json", "fingerprint.json", "progress.jsonl",
    )}


def test_future_g5_needs_separate_review_and_rejects_adapter_only(tmp_path):
    authority = {"manifest_sha256": "m", "g3_result_fingerprint": "g3", "work_sha256": "w",
                 "original_code_fingerprint": "original"}
    evidence = SimpleNamespace(approval={"bindings": authority})
    binding = {"approval_fingerprint": v2.APPROVAL_DIGEST, "manifest_sha256": "m",
               "g3_result_fingerprint": "g3", "work_sha256": "w", "candidate_code_fingerprint": "a" * 64}
    name = "f01_g5_integration_report.json"
    report = {"contract": v2.G5_CONTRACT, "binding": binding, "status": "PASS", "gaps": [],
              "scope": "ISOLATED_DATA_SYNCHRONIZER_INTEGRATION", "adapter_only": False,
              "probes": dict.fromkeys(v2.G5_PROBES, "PASS"), "production_cutover_authorized": False}
    monitor = SimpleNamespace(check=lambda: None)

    def seal():
        for filename, payload in {
            name: report,
            "fingerprint.json": {"contract": v2.G5_CONTRACT, "report_fingerprint": v2.digest(report)},
        }.items():
            (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")
        review = tmp_path / "review.json"
        review.write_text(json.dumps({
            "decision": "REVIEWED_ISOLATED_G5_INTEGRATION", "reviewer": "fixture",
            "reviewed_at_utc": "2026-11-20T00:00:00Z", "binding": binding,
            "report_fingerprint": v2.digest(report), "report_sha256": v2.g3.sha(tmp_path / name),
            "operational_release_authorized": False,
        }), encoding="utf-8")
        write_seal(tmp_path, "seal.json", v2.G5_FILES)
        return v2.g3.sha(review)

    approved_review = seal()
    result = v2.g5_evidence(tmp_path, approved_review, evidence, monitor)
    assert result["status"] == "REVIEWED_INTEGRATION_EVIDENCE_RECORDED"
    report["adapter_only"] = True
    replacement = seal()
    with pytest.raises(v2.Blocked, match="APPROVED_FILE_DRIFT"):
        v2.g5_evidence(tmp_path, approved_review, evidence, monitor)
    with pytest.raises(v2.Blocked, match="G5_INTEGRATION_EVIDENCE_INCOMPLETE"):
        v2.g5_evidence(tmp_path, replacement, evidence, monitor)
