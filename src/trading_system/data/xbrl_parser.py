"""Normalize heterogeneous SEC US-GAAP Company Facts tags."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_system.models.fundamentals import FundamentalFact

TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditures": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "total_debt": (
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligations",
        "DebtLongtermAndShorttermCombinedAmount",
        "DebtAndCapitalLeaseObligations",
    ),
    "debt_current": (
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "ShortTermBorrowings",
        "NotesPayableCurrent",
    ),
    "debt_noncurrent": (
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebtNoncurrent",
        "LongTermNotesAndLoans",
        "LongTermDebt",
    ),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "total_assets": ("Assets",),
    "total_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "shares_outstanding": (
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
    ),
    "interest_expense": (
        "InterestExpenseNonOperating",
        "InterestAndDebtExpense",
        "InterestExpense",
    ),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ),
    "depreciation": ("Depreciation",),
    "amortization": (
        "AmortizationOfIntangibleAssets",
        "FiniteLivedIntangibleAssetsAmortizationExpense",
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
    gaap = payload.get("facts", {}).get("us-gaap", {})
    if not isinstance(gaap, Mapping):
        return []
    parsed: list[FundamentalFact] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for metric, aliases in TAG_ALIASES.items():
        # The alias order is the documented semantic preference, not a fallback
        # that discards other tags. Dedupe removes identical filing observations.
        for tag in aliases:
            concept = gaap.get(tag)
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
                key = (metric, raw["filed"], raw["end"], str(accession), unit)
                if key in seen:
                    continue
                seen.add(key)
                parsed.append(
                    FundamentalFact(
                        cik=cik,
                        symbol=symbol.upper(),
                        metric=metric,
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
