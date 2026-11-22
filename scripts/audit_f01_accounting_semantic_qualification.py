"""Replay the bound extended filing evidence and write an unreviewed accounting draft."""

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import prepare_f01_filing_evidence as filings

from trading_system.fundamentals.metrics import MAX_SHARES_OUTSTANDING_AGE
from trading_system.fundamentals.semantic import (
    DEBT_COMPONENTS,
    ISSUER_SCOPE,
    TOTAL_DEBT,
    Evidence,
    research_accounting_as_of,
)
from trading_system.models.fundamentals import FundamentalFact

FILING_SHA = "89df83b5b2fc304986aa09f9e26391aa66a62b530d46d73cb0012c3425332194"
ACCOUNTING_SHA = "f28307df196f98c69b3065060673786a2f6e98569b0cdcb4da52a81ec359992f"
BASELINE = "4db32c8ef4e6b900aee5f7cea5d09401fc70e23e9643810990cd2e89454bc480"
RUN = "20261122-124810"
DEI = "{http://xbrl.sec.gov/dei/2021q4}EntityCommonStockSharesOutstanding"
SHARE_TAG = "EntityCommonStockSharesOutstanding"
SHARES = ("dei", SHARE_TAG)
ANCHORS = [date(year, 12, 31) for year in range(2016, 2026)] + [date(2026, 11, 20)]
NY = ZoneInfo("America/New_York")
require = filings.require

# Evidence review cases, not selection rules or issuer-specific accounting fallbacks.
CASES = {
    "0001467373-24-000196": (
        "total_debt",
        "2024-05-31",
        (
            "DebtLongtermAndShorttermCombinedAmount",
            "DebtCurrent",
            "LongTermDebtAndCapitalLeaseObligations",
        ),
        r"short-term commercial paper|borrowings and indebtedness",
    ),
    "0000320193-24-000069": (
        "debt_and_leases",
        "2024-03-30",
        (
            "LongTermDebt",
            "LongTermDebtCurrent",
            "LongTermDebtNoncurrent",
            "CommercialPaper",
        ),
        r"commercial paper\s+1,997\s+5,985",
    ),
    "0001564590-18-024893": (
        "finance_leases",
        "2018-09-30",
        (
            "LongTermDebt",
            "LongTermDebtCurrent",
            "LongTermDebtNoncurrent",
            "FinanceLeaseLiability",
            "OperatingLeaseLiability",
            "OperatingLeaseLiabilityNoncurrent",
        ),
        r"Total finance lease liabilities",
    ),
    "0000909832-21-000021": (
        "income_and_equity",
        "2021-11-21",
        (
            "ProfitLoss",
            "NetIncomeLoss",
            "NetIncomeLossAttributableToNoncontrollingInterest",
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "IncomeTaxExpenseBenefit",
            "OperatingIncomeLoss",
        ),
        r"net income including noncontrolling interests",
    ),
    "0000018230-19-000224": (
        "operating_cash_flow",
        "2019-06-30",
        (
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "OperatingIncomeLoss",
        ),
        r"provided by operating activities",
    ),
    "0000018230-20-000214": (
        "operating_cash_flow_comparative",
        "2019-06-30",
        ("NetCashProvidedByUsedInOperatingActivities",),
        r"provided by operating activities",
    ),
    "0000059478-26-000108": (
        "ebitda_roic_inputs",
        "2026-09-30",
        (
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "NetIncomeLoss",
            "ProfitLoss",
            "InterestExpenseNonoperating",
            "DepreciationDepletionAndAmortization",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "IncomeTaxExpenseBenefit",
        ),
        r"purchases of property and equipment|interest expense\s+\$\s+378",
    ),
    "0001045810-26-000075": (
        "capex",
        "2026-07-26",
        (
            "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsForFinancedPropertyPlantAndEquipmentAndIntangibleAssetsFinancingActivities",
        ),
        r"purchases related to property and equipment|property and equipment",
    ),
}


class NoteText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def original_text(filing):
    primary = filing["primary"]
    folder = filings.ROOT / filing["cik"] / filing["accession"]
    raw, source = filings.cache_read(folder / primary, filing, primary)
    require(source in filing["cached_sources"], "ORIGINAL_HTML_BINDING_MISMATCH")
    parser = NoteText()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return " ".join(" ".join(parser.parts).split()), source


def source_ref(row):
    return {
        key: row.get(key)
        for key in (
            "source",
            "document_ordinal",
            "source_line",
            "concept",
            "context_id",
            "context",
            "unit",
            "value",
            "decimals",
        )
    }


def report_context(fact, row):
    decimals = row.get("decimals")
    increment = (
        str(Decimal(1).scaleb(-int(decimals)))
        if filings.rounding(Decimal(row["value"]), decimals) and decimals != "INF"
        else None
    )
    return {
        "fact": fact,
        "context": {
            "cik": fact["cik"],
            "scope": ISSUER_SCOPE,
            "reference": f"sha256:{row['source']['sha256']}#fact={row['document_ordinal']}",
            "rounding_increment": increment,
        },
    }


def domain_candidate(filing, joins, case):
    topic, period, tags, note_pattern = case
    text, source = original_text(filing)
    notes = [m.group() for m in list(re.finditer(note_pattern, text, re.I))[:4]]
    verified = defaultdict(list)
    for item in joins:
        fact = item["fact"]
        if (
            item["status"] == "VERIFIED_SOURCE_FACT"
            and fact["metric"]
            and fact["period_end"] == period
        ):
            for ordinal in item["whole_entity_candidates"]:
                verified[ordinal].append(fact)
    observations, contexts = [], []
    for row in filing["observations"]:
        ctx = row.get("context")
        concept = (row["concept"] or "").rsplit("}", 1)[-1]
        if not ctx or (ctx["instant"] or ctx["endDate"]) != period or concept not in tags:
            continue
        edges = [edge for edge in row["relationships"] if edge.get("source_sha256")]
        observations.append(source_ref(row) | {"linkbase_relationships": edges})
        for fact in verified[row["document_ordinal"]]:
            if row["value"] is not None and Decimal(row["value"]) == Decimal(fact["value"]):
                contexts.append(report_context(fact, row))
    return {
        "topic": topic,
        "symbol": filing["symbol"],
        "accession": filing["accession"],
        "period_end": period,
        "reported_acceptance": filing["acceptance"],
        "acceptance_source": filing.get("availability_source"),
        "note_source": source,
        "note_terms": sorted(set(notes)),
        "original_observations": observations,
        "proposed_api_contexts": contexts,
        "debt_coverage_review_candidates": debt_pair_candidates(contexts),
        "review_required": True,
    }


def debt_pair_candidates(contexts):
    by_tag = defaultdict(dict)
    for entry in contexts:
        fact = FundamentalFact.model_validate(entry["fact"])
        if fact.period_start is None and fact.unit == "USD":
            by_tag[fact.tag].setdefault(fact, entry["context"]["reference"])
    result = []
    for left, right in DEBT_COMPONENTS:
        if any(len(by_tag[tag]) != 1 for tag in (TOTAL_DEBT, left, right)):
            continue
        (aggregate,), (current,), (noncurrent,) = (by_tag[tag] for tag in (TOTAL_DEBT, left, right))
        if (
            len(
                {
                    (f.cik, f.accession_number, f.period_end, f.filed, f.form)
                    for f in (aggregate, current, noncurrent)
                }
            )
            != 1
        ):
            continue
        if current.value + noncurrent.value == aggregate.value:
            result.append(
                {
                    "current": current.model_dump(mode="json"),
                    "noncurrent": noncurrent.model_dump(mode="json"),
                    "reference": by_tag[TOTAL_DEBT][aggregate],
                    "review_required": True,
                    "open_decision": "Verify the reported total's financing and lease scope",
                }
            )
    return result


def tradable_class_rows(filing, *, symbol, member):
    rows = filing["observations"]
    symbols = [
        row
        for row in rows
        if filings.taxonomy(row["concept"]) == ("dei", "TradingSymbol")
        and (row.get("text_value") or row["raw_text"]).strip() == symbol
        and row.get("context")
        and not row.get("evidence_issue")
        and not row["context"]["opaque_context"]
        and filings.bulk.matches_cik(row["context"]["entity"], filing["cik"])
    ]
    if not symbols or not filing.get("acceptance") or filing.get("availability_unverified"):
        return []
    found = []
    for row in rows:
        ctx = row.get("context")
        if not (
            filings.taxonomy(row["concept"]) == SHARES
            and ctx
            and ctx["instant"]
            and ctx["scheme"] in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
            and filings.bulk.matches_cik(ctx["entity"], filing["cik"])
            and not row.get("evidence_issue")
            and not ctx["opaque_context"]
            and filings.unit_matches(row["unit"], "shares")
            and row["value"] is not None
            and Decimal(row["value"]) > 0
            and len(ctx["dimensions"]) == 1
        ):
            continue
        dim = ctx["dimensions"][0]
        if (
            filings.taxonomy(dim["axis"]) == ("us-gaap", "StatementClassOfStockAxis")
            and filings.taxonomy(dim.get("member")) == ("us-gaap", member)
            and not dim.get("is_default")
        ):
            relevant_symbols = [
                source_ref(item)
                for item in symbols
                if not item["context"]["dimensions"]
                or item["context"]["dimensions"] == ctx["dimensions"]
            ]
            if relevant_symbols:
                found.append((row, relevant_symbols))
    return found


def select_class_snapshot(candidates, session):
    available = []
    for filing, row, symbols in candidates:
        accepted = datetime.fromisoformat(filing["acceptance"])
        measured = date.fromisoformat(row["context"]["instant"])
        filed = date.fromisoformat(filing["filed"])
        if (
            accepted <= datetime.combine(session, time(16), NY)
            and measured <= session
            and filed <= session
            and session - measured <= MAX_SHARES_OUTSTANDING_AGE
            and session - filed <= MAX_SHARES_OUTSTANDING_AGE
        ):
            available.append(((measured, filed), filing, row, symbols))
    latest = max((rank for rank, *_ in available), default=None)
    current = [(filing, row, symbols) for rank, filing, row, symbols in available if rank == latest]
    values = {Decimal(row["value"]) for _, row, _ in current}
    return current if len(values) == 1 else []


def class_preview(candidates):
    grid = []
    for session in ANCHORS:
        matches = select_class_snapshot(candidates, session)
        sources = []
        for filing, row, symbols in matches:
            text, source = original_text(filing)
            title = re.search(r"Class\s+A\s+ordinary shares", text, re.I)
            if not title:
                continue
            related = re.search(
                r"includes\s+([\d,]+)\s+issued shares held by the registrant", text, re.I
            )
            excerpt = text[related.start() : related.start() + 150] if related else None
            sources.append(
                {
                    "reported_acceptance": filing["acceptance"],
                    "filed": filing["filed"],
                    "availability_source": filing.get("availability_source"),
                    "share_fact": source_ref(row),
                    "symbol_facts": symbols,
                    "cover_source": source,
                    "cover_title": title.group(),
                    "registrant_held_share_count_candidate": related.group(1) if related else None,
                    "registrant_held_share_text_witness": excerpt,
                }
            )
        grid.append(
            {
                "session": session.isoformat(),
                "status": "REVIEW_REQUIRED" if sources else "MISSING_EVIDENCE",
                "sources": sources,
            }
        )
    return grid


def coverage(facts, acceptance, *, preview=None):
    counts, grid = defaultdict(Counter), []
    for symbol, issuer_facts in sorted(facts.items()):
        cik = next(iter(issuer_facts)).cik
        for session in ANCHORS:
            accounting = research_accounting_as_of(
                issuer_facts,
                session,
                cik=cik,
                decision_at=datetime.combine(session, time(16), NY),
                evidence=Evidence(acceptance=acceptance, **(preview or {})),
            )
            selections = {
                key: accounting.current_balance[key] for key in ("shares_outstanding", "total_debt")
            }
            selections.update(
                {
                    key: accounting.current_flows[key]
                    for key in (
                        "revenue",
                        "eps_diluted",
                        "parent_net_income",
                        "net_income",
                        "operating_cash_flow",
                        "capital_expenditures",
                        "interest_expense",
                        "operating_income",
                    )
                }
            )
            states = {key: selection.value is not None for key, selection in selections.items()}
            states.update(
                ebitda=accounting.state.current.ebitda is not None,
                roic=accounting.metrics().roic is not None,
            )
            era = "2016-2019" if session.year < 2020 else "2020-2026"
            for key, available in states.items():
                counts[symbol, era][key] += int(available)
            share_sources = [
                f.model_dump(mode="json") for f in selections["shares_outstanding"].sources
            ]
            grid.append(
                {
                    "symbol": symbol,
                    "session": session.isoformat(),
                    "available": states,
                    "share_sources": share_sources,
                }
            )
    return {
        "counts": {f"{symbol}/{era}": dict(rows) for (symbol, era), rows in counts.items()},
        "grid": grid,
    }


def run(source, derived, current, *, preview=None):
    manifest, accounting = read_bound(source, FILING_SHA), read_bound(derived, ACCOUNTING_SHA)
    require(
        manifest["candidate_code"] == accounting["candidate_code"]
        and manifest["candidate_code"]["fingerprint"] == BASELINE
        and accounting["inputs"]["filing_manifest_sha256"] == FILING_SHA,
        "EXTENDED_INPUT_DRIFT",
    )
    before, additions, acceptance = defaultdict(set), defaultdict(set), {}
    restored, cases, acn, all_statuses = [], [], [], Counter()
    for (entry, row), derived_entry in zip(
        filings.extended_records(source, manifest), accounting["filings"], strict=True
    ):
        require(derived_entry["source_sha256"] == entry["sha256"], "ACCOUNTING_SOURCE_DRIFT")
        symbol, acc = row["symbol"], row["accession"]
        if (
            row["acceptance"]
            and not row.get("availability_unverified")
            and not row.get("availability_conflict")
        ):
            acceptance[acc] = datetime.fromisoformat(row["acceptance"])
        dei = [item for item in row["observations"] if item["concept"] == DEI]
        for previous in row["companyfacts_joins"]:
            fact = previous["fact"]
            all_statuses[previous["status"]] += 1
            if not fact["metric"]:
                continue
            if previous["status"] == "VERIFIED_SOURCE_FACT":
                before[symbol].add(FundamentalFact.model_validate(fact))
            elif (
                previous["status"] == "MISSING_EVIDENCE"
                and (fact["taxonomy"], fact["tag"]) == SHARES
                and dei
            ):
                match = filings.join(fact, dei, row["cik"])
                if (
                    match["status"] == "VERIFIED_SOURCE_FACT"
                    and row["filed"] == fact["filed"]
                    and acc in acceptance
                ):
                    additions[symbol].add(FundamentalFact.model_validate(fact))
                    restored.append(
                        {
                            "fact": fact,
                            "status": match["status"],
                            "reported_acceptance": row["acceptance"],
                            "availability_source": row.get("availability_source"),
                            "source_refs": [
                                source_ref(o)
                                for o in dei
                                if o["document_ordinal"] in match["whole_entity_candidates"]
                            ],
                        }
                    )
        case = CASES.get(acc)
        if case:
            cases.append(domain_candidate(row, row["companyfacts_joins"], case))
        if symbol == "ACN":
            metadata = {
                key: row.get(key)
                for key in (
                    "acceptance",
                    "filed",
                    "availability_source",
                    "cik",
                    "accession",
                    "primary",
                    "cached_sources",
                )
            }
            acn.extend(
                (metadata, item, symbols)
                for item, symbols in tradable_class_rows(
                    row, symbol="ACN", member="CommonClassAMember"
                )
            )
    require(len(restored) == 8 and len(before) == 7, "UNEXPECTED_QUALIFICATION_SCOPE")
    require(all_statuses["VERIFIED_SOURCE_FACT"] == 139734, "HISTORICAL_JOIN_COUNT_DRIFT")
    class_rows = class_preview(acn)
    corrected = {symbol: items | additions[symbol] for symbol, items in before.items()}
    return {
        "contract": "F01_ACCOUNTING_SEMANTIC_QUALIFICATION_DRAFT_V1",
        "review_required": True,
        "candidate_code": current,
        "inputs": {
            "filing_manifest_sha256": FILING_SHA,
            "accounting_manifest_sha256": ACCOUNTING_SHA,
        },
        "semantic_evidence": {
            "contexts": [],
            "acceptance": [],
            "amendments": [],
            "debt_coverage": [],
        },
        "dei_corrections": restored,
        "class_a_preview": class_rows,
        "domain_review_candidates": cases,
        "coverage_before": coverage(before, acceptance),
        "coverage_after_dei": coverage(corrected, acceptance),
        **(
            {"coverage_preview": coverage(corrected, acceptance, preview=preview)}
            if preview is not None
            else {}
        ),
    }


def read_bound(path, expected, *, max_bytes=filings.bulk.MAX_METADATA_BYTES):
    raw, payload = filings.bulk.read_metadata(path, max_bytes=max_bytes)
    require(filings.sha(raw) == expected, "MANIFEST_HASH_MISMATCH")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = filings.pilot.code_binding()
    output = args.output.absolute()
    filings.bulk.directory(output.parent)
    require(
        output.drive.upper() == "G:" and not filings.bulk.exists(output),
        "NEW_G_DRIVE_OUTPUT_REQUIRED",
    )
    source = filings.ROOT / f"f01-extended-filing-evidence-{RUN}" / "manifest.json"
    derived = (
        filings.qualification.ARTIFACTS
        / f"f01-extended-accounting-evidence-{RUN}"
        / "manifest.json"
    )
    result = run(source, derived, candidate)
    filings.bulk.write_new(output, filings.bulk.json_bytes(result))
    print(output)


if __name__ == "__main__":
    try:
        main()
    except (filings.bulk.Blocked, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ACCOUNTING_SEMANTIC_QUALIFICATION_BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from None
