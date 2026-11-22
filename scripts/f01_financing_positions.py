"""Source-bound financing disclosures for the existing Research accounting replay."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

import audit_f01_accounting_semantic_qualification as audit

f = audit.filings
VERSION = "F01_RESEARCH_FINANCING_POSITIONS_V1"
TERM_SUBTOTAL = "current_and_noncurrent_term_debt_carrying_subtotal_v1"
PRESENTATION = "http://www.xbrl.org/2003/arcrole/parent-child"
TAGS = {
    "ShortTermBorrowings": "REPORTED_COMPONENT",
    "DebtCurrent": "REPORTED_AGGREGATE",
    "LongTermDebtCurrent": "REPORTED_COMPONENT",
    "LongTermDebtNoncurrent": "REPORTED_COMPONENT",
    "LongTermDebt": "REPORTED_AGGREGATE",
    "LongTermDebtAndCapitalLeaseObligationsCurrent": "REPORTED_AGGREGATE",
    "LongTermDebtAndCapitalLeaseObligations": "REPORTED_AGGREGATE",
    "LongTermDebtAndFinanceLeaseObligationsCurrent": "REPORTED_AGGREGATE",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent": "REPORTED_AGGREGATE",
    "CommercialPaper": "REPORTED_COMPONENT",
    "FinanceLeaseLiability": "REPORTED_AGGREGATE",
    "DebtLongtermAndShorttermCombinedAmount": "REPORTED_AGGREGATE",
}
TERM_PAIR = ("LongTermDebtCurrent", "LongTermDebtNoncurrent")
POSITION_TAGS = TAGS.keys() | {"Assets"}
RULES = {
    "DIRECT_POSITION": {
        "tags": TAGS,
        "scope": "Exact US GAAP carrying concept on the bound consolidated Assets context.",
        "conflicts": "Differing observations for the same whole entity concept leave it open.",
    },
    "TERM_SUBTOTAL": {
        "metric": TERM_SUBTOTAL,
        "components": TERM_PAIR,
        "evidence": "Complementary term debt on one balance role, or a reconciled direct "
        "LongTermDebt aggregate on the same context.",
        "scope": "Term debt only. No separate short-term debt, commercial paper or finance "
        "lease liability is imputed. The subtotal cannot supply total_debt or ROIC.",
    },
    "TOTAL_DEBT": {
        "policy": "F01_ACCOUNTING_RESEARCH_POLICY_V1",
        "scope": "Consolidated carrying borrowings, including finance leases once.",
        "evidence": "Existing reported aggregate or closed, disjoint borrowing perimeter.",
    },
}


def amount(item):
    try:
        value = Decimal(str(item.get("value")))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def index_filing(row, shard_sha256, periods, observations):
    """Keep only needed whole-entity USD observations from an already verified shard."""
    result = defaultdict(lambda: defaultdict(list))
    sources = {(item["sha256"], item["filename"]): item for item in row["cached_sources"]}
    for item in observations:
        taxonomy, tag = f.taxonomy(item["concept"])
        ctx = item.get("context")
        source = item.get("source", {})
        if (
            taxonomy != "us-gaap"
            or tag not in POSITION_TAGS
            or not ctx
            or ctx["instant"] not in periods
            or ctx["dimensions"]
            or ctx["opaque_context"]
            or ctx["scheme"] not in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
            or not f.bulk.matches_cik(ctx["entity"], row["cik"])
            or item.get("evidence_issue")
            or not f.unit_matches(item["unit"], "USD")
            or amount(item) is None
            or source.get("accession") != row["accession"]
            or source.get("acceptance") != row["acceptance"]
            or sources.get((source.get("sha256"), source.get("filename"))) != source
        ):
            continue
        refs = audit.source_ref(item) | {
            "filing_shard_sha256": shard_sha256,
            "presentation": sorted(
                {
                    (arc["linkrole"], arc["source_sha256"])
                    for arc in item["relationships"]
                    if arc["arcrole"] == PRESENTATION
                    and arc.get("source_sha256")
                    and arc["to"] == item["concept"]
                }
            ),
        }
        result[ctx["instant"]][tag].append(refs)
    return result


def aspects(item):
    context = item["context"]
    return item["source"]["sha256"], item["context_id"], context["scheme"], context["entity"]


def reported_positions(index, assets, decision_at):
    """Select facts tied to the actual Assets witnesses, preserving original tags and scopes."""
    keys = {(item["source"]["accession"], item["context"]["instant"]) for item in assets}
    if len(keys) != 1:
        return {}
    accession, period = next(iter(keys))
    rows = index.get((accession, period), {})
    anchor = {aspects(item) for item in assets}
    eligible = {}
    for tag, originals in rows.items():
        available = [
            item
            for item in originals
            if datetime.fromisoformat(item["source"]["acceptance"]) <= decision_at
        ]
        exact = [item for item in available if aspects(item) in anchor]
        if not exact:
            continue
        equivalent = {(item["concept"], amount(item)) for item in available}
        unique = len(equivalent) == 1
        reported = amount(exact[0])
        selected = unique and reported >= 0
        status = TAGS.get(tag, "BALANCE_SCOPE_WITNESS")
        if not selected:
            status = "NONNEGATIVE_CARRYING_AMOUNT_REQUIRED" if unique else "CONFLICT"
        eligible[tag] = {
            "value": str(reported) if selected else None,
            "status": status,
            "sources": sorted(
                exact if selected else available,
                key=lambda item: (item["source"]["sha256"], item["document_ordinal"]),
            ),
        }
    return eligible


def term_subtotal(positions):
    direct = positions.get("LongTermDebt")
    parts = [positions.get(tag, {}) for tag in TERM_PAIR]
    blocked = {"value": None, "status": "INCOMPLETE_AGGREGATE"}
    if any(item.get("value") is None for item in parts):
        return blocked | {"reason": "COMPLEMENTARY_TERM_COMPONENTS_REQUIRED"}
    left, right = parts
    common_context = {aspects(item) for item in left["sources"]} & {
        aspects(item) for item in right["sources"]
    }
    if not common_context:
        return blocked | {"reason": "NO_COMMON_TERM_CONTEXT"}
    term_namespaces = {
        item["concept"].split("}", 1)[0] for part in parts for item in part["sources"]
    }
    if len(term_namespaces) != 1:
        return blocked | {"reason": "INCOMPATIBLE_TERM_TAXONOMIES"}
    value = Decimal(left["value"]) + Decimal(right["value"])
    if direct and (direct["value"] is None or Decimal(direct["value"]) != value):
        return blocked | {"reason": "REPORTED_TERM_AMOUNT_CONFLICT"}
    assets = positions.get("Assets", {}).get("sources", ())
    context_roles = [
        {
            tuple(role)
            for item in group
            if aspects(item) in common_context
            for role in item["presentation"]
        }
        for group in (assets, left["sources"], right["sources"])
    ]
    roles = sorted(set.intersection(*context_roles))
    reconciled = direct and any(
        aspects(item) in common_context and item["concept"].split("}", 1)[0] in term_namespaces
        for item in direct["sources"]
    )
    if not roles and not reconciled:
        return blocked | {"reason": "BALANCE_ROLE_OR_RECONCILIATION_REQUIRED"}
    return {
        "value": str(value),
        "status": "VERIFIED_AGGREGATE",
        "reason": "RECONCILED_REPORTED_TERM_DEBT" if reconciled else "ALIGNED_BALANCE_COMPONENTS",
        "component_tags": TERM_PAIR,
        "reconciled_tag": "LongTermDebt" if reconciled else None,
        "balance_roles": roles,
    }
