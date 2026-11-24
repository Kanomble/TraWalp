#!/usr/bin/env python3
"""Plan a D.6 historical SEC batch using local inventories and file metadata."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import stat
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.dont_write_bytecode = True

from f01_research_evidence import REPO, repository_evidence  # noqa: E402

CONTRACT = "F01_HISTORICAL_50_RESEARCH_BATCH_V1"
RESEARCH = Path(r"G:\TraWalpResearch")
CACHE = RESEARCH / "sec-originals" / "filing-evidence"
ARTIFACTS = RESEARCH / "import-artifacts"
TARGET = (
    RESEARCH
    / "research-databases"
    / "f01-sec-isolated-research-historical50-v1"
    / "f01-sec-candidate.sqlite3"
)
D6_SHA = (
    "75627e9032cf0dd1410d9ad93663a3248a22df1998b394d7aff892df6fbe0955",
    "2c47e3da2bd21c03cc262232e85d775c0fde7190d49149656dfe72d8c3fc9752",
)
CONTROLS = {"AAPL", "ACN", "CAT", "COST", "LLY", "MSFT", "NVDA"}
D6_CONTRACT = "F01_HISTORICAL_SECURITY_50_ISSUER_PREFLIGHT_V1"
START, END = date(2011, 12, 29), date(2026, 11, 20)
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
CIK = re.compile(r"\d{10}")
HASH = re.compile(r"[0-9a-f]{64}")
PERIOD = date(2016, 1, 1)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bound(path, expected=None):
    raw = path.read_bytes()
    digest = sha(raw)
    if expected is not None and expected != digest:
        raise ValueError(f"INPUT_HASH_MISMATCH: {path}")
    return raw, {"path": str(path.absolute()), "sha256": digest}


def d6_inputs(universe_raw, inventory_raw):
    universe = list(csv.DictReader(io.StringIO(universe_raw.decode("utf-8-sig"))))
    inventory = json.loads(inventory_raw)
    candidates = {row["symbol"]: row for row in inventory["candidates"]}
    filings = inventory["issuer_filing_inventories"]
    by_symbol = {row["symbol_label"]: row for row in universe}
    ciks = {row["planning_cik"] for row in universe}
    if (
        inventory["contract"] != D6_CONTRACT
        or not len(universe) == len(by_symbol) == len(ciks) == len(inventory["candidates"]) == 50
        or set(by_symbol) != candidates.keys()
        or candidates.keys() != filings.keys()
        or any(not CIK.fullmatch(cik) for cik in ciks)
        or "SPY" in by_symbol
        or {sym for sym, row in by_symbol.items() if row["category"] == "E11_CONTROL"} != CONTROLS
        or any(
            row["planning_cik"] != candidates[sym]["research_cik"]
            or row["planning_cik"] != filings[sym]["cik"]
            or row["category"] != candidates[sym]["category"]
            for sym, row in by_symbol.items()
        )
    ):
        raise ValueError("D6_COHORT_OR_CIK_MISMATCH")
    return by_symbol, candidates, filings, inventory["estimated_capacity"]


def plain(path, *, directory=False):
    info = path.lstat()
    check = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not check(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError(f"UNSAFE_PATH: {path}")
    return info


def plain_tree(path):
    for component in reversed((path, *path.parents)):
        plain(component, directory=True)


def filing_index(raw, contract):
    if raw is None:
        return {}
    manifest = json.loads(raw)
    if manifest["contract"] != contract:
        raise ValueError(f"FILING_MANIFEST_CONTRACT_MISMATCH: {contract}")
    rows = {}
    for row in manifest["filings"]:
        key = row["cik"], row["accession"]
        if key in rows or not (CIK.fullmatch(key[0]) and ACCESSION.fullmatch(key[1])):
            raise ValueError(f"CACHE_MANIFEST_INVALID_KEY: {key}")
        rows[key] = row
    return rows


def extraction_status(root, key, record, historical_gap):
    if record is None:
        return "NO_D6_SHARD" if historical_gap else "D6_SHARD_NOT_RECHECKED"
    name = "-".join(key) + ".json"
    if record["file"] != name or not HASH.fullmatch(record["sha256"]):
        raise ValueError(f"INVALID_EXTRACTION_MANIFEST_ENTRY: {key}")
    try:
        if plain(root / name).st_size == record["bytes"]:
            return "SHARD_SIZE_MATCH_REVIEW_REQUIRED"
    except (OSError, ValueError):
        return "SHARD_UNVERIFIED"
    return "SHARD_SIZE_MISMATCH"


def cache_presence(root, cik, accession, record):
    folder = root / cik / accession
    try:
        plain(folder.parent, directory=True)
        plain(folder, directory=True)
        # Bound cache records name the primary. A generic directory needs an index
        # plus a document candidate, which still requires semantic/source review.
        names = (
            [record["primary"]]
            if record and record.get("primary")
            else [
                p.name
                for p in folder.iterdir()
                if p.suffix.lower() in {".htm", ".html", ".xml"}
                and not p.stem.lower().endswith(("_cal", "_def", "_lab", "_pre"))
                and p.name.lower() != "filingsummary.xml"
            ]
        )
        for name in names:
            if Path(name).name != name or ":" in name:
                raise ValueError(f"UNSAFE_CACHE_DOCUMENT: {name}")
        listed = {item.name for item in folder.iterdir()}
        if "index.json" not in listed or not set(names) & listed:
            return "CACHE_INCOMPLETE", None
        try:
            sizes = [plain(folder / name).st_size for name in names]
            index_size = plain(folder / "index.json").st_size
        except (PermissionError, ValueError):
            return "ORIGINAL_LISTED_UNVERIFIED", None
        if not any(sizes):
            return "CACHE_INCOMPLETE", None
        if not index_size:
            return "CACHE_INCOMPLETE", None
        return "ORIGINAL_PRESENT", sum(sizes)
    except FileNotFoundError as error:
        if Path(error.filename) in {folder.parent, folder}:
            return "PHYSICALLY_MISSING", None
        return "CACHE_INCOMPLETE", None
    except (OSError, ValueError):
        return "CACHE_UNVERIFIED", None


def historical_gaps(filings, expected_count):
    gaps = set()
    for record in filings.values():
        for era in record["period_counts"].values():
            for accession in era["missing_original_shard_keys"]:
                if accession not in record["filings"]:
                    raise ValueError(f"GAP_WITHOUT_METADATA: {accession}")
                gaps.add((record["cik"], accession))
    if len(gaps) != expected_count:
        raise ValueError("D6_GAP_COUNT_MISMATCH")
    return gaps


def taxonomy_gate(cache_record, coverage):
    urls = coverage.get("urls", {})
    issues = (cache_record or {}).get("issues", [])
    # Cache manifests do not carry a reviewed DTS closure per accession.
    return {
        "status": "DTS_CLOSURE_UNVERIFIED",
        "known_unbound_urls_in_inventory": sorted(
            url for url, record in urls.items() if record["status"] != "BOUND"
        ),
        "cache_issues": issues,
    }


def _rank(row):
    day = date.fromisoformat(row["filed"])
    # Prefer local reuse, the 2016 boundary including prior warm-up, annual
    # anchors, then stable CIK and accession. No economic features enter here.
    return (
        row["physical_status"] != "ORIGINAL_PRESENT",
        abs((day - PERIOD).days),
        row["form"] != "10-K",
        row["cik"],
        row["accession"],
    )


def select_batch(rows, limit):
    by_symbol = defaultdict(list)
    for row in sorted(rows, key=_rank):
        if (
            row["category"] != "E11_CONTROL"
            and row["acceptance"]
            and not row["filing_index_conflicted"]
            and row["physical_status"] in {"PHYSICALLY_MISSING", "ORIGINAL_PRESENT"}
        ):
            by_symbol[row["symbol"]].append(row)
    # Balanced first review batch: five issuers, with different D.6
    # categories first where available, and round-robin within those issuers.
    symbols = sorted(
        by_symbol,
        key=lambda sym: (
            by_symbol[sym][0]["category"] != "DATED_MARKET_AND_ISSUER_CANDIDATE",
            _rank(by_symbol[sym][0]),
        ),
    )
    first_by_category = {}
    for symbol in symbols:
        first_by_category.setdefault(by_symbol[symbol][0]["category"], symbol)
    chosen = list(dict.fromkeys([*first_by_category.values(), *symbols]))[:5]
    selected = []
    for rank in range(limit):
        for symbol in chosen:
            if len(by_symbol[symbol]) > rank:
                selected.append(by_symbol[symbol][rank])
                if len(selected) == limit:
                    return selected
    return selected


def prepare(
    universe_raw,
    inventory_raw,
    *,
    root,
    cache_raw=None,
    extraction_raw=None,
    extraction_root=None,
    taxonomy=None,
    issuers=None,
    start=START,
    end=END,
    limit=20,
):
    if not START <= start <= end <= END or not 1 <= limit <= 20:
        raise ValueError("DATE_RANGE_OR_BATCH_LIMIT_OUTSIDE_D6")
    universe, candidates, filings, reference = d6_inputs(universe_raw, inventory_raw)
    scope = set(universe) if issuers is None else set(issuers)
    if not scope or scope - universe.keys() or (issuers is not None and len(scope) != len(issuers)):
        raise ValueError("EXPLICIT_ISSUER_SCOPE_OUTSIDE_D6_OR_DUPLICATE")
    gaps = historical_gaps(filings, reference["missing_distinct_periodic_accessions"])
    cache = filing_index(cache_raw, "F01_EXTENDED_FILING_CACHE_V1")
    extracts = filing_index(extraction_raw, "F01_EXTENDED_FILING_EVIDENCE_V1")
    plain_tree(root)
    if extraction_root is not None:
        plain_tree(extraction_root)
    rows, issuer_rows = [], []
    for symbol in sorted(scope):
        candidate, inventory = candidates[symbol], filings[symbol]
        cik, category = inventory["cik"], candidate["category"]
        issuer_rows.append(
            {
                **universe[symbol],
                "filing_start": start.isoformat(),
                "filing_end": end.isoformat(),
                "source_admission": "REVIEW_REQUIRED",
                "accounting_admission": "REVIEW_REQUIRED",
                "submissions_member_sha256": inventory["submissions_member_sha256"],
            }
        )
        conflicts = inventory["filing_index_conflicts"]
        for accession, filing in sorted(inventory["filings"].items()):
            if not ACCESSION.fullmatch(accession):
                raise ValueError(f"METADATA_INVALID_ACCESSION: {accession}")
            if (
                filing["form"] not in FORMS
                or not start <= date.fromisoformat(filing["filed"]) <= end
            ):
                continue
            acceptance = filing["acceptance"]
            if acceptance and datetime.fromisoformat(acceptance).utcoffset() is None:
                raise ValueError(f"METADATA_UNZONED_ACCEPTANCE: {accession}")
            key = cik, accession
            record = cache.get(key)
            status, size = cache_presence(root, *key, record)
            rows.append(
                {
                    **filing,
                    "symbol": symbol,
                    "cik": cik,
                    "accession": accession,
                    "category": category,
                    "historical_gap": key in gaps,
                    "physical_status": status,
                    "observed_document_bytes": size,
                    "cache_manifest_listed": record is not None,
                    "extraction": extraction_status(
                        extraction_root, key, extracts.get(key), key in gaps
                    ),
                    "taxonomy": taxonomy_gate(record, taxonomy or {}),
                    "identity_blockers": candidate["reasons"],
                    "filing_index_conflicted": accession in conflicts,
                    "source_admission": "REVIEW_REQUIRED",
                    "accounting_admission": "REVIEW_REQUIRED",
                    "qualification": "UNVERIFIED_FOR_EXPANSION",
                }
            )
    selected = [
        row
        | {
            "selection_basis": "D6 CIK and acceptance, local reuse, 2016 boundary",
            "review_action": "VERIFY_LOCAL_SOURCE"
            if row["physical_status"] == "ORIGINAL_PRESENT"
            else "REVIEW_FOR_SEPARATELY_AUTHORIZED_ACQUISITION",
        }
        for row in select_batch(rows, limit)
    ]
    counts = Counter(row["physical_status"] for row in rows)
    historical_counts = Counter(row["physical_status"] for row in rows if row["historical_gap"])
    count = sum(row["physical_status"] == "PHYSICALLY_MISSING" for row in selected)
    reference_count = reference["reference_filing_count"]
    capacity = {
        "selected_accessions": len(selected),
        "originals_missing_in_selected_batch": count,
        "selected_local_document_bytes_observed": sum(
            row["observed_document_bytes"] or 0 for row in selected
        ),
        "estimated_new_original_bytes": round(
            count * reference["reference_original_cache_bytes"] / reference_count
        ),
        "estimated_extraction_evidence_bytes": round(
            len(selected) * reference["reference_shard_bytes"] / reference_count
        ),
        "free_bytes_observed": shutil.disk_usage(root).free,
        "basis": "D6 means. Excludes staging, taxonomies, requests, sidecars, reserve and backup.",
    }
    return {
        "contract": CONTRACT,
        "classification": "RESEARCH_ONLY",
        "planned_target": str(TARGET),
        "benchmark": "SPY",
        "cohort_count": 50,
        "control_count": 7,
        "additional_count": 43,
        "issuer_scope": issuer_rows,
        "date_range": [start.isoformat(), end.isoformat()],
        "d6_historical_gap_distinct_cik_accession": len(gaps),
        "current_physical_status": dict(counts),
        "d6_gap_current_physical_status": dict(historical_counts),
        "batch_limit": limit,
        "accessions": selected,
        "capacity": capacity,
        "readiness": {
            "planning": "REVIEW_BATCH_PREPARED" if selected else "NO_ELIGIBLE_REVIEW_CANDIDATES",
            "acquisition": "REQUIRES_EXPLICIT_AUTHORIZATION",
            "import": "BLOCKED_PENDING_SOURCE_ACCOUNTING_IDENTITY_AND_DTS_REVIEW",
            "trading": "BLOCKED",
        },
    }, rows


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path, value):
    with path.open("xb") as handle:
        handle.write(json_bytes(value))


def write_csv(path, records, columns):
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in record.items()
                }
            )


def publish(output, batch, rows):
    if (
        not output.is_absolute()
        or ".." in output.parts
        or not output.name.startswith("f01-historical-50-")
    ):
        raise ValueError("NEW_NAMED_ARTIFACT_DIRECTORY_REQUIRED")
    plain_tree(output.parent)
    if output.is_relative_to(RESEARCH) and not output.is_relative_to(ARTIFACTS):
        raise ValueError("OUTPUT_MUST_BE_UNDER_RESEARCH_IMPORT_ARTIFACTS")
    if any(output.is_relative_to(REPO / name) for name in ("data", "reports")):
        raise ValueError("PROTECTED_OUTPUT")
    output.mkdir()
    selected = batch["accessions"]
    selected_symbols = {row["symbol"] for row in selected}
    columns = (
        "symbol",
        "cik",
        "accession",
        "filed",
        "acceptance",
        "form",
        "category",
        "historical_gap",
        "physical_status",
        "observed_document_bytes",
        "cache_manifest_listed",
        "extraction",
        "taxonomy",
        "identity_blockers",
        "filing_index_conflicted",
        "source_admission",
        "accounting_admission",
        "qualification",
        "selection_basis",
        "review_action",
    )
    write_csv(
        output / "first_batch_issuers.csv",
        (row for row in batch["issuer_scope"] if row["symbol_label"] in selected_symbols),
        tuple(batch["issuer_scope"][0]),
    )
    write_csv(output / "first_batch_accessions.csv", selected, columns)
    write_csv(
        output / "first_batch_missing_sources.csv",
        (row for row in selected if row["physical_status"] != "ORIGINAL_PRESENT"),
        columns,
    )
    write_csv(
        output / "first_batch_accounting_risks.csv",
        selected,
        (
            "symbol",
            "cik",
            "accession",
            "form",
            "extraction",
            "taxonomy",
            "accounting_admission",
            "identity_blockers",
        ),
    )
    write_csv(output / "filing_gap_inventory.csv", rows, columns)
    write_json(output / "first_batch_capacity.json", batch["capacity"])
    write_json(output / "first_batch_readiness.json", batch["readiness"])
    # Written last. A directory without this manifest is incomplete.
    hashes = {path.name: sha(path.read_bytes()) for path in sorted(output.iterdir())}
    write_json(output / "first_batch_manifest.json", batch | {"artifacts_sha256": hashes})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--universe", type=Path, default=REPO / "reports/f01_historical_50_pilot.csv"
    )
    parser.add_argument(
        "--metadata", type=Path, default=REPO / "reports/f01_historical_50_preflight.json"
    )
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--cache-manifest", type=Path)
    parser.add_argument("--extraction-manifest", type=Path)
    parser.add_argument("--taxonomy-inventory", type=Path)
    parser.add_argument(
        "--issuer", action="append", help="D.6 symbol label; repeat for multiple issuers"
    )
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=END)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and not args.plan:
        parser.error("--output requires --plan")
    try:
        universe, universe_binding = read_bound(args.universe, D6_SHA[0])
        metadata, metadata_binding = read_bound(args.metadata, D6_SHA[1])
        cache_raw, cache_binding = (
            read_bound(args.cache_manifest) if args.cache_manifest else (None, None)
        )
        taxonomy_raw, taxonomy_binding = (
            read_bound(args.taxonomy_inventory) if args.taxonomy_inventory else (None, None)
        )
        extraction_raw, extraction_binding = (
            read_bound(args.extraction_manifest) if args.extraction_manifest else (None, None)
        )
        batch, rows = prepare(
            universe,
            metadata,
            root=args.cache.absolute(),
            cache_raw=cache_raw,
            extraction_raw=extraction_raw,
            extraction_root=args.extraction_manifest.absolute().parent
            if args.extraction_manifest
            else None,
            taxonomy=json.loads(taxonomy_raw) if taxonomy_raw else None,
            issuers=args.issuer,
            start=args.start,
            end=args.end,
            limit=args.batch_size,
        )
        evidence = repository_evidence(REPO)
        batch["candidate"] = {
            "head": evidence["git_head"],
            "fingerprint": evidence["code_fingerprint"],
        }
        batch["bindings"] = {
            "universe": universe_binding,
            "sec_metadata": metadata_binding,
            "local_cache_manifest": cache_binding,
            "local_cache_root": str(args.cache.absolute()),
            "taxonomy_inventory": taxonomy_binding,
            "extraction_manifest": extraction_binding,
            "historical_source_generation": json.loads(metadata)["bindings"][
                "bulk_manifest_sha256"
            ],
        }
        # Free disk space is a volatile observation. It cannot invalidate resume.
        stable_batch = {k: v for k, v in batch.items() if k != "capacity"}
        batch["plan_sha256"] = sha(json_bytes(stable_batch))
        if args.plan:
            publish(args.output or ARTIFACTS / "f01-historical-50-preparation-v1", batch, rows)
        print(json.dumps(batch, ensure_ascii=True))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
