"""Opt-in point-in-time accounting reads and an offline validation of the sealed E.11 pilot."""

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import f01_accounting_research_policy as policy
import f01_financing_positions as financing
import import_f01_sec_bulk_pilot as pilot
import prepare_f01_accounting_handoff as handoff
import preview_f01_accounting_rules as replay
import preview_f01_advanced_accounting as advanced
import replay_f01_accounting_fallback as fallback
import validate_f01_sec_bulk_pilot as qualification

from trading_system.data.canonical_paths import pinned_files
from trading_system.fundamentals.accounting_fallback import DEBT_BRIDGE, OriginalAmount
from trading_system.fundamentals.canonical_research import (
    CONTRACT,
    FLOW_RULES,
    RULES,
    CanonicalResearchReader,
    values,
)
from trading_system.fundamentals.semantic import Evidence
from trading_system.shadow.inputs import LocalInputs

bulk = pilot.bulk
require = bulk.require
RELEASE = "F01_CANONICAL_RESEARCH_FALLBACK_APPROVAL_V1"
REPLAY_SHA = "a75977fe70e4029db58e6f354f72e167a54efeae1c2ea9f045338255da5025c3"
E11_REVIEW = "f01-accounting-e11-reviewed-20261122-195121.json"
ARTIFACTS = pilot.RESEARCH_ROOT / "import-artifacts"


def review_template(intent, expected, code):
    return {
        "contract": RELEASE,
        "binding": {
            "reader": CONTRACT,
            "reader_candidate": code,
            "historical_import_candidate": intent["candidate"],
            "historical_fallback_candidate": expected["candidate"],
            "e11_import_sha256": fallback.IMPORT_SHA,
            "e11_intent_sha256": fallback.INTENT_SHA,
            "e11_review_sha256": intent["accounting_handoff"]["review_sha256"],
            "dataset_id": intent["dataset_id"],
            "dataset_version": 8,
            "schema_fingerprint": intent["schema_fingerprint"],
            "bootstrap_manifest_sha256": intent["bootstrap_manifest_sha256"],
            "fallback_replay_sha256": REPLAY_SHA,
            "fallback_policy": expected["contract"],
            "original_filing_manifest_sha256": handoff.FILING_SHA,
            "accounting_manifest_sha256": handoff.ACCOUNTING_SHA,
            "fallback_catalog_sha256": pilot.sha(bulk.json_bytes(expected["rules"])),
        },
        "rule_definitions": expected["rules"],
        "reviewer": "",
        "decisions": dict.fromkeys(sorted(RULES), "PENDING"),
    }


def reviewed_rules(template, reviewed):
    skip = {"reviewer", "decisions"}
    require(
        {k: v for k, v in template.items() if k not in skip}
        == {k: v for k, v in reviewed.items() if k not in skip},
        "FALLBACK_REVIEW_BINDING_DRIFT",
    )
    name = reviewed.get("reviewer")
    decisions = reviewed.get("decisions", {})
    require(
        isinstance(name, str)
        and name.strip()
        and set(decisions) == RULES
        and set(decisions.values()) <= {"APPROVE_RESEARCH", "DENY"},
        "INDEPENDENT_FALLBACK_REVIEW_REQUIRED",
    )
    return frozenset(key for key, value in decisions.items() if value == "APPROVE_RESEARCH")


def load_bindings(e11_review):
    intent = fallback.pilot_evidence()
    run = ARTIFACTS / f"f01-pilot-{fallback.DATASET}" / "result"
    report = handoff.read_bound(run / pilot.REPORT, fallback.IMPORT_SHA)
    old_review = handoff.read_bound(e11_review, intent["accounting_handoff"]["review_sha256"])
    expected = handoff.read_bound(
        pilot.REPO / "reports/f01_accounting_fallback_coverage.json", REPLAY_SHA
    )
    require(
        expected["review_required"] is True
        and expected["approved_rules"] == []
        and expected["bindings"]["e11_import_sha256"] == fallback.IMPORT_SHA
        and expected["bindings"]["e11_intent_sha256"] == fallback.INTENT_SHA
        and set(expected["rules"]) == RULES
        and old_review["contract"] == handoff.CONTRACT
        and old_review["binding"] == intent["accounting_handoff"]["binding"]
        and old_review["binding"]["candidate"] == intent["candidate"]
        and bool(old_review["reviewer"].strip())
        and old_review["decisions"]
        == dict.fromkeys(old_review["rule_definitions"], "APPROVE_RESEARCH")
        and sorted(old_review["decisions"])
        == intent["accounting_handoff"]["approved_research_rules"],
        "E11_OR_E12_APPROVAL_BINDING_MISMATCH",
    )
    return intent, report, expected


def canonical_facts(intent, report):
    target = Path(intent["target"])
    layout = bulk.Layout.load(ARTIFACTS / "f01-sec-bulk-layout-v1.json")
    external, manifest = pilot.bootstrap_target(layout, target)
    admit_path = ARTIFACTS / "f01-pilot-admission-reviewed-v2.json"
    admissions = pilot.load_admission(
        admit_path, intent["admission_sha256"], pilot.pilot_spec(), layout
    )
    admission = handoff.read_bound(admit_path, intent["admission_sha256"])
    identity = {
        cik: handoff.read_bound(
            Path(record["identity_evidence"]["path"]), record["identity_evidence"]["sha256"]
        )
        for cik, record in admission["issuers"].items()
    }
    facts, errors = {}, []
    counts = {key: report["totals"][key] for key in qualification.COUNTS}
    qualification.no_sidecars(target)
    with pinned_files({"research": target}) as pins:
        pin = pins["research"]
        stat = os.fstat(pin.fileno())
        qualification.sqlite_header(pin)
        require(
            os.path.samestat(stat, bulk.plain(target))
            and [stat.st_dev, stat.st_ino] == intent["target_file_identity"]
            and stat.st_size == report["database_bytes_after"],
            "E11_READONLY_FILE_BINDING_MISMATCH",
        )
        pin.seek(0)
        before = hashlib.file_digest(pin, "sha256").hexdigest()
        with (
            LocalInputs(target, capture_provenance=False).snapshot() as reader,
            reader.connect() as db,
        ):
            for issuer, envelope, _, _ in qualification.read_snapshot(
                db,
                report,
                identity,
                admissions,
                external,
                manifest,
                target=target,
                dataset_id=fallback.DATASET,
                counts=counts,
            ):
                facts[issuer["symbol"]] = qualification.check_facts(
                    reader,
                    db,
                    issuer,
                    envelope,
                    errors,
                    target=target,
                )
        pin.seek(0)
        require(
            hashlib.file_digest(pin, "sha256").hexdigest() == before and not errors,
            f"CANONICAL_READONLY_PROJECTION_DRIFT: {errors}",
        )
        qualification.no_sidecars(target)
    return facts, before


class FilingEvidence:
    def __init__(self, symbols, rules):
        self.rules = rules
        self.flows = defaultdict(list)
        self.debts, self.proofs = {}, {}
        self.notes = []
        self.positions = {}
        self.amendments = defaultdict(list)
        a, f = replay.audit, replay.f
        source = f.ROOT / f"f01-extended-filing-evidence-{a.RUN}" / "manifest.json"
        derived = (
            f.qualification.ARTIFACTS / f"f01-extended-accounting-evidence-{a.RUN}/manifest.json"
        )
        _, self.originals, accepted, _, _, _ = replay.lookup(
            source,
            derived,
            (),
            on_filing=self.on_filing,
            symbols=symbols,
        )
        e6 = handoff.read_bound(
            pilot.REPO / "reports/f01_advanced_accounting_preview.json", replay.PREVIEW_SHA
        )
        trial = advanced.trial_evidence(e6["filings"])
        trial["debt_coverage"] += tuple(coverage for coverage, _, _ in self.notes)
        for _, contexts, _ in self.notes:
            trial["contexts"].update(contexts)
        self.semantic = Evidence(acceptance=accepted, **trial)

    def on_filing(self, entry, row, facts, originals):
        symbol, accession = row["symbol"], row["accession"]
        accepted = datetime.fromisoformat(row["acceptance"])
        if row["form"].endswith("/A"):
            for item in row["observations"]:
                context = item.get("context") or {}
                concept = fallback.tag(item["concept"])
                if context.get("dimensions") != []:
                    continue
                if context.get("instant") and ("Debt" in concept or "CommercialPaper" in concept):
                    self.amendments[symbol, "total_debt"].append(
                        (date.fromisoformat(context["instant"]), accepted)
                    )
                if context.get("endDate"):
                    metric = (
                        "capital_expenditures"
                        if concept in fallback.PPE_TAGS
                        else "operating_income"
                        if concept in {fallback.PRETAX, "OperatingIncomeLoss"}
                        else None
                    )
                    if metric:
                        self.amendments[symbol, metric].append(
                            (date.fromisoformat(context["endDate"]), accepted)
                        )
            return
        if row["form"] not in {"10-K", "10-Q"}:
            return
        assets = [
            item for item in facts if item.metric == "total_assets" and item.period_start is None
        ]
        periods = {item.period_end.isoformat() for item in assets}
        index = financing.index_filing(row, entry["sha256"], periods, row["observations"])
        index = {(accession, period): records for period, records in index.items()}
        for anchor in assets:
            period = anchor.period_end.isoformat()
            sources = originals[anchor]["matches"]
            positions = financing.reported_positions(index, sources, accepted)
            self.positions[anchor] = positions
            if DEBT_BRIDGE in self.rules:
                proof = fallback.debt_proof(entry, row, period, positions, sources)
                if proof:
                    ref = pilot.sha(bulk.json_bytes(proof))
                    self.proofs[ref] = proof
                    self.debts[anchor] = OriginalAmount(
                        anchor, Decimal(proof["value"]), accepted, ref
                    )
        for period in sorted(periods):
            if note := policy.qualify_note(row, facts, period):
                self.notes.append(note)
        wanted = {metric for metric, rule in FLOW_RULES.items() if rule in self.rules}
        if wanted:
            for metric, amount in fallback.filing_flows(entry, row, wanted, self.proofs):
                self.flows[symbol, metric].append(amount)

    def reader(self, symbol, cik, facts):
        return CanonicalResearchReader(
            facts,
            cik=cik,
            evidence=self.semantic,
            originals=self.originals,
            debts=self.debts,
            flows={m: self.flows[symbol, m] for m in FLOW_RULES},
            amendments={m: tuple(self.amendments[symbol, m]) for m in (*FLOW_RULES, "total_debt")},
            reviewed_rules=self.rules,
        )


def proof_refs(read):
    def debt(scope):
        selected = scope.get("total_debt")
        if selected and selected.reason == "ORIGINAL_FALLBACK":
            return selected.references[0]
        return None

    return {
        "current_debt_proof": debt(read.current_balance),
        "prior_debt_proof": debt(read.prior_balance),
        "fallback_windows": {
            metric: {
                "ttm_period_end": str(window.period_end),
                "quarters": [
                    {
                        "period_end": str(end),
                        "value": str(chosen.value),
                        "original_proofs": list(chosen.references),
                        "selection": chosen.reason,
                    }
                    for end, chosen in window.quarters
                ],
            }
            for metric in FLOW_RULES
            for window in (read.current_flows[metric],)
            if any(item.references for _, item in window.quarters)
            and all(
                item.reason in {"ORIGINAL_FALLBACK", "CUMULATIVE_DIFFERENCE"}
                for _, item in window.quarters
            )
        },
    }


def proof_keys(refs):
    return {
        ref
        for ref in (
            refs["current_debt_proof"],
            refs["prior_debt_proof"],
            *(
                ref
                for window in refs["fallback_windows"].values()
                for quarter in window["quarters"]
                for ref in quarter["original_proofs"]
            ),
        )
        if ref
    }


def verify_proofs(proofs):
    locations = {
        (proof["cik"], proof["accession"], proof["evidence"]["role"][1])
        for proof in proofs.values()
    }
    paths = []
    for cik, accession, digest in sorted(locations):
        path = bulk.directory(replay.f.ROOT / cik / accession)
        matches = []
        for cached in sorted(path.iterdir()):
            if cached.suffix.lower() in {".xml", ".xsd", ".htm", ".html"}:
                _, meta = bulk.read_metadata(cached / "source.json")
                if meta["sha256"] == digest:
                    require(
                        (meta["cik"], meta["accession"], meta["filename"])
                        == (cik, accession, cached.name),
                        "FALLBACK_LINKBASE_SOURCE_DRIFT",
                    )
                    matches.append((cached / "content", meta))
        require(matches, "FALLBACK_LINKBASE_MISSING")
        paths.extend(matches)
    handoff.verify_files(paths)


def output_row(result, evidence, cutoff):
    chosen = result.accounting
    trace = replay.Trace(evidence.originals)
    details = trace.grid_row(chosen)
    proven = proof_refs(chosen)
    asset = chosen.current_balance["total_assets"].sources
    positions = evidence.positions.get(asset[0], {}) if len(asset) == 1 else {}
    periods = chosen.state.current.metric_period_ends
    latest = asset[0].period_end if len(asset) == 1 else None
    metrics = values(chosen, require_aligned_roic=True)
    metrics.update(
        {metric: value for metric, value in values(result.base).items() if value is not None}
    )
    status = {}
    for metric, value in metrics.items():
        period = periods.get("free_cash_flow" if metric == "fcf" else metric)
        if metric == "total_debt":
            debt_sources = chosen.current_balance["total_debt"].sources
            period = debt_sources[0].period_end if debt_sources else None
        if metric in {"debt_to_ebitda", "roic"} and value is not None:
            period = periods.get("ebitda" if metric == "debt_to_ebitda" else "operating_income")
        reason = details.get(metric, {}).get("reason") or "MISSING_QUALIFIED_ALIGNED_INPUTS"
        status[metric] = {
            "status": (
                "MISSING"
                if value is None
                else "LAGS_LATEST_BALANCE_PERIOD"
                if period and latest and period < latest
                else "AVAILABLE"
            ),
            "period_end": period,
            "age_days_at_decision": (chosen.as_of - period).days if period else None,
            "reason": reason if value is None else None,
        }
    return {
        "at": cutoff.isoformat(),
        "cik": chosen.cik,
        "latest_consolidated_balance_period": latest,
        "values": metrics,
        "status": status,
        "financing_positions": {key: row for key, row in positions.items() if key != "Assets"},
        financing.TERM_SUBTOTAL: financing.term_subtotal(positions),
        "selections": details,
        "canonical_sources": {
            key: row for key, row in trace.registry.items() if row["original"] is not None
        },
        "fallback": proven,
        "fallback_proofs": {ref: evidence.proofs[ref] for ref in sorted(proof_keys(proven))},
    }


def decimal_equal(left, right):
    return (
        (left is right)
        if left is None or right is None
        else Decimal(str(left)) == Decimal(str(right))
    )


def boundary_results(readers, evidence, expected):
    checks = []
    latest = max(row["session"] for row in expected["grid"])
    for row in expected["grid"]:
        if row["session"] != latest:
            continue
        use = proof_keys(row)
        if not use:
            continue
        symbol = row["symbol"]
        timely = sorted(use, key=lambda ref: (expected["proofs"][ref]["acceptance"], ref))[-1]
        at = datetime.fromisoformat(expected["proofs"][timely]["acceptance"])
        accession = expected["proofs"][timely]["accession"]
        filed = date.fromisoformat(
            expected["proofs"][timely]["original_facts"][0]["source"]["filed"]
        )
        earliest = max(at, datetime.combine(filed, time.min, replay.audit.NY))
        instants = [at - timedelta(microseconds=1), at + timedelta(microseconds=1)]
        if earliest > instants[1]:
            instants.append(earliest)
        for instant in instants:
            read = readers[symbol].read(instant).accounting
            selected = proof_keys(proof_refs(read))
            require(
                all(
                    datetime.fromisoformat(evidence.proofs[ref]["acceptance"]) <= instant
                    for ref in selected
                ),
                "READER_SELECTED_FUTURE_FILING",
            )
            from_accession = sorted(
                ref for ref in selected if evidence.proofs[ref]["accession"] == accession
            )
            eligible = any(fact.accession_number == accession for fact in read.eligible_facts)
            require(eligible == (instant >= earliest), "EDGAR_ACCEPTANCE_OR_FILED_BOUNDARY_DRIFT")
            require(not from_accession or instant >= earliest, "FALLBACK_BEFORE_FILED_BOUNDARY")
            checks.append(
                {
                    "symbol": symbol,
                    "tested_accession": accession,
                    "acceptance": at.isoformat(),
                    "decision_at": instant.isoformat(),
                    "offset_microseconds": (instant - at) // timedelta(microseconds=1),
                    "filed": filed,
                    "canonical_accession_eligible": eligible,
                    "selected_from_accession": from_accession,
                    "values": values(read),
                }
            )
    return checks


def validate(readers, evidence, expected, e10):
    findings, grid, used = [], [], {}
    canonical_sources = {}
    count = dict.fromkeys(expected["coverage"], 0)
    for row, e10_row in zip(expected["grid"], e10["probes"], strict=True):
        symbol, session = row["symbol"], row["session"]
        require((symbol, session) == (e10_row["symbol"], e10_row["session"]), "READER_GRID_DRIFT")
        cutoff = replay.close(date.fromisoformat(session))
        result = readers[symbol].read(cutoff)
        selected = output_row(result, evidence, cutoff)
        used.update(selected["fallback_proofs"])
        for stage, actual in (("before", values(result.base)), ("after", selected["values"])):
            for metric, actual_value in actual.items():
                expected_value = row[stage][metric]
                if not decimal_equal(actual_value, expected_value):
                    findings.append(
                        {
                            "symbol": symbol,
                            "session": session,
                            "stage": stage,
                            "metric": metric,
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )
                if stage == "after":
                    count[metric] += int(actual_value is not None)
        trace = replay.Trace(evidence.originals)
        baseline = trace.grid_row(result.base)
        canonical_sources.update(trace.registry)
        for metric in (
            "total_debt",
            "operating_income",
            "operating_cash_flow",
            "capital_expenditures",
            "ebitda",
            "roic",
        ):
            if bulk.json_bytes(baseline[metric]) != bulk.json_bytes(e10_row["preview"][metric]):
                findings.append(
                    {
                        "symbol": symbol,
                        "session": session,
                        "stage": "canonical_source",
                        "metric": metric,
                        "reason": "E10_TRACE_DIFFERS",
                    }
                )
        archived = {
            key: row[key] for key in ("current_debt_proof", "prior_debt_proof", "fallback_windows")
        }
        if archived != selected["fallback"]:
            findings.append(
                {
                    "symbol": symbol,
                    "session": session,
                    "stage": "fallback_source",
                    "expected": archived,
                    "actual": selected["fallback"],
                }
            )
        grid.append(
            {
                "symbol": symbol,
                "session": session,
                "values": selected["values"],
                "status": selected["status"],
                "fallback": selected["fallback"],
            }
        )
    for ref, proof in used.items():
        if bulk.json_bytes(expected["proofs"].get(ref)) != bulk.json_bytes(proof):
            findings.append({"stage": "original_proof", "reference": ref})
    for ref, source in canonical_sources.items():
        if bulk.json_bytes(e10["source_facts"].get(ref)) != bulk.json_bytes(source):
            findings.append({"stage": "canonical_original_provenance", "reference": ref})
    verify_proofs(used)
    return {
        "coverage": count,
        "expected_coverage": {
            metric: item["after"] for metric, item in expected["coverage"].items()
        },
        "probe_count": len(grid),
        "findings": findings,
        "probes": grid,
        "verified_original_proofs": len(used),
        "canonical_source_facts_compared": len(canonical_sources),
        "acceptance_boundary_checks": boundary_results(readers, evidence, expected),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--review-template", action="store_true")
    modes.add_argument("--validate", action="store_true")
    modes.add_argument("--preview-validation", action="store_true")
    modes.add_argument("--at", type=datetime.fromisoformat)
    parser.add_argument("--symbol")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-sha256")
    parser.add_argument("--e11-review", type=Path, default=ARTIFACTS / E11_REVIEW)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.at is not None) != bool(args.symbol):
        parser.error("--at and --symbol are required together")
    if args.at is not None and args.at.utcoffset() is None:
        parser.error("--at requires an offset")
    if (args.validate or args.at is not None) != bool(
        args.approval and bulk.SHA256.fullmatch(args.approval_sha256 or "")
    ):
        parser.error("Approved reads and validation require --approval and --approval-sha256")
    if not (args.validate or args.at is not None) and (args.approval or args.approval_sha256):
        parser.error("Preview and template cannot take an approval")
    if (args.validate or args.preview_validation) and not args.output:
        parser.error("Validation requires --output")
    if args.output:
        bulk.directory(args.output.absolute().parent)
        require(not bulk.exists(args.output), "NEW_READER_OUTPUT_REQUIRED")
    code = pilot.code_binding()
    intent, report, expected = load_bindings(args.e11_review)
    template = review_template(intent, expected, code)
    if args.review_template:
        print(bulk.json_bytes(template).decode(), end="")
        return 0
    reviewed = (
        reviewed_rules(template, handoff.read_bound(args.approval, args.approval_sha256))
        if args.approval
        else frozenset(RULES)
    )
    ciks = {x["symbol"]: x["cik"] for x in intent["active_issuers"]}
    if args.symbol:
        require(args.symbol in ciks, "SYMBOL_OUTSIDE_E11_RESEARCH_SCOPE")
        ciks = {args.symbol: ciks[args.symbol]}
    facts, database_sha = canonical_facts(intent, report)
    evidence = FilingEvidence(ciks.keys(), reviewed)
    readers = {symbol: evidence.reader(symbol, cik, facts[symbol]) for symbol, cik in ciks.items()}
    if args.at is not None:
        data = output_row(readers[args.symbol].read(args.at), evidence, args.at)
        verify_proofs(data["fallback_proofs"])
        status = "RESEARCH_READ"
    else:
        e10 = handoff.read_bound(
            pilot.REPO / "reports/f01_accounting_policy_consolidation.json", handoff.E10_SHA
        )
        data = validate(readers, evidence, expected, e10)
        status = "PARITY_DIFFERS" if data["findings"] else "PARITY_VERIFIED"
        if reviewed != RULES:
            status = "PARTIAL_APPROVAL_NOT_COMPARABLE_TO_FULL_E12"
    output = {
        "contract": CONTRACT,
        "status": status,
        "candidate_preview_only": args.preview_validation,
        "approved_rules": sorted(reviewed) if args.approval else [],
        "approval_sha256": args.approval_sha256,
        "bindings": template["binding"],
        "database_sha256": database_sha,
        **data,
    }
    require(pilot.code_binding() == code, "RESEARCH_READER_CANDIDATE_CHANGED")
    content = (json.dumps(output, default=str, ensure_ascii=True, indent=2) + "\n").encode()
    if args.output:
        bulk.write_new(args.output, content)
        print(status)
    else:
        print(content.decode(), end="")
    return 4 if status == "PARITY_DIFFERS" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as error:
        print(f"CANONICAL_RESEARCH_READER_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(3) from None
