"""Separate, SHA-bound acquisition and local registration of taxonomy sources."""

import io
import re
import shutil
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import f01_task96_taxonomy as tax
from f01_task96_contract import authorization, publish, require
from f01_task96_network import Budget, CountedResponse

from trading_system.data.canonical_facts import fingerprint

task9 = tax.task9
CONTRACT = "F01_TASK961_TAXONOMY_ACQUISITION_V1"
FASB = "https://xbrl.fasb.org/us-gaap/2012/"
SEC = "https://xbrl.sec.gov/"
REFERENCES = {
    "fasb": "https://xbrl.us/data-rule/taxonomy/2012-us-gaap-financial-reporting-taxonomy/",
    "sec": "https://www.sec.gov/structureddata/osdinteractivedata",
    "sec_release_notes": "https://www.sec.gov/info/edgar/edgartaxonomies.shtml",
}
SOURCES = {
    "fasb-2012-zip": (FASB + "us-gaap-2012-01-31.zip", FASB, "ZIP"),
    "sec-2012-zip": (SEC + "2012.zip", SEC, "ZIP"),
    **{Path(uri).stem: (uri.replace("http://", "https://", 1), "", "XSD") for uri in tax.REQUIRED},
}


def acquisition_plan(gaps):
    blocks = {}
    for source_id, (url, prefix, kind) in SOURCES.items():
        uris = sorted(
            uri
            for uri in gaps & tax.REQUIRED.keys()
            if (transport := uri.replace("http://", "https://", 1)) == url
            or (prefix and transport.startswith(prefix))
        )
        if not uris:
            continue
        cap = tax.MAX_ARCHIVE if kind == "ZIP" else 16 * tax.MIB
        scope = {
            "contract": CONTRACT,
            "source_id": source_id,
            "url": url,
            "host": urlsplit(url).netloc,
            "kind": kind,
            "release": tax.RELEASE,
            "required": {uri: tax.REQUIRED[uri] for uri in uris},
            "observed_bytes": None,
            "response_cap_bytes": cap,
            "free_space_required_bytes": 6 * cap + tax.MAX_EXPANDED,
            "status": "AWAITING_TAXONOMY_AUTHORIZATION",
            "endpoint_status": "ZIP_LOCATION_REQUIRES_CONFIRMATION"
            if kind == "ZIP"
            else "HTTPS_TRANSPORT_OF_EXACT_FILING_URI",
        }
        blocks[source_id] = scope | {
            "authorization_draft": {
                "contract": "F01_TASK96_SINGLE_BLOCK_V1",
                "scope": fingerprint(scope),
                "decision": "PENDING",
                "authorized_by": None,
                "authorized_at": None,
                "provider_terms_evidence": None,
                "retention_rights_evidence": None,
                "backup_restore_evidence": None,
                "limits": {"max_requests": 3, "max_bytes": 3 * cap, "requests_per_minute": 30},
            }
        }
    manual = {}
    for uri in sorted(gaps - tax.REQUIRED.keys()):
        parts = tax.TAX.parts(uri)
        preferred = tax.TAX.package_url(uri) if parts else None
        host = urlsplit(uri).netloc
        manual.setdefault(preferred or host or "unresolved", []).append(uri)
    return {
        "contract": CONTRACT,
        "official_reference_candidates": REFERENCES,
        "size_status": "UNMEASURED_NO_NETWORK",
        "network_authorized": False,
        "blocks": blocks,
        "manual_source_review_required": manual,
    }


def legacy_paths(raw, prefix, candidates):
    """Only recognize full version-specific paths from the declared release layout."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = tax.archive_members(archive)
        for uri in sorted(candidates):
            url = urlsplit(uri)
            full = url.path.lstrip("/")
            if prefix == FASB:
                allowed = {full, f"us-gaap-{tax.RELEASE}/" + full.removeprefix("us-gaap/2012/")}
            else:
                allowed = {full, f"2012/{full}"}
            names = members.keys() & allowed
            require(len(names) <= 1, f"AMBIGUOUS_LEGACY_TAXONOMY_PATH: {uri}")
            if names:
                name = names.pop()
                data = tax.read_member(archive, members, name)
                yield (
                    uri,
                    {
                        "member": name,
                        "sha256": task9.sha(data),
                        "namespace": tax.document_namespace(data, uri),
                    },
                )


def release_entries(raw, scope):
    uri = next(iter(scope["required"]))
    source = scope["url"]
    if scope["kind"] == "XSD":
        return {uri: tax.taxonomy_entry(raw, uri, {"source_url": source})}
    prefix = SOURCES[scope["source_id"]][1]
    # Resolve intra-release imports and linkbases from the same source in one pass.
    pending = set(scope["required"])
    entries = {}
    while pending:
        require(len(entries) + len(pending) <= 1000, "RELEASE_DEPENDENCY_LIMIT")
        explicit = dict(legacy_paths(raw, prefix, pending))
        package, bound = tax.bind_archive(raw, source=source, candidates=pending, explicit=explicit)
        package_years = re.findall(r"\b(?:19|20)\d{2}\b", package.get("version") or "")
        require(not package_years or set(package_years) == {"2012"}, "PACKAGE_VERSION_MISMATCH")
        require(
            not (pending & scope["required"].keys() - bound.keys()),
            "REQUIRED_RELEASE_MEMBERS_MISSING",
        )
        entries.update(bound)
        missing = pending - bound.keys()
        pending = (
            {
                dep
                for uri, (data, _) in bound.items()
                if not uri.endswith(".dtd")
                for dep, _ in tax.references(data, uri)
                if tax.TAX.parts(dep)
                and (
                    prefix == FASB
                    and dep.replace("http://", "https://", 1).startswith(FASB)
                    or prefix == SEC
                    and urlsplit(dep).netloc == "xbrl.sec.gov"
                    and "/2012/" in urlsplit(dep).path
                )
            }
            - entries.keys()
            - missing
        )
    return entries


def register(archive, manifest, expected, *, root=tax.CACHE):
    """Register a locally staged official archive with independent source/member evidence."""
    _, binding = task9.checked(manifest, expected)
    source = binding.get("source_url")
    root = tax.safe_root(root)
    try:
        require(
            bool(expected)
            and isinstance(binding.get("provenance"), str)
            and bool(binding["provenance"].strip())
            and binding.get("uris")
            and source,
            "REVIEWED_TAXONOMY_MANIFEST_REQUIRED",
        )
        require(
            urlsplit(source).netloc in {"xbrl.fasb.org", "xbrl.sec.gov", "xbrl.us", "www.sec.gov"},
            "UNRECOGNIZED_TAXONOMY_PUBLISHER",
        )
        raw = tax.check_bytes(archive.absolute(), binding["sha256"], binding["bytes"])
        _, entries = tax.bind_archive(
            raw,
            source=source,
            candidates=binding["uris"],
            explicit=binding["uris"],
        )
        return tax.store(
            root,
            raw,
            source,
            entries,
            {"review_manifest_sha256": expected, "provenance": binding["provenance"]},
        )
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        record_quarantine(root, source, exc, {"review_manifest_sha256": expected})
        raise


def record_quarantine(root, source, error, evidence):
    quarantine = task9.filing.safe_directory(root / "quarantine")
    record = {"source_url": source, "reason": str(error), "evidence": evidence}
    publish(quarantine / f"{fingerprint(record)}.json", record)


class TaxonomyOpener:
    def __init__(self, opener, budget, cap):
        self.opener, self.budget, self.cap = opener, budget, cap

    def open(self, request, *, timeout):
        self.budget.reserve(self.cap)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            self.budget.finish(0, error.code)
            raise
        except task9.bulk.TRANSPORT_ERRORS:
            self.budget.finish(0, "TRANSPORT_FAILED")
            raise
        return CountedResponse(response, self.budget)


def receipt_for(job, scope, auth, binding, user_agent, limiter):
    target = job / "response"
    if not target.exists():
        budget = Budget(job / "request-budget.json", binding, auth["limits"])
        client = task9.bulk.SecHttp(user_agent)
        client.opener = TaxonomyOpener(
            task9.PersistedSecOpener(limiter, client.opener),
            budget,
            scope["response_cap_bytes"],
        )
        with closing(client.open_url(scope["url"], "GET")) as response:
            require(
                response.status == 200
                and response.headers.get("Content-Encoding", "identity") == "identity",
                "UNEXPECTED_TAXONOMY_RESPONSE",
            )
            raw = response.read(scope["response_cap_bytes"] + 1)
            length = response.headers.get("Content-Length")
            require(
                raw and (length is None or length == str(len(raw))),
                "TAXONOMY_RESPONSE_LENGTH_MISMATCH",
            )
            headers = {
                k: response.headers[k] for k in ("ETag", "Last-Modified") if k in response.headers
            }
            receipt = binding | {
                "url": scope["url"],
                "sha256": task9.sha(raw),
                "bytes": len(raw),
                "retrieved_utc": datetime.now(UTC).isoformat(),
                "http_headers": headers,
            }
            # Publish the response before closing/finalizing the budget reservation.
            with TemporaryDirectory(dir=job, prefix=".response-") as temp:
                stage = Path(temp) / "response"
                stage.mkdir()
                task9.bulk.write_new(stage / "content", raw)
                publish(stage / "receipt.json", receipt)
                stage.rename(target)
    _, receipt = task9.checked(target / "receipt.json")
    require(
        all(receipt.get(k) == v for k, v in binding.items()) and receipt["url"] == scope["url"],
        "TAXONOMY_RESPONSE_RECEIPT_MISMATCH",
    )
    return tax.check_bytes(target / "content", receipt["sha256"], receipt["bytes"]), receipt


def acquire(path, sha, approval_path, approval_sha, user_agent, *, root=tax.CACHE):
    require(
        all((path, sha, approval_path, approval_sha, user_agent)),
        "TAXONOMY_APPROVAL_INPUTS_REQUIRED",
    )
    _, scope = task9.checked(path, sha)
    expected = acquisition_plan(set(scope["required"]))["blocks"].get(scope["source_id"])
    require(scope == expected, "TAXONOMY_ACQUISITION_SCOPE_CHANGED")
    auth = authorization(scope, approval_path, approval_sha)
    # Validate the agent before creating a checkpoint.
    task9.bulk.SecHttp(user_agent)
    root = tax.safe_root(root)
    with task9.stage_lock(root / ".network.lock"):
        resolver = tax.Resolver(root=root)
        if all(resolver.read(uri) for uri in scope["required"]):
            return {"status": "TAXONOMY_ALREADY_BOUND"}
        jobs = task9.filing.safe_directory(root / "acquisition")
        job = task9.filing.safe_directory(jobs / auth["scope"])
        require(
            shutil.disk_usage(root).free >= scope["free_space_required_bytes"],
            "INSUFFICIENT_TAXONOMY_STORAGE",
        )
        binding = {"scope": auth["scope"], "authorization_sha256": approval_sha}
        publish(job / "scope.json", scope)
        raw, receipt = receipt_for(job, scope, auth, binding, user_agent, root / "rate-state.json")
        try:
            entries = release_entries(raw, scope)
            return tax.store(root, raw, scope["url"], entries, receipt)
        except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
            record_quarantine(root, scope["url"], exc, receipt)
            raise
