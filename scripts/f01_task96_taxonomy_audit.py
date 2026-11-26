"""Publish the offline COST/ORCL taxonomy inventory and recovery evidence."""

import os
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import f01_task96_taxonomy as tax
from f01_readiness_inventory import digest
from f01_task96_contract import (
    ARTIFACTS,
    JOBS,
    PARENT,
    REPO,
    RESEARCH,
    current_code,
    key,
    publish,
    require,
)
from f01_task96_evidence import (
    METADATA_EXCEPTIONS,
    READER,
    READER_SHA,
    checked_original,
    handoff,
    published_catalog,
)
from f01_task96_process import original_references, progress_for
from f01_task96_taxonomy_acquisition import acquisition_plan

from trading_system.data.canonical_facts import fingerprint

task9 = tax.task9
OUTPUT = ARTIFACTS / "f01-task961-taxonomy-resolution-v1"
PILOT = "1376a9bd153436467d5f71e3d0ee6a968b71a0d02f02e57254eaa2176b5a7801"
OLD = RESEARCH / "arelle-evidence/f01-task96-acquisition-v1"
DIAGNOSES = {
    "0000909832/0001193125-12-512914": (
        "70f328847a53693b8c9be8ed67bb45b91124429eb122a4ccf8d3684b6ecec0b3/"
        "taxonomy-9f3bfd29ee0dc5c62b8327f8286e848a2e15a92d552ff422fa34831f632fc2bb.json",
        "a38c6c5f79c7137cd05152ce2eb7458f3f0fdf998dce9ab805966bb1accf14c4",
    ),
    "0001341439/0001193125-12-513009": (
        "ed4a7508526160bf48c7a4a4f245a379d237e81423ff1f542a16c0acad4b424b/"
        "taxonomy-c9de3bce7ec811ed4e7a17b385dd204bffc556cc225d53a3e8b09b0317b8d5ee.json",
        "569a1a1ff4d318d73a92de240f295a94a5e8eef20666a0b5ecaaa6031e4d9156",
    ),
}


def inspect_local(resolver):
    arelle = resolver.arelle
    home = Path(os.environ["USERPROFILE"])
    roots = [
        arelle / "cache",
        arelle / "packages",
        arelle / "bootstrap-packages",
        arelle / "site/arelle/resources/cache",
        arelle / "pip-cache",
        home / "AppData/Local/Arelle",
        home / "AppData/Roaming/Arelle",
        Path("C:/Users/Lukas Becker/AppData/Local/Arelle"),
        Path("C:/Users/Lukas Becker/AppData/Roaming/Arelle"),
        RESEARCH / "taxonomy-cache",
        RESEARCH / "edgartools",
    ]
    names = {uri.rsplit("/", 1)[-1] for uri in tax.REQUIRED}
    result = {"searched_roots": {}, "candidates": [], "package_manifests": [], "limitations": []}
    manifest = arelle / "taxonomy-packages.json"
    if tax.BULK.exists(manifest):
        task9.bulk.plain(manifest)
        result["package_manifests"].append({"path": str(manifest), "sha256": digest(manifest)})

    def error(exc):
        result["limitations"].append(str(exc))

    for root in dict.fromkeys(roots):
        if not tax.BULK.exists(root):
            result["searched_roots"][str(root)] = "ABSENT"
            continue
        result["searched_roots"][str(root)] = "SCANNED_LISTED_FILES"
        for directory, folders, files in os.walk(root, onerror=error):
            folders.sort()
            for name in sorted(files):
                path = Path(directory) / name
                try:
                    if name in {"taxonomy-packages.json", "packages.json"}:
                        task9.bulk.plain(path)
                        result["package_manifests"].append(
                            {"path": str(path), "sha256": digest(path)}
                        )
                    if name in names:
                        task9.bulk.plain(path)
                        result["candidates"].append(
                            {
                                "path": str(path),
                                "sha256": digest(path),
                                "kind": "LOOSE",
                                "status": "REQUIRES_EXACT_URI_PROVENANCE",
                            }
                        )
                    is_bootstrap = name == "content" and root.name == "bootstrap-packages"
                    if not (is_bootstrap or path.suffix.lower() == ".zip"):
                        continue
                    item = {"path": str(path), "kind": "ZIP"}
                    result["candidates"].append(item)
                    size = task9.bulk.plain(path).st_size
                    item["bytes"] = size
                    if size > tax.MAX_ARCHIVE:
                        item["status"] = "EXCEEDS_ARCHIVE_INVENTORY_BOUND"
                        continue
                    item["sha256"] = digest(path)
                    if is_bootstrap:
                        metadata_path = path.with_name("source.json")
                        raw, metadata = task9.checked(metadata_path)
                        require(
                            metadata["bytes"] == size and metadata["sha256"] == item["sha256"],
                            "BOOTSTRAP_ARCHIVE_BINDING_MISMATCH",
                        )
                        item.update(source=metadata, source_metadata_sha256=task9.sha(raw))
                    with zipfile.ZipFile(path) as archive:
                        members = tax.archive_members(archive)
                        item["matching_member_paths"] = sorted(
                            member for member in members if member.rsplit("/", 1)[-1] in names
                        )
                        item["package"], _ = tax.package_catalog(archive, members)
                    item["status"] = (
                        "REVIEW_REQUIRED" if item["matching_member_paths"] else "NO_MATCHING_MEMBER"
                    )
                except (*METADATA_EXCEPTIONS, zipfile.BadZipFile, tax.ET.ParseError) as exc:
                    error(exc)
    return result


def older_evidence(plan):
    try:
        parent = plan.inputs.json(PARENT / "manifest.json")
        jobs, _, shards, dts = published_catalog(plan, parent)
        checked_diagnostics = {
            path: evidence
            for path, evidence in plan.inputs.files.items()
            if "taxonomy-" in path or "taxonomy-extended-coverage-" in path
        }
        matches = {}
        for path, evidence in checked_diagnostics.items():
            _, report = task9.checked(Path(path), evidence["sha256"])
            maps = [report["urls"]] if "urls" in report else report["filings"].values()
            matches[path] = sorted({uri for urls in maps for uri in urls if uri in tax.REQUIRED})
        reader = plan.inputs.json(READER, READER_SHA)
        scope = reader["bindings"]
        require(
            reader["status"] == "PARITY_VERIFIED"
            and not reader["findings"]
            and scope["original_filing_manifest_sha256"] == handoff.FILING_SHA,
            "E13_READER_EVIDENCE_UNBOUND",
        )
        review = ARTIFACTS / "f01-accounting-e11-reviewed-20261122-195121.json"
        plan.inputs.json(review, scope["e11_review_sha256"])
        return {
            "status": "BOUND_CATALOG_CHECKED",
            "task9_jobs": [str(job) for job in jobs],
            "shards_in_catalog": len(shards),
            "shards_with_matching_local_dts": sum(
                row["status"] == "LOCAL_BOUND_DEPENDENCIES_MATCH" for row in dts.values()
            ),
            "taxonomy_evidence": checked_diagnostics,
            "required_uri_matches": matches,
            "e11_e13_manifest_bindings": {
                str(READER): READER_SHA,
                str(review): scope["e11_review_sha256"],
                "original_filing_manifest_sha256": handoff.FILING_SHA,
            },
        }
    except METADATA_EXCEPTIONS as exc:
        return {"status": "CATALOG_UNVERIFIED", "reason": str(exc)}


def pilot_data(plan):
    provider, block = plan.blocks[PILOT]
    require(provider == "SEC", "PILOT_PLAN_PROVIDER_CHANGED")
    rows = [row for row in block["accessions"] if "/".join(key(row)) in DIAGNOSES]
    require(len(rows) == len(DIAGNOSES), "PILOT_PLAN_FILINGS_CHANGED")
    resolved, blocked = plan.metadata(rows)
    job = JOBS / PILOT
    receipt_path = job / "result.json"
    raw, receipt = task9.checked(receipt_path)
    scope_raw, scope = task9.checked(job / "scope.json")
    budget_raw, budget = task9.checked(job / "request-budget.json")
    require(
        scope["block_id"] == PILOT
        and scope["parent"] == plan.binding()
        and receipt["status"] == "SEC_BLOCK_COMPLETE"
        and receipt["binding"] == budget["binding"]
        and receipt["binding"]["scope"]
        == scope["authorization_draft"]["scope"]
        == fingerprint({k: v for k, v in scope.items() if k != "authorization_draft"})
        and set(receipt["verified_filings"]) == DIAGNOSES.keys(),
        "PILOT_RECEIPT_SCOPE_CHANGED",
    )
    pilot = {
        "receipt_sha256": task9.sha(raw),
        "scope_sha256": task9.sha(scope_raw),
        "budget_sha256": task9.sha(budget_raw),
        "requests": budget["requests"],
        "received_bytes": budget["received_bytes"],
        "bytes_archived": receipt["bytes_archived"],
        "filings": receipt["verified_filings"],
    }
    originals = {}
    for row in rows:
        pair = key(row)
        filing_id = "/".join(pair)
        item = pilot["filings"][filing_id]
        path = job.joinpath("originals", *pair)
        require(Path(item["directory"]) == path, "PILOT_ORIGINAL_DIRECTORY_CHANGED")
        try:
            require(pair in resolved, blocked.get(pair, "PILOT_FILING_METADATA_MISSING"))
            source = checked_original(path, resolved[pair], item["documents"])
        except METADATA_EXCEPTIONS as exc:
            item["recheck"] = {"status": "ORIGINAL_RECHECK_BLOCKED", "reason": str(exc)}
        else:
            originals[pair] = {"status": "ORIGINAL_VERIFIED", "source": source}
            item["recheck"] = {"status": "ORIGINAL_VERIFIED"}
    return pilot, [resolved[pair] for pair in originals], originals


def pilot_audit(plan):
    root = tax.safe_root()
    task9.filing.safe_directory(root / "objects")
    resolver = tax.Resolver(root)
    inventory = inspect_local(resolver)
    old = older_evidence(plan)
    pilot, rows, originals = pilot_data(plan)
    observed = {
        "/".join(key(row)): original_references(row, originals[key(row)]["source"]) for row in rows
    }
    processing = progress_for(PILOT, rows, originals, process=True) if rows else {"filings": []}
    current = {"/".join(key(row)): row for row in processing["filings"]}
    required, closure, gaps, reuse = {}, {}, {}, {}
    for filing_id, (name, sha) in DIAGNOSES.items():
        _, historical = task9.checked(OLD / name, sha)
        prior = historical["filings"][filing_id]
        required[filing_id] = {
            uri: {
                "namespace_to_verify": tax.REQUIRED[uri],
                "original_import_namespaces": sorted(observed.get(filing_id, {}).get(uri, ())),
                "diagnostic_status": prior[uri]["status"],
            }
            for uri in sorted(prior.keys() & tax.REQUIRED.keys())
        }
        item = current.get(
            filing_id,
            {
                "status": "DTS_INCOMPLETE",
                "extraction_status": "NOT_RUN_ORIGINAL_RECHECK_BLOCKED",
                "original_recheck": pilot["filings"][filing_id]["recheck"],
            },
        )
        current[filing_id] = item
        if item.get("closure_path"):
            _, report = task9.checked(Path(item["closure_path"]), item["closure_sha256"])
            dependencies = report["filings"][filing_id]
            basis = "REVALIDATED_ORIGINALS"
        else:
            dependencies = resolver.closure({uri: set() for uri in prior})
            basis = "ARCHIVED_DIAGNOSTIC_SEEDS"
        gaps[filing_id] = {
            uri: data for uri, data in dependencies.items() if data["status"] != "BOUND"
        }
        reuse[filing_id] = {
            uri: data["source"] for uri, data in dependencies.items() if data["status"] == "BOUND"
        }
        closure[filing_id] = {
            "basis": basis,
            "archived_diagnostic_path": str(OLD / name),
            "archived_diagnostic_sha256": sha,
            "archived_diagnostic_originals_sha256": historical["source_sha256"],
            "status": "TAXONOMY_PENDING"
            if gaps[filing_id] or basis != "REVALIDATED_ORIGINALS"
            else report["status"],
            "dependencies": dependencies,
        }
    return publish_audit(
        {
            "local_taxonomy_inventory.json": inventory | {"older_evidence": old},
            "required_2012_taxonomies.json": required,
            "taxonomy_reuse_report.json": {"filings": reuse, "older_evidence": old},
            "taxonomy_acquisition_plan.json": acquisition_plan(
                {uri for group in gaps.values() for uri in group}
            ),
            "dts_closure_cost_orcl.json": closure,
            "arelle_reprocessing_report.json": {
                "pilot_receipt": pilot,
                "filings": current,
                "required_runtime": task9.filing.ARELLE_VERSION,
                "canonical_import_authorized": False,
                "pit_trading_authorized": False,
            },
            "remaining_taxonomy_gaps.json": {
                "filings": gaps,
                "follow_on_dependencies": "OPEN_UNTIL_MISSING_SCHEMAS_ARE_AVAILABLE"
                if any(gaps.values())
                else "SEE_DTS_AND_EXTRACTION_REPORTS",
            },
        },
        plan.binding(),
    )


def publish_audit(documents, plan_binding):
    runbook = (REPO / "docs/f01_task961_taxonomy_resolution_runbook.md").read_bytes()
    with (
        task9.stage_lock(ARTIFACTS / ".task961-taxonomy-report.lock"),
        TemporaryDirectory(dir=ARTIFACTS, prefix=".task961-") as tmp,
    ):
        stage = Path(tmp) / "report"
        stage.mkdir()
        hashes = {name: publish(stage / name, value) for name, value in documents.items()}
        plan = documents["taxonomy_acquisition_plan.json"]
        for name, scope in plan["blocks"].items():
            file = f"acquire-{name}.json"
            hashes[file] = publish(stage / file, scope)
        task9.bulk.write_new(stage / "runbook.md", runbook)
        hashes["runbook.md"] = task9.sha(runbook)
        manifest = {
            "contract": "F01_TASK961_TAXONOMY_RESOLUTION_V1",
            "plan_binding": plan_binding,
            "code_fingerprint": current_code(),
            "outputs": hashes,
            "network_requests": 0,
        }
        manifest_sha = publish(stage / "manifest.json", manifest)
        target = OUTPUT
        if target.exists():
            task9.bulk.directory(target)
            _, old = task9.checked(target / "manifest.json")
            if old != manifest:
                target = target / manifest_sha
        if target.exists():
            task9.checked(target / "manifest.json", manifest_sha)
            for name, sha in hashes.items():
                require(digest(target / name) == sha, "TAXONOMY_REPORT_OUTPUT_CHANGED")
        else:
            stage.rename(target)
    return {
        "directory": str(target),
        "manifest_sha256": manifest_sha,
        "filings": documents["arelle_reprocessing_report.json"]["filings"],
    }
