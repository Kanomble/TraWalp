#!/usr/bin/env python3
"""Historical 50 acquisition and local evidence reuse with explicit block authorization."""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE / "src"), str(HERE / "reports")]
sys.dont_write_bytecode = True

import run_f01_historical_50_manual as task9  # noqa: E402
from f01_readiness_inventory import digest, encoding  # noqa: E402
from f01_task96_contract import (  # noqa: E402
    JOBS,
    MARKET,
    OUTPUT,
    Plan,
    allocation,
    authorization,
    current_code,
    key,
    publish,
    require,
)
from f01_task96_evidence import inventory  # noqa: E402
from f01_task96_fundamentals import RULESET, SHARES, analyze, coverage_rows  # noqa: E402
from f01_task96_network import (  # noqa: E402
    current_spy,
    execute,
    known_spy,
    scope_for,
    spy_download,
    spy_execution_scope,
)  # noqa: E402
from f01_task96_process import progress_for  # noqa: E402

from trading_system.data.canonical_facts import fingerprint  # noqa: E402

READY = "AWAITING_EXPLICIT_PROVIDER_AND_BATCH_AUTHORIZATION"


def progress(message):
    print(message, file=sys.stderr, flush=True)


def cache_root():
    return task9.filing.safe_directory(OUTPUT)


def read_progress(plan):
    results = []
    for root in (JOBS, MARKET):
        if not root.is_dir():
            continue
        task9.bulk.directory(root)
        for folder in sorted(root.iterdir()):
            if folder.name not in plan.blocks:
                continue
            task9.bulk.directory(folder)
            path = folder / "result.json"
            state_path = folder / "request-budget.json"
            if not path.is_file() and not state_path.is_file():
                continue
            scope = plan.inputs.json(folder / "scope.json")
            info = {k: v for k, v in scope.items() if k != "authorization_draft"}
            require(
                scope["parent"] == plan.binding()
                and scope["block_id"] == folder.name
                and scope["authorization_draft"]["scope"] == fingerprint(info),
                "PROGRESS_SCOPE_INVALID",
            )
            budget = plan.inputs.json(state_path)
            result = plan.inputs.json(path) if path.is_file() else {}
            require(
                result.get("binding", budget["binding"]) == budget["binding"]
                and budget["binding"]["scope"] == scope["authorization_draft"]["scope"],
                "PROGRESS_RESULT_BINDING_INVALID",
            )
            if result and scope["provider"] == "SPY":

                def reject_missing(_query):
                    raise ValueError("PUBLISHED_SPY_PAGE_MISSING")

                require((folder / "execution-sessions.json").is_file(), "SPY_SESSION_PLAN_MISSING")
                execution = spy_execution_scope(folder, scope, reject_missing)
                verified = spy_download(folder, execution, reject_missing)
                require(
                    verified["sessions_verified"] == result["sessions_verified"],
                    "SPY_RESULT_SESSION_BINDING_MISMATCH",
                )
            results.append(
                {
                    "block_id": folder.name,
                    "provider": scope["provider"],
                    "requests": budget["requests"],
                    "received_bytes": budget["received_bytes"],
                    "uncertain_bytes": budget["uncertain_bytes"],
                    "status": result.get("status", "PENDING_RESUME"),
                    "sec_filings": len(result.get("verified_filings", {})),
                    "spy_sessions": result.get("sessions_verified", []),
                    "processed_bytes": result.get("bytes_archived", 0),
                }
            )
    return results


def sec_remaining(plan, evidence):
    need, held, block_records = [], [], []
    for block in plan.groups[5]:
        block_id = block["block_id"]
        statuses = Counter()
        for row in block["accessions"]:
            item = evidence[key(row)]
            if item["status"] != "ORIGINAL_MISSING":
                status = "LOCAL_REUSE_OR_REVIEW"
            elif item["issues"]:
                status = "BLOCKED_METADATA"
                held.append(
                    {
                        "block_id": block_id,
                        "cik": row["cik"],
                        "accession": row["accession"],
                        "reasons": item["issues"],
                    }
                )
            else:
                status = "DOWNLOAD_REQUIRED"
                need.append({"block_id": block_id, **row})
            statuses[status] += 1
        block_records.append(
            {
                "block_id": block_id,
                "category": block["category"],
                "status": "PLAN_ONLY",
                "counts": dict(statuses),
            }
        )
    review = [
        {
            "block_id": group["block_id"],
            "cik": row["cik"],
            "accession": row["accession"],
            "form": row["form"],
            "reasons": row["problems"],
        }
        for group in plan.groups[6]
        for row in group["accessions"]
    ]
    return {
        "downloads": need,
        "metadata_blocked_in_batches": held,
        "separate_review": review,
        "blocks": block_records,
    }


def local_blocks(plan, evidence):
    return [
        {
            "block_id": block["block_id"],
            "accessions": len(block["accessions"]),
            "counts": dict(Counter(evidence[key(row)]["status"] for row in block["accessions"])),
        }
        for block in plan.groups[1]
    ]


def csv_output(path, rows):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def json_output(folder, name, document):
    task9.bulk.write_new(folder / name, encoding(document))


def offline_report(plan):
    existing = read_progress(plan)
    evidence, documents, reuse = inventory(plan, progress=progress)
    remaining = sec_remaining(plan, evidence)
    findings = analyze(plan)
    coverage = list(coverage_rows(plan, findings, evidence))
    reused_spy = (
        known_spy(plan)
        | current_spy(plan, "2016-01-01", "2026-11-06")
        | {s for item in existing for s in item["spy_sessions"]}
    )
    spy = [scope_for(plan, b["gap_id"], spy=reused_spy) for b in plan.groups[2]]
    spy_pilot = next((b for b in spy if b["status"] == READY), None)
    qualifying = [b for b in remaining["blocks"] if b["counts"].get("DOWNLOAD_REQUIRED")]
    pilot = min(
        qualifying,
        key=lambda b: (not b["category"].startswith("P1"), b["counts"]["DOWNLOAD_REQUIRED"]),
        default=None,
    )
    sec_pilot = scope_for(plan, pilot["block_id"], evidence) if pilot else None
    groups = local_blocks(plan, evidence)
    processing = local_processing_results(plan)
    processed = {key(row): row for record in processing for row in record["filings"]}
    stats = {
        "new_offline_extraction_complete": sum(
            row.get("extraction_status") == "EXTRACTION_COMPLETE" for row in processed.values()
        ),
        "new_offline_v1_accounting_candidates": sum(
            row.get("v1_admitted_candidates", 0) for row in processed.values()
        ),
        "quality_valuation_candidate_cell_proxies": sum(
            row["component_presence_proxy"] == "PRESENT_UNALIGNED" for row in coverage
        ),
        "universe": len(plan.labels),
        "local_blocks_checked": len(groups),
        **reuse,
        "retrospective_shares_candidates_added": len(findings["shares"]),
        "share_issuer_year_cells_added": len(
            {(r["cik"], r["period_end"][:4]) for r in findings["shares"]}
        ),
        "share_conflicts": len(findings["share_conflicts"]),
        "direct_quarter_only_ocf_ttm_windows": len(findings["ocf_windows"]),
        "new_original_pit_values": 0,
        "new_canonical_admissions": 0,
        "originals_still_missing": sum(
            r["status"] == "ORIGINAL_MISSING" for r in evidence.values()
        ),
        "download_candidates": len(remaining["downloads"]),
        "separate_metadata_amendment_review": len(remaining["separate_review"]),
        "blocked_metadata_inside_download_plan": len(remaining["metadata_blocked_in_batches"]),
        "new_sec_filings": sum(r["sec_filings"] for r in existing),
        "new_spy_sessions": len({s for r in existing for s in r["spy_sessions"]}),
        "spy_sessions_still_missing": sum(
            len(w) for s in spy for w in s["network_session_windows"]
        ),
        "spy_entitlement_blocked_windows": sum(s["status"] == READY for s in spy),
        "spy_pre2016_source_blocked_windows": sum(
            s["status"] == "BLOCKED_PRE2016_PROVIDER_UNRESOLVED" for s in spy
        ),
        "network_requests": sum(r["requests"] for r in existing),
        "new_provider_payload_bytes": sum(r["processed_bytes"] for r in existing),
    }
    progress("Publishing evidence, component coverage and pilot preflights")
    with TemporaryDirectory(prefix=".f01-task96-", dir=OUTPUT.parent) as temp:
        target = Path(temp)
        json_output(
            target,
            "sec_evidence_inventory.json",
            {"filings": list(evidence.values()), "documents": documents},
        )
        json_output(target, "local_reuse_report.json", {"block_results": groups, "summary": stats})
        json_output(
            target,
            "sec_remaining_downloads.json",
            {
                "plan_binding": plan.binding(),
                "download_candidates": remaining["downloads"],
                "metadata_blocked_in_batches": remaining["metadata_blocked_in_batches"],
                "separate_review": remaining["separate_review"],
            },
        )
        json_output(
            target, "sec_batch_status.json", {"planned": remaining["blocks"], "actual": existing}
        )
        json_output(
            target, "spy_acquisition_preflight.json", {"scopes": spy, "selected": spy_pilot}
        )
        json_output(
            target,
            "security_blockers.json",
            {
                "state": "BLOCKED_PENDING_VERIFIED_HISTORICAL_SECURITY_EPOCH_AND_ACTIONS",
                "security_and_actions": plan.groups[3],
                "issuer_daily": plan.groups[4],
                "downloadable_issuer_daily": 0,
            },
        )
        json_output(
            target,
            "accounting_admission_summary.json",
            {
                "new_admission_granted": False,
                "offline_processing_reports": processing,
                "new_original_pit_values": 0,
                "historical_e11_e13_exact_witness_facts": stats.get(
                    "cross_lineage_fact_witnesses", 0
                ),
                "nke_archived_reviews_verified_facts": stats.get("archived_nke_admitted_facts", 0),
                "original_only_scope": "AS_FILED_ASSETS_AND_CASH_EXISTING_V1",
                "ocf_ttm_pit_qualified": 0,
                "new_debt_synthesis": False,
            },
        )
        json_output(
            target,
            "remaining_gaps.json",
            {
                "summary": stats,
                "blocking": [
                    "HISTORICAL_SECURITY_EPOCHS_ACTIONS_AND_EXITS",
                    "SPY_PRE2016_SOURCE_AND_POST2016_ENTITLEMENT",
                    "ORIGINAL_PIT_AND_REPORTING_SCOPE_FOR_CHAMPION_COMPONENTS",
                    "ORIGINAL_QUARTER_YTD_AND_TTM_SCOPE_ALIGNMENT",
                ],
            },
        )
        json_output(
            target,
            "fundamental_candidates.json",
            {
                "contract": RULESET,
                "classification": "RESEARCH_CANDIDATES_ONLY",
                "shares": findings["shares"],
                "shares_conflicts": findings["share_conflicts"],
                "ocf_ttm": findings["ocf_windows"],
                "raw_tag_inventory": findings["raw_tags"],
                "staging_dispositions": findings["stage_dispositions"],
                "new_pit_admissions": 0,
            },
        )
        csv_output(target / "fundamental_normalization_coverage.csv", coverage)
        csv_output(
            target / "champion_fundamental_gaps.csv",
            [
                row
                for row in coverage
                if row["item"].endswith("_proxy")
                or row["item"] in {SHARES, "operating_cash_flow", "total_debt"}
            ],
        )
        csv_output(
            target / "acquisition_progress.csv",
            [{"measure": k, "amount": v} for k, v in sorted(stats.items())],
        )
        runbook = HERE / "docs/f01_task96_historical_acquisition_runbook.md"
        task9.bulk.write_new(target / "runbook.md", plan.inputs.read(runbook))
        for name, item in (("sec_pilot_preflight", sec_pilot), ("spy_pilot_preflight", spy_pilot)):
            if item:
                json_output(target, name + ".json", item)
                json_output(target, name + "-authorization-draft.json", item["authorization_draft"])
        manifest = {
            "contract": "F01_TASK96_LOCAL_ACQUISITION_EVIDENCE_V1",
            "plan_binding": plan.binding(),
            "code_fingerprint": current_code(),
            "input_bindings": plan.inputs.files,
            "outputs": {p.name: digest(p) for p in sorted(target.iterdir())},
        }
        json_output(target, "manifest.json", manifest)
        if OUTPUT.exists():
            task9.bulk.directory(OUTPUT)
            destination = OUTPUT / fingerprint(manifest)
            if destination.exists():
                _, previous = task9.checked(destination / "manifest.json")
                require(
                    previous == manifest
                    and all(
                        digest(destination / n) == sha for n, sha in manifest["outputs"].items()
                    ),
                    "EXISTING_REPORT_CHANGED",
                )
            else:
                target.rename(destination)
        else:
            destination = OUTPUT
            target.rename(destination)
    return {
        "status": "OFFLINE_REUSE_AND_PILOT_PREFLIGHT_COMPLETE",
        "path": str(destination),
        "manifest_sha256": digest(destination / "manifest.json"),
        "summary": stats,
        "pilots": {
            "sec": sec_pilot["block_id"] if sec_pilot else None,
            "spy": spy_pilot["block_id"] if spy_pilot else None,
        },
    }


def active_report():
    if not OUTPUT.is_dir():
        return None
    code = current_code()
    candidates = [
        OUTPUT / "manifest.json",
        *(p / "manifest.json" for p in OUTPUT.iterdir() if p.is_dir() and len(p.name) == 64),
    ]
    for path in sorted(
        (p for p in candidates if p.is_file()), key=lambda p: p.stat().st_mtime_ns, reverse=True
    ):
        _, record = task9.checked(path)
        if record.get("code_fingerprint") == code:
            require(
                all(digest(path.parent / n) == sha for n, sha in record["outputs"].items()),
                "ACTIVE_REPORT_OUTPUT_HASH_CHANGED",
            )
            return str(path.parent)
    return None


def local_processing_results(plan):
    if not OUTPUT.is_dir():
        return []
    results = []
    for path in sorted(OUTPUT.glob("processing-*.json")):
        record = plan.inputs.json(path)
        require(
            path.name == f"processing-{fingerprint(record)}.json"
            and record["contract"] == "F01_TASK96_OFFLINE_FILING_EVIDENCE_V1",
            "LOCAL_PROCESSING_REPORT_CHANGED",
        )
        results.append(record)
    return results


def validate_scope(plan, scope, block_id):
    provider, parent = plan.blocks.get(block_id, (None, None))
    factory = scope_for(plan, block_id, spy=set()) if provider == "SEC" else None
    require(
        provider in {"SEC", "SPY"}
        and scope["provider"] == provider
        and scope["block_id"] == block_id
        and scope["block"] == parent
        and scope["parent"] == plan.binding()
        and scope["code_fingerprint"] == current_code()
        and scope["authorization_draft"]["scope"]
        == fingerprint({k: v for k, v in scope.items() if k != "authorization_draft"}),
        "PREFLIGHT_IDENTITY_CHANGED",
    )
    count = len(
        scope["network_filing_scope"] if provider == "SEC" else scope["network_session_windows"]
    )
    require(
        scope["authorization_draft"]["limits"] == allocation(provider, count),
        "PREFLIGHT_LIMITS_CHANGED",
    )
    if provider == "SEC":
        expected = {key(r): r for r in factory["network_filing_scope"]}
        rows = scope["network_filing_scope"]
        require(
            rows
            and len(rows) == len(set(map(key, rows)))
            and all(expected.get(key(r)) == r for r in rows),
            "SEC_PREFLIGHT_SCOPE_WIDENED",
        )
    else:
        from f01_task96_network import trading_days

        available = set(trading_days(parent["start"], parent["end"]))
        windows = scope["network_session_windows"]
        days = [day for window in windows for day in window]
        require(
            parent["provider"] == "ALPACA_SIP"
            and parent["start"] >= "2016-01-01"
            and days
            and days == sorted(set(days))
            and set(days) <= available
            and all(window and window == trading_days(window[0], window[-1]) for window in windows),
            "SPY_PREFLIGHT_SCOPE_WIDENED",
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "status",
            "reuse",
            "fundamentals",
            "preflight",
            "process",
            "execute",
            "resume",
            "report",
        ),
    )
    parser.add_argument("--block-id")
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--preflight-sha256")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--authorization-sha256")
    parser.add_argument("--user-agent")
    args = parser.parse_args(argv)
    plan = Plan()
    operation = args.operation
    if operation == "status":
        result = {
            "plan_binding": plan.binding(),
            "completed_or_resumable": read_progress(plan),
            "active_report_directory": active_report(),
        }
    elif operation == "report":
        result = offline_report(plan)
    elif operation == "reuse":
        _, _, stats = inventory(plan, progress=progress)
        result = stats
    elif operation == "fundamentals":
        data = analyze(plan)
        result = {
            "contract": RULESET,
            "shares": len(data["shares"]),
            "share_conflicts": len(data["share_conflicts"]),
            "ocf_ttm": len(data["ocf_windows"]),
        }
    elif operation == "preflight":
        provider, _ = plan.blocks.get(args.block_id, (None, None))
        evidence = inventory(plan, progress=progress)[0] if provider == "SEC" else {}
        sessions = None
        if provider == "SPY":
            market = plan.blocks[args.block_id][1]
            sessions = (
                known_spy(plan)
                | current_spy(plan, market["start"], market["end"])
                | {day for r in read_progress(plan) for day in r["spy_sessions"]}
            )
        result = scope_for(plan, args.block_id, evidence, spy=sessions)
        path = cache_root() / f"preflight-{fingerprint(result)}.json"
        publish(path, result)
        result = {
            "path": str(path),
            "sha256": digest(path),
            "block_id": args.block_id,
            "status": result["status"],
        }
    elif operation == "process":
        candidates = {b["block_id"]: b for b in (*plan.groups[1], *plan.groups[5])}
        require(args.block_id in candidates, "SEC_OFFLINE_BLOCK_NOT_IN_PLAN")
        rows = candidates[args.block_id]["accessions"]
        resolved, _ = plan.metadata(rows)
        evidence = inventory(plan, progress=progress)[0]
        result = progress_for(
            args.block_id,
            [resolved[key(r)] for r in rows if key(r) in resolved],
            evidence,
            process=True,
        )
        path = cache_root() / f"processing-{fingerprint(result)}.json"
        publish(path, result)
        result = {"path": str(path), "counts": result["counts"]}
    else:
        require(
            args.preflight
            and args.preflight_sha256
            and args.authorization
            and args.authorization_sha256
            and args.block_id,
            "SHA_BOUND_BLOCK_INPUTS_REQUIRED",
        )
        _, scope = task9.checked(args.preflight, args.preflight_sha256)
        validate_scope(plan, scope, args.block_id)
        auth = authorization(scope, args.authorization, args.authorization_sha256)

        def fresh_spy():
            block = scope["block"]
            return (
                known_spy(plan)
                | current_spy(plan, block["start"], block["end"])
                | {day for r in read_progress(plan) for day in r["spy_sessions"]}
            )

        result = execute(
            scope,
            auth,
            args.authorization_sha256,
            user_agent=args.user_agent,
            resume=operation == "resume",
            spy_reuse=fresh_spy,
        )
    print(encoding(result).decode(), end="")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        sys.exit(3)
