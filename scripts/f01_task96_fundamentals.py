"""Conservative shares candidates and Champion component diagnostics outside Canonical."""

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import f01_bulk_staging_rules as staging_rules
from f01_readiness_inventory import STAGE_DATABASE, STAGE_PLAN, read_database
from f01_task96_contract import STAGING, require

from trading_system.fundamentals.metrics import QuarterlyValue, ttm_from_quarters

RULESET = "F01_TASK96_COMPANYFACTS_RESEARCH_CANDIDATES_V1"
COVER = ("dei", "EntityCommonStockSharesOutstanding")
ASC = frozenset({"10-K", "10-Q", "20-F", "40-F"})
OCF = "operating_cash_flow"
SHARES = "shares_outstanding"
RESEARCH_ONLY = "RETROSPECTIVE_COMPANYFACTS_SCOPE_UNVERIFIED"
LOOKUPS = {
    "quality_growth_proxy": {"revenue", "eps_diluted", OCF},
    "quality_margin_proxy": {"operating_income", "revenue"},
    "quality_roic_proxy": {
        "operating_income",
        "tax_expense",
        "net_income",
        "total_equity",
        "cash",
        "total_debt",
    },
    "quality_leverage_proxy": {"total_debt", "operating_income", "depreciation_amortization"},
    "valuation_pe_proxy": {"eps_diluted", SHARES},
    "valuation_ev_proxy": {"operating_income", "total_debt", "cash", SHARES},
    "valuation_fcf_proxy": {OCF, "capital_expenditures", SHARES},
}


def observed_shares(taxonomy, tag, unit, raw, metadata):
    if (taxonomy, tag) != COVER:
        return "ISSUED_WEIGHTED_PREFERRED_OR_UNVERIFIED_CLASS", None
    if unit != "shares" or not isinstance(raw, dict):
        return "INVALID_SHARES_OBSERVATION", None
    end = staging_rules.day(raw.get("end"))
    filed = staging_rules.day(raw.get("filed"))
    accession = raw.get("accn")
    known = metadata.get(accession) if isinstance(accession, str) else None
    if (
        not end
        or not filed
        or not known
        or "start" in raw
        or "dimensions" in raw
        or raw.get("form") not in ASC
        or known["filed"] != raw.get("filed")
        or known["form"] != raw.get("form")
        or not known["acceptance_utc"]
        or not 0 <= (filed - end).days <= 460
    ):
        return "SHARES_PERIOD_FORM_OR_ACCEPTANCE_UNBOUND", None
    try:
        value = Decimal(str(raw["val"]))
    except (KeyError, InvalidOperation, ValueError):
        return "INVALID_SHARES_VALUE", None
    if (
        not value.is_finite()
        or not 0 < value < Decimal(10**18)
        or value != value.to_integral_value()
    ):
        return "INVALID_SHARES_VALUE", None
    return "COVER_TAG_CANDIDATE", {
        "taxonomy": taxonomy,
        "tag": tag,
        "unit": unit,
        "period_end": end.isoformat(),
        "filed": filed.isoformat(),
        "accession": accession,
        "reported_value": str(int(value)),
        "acceptance_utc": known["acceptance_utc"],
        "source_raw": raw,
        "reporting_scope": "UNVERIFIED_ISSUER_OR_SHARE_CLASS",
        "status": RESEARCH_ONLY,
        "original_pit_qualified": False,
    }


def choose_shares(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["cik"], row["accession"], row["period_end"]].append(row)
    candidates, blocked = [], []
    for members in groups.values():
        if len({r["reported_value"] for r in members}) != 1:
            blocked.extend(members)
        else:
            candidates.append(members[0] | {"source_ordinals": [r["ordinal"] for r in members]})
    return candidates, blocked


def ocf_ttm(rows):
    """Use only four adjacent, direct quarterly amounts from their own original filings."""
    by_end, results = defaultdict(list), []
    for row in rows:
        if not row["acceptance_utc"] or not row["start"] or row["report_date"] != row["end"]:
            continue
        start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        if 60 <= (end - start).days <= 120 and row["form"] in {"10-K", "10-Q"}:
            by_end[end].append((start, row))
    dates = sorted(by_end)
    for ix, end in enumerate(dates[3:], 3):
        if len(by_end[end]) != 1:
            continue
        _, target = by_end[end][0]
        cutoff = target["acceptance_utc"]
        selection = [
            [(start, row) for start, row in by_end[d] if row["acceptance_utc"] <= cutoff]
            for d in dates[ix - 3 : ix + 1]
        ]
        if any(len(xs) != 1 for xs in selection):
            continue
        four = [xs[0] for xs in selection]
        ends = dates[ix - 3 : ix + 1]
        if any(
            start not in {prior, prior + timedelta(days=1)}
            for (start, _), prior in zip(four[1:], ends[:-1], strict=True)
        ):
            continue
        values = [
            QuarterlyValue(
                period,
                date.fromisoformat(row["filed"]),
                Decimal(row["value"]),
                "USD",
                (),
                "DIRECT_COMPARABLE_CANDIDATE",
            )
            for (_, row), period in zip(four, ends, strict=True)
        ]
        value, _, _ = ttm_from_quarters(values)
        if value is not None:
            results.append(
                {
                    "end": end.isoformat(),
                    "candidate_ttm_value": str(value),
                    "acceptance_cutoff": cutoff,
                    "source_candidate_ids": [row["id"] for _, row in four],
                    "original_pit_qualified": False,
                    "status": RESEARCH_ONLY,
                }
            )
    return results


def analyze(plan):
    coverage, roc = defaultdict(Counter), defaultdict(list)
    observations, shares, tag_inventory = Counter(), [], Counter()
    sources = defaultdict(dict)
    for pair, row in plan.filings.items():
        sources[pair[0]][pair[1]] = row
    meta, _ = plan.metadata(plan.filings.values())
    missing_original = defaultdict(set)
    with read_database(
        STAGING / "companyfacts-staging.sqlite3", plan.inputs.files, STAGE_DATABASE
    ) as db:
        require(
            db.execute("SELECT plan_sha256 FROM staging_binding").fetchone() == (STAGE_PLAN,)
            and dict(db.execute("SELECT cik,symbol_label FROM issuer")) == plan.labels,
            "STAGING_BINDING_MISMATCH",
        )
        stage_counts = dict(db.execute("SELECT disposition,COUNT(*) FROM observation GROUP BY 1"))
        for cik, metric, year, count, n in db.execute(
            """SELECT cik,metric,substr(period_end,1,4),COUNT(*),COUNT(acceptance_utc)
               FROM candidate GROUP BY 1,2,3"""
        ):
            coverage[cik, year, metric].update(normalized=count, acceptance=n)
        for cik, metric, end, accession in db.execute(
            "SELECT cik,metric,period_end,accession FROM candidate"
        ):
            missing_original[cik, end[:4], metric].add(accession)
        for row in db.execute(
            """SELECT cik,taxonomy,tag,unit,member_unit_ordinal,payload_json
            FROM observation WHERE lower(tag) LIKE '%shares%outstanding%'
            OR lower(tag) LIKE '%shares%issued%'
            ORDER BY cik,taxonomy,tag,unit,member_unit_ordinal"""
        ):
            cik, taxonomy, tag, unit, ordinal, payload = row
            raw = json.loads(payload, parse_float=Decimal)
            # Keep raw provenance as the exact SQLite payload instead of recalculating its JSON.
            status, candidate = observed_shares(taxonomy, tag, unit, raw, sources[cik])
            tag_inventory[taxonomy, tag, unit, status] += 1
            if (taxonomy, tag) == COVER and isinstance(raw, dict):
                end = staging_rules.day(raw.get("end"))
                if end:
                    coverage[cik, str(end.year), SHARES]["raw"] += 1
            observations[status] += 1
            if candidate:
                candidate["source_raw"] = payload
                shares.append(candidate | {"cik": cik, "ordinal": ordinal})
        for cik, year, metric, taxonomy, tag, unit, count in db.execute(
            """SELECT cik,substr(json_extract(payload_json,'$.end'),1,4),
            metric,taxonomy,tag,unit,COUNT(*) FROM observation WHERE metric IS NOT NULL
            GROUP BY 1,2,3,4,5,6"""
        ):
            coverage[cik, year, metric]["raw"] += count
            tag_inventory[taxonomy, tag, unit, "TASK94_MAPPED_OR_QUARANTINED"] += count
        semantic_legacy = (
            "Revenue",
            "NetIncome",
            "ProfitLoss",
            "OperatingIncome",
            "CashAndCashEquivalents",
            "StockholdersEquity",
            "Equity",
            "CashFlowsFromUsedInOperatingActivities",
            "GrossDebt",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "Borrowings",
            "CurrentBorrowings",
            "NoncurrentBorrowings",
        )
        placeholders = ",".join("?" for _ in semantic_legacy)
        for taxonomy, tag, unit, count in db.execute(
            f"""SELECT taxonomy,tag,unit,COUNT(*) FROM observation
            WHERE metric IS NULL AND tag IN ({placeholders}) GROUP BY 1,2,3""",
            semantic_legacy,
        ):
            tag_inventory[taxonomy, tag, unit, "ALTERNATE_REPORTING_SCOPE_UNRESOLVED"] += count
        for cik, identifier, start, end, filed, accession, value, acceptance in db.execute(
            """SELECT cik,candidate_id,period_start,period_end,filed,accession,value_decimal,
               acceptance_utc FROM candidate WHERE metric=? AND unit='USD'
               AND taxonomy='us-gaap' AND tag='NetCashProvidedByUsedInOperatingActivities'
               ORDER BY cik,period_end,candidate_id""",
            (OCF,),
        ):
            filing = meta.get((cik, accession), {})
            roc[cik].append(
                {
                    "id": identifier,
                    "start": start,
                    "end": end,
                    "filed": filed,
                    "value": value,
                    "form": filing.get("form"),
                    "report_date": filing.get("report_date"),
                    "acceptance_utc": acceptance,
                }
            )
    shares, blocked = choose_shares(shares)
    for row in shares:
        k = row["cik"], row["period_end"][:4], SHARES
        coverage[k]["additional"] += 1
        missing_original[k].add(row["accession"])
    for row in blocked:
        coverage[row["cik"], row["period_end"][:4], SHARES]["conflicts"] += 1
    windows = []
    for cik, rows in sorted(roc.items()):
        for row in ocf_ttm(rows):
            windows.append(row | {"cik": cik})
            coverage[cik, row["end"][:4], OCF]["unsafe_for_pit_ttm_windows"] += 1
    return {
        "coverage": coverage,
        "originals_by_cell": missing_original,
        "stage_dispositions": stage_counts,
        "shares": shares,
        "share_conflicts": blocked,
        "share_observation_counts": dict(observations),
        "ocf_windows": windows,
        "raw_tags": [
            dict(taxonomy=t, tag=n, unit=u, disposition=s, count=c)
            for (t, n, u, s), c in sorted(tag_inventory.items())
        ],
    }


def coverage_rows(plan, findings, originals):
    metrics = (*staging_rules.METRICS, SHARES)
    for cik, symbol in sorted(plan.labels.items()):
        for year in range(2010, 2027):
            available = set()
            for metric in metrics:
                k = cik, str(year), metric
                counts = findings["coverage"][k]
                missing = sum(
                    originals.get((cik, acc), {}).get("status") != "ORIGINAL_VERIFIED"
                    for acc in findings["originals_by_cell"][k]
                )
                if counts["normalized"] or counts["additional"]:
                    available.add(metric)
                yield {
                    "cik": cik,
                    "symbol": symbol,
                    "year": year,
                    "item": metric,
                    "raw_observations": counts["raw"],
                    "prior_candidates": counts["normalized"],
                    "additional_candidates": counts["additional"],
                    "provisional_acceptance": counts["acceptance"],
                    "unresolved_conflicts": counts["conflicts"],
                    "raw_ttm_windows": counts["unsafe_for_pit_ttm_windows"],
                    "accessions_without_verified_original": missing,
                    "component_presence_proxy": "",
                    "original_pit_qualified_new": 0,
                }
            for name, needed in sorted(LOOKUPS.items()):
                yield {
                    "cik": cik,
                    "symbol": symbol,
                    "year": year,
                    "item": name,
                    "raw_observations": "",
                    "prior_candidates": "",
                    "additional_candidates": "",
                    "provisional_acceptance": "",
                    "unresolved_conflicts": "",
                    "raw_ttm_windows": "",
                    "accessions_without_verified_original": "",
                    "component_presence_proxy": ";".join(sorted(needed - available))
                    or "PRESENT_UNALIGNED",
                    "original_pit_qualified_new": 0,
                }
