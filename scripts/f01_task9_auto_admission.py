"""Task 9.2 deterministic admission from bound original-filing evidence."""

from collections import Counter
from datetime import date
from decimal import InvalidOperation
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import f01_task9_admission as review
import prepare_f01_filing_evidence as filing
from prepare_f01_historical_50_batch import sha

CONTRACT = "F01_AUTOMATED_ADMISSION_V1"
RULESET_ID = "F01_ORIGINAL_CONSOLIDATED_BALANCE"
RULESET_VERSION = "1"
RULESET_SHA256 = sha(Path(__file__).read_bytes())
DEI = ("EntityCentralIndexKey", "DocumentType", "DocumentPeriodEndDate", "AmendmentFlag")
require = filing.require


def proof(item):
    return {
        "observation_sha256": review.observation_id(item),
        **{
            k: item.get(k)
            for k in (
                "concept",
                "source_document",
                "source_line",
                "context_id",
                "context",
                "unit",
                "decimals",
                "value",
                "text_value",
            )
        },
        "original_sha256": (item.get("source") or {}).get("sha256"),
    }


def original(item, document):
    return (
        item.get("source") == document
        and item.get("source_document") == document["url"]
        and type(item.get("source_line")) is int
        and item["source_line"] > 0
        and not item.get("evidence_issue")
    )


def source_checks(row, source, shard, closure, entrypoints):
    checks = {}
    checks["FILING_METADATA"] = False
    try:
        filed = date.fromisoformat(row["filed"])
        report = date.fromisoformat(row["report_date"])
        accepted = review.instant(row["acceptance"])
        checks["FILING_METADATA"] = (
            row["form"] in {"10-K", "10-Q"}
            and report <= filed
            and report <= accepted.date()
            and abs((filed - accepted.date()).days) <= 1
        )
    except (KeyError, ValueError, TypeError):
        pass

    records = source["documents"]
    urls = {item["url"]: item["sha256"] for item in records}
    checks["VERIFIED_SOURCE_SET"] = (
        records == shard["source_documents"]
        and len(urls) == len(records)
        and {"index.json", row["primary"]} <= {item["filename"] for item in records}
    )
    selected = [doc for doc in shard["documents"] if doc["selected"]]
    document = selected[0]["source"] if len(selected) == 1 else None
    checks["SELECTED_ORIGINAL_XBRL"] = bool(
        document
        and document in records
        and document["filename"] in entrypoints
        and selected[0]["format"] in {"INLINE_XBRL", "XBRL_DOCUMENT"}
    )
    dts = selected[0]["taxonomy_documents"] if document else []
    externals = {
        url: entry["source"]["sha256"]
        for url, entry in closure.items()
        if entry["status"] == "BOUND"
    }
    checks["ACTUAL_DTS_BOUND"] = (
        bool(externals)
        and len(externals) == len(closure)
        and bool(dts)
        and any(urldefrag(dep["uri"])[0] in externals for dep in dts)
        and all(
            dep.get("sha256") and (urls | externals).get(urldefrag(dep["uri"])[0]) == dep["sha256"]
            for dep in dts
        )
    )
    checks["EXTRACTION_COMPLETE"] = (
        shard["contract"] == "F01_TASK9_ARELLE_FILING_V1"
        and shard["status"] == "EXTRACTION_COMPLETE"
        and shard["filing"] == review.filing_key(row)
        and shard["runtime"]["version"] == filing.ARELLE_VERSION
        and all(
            issue.get("category") == "INCOMPATIBLE_DOCUMENT_FORMAT" for issue in shard["issues"]
        )
    )
    witnesses = {tag: [] for tag in DEI}
    if checks["SELECTED_ORIGINAL_XBRL"]:
        for item in shard["observations"]:
            namespace, tag = (item.get("concept") or "").lstrip("{").partition("}")[::2]
            if (
                tag in witnesses
                and filing.taxonomy(item.get("concept")) == ("dei", tag)
                and namespace.startswith(("http://xbrl.sec.gov/", "https://xbrl.sec.gov/"))
            ):
                witnesses[tag].append(item)
    expected = dict(zip(DEI, (row["cik"], row["form"], row["report_date"], "false"), strict=True))
    for tag, items in witnesses.items():
        checks[f"DEI_{tag}"] = (
            len(items) == 1
            and original(items[0], document)
            and items[0].get("is_nil") is False
            and (items[0].get("text_value") or "").strip() == expected[tag]
            and entity_context(items[0], row, instant_only=False)
        )
    checks["US_GAAP_MEMBERSHIP"] = bool(
        document
        and any(
            filing.taxonomy(item.get("concept"))[0] == "us-gaap"
            and original(item, document)
            and filing.bulk.matches_cik((item.get("context") or {}).get("entity"), row["cik"])
            for item in shard["observations"]
        )
    )
    evidence = {tag: [proof(item) for item in items] for tag, items in witnesses.items()}
    return checks, evidence, document


def entity_context(item, row, *, instant_only):
    ctx = item.get("context") or {}
    return (
        ctx.get("scheme") in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
        and filing.bulk.matches_cik(ctx.get("entity"), row["cik"])
        and ctx.get("dimensions") == []
        and ctx.get("opaque_context") is False
        and (ctx.get("instant") if instant_only else ctx.get("instant") or ctx.get("endDate"))
        == row["report_date"]
    )


def balance_roles(item, source):
    hashes = {entry["url"]: entry["sha256"] for entry in source["documents"]}
    roles = []
    for arc in item.get("relationships", []):
        role = urlsplit(arc.get("linkrole") or "").path.rsplit("/", 1)[-1].lower()
        if (
            "consolidatedbalancesheet" in role
            and "nonconsolidated" not in role
            and "unconsolidated" not in role
            and "parent" not in role
            and arc.get("arcrole") == "http://www.xbrl.org/2003/arcrole/parent-child"
            and arc.get("to") == item["concept"]
            and arc.get("source_sha256")
            and hashes.get(arc.get("source_document")) == arc["source_sha256"]
            and type(arc.get("source_line")) is int
            and arc["source_line"] > 0
        ):
            roles.append(arc)
    return roles


def qualify_fact(row, item, document, source, metric):
    role = balance_roles(item, source)
    if not document or not original(item, document):
        return "ORIGINAL_REFERENCE_UNBOUND", None, role
    if not entity_context(item, row, instant_only=True):
        return "ENTITY_PERIOD_OR_DIMENSIONS", None, role
    if item.get("is_nil") is not False or not isinstance(item.get("value"), str):
        return "NIL_OR_NONDECIMAL", None, role
    if not role:
        return "CONSOLIDATED_BALANCE_ROLE_UNBOUND", None, role
    try:
        fact = review.fact_from_original(row, item, metric)
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return "UNIT_VALUE_OR_CONTEXT", None, role
    return ("ELIGIBLE", fact, role) if fact.value > 0 else ("NONPOSITIVE_VALUE", None, role)


def evaluate_filing(row, source, shard, closure, entrypoints):
    checks, dei, document = source_checks(row, source, shard, closure, entrypoints)
    source_ok = all(checks.values())
    groups = {metric: [] for metric in review.METRICS.values()}
    for item in shard["observations"]:
        namespace, tag = filing.taxonomy(item.get("concept"))
        if namespace == "us-gaap" and tag in review.METRICS:
            groups[review.METRICS[tag]].append(item)
    decisions = {}
    for metric, observations in groups.items():
        candidates = []
        valid = []
        same_aspect = 0
        for item in observations:
            same_aspect += entity_context(item, row, instant_only=True) and filing.unit_matches(
                item.get("unit"), "USD"
            )
            reason, fact, roles = qualify_fact(row, item, document, source, metric)
            evidence = proof(item) | {"reason": reason, "consolidated_roles": roles}
            candidates.append(evidence)
            if fact is not None:
                valid.append((fact, evidence))
        reason = "SOURCE_BLOCKED" if not source_ok else "NO_ELIGIBLE_ORIGINAL"
        admitted = None
        if source_ok and len(valid) == 1 and same_aspect == 1:
            fact, evidence = valid[0]
            admitted = {
                "fact": fact.model_dump(mode="json"),
                "original": evidence,
                "version": "AS_FILED",
                "scope": "CONSOLIDATED",
                "fallback": "NONE",
            }
            reason = "UNIQUE_ORIGINAL"
        elif source_ok and same_aspect > 1:
            reason = "DUPLICATE_OR_CONFLICTING_ORIGINALS"
        decisions[metric] = {"reason": reason, "admitted": admitted, "candidates": candidates}
    return {
        "source": {
            "decision": "AUTO_ADMITTED" if source_ok else "BLOCKED",
            "checks": checks,
            "failures": [key for key, passed in checks.items() if not passed],
            "dei": dei,
            "selected_original": document,
            "membership": ["us-gaap"] if source_ok else [],
            "filing_version": "ORIGINAL_FILING" if source_ok else None,
        },
        "accounting": decisions,
    }


def batch_report(filings, binding):
    reasons = Counter()
    metrics = Counter()
    candidate_reasons = Counter()
    checked_metrics = Counter()
    excluded_metrics = Counter()
    facts_checked = 0
    facts_admitted = 0
    admitted_filings = 0
    for record in filings.values():
        reasons.update(record["source"]["failures"])
        admitted = False
        for metric, result in record["accounting"].items():
            reasons[result["reason"]] += 1
            candidate_reasons.update(item["reason"] for item in result["candidates"])
            facts_checked += len(result["candidates"])
            checked_metrics[metric] += len(result["candidates"])
            excluded_metrics[metric] += len(result["candidates"]) - int(
                result["admitted"] is not None
            )
            if result["admitted"] is not None:
                facts_admitted += 1
                metrics[metric] += 1
                admitted = True
        admitted_filings += admitted
    report = {
        "contract": CONTRACT,
        "decision_authority": "AUTOMATED",
        "ruleset": {"id": RULESET_ID, "version": RULESET_VERSION, "sha256": RULESET_SHA256},
        "binding": binding,
        "status": "AUTO_ADMISSION_READY"
        if admitted_filings == len(filings) and filings
        else "BLOCKED",
        "filings": filings,
        "summary": {
            "filings_checked": len(filings),
            "filings_admitted": admitted_filings,
            "filings_quarantined": len(filings) - admitted_filings,
            "facts_checked": facts_checked,
            "facts_admitted": facts_admitted,
            "facts_excluded": facts_checked - facts_admitted,
            "checked_by_metric": dict(checked_metrics),
            "excluded_by_metric": dict(excluded_metrics),
            "admitted_by_metric": dict(metrics),
            "decisions_by_reason": dict(reasons),
            "candidates_by_reason": dict(candidate_reasons),
        },
    }
    return report | {"evidence_sha256": sha(review.json_bytes(report))}


def require_current_report(provided, recomputed):
    require(
        provided == recomputed
        and recomputed["binding"]["current_selection"]
        and recomputed["status"] == "AUTO_ADMISSION_READY",
        "AUTOMATED_ADMISSION_CHANGED_OR_BLOCKED",
    )
