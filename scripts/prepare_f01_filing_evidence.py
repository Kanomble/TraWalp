"""Prepare SEC filing evidence in an isolated document cache, without SQLite."""

from __future__ import annotations

import argparse
import hashlib
import html.entities
import io
import json
import msvcrt
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.dont_write_bytecode = True
import validate_f01_sec_bulk_pilot as qualification  # noqa: E402

from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.xbrl_parser import TAG_ALIASES, parse_company_facts  # noqa: E402

pilot = qualification.pilot
bulk = pilot.bulk
require = bulk.require
ROOT = pilot.RESEARCH_ROOT / "sec-originals" / "filing-evidence"
DIAG = "f01-fundamental-gaps-readonly-20261121-220344.json"
DIAG_SHA = "43609781335d384931b7b3b44ad9bc3ed18d7aaf40b7ecb7b447adfa4c4a805f"
SEC = "https://www.sec.gov/Archives/edgar/data"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
CIK = re.compile(r"\d{10}", re.ASCII)
LIMIT = 64 * 1024**2
RESERVE = 512 * 1024**2
XBRL = "http://www.xbrl.org/2003/instance"
IX = {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
DIM = "http://xbrl.org/2006/xbrldi"
ISO = "http://www.xbrl.org/2003/iso4217"
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


def documents(row, listing):
    items = listing["directory"]["item"]
    require(len(items) <= 3000, "SEC_DIRECTORY_TOO_LARGE")
    names = {
        item["name"]
        for item in items
        if isinstance(item.get("name"), str)
        and NAME.fullmatch(item["name"])
        and ".." not in item["name"]
    }
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
                for filename in documents(row, json.loads(listing)):
                    get_document(client, root, row, filename)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def expanded(raw, names):
    if raw.startswith("{"):
        return raw
    prefix, separator, name = raw.partition(":")
    if not separator:
        return f"{{{names['']}}}{prefix}" if "" in names else prefix
    return f"{{{names[prefix]}}}{name}" if prefix in names else None


def parse_xml(data):
    raw = data.decode("utf-8-sig")
    raw = re.sub(r"<!DOCTYPE\s+html\s*>", "", raw, flags=re.IGNORECASE)
    require(not re.search(r"<!DOCTYPE|<!ENTITY", raw, re.IGNORECASE), "UNSUPPORTED_XML_DECLARATION")
    # SEC XHTML can use predefined HTML names without a DTD.
    raw = re.sub(
        r"&([A-Za-z][A-Za-z0-9]+);",
        lambda m: (
            f"&#{html.entities.name2codepoint[m[1]]};"
            if m[1] in html.entities.name2codepoint
            and m[1] not in {"amp", "lt", "gt", "quot", "apos"}
            else m[0]
        ),
        raw,
    )
    namespace, parents, pending, stack = {}, {}, {}, []
    events = ET.iterparse(io.StringIO(raw), events=("start", "end", "start-ns"))
    for event, value in events:
        if event == "start-ns":
            pending[value[0]] = value[1]
        elif event == "start":
            parent = stack[-1] if stack else None
            names = namespace[parent] if parent is not None else {}
            namespace[value] = names | pending if pending else names
            pending = {}
            parents[value] = parent
            stack.append(value)
        else:
            stack.pop()
    return events.root, namespace, parents


def context_data(node, names):
    prefix = f"{{{XBRL}}}"
    identifier = node.find(f".//{prefix}identifier")
    period = node.find(f"{prefix}period")
    times = {
        tag: period.findtext(prefix + tag) if period is not None else None
        for tag in ("instant", "startDate", "endDate")
    }
    dimensions = []
    for item in node.iter():
        if item.tag == f"{{{DIM}}}explicitMember":
            dimensions.append(
                {
                    "axis": expanded(item.get("dimension", ""), names[item]),
                    "member": expanded((item.text or "").strip(), names[item]),
                }
            )
        elif item.tag == f"{{{DIM}}}typedMember":
            dimensions.append(
                {
                    "axis": expanded(item.get("dimension", ""), names[item]),
                    "typed_xml": ET.tostring(item, encoding="unicode"),
                }
            )
    # Undeclared segment/scenario content cannot be silently treated as dimensionless.
    opaque_context = any(
        child.tag not in {f"{{{DIM}}}explicitMember", f"{{{DIM}}}typedMember"}
        for container in node.iter()
        if container.tag in {prefix + "segment", prefix + "scenario"}
        for child in container
    )
    return times | {
        "entity": identifier.text if identifier is not None else None,
        "scheme": identifier.get("scheme") if identifier is not None else None,
        "dimensions": dimensions,
        "opaque_context": opaque_context,
    }


def numeric(raw, transform, scale, sign):
    if transform:
        uri, _, local = transform[1:].partition("}") if transform.startswith("{") else ("", "", "")
        if not re.fullmatch(
            r"http://www\.xbrl\.org/inlineXBRL/transformation/\d{4}-\d{2}-\d{2}", uri
        ):
            return None
        cleaned = re.sub(r"\s", "", raw)
        if local in {"numdotdecimal", "num-dot-decimal"} and re.fullmatch(
            r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", cleaned
        ):
            raw = cleaned.replace(",", "")
        elif local in {"numcommadecimal", "num-comma-decimal"} and re.fullmatch(
            r"(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?", cleaned
        ):
            raw = cleaned.replace(".", "").replace(",", ".")
        elif local == "fixed-zero" or (local == "zerodash" and cleaned in {"-", "–", "—"}):
            raw = "0"
        else:
            return None
    if sign not in {"", "-"} or not re.fullmatch(r"[+-]?\d{1,2}", scale):
        return None
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw.strip()) or len(raw) > 160:
        return None
    try:
        val = Decimal(raw)
        if sign == "-" and val.is_signed():
            return None
        num = val.as_tuple()
        return str(Decimal((int(sign == "-" or num.sign), num.digits, num.exponent + int(scale))))
    except InvalidOperation:
        return None


def extract(data):
    tree, names, parents = parse_xml(data)
    prefix = f"{{{XBRL}}}"
    contexts, units = {}, {}
    for node in tree.iter():
        if node.tag == prefix + "context":
            require(node.get("id") and node.get("id") not in contexts, "DUPLICATE_CONTEXT_ID")
            contexts[node.get("id")] = context_data(node, names)
        if node.tag == prefix + "unit":
            require(node.get("id") and node.get("id") not in units, "DUPLICATE_UNIT_ID")
            units[node.get("id")] = {
                "numerator": [
                    expanded((item.text or "").strip(), names[item])
                    for item in node.iter(prefix + "measure")
                    if not any(
                        item in list(branch) for branch in node.iter(prefix + "unitDenominator")
                    )
                ],
                "denominator": [
                    expanded((item.text or "").strip(), names[item])
                    for branch in node.iter(prefix + "unitDenominator")
                    for item in branch.iter(prefix + "measure")
                ],
            }
    for ordinal, node in enumerate(tree.iter(), 1):
        cref, uref = node.get("contextRef"), node.get("unitRef")
        if not cref or not uref:
            continue
        inline = node.tag in {f"{{{uri}}}nonFraction" for uri in IX}
        concept = expanded(node.get("name", ""), names[node]) if inline else node.tag
        transform = expanded(node.get("format"), names[node]) if node.get("format") else ""
        scale, sign = node.get("scale", "0"), node.get("sign", "")
        raw = "".join(node.itertext()).strip()
        value = None
        markup = {
            "{http://www.w3.org/1999/xhtml}" + tag for tag in ("span", "b", "i", "em", "strong")
        }
        if (
            transform is not None
            and not node.get("continuedAt")
            and all(child.tag in markup for child in list(node.iter())[1:])
        ):
            value = numeric(raw, transform, scale, sign)
        parent = parents[node]
        while parent is not None and parent.tag.rsplit("}", 1)[-1].lower() != "table":
            parent = parents[parent]
        captions = (
            []
            if parent is None
            else [
                " ".join("".join(child.itertext()).split())
                for child in parent
                if child.tag.rsplit("}", 1)[-1].lower() == "caption"
            ]
        )
        yield {
            "concept": concept,
            "raw_text": raw,
            "value": value,
            "scale": scale,
            "sign": sign,
            "decimals": node.get("decimals"),
            "format": transform,
            "context_id": cref,
            "context": contexts.get(cref),
            "unit_id": uref,
            "unit": units.get(uref),
            "fact_id": node.get("id"),
            "document_ordinal": ordinal,
            "table_id": parent.get("id") if parent is not None else None,
            "table_captions": captions,
        }


def taxonomy(concept):
    uri, _, tag = (concept or "").lstrip("{").partition("}")
    if re.fullmatch(r"https?://fasb\.org/us-gaap/\d{4}(?:-\d\d-\d\d)?", uri):
        return "us-gaap", tag
    if re.fullmatch(r"https?://xbrl\.sec\.gov/dei/\d{4}(?:-\d\d-\d\d)?", uri):
        return "dei", tag
    return None, tag


def unit_matches(raw, unit):
    shares = f"{{{XBRL}}}shares"
    expected = {
        "USD": ([f"{{{ISO}}}USD"], []),
        "shares": ([shares], []),
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
    comparable = [row for row in rows if matches(row, fact, cik)]
    exact = [
        row
        for row in comparable
        if row["value"] is not None and Decimal(row["value"]) == Decimal(fact["value"])
    ]
    if len(comparable) == len(exact) == 1 and not (
        exact[0]["context"]["dimensions"] or exact[0]["context"]["opaque_context"]
    ):
        status = "VERIFIED_SOURCE_FACT"
    elif exact or any(
        row["value"] is None or row["context"]["dimensions"] or row["context"]["opaque_context"]
        for row in comparable
    ):
        status = "UNRESOLVED_CONTEXT"
    else:
        status = "CONFLICT" if comparable else "MISSING_EVIDENCE"
    return {
        "status": status,
        "fact": fact,
        "source_candidates": [row["document_ordinal"] for row in comparable],
    }


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
        }
        observations = filings.get(acc, {}).get("observations", [])
        match = join(fact, observations, "0000059478")
        if match["status"] != "VERIFIED_SOURCE_FACT":
            return {"status": match["status"], "accession": acc, "target": fact}
        row = next(
            r for r in observations if r["document_ordinal"] == match["source_candidates"][0]
        )
        interval = rounding(Decimal(value), row["decimals"])
        if interval is None:
            return {
                "status": "MISSING_EVIDENCE",
                "accession": acc,
                "reason": "DECIMALS_UNAVAILABLE",
            }
        intervals.append(interval)
        items.append(
            {"accession": acc, "ordinal": row["document_ordinal"], "decimals": row["decimals"]}
        )
    direct, ytd, half = intervals
    derived = ytd[0] - half[1], ytd[1] - half[0]
    overlap = max(direct[0], derived[0]) <= min(direct[1], derived[1])
    return {
        "status": "CANDIDATE_EQUIVALENCE" if overlap else "CONFLICT",
        "inputs": items,
        "direct_interval": list(map(str, direct)),
        "derived_interval": list(map(str, derived)),
        "review_required": [
            "same Reporting Entity and disclosure scope",
            "H1-to-9M comparability",
            "rounding interval boundary and possible restatement",
        ],
    }


def source_metadata(rows):
    """Bind the pilot observations to the existing verified Bulk generation."""
    scope = pilot.pilot_spec()
    issuers = [i for i in scope.active if i["cik"] in {row["cik"] for row in rows}]
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
    return issuers, paths, excluded, sources, manifest


def companyfacts(rows, metadata):
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
            pattern = re.compile(FOCUS.get(symbol, "."), re.IGNORECASE)
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


def draft(rows, root, facts):
    results = {}
    for row in rows:
        acc, cik = row["accession"], row["cik"]
        folder = root / cik / acc
        observations, alternatives, errors = [], [], []
        index_source = None
        if bulk.exists(folder / "index.json"):
            listing, index_source = cache_read(folder / "index.json", row, "index.json")
            for name in documents(row, json.loads(listing)):
                if not bulk.exists(folder / name):
                    errors.append({"document": name, "reason": "MISSING_CACHED_DOCUMENT"})
                    continue
                data, meta = cache_read(folder / name, row, name)
                try:
                    pattern = FOCUS.get(row["symbol"])
                    parsed = [
                        fact
                        for fact in extract(data)
                        if not pattern
                        or re.search(
                            pattern, (fact["concept"] or "").rsplit("}", 1)[-1], flags=re.IGNORECASE
                        )
                    ]
                except (ET.ParseError, UnicodeError, ValueError) as error:
                    errors.append({"document": name, "reason": str(error)})
                    continue
                for fact in parsed:
                    fact["source"] = meta
                if not observations and parsed:
                    observations = parsed
                    alternatives.append(
                        {"source": meta, "observation_count": len(parsed), "selected": True}
                    )
                else:
                    alternatives.append({"source": meta, "observations": parsed, "selected": False})
        joins = [
            join(fact, observations, cik)
            | {
                "companyfacts_source": source,
                "filing_date": row.get("filed"),
                "acceptance": row.get("acceptance"),
            }
            for fact, source in facts.get((cik, acc), [])
        ]
        for item in joins:
            if not row.get("filed") or row["filed"] != item["fact"].get("filed"):
                item["status"] = "UNRESOLVED_CONTEXT"
                item["reason"] = "FILING_DATE_BINDING_UNAVAILABLE_OR_INCONSISTENT"
        results[cik, acc] = row | {
            "status": "UNRESOLVED_CONTEXT" if observations else "MISSING_EVIDENCE",
            "observations": observations,
            "companyfacts_joins": joins,
            "documents": alternatives,
            "index_source": index_source,
            "issues": errors,
        }
    lly = {acc: record for (cik, acc), record in results.items() if cik == "0000059478"}
    return {
        "contract": "F01_ORIGINAL_FILING_EVIDENCE_DRAFT_V1",
        "review_required": True,
        "semantic_evidence": {
            "contexts": [],
            "acceptance": [],
            "amendments": [],
            "debt_coverage": [],
        },
        "filings": list(results.values()),
        "lly_rounding": lly_rounding(lly),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--download", action="store_true")
    modes.add_argument("--resume", action="store_true")
    modes.add_argument("--extract", action="store_true")
    parser.add_argument("--authorize-download", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument("--include-additional", action="store_true")
    parser.add_argument(
        "--inventory-file",
        type=Path,
        help="Custom filing metadata list. Only the built-in inventory has Companyfacts joins.",
    )
    parser.add_argument("--output", default="f01-original-filing-evidence-draft.json")
    args = parser.parse_args(argv)
    online = args.download or args.resume
    if online != args.authorize_download or (online and not args.user_agent):
        parser.error("Network modes require --authorize-download and --user-agent")
    try:
        candidate = pilot.code_binding()  # Enforce the branch before any mode runs.
        if args.inventory_file:
            _, rows = bulk.read_metadata(args.inventory_file.absolute())
            metadata = None
        else:
            rows = inventory()
            if not args.include_additional and (online or args.extract):
                rows = [r for r in rows if r["tier"] == "INITIAL"]
            metadata = source_metadata(rows)
        validate_rows(rows)
        if online:
            acquire(rows, safe_directory(ROOT), bulk.SecHttp(args.user_agent))
        elif args.extract:
            require(
                NAME.fullmatch(args.output)
                and ".." not in args.output
                and args.output.endswith(".json"),
                "INVALID_DRAFT_FILENAME",
            )
            directory = safe_directory(ROOT)
            target = directory / args.output
            require(not bulk.exists(target), "EVIDENCE_OUTPUT_EXISTS")
            value = draft(rows, directory, companyfacts(rows, metadata) if metadata else {})
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
                    },
                    indent=2,
                )
            )
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"FILING_EVIDENCE_BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
