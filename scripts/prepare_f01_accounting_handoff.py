"""Bind reviewed Accounting evidence to a new Canonical Research pilot lineage."""

import argparse
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import download_f01_sec_bulk as bulk

CONTRACT = "F01_CANONICAL_ACCOUNTING_HANDOFF_APPROVAL_V1"
SCOPE = "F01_CANONICAL_ACCOUNTING_HANDOFF_V1"
E8_SHA = "949f8cbc7a0d7578fbd9aa748f3aab95e9aed9ec45d33348dcc74a0d89e5948b"
E10_SHA = "b6f066658e3817b0e742c9667049fa9d4c44d1662c94c4131f861646289e4d2f"
E10_CODE = "9e6f9f97fa8dd4939fd28644deccaf42e1da501aaedcc4185c39020b70621846"
FILING_SHA = "89df83b5b2fc304986aa09f9e26391aa66a62b530d46d73cb0012c3425332194"
ACCOUNTING_SHA = "f28307df196f98c69b3065060673786a2f6e98569b0cdcb4da52a81ec359992f"
TERM = "current_and_noncurrent_term_debt_carrying_subtotal_v1"
RUN = "20261122-124810"
NY = ZoneInfo("America/New_York")
SOURCE_FIELDS = {"accession", "cik", "sha256", "filename", "acceptance", "bytes"}
require = bulk.require


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bound(path, expected):
    raw, value = bulk.read_metadata(path, max_bytes=64 * 1024**2)
    require(sha(raw) == expected, f"ACCOUNTING_HANDOFF_SHA256_MISMATCH: {path.name}")
    return value


def validate_parity(e8, e10):
    historical = (
        "source_facts",
        "coverage",
        "trial_debt_references",
        "policy_qualification",
        "open_technical_facts",
        "inputs",
        "rule_catalog",
        "rule_catalog_sha256",
    )
    require(all(e8[key] == e10[key] for key in historical), "E8_E10_VALUE_OR_SOURCE_DRIFT")
    for old, new in zip(e8["probes"], e10["probes"], strict=True):
        require(old == {k: v for k, v in new.items() if k != "financing"}, "E8_E10_PROBE_DRIFT")
    require(len({(r["symbol"], r["session"]) for r in e10["probes"]}) == 77, "HANDOFF_GRID_DRIFT")


def nested(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nested(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested(item)


def source_bindings(e10):
    registry = e10["source_facts"]
    documents = {}
    linkbases = defaultdict(set)
    for obj in nested(e10):
        if obj.keys() >= SOURCE_FIELDS:
            key = obj["cik"], obj["accession"], obj["filename"]
            require(documents.setdefault(key, obj) == obj, "ORIGINAL_SOURCE_BINDING_CONFLICT")
        if obj.get("presentation") and (source := obj.get("source")):
            linkbases[source["cik"], source["accession"]].update(
                digest for _, digest in obj["presentation"]
            )
        if "linkbase_sha256" in obj and (source := obj.get("note")):
            linkbases[source["cik"], source["accession"]].add(obj["linkbase_sha256"])
    for probe in e10["probes"]:
        cutoff = datetime.combine(datetime.fromisoformat(probe["session"]).date(), time(16), NY)
        used = set()
        for node in nested(probe["preview"] | probe["financing"]):
            if node.keys() >= SOURCE_FIELDS:
                used.add(node["acceptance"])
            for ref in node.get("sources", ()):
                if isinstance(ref, str):
                    used.add(registry[ref]["original"]["acceptance"])
        for value in used:
            observed = datetime.fromisoformat(value)
            require(observed.tzinfo is not None and observed <= cutoff, "ORIGINAL_LOOK_AHEAD")
    return documents, linkbases


def manifests(root, documents, linkbases):
    original = root / "sec-originals" / "filing-evidence"
    filing_dir = original / f"f01-extended-filing-evidence-{RUN}"
    accounting_dir = root / "import-artifacts" / f"f01-extended-accounting-evidence-{RUN}"
    filing = read_bound(filing_dir / "manifest.json", FILING_SHA)
    accounting = read_bound(accounting_dir / "manifest.json", ACCOUNTING_SHA)
    require(
        accounting["inputs"]["filing_manifest_sha256"] == FILING_SHA, "SHARD_MANIFEST_LINK_DRIFT"
    )
    selected = {(cik, accession) for cik, accession, _ in documents}
    files = []
    for base, derived in zip(filing["filings"], accounting["filings"], strict=True):
        key = base["cik"], base["accession"]
        name = f"{key[0]}-{key[1]}.json"
        require(
            derived["source_sha256"] == base["sha256"]
            and (derived["cik"], derived["accession"]) == key
            and derived["file"] == base["file"] == name,
            "ACCOUNTING_SHARD_LINK_DRIFT",
        )
        if key in selected:
            files += [(filing_dir / name, base), (accounting_dir / name, derived)]
            selected.remove(key)
    require(not selected, "ORIGINAL_ACCESSION_NOT_IN_BOUND_SHARDS")
    for (cik, accession), digests in sorted(linkbases.items()):
        folder = bulk.directory(original / cik / accession)
        resolved = {
            meta["sha256"]
            for (issuer, filing, _), meta in documents.items()
            if (issuer, filing) == (cik, accession) and meta["sha256"] in digests
        }
        for cached in sorted(folder.iterdir()):
            if resolved == digests:
                break
            if cached.suffix.lower() not in {".xml", ".xsd"}:
                continue
            _, meta = bulk.read_metadata(cached / "source.json")
            if meta["sha256"] in digests:
                key = cik, accession, cached.name
                require(documents.setdefault(key, meta) == meta, "LINKBASE_CACHE_BINDING_CONFLICT")
                resolved.add(meta["sha256"])
        require(resolved == digests, "BOUND_LINKBASE_MISSING_FROM_CACHE")
    for (cik, accession, name), reference in sorted(documents.items()):
        require(
            re.fullmatch(r"[0-9]{10}", cik)
            and re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name)
            and ".." not in name,
            "UNSAFE_ORIGINAL_DOCUMENT_REFERENCE",
        )
        target = original / cik / accession / name
        _, meta = bulk.read_metadata(target / "source.json")
        require(
            meta == reference
            and (meta["cik"], meta["accession"], meta["filename"]) == (cik, accession, name),
            "ORIGINAL_CACHE_METADATA_DRIFT",
        )
        files.append((target / "content", reference))
    return files


def verify_files(files):
    for path, reference in files:
        bulk.directory(path.parent)
        info = bulk.plain(path)
        require(info.st_size == reference["bytes"], f"HANDOFF_FILE_SIZE_MISMATCH: {path}")
        with path.open("rb") as stream:
            require(os.path.samestat(info, os.fstat(stream.fileno())), "HANDOFF_FILE_REPLACED")
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(actual == reference["sha256"], f"HANDOFF_FILE_SHA256_MISMATCH: {path}")


def evidence(repo, root, *, verify_shards=False):
    report_dir = repo / "reports"
    e8 = read_bound(report_dir / "f01_accounting_policy_qualification.json", E8_SHA)
    e10 = read_bound(report_dir / "f01_accounting_policy_consolidation.json", E10_SHA)
    validate_parity(e8, e10)
    e8_template = e8["approval_template"]
    e8_binding = e8_template["binding"]
    financing = e10["financing_consolidation"]
    require(
        e10["candidate_code"]["fingerprint"] == E10_CODE
        and financing["baseline_sha256"] == E8_SHA
        and financing["rules_sha256"] == sha(bulk.json_bytes(financing["rules"]))
        and e8_binding["rules_sha256"] == sha(bulk.json_bytes(e8_template["rule_definitions"]))
        and e8_binding["candidate_fingerprint"] == e8["candidate_code"]["fingerprint"]
        and e8["inputs"]["filing_manifest_sha256"] == FILING_SHA
        and e8["inputs"]["accounting_manifest_sha256"] == ACCOUNTING_SHA,
        "ACCOUNTING_CATALOG_OR_SOURCE_DRIFT",
    )
    documents, linkbases = source_bindings(e10)
    files = manifests(root, documents, linkbases)
    if verify_shards:
        verify_files(files)
    rules = {f"E8/{name}": data for name, data in e8_template["rule_definitions"].items()}
    rules.update(
        {f"E10/{name}": financing["rules"][name] for name in ("DIRECT_POSITION", "TERM_SUBTOTAL")}
    )
    binding = {
        "e8_replay_sha256": E8_SHA,
        "e10_replay_sha256": E10_SHA,
        "replay_paths_from_repo": {
            "e8": "reports/f01_accounting_policy_qualification.json",
            "e10": "reports/f01_accounting_policy_consolidation.json",
        },
        "historical_e8_candidate": e8["candidate_code"]["fingerprint"],
        "historical_e10_candidate": E10_CODE,
        "original_filing_manifest_sha256": FILING_SHA,
        "accounting_manifest_sha256": ACCOUNTING_SHA,
        "bulk_source_manifest_sha256": e8_binding["bulk_source_manifest_sha256"],
        "e8_catalog_sha256": e8_binding["rules_sha256"],
        "e10_catalog_sha256": financing["rules_sha256"],
        "review_catalog_sha256": sha(bulk.json_bytes(rules)),
        "file_inventory_sha256": sha(
            bulk.json_bytes([[str(path), item["bytes"], item["sha256"]] for path, item in files])
        ),
        "file_inventory_bytes": sum(item["bytes"] for _, item in files),
        "file_inventory_count": len(files),
        "term_subtotal": {"metric": TERM, "storage": "E10_RESEARCH_SIDECAR_ONLY"},
    }
    return binding, rules, e10["source_facts"], files


def template(binding, rules, external, code):
    lineage = external["lineage"]
    require(
        lineage["classification"] == "RESEARCH_ONLY"
        and lineage["research_scope"] == SCOPE
        and lineage["code_fingerprint"] == code["fingerprint"],
        "NEW_CANDIDATE_RESEARCH_BOOTSTRAP_REQUIRED",
    )
    return {
        "contract": CONTRACT,
        "binding": binding
        | {
            "candidate": code,
            "lineage": lineage,
            "target": external["database"],
            "schema_fingerprint": external["schema_fingerprint"],
            "bootstrap_database_sha256": external["database_sha256_at_bootstrap"],
        },
        "rule_definitions": rules,
        "reviewer": "",
        "decisions": dict.fromkeys(rules, "PENDING"),
    }


def validate_review(expected, reviewed):
    require(
        {k: v for k, v in reviewed.items() if k not in {"reviewer", "decisions"}}
        == {k: v for k, v in expected.items() if k not in {"reviewer", "decisions"}},
        "ACCOUNTING_REVIEW_BINDING_DRIFT",
    )
    reviewer = reviewed.get("reviewer")
    require(isinstance(reviewer, str) and reviewer.strip(), "INDEPENDENT_REVIEWER_REQUIRED")
    require(
        reviewed.get("decisions") == dict.fromkeys(expected["decisions"], "APPROVE_RESEARCH"),
        "FULL_RESEARCH_POLICY_REVIEW_REQUIRED",
    )


def projection_summary(registry, facts):
    from trading_system.data.canonical_facts import COLS, KEY, MUTABLE
    from trading_system.data.database import _fact_rows
    from trading_system.models.fundamentals import FundamentalFact

    slots = {name: COLS.index(name) for name in (*KEY, *MUTABLE, "taxonomy", "symbol")}
    key_slots = tuple(slots[name] for name in KEY)
    comparable = tuple(slots[name] for name in (*MUTABLE, "taxonomy", "symbol"))
    by_key = {}
    for row in _fact_rows(facts):
        key = tuple(row[i] for i in key_slots)
        value = tuple(row[i] for i in comparable)
        require(by_key.setdefault(key, value) == value, "PLANNED_CANONICAL_KEY_CONFLICT")
    counts = Counter()
    for row in _fact_rows(
        FundamentalFact.model_validate(item["fact"]) for item in registry.values()
    ):
        key = tuple(row[i] for i in key_slots)
        if key not in by_key:
            counts["evidence_only"] += 1
        elif by_key[key] == tuple(row[i] for i in comparable):
            counts["exact_planned_fact"] += 1
        else:
            counts["same_key_conflict"] += 1
    return dict(counts)


def main():
    import import_f01_sec_bulk_pilot as pilot

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--verify-shards", action="store_true")
    args = parser.parse_args()
    if bool(args.layout) != bool(args.target):
        parser.error("--layout and --target must be supplied together")
    code = pilot.code_binding()
    binding, rules, registry, _ = evidence(
        pilot.REPO, pilot.RESEARCH_ROOT, verify_shards=args.verify_shards
    )
    if args.target:
        external, _ = pilot.bootstrap_target(bulk.Layout.load(args.layout), args.target)
        result = template(binding, rules, external, code)
    else:
        result = {
            "status": "POLICY_PREVIEW_PARITY_VERIFIED",
            "candidate": code,
            "binding": binding,
            "review_rules": rules,
            "source_facts": len(registry),
            "referenced_file_hashes_verified": args.verify_shards,
        }
    sys.stdout.write(bulk.json_bytes(result).decode())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ACCOUNTING_HANDOFF_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(3) from None
