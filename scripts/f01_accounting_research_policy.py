"""Executable, opt-in Research evidence predicates and independent approval binding."""

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

import audit_f01_accounting_semantic_qualification as audit

from trading_system.fundamentals.semantic import ISSUER_SCOPE, DebtCoverage, ReportingContext

f = audit.filings
VERSION = "F01_ACCOUNTING_RESEARCH_POLICY_V1"
APPROVAL = "F01_ACCOUNTING_RESEARCH_POLICY_APPROVAL_V1"
ROOTS = ("DebtCurrent", "LongTermDebtAndCapitalLeaseObligations")
SCOPE = "CONSOLIDATED_CARRYING_BORROWINGS_INCLUDING_FINANCE_LEASES"
CALCS = {
    "http://www.xbrl.org/2003/arcrole/summation-item",
    "https://xbrl.org/2023/arcrole/summation-item",
}

RULES = {
    "DEBT_DIRECT": {
        "scope": SCOPE,
        "evidence": "Latest eligible precise GAAP aggregate with original join.",
        "conflicts": "Preserve conflicting observations. No fallback to an older period.",
    },
    "DEBT_CLOSED_NOTE_PARTITION": {
        "scope": SCOPE,
        "evidence": "Complete original carrying-debt schedule, two reconciled disjoint subtotals, "
        "bound Calculation role and explicit Commercial Paper and finance-lease treatment.",
        "conflicts": "Reject ambiguous roles, missing contributors and overlapping branches.",
    },
    "COMMERCIAL_PAPER": {
        "scope": "CURRENT_CARRYING_BORROWINGS",
        "evidence": "Book amount in the current branch or separately closed borrowing perimeter.",
        "conflicts": "Count each exposure once. Face value and unused capacity are excluded.",
    },
    "FINANCE_LEASE": {
        "scope": "SAME_BALANCE_SEPARATE_FINANCE_LEASE_LIABILITY",
        "evidence": "Direct liability or explicit inclusion in the closed debt branches.",
        "conflicts": "Operating leases are separate. Missing is unresolved. Count each lease once.",
    },
    "CLASS_ISSUER_SCOPE": {
        "scope": "LISTED_CLASS_ONLY_UNLESS_ISSUER_ECONOMIC_SCOPE_PROVEN",
        "evidence": "Exact cover and listing. Issuer use requires complete claims and price.",
        "conflicts": "Keep class-only shares out of issuer Market Cap and EV.",
    },
    "HOMOGENEOUS_FLOWS": {
        "scope": "EXISTING_RESEARCH_ACCOUNTING_SCOPES",
        "evidence": "Exact concept, original acceptance and compatible quarter predecessors.",
        "conflicts": "Missing quarters and incompatible cash flow or EBIT scopes stay unresolved.",
    },
    "EXISTING_DERIVED_METRICS": {
        "scope": "TRAWALP_EBITDA_AND_ROIC",
        "evidence": "Aligned EBITDA bridge or comparably scoped invested capital for both periods.",
        "conflicts": "No new formula and no fill of a missing or economically incompatible input.",
    },
}


def note_totals(text, instant):
    """Read only the narrowly recognized complete carrying-debt disclosure family."""
    values = set()
    heading = r"summary of total outstanding debt as of ([A-Za-z]+ \d{1,2}, \d{4})"
    for match in re.finditer(heading, text, re.I):
        if datetime.strptime(match[1], "%B %d, %Y").date() != date.fromisoformat(instant):
            continue
        section = text[match.end() : match.end() + 3400]
        current = re.search(
            r"Total current portion of long-term debt and bank borrowings\s*\$?\s*([\d,]+)",
            section,
            re.I,
        )
        later = re.search(r"Total long-term debt\s*\$?\s*([\d,]+)", section, re.I)
        carrying = re.search(r"Total carrying amount\s*\$?\s*([\d,]+)", section, re.I)
        leases = re.search(
            r"\((\d+)\) Amounts primarily include finance lease liabilities\.", section, re.I
        )
        if (
            not current
            or not later
            or not carrying
            or not leases
            or not (current.end() < carrying.start() < later.start() < leases.start())
        ):
            continue
        first = section[: current.start()]
        second = section[current.end() : later.start()]
        footnote = rf"\bOther\s*\({leases[1]}\)"
        if re.search(r"\bCommercial paper\s*\(", first, re.I) and all(
            re.search(footnote, part, re.I) for part in (first, second)
        ):
            values.add(tuple(int(item[1].replace(",", "")) for item in (current, later, carrying)))
    return next(iter(values)) if len(values) == 1 else None


def descendants(root, links, visited=()):
    if root in visited:
        return None
    found = {root}
    for child in sorted(links[root]):
        branch = descendants(child, links, (*visited, root))
        if branch is None or branch & found:
            return None
        found.update(branch)
    return found


def exact_role_partition(rows, roots):
    """Use the existing Arelle Calculation arcs as topology and arithmetic witnesses."""
    index, roles = defaultdict(list), defaultdict(set)
    for row in rows:
        index[row["concept"]].append(row)
        for arc in row["relationships"]:
            if arc["arcrole"] in CALCS and arc.get("source_sha256"):
                roles[arc["linkrole"], arc["source_sha256"]].add(
                    (arc["from"], arc["to"], arc["weight"])
                )
    good = []
    for role, arcs in sorted(roles.items()):
        links = defaultdict(set)
        for parent, child, _ in arcs:
            links[parent].add(child)
        first, second = (descendants(root, links) for root in roots)
        if not first or not second or first & second:
            continue
        # Only these separately reported borrowing branches are currently covered by the note test.
        first_tags, second_tags = (
            {name.rsplit("}", 1)[-1] for name in links[root]} for root in roots
        )
        if first_tags != {"CommercialPaper", "OtherLongTermDebtCurrent"} or second_tags != {
            "LongTermDebt",
            "OtherLongTermDebt",
        }:
            continue
        participating = [arc for arc in arcs if arc[0] in roots]
        if len(participating) != 4 or any(
            weight is None or Decimal(weight) != 1 for _, _, weight in participating
        ):
            continue
        pick = {
            key: {Decimal(row["value"]) for row in index[key]}
            for key in set(roots) | links[roots[0]] | links[roots[1]]
        }
        if all(
            len(v) == 1 and all(value.is_finite() and value >= 0 for value in v)
            for v in pick.values()
        ) and all(
            next(iter(pick[root])) == sum(next(iter(pick[key])) for key in links[root])
            for root in roots
        ):
            proof_rows = [audit.source_ref(row) for key in sorted(pick) for row in index[key]]
            good.append(
                {
                    "role": role[0],
                    "linkbase_sha256": role[1],
                    "arcs": sorted(participating),
                    "witnesses": proof_rows,
                }
            )
    return good[0] if len(good) == 1 else None


def qualify_note(row, facts, instant):
    available = {
        tag: [
            fact
            for fact in facts
            if fact.tag == tag
            and fact.period_start is None
            and fact.period_end.isoformat() == instant
            and fact.unit == "USD"
        ]
        for tag in ROOTS
    }
    if any(len(values) != 1 for values in available.values()):
        return None
    current, noncurrent = (available[tag][0] for tag in ROOTS)
    pair = current, noncurrent
    if any(fact.accession_number != row["accession"] or fact.cik != row["cik"] for fact in pair):
        return None
    # Bind the original note to the exact Companyfacts selections.
    rows = [
        item
        for item in row["observations"]
        if (ctx := item.get("context"))
        and ctx["instant"] == instant
        and not ctx["opaque_context"]
        and (
            not ctx["dimensions"]
            or (
                f.taxonomy(item["concept"]) == ("us-gaap", "LongTermDebt")
                and len(ctx["dimensions"]) == 1
                and f.taxonomy(ctx["dimensions"][0].get("axis"))
                == ("us-gaap", "LongtermDebtTypeAxis")
                and f.taxonomy(ctx["dimensions"][0].get("member"))
                == ("us-gaap", "SeniorNotesMember")
                and not ctx["dimensions"][0].get("is_default")
            )
        )
        and not item.get("evidence_issue")
        and ctx["scheme"] in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
        and f.bulk.matches_cik(ctx["entity"], row["cik"])
        and item["value"] is not None
        and f.unit_matches(item["unit"], "USD")
        and item.get("source", {}).get("accession") == row["accession"]
    ]
    roots = [
        {
            o["concept"]
            for o in rows
            if f.taxonomy(o["concept"]) == ("us-gaap", fact.tag)
            and Decimal(o["value"]) == fact.value
        }
        for fact in pair
    ]
    if any(len(names) != 1 for names in roots):
        return None
    root_names = tuple(next(iter(names)) for names in roots)
    proof = exact_role_partition(rows, root_names)
    if proof is None:
        return None
    text, source = audit.original_text(row)
    if any(o["source"]["sha256"] != source["sha256"] for o in proof["witnesses"]):
        return None
    totals = note_totals(text, instant)
    if totals is None:
        return None
    carrying = [
        o
        for o in rows
        if f.taxonomy(o["concept"]) == ("us-gaap", "LongTermDebt")
        and o["source"]["sha256"] == source["sha256"]
    ]
    if not carrying or any(
        not re.fullmatch(r"[\d,]+", o["raw_text"])
        or int(o["raw_text"].replace(",", "")) != totals[2]
        for o in carrying
    ):
        return None
    matched = [
        next(
            (
                o
                for o in rows
                if o["concept"] == name
                and o["source"]["sha256"] == source["sha256"]
                and Decimal(o["value"]) == fact.value
                and re.fullmatch(r"[\d,]+", o["raw_text"])
                and int(o["raw_text"].replace(",", "")) == number
            ),
            None,
        )
        for name, fact, number in zip(root_names, pair, totals[:2], strict=True)
    ]
    if not all(matched):
        return None
    ref = f"sha256:{source['sha256']}#closed-debt-schedule/{instant}"
    contexts = {
        fact: ReportingContext(
            fact.cik,
            ISSUER_SCOPE,
            audit.report_context(fact.model_dump(mode="json"), original)["context"]["reference"],
        )
        for fact, original in zip(pair, matched, strict=True)
    }
    return (
        DebtCoverage(current, noncurrent, ref),
        contexts,
        proof | {"note": source, "reference": ref, "scope": SCOPE, "period": instant},
    )


def validate_approval(template, approval):
    """Check an independent review contract without activating the policy."""
    require = f.require
    binding = template["binding"]
    require(
        binding["policy"] == VERSION
        and binding["required_lineage"] == "RESEARCH_ONLY"
        and binding["rules_sha256"] == f.sha(f.bulk.json_bytes(RULES))
        and binding["expected_coverage_sha256"]
        == f.sha(f.bulk.json_bytes(template["expected_coverage"])),
        "POLICY_TEMPLATE_DRIFT",
    )
    require(
        approval.get("contract") == APPROVAL and approval.get("binding") == binding,
        "POLICY_APPROVAL_BINDING_DRIFT",
    )
    reviewer = approval.get("reviewer")
    require(
        isinstance(reviewer, str) and bool(reviewer.strip()), "INDEPENDENT_POLICY_REVIEW_REQUIRED"
    )
    decisions = approval.get("decisions", {})
    require(
        set(decisions) == set(RULES) and set(decisions.values()) <= {"APPROVE_RESEARCH", "DENY"},
        "POLICY_DECISIONS_INCOMPLETE",
    )
    accepted = {name for name, decision in decisions.items() if decision == "APPROVE_RESEARCH"}
    prereqs = {"COMMERCIAL_PAPER", "FINANCE_LEASE"}
    require(
        "DEBT_CLOSED_NOTE_PARTITION" not in accepted or prereqs <= accepted,
        "DEBT_PARTITION_POLICY_PREREQUISITES",
    )
    return sorted(accepted)


def canonical_handoff(template, approval, lineage, import_binding, approval_sha256):
    """Prepare the immutable sidecar binding for a future Research-only importer call."""
    accepted = validate_approval(template, approval)
    binding = template["binding"]
    f.require(
        lineage["classification"] == binding["required_lineage"] == "RESEARCH_ONLY"
        and lineage["dataset_id"] == import_binding["dataset_id"]
        and lineage["manifest_sha256"] == import_binding["bootstrap_manifest_sha256"]
        and import_binding["manifest_sha256"] == binding["bulk_source_manifest_sha256"]
        and import_binding["candidate"]["fingerprint"] == binding["candidate_fingerprint"],
        "RESEARCH_POLICY_IMPORT_SCOPE_DRIFT",
    )
    return {
        "dataset_id": lineage["dataset_id"],
        "bootstrap_manifest_sha256": lineage["manifest_sha256"],
        "import_binding_sha256": f.sha(f.bulk.json_bytes(import_binding)),
        "policy_binding": binding,
        "approval_sha256": approval_sha256,
        "approved_research_rules": accepted,
        "policy_activated": False,
    }
