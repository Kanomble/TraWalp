#!/usr/bin/env python3
"""Scoped F01 evidence and explicitly authorized isolated G6 recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import types
import uuid
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "F01_ACCEPTANCE_SCOPES_V2"
APPROVAL_DIGEST = "d1a10d358684ca2a2d3962af015e33ea3bb8adcfb66db9fcd9dff82e695a876f"
ADDITIONS = frozenset({
    "scripts/validate_f01_acceptance_v2.py",
    "tests/test_validate_f01_acceptance_v2.py",
    "docs/f01_acceptance_v2.md",
    "docs/f01_acceptance_v2_approval.json",
    "reports/f01_acceptance_contract_resolution_audit.md",
    "reports/f01_acceptance_bridge_audit.md",
})
RESULT_PATHS = {
    "g3": "g3_runs/20261115T120802_b1cbe578e8f94f969ba6474140477dd2/result",
    "g4": "acceptance_runs/2026-11-15T151756_0000_g4_747e7c788562433d969f4aca340fffaa/result",
    "ranking": "g4_supplement_runs/2026-11-15T175717_0000_ranking_1104d6b9c7324358bf8d89fad8ca3a42/result",
    "identity": "g4_supplement_runs/2026-11-15T193911_0000_identity_41123deee1424f239adb90baf2ee22fd/result",
}
G5_CONTRACT = "F01_G5_INTEGRATION_EVIDENCE_V1"
G5_FILES = {"f01_g5_integration_report.json", "review.json", "fingerprint.json"}
G5_PROBES = frozenset({
    "regular_full_and_incremental_sync", "canonical_writer_atomicity",
    "commit_failure_and_receipt_replay", "cursor_and_stage_recovery",
    "readiness_failure_propagation", "schema_and_identity_guards",
})
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


def bound_module(path, expected, name):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("BOUND_MODULE_DRIFT_BEFORE_IMPORT: " + path.name)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


supp = bound_module(
    ROOT / "scripts/validate_f01_g4_supplement.py",
    "052943ba338088db5954bbe331552ae3f60293a4aff80741f14f7f4e367f93fd",
    "_f01_acceptance_v2_supplement",
)
old, g3 = supp.old, supp.g3
Blocked, require, digest = old.Blocked, old.require, old.digest


@dataclass(frozen=True)
class Policy(supp.Policy):
    contract_approval: Path = ROOT / "docs/f01_acceptance_v2_approval.json"
    bridge_review: Path = ROOT / "reports/f01_acceptance_v2_additions_review.json"


@dataclass(frozen=True)
class Evidence:
    manifest: dict
    prep: types.ModuleType
    approval: dict
    binding: dict
    g4: dict
    ranking: dict
    identity: dict


def verified_metadata_file(path, expected, monitor):
    """Check the exact bytes parsed, including on the first discovery pass."""
    monitor.check()
    raw = g3.safe(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected, "APPROVED_FILE_DRIFT: " + path.name)
    return json.loads(raw.decode("utf-8-sig"))


def merge_reviews(reviews, base, manifest_sha):
    combined = {}
    for review, expected_names in reviews:
        require(
            review.get("review_status") == "REVIEWED_ADDITIVE_ONLY"
            and review.get("reviewer")
            and review.get("manifest_sha256") == manifest_sha
            and review.get("base_git_head") == base
            and set(review.get("files", {})) == expected_names,
            "INCOMPLETE_OR_UNBOUND_ADDITIONS_REVIEW",
        )
        for path, sha in review["files"].items():
            require(path not in combined or combined[path] == sha, "CONFLICTING_ADDITION: " + path)
            combined[path] = sha
    return combined


def verify_expanded_bundle(policy, historical_g3_review, additions, monitor):
    """Only the repository gate sees the expanded review, in private globals."""
    union_review = {**historical_g3_review, "files": additions}
    namespace = dict(vars(g3), ADDITIONS=frozenset(additions))
    namespace["read_json"] = lambda path: (
        union_review if path == policy.approval else g3.read_json(path)
    )
    # Git's porcelain diff may otherwise refresh the index opportunistically.
    namespace["subprocess"] = types.SimpleNamespace(
        run=lambda argv, **kwargs: subprocess.run(
            [argv[0], "--no-optional-locks", *argv[1:]], **kwargs
        )
    )
    for name in ("git_changes", "verify_bundle"):
        fn = getattr(g3, name)
        namespace[name] = types.FunctionType(fn.__code__, namespace, name, fn.__defaults__, fn.__closure__)
    return namespace["verify_bundle"](policy, monitor)


def historical_bindings(authority, additions, g3_seal, g4_seal):
    common = {key: authority[key] for key in (
        "manifest_sha256", "g3_result_fingerprint", "work_sha256", "original_code_fingerprint",
    )}
    review_shas = authority["historical_reviews_sha256"]
    g4_binding = {
        **common, "contract": old.CONTRACT, "g3_seal": g3_seal,
        "review_sha256": review_shas["g4"],
        "additions": {name: additions[name] for name in g3.ADDITIONS | old.ADDITIONS},
    }
    supplement_binding = {
        **common, "contract": supp.CONTRACT, "original_g4_binding": g4_binding,
        "g4_report_fingerprint": authority["g4_report_fingerprint"], "g4_seal": g4_seal,
        "additions": {name: additions[name] for name in g3.ADDITIONS | old.ADDITIONS | supp.ADDITIONS},
        "supplement_review_sha256": review_shas["supplement"],
    }
    return g4_binding, supplement_binding


def load_historical(path, phase, binding, expected, monitor):
    report = (old.read_phase(path, phase, binding, monitor) if phase == "g4"
              else supp.load_supplement(path, binding, monitor))
    require(
        report["phase"] == phase and report["status"] == INSUFFICIENT
        and digest(report) == expected,
        "UNAPPROVED_HISTORICAL_REPORT: " + phase,
    )
    if phase != "g4":
        require(report.get("cutover_go") is False, "SUPPLEMENT_CUTOVER_CLAIM")
        supp.validate_clearances(report)
    return report


def seal_paths(policy, results):
    return {"preparation": policy.verified / "seal.json",
            **{phase: path / ("result_seal.json" if phase in {"g3", "g4"} else "seal.json")
               for phase, path in results.items()}}


def verify_metadata(policy, results, monitor):
    approval = g3.read_json(policy.contract_approval)
    require(digest(approval) == APPROVAL_DIGEST and approval["contract"] == CONTRACT,
            "SCOPED_APPROVAL_DRIFT")
    authority = approval["bindings"]
    manifest_sha = authority["manifest_sha256"]
    seals = {phase: verified_metadata_file(path, authority["seal_sha256"][phase], monitor)
             for phase, path in seal_paths(policy, results).items()}
    reviews = {phase: verified_metadata_file(path, authority["historical_reviews_sha256"][phase], monitor)
               for phase, path in (("g3", policy.approval), ("g4", policy.acceptance_review),
                                   ("supplement", policy.supplement_review))}
    bridge_review = g3.read_json(policy.bridge_review)
    require(bridge_review.get("contract") == CONTRACT
            and bridge_review.get("approval_fingerprint") == APPROVAL_DIGEST
            and bridge_review.get("historical_bindings") == authority,
            "V2_REVIEW_AUTHORITY_DRIFT")
    additions = merge_reviews(
        [(reviews["g3"], g3.ADDITIONS), (reviews["g4"], old.ADDITIONS),
         (reviews["supplement"], supp.ADDITIONS), (bridge_review, ADDITIONS)],
        reviews["g3"]["base_git_head"], manifest_sha,
    )
    require(all(reviews[p]["g3_result_fingerprint"] == authority["g3_result_fingerprint"]
                for p in ("g4", "supplement"))
            and reviews["supplement"]["g4_report_fingerprint"] == authority["g4_report_fingerprint"]
            and all(reviews[p]["original_code_fingerprint"] == authority["original_code_fingerprint"]
                    for p in ("g4", "supplement")),
            "HISTORICAL_REVIEW_PARENT_DRIFT")
    manifest, prep, g0_binding = verify_expanded_bundle(policy, reviews["g3"], additions, monitor)
    require(g0_binding["seal"] == seals["preparation"]
            and seals["preparation"]["f01_copy_rehearsal_manifest.json"] == manifest_sha
            and manifest["input_inventory"]["repository"]["code_fingerprint"] == authority["original_code_fingerprint"],
            "PREPARATION_BINDING_DRIFT")
    g3_result, g3_fp, g3_seal = old.g3_result(results["g3"], manifest_sha, monitor)
    require(g3_fp == authority["g3_result_fingerprint"] and g3_seal == seals["g3"]
            and g3_result["binding"]["reviewed_additions"] == reviews["g3"]
            and g3_result["binding"]["seal"] == seals["preparation"]
            and g3_result["work_path"] == manifest["work"]
            and g3_result["work_sha256"] == authority["work_sha256"],
            "G3_BINDING_DRIFT")
    validation = g3_result["validation"]
    expected = manifest["expected_counts"]
    require(
        all(validation.get(key) is True for key in (
            "original_ids_preserved", "one_mapping_per_original",
            "executor_full_legacy_forward_stream_check", "executor_integrity_and_fk_check",
        ))
        and {k: v for k, v in expected.items() if k != "content_conflicts"} == validation["counts"]
        and expected["occurrences"] == approval["coverage"]["occurrences"],
        "G3_STORAGE_PROOF_MISSING",
    )
    g4_binding, supp_binding = historical_bindings(authority, additions, g3_seal, seals["g4"])
    reports = {
        phase: load_historical(results[phase], phase, binding, authority[phase + "_report_fingerprint"], monitor)
        for phase, binding in (("g4", g4_binding), ("ranking", supp_binding), ("identity", supp_binding))
    }
    binding = {
        "contract": CONTRACT, "approval_fingerprint": APPROVAL_DIGEST,
        "historical_bindings": authority, "original_g4_binding": g4_binding,
        "original_supplement_binding": supp_binding, "reviewed_additions": additions,
        "bridge_review_sha256": g3.sha(policy.bridge_review, monitor),
        "checked_git_head": g0_binding["current_head"],
    }
    return Evidence(manifest, prep, approval, binding, **reports)


def assess(evidence):
    """Evaluate the approved snapshot only. A mixed result never lifts a gate."""
    g4, ranking, identity = evidence.g4, evidence.ranking, evidence.identity
    coverage = evidence.approval["coverage"]
    inherited = supp.reused_evidence(g4)
    require(
        inherited["pit_accounting_comparisons"] == coverage["pit_accounting_comparisons"]
        and len(inherited["non_fact_tables"]) == coverage["non_fact_tables"]
        and g4["non_fact_tables"] == {
            name: item for name, item in evidence.manifest["input_inventory"]["database"]["tables"].items()
            if name not in {"fundamental_facts", "sqlite_sequence"}
        },
        "G4_SEMANTIC_PROOF_MISSING",
    )
    require(set(ranking["modes"]) == {"legacy_ticker", "forward_cik_sip"}
            and ranking.get("strategy_changed") is False
            and ranking.get("selection_uses_work_scores") is False, "RANKING_SCOPE_DRIFT")
    for mode, row in ranking["modes"].items():
        universe = row["universe"]
        symbols = {item["symbol"] for item in universe["companies"]}
        require(
            row["parity"] is True and row["universe_fingerprint"] == digest(universe)
            and len(universe["companies"]) == len(symbols) == coverage["ranking_companies"]
            and universe["selection"] == "ALL_PRODUCTION_TRADABLE_COMPANIES"
            and universe["session"] == str(supp.original_session(g4, mode)),
            "RANKING_UNIVERSE_DRIFT: " + mode,
        )
    legacy = ranking["modes"]["legacy_ticker"]["evidence"]
    forward = ranking["modes"]["forward_cik_sip"]["evidence"]
    require(legacy["ranking_valid"] is True
            and all(legacy[key] and legacy[key][0]["rank"] == 1
                    for key in ("f_ranking", "quality_value_ranking")), "LEGACY_RANK1_MISSING")
    unresolved = forward["qualification"]["unresolved_rank_relevant_count"]
    require(unresolved == coverage["unresolved_forward_candidates"] > 0
            and forward["ranking_valid"] is False
            and forward["f_ranking"] == forward["quality_value_ranking"] == [],
            "FORWARD_QUALIFICATION_BOUNDARY_DRIFT")
    census, synthetic = identity["census"], identity["synthetic_regression"]
    require(
        len(census) == len({row["symbol"] for row in census}) == coverage["identity_symbols"]
        and Counter(row["classification"] for row in census) == identity["classification_counts"]
        and sum(p["occurrences"] for row in census for p in row["fact_provenance"]) == coverage["occurrences"]
        and bool(identity["reader_witnesses"]) and bool(identity["census_source_equivalence"])
        and identity["identity_epochs"] == [] and identity["real_reuse_witness_count"] == 0,
        "REAL_IDENTITY_INVENTORY_DRIFT",
    )
    require(
        synthetic["status"] == "PASS" and synthetic["material"] == "SYNTHETIC_IN_MEMORY_ONLY"
        and synthetic["real_reuse_credit"] is False and synthetic["legacy_forward_parity"] is True
        and synthetic["no_cik_merge_or_fallback"] is True, "SYNTHETIC_IDENTITY_PROOF_MISSING",
    )
    statuses = {
        "A": {"status": "EVIDENCE_SATISFIED_FOR_BOUND_SNAPSHOT", "coverage": coverage,
              "synthetic_identity_regression": "PASS", "real_reuse_credit": False},
        "B": {"status": INSUFFICIENT, "unresolved_rank_relevant_candidates_per_side": unresolved,
              "qualification_parity": True, "qualified_forward_rank_1": None},
        "C": {"status": "NOT_ESTABLISHED", "identity_epochs": 0, "authoritative_reuse_witnesses": 0},
        "D": {"status": "NOT_RELEASED"},
    }
    historical = {
        phase: {"status": report["status"], "gaps": report["gaps"],
                "clears": report.get("clears", []),
                "report_fingerprint": evidence.approval["bindings"][phase + "_report_fingerprint"]}
        for phase, report in (("g4", g4), ("ranking", ranking), ("identity", identity))
    }
    authority = evidence.approval["bindings"]
    return {
        "contract": CONTRACT,
        "dimensions": {name: statuses[letter] for letter, name in evidence.approval["dimensions"].items()},
        "historical_results": {
            "g0_g2": {"status": "G0_G2_READY", "manifest_sha256": authority["manifest_sha256"]},
            "g3": {"status": "G3_COMPLETE", "result_fingerprint": authority["g3_result_fingerprint"]},
            **historical,
        },
        "legacy_gate_override": False, "rehearsal_go": False,
        "operational_release_authorized": False, "production_cutover_authorized": False,
    }


def g5_evidence(directory, review_sha, evidence, monitor):
    authority = evidence.approval["bindings"]
    review = verified_metadata_file(directory / "review.json", review_sha, monitor)
    old.sealed(directory, "seal.json", G5_FILES, monitor)
    path = directory / "f01_g5_integration_report.json"
    report = g3.read_json(path)
    fingerprint = digest(report)
    require(g3.read_json(directory / "fingerprint.json") == {
        "contract": G5_CONTRACT, "report_fingerprint": fingerprint,
    }, "G5_INTEGRATION_FINGERPRINT_DRIFT")
    candidate = report["binding"]["candidate_code_fingerprint"]
    binding = {
        "approval_fingerprint": APPROVAL_DIGEST,
        "manifest_sha256": authority["manifest_sha256"],
        "g3_result_fingerprint": authority["g3_result_fingerprint"],
        "work_sha256": authority["work_sha256"],
        "candidate_code_fingerprint": candidate,
    }
    require(
        report.get("contract") == G5_CONTRACT and report["binding"] == binding
        and re.fullmatch(r"[0-9a-f]{64}", candidate)
        and candidate != authority["original_code_fingerprint"]
        and report.get("scope") == "ISOLATED_DATA_SYNCHRONIZER_INTEGRATION"
        and report.get("adapter_only") is False
        and report.get("status") == "PASS" and report.get("gaps") == []
        and report.get("probes") == dict.fromkeys(G5_PROBES, "PASS")
        and report.get("production_cutover_authorized") is False,
        "G5_INTEGRATION_EVIDENCE_INCOMPLETE",
    )
    require(
        review.get("decision") == "REVIEWED_ISOLATED_G5_INTEGRATION"
        and review.get("reviewer") and review.get("reviewed_at_utc")
        and review.get("binding") == binding and review.get("report_fingerprint") == fingerprint
        and review.get("report_sha256") == g3.sha(path, monitor)
        and review.get("operational_release_authorized") is False,
        "G5_INTEGRATION_REVIEW_DRIFT",
    )
    return {"status": "REVIEWED_INTEGRATION_EVIDENCE_RECORDED", "binding": binding,
            "report_fingerprint": fingerprint, "review_sha256": review_sha,
            "seal_sha256": g3.sha(directory / "seal.json", monitor)}


def authorize(args):
    require(not args.g6 or args.authorize_recovery_copies, "G6_REQUIRES_--authorize-recovery-copies")
    require(args.g6 or not (args.authorize_recovery_copies or args.rebuild_recovery),
            "RECOVERY_FLAGS_REQUIRE_--g6")
    require(1 <= args.batch_size <= 50000, "BOUNDED_RECOVERY_BATCH_REQUIRED")
    require(bool(args.g5_result) == bool(args.g5_review_sha256),
            "G5_RESULT_AND_REVIEW_SHA256_REQUIRED_TOGETHER")
    if args.g5_review_sha256:
        require(re.fullmatch(r"[0-9a-f]{64}", args.g5_review_sha256), "INVALID_G5_REVIEW_SHA256")


def metadata_paths(policy, results, manifest, g5_result):
    # These roles always deny writes. Database paths are added separately for
    # the explicitly requested physical verification and recovery modes.
    historical = seal_paths(policy, results)
    files = [
        *historical.values(), policy.approval, policy.acceptance_review, policy.supplement_review,
        policy.contract_approval, policy.bridge_review,
        *(policy.verified / name for name in g3.SEAL_FILES),
        *(results["g3"] / name for name in (
            "f01_copy_migration_g3_report.json", "f01_copy_migration_g3_audit.md",
            "f01_copy_migration_g3_fingerprint.json",
        )),
        *(results["g4"] / name for name in (
            "f01_migration_acceptance_g4.json", "f01_migration_acceptance_audit.md",
            "progress.jsonl", "result_fingerprint.json",
        )),
        *(results[phase] / name for phase in ("ranking", "identity") for name in supp.RESULT_FILES),
        *(g3.bound_repo_path(policy.root, name) for name in
          (g3.ADDITIONS | old.ADDITIONS | supp.ADDITIONS | ADDITIONS
           | manifest["input_inventory"]["repository"]["files"].keys())),
        *(Path(entry["path"]) for entry in manifest["input_inventory"]["external_review"]["external_files"]),
    ]
    if g5_result:
        files.extend(g5_result / name for name in G5_FILES | {"seal.json"})
    return {"metadata_" + str(i): g3.safe(path) for i, path in enumerate(dict.fromkeys(files))}


def publish_g6(attempt, report, monitor):
    staging = attempt / "unpublished"
    staging.mkdir()
    g3.new_json(staging / "f01_acceptance_v2_g6.json", report)
    g3.new_json(staging / "fingerprint.json", {"contract": CONTRACT, "report_fingerprint": digest(report)})
    with monitor.lock:
        monitor.logfile.flush()
        with (staging / "progress.jsonl").open("xb") as target:
            target.write((attempt / "progress.jsonl").read_bytes())
            target.flush()
            os.fsync(target.fileno())
    seal = {path.name: g3.sha(path, monitor) for path in staging.iterdir()}
    g3.new_json(staging / "seal.json", seal)
    monitor.check()
    result = attempt / "result"
    staging.rename(result)
    return result


def run_g6(args, policy, evidence, assessment, monitor):
    manifest, prep = evidence.manifest, evidence.prep
    g4_binding = evidence.binding["original_g4_binding"]
    attempts = g3.safe(policy.job / "acceptance_v2_runs", file=False)
    attempts.mkdir(exist_ok=True)
    attempt = attempts / (g3.stamp().replace(":", "").replace("+", "_") + "_g6_" + uuid.uuid4().hex)
    attempt.mkdir()
    monitor.run_dir = attempt
    monitor.logfile = (attempt / "progress.jsonl").open("x", encoding="utf-8")
    (attempt / "scratch").mkdir()
    try:
        with prep.pinned_files({"work": attempt / "progress.jsonl"}), old.network_denied():
            result = old.g6_phase(manifest, prep, monitor, attempt, args.rebuild_recovery, args.batch_size)
            require(result["status"] == ("PASS" if args.rebuild_recovery else INSUFFICIENT),
                    "UNEXPECTED_G6_STATUS")
            if args.rebuild_recovery:
                require([probe["event"] for probe in result["crash_probes"]]
                        == ["before_batch_commit", "after_batch_commit", "during_index_build", "complete"]
                        and not result["gaps"], "G6_CRASH_RESUME_EVIDENCE_MISSING")
            monitor.capacity(0, monitor.targets)
            old.verify_files(manifest, g4_binding, prep, monitor)
            require(prep.migration.EXECUTION_ENABLED is False, "EXECUTOR_DEFAULT_NOT_RESTORED")
            report = {
                **assessment, "phase": "g6", "status": result["status"], "g6": result,
                "binding": evidence.binding, "references_unchanged": True,
                "executor_default_restored": True, "at": g3.stamp(),
                "elapsed_seconds": time.monotonic() - monitor.start,
                "resources": prep.memory_usage(), "capacity": monitor.budget(),
            }
            return result["status"], publish_g6(attempt, report, monitor)
    except BaseException as exc:
        try:
            failure = attempt / "failure.json"
            g3.new_json(failure, {
                "contract": CONTRACT, "phase": "g6", "binding": evidence.binding,
                "status": "ABORTED" if isinstance(exc, KeyboardInterrupt) else "BLOCKED",
                "error": f"{type(exc).__name__}: {exc}", "production_cutover_authorized": False,
            })
            print("Failure evidence: " + str(failure), file=sys.stderr, flush=True)
        except OSError as publication_error:
            exc.add_note(f"Could not publish failure evidence: {publication_error}")
        raise


def run(args, policy=None):
    authorize(args)
    policy = policy or Policy()
    physical = args.verify_references or args.g6
    results = {phase: policy.job / path for phase, path in RESULT_PATHS.items()}
    monitor = old.Monitor(policy)
    monitor.phase = "F01 V2 metadata / artifact bindings"
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: monitor.stop(KeyboardInterrupt()))
    try:
        monitor.begin()
        # The approval anchors the manifest before it supplies any pin paths.
        approval = g3.read_json(policy.contract_approval)
        require(digest(approval) == APPROVAL_DIGEST, "SCOPED_APPROVAL_DRIFT")
        manifest = verified_metadata_file(
            policy.verified / "f01_copy_rehearsal_manifest.json",
            approval["bindings"]["manifest_sha256"], monitor,
        )
        pins = metadata_paths(policy, results, manifest, args.g5_result)
        native_pin_path = "src/trading_system/data/canonical_paths.py"
        pin_module = bound_module(
            g3.bound_repo_path(policy.root, native_pin_path),
            manifest["input_inventory"]["repository"]["files"][native_pin_path],
            "_f01_acceptance_v2_native_pins",
        )

        with ExitStack() as stack:
            stack.enter_context(pin_module.pinned_files(pins))
            evidence = verify_metadata(policy, results, monitor)
            require(evidence.manifest == manifest, "PINNED_MANIFEST_DRIFT")
            assessment = assess(evidence)
            assessment["g5"] = (
                g5_evidence(args.g5_result, args.g5_review_sha256, evidence, monitor)
                if args.g5_result else {"status": "NO_REVIEWED_INTEGRATION_EVIDENCE"}
            )
            if physical:
                references = {"reference_" + role: g3.safe(Path(manifest[role]))
                              for role in ("source", "backup", "work")}
                references["restore_reference"] = g3.safe(Path(manifest["restore_evidence"]["path"]))
                stack.enter_context(evidence.prep.pinned_files(references))
                if args.g6:
                    stack.enter_context(g3.execution_lock(g3.safe(policy.job, file=False) / "f01_acceptance.lock"))
                monitor.manifest, monitor.prep = manifest, evidence.prep
                monitor.capacity()
                old.verify_files(manifest, evidence.binding["original_g4_binding"], evidence.prep, monitor)
            if args.g6:
                status, result_path = run_g6(args, policy, evidence, assessment, monitor)
                print(f"{status}: {result_path}", flush=True)
                return 0 if status == "PASS" else 2
            monitor.check()
            print(json.dumps({
                **assessment, "status": "SCOPED_PREFLIGHT_VERIFIED",
                "reference_freshness": "VERIFIED" if physical else "NOT_CHECKED",
                "binding": evidence.binding,
            }, indent=2, ensure_ascii=True), flush=True)
            return 0
    finally:
        monitor.close()
        if monitor.logfile:
            monitor.logfile.close()
        signal.signal(signal.SIGINT, previous_sigint)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--verify-references", action="store_true",
                      help="Read-only physical freshness checks in addition to metadata preflight")
    mode.add_argument("--g6", action="store_true", help="Run the isolated recovery phase")
    cli.add_argument("--authorize-recovery-copies", action="store_true")
    cli.add_argument("--rebuild-recovery", action="store_true",
                     help="Authorize one additional isolated rebuild for crash/resume probes")
    cli.add_argument("--batch-size", type=int, default=5000)
    cli.add_argument("--g5-result", type=Path, help="Absolute path to a separately reviewed integration result")
    cli.add_argument("--g5-review-sha256", help="Independently supplied SHA-256 of the G5 review.json")
    return cli


if __name__ == "__main__":
    try:
        raise SystemExit(run(parser().parse_args()))
    except (KeyboardInterrupt, Exception) as error:
        status = "ABORTED" if isinstance(error, KeyboardInterrupt) else "BLOCKED"
        print(f"{status}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(130 if status == "ABORTED" else 2)
