"""Historical source policy for the shared Task-9.6.2 taxonomy transport and binder."""

import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import f01_task96_taxonomy as tax
from f01_task96_taxonomy_acquisition import record_quarantine
from run_f01_task962 import ERRORS, Downloads, bind_release_archive, check_namespace

task9 = tax.task9
FASB = {"us-gaap", "srt"}
SEC = {"country", "currency", "dei", "exch", "invest", "naics", "rr", "sic", "stpr"}


def edition(uri):
    """Only exact published URI layouts, excluding drafts and alternate editions."""
    parts = tax.TAX.parts(uri)
    if not parts or len(parts) < 5 or "draft" in uri.lower() or urlsplit(uri).fragment:
        return None
    _, host, family, year, *tail = parts
    version = r"20(?:0[9]|1[0-9]|2[0-6])"
    if host == "xbrl.sec.gov":
        version += r"(?:q[1-4])?"
    if not re.fullmatch(version, year):
        return None
    if host == "xbrl.fasb.org" and family in FASB or host == "xbrl.sec.gov" and family in SEC:
        dates = re.findall(r"(?<!\d)(20\d{2})-\d{2}-\d{2}(?!\d)", tail[-1])
        if not set(dates) - {year[:4]} and tail[-1].lower().endswith((".xml", ".xsd")):
            return host, family, year
    return None


def package_options(uri, namespaces):
    published = edition(uri)
    if not published:
        return {}
    host, family, year = published
    named = re.search(rf"{year}-\d{{2}}-\d{{2}}", urlsplit(uri).path.rsplit("/", 1)[-1])
    dates = {ns.rstrip("/").rsplit("/", 1)[-1] for ns in namespaces}
    release = (
        named[0]
        if named
        else next(
            (d for d in sorted(dates) if re.fullmatch(rf"{year}-\d{{2}}-\d{{2}}", d)),
            year,
        )
    )
    if host == "xbrl.sec.gov":
        return {
            f"https://{host}/{year}.zip": (SEC, year),
            f"https://{host}/{family}/{year}/{family}-{release}.zip": ({family}, release),
        }
    url = tax.TAX.package_url(uri) or f"https://{host}/{family}/{year}/{family}-{release}.zip"
    return {url: ({family}, release)}


def missing_sources(processing):
    gaps = defaultdict(set)
    rejected = set()
    for row in processing["filings"]:
        if not row.get("closure_path"):
            continue
        _, closure = task9.checked(Path(row["closure_path"]), row["closure_sha256"])
        for items in closure["filings"].values():
            for uri, item in items.items():
                if item["status"] == "MISSING_EVIDENCE" and edition(uri):
                    gaps[uri].update(item.get("import_namespaces", ()))
                elif item["status"] not in {"BOUND", "MISSING_EVIDENCE"}:
                    rejected.add(uri)
    return {uri: ns for uri, ns in gaps.items() if uri not in rejected}


class TaxonomyBatch:
    """Reuse exact package bytes and retry new gaps without rescanning the global cohort."""

    def __init__(self, run, responses, cache, budget, opener, agent, denied):
        self.packages = {}
        self.allowed = set()
        self.transport = Downloads(
            run,
            responses,
            None,
            None,
            opener=opener,
            budget=budget,
            endpoint_check=self.allowed.__contains__,
            packages=self.packages,
            user_agent=agent,
        )
        self.cache = cache
        self.denied = denied
        self.attempted = set()
        self.unavailable = set()
        self.events = []

    def bind(self, processing):
        gaps = missing_sources(processing)
        scopes = defaultdict(dict)
        for uri, expected in sorted(gaps.items()):
            for endpoint, profile in package_options(uri, expected).items():
                self.packages.setdefault(endpoint, profile)
                scopes[endpoint][uri] = expected
        for uri, expected in sorted(gaps.items()):
            scopes[uri.replace("http://", "https://", 1)][uri] = expected
        new = 0
        self.allowed.update(scopes)
        for url, proposed in scopes.items():
            host = urlsplit(url).netloc
            wanted = {uri: ns for uri, ns in proposed.items() if uri in gaps}
            attempt = (url, frozenset(wanted))
            if not wanted or host in self.denied or url in self.unavailable:
                continue
            if attempt in self.attempted or self.transport.hold:
                continue
            self.attempted.add(attempt)
            receipt = None
            try:
                raw, receipt = self.transport.fetch(url)
                entries = self.entries(url, raw, wanted)
                tax.store(self.cache, raw, url, entries, receipt)
            except ERRORS as exc:
                reason = str(exc)
                self.events.append(
                    {"url": url, "uris": sorted(wanted), "reason": reason, "receipt": receipt}
                )
                if receipt:
                    record_quarantine(self.cache, url, exc, receipt)
                else:
                    self.unavailable.add(url)
                if "HTTP_403" in reason or "HTTP_401" in reason:
                    self.denied.add(host)
                if self.transport.hold or any(
                    term in reason
                    for term in (
                        "BUDGET_EXHAUSTED",
                        "CLOCK_ANOMALY",
                        "LIMITER_MANUAL_REVIEW",
                        "STORAGE",
                    )
                ):
                    raise
            else:
                self.events.append({"url": url, "uris": sorted(entries), "receipt": receipt})
                for uri in entries:
                    gaps.pop(uri, None)
                new += len(entries)
        return new

    def entries(self, url, raw, wanted):
        if url in self.packages:
            families, release = self.packages[url]
            host = urlsplit(url).netloc
            version = release if re.fullmatch(r"20\d{2}q[1-4]", release) else release[:4]

            def family_lookup(uri):
                candidate = edition(uri)
                if candidate and candidate[0] == host and candidate[2] == version:
                    return candidate[1]
                return None

            entries = bind_release_archive(raw, url, wanted, families, family_lookup, release)
            for uri, (data, meta) in entries.items():
                validate_member(uri, data, meta)
            return entries
        if url.lower().endswith(".xml"):
            task9.require(tax.xml(raw).tag == tax.LINK + "linkbase", "EXPECTED_TAXONOMY_LINKBASE")
        entries = {}
        for uri, namespace in wanted.items():
            entry = tax.taxonomy_entry(raw, uri, {"source_url": url})
            check_namespace(uri, entry[1]["namespace"], namespace)
            validate_member(uri, raw, entry[1])
            entries[uri] = entry
        return entries


def validate_member(uri, data, meta):
    _, family, year = edition(uri)
    filename = Path(urlsplit(uri).path).stem
    root = re.fullmatch(rf"({family}|us-types)-({year}(?:-\d{{2}}-\d{{2}})?)", filename)
    if uri.lower().endswith(".xml"):
        task9.require(tax.xml(data).tag == tax.LINK + "linkbase", "EXPECTED_TAXONOMY_LINKBASE")
    if root:
        host = "fasb.org" if family in FASB else "xbrl.sec.gov"
        namespace = meta.get("namespace") or ""
        expected = f"{host}/{root[1]}/{root[2]}"
        task9.require(
            namespace.removeprefix("http://").removeprefix("https://") == expected,
            f"TAXONOMY_RELEASE_NAMESPACE_MISMATCH: {uri}",
        )
