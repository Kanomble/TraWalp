"""Reconcile SEC bytes, published original evidence and exact Canonical lineage facts."""

import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import f01_task9_auto_admission as auto
import prepare_f01_accounting_handoff as handoff
import run_f01_historical_50_manual as task9
from f01_readiness_inventory import digest, read_database
from f01_task96_contract import ARTIFACTS, JOBS, PARENT, REPO, RESEARCH, key, require

FACT_KEYS = (
    "cik",
    "accession_number",
    "metric",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "filed",
    "value",
)

filing = task9.filing
cache_root = filing.ROOT
EXT_ROOT = cache_root / "f01-extended-filing-evidence-20261122-124810"
READER = ARTIFACTS / "f01-e13-reader-validation-20261122-211700.json"
READER_SHA = "0dac82fea25004b98a732735b45342f0919243bb8e80039218166c9c29d9195b"
LINEAGES = (
    "f01-sec-isolated-research-pilot01",
    "f01-sec-isolated-research-accounting-e11",
    "f01-sec-isolated-research-historical50-v1",
)
METADATA_EXCEPTIONS = (ValueError, KeyError, TypeError, OSError, RuntimeError)


def checked_original(folder, row, expected):
    """Verify the listing and full document set against an independent published commitment."""
    task9.bulk.directory(folder)
    index, listing = filing.cache_read(folder / "index.json", row, "index.json")
    names = [
        "index.json",
        *dict.fromkeys(
            filing.documents(row, json.loads(index)) + filing.taxonomy_files(json.loads(index))
        ),
    ]
    bound = {doc["filename"]: doc for doc in expected}
    require(len(bound) == len(expected) and set(names) == set(bound), "DOCUMENT_SET_NOT_BOUND")
    documents = []
    for name in names:
        _, meta = (
            (index, listing)
            if name == "index.json"
            else filing.cache_read(folder / name, row, name)
        )
        require(
            meta == bound[name]
            and meta["filed"] == row["filed"]
            and task9.review.instant(meta["acceptance"]) == task9.review.instant(row["acceptance"]),
            f"ORIGINAL_PROVENANCE_MISMATCH: {name}",
        )
        documents.append(meta)
    return {"directory": str(folder), "documents": documents}


def dts_state(report, sha, urls, known):
    blocker = []
    for url, entry in sorted(urls.items()):
        if url not in known:
            try:
                cached = task9.taxonomy.bound_input(filing.ARELLE_ROOT, url)
                known[url] = cached[1]["sha256"] if cached else None
            except METADATA_EXCEPTIONS:
                known[url] = None
        if not known[url] or entry.get("source", {}).get("sha256") != known[url]:
            blocker.append(url)
    return {
        "historical_report_sha256": sha,
        "url_count": len(urls),
        "unbound_urls": blocker,
        "status": "DTS_INCOMPLETE"
        if blocker or not urls or not report["sec_plugin_verified"]
        else "LOCAL_BOUND_DEPENDENCIES_MATCH",
    }


def published_catalog(plan, parent):
    read = plan.inputs.json
    extended = read(EXT_ROOT / "manifest.json", handoff.FILING_SHA)
    cache = read(
        cache_root / "f01-extended-cache-20261122-124810.json", extended["inputs"]["cache_sha256"]
    )
    taxonomy_sha = extended["inputs"]["taxonomy_coverage_sha256"]
    taxonomy = read(
        filing.ARELLE_ROOT / "taxonomy-extended-coverage-20261122-124810.json", taxonomy_sha
    )
    require(
        taxonomy["draft_sha256"] == extended["inputs"]["cache_sha256"],
        "EXTENDED_TAXONOMY_CACHE_BINDING_CHANGED",
    )
    cached_dts = {}
    shared = dts_state(taxonomy, taxonomy_sha, taxonomy["urls"], cached_dts)
    shard_index = {key(r): r for r in extended["filings"]}
    sources, shards, dts = defaultdict(list), {}, {}
    for row in cache["filings"]:
        if row["index_source"]:
            sources[key(row)].append(
                {
                    "folder": cache_root.joinpath(*key(row)),
                    "documents": [row["index_source"], *row["cached_sources"]],
                    "binding_sha256": extended["inputs"]["cache_sha256"],
                }
            )
        entry = shard_index.get(key(row))
        if entry:
            path = EXT_ROOT / f"{row['cik']}-{row['accession']}.json"
            require(entry["file"] == path.name, "EXTENDED_EXTRACTION_PATH_MISMATCH")
            shards[key(row)] = (path, entry["sha256"], entry["bytes"], None)
            dts[key(row)] = shared
    jobs = sorted(task9.ARTIFACTS.glob("f01-task9-*"))
    for job in jobs:
        for path in sorted(job.glob("verified-originals*.json")):
            binding = parent["input_bindings"].get(str(path), {})
            if not binding.get("sha256"):
                continue
            report = read(path, binding["sha256"])
            if (
                report["status"] != "SOURCE_VERIFIED"
                or report["source_generation"] != plan.source_sha
            ):
                continue
            for filing_id, item in report["filings"].items():
                pair = tuple(filing_id.split("/"))
                folder = Path(item["directory"])
                require(
                    folder in {job.joinpath("originals", *pair), cache_root.joinpath(*pair)},
                    "ORIGINAL_SOURCE_PATH_OUTSIDE_BOUND_CACHE",
                )
                sources[pair].append(
                    {
                        "folder": folder,
                        "documents": item["documents"],
                        "binding_sha256": binding["sha256"],
                    }
                )
        path = job / "extraction-manifest.json"
        binding = parent["input_bindings"].get(str(path), {})
        if not binding.get("sha256"):
            continue
        report = read(path, binding["sha256"])
        closures = [
            p
            for p in sorted(job.glob("taxonomy-*.json"))
            if parent["input_bindings"].get(str(p), {}).get("sha256") == report["taxonomy_sha256"]
        ]
        closure = read(closures[0], report["taxonomy_sha256"]) if len(closures) == 1 else None
        for filing_id, entry in report["filings"].items():
            pair = tuple(filing_id.split("/"))
            if not any(r["binding_sha256"] == report["source_sha256"] for r in sources[pair]):
                continue
            _, accession = pair
            expected = entry.get("sha256") if isinstance(entry, dict) else entry
            item = job / f"{accession}-extraction.json"
            shards[pair] = (item, expected, item.stat().st_size, report["source_sha256"])
            if closure and closure["source_sha256"] == report["source_sha256"]:
                dts[pair] = dts_state(
                    closure, report["taxonomy_sha256"], closure["filings"][filing_id], cached_dts
                )
    if JOBS.is_dir():
        for job in sorted(JOBS.iterdir()):
            path = job / "result.json"
            block = plan.blocks.get(job.name)
            if not path.is_file() or not block or block[0] != "SEC":
                continue
            record = read(path)
            scope = read(job / "scope.json")
            scope_keys = {key(r) for r in scope["network_filing_scope"]}
            require(
                scope["block_id"] == job.name
                and scope["parent"] == plan.binding()
                and scope_keys <= {key(r) for r in block[1]["accessions"]}
                and record["binding"]["scope"] == scope["authorization_draft"]["scope"],
                "NEW_SEC_RECEIPT_NOT_BOUND_TO_PLAN",
            )
            for filing_id, item in record["verified_filings"].items():
                pair = tuple(filing_id.split("/"))
                folder = job.joinpath("originals", *pair)
                require(
                    pair in scope_keys and Path(item["directory"]) == folder,
                    "NEW_SEC_RECEIPT_ACCESSION_MISMATCH",
                )
                sources[pair].append(
                    {
                        "folder": folder,
                        "documents": item["documents"],
                        "binding_sha256": plan.inputs.files[str(path)]["sha256"],
                    }
                )
    return jobs, sources, shards, dts


def canonical_links(plan):
    """Legacy storage alone makes no claim about an original or reader eligibility."""
    dbs = RESEARCH / "research-databases"
    read = plan.inputs.json
    reader = read(READER, READER_SHA)
    scope = reader["bindings"]
    require(
        reader["status"] == "PARITY_VERIFIED"
        and not reader["findings"]
        and scope["original_filing_manifest_sha256"] == handoff.FILING_SHA,
        "E13_READER_EVIDENCE_UNBOUND",
    )
    reviewed = read(
        ARTIFACTS / "f01-accounting-e11-reviewed-20261122-195121.json", scope["e11_review_sha256"]
    )
    require(
        all(v == "APPROVE_RESEARCH" for v in reviewed["decisions"].values()), "E11_NOT_APPROVED"
    )
    trace = read(REPO / "reports/f01_accounting_policy_consolidation.json", handoff.E10_SHA)
    original_refs = defaultdict(list)
    for ref, row in trace["source_facts"].items():
        fact = row["fact"]
        if fact["accession_number"] and row["original"]["matches"]:
            original_refs[fact["cik"], fact["accession_number"]].append((ref, row))
    links, stored = defaultdict(dict), set()
    for lineage in LINEAGES:
        path = dbs / lineage / "f01-sec-candidate.sqlite3"
        with read_database(
            path, plan.inputs.files, reader["database_sha256"] if lineage == LINEAGES[1] else None
        ) as db:
            version = db.execute("SELECT MAX(version) FROM fact_dataset").fetchone()[0]
            if lineage == LINEAGES[1]:
                require(version == scope["dataset_version"], "E13_DATASET_VERSION_CHANGED")
                for row in db.execute(
                    """SELECT f.cik,f.accession_number,f.metric,f.tag,f.unit,f.period_start,
                    f.period_end,f.filed,f.value FROM canonical_fact f"""
                ):
                    if row[:2] in original_refs:
                        stored.add((*row[:-1], Decimal(row[-1])))
            for cik, accession, count in db.execute(
                "SELECT cik,accession_number,COUNT(*) FROM canonical_fact GROUP BY 1,2"
            ):
                links[cik, accession][lineage] = {"stored_facts": count, "dataset_version": version}
            if lineage == LINEAGES[-1]:
                rows = db.execute(
                    """SELECT f.cik,f.accession_number,f.metric,r.value,o.version,o.evidence,
                        h.request_id
                    FROM canonical_fact f
                        JOIN source_occurrence o USING(fact_id)
                    JOIN fact_revision r ON r.fact_id=f.fact_id AND r.version=o.version
                    JOIN ingestion_receipt h ON h.version=o.version
                        WHERE o.forward_order_admitted=1"""
                )
                for cik, accession, metric, value, ver, evidence, request_id in rows:
                    values = links[cik, accession][lineage].setdefault("imported_witnesses", [])
                    values.append(
                        {
                            "metric": metric,
                            "value": value,
                            "version": ver,
                            "receipt_id": request_id,
                            "binding": json.loads(evidence),
                        }
                    )
    return links, original_refs, scope, stored


def proof_facts(pair, original_refs, result, links, stored):
    if LINEAGES[1] not in links.get(pair, {}) or result["status"] != "ORIGINAL_VERIFIED":
        return []
    docs = {(d["filename"], d["sha256"]) for d in result["documents"]}
    return [
        {"trace_reference": ref, "metric": r["fact"]["metric"], "fact": r["fact"]}
        for ref, r in original_refs[pair]
        if tuple(r["fact"][k] if k != "value" else Decimal(r["fact"][k]) for k in FACT_KEYS)
        in stored
        and any(
            (match["source"]["filename"], match["source"]["sha256"]) in docs
            and match["source"]["cik"] == pair[0]
            and match["source"]["accession"] == pair[1]
            for match in r["original"]["matches"]
        )
    ]


def original_state(pair, row, known, roots):
    folders = [root.joinpath(*pair) for root in roots if root.joinpath(*pair).is_dir()]
    issues = []
    if row is None:
        issues.append("ARCHIVED_SUBMISSIONS_IDENTITY_OR_PERIOD_UNRESOLVED")
    else:
        for source in known:
            try:
                result = checked_original(source["folder"], row, source["documents"])
            except METADATA_EXCEPTIONS as exc:
                issues.append(str(exc))
            else:
                return {
                    "status": "ORIGINAL_VERIFIED",
                    **result,
                    "issues": issues,
                    "original_binding_sha256": source["binding_sha256"],
                    "cache_present": True,
                }
    return {
        "status": "LOCAL_ORIGINAL_REVIEW_REQUIRED" if folders or known else "ORIGINAL_MISSING",
        "directories": [str(path) for path in folders],
        "cache_present": bool(folders),
        "issues": issues or (["NO_TRUSTED_DOCUMENT_SET_MANIFEST"] if folders else []),
    }


def admission_archive(plan, pair, result, links, shard):
    """Reuse original Task-9 decisions only when a stored receipt refers to the exact documents."""
    witnesses = links.get(pair, {}).get(LINEAGES[-1], {}).get("imported_witnesses", [])
    accepted = []
    root = task9.ARTIFACTS
    for item in witnesses:
        bind = item["binding"]
        if bind["source_sha256"] != result.get("original_binding_sha256") or not shard:
            continue
        job = (
            shard["path"].parent if isinstance(shard["path"], Path) else Path(shard["path"]).parent
        )
        if job.parent != root:
            continue
        sha = bind.get("auto_admission_sha256")
        if sha:
            path = job / f"auto-admission-{sha}.json"
            report = plan.inputs.json(path, sha)
            candidate = report["filings"]["/".join(pair)]["accounting"][item["metric"]]["admitted"]
            if (
                report == auto.batch_report(report["filings"], report["binding"])
                and report["binding"]["extraction_sha256"] == bind["extraction_sha256"]
                and candidate
                and candidate["fact"]["value"] == item["value"]
            ):
                accepted.append(item | {"authority": "AUTOMATED_ARCHIVED"})
        elif bind.get("accounting_review_sha256") and bind.get("source_review_sha256"):
            account = plan.inputs.json(
                root / "nke-01-accounting-approved-task91.json", bind["accounting_review_sha256"]
            )
            source = plan.inputs.json(
                root / "nke-01-source-approved-task91.json", bind["source_review_sha256"]
            )
            if account["decision"] == source["decision"] == "APPROVED":
                accepted.append(item | {"authority": "MANUAL_ARCHIVED"})
    return accepted


def inventory(plan, *, progress=lambda _text: None, metadata=None):
    parent = plan.inputs.json(PARENT / "manifest.json")
    jobs, sources, shards, dts = published_catalog(plan, parent)
    roots = [cache_root, *(job / "originals" for job in jobs)]
    if JOBS.is_dir():
        roots.extend(job / "originals" for job in sorted(JOBS.iterdir()) if job.is_dir())
    if metadata is None:
        metadata, _ = plan.metadata(plan.filings.values())
    links, original_refs, scope, stored = canonical_links(plan)
    results, documents, stats = {}, [], Counter()
    for number, (pair, row) in enumerate(sorted(plan.filings.items()), 1):
        if number % 250 == 0:
            progress(f"SEC evidence {number}/{len(plan.filings)}")
        current = original_state(pair, metadata.get(pair), sources[pair], roots)
        entry = shards.get(pair)
        extraction = None
        if entry and current["status"] == "ORIGINAL_VERIFIED":
            path, sha, size, source = entry
            try:
                verified = task9.bulk.plain(path).st_size == size and digest(path) == sha
            except OSError:
                verified = False
            if verified and (source is None or source == current["original_binding_sha256"]):
                extraction = {
                    "path": str(path),
                    "sha256": sha,
                    "bytes": size,
                    "status": "EXTRACTION_COMPLETE"
                    if source
                    else "EXTENDED_SHARD_BOUND_REVIEW_REQUIRED",
                    "taxonomy_evidence": dts.get(pair),
                }
            else:
                current["issues"].append("EXTRACTION_SHARD_OR_SOURCE_BINDING_MISMATCH")
        exact = proof_facts(pair, original_refs, current, links, stored)
        old = admission_archive(plan, pair, current, links, extraction)
        tier = (
            "QUALIFIED_IN_BOUND_OTHER_LINEAGE"
            if exact
            else "LOCAL_ORIGINAL_CANDIDATE"
            if current["status"] != "ORIGINAL_MISSING"
            else "ORIGINAL_MISSING"
        )
        stats[current["status"]] += 1
        stats["physically_present_directories"] += current["cache_present"]
        stats["extraction_shards_reused"] += extraction is not None
        stats["local_dts_evidence_consistent"] += bool(
            extraction and dts.get(pair, {}).get("status") == "LOCAL_BOUND_DEPENDENCIES_MATCH"
        )
        stats["cross_lineage_filing_witnesses"] += bool(exact)
        stats["cross_lineage_fact_witnesses"] += len(exact)
        stats["archived_nke_admitted_facts"] += len(old)
        stats["verified_original_bytes"] += sum(d["bytes"] for d in current.get("documents", []))
        if current["status"] == "ORIGINAL_VERIFIED":
            for doc in current["documents"]:
                documents.append(
                    {
                        "cik": pair[0],
                        "accession": pair[1],
                        "document": doc["filename"],
                        "sha256": doc["sha256"],
                        "bytes": doc["bytes"],
                        "url": doc["url"],
                        "status": current["status"],
                        "binding": current["original_binding_sha256"],
                    }
                )
        results[pair] = {
            "cik": pair[0],
            "accession": pair[1],
            "symbol": row["symbol_label"],
            "acceptance": row["acceptance_utc"],
            "form": row["form"],
            "stage": tier,
            "status": current["status"],
            "issues": current["issues"],
            "source": current,
            "extraction": extraction,
            "canonical": links[pair],
            "reader_scope": scope if exact else None,
            "qualified_witnesses": exact,
            "archived_admission": old,
            "new_lineage_admission": "NOT_GRANTED",
        }
    return results, documents, dict(stats)
