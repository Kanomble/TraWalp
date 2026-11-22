"""Run focused, offline Arelle diagnostics and an unapproved Research coverage preview."""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import audit_f01_accounting_semantic_qualification as audit

from trading_system.fundamentals.semantic import TOTAL_DEBT, DebtCoverage, ReportingContext
from trading_system.models.fundamentals import FundamentalFact

filings = audit.filings
require = filings.require
DRAFT_SHA = "0048d46b2ca04f28bc5588293a388527693372cd34438234ad4fbae1f65d1780"


def selected_models(manifest_path, manifest, candidates, processor):
    by_accession = {item["accession"]: item for item in candidates}
    selected = manifest | {
        "filings": [item for item in manifest["filings"] if item["accession"] in by_accession],
    }
    require(len(selected["filings"]) == len(candidates), "UNBOUND_VALIDATION_CASE")
    results = []
    for entry, row in filings.extended_records(manifest_path, selected):
        print(f"Validate {row['symbol']} {row['accession']}", file=sys.stderr)
        topic = by_accession[row["accession"]]
        concepts = {item["concept"].rsplit("}", 1)[-1] for item in topic["original_observations"]}
        concepts.update(
            (audit.SHARE_TAG, "TradingSymbol", "Security12bTitle", "SecurityExchangeName")
        )
        folder = filings.ROOT / row["cik"] / row["accession"]
        data, index = filings.cache_read(folder / "index.json", row, "index.json")
        require(index == row["index_source"], "ADVANCED_INDEX_BINDING_MISMATCH")
        started = perf_counter()
        observations, docs, issues, sources = filings.extract_cached(
            row,
            folder,
            json.loads(data),
            processor,
            extended=True,
            advanced_concepts=sorted(concepts),
        )
        require(sources == row["cached_sources"], "ADVANCED_SOURCE_BINDING_MISMATCH")
        results.append(
            {
                "case": topic,
                "filing_sha256": entry["sha256"],
                "index_source": index,
                "documents": docs,
                "observations": observations,
                "issues": issues,
                "wall_seconds": round(perf_counter() - started, 3),
                "cache_payload_bytes": sum(source["bytes"] for source in sources),
            }
        )
    return results


def trial_evidence(results):
    """Use only reported-total-reconciled pairs from equal Arelle aspect groups."""
    contexts, debt = {}, []
    for item in results:
        topic = item["case"]
        by_ref = {
            f"sha256:{o['source']['sha256']}#fact={o['document_ordinal']}": o
            for o in item["observations"]
            if o.get("source") and o.get("context_unit_group")
        }
        by_fact = {
            FundamentalFact.model_validate(row["fact"]): row["context"]
            for row in topic["proposed_api_contexts"]
            if row["context"]["reference"] in by_ref
        }
        for proposal in topic["debt_coverage_review_candidates"]:
            left, right = (
                FundamentalFact.model_validate(proposal[key]) for key in ("current", "noncurrent")
            )
            if left not in by_fact or right not in by_fact or proposal["reference"] not in by_ref:
                continue
            refs = [by_fact[left]["reference"], by_fact[right]["reference"], proposal["reference"]]
            rows = [by_ref[ref] for ref in refs]
            if len({(row["source"]["sha256"], row["context_unit_group"]) for row in rows}) != 1:
                continue
            if not any(
                fact.tag == TOTAL_DEBT and context["reference"] == proposal["reference"]
                for fact, context in by_fact.items()
            ):
                continue
            for fact in (left, right):
                context = by_fact[fact]
                increment = context["rounding_increment"]
                contexts[fact] = ReportingContext(
                    context["cik"],
                    context["scope"],
                    context["reference"],
                    Decimal(increment) if increment is not None else None,
                )
            debt.append(DebtCoverage(left, right, proposal["reference"]))
    return {"contexts": contexts, "debt_coverage": tuple(debt)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--accession", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = filings.pilot.code_binding()
    started = perf_counter()
    require(
        1 <= len(args.accession) <= 8 and len(set(args.accession)) == len(args.accession),
        "REQUIRE_ONE_TO_EIGHT_UNIQUE_ACCESSIONS",
    )
    output = args.output.absolute()
    filings.bulk.directory(output.parent)
    require(
        output.drive.upper() == "G:" and not filings.bulk.exists(output),
        "NEW_G_DRIVE_OUTPUT_REQUIRED",
    )
    require(args.draft.absolute().drive.upper() == "G:", "EVIDENCE_REQUIRES_G_DRIVE")
    prior = audit.read_bound(args.draft, DRAFT_SHA, max_bytes=filings.LIMIT)
    candidates = [
        item for item in prior["domain_review_candidates"] if item["accession"] in args.accession
    ]
    require(len(candidates) == len(args.accession), "UNKNOWN_QUALIFICATION_ACCESSION")
    source = filings.ROOT / f"f01-extended-filing-evidence-{audit.RUN}" / "manifest.json"
    derived = (
        filings.qualification.ARTIFACTS
        / f"f01-extended-accounting-evidence-{audit.RUN}"
        / "manifest.json"
    )
    manifest = audit.read_bound(source, audit.FILING_SHA)
    processor = filings.ArelleProcessor(filings.ARELLE_ROOT)
    require(processor.sec_plugin, "ADVANCED_PREVIEW_REQUIRES_SEC_TRANSFORMS")
    models = selected_models(source, manifest, candidates, processor)
    preview = trial_evidence(models)
    replay = audit.run(source, derived, current, preview=preview)
    base = prior["coverage_after_dei"]
    require(base == replay["coverage_after_dei"], "QUALIFICATION_BASELINE_DRIFT")
    result = {
        "contract": "F01_ADVANCED_ACCOUNTING_PREVIEW_V1",
        "review_required": True,
        "candidate_code": current,
        "input_draft_sha256": DRAFT_SHA,
        "inputs": replay["inputs"],
        "filings": models,
        "trial_debt_references": [item.reference for item in preview["debt_coverage"]],
        "coverage_before": base,
        "coverage_preview": replay["coverage_preview"],
        "class_a_preview": prior["class_a_preview"],
        "semantic_evidence": replay["semantic_evidence"],
        "wall_seconds": round(perf_counter() - started, 3),
    }
    filings.bulk.write_new(output, filings.bulk.json_bytes(result))
    print(output)


if __name__ == "__main__":
    try:
        main()
    except (filings.bulk.Blocked, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ADVANCED_ACCOUNTING_PREVIEW_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from None
