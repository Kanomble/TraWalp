"""Normalize heterogeneous SEC Company Facts concepts across taxonomies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_system.models.fundamentals import FundamentalFact

TAG_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "SalesRevenueGoodsNet"),
    ),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "net_income": (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
    "eps_diluted": (
        ("us-gaap", "EarningsPerShareDiluted"),
        ("us-gaap", "EarningsPerShareBasicAndDiluted"),
    ),
    "operating_cash_flow": (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),),
    "capital_expenditures": (
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsForAdditionsToPropertyPlantAndEquipment"),
    ),
    "cash": (
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "total_debt": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligations"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ("us-gaap", "DebtLongtermAndShorttermCombinedAmount"),
        ("us-gaap", "DebtAndCapitalLeaseObligations"),
    ),
    "debt_current": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsCurrent"),
        ("us-gaap", "LongTermDebtCurrent"),
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "NotesPayableCurrent"),
    ),
    "debt_noncurrent": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"),
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermNotesAndLoans"),
        ("us-gaap", "LongTermDebt"),
    ),
    "current_assets": (("us-gaap", "AssetsCurrent"),),
    "current_liabilities": (("us-gaap", "LiabilitiesCurrent"),),
    "total_assets": (("us-gaap", "Assets"),),
    "total_equity": (
        ("us-gaap", "StockholdersEquity"),
        (
            "us-gaap",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    "shares_outstanding": (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ),
    "interest_expense": (
        ("us-gaap", "InterestExpenseNonOperating"),
        ("us-gaap", "InterestAndDebtExpense"),
        ("us-gaap", "InterestExpense"),
    ),
    "tax_expense": (("us-gaap", "IncomeTaxExpenseBenefit"),),
    "depreciation_amortization": (
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment"),
    ),
    "depreciation": (("us-gaap", "Depreciation"),),
    "amortization": (
        ("us-gaap", "AmortizationOfIntangibleAssets"),
        ("us-gaap", "FiniteLivedIntangibleAssetsAmortizationExpense"),
    ),
}

PREFERRED_UNITS: dict[str, tuple[str, ...]] = {
    "eps_diluted": ("USD/shares",),
    "shares_outstanding": ("shares",),
}
VALID_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F"}


def _unit_items(units: Mapping[str, Any], metric: str) -> Iterable[tuple[str, dict[str, Any]]]:
    preferred = PREFERRED_UNITS.get(metric, ("USD",))
    ordered = [*preferred, *(unit for unit in units if unit not in preferred)]
    for unit in ordered:
        observations = units.get(unit, [])
        if isinstance(observations, list):
            for observation in observations:
                if isinstance(observation, dict):
                    yield unit, observation


def parse_company_facts(payload: Mapping[str, Any], symbol: str) -> list[FundamentalFact]:
    """Return normalized facts without merging distinct filing observations.

    Keeping amendments and duplicate periods is intentional: point-in-time selection
    happens using ``filed`` and accession number in the database layer.
    """

    cik = str(payload.get("cik", "")).zfill(10)
    taxonomy_facts = payload.get("facts", {})
    if not isinstance(taxonomy_facts, Mapping):
        return []
    parsed: list[FundamentalFact] = []
    seen: set[tuple[str, str, str | None, str, str, str]] = set()
    for metric, aliases in TAG_ALIASES.items():
        # The alias order is the documented semantic preference, not a fallback
        # that discards other tags. Dedupe removes identical filing observations.
        for taxonomy, tag in aliases:
            concepts = taxonomy_facts.get(taxonomy, {})
            if not isinstance(concepts, Mapping):
                continue
            concept = concepts.get(tag)
            if not isinstance(concept, Mapping):
                continue
            units = concept.get("units", {})
            if not isinstance(units, Mapping):
                continue
            for unit, raw in _unit_items(units, metric):
                if raw.get("form") not in VALID_FORMS or not raw.get("filed") or not raw.get("end"):
                    continue
                try:
                    value = Decimal(str(raw["val"]))
                    filed = date.fromisoformat(raw["filed"])
                    period_end = date.fromisoformat(raw["end"])
                    period_start = date.fromisoformat(raw["start"]) if raw.get("start") else None
                except (InvalidOperation, ValueError, KeyError, TypeError):
                    continue
                accession = raw.get("accn")
                key = (
                    metric,
                    raw["filed"],
                    raw.get("start"),
                    raw["end"],
                    str(accession),
                    unit,
                )
                if key in seen:
                    continue
                seen.add(key)
                parsed.append(
                    FundamentalFact(
                        cik=cik,
                        symbol=symbol.upper(),
                        metric=metric,
                        taxonomy=taxonomy,
                        tag=tag,
                        value=value,
                        unit=unit,
                        period_start=period_start,
                        period_end=period_end,
                        filed=filed,
                        fiscal_year=raw.get("fy"),
                        fiscal_period=raw.get("fp"),
                        form=raw["form"],
                        accession_number=accession,
                        frame=raw.get("frame"),
                    )
                )
    return sorted(parsed, key=lambda fact: (fact.filed, fact.period_end, fact.metric, fact.tag))
