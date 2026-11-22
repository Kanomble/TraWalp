"""Prepare SEC filing evidence in an isolated document cache, without SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.dont_write_bytecode = True
import validate_f01_sec_bulk_pilot as qualification  # noqa: E402
from f01_arelle_adapter import EDGAR_COMMIT, SEC_PLUGIN_FILES  # noqa: E402
from f01_arelle_adapter import VERSION as ARELLE_VERSION  # noqa: E402

from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.xbrl_parser import TAG_ALIASES, parse_company_facts  # noqa: E402

pilot = qualification.pilot
bulk = pilot.bulk
require = bulk.require
ROOT = pilot.RESEARCH_ROOT / "sec-originals" / "filing-evidence"
ARELLE_ROOT = pilot.RESEARCH_ROOT / "arelle" / ARELLE_VERSION
DIAG = "f01-fundamental-gaps-readonly-20261121-220344.json"
DIAG_SHA = "43609781335d384931b7b3b44ad9bc3ed18d7aaf40b7ecb7b447adfa4c4a805f"
SEC = "https://www.sec.gov/Archives/edgar/data"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
CIK = re.compile(r"\d{10}", re.ASCII)
LIMIT = 64 * 1024**2
SHARD_LIMIT = 256 * 1024**2
RESERVE = 512 * 1024**2
XBRL = "http://www.xbrl.org/2003/instance"
ISO = "http://www.xbrl.org/2003/iso4217"
EXTENDED_INPUT = "F01_EXTENDED_FILING_CACHE_V1"
EXTENDED_DRAFT = "F01_EXTENDED_FILING_EVIDENCE_V1"
WARMUP_START = date(2015, 1, 1)
ACCOUNTING_ACCESSIONS = {
    "ACN": (
        "19-000395",
        "20-000431",
        "21-000297",
        "22-000370",
        "23-000403",
        "24-000108",
        "24-000352",
        "25-000222",
        "26-000014",
        "23-000221",
    ),
    "MSFT": ("0001564590-19-037549",),
    "CAT": ("19-000281", "20-000056", "20-000155"),
    "COST": ("19-000019", "19-000033", "20-000004", "20-000009"),
    "LLY": ("23-000284", "24-000065", "24-000245", "25-000132", "26-000013"),
}
FOCUS = {
    "ACN": r"debt|borrow|lease|sharesoutstanding|classofstock",
    "AAPL": r"debt|borrow|lease|commercialpaper",
    "MSFT": r"debt|borrow|lease|commercialpaper",
    "COST": r"netincome|profitloss|noncontrolling|stockholdersequity|"
    r"operatingactivities|discontinued",
    "CAT": r"operatingactivities|discontinued",
    "LLY": r"interestexpense|netincome|profitloss|operatingincome|operatingprofit|"
    r"propertyplant|capitalexpenditure|incometax|amortization",
}
INITIAL = {
    "ACN": ("0001467373-24-000196", "0001467373-26-000049", "0001467373-26-000032"),
    "AAPL": ("0000320193-24-000069",),
    "MSFT": ("0001564590-18-024893",),
    "COST": ("0000909832-21-000021", "0000909832-19-000003", "0000909832-19-000013"),
    "CAT": ("0000018230-19-000224", "0000018230-20-000214"),
    "LLY": (
        "0000059478-24-000118",
        "0000059478-24-000187",
        "0000059478-25-000067",
        "0000059478-25-000204",
        "0000059478-25-000254",
        "0000059478-26-000045",
        "0000059478-26-000081",
        "0000059478-26-000108",
    ),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def filing_base(row):
    cik, accession = row["cik"], row["accession"]
    require(
        isinstance(cik, str)
        and CIK.fullmatch(cik)
        and int(cik) > 0
        and isinstance(accession, str)
        and pilot.ACCESSION.fullmatch(accession),
        "INVALID_CIK_OR_ACCESSION",
    )
    return f"{SEC}/{int(cik)}/{accession.replace('-', '')}"


def selected_sources(trace):
    for name in ("debt", "prior_debt"):
        yield from trace.get(name, {}).get("selected", ())
    shares = trace.get("shares", {}).get("selected")
    if shares:
        yield shares
    for flow in trace.get("flows", {}).values():
        for quarter in flow["same_unit_last_four"]:
            yield from quarter["inputs"]


def inventory():
    diag = qualification.checked_metadata(qualification.ARTIFACTS / DIAG, DIAG_SHA)
    scope = pilot.pilot_spec()
    active = {item["cik"]: item["symbol"] for item in scope.active}
    rows = {
        (cik, acc): {
            "cik": cik,
            "symbol": symbol,
            "accession": acc,
            "tier": "INITIAL",
            "probes": [],
        }
        for cik, symbol in active.items()
        for acc in INITIAL.get(symbol, ())
    }
    require(len(rows) == 18, "INITIAL_SCOPE_MUST_CONTAIN_18_ACCESSIONS")
    probe_count = 0
    for symbol, case in diag["cases"].items():
        cik = case["cik"]
        require(active[cik] == symbol, "DIAGNOSIS_CIK_MISMATCH")
        for probe in case["probes"]:
            probe_count += 1
            for name in ("actual_legacy_reader_trace", "canonical_from_verified_members"):
                for fact in selected_sources(probe[name]):
                    if not fact["accession_number"] or fact["cik"] != cik:
                        continue
                    acc = fact["accession_number"]
                    record = rows.setdefault(
                        (cik, acc),
                        {
                            "cik": cik,
                            "symbol": symbol,
                            "accession": acc,
                            "tier": "ADDITIONAL_SELECTED_INPUT",
                            "probes": [],
                        },
                    )
                    marker = [probe["session"], name, fact["metric"]]
                    if marker not in record["probes"]:
                        record["probes"].append(marker)
    require(probe_count == 49, "DIAGNOSIS_PROBE_COUNT_MISMATCH")
    for (_, acc), row in rows.items():
        witness = diag["cases"][row["symbol"]]["filing_witnesses"].get(acc, {})
        primary_url = witness.get("primary_document_url")
        if primary_url and primary_url.rsplit("/", 1)[0] == filing_base(row):
            row["primary"] = primary_url.rsplit("/", 1)[1]
        row["filed"] = witness.get("filing_date")
        row["acceptance"] = witness.get("accepted")
        row["form"] = witness.get("form")
        row["index_url"] = f"{filing_base(row)}/{acc}-index.html"
    return sorted(rows.values(), key=lambda r: (r["tier"] != "INITIAL", r["cik"], r["accession"]))


def validate_rows(rows):
    seen = set()
    for row in rows:
        filing_base(row)
        key = row["cik"], row["accession"]
        require(
            key not in seen and isinstance(row["symbol"], str) and row["symbol"],
            "DUPLICATE_OR_INVALID_FILING",
        )
        seen.add(key)
        primary = row.get("primary")
        require(
            primary is None
            or (NAME.fullmatch(primary) and primary.lower().endswith((".htm", ".html"))),
            "INVALID_PRIMARY_DOCUMENT",
        )
        if row.get("filed"):
            date.fromisoformat(row["filed"])
        if row.get("acceptance"):
            require(
                datetime.fromisoformat(row["acceptance"]).utcoffset() is not None,
                "ACCEPTANCE_REQUIRES_TIMEZONE",
            )
    return rows


def safe_directory(path):
    if not path.exists():
        bulk.directory(path.parent)
        path.mkdir(exist_ok=True)
    return bulk.directory(path)


def cache_read(path, row, filename):
    bulk.directory(path)
    _, meta = bulk.read_metadata(path / "source.json")
    data_path = path / "content"
    size = bulk.plain(data_path).st_size
    require(
        meta["cik"] == row["cik"]
        and meta["accession"] == row["accession"]
        and meta["filename"] == filename
        and meta["url"] == f"{filing_base(row)}/{filename}"
        and 0 < size == meta["bytes"] <= LIMIT,
        f"CACHE_BINDING_MISMATCH: {path}",
    )
    data = data_path.read_bytes()
    require(len(data) == size and sha(data) == meta["sha256"], f"CACHE_SHA256_MISMATCH: {path}")
    return data, meta


def get_document(client, root, row, filename):
    require(NAME.fullmatch(filename) and ".." not in filename, "INVALID_SEC_DOCUMENT_NAME")
    filing = safe_directory(safe_directory(root / row["cik"]) / row["accession"])
    target = filing / filename
    if bulk.exists(target):
        return cache_read(target, row, filename)
    require(shutil.disk_usage(filing).free >= LIMIT + RESERVE, "FILING_CACHE_SPACE_REQUIRED")
    url = f"{filing_base(row)}/{filename}"
    stage = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=filing))
    digest = hashlib.sha256()
    started = datetime.now(UTC)
    size = 0
    with closing(client.open_url(url, "GET")) as response, (stage / "content").open("xb") as stream:
        length = response.headers.get("Content-Length")
        require(
            response.status == 200
            and response.headers.get("Content-Encoding", "identity") == "identity",
            "UNEXPECTED_SEC_RESPONSE",
        )
        require(
            length is None or (length.isdecimal() and 0 < int(length) <= LIMIT),
            "INVALID_CONTENT_LENGTH",
        )
        while chunk := response.read(bulk.BLOCK_SIZE):
            size += len(chunk)
            require(size <= LIMIT, "SEC_DOCUMENT_TOO_LARGE")
            stream.write(chunk)
            digest.update(chunk)
        require(size > 0 and (length is None or size == int(length)), "INCOMPLETE_SEC_DOCUMENT")
        stream.flush()
        os.fsync(stream.fileno())
    meta = {
        "cik": row["cik"],
        "accession": row["accession"],
        "filename": filename,
        "url": url,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "download_started": started.isoformat(),
        "download_completed": datetime.now(UTC).isoformat(),
        "filed": row.get("filed"),
        "acceptance": row.get("acceptance"),
    }
    bulk.write_new(stage / "source.json", bulk.json_bytes(meta))
    stage.rename(target)  # Windows same-volume, no replacement.
    return cache_read(target, row, filename)


def listing_names(listing):
    items = listing["directory"]["item"]
    require(len(items) <= 3000, "SEC_DIRECTORY_TOO_LARGE")
    return {
        item["name"]
        for item in items
        if isinstance(item.get("name"), str)
        and NAME.fullmatch(item["name"])
        and ".." not in item["name"]
    }


def taxonomy_files(listing):
    names = sorted(
        name
        for name in listing_names(listing)
        if re.search(r"(?:\.xsd|_(?:cal|pre|def|lab|ref)\.xml)$", name, re.IGNORECASE)
    )
    require(len(names) <= 32, "FILING_TAXONOMY_DOCUMENT_LIMIT")
    return names


def documents(row, listing):
    names = listing_names(listing)
    primary = row.get("primary")
    instances = sorted(name for name in names if re.search(r"(?:_html?|[-_]\d{8})\.xml$", name))
    if not primary:
        # A human can supply an independently established primary via the custom inventory.
        return instances if len(instances) == 1 else []
    require(primary in names, "PRIMARY_DOCUMENT_NOT_IN_SEC_DIRECTORY")
    instance_name = primary.replace(".", "_") + ".xml"
    base = Path(primary).stem
    preferred = sorted(names & {instance_name, f"{base}.xml"})
    return [primary, *(preferred or (instances if len(instances) == 1 else []))]


def acquire(rows, root, client):
    lock_path = root / ".download.lock"
    if bulk.exists(lock_path):
        bulk.plain(lock_path)
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            for row in rows:
                print(f"{row['symbol']} {row['accession']}", file=sys.stderr)
                listing, _ = get_document(client, root, row, "index.json")
                directory = json.loads(listing)
                for filename in dict.fromkeys(
                    documents(row, directory) + taxonomy_files(directory)
                ):
                    get_document(client, root, row, filename)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def taxonomy(concept):
    uri, _, tag = (concept or "").lstrip("{").partition("}")
    if re.fullmatch(r"https?://fasb\.org/us-gaap/\d{4}(?:-\d\d-\d\d)?", uri):
        return "us-gaap", tag
    if uri == "http://xbrl.sec.gov/dei/2021q4" or re.fullmatch(
        r"https?://xbrl\.sec\.gov/dei/\d{4}(?:-\d\d-\d\d)?", uri
    ):
        return "dei", tag
    return None, tag


def unit_matches(raw, unit):
    shares = f"{{{XBRL}}}shares"
    expected = {
        "USD": ([f"{{{ISO}}}USD"], []),
        "shares": ([shares], []),
        "pure": ([f"{{{XBRL}}}pure"], []),
        "USD/shares": ([f"{{{ISO}}}USD"], [shares]),
    }.get(unit)
    return bool(raw and expected and (raw["numerator"], raw["denominator"]) == expected)


def matches(raw, fact, cik):
    context = raw.get("context")
    return bool(
        context
        and taxonomy(raw.get("concept")) == (fact["taxonomy"], fact["tag"])
        and context["scheme"] in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
        and bulk.matches_cik(context["entity"], cik)
        and (context["startDate"], context["instant"] or context["endDate"])
        == (fact["period_start"], fact["period_end"])
        and unit_matches(raw["unit"], fact["unit"])
    )


def join(fact, rows, cik):
    accession = fact.get("accession_number")
    comparable = [
        row
        for row in rows
        if matches(row, fact, cik)
        and (not accession or row.get("source", {}).get("accession") == accession)
    ]
    # Companyfacts covers the whole filing entity. Preserve dimensional matches,
    # but never infer that a reported member is equivalent to the whole entity.
    whole = [row for row in comparable if not row["context"]["dimensions"]]
    if not comparable:
        status, reason = "MISSING_EVIDENCE", "NO_COMPARABLE_SOURCE"
    elif not whole or any(
        row["context"]["opaque_context"]
        or row.get("evidence_issue")
        or any(
            not dim.get("axis")
            or not (dim.get("member") or dim.get("typed_xml"))
            or dim.get("is_default")
            for dim in row["context"]["dimensions"]
        )
        for row in comparable
    ):
        status, reason = "UNRESOLVED_CONTEXT", "REPORTING_CONTEXT_UNRESOLVED"
    else:
        status, reason = whole_entity_match(whole, Decimal(str(fact["value"])))
    return {
        "status": status,
        "reason": reason,
        "fact": fact,
        "source_candidates": sorted(row["document_ordinal"] for row in comparable),
        "whole_entity_candidates": sorted(row["document_ordinal"] for row in whole),
    }


def whole_entity_match(rows, target):
    aspects = {
        (
            row["concept"],
            row["context"]["scheme"],
            row["context"]["entity"],
            row.get("source", {}).get("accession"),
            row.get("source", {}).get("sha256"),
        )
        for row in rows
    }
    if len(aspects) != 1 or any(row["value"] is None for row in rows):
        return "UNRESOLVED_CONTEXT", "DISTINCT_OR_INCOMPLETE_SOURCE_ASPECTS"
    observations = {(Decimal(row["value"]), row.get("decimals")) for row in rows}
    if len(observations) == 1:
        value, _ = next(iter(observations))
        if value == target:
            return "VERIFIED_SOURCE_FACT", "COMPLETE_DUPLICATES" if len(rows) > 1 else "EXACT"
        return "CONFLICT", "DIFFERENT_REPORTED_VALUE"
    precisions = {}
    intervals = []
    for value, decimals in observations:
        interval = rounding(value, decimals)
        if interval is None:
            return "UNRESOLVED_CONTEXT", "DECIMALS_UNAVAILABLE"
        precision = float("inf") if decimals == "INF" else int(decimals)
        if precision in precisions and precisions[precision] != value:
            return "CONFLICT", "INCONSISTENT_DUPLICATES"
        precisions[precision] = value
        intervals.append(interval)
    if max(low for low, _ in intervals) > min(high for _, high in intervals):
        return "CONFLICT", "INCONSISTENT_DUPLICATES"
    if precisions[max(precisions)] == target:
        return "VERIFIED_SOURCE_FACT", "CONSISTENT_DUPLICATES_MOST_PRECISE"
    if target in precisions.values():
        return "CANDIDATE_EQUIVALENCE", "CONSISTENT_DUPLICATES_REVIEW_VALUE"
    return "CONFLICT", "DIFFERENT_REPORTED_VALUE"


def rounding(value, decimals):
    if decimals == "INF":
        return value, value
    if not decimals or not re.fullmatch(r"[+-]?\d{1,2}", decimals):
        return None
    half = Decimal(5).scaleb(-int(decimals) - 1)
    return value - half, value + half


def lly_rounding(filings):
    targets = (
        ("0000059478-25-000254", "2025-07-01", "2025-09-30", "179600000"),
        ("0000059478-25-000254", "2025-01-01", "2025-09-30", "672400000"),
        ("0000059478-25-000204", "2025-01-01", "2025-06-30", "492700000"),
    )
    items, intervals = [], []
    for acc, start, end, value in targets:
        fact = {
            "taxonomy": "us-gaap",
            "tag": "InterestExpenseNonoperating",
            "period_start": start,
            "period_end": end,
            "unit": "USD",
            "value": value,
            "accession_number": acc,
        }
        observations = filings.get(acc, {}).get("observations", [])
        match = join(fact, observations, "0000059478")
        if match["status"] != "VERIFIED_SOURCE_FACT":
            return {"status": match["status"], "accession": acc, "target": fact}
        supporting = [
            r
            for r in observations
            if r["document_ordinal"] in match["whole_entity_candidates"]
            and Decimal(r["value"]) == Decimal(value)
        ]
        intervals_with_precision = {rounding(Decimal(value), r["decimals"]) for r in supporting}
        valid_intervals = [
            interval for interval in intervals_with_precision if interval is not None
        ]
        interval = min(valid_intervals, key=lambda bounds: bounds[1] - bounds[0], default=None)
        if interval is None:
            return {
                "status": "MISSING_EVIDENCE",
                "accession": acc,
                "reason": "DECIMALS_UNAVAILABLE",
            }
        intervals.append(interval)
        items.append(
            {
                "accession": acc,
                "supporting_sources": [
                    {
                        "ordinal": r["document_ordinal"],
                        "decimals": r["decimals"],
                        "source": r["source"],
                        "context": r["context"],
                        "concept": r["concept"],
                        "value": r["value"],
                        "unit": r["unit"],
                        "table_captions": r.get("table_captions", []),
                    }
                    for r in supporting
                ],
            }
        )
    direct, ytd, half = intervals
    derived = ytd[0] - half[1], ytd[1] - half[0]
    overlap = max(direct[0], derived[0]) <= min(direct[1], derived[1])
    return {
        "status": "CANDIDATE_EQUIVALENCE" if overlap else "CONFLICT",
        "inputs": items,
        "direct_interval": list(map(str, direct)),
        "derived_interval": list(map(str, derived)),
        "common_interval": [str(max(direct[0], derived[0])), str(min(direct[1], derived[1]))]
        if overlap
        else None,
        "review_required": [
            "same Reporting Entity and disclosure scope",
            "H1-to-9M comparability",
            "rounding interval boundary and possible restatement",
        ],
    }


def add_extended_rows(rows, issuer, filings):
    cik, symbol = issuer["cik"], issuer["symbol"]
    existing = {row["accession"]: row for row in rows if row["cik"] == cik}
    reasons = defaultdict(set)
    for acc, (filed, form, _) in filings.items():
        if form in qualification.FORMS and WARMUP_START <= filed <= qualification.COVERAGE_END:
            reasons[acc].add("WARMUP" if filed.year < 2020 else "RESEARCH_PERIOD")
    for suffix in ACCOUNTING_ACCESSIONS.get(symbol, ()):
        accession = f"{cik}-{suffix}" if len(suffix) == 9 else suffix
        reasons[accession].add("ACCOUNTING_AUDIT")
    for acc, tags in sorted(reasons.items()):
        if acc not in existing:
            existing[acc] = {
                "cik": cik,
                "symbol": symbol,
                "accession": acc,
                "tier": "EXTENDED",
                "probes": [],
            }
            rows.append(existing[acc])
        existing[acc]["coverage_reasons"] = sorted(tags)
    for row in existing.values():
        tags = set(row.get("coverage_reasons", [])) | {row["tier"]}
        if row["probes"]:
            tags.add("BOUND_DIAGNOSIS")
        row["coverage_reasons"] = sorted(tags)
        row["index_url"] = f"{filing_base(row)}/{row['accession']}-index.html"


def source_metadata(rows, *, extended=False):
    """Bind the pilot observations to the existing verified Bulk generation."""
    scope = pilot.pilot_spec()
    issuers = [i for i in scope.active if extended or i["cik"] in {row["cik"] for row in rows}]
    layout = bulk.Layout({"originals": pilot.RESEARCH_ROOT / "sec-originals"}, {})
    manifest, paths, excluded = pilot.source_inputs(layout, scope)
    report = qualification.checked_metadata(
        qualification.RESULT / pilot.REPORT, qualification.REPORT_SHA
    )
    plan = qualification.checked_metadata(
        qualification.ARTIFACTS / "f01-pilot-admission-reviewed-v2.json",
        report["binding"]["admission_sha256"],
    )
    sources = {}
    for issuer in issuers:
        evidence = plan["issuers"][issuer["cik"]]["identity_evidence"]
        sources[issuer["cik"]] = qualification.checked_metadata(
            Path(evidence["path"]), evidence["sha256"]
        )["sources"]
    with pinned_files({"submissions": paths["submissions"]}):
        submissions = pilot.submissions_members(paths, issuers, excluded["submissions"])
    for issuer in issuers:
        cik = issuer["cik"]
        main, digest, history = submissions[cik]
        expected = sources[cik]["submissions"]
        require(
            digest == expected["member_sha256"]
            and {h["member"]: h["sha256"] for h in history}
            == {h["member"]: h["member_sha256"] for h in expected["history_members"]},
            "SUBMISSIONS_MEMBER_BINDING_MISMATCH",
        )
        filings, conflicts, _ = pilot.filing_index(main, history)
        if extended:
            add_extended_rows(rows, issuer, filings)
        documents_by_filing, _ = qualification.filing_documents(main, history)
        for row in (r for r in rows if r["cik"] == cik):
            acc = row["accession"]
            if acc in filings:
                day, form, acceptance = filings[acc]
                row.update(
                    filed=day.isoformat(),
                    form=form,
                    acceptance=acceptance.isoformat() if acceptance else None,
                    availability_source={
                        "primary_submissions_member_sha256": digest,
                        "history_member_sha256": {h["member"]: h["sha256"] for h in history},
                    },
                )
            if acc in conflicts:
                row.update(filed=None, acceptance=None, availability_conflict=True)
            candidates = documents_by_filing.get(acc, set())
            if len(candidates) == 1:
                primary = next(iter(candidates))
                if NAME.fullmatch(primary) and primary.lower().endswith((".htm", ".html")):
                    row["primary"] = primary
            if extended and acc not in filings:
                # A diagnosis date is retained only as a lead when Submissions has no witness.
                row.update(availability_unverified=True)
    if extended:
        rows.sort(key=lambda row: (row["tier"] != "INITIAL", row["cik"], row["accession"]))
    return issuers, paths, excluded, sources, manifest


def companyfacts(rows, metadata, *, extended=False):
    issuers, paths, excluded, sources, manifest = metadata
    observations = defaultdict(list)
    requested = {(r["cik"], r["accession"]) for r in rows}
    mapped = {key for aliases in TAG_ALIASES.values() for key in aliases}
    with (
        pinned_files({"companyfacts": paths["companyfacts"]}),
        zipfile.ZipFile(paths["companyfacts"]) as archive,
    ):
        for issuer in issuers:
            cik, symbol = issuer["cik"], issuer["symbol"]
            payload, digest = pilot.zip_member(
                archive, issuer["companyfacts_member"], "companyfacts", excluded["companyfacts"]
            )
            require(
                pilot.provider_cik(payload) == cik
                and digest == sources[cik]["companyfacts"]["member_sha256"],
                "COMPANYFACTS_MEMBER_BINDING_MISMATCH",
            )
            source = {
                "member_sha256": digest,
                "member": issuer["companyfacts_member"],
                "manifest_sha256": pilot.SOURCE_SHA,
                "retrieval_ended": manifest["archives"]["companyfacts"]["retrieval_ended_at_utc"],
            }
            pattern = re.compile("." if extended else FOCUS.get(symbol, "."), re.IGNORECASE)
            for fact in parse_company_facts(payload, symbol, preserve_observations=True):
                key = cik, fact.accession_number
                if key in requested and pattern.search(fact.tag):
                    observations[key].append((fact.model_dump(mode="json"), source))
            for taxonomy_name, concepts in payload["facts"].items():
                for tag, concept in concepts.items():
                    if (taxonomy_name, tag) in mapped or not pattern.search(tag):
                        continue
                    for unit, items in concept["units"].items():
                        for item in items:
                            key = cik, item.get("accn")
                            if key not in requested:
                                continue
                            try:
                                numeric_value = Decimal(str(item["val"]))
                            except (KeyError, InvalidOperation):
                                continue
                            if numeric_value.is_finite():
                                observations[key].append(
                                    (
                                        {
                                            "cik": cik,
                                            "symbol": symbol,
                                            "accession_number": key[1],
                                            "taxonomy": taxonomy_name,
                                            "tag": tag,
                                            "unit": unit,
                                            "period_start": item.get("start"),
                                            "period_end": item.get("end"),
                                            "filed": item.get("filed"),
                                            "value": str(numeric_value),
                                            "metric": None,
                                            "raw_observation": item,
                                        },
                                        source,
                                    )
                                )
    return observations


class ArelleProcessor:
    def __init__(self, directory):
        require(
            directory.is_absolute() and directory.drive.upper() == "G:",
            "ARELLE_RESEARCH_DIRECTORY_MUST_BE_ON_G",
        )
        self.directory = bulk.directory(directory)
        self.site = bulk.directory(directory / "site")
        self.cache = safe_directory(directory / "cache")
        self.sec_plugin = verified_sec_plugin(directory)
        self.packages = []
        manifest = directory / "taxonomy-packages.json"
        if bulk.exists(manifest):
            _, items = bulk.read_metadata(manifest)
            for item in items:
                name = item["file"]
                require(
                    NAME.fullmatch(name) and ".." not in name and name.endswith(".zip"),
                    "INVALID_TAXONOMY_PACKAGE_NAME",
                )
                folder = bulk.directory(directory / "packages")
                path = folder / name
                bulk.plain(path)
                require(pilot.file_sha(path) == item["sha256"], "TAXONOMY_PACKAGE_SHA256_MISMATCH")
                require(
                    all(manifest_entry["path"] != str(path) for manifest_entry in self.packages),
                    "DUPLICATE_TAXONOMY_PACKAGE",
                )
                self.packages.append({"path": str(path), "sha256": item["sha256"]})
        self.release = self.run(probe=True)

    def run(self, **request):
        payload = {
            "site": str(self.site),
            "cache": str(self.cache),
            "packages": [p["path"] for p in self.packages],
            "sec_plugin": str(self.sec_plugin) if self.sec_plugin else None,
            **request,
        }
        env = os.environ | {"TEMP": str(self.directory), "TMP": str(self.directory)}
        child = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(Path(__file__).with_name("f01_arelle_adapter.py")),
            ],
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            timeout=180,
            check=False,
        )
        if child.returncode:
            raise ValueError(f"ARELLE_WORKER_FAILED: {child.stderr.strip()[-2000:]}")
        return json.loads(child.stdout)


def verified_sec_plugin(directory):
    plugin = directory / "plugins" / f"edgar-{EDGAR_COMMIT}" / "transform"
    if not bulk.exists(plugin):
        return None
    bulk.directory(plugin)
    for name, expected in SEC_PLUGIN_FILES.items():
        bulk.plain(plugin / name)
        require(pilot.file_sha(plugin / name) == expected, f"SEC_PLUGIN_HASH_MISMATCH: {name}")
    return plugin


def document_format(name, data):
    if not name.lower().endswith((".html", ".htm")):
        return "XBRL_DOCUMENT"
    if re.search(rb"https?://www\.xbrl\.org/(?:2008|2013)/inlineXBRL", data, re.I):
        return "INLINE_XBRL"
    if re.search(rb"<(?:!doctype\s+html\b|html[\s>])", data, re.I):
        return "ORDINARY_HTML"
    return "UNKNOWN_FORMAT"


def diagnostic_category(issue):
    code = issue.get("code", "").lower()
    reason = issue.get("reason", "").lower()
    if "unrecognized transformation" in reason or (
        "transformation" in code and "namespace" in reason
    ):
        return "MISSING_OR_UNSUPPORTED_TRANSFORMATION"
    if "missingreferences" in code:
        return "UNRESOLVED_XBRL_REFERENCE"
    if "schemaimportmissing" in code or (
        "ioerror" in code and ("offline" in reason or ".xsd" in reason)
    ):
        return "MISSING_TAXONOMY_OR_DTS_REFERENCE"
    return "XBRL_ERROR"


def extract_cached(row, folder, listing, processor, *, extended=False):
    entrypoints = documents(row, listing)
    observations, alternatives, errors, sources, formats = [], [], [], {}, {}
    pattern = None if extended else FOCUS.get(row["symbol"])
    with tempfile.TemporaryDirectory(prefix="filing-", dir=processor.directory) as temporary:
        stage = Path(temporary)
        for name in dict.fromkeys(entrypoints + taxonomy_files(listing)):
            if not bulk.exists(folder / name):
                errors.append({"document": name, "reason": "MISSING_CACHED_DOCUMENT"})
                continue
            data, source = cache_read(folder / name, row, name)
            sources[name] = source
            formats[name] = document_format(name, data)
            (stage / name).write_bytes(data)
        staged_sources = {(stage / name).resolve(): source for name, source in sources.items()}
        for name in entrypoints:
            if name not in sources:
                continue
            if formats[name] in {"ORDINARY_HTML", "UNKNOWN_FORMAT"}:
                errors.append(
                    {
                        "document": name,
                        "code": formats[name],
                        "category": "INCOMPATIBLE_DOCUMENT_FORMAT",
                    }
                )
                alternatives.append(
                    {"source": sources[name], "selected": False, "format": formats[name]}
                )
                continue
            try:
                request = {"document": str(stage / name)}
                if extended:
                    request["all_facts"] = True
                result = processor.run(**request)
            except (ValueError, subprocess.TimeoutExpired) as error:
                errors.append(
                    {
                        "document": name,
                        "reason": str(error),
                        "category": "WORKER_ERROR",
                        "status": "MISSING_EVIDENCE",
                    }
                )
                continue
            issues = result["issues"]
            errors.extend(
                {
                    "document": name,
                    "status": "MISSING_EVIDENCE",
                    "category": diagnostic_category(issue),
                    **issue,
                }
                for issue in issues
            )
            parsed = [
                item
                for item in result["observations"]
                if not pattern
                or re.search(pattern, (item["concept"] or "").rsplit("}", 1)[-1], re.I)
            ]
            for item in parsed:
                source = staged_sources.get(Path(item["source_document"]).resolve())
                if source is None:
                    item["evidence_issue"] = "ORIGINAL_DOCUMENT_BINDING_UNAVAILABLE"
                else:
                    item["source"] = source
                    item["source_document"] = source["url"]
                for relationship in item["relationships"]:
                    linkbase = staged_sources.get(Path(relationship["source_document"]).resolve())
                    if linkbase is not None:
                        relationship["source_document"] = linkbase["url"]
                        relationship["source_sha256"] = linkbase["sha256"]
            taxonomy = []
            for dependency in result.get("taxonomy_documents", []):
                resolved = dependency["resolved_path"]
                staged = staged_sources.get(Path(resolved).resolve()) if resolved else None
                if staged is not None:
                    dependency = {"uri": staged["url"], "sha256": staged["sha256"]}
                elif resolved and Path(resolved).resolve().is_relative_to(processor.cache):
                    local = Path(resolved)
                    binding = local.with_name(local.name + ".f01-source.json")
                    if bulk.exists(binding):
                        _, metadata = bulk.read_metadata(binding)
                        require(
                            metadata["sha256"] == dependency["sha256"],
                            f"TAXONOMY_CACHE_BINDING_MISMATCH: {resolved}",
                        )
                        dependency["bootstrap_source"] = metadata
                taxonomy.append(dependency)
            selected = bool(parsed and not observations and not issues)
            if selected:
                observations = parsed
            alternatives.append(
                {
                    "source": sources[name],
                    "observations": None if selected else parsed,
                    "selected": selected,
                    "format": formats[name],
                    "taxonomy_documents": taxonomy,
                }
            )
    return observations, alternatives, errors, list(sources.values())


def draft(rows, root, facts, processor, *, extended=False):
    results = {}
    for row in rows:
        acc, cik = row["accession"], row["cik"]
        folder = root / cik / acc
        observations, alternatives, errors, sources = [], [], [], []
        index_source = None
        if bulk.exists(folder / "index.json"):
            listing, index_source = cache_read(folder / "index.json", row, "index.json")
            observations, alternatives, errors, sources = extract_cached(
                row, folder, json.loads(listing), processor, extended=extended
            )
        by_concept = defaultdict(list)
        for observation in observations:
            by_concept[taxonomy(observation.get("concept"))].append(observation)
        joins = [
            join(fact, by_concept[fact["taxonomy"], fact["tag"]], cik)
            | {
                "companyfacts_source": source,
                "filing_date": row.get("filed"),
                "acceptance": row.get("acceptance"),
            }
            for fact, source in facts.get((cik, acc), [])
        ]
        for item in joins:
            if (
                not row.get("filed")
                or row.get("availability_unverified")
                or row["filed"] != item["fact"].get("filed")
            ):
                item["status"] = "UNRESOLVED_CONTEXT"
                item["reason"] = "FILING_DATE_BINDING_UNAVAILABLE_OR_INCONSISTENT"
        results[cik, acc] = row | {
            "status": "UNRESOLVED_CONTEXT" if observations else "MISSING_EVIDENCE",
            "observations": observations,
            "companyfacts_joins": joins,
            "documents": alternatives,
            "cached_sources": sources,
            "index_source": index_source,
            "issues": errors,
            "diagnostic_summary": {
                "issues_by_category": dict(
                    Counter(item.get("category", "MISSING_DOCUMENT") for item in errors)
                ),
                "joins_by_status": dict(Counter(item["status"] for item in joins)),
            },
        }
    lly = {acc: record for (cik, acc), record in results.items() if cik == "0000059478"}
    return {
        "contract": "F01_ORIGINAL_FILING_EVIDENCE_DRAFT_V1",
        "review_required": True,
        "xbrl_processor": processor.release
        | {
            "name": "Arelle",
            "taxonomy_packages": processor.packages,
            "requirements_sha256": pilot.file_sha(pilot.REPO / "requirements-f01-arelle.txt"),
            "sec_transforms": {"commit": EDGAR_COMMIT, "files": SEC_PLUGIN_FILES}
            if processor.sec_plugin
            else None,
        },
        "semantic_evidence": {
            "contexts": [],
            "acceptance": [],
            "amendments": [],
            "debt_coverage": [],
        },
        "filings": list(results.values()),
        "lly_rounding": lly_rounding(lly),
    }


def extended_cache(rows, root, candidate):
    filings = []
    for row in rows:
        folder = root / row["cik"] / row["accession"]
        sources, issues, index_source = [], [], None
        if not bulk.exists(folder / "index.json"):
            issues.append({"reason": "MISSING_CACHED_FILING_INDEX"})
        else:
            data, index_source = cache_read(folder / "index.json", row, "index.json")
            listing = json.loads(data)
            entrypoints = documents(row, listing)
            if not entrypoints:
                issues.append({"reason": "NO_BOUND_FILING_ENTRYPOINT"})
            for name in dict.fromkeys(entrypoints + taxonomy_files(listing)):
                if not bulk.exists(folder / name):
                    issues.append({"document": name, "reason": "MISSING_CACHED_DOCUMENT"})
                    continue
                _, source = cache_read(folder / name, row, name)
                sources.append(source)
        filings.append(
            row | {"index_source": index_source, "cached_sources": sources, "issues": issues}
        )
    return {
        "contract": EXTENDED_INPUT,
        "candidate": candidate,
        "pilot_diagnosis_sha256": DIAG_SHA,
        "filings": filings,
    }


def verify_extended_inputs(cache, coverage, cache_sha, candidate):
    require(
        cache["contract"] == EXTENDED_INPUT
        and cache["candidate"] == candidate
        and coverage["candidate"] == candidate
        and coverage["draft_sha256"] == cache_sha
        and coverage.get("offline_coverage") is True
        and coverage["sec_plugin_verified"] is True
        and coverage["sec_plugin_commit"] == EDGAR_COMMIT
        and bool(coverage["urls"])
        and all(r["status"] == "BOUND" for r in coverage["urls"].values()),
        "EXTENDED_TAXONOMY_COVERAGE_NOT_BOUND",
    )


def write_extended_draft(rows, root, facts, processor, candidate, cache, coverage, hashes, target):
    require(processor.sec_plugin, "EXTENDED_EXTRACTION_REQUIRES_SEC_PLUGIN")
    require(rows, "EXTENDED_INVENTORY_EMPTY")
    stage = Path(tempfile.mkdtemp(prefix=".extended-evidence-incomplete-", dir=root))
    entries = []
    for row, expected in zip(rows, cache["filings"], strict=True):
        print(f"Extract {row['symbol']} {row['accession']}", file=sys.stderr)
        staging_bytes = sum(source["bytes"] for source in expected["cached_sources"])
        require(
            shutil.disk_usage(root).free >= staging_bytes + RESERVE, "FILING_OUTPUT_SPACE_REQUIRED"
        )
        record = draft([row], root, facts, processor, extended=True)
        item = record["filings"][0]
        require(
            item["index_source"] == expected["index_source"]
            and item["cached_sources"] == expected["cached_sources"],
            "EXTENDED_CACHE_CHANGED_DURING_EXTRACTION",
        )
        for document in item["documents"]:
            for dependency in document.get("taxonomy_documents", []):
                url = dependency.get("bootstrap_source", {}).get("url", dependency["uri"])
                if url in coverage["urls"]:
                    require(
                        dependency["sha256"] == coverage["urls"][url]["source"]["sha256"],
                        f"EXTENDED_TAXONOMY_CHANGED_DURING_EXTRACTION: {url}",
                    )
        item["issues"].extend(issue for issue in expected["issues"] if issue not in item["issues"])
        item["diagnostic_summary"]["issues_by_category"] = dict(
            Counter(issue.get("category", "MISSING_DOCUMENT") for issue in item["issues"])
        )
        name = f"{row['cik']}-{row['accession']}.json"
        raw = (json.dumps(item, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        require(len(raw) <= SHARD_LIMIT, "FILING_SHARD_SIZE_LIMIT")
        require(shutil.disk_usage(root).free >= len(raw) + RESERVE, "FILING_OUTPUT_SPACE_REQUIRED")
        bulk.write_new(stage / name, raw)
        entries.append(
            {
                "cik": row["cik"],
                "symbol": row["symbol"],
                "accession": row["accession"],
                "file": name,
                "sha256": sha(raw),
                "bytes": len(raw),
            }
        )
    manifest = {
        "contract": EXTENDED_DRAFT,
        "candidate_code": candidate,
        "pilot_diagnosis_sha256": DIAG_SHA,
        "inputs": hashes,
        "xbrl_processor": record["xbrl_processor"],
        "filings": entries,
        "review_required": True,
    }
    bulk.write_new(stage / "manifest.json", bulk.json_bytes(manifest))
    stage.rename(target)


def extended_records(path, manifest):
    require(manifest["contract"] == EXTENDED_DRAFT, "INVALID_EXTENDED_DRAFT")
    validate_rows(manifest["filings"])
    for entry in manifest["filings"]:
        require(
            entry["file"] == f"{entry['cik']}-{entry['accession']}.json",
            "INVALID_EXTENDED_SHARD_NAME",
        )
        shard = path.parent / entry["file"]
        raw, row = bulk.read_metadata(shard, max_bytes=SHARD_LIMIT)
        require(
            len(raw) == entry["bytes"]
            and sha(raw) == entry["sha256"]
            and all(row[key] == entry[key] for key in ("symbol", "cik", "accession")),
            "EXTENDED_SHARD_BINDING_MISMATCH",
        )
        yield entry, row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--download", action="store_true")
    modes.add_argument("--resume", action="store_true")
    modes.add_argument("--extract", action="store_true")
    modes.add_argument("--cached-inputs", action="store_true")
    parser.add_argument("--authorize-download", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument("--include-additional", action="store_true")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--cache-manifest", type=Path)
    parser.add_argument("--taxonomy-coverage", type=Path)
    parser.add_argument(
        "--inventory-file",
        type=Path,
        help="Custom filing metadata list. Only the built-in inventory has Companyfacts joins.",
    )
    parser.add_argument("--output", default="f01-original-filing-evidence-draft.json")
    parser.add_argument("--arelle-dir", type=Path, default=ARELLE_ROOT)
    args = parser.parse_args(argv)
    online = args.download or args.resume
    if online != args.authorize_download or (online and not args.user_agent):
        parser.error("Network modes require --authorize-download and --user-agent")
    if args.extended and (args.inventory_file or args.include_additional):
        parser.error("--extended uses only the bound Research inventory")
    if args.cached_inputs and not args.extended:
        parser.error("--cached-inputs requires --extended")
    if bool(args.cache_manifest or args.taxonomy_coverage) != bool(
        args.extended and args.extract
    ) or (args.extended and args.extract and not (args.cache_manifest and args.taxonomy_coverage)):
        parser.error("Extended extraction requires --cache-manifest and --taxonomy-coverage")
    try:
        candidate = pilot.code_binding()  # Enforce the branch before any mode runs.
        if args.inventory_file:
            _, rows = bulk.read_metadata(args.inventory_file.absolute())
            metadata = None
        else:
            rows = inventory()
            if not (args.include_additional or args.extended) and (online or args.extract):
                rows = [r for r in rows if r["tier"] == "INITIAL"]
            metadata = source_metadata(rows, extended=args.extended)
        validate_rows(rows)
        if online:
            acquire(rows, safe_directory(ROOT), bulk.SecHttp(args.user_agent))
        elif args.extract or args.cached_inputs:
            require(
                NAME.fullmatch(args.output)
                and ".." not in args.output
                and (args.output.endswith(".json") or args.extended and args.extract),
                "INVALID_DRAFT_FILENAME",
            )
            directory = safe_directory(ROOT)
            target = directory / args.output
            require(not bulk.exists(target), "EVIDENCE_OUTPUT_EXISTS")
            if args.cached_inputs:
                bulk.write_new(target, bulk.json_bytes(extended_cache(rows, directory, candidate)))
                print(str(target))
                return 0
            if args.extended:
                require(
                    args.arelle_dir == ARELLE_ROOT
                    and all(
                        item.is_absolute() and item.is_relative_to(pilot.RESEARCH_ROOT)
                        for item in (args.cache_manifest, args.taxonomy_coverage)
                    ),
                    "EXTENDED_RESEARCH_PATH_REQUIRED",
                )
                cache_raw, cache = bulk.read_metadata(args.cache_manifest, max_bytes=LIMIT)
                coverage_raw, coverage = bulk.read_metadata(args.taxonomy_coverage, max_bytes=LIMIT)
                hashes = {
                    "cache_sha256": sha(cache_raw),
                    "taxonomy_coverage_sha256": sha(coverage_raw),
                }
                verify_extended_inputs(cache, coverage, hashes["cache_sha256"], candidate)
                require(
                    [
                        {key: item.get(key) for key in row}
                        for row, item in zip(rows, cache["filings"], strict=True)
                    ]
                    == rows,
                    "EXTENDED_INVENTORY_DRIFT",
                )
                write_extended_draft(
                    rows,
                    directory,
                    companyfacts(rows, metadata, extended=True),
                    ArelleProcessor(args.arelle_dir),
                    candidate,
                    cache,
                    coverage,
                    hashes,
                    target,
                )
                print(str(target / "manifest.json"))
                return 0
            processor = ArelleProcessor(args.arelle_dir)
            value = draft(
                rows, directory, companyfacts(rows, metadata) if metadata else {}, processor
            )
            value["candidate_code"] = candidate
            value["pilot_diagnosis_sha256"] = DIAG_SHA if metadata else None
            stage = Path(tempfile.mkdtemp(prefix=".draft-incomplete-", dir=directory))
            staged = stage / "draft.json"
            bulk.write_new(staged, bulk.json_bytes(value))
            staged.rename(target)
            stage.rmdir()
            print(str(target))
        else:
            summary = Counter((r.get("tier", "CUSTOM"), r["symbol"]) for r in rows)
            print(
                json.dumps(
                    {
                        "cache": str(ROOT),
                        "counts": [
                            {"tier": tier, "symbol": symbol, "count": count}
                            for (tier, symbol), count in sorted(summary.items())
                        ],
                        "no_primary": sum(not r.get("primary") for r in rows),
                        "filings": rows,
                        **(
                            {
                                "candidate": candidate,
                                "pilot_diagnosis_sha256": DIAG_SHA,
                                "no_acceptance": sum(not r.get("acceptance") for r in rows),
                            }
                            if args.extended
                            else {}
                        ),
                    },
                    indent=2,
                )
            )
    except (bulk.Blocked, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"FILING_EVIDENCE_BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
