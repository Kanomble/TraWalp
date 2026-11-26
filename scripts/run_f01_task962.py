#!/usr/bin/env python3
"""Complete the two archived 2012 filings with one bounded taxonomy invocation."""

import argparse
import http.client
import io
import os
import re
import shutil
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urldefrag, urlsplit
from uuid import uuid4

from run_f01_historical_acquisition import Plan, encoding, fingerprint, key, publish, require, task9

# The Task-9.6 runner initializes the existing script and report imports.
# isort: split
import f01_task96_taxonomy as tax
from f01_task96_contract import ARTIFACTS, TASK962_OUTPUT
from f01_task96_network import Budget, replace_checkpoint
from f01_task96_process import original_references, progress_for
from f01_task96_taxonomy_acquisition import record_quarantine
from f01_task96_taxonomy_audit import DIAGNOSES, PILOT, pilot_data

CONTRACT = "F01_TASK962_COST_ORCL_FINAL_2012_V1"
PRIOR_SHA = "095e37465b3727a96a3080063c04cb5452a2565cde9fb502ddf320c6a880bbe9"
PRIOR = ARTIFACTS / "f01-task961-taxonomy-resolution-v1" / PRIOR_SHA
LIMITS = {"max_requests": 30, "max_bytes": 128 * tax.MIB, "requests_per_minute": 30}
FASB = "https://xbrl.fasb.org/us-gaap/2012/"
SEC = "https://xbrl.sec.gov/"
SEC_FAMILIES = ("country", "dei", "invest")
RELEASE_NOTES = SEC + "doc/2012/releasenotes-2012-01-31.pdf"
PACKAGES = {
    FASB + "us-gaap-2012-01-31.zip": ("us-gaap",),
    SEC + "2012.zip": SEC_FAMILIES,
    **{f"{SEC}{family}/2012/{family}-2012-01-31.zip": (family,) for family in SEC_FAMILIES},
}
ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    RuntimeError,
    ET.ParseError,
    zipfile.BadZipFile,
    http.client.HTTPException,
)


def family_for(uri):
    if not tax.TAX.parts(uri):
        return None
    endpoint = uri.replace("http://", "https://", 1)
    filename = urlsplit(uri).path.rsplit("/", 1)[-1]
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", filename)
    dates = re.findall(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)", filename)
    if "draft" in uri.lower() or set(years) - {"2012"} or set(dates) - {tax.RELEASE}:
        return None
    if endpoint.startswith(FASB):
        return "us-gaap"
    return next(
        (family for family in SEC_FAMILIES if endpoint.startswith(f"{SEC}{family}/2012/")), None
    )


def official_endpoint(url):
    return (
        url in PACKAGES
        or url.startswith("https://")
        and family_for(url)
        and not urlsplit(url).fragment
        and url.lower().endswith((".xsd", ".xml"))
    )


def load_inputs(plan):
    _, manifest = task9.checked(PRIOR / "manifest.json", PRIOR_SHA)
    require(manifest["plan_binding"] == plan.binding(), "TASK961_PLAN_BINDING_CHANGED")
    previous = {
        name: task9.checked(PRIOR / name, manifest["outputs"][name])[1]
        for name in (
            "required_2012_taxonomies.json",
            "arelle_reprocessing_report.json",
            "dts_closure_cost_orcl.json",
        )
    }
    pilot, rows, originals = pilot_data(plan)
    old = previous["arelle_reprocessing_report.json"]["pilot_receipt"]
    require(
        all(pilot[k] == old[k] for k in ("receipt_sha256", "scope_sha256", "budget_sha256")),
        "SEC_PILOT_RECEIPT_CHANGED",
    )
    require(
        len(rows) == len(DIAGNOSES),
        f"ORIGINAL_RECHECK_BLOCKED: { {k: v['recheck'] for k, v in pilot['filings'].items()} }",
    )
    imports = previous["required_2012_taxonomies.json"]
    require(set(imports) == DIAGNOSES.keys(), "TASK961_FILING_SCOPE_CHANGED")
    for row in rows:
        filing_id = "/".join(key(row))
        source = originals[key(row)]["source"]
        prior_dts = previous["dts_closure_cost_orcl.json"][filing_id]
        require(
            fingerprint(source) == prior_dts["archived_diagnostic_originals_sha256"],
            f"ORIGINAL_DTS_SOURCE_BINDING_CHANGED: {filing_id}",
        )
        discovered = original_references(row, source)
        require(
            set(discovered) & tax.REQUIRED.keys() == imports[filing_id].keys()
            and all(
                discovered[uri] == {tax.REQUIRED[uri]} == set(proof["original_import_namespaces"])
                for uri, proof in imports[filing_id].items()
            ),
            f"ORIGINAL_TAXONOMY_VERSION_CHANGED: {filing_id}",
        )
    return pilot, rows, originals


class RateLimiter(task9.PersistedSecOpener):
    def save(self, deadline, blocked=None):
        # This Task-9.6.2-only checkpoint uses a bounded cooldown for unknown Retry-After.
        now = self.clock()
        super().save(max(deadline, now + (60 if blocked else 2)))


class Downloads:
    def __init__(
        self,
        run,
        responses,
        limiter,
        binding,
        *,
        opener=None,
        budget=None,
        endpoint_check=official_endpoint,
        packages=PACKAGES,
        user_agent=None,
    ):
        self.run, self.responses = run, responses
        self.budget = budget or Budget(run / "budget.json", binding, LIMITS)
        self.opener = opener or RateLimiter(
            limiter, urllib.request.build_opener(task9.bulk.NoRedirect())
        )
        self.agent = (
            user_agent or os.getenv("SEC_USER_AGENT") or "TraWalp Historical XBRL Research/1.0"
        )
        self.hold = None
        self.endpoint_check = endpoint_check
        self.packages = packages

    def request(self, url, method, cap, consume):
        try:
            return self.attempt(url, method, cap, consume)
        except ValueError as exc:
            error = exc.__cause__
            if not isinstance(error, urllib.error.HTTPError) or error.code not in {
                500,
                502,
                503,
                504,
            }:
                raise
        return self.attempt(url, method, cap, consume)

    def attempt(self, url, method, cap, consume):
        require(self.endpoint_check(url), f"TAXONOMY_ENDPOINT_OUTSIDE_CONTRACT: {url}")
        require(not self.hold, self.hold or "PROVIDER_HOLD")
        self.budget.reserve(cap)
        journal = self.run / f"req-{self.budget.state['requests']:02}.json"
        event = {
            "url": url,
            "method": method,
            "utc": datetime.now(UTC).isoformat(),
            "cap_bytes": cap,
            "status": "IN_FLIGHT",
        }
        publish(journal, event)
        size = 0
        try:
            request = urllib.request.Request(
                url,
                method=method,
                headers={
                    "User-Agent": self.agent,
                    "Accept-Encoding": "identity",
                },
            )
            with closing(self.opener.open(request, timeout=30)) as response:
                require(
                    response.geturl() == url and response.status == 200,
                    f"TAXONOMY_RESPONSE_ENDPOINT_OR_STATUS_CHANGED: {url}",
                )
                if method == "GET":
                    require(
                        response.headers.get("Content-Encoding", "identity") == "identity",
                        "UNEXPECTED_TAXONOMY_CONTENT_ENCODING",
                    )
                    length = content_length(response)
                    require(length is None or 0 < length <= cap, "TAXONOMY_RESPONSE_TOO_LARGE")
                    data = response.read(length if length is not None else cap)
                    size = len(data)
                    require(
                        size > 0 and (size == length if length is not None else size < cap),
                        "TAXONOMY_RESPONSE_LENGTH_UNVERIFIED",
                    )
                else:
                    data = None
                result = consume(response, data)
                event.update(status=200)
                return result
        except urllib.error.HTTPError as exc:
            with closing(exc):
                event.update(status=exc.code, retry_after=exc.headers.get("Retry-After"))
            if exc.code == 429:
                self.hold = f"HTTP_429: {url}; Retry-After={event['retry_after']}"
            raise ValueError(f"HTTP_{exc.code}: {url}") from exc
        except ERRORS as exc:
            context = exc.__context__
            if isinstance(context, urllib.error.HTTPError) and context.code == 429:
                self.hold = f"HTTP_429_RETRY_AFTER_UNVERIFIED: {url}"
                event.update(http_status=429, retry_after=context.headers.get("Retry-After"))
                context.close()
            event.update(status="BLOCKED", reason=self.hold or str(exc))
            # A failed body read may have consumed bytes before raising.
            if method == "GET" and isinstance(exc, task9.bulk.TRANSPORT_ERRORS):
                size = cap
            raise
        finally:
            self.budget.finish(size, event["status"])
            replace_checkpoint(journal, event | {"payload_bytes_charged": size})

    def fetch(self, url):
        target = self.responses / fingerprint(url)[:24]
        binding = {"contract": CONTRACT, "url": url}
        if not target.exists():
            maximum = tax.MAX_ARCHIVE if url in self.packages else 16 * tax.MIB
            probe = self.request(
                url,
                "HEAD",
                0,
                lambda response, _: {"status": response.status, "bytes": content_length(response)},
            )
            cap = probe["bytes"] if probe["bytes"] is not None else maximum
            require(0 < cap <= maximum, f"TAXONOMY_RESOURCE_SIZE_LIMIT: {url}")
            require(
                shutil.disk_usage(self.responses).free >= 2 * cap + tax.MAX_EXPANDED,
                "INSUFFICIENT_TAXONOMY_STORAGE",
            )

            def save(response, data):
                receipt = binding | {
                    "sha256": task9.sha(data),
                    "bytes": len(data),
                    "head_probe": probe,
                    "retrieved_utc": datetime.now(UTC).isoformat(),
                    "http_headers": {
                        k: response.headers[k]
                        for k in ("ETag", "Last-Modified")
                        if k in response.headers
                    },
                }
                with TemporaryDirectory(dir=self.responses, prefix=".get-") as tmp:
                    stage = Path(tmp) / "response"
                    stage.mkdir()
                    task9.bulk.write_new(stage / "content", data)
                    publish(stage / "receipt.json", receipt)
                    stage.rename(target)

            self.request(url, "GET", cap, save)
        _, receipt = task9.checked(target / "receipt.json")
        require(
            all(receipt.get(k) == v for k, v in binding.items()),
            f"TAXONOMY_RESPONSE_RECEIPT_MISMATCH: {url}",
        )
        content = tax.check_bytes(target / "content", receipt["sha256"], receipt["bytes"])
        return content, receipt


def content_length(response):
    values = response.headers.get_all("Content-Length") or []
    require(
        len(values) <= 1 and (not values or re.fullmatch(r"[0-9]{1,12}", values[0])),
        "TAXONOMY_RESPONSE_CONTENT_LENGTH_INVALID",
    )
    return int(values[0]) if values else None


def legacy_member(uri, family, members, single_family, *, release=tax.RELEASE):
    path = urlsplit(uri).path.lstrip("/")
    _, year, relative = path.split("/", 2)
    version = f"{family}-{release}"
    paths = {path, f"{year}/{path}", f"{version}/{relative}", f"{version}/{path}"}
    paths.add(f"{family}/{relative}")
    if single_family:
        paths.update({relative, f"{family}-{year}/{relative}"})
    names = paths & members.keys()
    require(len(names) <= 1, f"AMBIGUOUS_LEGACY_TAXONOMY_PATH: {uri}")
    return next(iter(names), None)


def package_entries(raw, url, gaps):
    """Find exact package members and recursively bind intra-release DTS references."""
    return bind_release_archive(raw, url, gaps, PACKAGES[url], family_for, tax.RELEASE)


def bind_release_archive(raw, url, gaps, families, family_lookup, release):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = tax.archive_members(archive)
        package, rules = tax.package_catalog(archive, members)
        years = re.findall(r"\b(?:19|20)\d{2}\b", package.get("version") or "")
        require(not years or set(years) == {release[:4]}, "PACKAGE_VERSION_MISMATCH")
        reviewed, seen = {}, set()
        expected = {uri: set(namespaces) for uri, namespaces in gaps.items()}
        pending = set(gaps)
        while pending:
            uri = min(pending)
            pending.remove(uri)
            family = family_lookup(uri)
            if uri in seen or family not in families or not uri.endswith((".xsd", ".xml")):
                continue
            seen.add(uri)
            require(len(seen) <= 1000, "RELEASE_DEPENDENCY_LIMIT")
            member = tax.member_for(uri, rules)
            if not member and package["kind"] == "LEGACY_ZIP":
                member = legacy_member(uri, family, members, len(families) == 1, release=release)
            if not member:
                continue
            data = tax.read_member(archive, members, member)
            reviewed[uri] = {
                "member": member,
                "sha256": task9.sha(data),
                "namespace": tax.document_namespace(data, uri),
            }
            for dep, namespace in tax.references(data, uri):
                if namespace:
                    expected.setdefault(dep, set()).add(namespace)
                if dep not in seen:
                    pending.add(dep)
    require(gaps.keys() & reviewed.keys(), f"REQUIRED_RELEASE_MEMBERS_MISSING: {url}")
    for uri, entry in reviewed.items():
        check_namespace(uri, entry["namespace"], expected.get(uri, set()))
    return tax.bind_archive(raw, source=url, candidates=reviewed, explicit=reviewed)[1]


def check_namespace(uri, namespace, expected):
    require(not expected or expected == {namespace}, f"IMPORT_NAMESPACE_MISMATCH: {uri}")


def collect(processing, rows, *, detail=False):
    filings, accounting, gaps, blockers = {}, {}, {}, []
    metadata = {key(row): row for row in rows}
    for item in processing["filings"]:
        filing_id = "/".join(key(item))
        row = metadata[key(item)]
        entry = item | {"acceptance_utc": row["acceptance"], "symbol": row["symbol"]}
        dependencies = {}
        ready = False
        if item.get("closure_path"):
            _, report = task9.checked(Path(item["closure_path"]), item["closure_sha256"])
            dependencies = report["filings"][filing_id]
            ready = report["status"] == "TAXONOMY_CLOSURE_READY"
        complete = (
            ready
            and item.get("extraction_status") == "EXTRACTION_COMPLETE"
            and bool(dependencies)
            and all(value["status"] == "BOUND" for value in dependencies.values())
        )
        entry.update(
            dts_status="DTS_COMPLETE" if complete else "DTS_INCOMPLETE", taxonomy=dependencies
        )
        if complete and detail:
            shard_path = (
                Path(item["offline_evidence_path"]) / f"{item['accession']}-extraction.json"
            )
            _, shard = task9.checked(
                shard_path, item["shard_sha256"], limit=task9.filing.SHARD_LIMIT
            )
            require(
                shard["status"] == "EXTRACTION_COMPLETE"
                and shard["runtime"]["version"] == task9.filing.ARELLE_VERSION
                and len(shard["observations"]) == item["facts_extracted"],
                f"ARELLE_SHARD_BINDING_MISMATCH: {filing_id}",
            )
            used = {}
            for dependency in task9.actual_dependencies(shard["documents"]):
                uri, _ = urldefrag(dependency["uri"])
                if uri in dependencies:
                    source = dependencies[uri]["source"]
                    require(
                        source["sha256"] == dependency["sha256"], f"ACTUAL_DTS_HASH_MISMATCH: {uri}"
                    )
                    used[uri] = source
            require(used, f"ACTUAL_EXTERNAL_DTS_EMPTY: {filing_id}")
            entry["used_taxonomy"] = used
            _, proof = task9.checked(Path(item["accounting_path"]), item["accounting_sha256"])
            policy = proof["filings"][filing_id]
            accounting[filing_id] = policy | {"ruleset": proof["ruleset"]}
            entry["accounting_review_items"] = {
                name: result["reason"]
                for name, result in policy["accounting"].items()
                if not result["admitted"]
            }
        elif not complete:
            entry.setdefault("extraction_status", "NOT_COMPLETE")
            blocked = {
                uri: value for uri, value in dependencies.items() if value["status"] != "BOUND"
            }
            for uri, value in blocked.items():
                previous = gaps.get(uri, {})
                combined = sorted(
                    set(previous.get("import_namespaces", ()))
                    | set(value.get("import_namespaces", ()))
                )
                gaps[uri] = value | {"import_namespaces": combined}
                if previous and previous["status"] != "MISSING_EVIDENCE":
                    gaps[uri]["status"] = previous["status"]
            blockers.extend(
                {
                    "filing": filing_id,
                    "resource": uri,
                    "reason": value.get("reason", value["status"]),
                }
                for uri, value in blocked.items()
            )
            if not blocked:
                diagnostics = []
                if detail and item.get("offline_evidence_path"):
                    folder = Path(item["offline_evidence_path"])
                    for diagnostic in sorted(folder.glob(f"{item['accession']}-quarantine-*.json")):
                        raw, report = task9.checked(diagnostic, limit=task9.filing.SHARD_LIMIT)
                        sha = task9.sha(raw)
                        require(
                            diagnostic.stem.endswith(f"-quarantine-{sha[:12]}"),
                            f"ARELLE_DIAGNOSTIC_CHANGED: {diagnostic}",
                        )
                        diagnostics.append(
                            {"path": str(diagnostic), "sha256": sha, "issues": report["issues"]}
                        )
                blockers.append(
                    {
                        "filing": filing_id,
                        "resource": item.get("closure_path"),
                        "reason": item.get("reason", item["status"]),
                        "worker_diagnostics": diagnostics,
                    }
                )
        filings[filing_id] = entry
    return filings, accounting, gaps, blockers


def recover(rows, originals, root, cache, downloads):
    processing = progress_for(
        PILOT, rows, originals, process=True, taxonomy_cache=cache, shard_root=root / "dts"
    )
    attempted, unavailable, receipts, failures = set(), set(), {}, {}
    while not downloads.hold:
        _, _, gaps, _ = collect(processing, rows)
        pending = {
            uri
            for uri, gap in gaps.items()
            if gap["status"] == "MISSING_EVIDENCE"
            and official_endpoint(uri.replace("http://", "https://", 1))
        }
        sources = {
            url: {uri for uri in pending if family_for(uri) in families}
            for url, families in PACKAGES.items()
        }
        for uri in sorted(pending):
            sources.setdefault(uri.replace("http://", "https://", 1), set()).add(uri)
        options = [
            (url, uris)
            for url, uris in sources.items()
            if uris and url not in unavailable and (url, frozenset(uris)) not in attempted
        ]
        if not options:
            break
        url, uris = options[0]
        attempted.add((url, frozenset(uris)))
        try:
            raw, receipt = downloads.fetch(url)
            receipts[url] = receipt
            expected = {uri: set(gaps[uri].get("import_namespaces", ())) for uri in uris}
            if url in PACKAGES:
                entries = package_entries(raw, url, expected)
            else:
                if url.lower().endswith(".xml"):
                    require(tax.xml(raw).tag == tax.LINK + "linkbase", "EXPECTED_TAXONOMY_LINKBASE")
                entries = {uri: tax.taxonomy_entry(raw, uri, {"source_url": url}) for uri in uris}
                for uri, (_, meta) in entries.items():
                    check_namespace(uri, meta["namespace"], expected[uri])
            tax.store(cache, raw, url, entries, receipt)
        except ERRORS as exc:
            failures[url] = {"resource": url, "affected": sorted(uris), "reason": str(exc)}
            if url in receipts:
                record_quarantine(cache, url, exc, receipts[url])
            else:
                unavailable.add(url)
        else:
            processing = progress_for(
                PILOT, rows, originals, process=True, taxonomy_cache=cache, shard_root=root / "dts"
            )
    filings, accounting, _, blockers = collect(processing, rows, detail=True)
    if blockers:
        blockers.extend(failures.values())
    return processing, filings, accounting, blockers, receipts, failures


def execute(root, run):
    binding = {
        "contract": CONTRACT,
        "task961_manifest_sha256": PRIOR_SHA,
        "limits": LIMITS,
        "entrypoint_sha256": task9.sha(Path(__file__).read_bytes()),
        "package_sources": PACKAGES,
        "sec_release_notes": RELEASE_NOTES,
        "filings": sorted(DIAGNOSES),
        "release": tax.RELEASE,
        "authorization": "EXPLICIT_TASK962_ENTRYPOINT_INVOCATION",
    }
    outputs = {"execution_contract.json": publish(run / "execution_contract.json", binding)}
    downloads = None
    try:
        pilot, rows, originals = load_inputs(Plan())
        cache = tax.safe_root()
        responses = task9.filing.safe_directory(root / "responses")
        with task9.stage_lock(cache / ".network.lock"):
            downloads = Downloads(run, responses, root / "rate-state.json", binding)
            processing, filings, accounting, blockers, receipts, failures = recover(
                rows, originals, root, cache, downloads
            )
        name = f"processing-{fingerprint(processing)}.json"
        sha = publish(root / name, processing)
        documents = {
            "sec_pilot_receipt_binding.json": pilot,
            "filing_results.json": filings,
            "accounting_candidates.json": accounting,
            "open_blockers.json": blockers,
            "download_receipts.json": receipts,
            "request_summary.json": {"budget": downloads.budget.state, "source_failures": failures},
            "task96_report_binding.json": {"path": str(root / name), "sha256": sha},
        }
        status = "PARTIAL_BLOCKED" if blockers or len(filings) != len(DIAGNOSES) else "COMPLETE"
    except ERRORS as exc:
        status, filings = "FAILED", {}
        blockers = [{"reason": str(exc)}]
        documents = {"open_blockers.json": blockers}
        if downloads:
            documents["request_summary.json"] = {"budget": downloads.budget.state}
    outputs.update({name: publish(run / name, doc) for name, doc in documents.items()})
    for path in run.glob("req-*.json"):
        outputs[path.name] = task9.sha(path.read_bytes())
    summary = {
        "contract": CONTRACT,
        "status": status,
        "outputs": outputs,
        "canonical_import_authorized": False,
        "pit_trading_authorized": False,
    }
    sha = publish(run / "manifest.json", summary)
    result = {
        "status": status,
        "manifest": str(run / "manifest.json"),
        "manifest_sha256": sha,
        "filings": {
            key: {
                "dts": item["dts_status"],
                "extraction": item["extraction_status"],
                "facts": item.get("facts_extracted"),
            }
            for key, item in filings.items()
        },
        "blockers": blockers,
    }
    replace_checkpoint(root / "latest.json", result)
    return result


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        root = task9.filing.safe_directory(TASK962_OUTPUT)
        with task9.stage_lock(root / ".run.lock"):
            run = task9.filing.safe_directory(root / f"run-{uuid4().hex[:16]}")
            result = execute(root, run)
    except ERRORS as exc:
        result = {"status": "FAILED", "blockers": [{"reason": str(exc)}]}
    print(encoding(result).decode(), end="")
    return {"COMPLETE": 0, "PARTIAL_BLOCKED": 2, "FAILED": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
