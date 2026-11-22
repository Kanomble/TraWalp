"""Reconcile a bound original-filing draft offline without granting semantic approval."""

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import prepare_f01_filing_evidence as evidence

BASELINE = "1a8c5162efc7ff9364a441e37b32550cb5dacfdbf15e269568c22da1f91aeddf"
REVIEW_DOMAINS = {
    "shares_and_classes": r"sharesoutstanding|classofstock|tradingsymbol|security12btitle",
    "debt_and_leases": r"debt|borrow|lease|commercialpaper",
    "income_and_equity": r"netincome|profitloss|noncontrollinginterest|stockholdersequity",
    "operating_cash_flow": r"operatingactivities|discontinuedoperations",
    "capital_expenditure": r"paymentstoacquire.*(?:propertyplant|equipment)|capitalexpenditur",
    "interest_and_operating_income": r"interestexpense|operatingincome|operatingprofit",
    "ebitda_and_roic_inputs": r"ebitda|depreciation|amortization|incometax|capital|profitloss|"
    r"operatingincome|interestexpense",
}


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
    return review_filings(filings)


def review_filings(filings):
    results = []
    transitions = Counter()
    for filing in filings:
        rows = filing["observations"]
        by_ordinal = {row["document_ordinal"]: row for row in rows}
        by_concept = defaultdict(list)
        evidence.require(len(by_ordinal) == len(rows), "DUPLICATE_SOURCE_ORDINAL")
        bound = {(item["url"], item["sha256"]) for item in filing["cached_sources"]}
        for row in rows:
            by_concept[evidence.taxonomy(row.get("concept"))].append(row)
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
            item = evidence.join(fact, by_concept[fact["taxonomy"], fact["tag"]], filing["cik"])
            if (
                not filing["filed"]
                or filing.get("availability_unverified")
                or filing["filed"] != fact.get("filed")
            ):
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


def review_domains(observations):
    return {
        domain: [
            row["document_ordinal"]
            for row in observations
            if re.search(pattern, (row.get("concept") or "").rsplit("}", 1)[-1], re.I)
        ]
        for domain, pattern in REVIEW_DOMAINS.items()
    }


def reconcile_extended(path, manifest, cache, coverage, hashes, candidate, target):
    require = evidence.require
    evidence.verify_extended_inputs(cache, coverage, hashes["cache_sha256"], candidate)
    require(
        manifest["candidate_code"] == candidate
        and manifest["inputs"] == hashes
        and manifest["pilot_diagnosis_sha256"]
        == cache["pilot_diagnosis_sha256"]
        == evidence.DIAG_SHA
        and manifest["xbrl_processor"]["sec_transforms"]["commit"] == evidence.EDGAR_COMMIT,
        "EXTENDED_DRAFT_BINDING_MISMATCH",
    )
    active = {(item["symbol"], item["cik"]) for item in evidence.pilot.pilot_spec().active}
    require(
        {(item["symbol"], item["cik"]) for item in manifest["filings"]} == active,
        "EXTENDED_PILOT_SCOPE_MISMATCH",
    )
    require(not evidence.bulk.exists(target), "EVIDENCE_OUTPUT_EXISTS")
    stage = Path(tempfile.mkdtemp(prefix=".accounting-evidence-incomplete-", dir=target.parent))
    results, transitions, lly = [], Counter(), {}
    for (entry, row), expected in zip(
        evidence.extended_records(path, manifest), cache["filings"], strict=True
    ):
        require(
            all(row.get(key) == value for key, value in expected.items() if key != "issues"),
            "FILING_CACHE_PROVENANCE_MISMATCH",
        )
        require(
            shutil.disk_usage(target.parent).free >= evidence.LIMIT + evidence.RESERVE,
            "FILING_OUTPUT_SPACE_REQUIRED",
        )
        if row["symbol"] == "LLY" and row["accession"] in (
            "0000059478-25-000254",
            "0000059478-25-000204",
        ):
            lly[row["accession"]] = row
        result = review_filings([row])
        result.pop("lly_rounding")  # Evaluated once, across both bound filings.
        result.update(
            filing=entry,
            reported_acceptance=row.get("acceptance"),
            availability_source=row.get("availability_source"),
            availability_unverified=bool(row.get("availability_unverified")),
            issues=row["issues"],
            review_domains=review_domains(row["observations"]),
            probe_references=row["probes"],
        )
        transitions.update(result["status_transitions"])
        raw = (json.dumps(result, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        evidence.bulk.write_new(stage / entry["file"], raw)
        results.append(
            entry
            | {
                "source_sha256": entry["sha256"],
                "sha256": evidence.sha(raw),
                "bytes": len(raw),
                "observations": len(row["observations"]),
                "issues": len(row["issues"]),
                "joins": len(row["companyfacts_joins"]),
                "acceptance_available": bool(row.get("acceptance")),
            }
        )
    summary = {
        "contract": "F01_EXTENDED_ACCOUNTING_EVIDENCE_DRAFT_V1",
        "review_required": True,
        "candidate_code": candidate,
        "inputs": hashes | {"filing_manifest_sha256": evidence.pilot.file_sha(path)},
        "filings": results,
        "lly_rounding": evidence.lly_rounding(lly),
        "status_transitions": dict(sorted(transitions.items())),
    }
    evidence.bulk.write_new(stage / "manifest.json", evidence.bulk.json_bytes(summary))
    stage.rename(target)
    print(str(target / "manifest.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--draft-sha256", required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--taxonomy-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache-manifest", type=Path, help="Required for extended filing manifests"
    )
    args = parser.parse_args()
    candidate = evidence.pilot.code_binding()
    if args.cache_manifest:
        evidence.require(
            all(
                path.is_absolute() and path.is_relative_to(evidence.pilot.RESEARCH_ROOT)
                for path in (args.draft, args.taxonomy, args.cache_manifest, args.output)
            ),
            "EXTENDED_RESEARCH_PATH_REQUIRED",
        )
        evidence.bulk.directory(args.output.parent)
        evidence.bulk.directory(args.draft.parent)
        evidence.bulk.directory(args.taxonomy.parent)
        evidence.bulk.plain(args.draft)
        evidence.bulk.plain(args.taxonomy)
        draft = read_bound(args.draft, args.draft_sha256)
        taxonomy = read_bound(args.taxonomy, args.taxonomy_sha256)
        cache_raw, cache = evidence.bulk.read_metadata(
            args.cache_manifest, max_bytes=evidence.LIMIT
        )
        reconcile_extended(
            args.draft,
            draft,
            cache,
            taxonomy,
            {
                "cache_sha256": evidence.sha(cache_raw),
                "taxonomy_coverage_sha256": args.taxonomy_sha256,
            },
            candidate,
            args.output,
        )
        return
    result = reconcile(
        read_bound(args.draft, args.draft_sha256), read_bound(args.taxonomy, args.taxonomy_sha256)
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
    try:
        main()
    except (evidence.bulk.Blocked, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"ACCOUNTING_EVIDENCE_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from None
