"""Replay opt-in Research fallback candidates against the bound E.11 pilot evidence."""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import audit_f01_accounting_semantic_qualification as audit
import import_f01_sec_bulk_pilot as pilot
import prepare_f01_accounting_handoff as handoff
from f01_accounting_research_policy import CALCS
from preview_f01_accounting_rules import close

from trading_system.fundamentals.accounting_fallback import (
    DEBT_BRIDGE,
    OPERATING_BRIDGE,
    POLICY,
    PPE_CASH,
    OriginalAmount,
    cash_ppe,
    closed_financing,
    flow_fallback,
    operating_bridge,
)
from trading_system.fundamentals.metrics import build_ttm, debt_to_ebitda
from trading_system.fundamentals.quality import FundamentalAccountingState
from trading_system.fundamentals.semantic import Evidence, ResearchAccounting
from trading_system.models.fundamentals import BalanceSheetSnapshot, FundamentalFact

f = audit.filings
DATASET = "f01-research-051f55358e844dd0b752ad0c10fa2e84"
IMPORT_SHA = "3c15eaa827b911d45eda569cee1c4da716e805937eaeb86b86f0322239022524"
INTENT_SHA = "a67074042ed005dd2cbdb242b65c01513ae140701bf853f27afaf742c373a9e7"
PPE_TAGS = {
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquirePropertyPlantAndEquipment",
}
PRETAX = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
)
INVESTING = "NetCashProvidedByUsedInInvestingActivities"
OPERATING_COST = {
    "CostOfGoodsAndServicesSold",
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    "ResearchAndDevelopmentExpense",
    "SellingGeneralAndAdministrativeExpense",
    "ResearchAndDevelopmentAssetAcquiredOtherThanThroughBusinessCombinationWrittenOff",
    "RestructuringSettlementAndImpairmentProvisions",
}


def tag(concept):
    return concept.rsplit("}", 1)[-1]


class StatementRows(HTMLParser):
    """Read the printed row label and table of already identified original fact IDs."""

    def __init__(self, ids, raw):
        super().__init__(convert_charrefs=True)
        self.ids = ids
        self.tables = []
        self.rows = []
        self.labels = {}
        self.ordinal = 0
        self.feed(raw.decode("utf-8", errors="replace"))
        self.close()

    def handle_starttag(self, name, attrs):
        if name == "table":
            self.ordinal += 1
            self.tables.append(self.ordinal)
        elif name == "tr" and self.tables:
            self.rows.append([self.tables[-1], 0, [], []])
        elif self.rows and self.tables and self.rows[-1][0] == self.tables[-1]:
            if name in {"td", "th"}:
                self.rows[-1][1] += 1
            elif (fact_id := dict(attrs).get("id")) in self.ids:
                self.rows[-1][3].append(fact_id)

    def handle_data(self, data):
        if self.rows and self.tables and self.rows[-1][:2] == [self.tables[-1], 1]:
            self.rows[-1][2].append(data)

    def handle_endtag(self, name):
        if name == "tr" and self.rows and self.tables and self.rows[-1][0] == self.tables[-1]:
            table, _, text, ids = self.rows.pop()
            description = " ".join("".join(text).split())
            for fact_id in ids:
                f.require(fact_id not in self.labels, "DUPLICATE_ORIGINAL_VISIBLE_FACT_ID")
                self.labels[fact_id] = table, description
        elif name == "table" and self.tables:
            self.tables.pop()


def calculation(row, role_word):
    roles = defaultdict(set)
    for arc in row["relationships"]:
        role = arc["linkrole"].rsplit("/", 1)[-1].casefold()
        if (
            arc["arcrole"] in CALCS
            and arc["from"] == row["concept"]
            and arc.get("source_sha256")
            and ("consolidated" in role or "statement" in role)
            and role_word in role
        ):
            roles[arc["linkrole"], arc["source_sha256"]].add((arc["to"], Decimal(arc["weight"])))
    return roles


def pick(rows, concept):
    candidates = [row for row in rows if row["concept"] == concept]
    if len({Decimal(row["value"]) for row in candidates}) == 1:
        return candidates
    return []


def equation(rows, edges):
    if len(edges) != len({concept for concept, _ in edges}):
        return None
    parts = {concept: pick(rows, concept) for concept, _ in sorted(edges)}
    if not parts or any(not values for values in parts.values()):
        return None
    value = sum(Decimal(parts[concept][0]["value"]) * weight for concept, weight in sorted(edges))
    return value, parts


def cash_proof(rows, parent, labels):
    for role, edges in calculation(parent, "cashflow").items():
        capex = [
            concept
            for concept, weight in edges
            if weight == -1 and f.taxonomy(concept)[0] == "us-gaap" and tag(concept) in PPE_TAGS
        ]
        if len(capex) != 1 or any(
            tag(concept) == "PaymentsForAdditionsToPropertyPlantAndEquipment"
            or (tag(concept) in PPE_TAGS and concept != capex[0])
            or any(
                token in tag(concept) for token in ("AcquireIntangible", "AcquireOtherProductive")
            )
            for concept, _ in edges
        ):
            continue
        result = equation(rows, edges)
        if not result or result[0] != Decimal(parent["value"]):
            continue
        picked = result[1][capex[0]]
        names = {labels[row["fact_id"]][1] for row in picked}
        if len(names) == 1:
            label = next(iter(names))
            amount = cash_ppe(tag(capex[0]), label, Decimal(picked[0]["value"]))
            if amount is not None:
                yield (
                    PPE_CASH,
                    amount,
                    [parent, *sum(result[1].values(), [])],
                    {
                        "role": list(role),
                        "row_label": label,
                        "investing_equation": sorted((name, str(weight)) for name, weight in edges),
                    },
                )


def income_proof(rows, parent):
    if any(f.taxonomy(row["concept"]) == ("us-gaap", "OperatingIncomeLoss") for row in rows):
        return
    for role, edges in calculation(parent, "operations").items():
        top = equation(rows, edges)
        if top is None or len(edges) != 2 or top[0] != Decimal(parent["value"]):
            continue
        revenues = [
            name
            for name, weight in edges
            if weight == 1
            and f.taxonomy(name)
            in {
                ("us-gaap", "Revenues"),
                ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            }
        ]
        costs = [name for name, weight in edges if weight == -1]
        if len(revenues) != 1 or len(costs) != 1:
            continue
        subtotal = top[1][costs[0]][0]
        children = calculation(subtotal, "operations").get(role, set())
        nonop = [
            name
            for name, weight in children
            if f.taxonomy(name) == ("us-gaap", "NonoperatingIncomeExpense") and weight == -1
        ]
        if len(nonop) != 1 or any(
            f.taxonomy(name) != ("us-gaap", tag(name))
            or tag(name) not in OPERATING_COST
            or weight != 1
            for name, weight in children
            if name != nonop[0]
        ):
            continue
        bottom = equation(rows, children)
        if bottom is None:
            continue

        def first(parts, name):
            return Decimal(parts[name][0]["value"])

        amount = operating_bridge(
            Decimal(parent["value"]),
            first(top[1], revenues[0]),
            Decimal(subtotal["value"]),
            first(bottom[1], nonop[0]),
            [first(bottom[1], name) for name in bottom[1] if name != nonop[0]],
        )
        if amount is not None:
            yield (
                OPERATING_BRIDGE,
                amount,
                [parent, *sum(top[1].values(), []), *sum(bottom[1].values(), [])],
                {
                    "role": list(role),
                    "nonoperating_tag": nonop[0],
                    "pretax_equation": sorted((name, str(weight)) for name, weight in edges),
                    "cost_equation": sorted((name, str(weight)) for name, weight in children),
                },
            )


def source_reference(shard, witness, context, parents, details):
    return {
        "filing_shard_sha256": shard["sha256"],
        "cik": witness["cik"],
        "accession": witness["accession"],
        "acceptance": witness["acceptance"],
        "period": [context["startDate"], context["endDate"] or context["instant"]],
        "original_facts": [audit.source_ref(row) for row in parents],
        "evidence": details,
    }


def filing_flows(entry, row, wanted, proofs):
    allowed = []
    for item in row["observations"]:
        ctx = item.get("context")
        if (
            ctx
            and ctx["startDate"]
            and ctx["endDate"]
            and not ctx["dimensions"]
            and not ctx["opaque_context"]
            and f.bulk.matches_cik(ctx["entity"], row["cik"])
            and ctx["scheme"] in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
            and not item.get("evidence_issue")
            and item.get("fact_id")
            and item.get("value") is not None
            and f.unit_matches(item.get("unit"), "USD")
        ):
            allowed.append(item)
    wanted_parents = {
        INVESTING if "capital_expenditures" in wanted else None,
        PRETAX if "operating_income" in wanted else None,
    }
    if not any(tag(item["concept"]) in wanted_parents for item in allowed):
        return
    name = row["primary"]
    if not name.lower().endswith((".htm", ".html")):
        return
    raw, source = f.cache_read(f.ROOT / row["cik"] / row["accession"] / name, row, name)
    if source["acceptance"] != row["acceptance"]:
        return
    labels = StatementRows({item["fact_id"] for item in allowed}, raw).labels
    grouped = defaultdict(list)
    for item in allowed:
        if item["source"] == source and item["fact_id"] in labels:
            grouped[item["context_id"], labels[item["fact_id"]][0]].append(item)
    source_hashes = {item["sha256"] for item in row["cached_sources"]}
    for rows in grouped.values():
        for parent in rows:
            if f.taxonomy(parent["concept"])[0] != "us-gaap":
                continue
            ruleset = tag(parent["concept"])
            if ruleset not in wanted_parents:
                continue
            if not pick(rows, parent["concept"]):
                continue
            is_cash = ruleset == INVESTING
            choices = cash_proof(rows, parent, labels) if is_cash else income_proof(rows, parent)
            metric = "capital_expenditures" if is_cash else "operating_income"
            for rule, value, parents, details in choices:
                if details["role"][1] not in source_hashes:
                    continue
                ctx = parent["context"]
                trace = source_reference(entry, source, ctx, parents, details) | {
                    "rule": rule,
                    "value": str(value),
                    "available_via": "ORIGINAL_FILING",
                }
                ref = f.sha(f.bulk.json_bytes(trace))
                proofs.setdefault(ref, trace)
                anchor = FundamentalFact(
                    cik=row["cik"],
                    symbol=row["symbol"],
                    taxonomy="us-gaap",
                    metric=metric,
                    tag=ruleset,
                    value=Decimal(parent["value"]),
                    unit="USD",
                    period_start=ctx["startDate"],
                    period_end=ctx["endDate"],
                    filed=row["filed"],
                    form=row["form"],
                    accession_number=row["accession"],
                )
                yield (
                    metric,
                    OriginalAmount(anchor, value, datetime.fromisoformat(row["acceptance"]), ref),
                )


def debt_proof(entry, row, period, positions, anchor_sources):
    if row["form"] != "10-K" or not positions:
        return None
    keys = ("LongTermDebtCurrent", "LongTermDebtNoncurrent", "FinanceLeaseLiability")
    if any(positions.get(name, {}).get("value") is None for name in keys):
        return None
    date_fact = [
        x
        for x in row["observations"]
        if f.taxonomy(x["concept"]) == ("dei", "DocumentPeriodEndDate")
    ]
    measured = date.fromisoformat(period)
    date_values = {" ".join(x["raw_text"].split()) for x in date_fact}
    if date_values not in ({period}, {f"{measured:%B} {measured.day}, {measured.year}"}):
        return None
    current, later, lease = (Decimal(positions[name]["value"]) for name in keys)
    competing = ("CommercialPaper", "ShortTermBorrowings", "DebtCurrent")
    if any(name in positions and positions[name]["value"] is None for name in competing):
        return None
    if any(
        positions.get(name, {}).get("value") not in (None, "0", str(current))
        for name in ("DebtCurrent",)
    ) or any(
        positions.get(name, {}).get("value") not in (None, "0")
        for name in ("CommercialPaper", "ShortTermBorrowings")
    ):
        return None
    text, source = audit.original_text(row)
    no_short = re.search(
        rf"there (?:were|was) no short-term borrowings outstanding at the end of {measured.year}"
        r"(?=[,.])",
        text,
        re.I,
    )
    if not no_short:
        return None
    lease_table = re.search(
        r"Finance lease liabilities\s*\((\d+)\)\s+([\d,]+) [\d,]+ Long-term "
        r"Operating lease liabilities .{1,90}?Finance lease liabilities\s*\((\d+)\)"
        r"\s+([\d,]+).{1,500}?Total lease liabilities.{1,500}?"
        r"\(\1\) Included in other current liabilities in the consolidated balance sheets\."
        r".{0,120}?\(\3\) Included in other long-term liabilities "
        r"in the consolidated balance sheets\.",
        text,
        re.I,
    )
    if not lease_table or lease_table[1] == lease_table[3]:
        return None
    facts = {}
    at_context = {ref["context_id"] for ref in anchor_sources if ref["source"] == source}
    required = {
        *keys,
        "FinanceLeaseLiabilityCurrent",
        "FinanceLeaseLiabilityNoncurrent",
        "OtherLiabilitiesCurrent",
        "OtherLiabilitiesNoncurrent",
    }
    for item in row["observations"]:
        name = tag(item["concept"])
        ctx = item.get("context")
        if (
            name in required
            and f.taxonomy(item["concept"])[0] == "us-gaap"
            and item["source"] == source
            and item["context_id"] in at_context
            and ctx
            and ctx["instant"] == period
            and not ctx["dimensions"]
            and not ctx["opaque_context"]
            and not item.get("evidence_issue")
            and item["value"] is not None
            and f.unit_matches(item["unit"], "USD")
        ):
            facts.setdefault(name, []).append(item)
    if facts.keys() != required or any(
        len({item["value"] for item in items}) != 1 for items in facts.values()
    ):
        return None

    def combined(name):
        return Decimal(facts[name][0]["value"])

    if (current, later, lease) != tuple(combined(key) for key in keys):
        return None
    lease_keys = "FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent"
    for key, cell, other in zip(
        lease_keys,
        (lease_table[2], lease_table[4]),
        ("OtherLiabilitiesCurrent", "OtherLiabilitiesNoncurrent"),
        strict=True,
    ):
        reported = combined(key)
        if not 0 <= reported <= combined(other) or not any(
            re.fullmatch(r"[\d,]+", item["raw_text"])
            and item["raw_text"].replace(",", "") == cell.replace(",", "")
            and Decimal(cell.replace(",", "")).scaleb(int(item["scale"])) == reported
            for item in facts[key]
        ):
            return None
    if sum(combined(key) for key in lease_keys) != lease:
        return None
    locations = (
        ("LongTermDebtCurrent", "OtherLiabilitiesCurrent", "LiabilitiesCurrent"),
        ("LongTermDebtNoncurrent", "OtherLiabilitiesNoncurrent", "Liabilities"),
    )
    scope_roles = []
    declared_sha = {doc["sha256"] for doc in row["cached_sources"]}
    for debt, other, parent in locations:
        scope_roles.append(
            set.intersection(
                *(
                    {
                        (edge["linkrole"], edge["source_sha256"])
                        for item in facts[name]
                        for edge in item["relationships"]
                        if edge["arcrole"] in CALCS
                        and tag(edge["from"]) == parent
                        and edge["to"] == item["concept"]
                        and Decimal(edge["weight"]) == 1
                        and edge.get("source_sha256") in declared_sha
                        and "balance" in edge["linkrole"].casefold()
                    }
                    for name in (debt, other)
                )
            )
        )
    roles = sorted(set.intersection(*scope_roles))
    if not roles:
        return None
    value = closed_financing(
        current, later, lease, other_borrowings_absent=True, leases_disjoint=True
    )
    if value is None:
        return None
    witness = facts[keys[0]][0]
    return source_reference(
        entry,
        source,
        witness["context"],
        sum(facts.values(), []),
        {
            "role": roles[0],
            "explicit_absence": no_short[0],
            "lease_placement": lease_table[0],
            "carrying_components_usd": [str(current), str(later), str(lease)],
        },
    ) | {"rule": DEBT_BRIDGE, "value": str(value), "available_via": "ORIGINAL_FILING"}


def pilot_evidence():
    run = pilot.RESEARCH_ROOT / "import-artifacts" / f"f01-pilot-{DATASET}"
    intent = handoff.read_bound(run / "binding.json", INTENT_SHA)
    report = handoff.read_bound(run / "result" / pilot.REPORT, IMPORT_SHA)
    bound = intent["accounting_handoff"]["binding"]
    f.require(
        report["binding"] == intent
        and intent["dataset_id"] == DATASET
        and bound["lineage"]["dataset_id"] == DATASET
        and report["status"] == pilot.IMPORTED
        and (
            report["dataset_version"],
            report["confirmed_receipts"],
            report["totals"]["canonical_facts"],
        )
        == (8, 7, 26184)
        and bound["e8_replay_sha256"] == handoff.E8_SHA
        and bound["e10_replay_sha256"] == handoff.E10_SHA
        and bound["original_filing_manifest_sha256"] == handoff.FILING_SHA
        and bound["accounting_manifest_sha256"] == handoff.ACCOUNTING_SHA,
        "E11_PILOT_BINDING_DRIFT",
    )
    return intent


def targets(probe, side, registry):
    return {
        (entry["fact"]["accession_number"], entry["fact"]["period_end"]): entry["original"][
            "matches"
        ]
        for ref in probe["preview"]["roic"][side].get("total_assets", {}).get("sources", ())
        for entry in (registry[ref],)
    }


def evidence_rows(probes, registry):
    path = f.ROOT / f"f01-extended-filing-evidence-{audit.RUN}" / "manifest.json"
    other = (
        f.qualification.ARTIFACTS / f"f01-extended-accounting-evidence-{audit.RUN}/manifest.json"
    )
    manifest = handoff.read_bound(path, handoff.FILING_SHA)
    accounting = handoff.read_bound(other, handoff.ACCOUNTING_SHA)
    flows, debts, proofs = defaultdict(list), {}, {}
    needed, debt_entries = defaultdict(set), defaultdict(dict)
    for probe in probes:
        preview = probe["preview"]
        for metric in ("capital_expenditures", "operating_income"):
            if preview[metric]["value"] is None:
                needed[probe["symbol"]].add(metric)
        if preview["total_debt"]["reason"] not in {
            "DEBT_COMPONENTS_OR_SCOPE_UNVERIFIED",
            "NO_DEBT_OBSERVATIONS",
        }:
            continue
        for (accession, period), source in targets(probe, "current", registry).items():
            debt_periods = {
                registry[ref]["fact"]["period_end"]
                for ref in preview["total_debt"]["blocked_candidates"]
            }
            if not debt_periods or debt_periods == {period}:
                debt_entries[accession][period] = probe["financing"]["positions"], source
    selected = []
    for entry, derived in zip(manifest["filings"], accounting["filings"], strict=True):
        f.require(entry["sha256"] == derived["source_sha256"], "ACCOUNTING_SHARD_BINDING_DRIFT")
        if entry["symbol"] in needed or entry["accession"] in debt_entries:
            selected.append(entry)
    for entry, row in f.extended_records(path, manifest | {"filings": selected}):
        if (
            not row.get("acceptance")
            or row.get("availability_conflict")
            or (row.get("availability_unverified") or row["form"] not in {"10-K", "10-Q"})
        ):
            continue
        for metric, amount in filing_flows(entry, row, needed[row["symbol"]], proofs):
            flows[row["symbol"], metric].append(amount)
        for period, (positions, source) in debt_entries.get(row["accession"], {}).items():
            debt = debt_proof(entry, row, period, positions, source)
            if debt:
                ref = f.sha(f.bulk.json_bytes(debt))
                proofs[ref] = debt
                debts[row["accession"], period] = ref
    return flows, debts, proofs


def ttm(preview, session, windows, registry):
    def selected(_facts, metric, _as_of, *, end_on_or_before=None):
        if metric in windows:
            item = windows[metric]
            return item.value, item.period_end, item.filed
        item = preview.get(metric)
        if not item or item["value"] is None:
            return None, None, None
        filed = max(
            date.fromisoformat(registry[ref]["fact"]["filed"])
            for quarter in item["quarters"]
            for ref in quarter["sources"]
        )
        return Decimal(item["value"]), date.fromisoformat(item["period_end"]), filed

    return build_ttm([], session, flow_ttm=selected)


def balance(probe, side, registry, debts, proofs, cutoff, *, use_fallback):
    decisions = probe["preview"]["roic"][side]
    asset_targets = targets(probe, side, registry)
    period = next(iter(asset_targets))[1] if len(asset_targets) == 1 else None
    values = {
        name: Decimal(decision["value"]) if decision["value"] is not None else None
        for name, decision in decisions.items()
    }
    ref = None
    if use_fallback and values.get("total_debt") is None and len(asset_targets) == 1:
        candidate = debts.get(next(iter(asset_targets)))
        if candidate and datetime.fromisoformat(proofs[candidate]["acceptance"]) <= cutoff:
            values["total_debt"] = Decimal(proofs[candidate]["value"])
            ref = candidate
    return BalanceSheetSnapshot(period_end=period, **values), ref


def replay_probe(probe, cik, registry, flows, debts, proofs):
    session = date.fromisoformat(probe["session"])
    cutoff = close(session)
    old = probe["preview"]
    current_periods = targets(probe, "current", registry)
    expected = (
        date.fromisoformat(next(iter(current_periods))[1]) if len(current_periods) == 1 else None
    )
    windows = {}
    for metric in ("operating_income", "capital_expenditures"):
        result = flow_fallback(
            flows[probe["symbol"], metric],
            cik=cik,
            as_of=session,
            decision_at=cutoff,
            required_end=expected,
            qualified_value=Decimal(old[metric]["value"])
            if old[metric]["value"] is not None
            else None,
            qualified_quarters={
                date.fromisoformat(item["period_end"]): Decimal(item["value"])
                for item in old[metric]["quarters"]
                if item["value"] is not None
            },
            blocked_quarters=frozenset(
                date.fromisoformat(item["period_end"])
                for item in old[metric]["quarters"]
                if item["reason"]
                in {
                    "UNRESOLVED_EQUIVALENT_OBSERVATIONS",
                    "UNRESOLVED_QUARTER_CONTEXT",
                }
            ),
        )
        if result:
            windows[metric] = result
    baseline_ttm = ttm(old, session, {}, registry)
    f.require(
        (str(baseline_ttm.ebitda) if baseline_ttm.ebitda is not None else None)
        == old["ebitda"]["value"],
        "BASELINE_EBITDA_REPLAY_DRIFT",
    )
    new_ttm = ttm(old, session, windows, registry)
    old_balance = balance(probe, "current", registry, debts, proofs, cutoff, use_fallback=False)[0]
    new_balance, debt_ref = balance(
        probe, "current", registry, debts, proofs, cutoff, use_fallback=True
    )
    prior_balance, prior_ref = balance(
        probe, "prior", registry, debts, proofs, cutoff, use_fallback=True
    )
    preserve_ebitda = (
        Decimal(old["ebitda"]["value"]) if old["ebitda"]["value"] is not None else None
    )
    aligned_balance = True
    for side in ("current", "prior"):
        asset_periods = {period for _, period in targets(probe, side, registry)}
        for name in ("total_debt", "total_equity", "cash"):
            selected = old["roic"][side].get(name, {}).get("sources", ())
            relevant = {registry[ref]["fact"]["period_end"] for ref in selected}
            aligned_balance &= not relevant or relevant == asset_periods
    accounting = ResearchAccounting(
        cik=cik,
        as_of=session,
        decision_at=cutoff,
        state=FundamentalAccountingState(new_ttm, baseline_ttm, new_balance, prior_balance),
        current_flows={},
        prior_flows={},
        current_balance={},
        prior_balance={},
        excluded=(),
        eligible_facts=(),
        evidence=Evidence(),
    )
    old_values = {
        metric: old[metric]["value"]
        for metric in (
            "total_debt",
            "operating_income",
            "capital_expenditures",
            "operating_cash_flow",
            "ebitda",
            "roic",
        )
    }
    old_values.update(
        fcf=baseline_ttm.free_cash_flow,
        debt_to_ebitda=debt_to_ebitda(old_balance.total_debt, preserve_ebitda),
    )
    choices = {
        "total_debt": new_balance.total_debt,
        "operating_income": new_ttm.operating_income,
        "capital_expenditures": new_ttm.capital_expenditures,
        "ebitda": new_ttm.ebitda,
        "fcf": new_ttm.free_cash_flow,
        "debt_to_ebitda": debt_to_ebitda(
            new_balance.total_debt,
            preserve_ebitda if preserve_ebitda is not None else new_ttm.ebitda,
        ),
        "roic": accounting.metrics().roic if aligned_balance else None,
    }
    after = {key: choices.get(key) if value is None else value for key, value in old_values.items()}
    return {
        "symbol": probe["symbol"],
        "session": probe["session"],
        "before": old_values,
        "after": after,
        "current_debt_proof": debt_ref,
        "prior_debt_proof": prior_ref,
        "fallback_windows": {
            metric: {
                "ttm_period_end": window.period_end,
                "quarters": [
                    {
                        "period_end": end,
                        "value": quarter.value,
                        "original_proofs": quarter.references,
                        "selection": quarter.reason,
                    }
                    for end, quarter in window.quarters
                ],
            }
            for metric, window in windows.items()
        },
        "ebitda_formula_if_added": new_ttm.ebitda_formula
        if old["ebitda"]["value"] is None and new_ttm.ebitda is not None
        else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference-only", action="store_true")
    args = parser.parse_args()
    if not args.reference_only and not args.output:
        parser.error("--output is required for the complete grid")
    if args.output:
        f.bulk.directory(args.output.absolute().parent)
        f.require(not f.bulk.exists(args.output), "NEW_OUTPUT_REQUIRED")
    current = pilot.code_binding()
    intent = pilot_evidence()
    e8 = handoff.read_bound(
        pilot.REPO / "reports/f01_accounting_policy_qualification.json", handoff.E8_SHA
    )
    e10 = handoff.read_bound(
        pilot.REPO / "reports/f01_accounting_policy_consolidation.json", handoff.E10_SHA
    )
    handoff.validate_parity(e8, e10)
    registry = e10["source_facts"]
    probes = e10["probes"]
    if args.reference_only:
        selected = sorted(probes, key=lambda item: item["session"], reverse=True)[0]["session"]
        probes = [item for item in probes if item["session"] == selected]
    ciks = {item["symbol"]: item["cik"] for item in intent["active_issuers"]}
    flows, debts, proofs = evidence_rows(probes, registry)
    grid = [
        replay_probe(probe, ciks[probe["symbol"]], registry, flows, debts, proofs)
        for probe in probes
    ]
    coverage = {}
    for key in grid[0]["before"]:
        before = sum(row["before"][key] is not None for row in grid)
        after = sum(row["after"][key] is not None for row in grid)
        coverage[key] = {"before": before, "after": after, "added": after - before}
    selected_proofs = {
        ref
        for row in grid
        for ref in (
            row["current_debt_proof"],
            row["prior_debt_proof"],
            *(
                ref
                for win in row["fallback_windows"].values()
                for quarter in win["quarters"]
                for ref in quarter["original_proofs"]
            ),
        )
        if ref
    }
    result = {
        "contract": POLICY,
        "review_required": True,
        "approved_rules": [],
        "candidate": current,
        "bindings": {
            "dataset_id": DATASET,
            "dataset_version": 8,
            "canonical_facts": 26184,
            "receipts": 7,
            "e8_replay_sha256": handoff.E8_SHA,
            "e10_replay_sha256": handoff.E10_SHA,
            "e11_import_sha256": IMPORT_SHA,
            "e11_intent_sha256": INTENT_SHA,
            "original_manifest_sha256": handoff.FILING_SHA,
            "accounting_manifest_sha256": handoff.ACCOUNTING_SHA,
        },
        "probe_count": len(grid),
        "coverage": coverage,
        "rules": {
            DEBT_BRIDGE: "Consolidated carrying debt, explicit short-term borrowing absence "
            "and disjoint finance leases independently reconciled.",
            PPE_CASH: "Exact cash PPE purchase line inside reconciled investing cash flow.",
            OPERATING_BRIDGE: "Exact and complete pretax and costs bridges on one statement.",
        },
        "grid": grid,
        "proofs": {ref: proofs[ref] for ref in sorted(selected_proofs)},
        "evidence_screen": {
            "observations_by_rule": dict(Counter(p["rule"] for p in proofs.values())),
        },
    }
    if args.output:
        f.require(pilot.code_binding() == current, "CANDIDATE_CHANGED_DURING_FALLBACK_REPLAY")
        raw = (
            json.dumps(result, default=str, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode()
        f.bulk.write_new(args.output, raw)
    print(json.dumps(coverage, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FALLBACK_REPLAY_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(3) from None
