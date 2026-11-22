"""Replay the bound pilot with reviewable Accounting rules and original-fact traces."""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import audit_f01_accounting_semantic_qualification as audit
import preview_f01_advanced_accounting as advanced

from trading_system.fundamentals.metrics import MAX_SHARES_OUTSTANDING_AGE
from trading_system.fundamentals.semantic import POLICY, Evidence, research_accounting_as_of
from trading_system.models.fundamentals import FundamentalFact

f = audit.filings
require = f.require
PREVIEW_SHA = "653b46434d6cf60ec3253c35be95d2ad7ec6c33920c19adea7f2aa50ca0b2382"
VERSION = "F01_ACCOUNTING_RESEARCH_RULES_V1"
RULES = {
    "SHARES_AGGREGATE": "Issuer-level shares instant with unit and freshness guards.",
    "SHARES_CLASS_NET": "Bound listing and explicit class with included issuer holdings.",
    "DEBT_REPORTED": "Direct combined total for the latest eligible Debt period.",
    "DEBT_COMPONENTS": "Same aspects, no overlap and reconciliation to the reported total.",
    "FINANCE_LEASE_REPORTED": "Separate direct lease liability on the Assets period and filing.",
    "PARENT_INCOME": "Use NetIncomeLoss for a homogeneous parent-attributable TTM series.",
    "CONSOLIDATED_INCOME": "Use ProfitLoss for a homogeneous consolidated TTM series.",
    "OCF_TOTAL": "Total OCF series with exact cumulative predecessors and filing versions.",
    "CAPEX_PPE": "Currently eligible PPE cash outflow definitions as separate complete series.",
    "OPERATING_INCOME": "Reported OperatingIncomeLoss at the qualified statement scope.",
    "EBITDA_EXISTING": "Use the existing TTM D&A and EBITDA bridges with matching period ends.",
    "ROIC_EXISTING": "Existing NOPAT and average invested capital with both balance periods.",
}
METRICS = {
    "parent_net_income": "PARENT_INCOME",
    "net_income": "CONSOLIDATED_INCOME",
    "operating_cash_flow": "OCF_TOTAL",
    "capital_expenditures": "CAPEX_PPE",
    "operating_income": "OPERATING_INCOME",
}
INSTANT_METRICS = ("total_assets", "total_equity", "cash", "total_debt")


def net_class_shares(text, title, measured, gross):
    """Match the measured gross and its explicit included holdings in one cover clause."""
    pattern = (
        re.escape(title) + r"[^()]{0,180}?outstanding as of ([A-Za-z]+ \d{1,2}, \d{4}) "
        r"was ([\d,]+) \(which number includes ([\d,]+) issued shares held by the registrant\)"
    )
    matches = {
        (
            datetime.strptime(day, "%B %d, %Y").date(),
            int(total.replace(",", "")),
            int(held.replace(",", "")),
        )
        for day, total, held in re.findall(pattern, text, re.I)
    }
    if len(matches) != 1:
        return None
    ((day, total, held),) = matches
    return (
        (total - held, held) if day == measured and total == gross and 0 <= held < total else None
    )


def net_share_preview(row, selection, session):
    text, cover = audit.original_text(row)
    require(cover == selection["cover_source"], "COVER_SOURCE_DRIFT")
    share = selection["share_fact"]
    measured = date.fromisoformat(share["context"]["instant"])
    filed = date.fromisoformat(selection["filed"])
    accepted = datetime.fromisoformat(selection["reported_acceptance"])
    require(
        share["source"]["accession"] == cover["accession"]
        and selection["reported_acceptance"] == row["acceptance"],
        "CLASS_SHARE_BINDING_DRIFT",
    )
    proof = net_class_shares(text, selection["cover_title"], measured, Decimal(share["value"]))
    if proof is None or not (
        accepted <= close(session)
        and measured <= session
        and filed <= session
        and session - measured <= MAX_SHARES_OUTSTANDING_AGE
        and session - filed <= MAX_SHARES_OUTSTANDING_AGE
    ):
        return None
    net, held = proof
    require(
        Decimal(selection["registrant_held_share_count_candidate"].replace(",", "")) == held,
        "COVER_HOLDINGS_DRIFT",
    )
    return {
        "rule": "SHARES_CLASS_NET",
        "value": net,
        "included_issuer_holdings": held,
        "share_original": share,
        "listing_originals": selection["symbol_facts"],
        "cover_original": cover,
        "scope": "LISTED_CLASS_ONLY",
        "issuer_market_cap": None,
        "remaining_evidence": "Comparable class price and complete issuer economic share scope",
    }


def close(session):
    return datetime.combine(session, time(16), audit.NY)


def instant_lease(row, fact):
    ctx = fact.get("context")
    if not (
        f.taxonomy(fact["concept"]) == ("us-gaap", "FinanceLeaseLiability")
        and ctx
        and ctx["instant"]
        and not ctx["dimensions"]
        and not ctx["opaque_context"]
        and ctx["scheme"] in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
        and f.bulk.matches_cik(ctx["entity"], row["cik"])
        and not fact.get("evidence_issue")
        and fact.get("value") is not None
        and f.unit_matches(fact["unit"], "USD")
    ):
        return None
    amount = Decimal(fact["value"])
    return amount if amount.is_finite() and amount >= 0 else None


def lease_selection(leases, assets):
    matches = [
        item
        for key in {(fact.accession_number, fact.period_end.isoformat()) for fact in assets.sources}
        for item in leases.get(key, ())
    ]
    values = {Decimal(row["value"]) for row in matches}
    return {
        "rule": "FINANCE_LEASE_REPORTED",
        "value": str(next(iter(values))) if len(values) == 1 else None,
        "originals": sorted(
            matches, key=lambda item: (item["source"]["sha256"], item["document_ordinal"])
        ),
        "reason": "REPORTED"
        if len(values) == 1
        else "CONFLICT"
        if values
        else "NO_EXACT_LEASE_AGGREGATE",
    }


def lookup(source, derived, wanted):
    raw = audit.read_bound(source, audit.FILING_SHA)
    accounting = audit.read_bound(derived, audit.ACCOUNTING_SHA)
    require(
        accounting["inputs"]["filing_manifest_sha256"] == audit.FILING_SHA, "FILING_MANIFEST_DRIFT"
    )
    accepted, facts, leases, originals, shares = {}, defaultdict(set), defaultdict(list), {}, {}
    open_refs = defaultdict(list)
    for (entry, row), record in zip(
        f.extended_records(source, raw), accounting["filings"], strict=True
    ):
        require(entry["sha256"] == record["source_sha256"], "ACCOUNTING_SHARD_DRIFT")
        acc = row["accession"]
        if (
            not row["acceptance"]
            or row.get("availability_unverified")
            or row.get("availability_conflict")
        ):
            continue
        accepted[acc] = datetime.fromisoformat(row["acceptance"])
        if acc in wanted:
            shares[acc] = row
        by_ordinal = {item["document_ordinal"]: item for item in row["observations"]}
        for item in by_ordinal.values():
            if instant_lease(row, item) is not None:
                leases[acc, item["context"]["instant"]].append(audit.source_ref(item))
        dei = [item for item in by_ordinal.values() if item["concept"] == audit.DEI]
        for result in row["companyfacts_joins"]:
            cf = result["fact"]
            if not cf["metric"] or cf["filed"] != row["filed"]:
                continue
            if (
                result["status"] == "MISSING_EVIDENCE"
                and cf["metric"] == "shares_outstanding"
                and dei
            ):
                result = f.join(cf, dei, row["cik"])
            fact = FundamentalFact.model_validate(cf)
            if result["status"] != "VERIFIED_SOURCE_FACT":
                open_refs[row["symbol"], fact.metric].append(fact)
                continue
            refs = tuple(
                audit.source_ref(item)
                for ordinal in result["whole_entity_candidates"]
                if (item := by_ordinal[ordinal])["value"] is not None
                and Decimal(item["value"]) == fact.value
            )
            require(refs, "VERIFIED_FACT_WITHOUT_EXACT_ORIGINAL")
            facts[row["symbol"]].add(fact)
            originals[fact] = {
                "matches": refs,
                "filing_shard": entry["sha256"],
                "acceptance": row["acceptance"],
            }
    return facts, originals, accepted, leases, shares, open_refs


class Trace:
    def __init__(self, originals):
        self.originals = originals
        self.registry = {}

    def keys(self, facts):
        refs = []
        for fact in facts:
            identity = fact.model_dump(mode="json")
            key = f.sha(f.bulk.json_bytes(identity))
            self.registry.setdefault(key, {"fact": identity, "original": self.originals.get(fact)})
            refs.append(key)
        return sorted(set(refs))

    def selection(self, pick):
        return {
            "value": str(pick.value) if pick.value is not None else None,
            "reason": pick.reason,
            "sources": self.keys(pick.sources),
            "blocked_candidates": self.keys(pick.candidates) if pick.value is None else [],
        }

    def window(self, item):
        return {
            "value": str(item.value) if item.value is not None else None,
            "reason": item.reason,
            "period_end": str(item.period_end) if item.period_end else None,
            "tags": item.tags,
            "quarters": [
                {"period_end": str(end), **self.selection(pick)} for end, pick in item.quarters
            ],
        }

    def grid_row(self, snap):
        flows = {
            key: self.window(snap.current_flows[key]) | {"rule": rule}
            for key, rule in METRICS.items()
        }
        debt = self.selection(snap.current_balance["total_debt"])
        debt["rule"] = (
            "DEBT_COMPONENTS"
            if debt["reason"] in {"VERIFIED_COMPLETE_COMPONENTS", "RECONCILED_ROUNDED_AGGREGATE"}
            else "DEBT_REPORTED"
        )
        shares = self.selection(snap.current_balance["shares_outstanding"]) | {
            "rule": "SHARES_AGGREGATE"
        }
        for name in (
            "revenue",
            "eps_diluted",
            "interest_expense",
            "tax_expense",
            "depreciation_amortization",
            "depreciation",
            "amortization",
        ):
            flows[name] = self.window(snap.current_flows[name])
        value = snap.state.current
        ebitda_inputs = []
        if value.ebitda is not None:
            ebitda_inputs.extend(
                ["operating_income"]
                if value.ebitda_formula.startswith("operating income")
                else ["net_income", "interest_expense", "tax_expense"]
            )
            combined = snap.current_flows["depreciation_amortization"]
            ebitda_inputs.extend(
                ["depreciation_amortization"]
                if combined.value == value.depreciation_amortization
                and combined.period_end == value.metric_period_ends["depreciation_amortization"]
                else ["depreciation", "amortization"]
            )
        roic_value = snap.metrics().roic
        return flows | {
            "shares_outstanding": shares,
            "total_debt": debt,
            "ebitda": {
                "rule": "EBITDA_EXISTING",
                "value": str(value.ebitda) if value.ebitda is not None else None,
                "period_end": str(value.metric_period_ends["ebitda"])
                if value.ebitda is not None
                else None,
                "formula": value.ebitda_formula,
                "selected_input_traces": ebitda_inputs,
                "reason": "CALCULATED" if ebitda_inputs else "NO_ALIGNED_EBITDA_BRIDGE",
            },
            "roic": {
                "rule": "ROIC_EXISTING",
                "value": roic_value,
                "reason": "CALCULATED" if roic_value is not None else "INCOMPLETE_ROIC_INPUTS",
                "current": {
                    key: self.selection(snap.current_balance[key]) for key in INSTANT_METRICS
                },
                "prior": {
                    key: self.selection(snap.prior_balance[key])
                    for key in INSTANT_METRICS
                    if key in snap.prior_balance
                },
                "input_traces": ["operating_income", "tax_expense", "net_income"],
            },
        }


def replay(data, baseline, e6):
    candidates = {item["session"]: item["sources"] for item in baseline["class_a_preview"]}
    # Use the bound listing fact itself as the scope witness, independent of the Debt sample.
    scope = {s["share_fact"]["source"]["cik"] for v in candidates.values() for s in v}
    require(len(scope) == 1, "AMBIGUOUS_LISTING_SCOPE")
    (scope_cik,) = scope
    facts, originals, accepted, leases, covers, open_refs = data
    trial = advanced.trial_evidence(e6["filings"])
    trace = Trace(originals)
    before_index = {(row["symbol"], row["session"]): row for row in e6["coverage_preview"]["grid"]}
    grid, totals = [], defaultdict(Counter)
    for symbol, issuer in sorted(facts.items()):
        cik = next(iter(issuer)).cik
        for session in audit.ANCHORS:
            args = dict(cik=cik, decision_at=close(session))
            base = research_accounting_as_of(
                issuer, session, evidence=Evidence(acceptance=accepted), **args
            )
            proposed = research_accounting_as_of(
                issuer, session, evidence=Evidence(acceptance=accepted, **trial), **args
            )
            old, new = trace.grid_row(base), trace.grid_row(proposed)
            ann = {
                key: value
                for key, value in old.items()
                if key in before_index[symbol, str(session)]["available"]
            }
            require(
                all(
                    (value["value"] is not None)
                    == before_index[symbol, str(session)]["available"][key]
                    for key, value in ann.items()
                ),
                "PREVIOUS_ACCOUNTING_COVERAGE_DRIFT",
            )
            new["finance_lease_liability"] = lease_selection(
                leases, proposed.current_balance["total_assets"]
            )
            old["finance_lease_liability"] = {"value": None, "reason": "NO_SEPARATE_RULE"}
            if cik == scope_cik:
                old["listed_class_net_shares"] = {"value": None, "reason": "NO_CLASS_RULE"}
                values = [
                    net_share_preview(covers[item["cover_source"]["accession"]], item, session)
                    for item in candidates[str(session)]
                ]
                valid = [item for item in values if item is not None]
                new["listed_class_net_shares"] = (
                    valid[0] | {"additional_sources": valid[1:]}
                    if valid
                    and len(valid) == len(values)
                    and len({item["value"] for item in valid}) == 1
                    else {
                        "rule": "SHARES_CLASS_NET",
                        "value": None,
                        "reason": "NO_UNAMBIGUOUS_COVER_CLAUSE",
                    }
                )
            era = "2016-2019" if session.year < 2020 else "2020-2026"
            for key, value in new.items():
                totals[symbol, era][key] += int(value["value"] is not None)
            grid.append({"symbol": symbol, "session": str(session), "before": old, "preview": new})
    require(len(grid) == 77, "INVALID_COVERAGE_GRID")
    return {
        "probes": grid,
        "source_facts": trace.registry,
        "coverage": {f"{symbol}/{era}": dict(counter) for (symbol, era), counter in totals.items()},
        "open_technical_facts": {
            f"{symbol}/{metric}": len(rows) for (symbol, metric), rows in open_refs.items()
        },
        "trial_debt_references": [item.reference for item in trial["debt_coverage"]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--advanced", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = f.pilot.code_binding()
    require(
        all(
            path.absolute().drive.upper() == "G:"
            for path in (args.draft, args.advanced, args.output)
        ),
        "G_DRIVE_REQUIRED",
    )
    f.bulk.directory(args.output.absolute().parent)
    require(not f.bulk.exists(args.output), "NEW_OUTPUT_REQUIRED")
    baseline = audit.read_bound(args.draft, advanced.DRAFT_SHA, max_bytes=f.LIMIT)
    e6 = audit.read_bound(args.advanced, PREVIEW_SHA, max_bytes=f.LIMIT)
    wanted = {
        s["cover_source"]["accession"]
        for item in baseline["class_a_preview"]
        for s in item["sources"]
    }
    source = f.ROOT / f"f01-extended-filing-evidence-{audit.RUN}" / "manifest.json"
    derived = (
        f.qualification.ARTIFACTS
        / f"f01-extended-accounting-evidence-{audit.RUN}"
        / "manifest.json"
    )
    result = replay(lookup(source, derived, wanted), baseline, e6)
    catalog = {"version": VERSION, "base_policy": POLICY, "rules": RULES}
    result.update(
        rule_catalog=catalog,
        rule_catalog_sha256=f.sha(f.bulk.json_bytes(catalog)),
        review_required=True,
        approved_rules=[],
        candidate_code=current,
        inputs={
            "draft_sha256": advanced.DRAFT_SHA,
            "advanced_sha256": PREVIEW_SHA,
            "filing_manifest_sha256": audit.FILING_SHA,
            "accounting_manifest_sha256": audit.ACCOUNTING_SHA,
        },
    )
    output = (json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    f.bulk.write_new(args.output, output)
    print(args.output)


if __name__ == "__main__":
    try:
        main()
    except (f.bulk.Blocked, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ACCOUNTING_RULE_PREVIEW_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from None
