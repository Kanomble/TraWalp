"""Reconcile a bound original-filing draft offline without granting semantic approval."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import prepare_f01_filing_evidence as evidence

BASELINE = "1a8c5162efc7ff9364a441e37b32550cb5dacfdbf15e269568c22da1f91aeddf"


def read_bound(path, expected):
    raw = path.read_bytes()
    evidence.require(hashlib.sha256(raw).hexdigest() == expected, "INPUT_HASH_MISMATCH")
    return json.loads(raw)


def reconcile(draft, taxonomy):
    evidence.require(
        draft["candidate_code"] == taxonomy["candidate"]
        and draft["candidate_code"]["fingerprint"] == BASELINE
        and draft["contract"] == "F01_ORIGINAL_FILING_EVIDENCE_DRAFT_V1",
        "BASELINE_BINDING_MISMATCH",
    )
    evidence.require(
        taxonomy["sec_plugin_verified"] is True
        and bool(taxonomy["urls"])
        and all(row["status"] == "BOUND" for row in taxonomy["urls"].values())
        and draft["xbrl_processor"]["sec_transforms"]["commit"] == taxonomy["sec_plugin_commit"],
        "TAXONOMY_COVERAGE_NOT_BOUND",
    )
    filings = draft["filings"]
    evidence.require(
        {(f["symbol"], f["accession"]) for f in filings}
        == {(symbol, acc) for symbol, accs in evidence.INITIAL.items() for acc in accs}
        and len(filings) == 18,
        "INITIAL_FILING_SCOPE_MISMATCH",
    )
    results = []
    transitions = Counter()
    for filing in filings:
        rows = filing["observations"]
        by_ordinal = {row["document_ordinal"]: row for row in rows}
        evidence.require(len(by_ordinal) == len(rows), "DUPLICATE_SOURCE_ORDINAL")
        bound = {(item["url"], item["sha256"]) for item in filing["cached_sources"]}
        for row in rows:
            source = row["source"]
            evidence.require(
                (source["url"], source["sha256"]) in bound
                and source["accession"] == filing["accession"]
                and source["cik"] == filing["cik"],
                "OBSERVATION_SOURCE_BINDING_MISMATCH",
            )
        for previous in filing["companyfacts_joins"]:
            fact = previous["fact"]
            evidence.require(
                fact["accession_number"] == filing["accession"],
                "COMPANYFACTS_ACCESSION_MISMATCH",
            )
            item = evidence.join(fact, rows, filing["cik"])
            if not filing["filed"] or filing["filed"] != fact.get("filed"):
                item.update(
                    status="UNRESOLVED_CONTEXT",
                    reason="FILING_DATE_BINDING_UNAVAILABLE_OR_INCONSISTENT",
                )
            label = previous["status"] + " -> " + item["status"]
            transitions[label] += 1
            results.append(
                item
                | {
                    "previous_status": previous["status"],
                    "companyfacts_source": previous["companyfacts_source"],
                    "reported_acceptance": filing.get("acceptance"),
                    "availability_source": filing.get("availability_source"),
                    "sources": [
                        {
                            "ordinal": ordinal,
                            "source_sha256": by_ordinal[ordinal]["source"]["sha256"],
                            "url": by_ordinal[ordinal]["source"]["url"],
                        }
                        for ordinal in item["source_candidates"]
                    ],
                }
            )
    return {
        "contract": "F01_ACCOUNTING_SEMANTIC_EVIDENCE_DRAFT_V1",
        "review_required": True,
        "semantic_evidence": {
            "contexts": [],
            "acceptance": [],
            "amendments": [],
            "debt_coverage": [],
        },
        "status_transitions": dict(sorted(transitions.items())),
        "previously_unresolved_by_reason": dict(
            sorted(
                Counter(
                    item["reason"]
                    for item in results
                    if item["previous_status"] == "UNRESOLVED_CONTEXT"
                ).items()
            )
        ),
        "review_candidates": results,
        "lly_rounding": evidence.lly_rounding(
            {f["accession"]: f for f in filings if f["symbol"] == "LLY"}
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--draft-sha256", required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--taxonomy-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = evidence.pilot.code_binding()
    result = reconcile(
        read_bound(args.draft, args.draft_sha256),
        read_bound(args.taxonomy, args.taxonomy_sha256),
    )
    result.update(
        candidate_code=candidate,
        inputs={
            "filing_draft_sha256": args.draft_sha256,
            "taxonomy_coverage_sha256": args.taxonomy_sha256,
        },
    )
    evidence.bulk.write_new(args.output, evidence.bulk.json_bytes(result))
    print(json.dumps(result["status_transitions"], sort_keys=True))
    print(json.dumps(result["lly_rounding"], sort_keys=True))


if __name__ == "__main__":
    main()
