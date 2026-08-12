"""Auditable XBRL provenance for normalized and derived fundamental values."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from trading_system.fundamentals.metrics import (
    balance_sheet_as_of,
    build_ttm,
    debt_as_of,
    discrete_quarters,
)
from trading_system.models.fundamentals import (
    BalanceSheetSnapshot,
    FundamentalDebugItem,
    FundamentalDebugReport,
    FundamentalFact,
    TTMFundamentals,
)

FLOW_LABELS = {
    "Revenue": "revenue",
    "Net Income": "net_income",
    "EPS": "eps_diluted",
    "Operating Income / EBIT": "operating_income",
    "Operating Cash Flow": "operating_cash_flow",
    "CapEx": "capital_expenditures",
}
BALANCE_LABELS = {
    "Debt": "total_debt",
    "Cash": "cash",
    "Shares Outstanding": "shares_outstanding",
}


def debug_fundamentals(
    symbol: str, facts: list[FundamentalFact], as_of: date
) -> FundamentalDebugReport:
    current = build_ttm(facts, as_of)
    balance = balance_sheet_as_of(facts, as_of)
    items = [
        _flow_item(label, metric, current, facts, as_of) for label, metric in FLOW_LABELS.items()
    ]
    items.append(_da_item(current, facts, as_of))
    items.extend(
        _balance_item(label, metric, balance, facts, as_of)
        for label, metric in BALANCE_LABELS.items()
    )
    items.append(_ebitda_item(current, facts, as_of))
    items.append(_derived_item("FCF", current.free_cash_flow, (items[4], items[5]), "OCF - CapEx"))
    return FundamentalDebugReport(symbol=symbol.upper(), as_of=as_of, items=tuple(items))


def _flow_item(
    label: str,
    metric: str,
    current: TTMFundamentals,
    facts: list[FundamentalFact],
    as_of: date,
) -> FundamentalDebugItem:
    value = getattr(current, metric)
    end = current.metric_period_ends.get(metric)
    quarters = [
        quarter
        for quarter in discrete_quarters(facts, metric, as_of)
        if end is None or quarter.period_end <= end
    ]
    if quarters:
        unit = quarters[-1].unit
        quarters = [quarter for quarter in quarters if quarter.unit == unit][-4:]
    sources = _unique_sources(source for quarter in quarters for source in quarter.sources)
    derivations = tuple(dict.fromkeys(quarter.formula for quarter in quarters))
    return _item(
        label,
        value,
        sources,
        quarters[-1].unit if quarters else None,
        "TTM = sum of four discrete quarters; " + "; ".join(derivations),
    )


def _balance_item(
    label: str,
    metric: str,
    balance: BalanceSheetSnapshot,
    facts: list[FundamentalFact],
    as_of: date,
) -> FundamentalDebugItem:
    value = getattr(balance, metric)
    if metric == "total_debt":
        value, sources = debt_as_of(facts, as_of)
        if sources:
            formula = (
                f"latest reported combined debt: {sources[0].tag}"
                if len(sources) == 1
                else "current debt + noncurrent debt from the same period"
            )
            return _item(label, value, sources, sources[0].unit, formula)
        return _item(label, value, (), None, "unavailable: no reliable mapped XBRL concept")
    matching = [fact for fact in facts if fact.metric == metric and fact.filed <= as_of]
    selected = max(matching, key=lambda fact: (fact.period_end, fact.filed), default=None)
    if selected:
        return _item(label, value, (selected,), selected.unit, f"latest reported {selected.tag}")
    return _item(label, value, (), None, "unavailable: no reliable mapped XBRL concept")


def _da_item(
    current: TTMFundamentals, facts: list[FundamentalFact], as_of: date
) -> FundamentalDebugItem:
    combined = _flow_item("D&A", "depreciation_amortization", current, facts, as_of)
    if combined.value is not None and combined.xbrl_concepts:
        return combined
    depreciation = _flow_item("Depreciation", "depreciation", current, facts, as_of)
    amortization = _flow_item("Amortization", "amortization", current, facts, as_of)
    return _derived_item(
        "D&A",
        current.depreciation_amortization,
        (depreciation, amortization),
        "Depreciation + Amortization of intangible assets; combined tag preferred",
    )


def _ebitda_item(
    current: TTMFundamentals, facts: list[FundamentalFact], as_of: date
) -> FundamentalDebugItem:
    if current.ebitda_formula and current.ebitda_formula.startswith("operating income"):
        components = (
            _flow_item("Operating Income", "operating_income", current, facts, as_of),
            _da_item(current, facts, as_of),
        )
    else:
        components = (
            _flow_item("Net Income", "net_income", current, facts, as_of),
            _flow_item("Interest Expense", "interest_expense", current, facts, as_of),
            _flow_item("Tax Expense", "tax_expense", current, facts, as_of),
            _da_item(current, facts, as_of),
        )
    return _derived_item(
        "EBITDA",
        current.ebitda,
        components,
        current.ebitda_formula or "unavailable: semantically aligned components not present",
    )


def _derived_item(
    name: str,
    value: Decimal | None,
    components: tuple[FundamentalDebugItem, ...],
    formula: str,
) -> FundamentalDebugItem:
    return FundamentalDebugItem(
        name=name,
        value=value,
        xbrl_concepts=tuple(dict.fromkeys(c for item in components for c in item.xbrl_concepts)),
        source_filings=tuple(dict.fromkeys(f for item in components for f in item.source_filings)),
        fiscal_periods=tuple(dict.fromkeys(p for item in components for p in item.fiscal_periods)),
        filed_dates=tuple(sorted(set(d for item in components for d in item.filed_dates))),
        unit=next((item.unit for item in components if item.unit), None),
        formula=formula,
    )


def _item(
    name: str,
    value: Decimal | None,
    sources: tuple[FundamentalFact, ...],
    unit: str | None,
    formula: str,
) -> FundamentalDebugItem:
    return FundamentalDebugItem(
        name=name,
        value=value,
        xbrl_concepts=tuple(dict.fromkeys(source.tag for source in sources)),
        source_filings=tuple(
            dict.fromkeys(f"{source.form} {source.accession_number or 'N/A'}" for source in sources)
        ),
        fiscal_periods=tuple(dict.fromkeys(source.fiscal_period or "N/A" for source in sources)),
        filed_dates=tuple(sorted({source.filed for source in sources})),
        unit=unit,
        formula=formula,
    )


def _unique_sources(sources) -> tuple[FundamentalFact, ...]:
    unique: dict[tuple[str, str | None, date, date], FundamentalFact] = {}
    for source in sources:
        key = (source.tag, source.accession_number, source.filed, source.period_end)
        unique[key] = source
    return tuple(unique.values())
