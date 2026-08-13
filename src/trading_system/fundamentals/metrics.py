"""Pure fundamental, TTM and valuation calculations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from trading_system.models.fundamentals import (
    BalanceSheetSnapshot,
    FundamentalFact,
    FundamentalMetrics,
    TTMFundamentals,
)

FLOW_METRICS = (
    "revenue",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "capital_expenditures",
    "depreciation_amortization",
    "depreciation",
    "amortization",
    "tax_expense",
    "interest_expense",
)
INSTANT_METRICS = (
    "cash",
    "total_debt",
    "debt_current",
    "debt_noncurrent",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_equity",
)

# Company Facts includes annual foreign filers as well as quarterly domestic filers.
# Fifteen months permits a normal annual filing cycle plus one quarter of timing
# variation, while preventing multi-year-old counts from driving current valuation.
MAX_SHARES_OUTSTANDING_AGE = timedelta(days=460)
SHARE_TAXONOMY_PRIORITY = {"us-gaap": 0, "dei": 1}


@dataclass(frozen=True)
class QuarterlyValue:
    period_end: date
    filed: date
    value: Decimal
    unit: str
    sources: tuple[FundamentalFact, ...]
    formula: str


def safe_ratio(numerator: Decimal | None, denominator: Decimal | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return float(numerator / denominator)


def growth_rate(current: Decimal | None, prior: Decimal | None) -> float | None:
    """Growth is unavailable for non-positive comparison bases."""

    if current is None or prior is None or prior <= 0:
        return None
    return float(current / prior - Decimal(1))


def free_cash_flow(
    operating_cash_flow: Decimal | None, capital_expenditures: Decimal | None
) -> Decimal | None:
    if operating_cash_flow is None or capital_expenditures is None:
        return None
    return operating_cash_flow - capital_expenditures


def ebitda(
    operating_income: Decimal | None, depreciation_amortization: Decimal | None
) -> Decimal | None:
    if operating_income is None or depreciation_amortization is None:
        return None
    return operating_income + depreciation_amortization


def effective_tax_rate(tax_expense: Decimal | None, net_income: Decimal | None) -> float | None:
    """Approximate pretax income as net income plus tax expense."""

    if tax_expense is None or net_income is None:
        return None
    pretax_income = net_income + tax_expense
    if pretax_income <= 0:
        return None
    rate = float(tax_expense / pretax_income)
    return rate if 0 <= rate <= 1 else None


def roic(
    operating_income: Decimal | None,
    tax_rate: float | None,
    current_invested_capital: Decimal | None,
    prior_invested_capital: Decimal | None,
) -> float | None:
    if (
        operating_income is None
        or tax_rate is None
        or current_invested_capital is None
        or prior_invested_capital is None
    ):
        return None
    average_capital = (current_invested_capital + prior_invested_capital) / 2
    if average_capital <= 0:
        return None
    nopat = operating_income * Decimal(str(1 - tax_rate))
    return float(nopat / average_capital)


def invested_capital(snapshot: BalanceSheetSnapshot) -> Decimal | None:
    if snapshot.total_debt is None or snapshot.total_equity is None or snapshot.cash is None:
        return None
    return snapshot.total_debt + snapshot.total_equity - snapshot.cash


def debt_to_ebitda(total_debt: Decimal | None, ttm_ebitda: Decimal | None) -> float | None:
    if total_debt is None or ttm_ebitda is None or ttm_ebitda <= 0:
        return None
    return float(total_debt / ttm_ebitda)


def price_to_earnings(price: Decimal | None, eps_ttm: Decimal | None) -> float | None:
    if price is None or eps_ttm is None or price <= 0 or eps_ttm <= 0:
        return None
    return float(price / eps_ttm)


def enterprise_value(
    market_cap: Decimal | None, total_debt: Decimal | None, cash: Decimal | None
) -> Decimal | None:
    if market_cap is None or total_debt is None or cash is None:
        return None
    return market_cap + total_debt - cash


def ev_to_ebitda(ev: Decimal | None, ttm_ebitda: Decimal | None) -> float | None:
    if ev is None or ttm_ebitda is None or ttm_ebitda <= 0:
        return None
    return float(ev / ttm_ebitda)


def ev_to_ebit(ev: Decimal | None, ttm_ebit: Decimal | None) -> float | None:
    if ev is None or ttm_ebit is None or ttm_ebit <= 0:
        return None
    return float(ev / ttm_ebit)


def fcf_yield(fcf: Decimal | None, market_cap: Decimal | None) -> float | None:
    if fcf is None or market_cap is None or market_cap <= 0:
        return None
    return float(fcf / market_cap)


def _latest_unique_periods(facts: list[FundamentalFact]) -> list[FundamentalFact]:
    latest: dict[tuple[date | None, date, str], FundamentalFact] = {}
    for fact in sorted(facts, key=lambda item: item.filed):
        latest[(fact.period_start, fact.period_end, fact.unit)] = fact
    return list(latest.values())


def discrete_quarters(
    facts: list[FundamentalFact], metric: str, as_of: date
) -> list[QuarterlyValue]:
    """Derive standalone quarters from discrete and cumulative SEC observations."""

    eligible = [
        fact
        for fact in facts
        if fact.metric == metric
        and fact.filed <= as_of
        and fact.period_start is not None
        and 60 <= (fact.period_end - fact.period_start).days <= 400
    ]
    unique = _latest_unique_periods(eligible)
    quarters: dict[tuple[date, str], QuarterlyValue] = {}

    # Direct quarter facts are preferable when the SEC supplies them.
    for fact in unique:
        duration = (fact.period_end - fact.period_start).days
        if 60 <= duration <= 120:
            quarters[(fact.period_end, fact.unit)] = QuarterlyValue(
                fact.period_end,
                fact.filed,
                fact.value,
                fact.unit,
                (fact,),
                f"reported discrete quarter: {fact.tag}",
            )

    # Cash flow statements commonly expose H1/9M/FY cumulatives. Difference adjacent values.
    by_start_unit: dict[tuple[date, str], list[FundamentalFact]] = defaultdict(list)
    for fact in unique:
        by_start_unit[(fact.period_start, fact.unit)].append(fact)  # type: ignore[arg-type]
    for (_period_start, unit), observations in by_start_unit.items():
        ordered = sorted(observations, key=lambda item: item.period_end)
        previous: FundamentalFact | None = None
        for observation in ordered:
            duration = (observation.period_end - observation.period_start).days  # type: ignore[operator]
            if previous is None:
                previous = observation
                continue
            previous_duration = (previous.period_end - previous.period_start).days  # type: ignore[operator]
            if (
                60 <= observation.period_end.toordinal() - previous.period_end.toordinal() <= 120
                and previous_duration >= 60
                and duration > previous_duration
            ):
                key = (observation.period_end, unit)
                quarters.setdefault(
                    key,
                    QuarterlyValue(
                        observation.period_end,
                        max(previous.filed, observation.filed),
                        observation.value - previous.value,
                        unit,
                        (observation, previous),
                        f"cumulative {observation.tag} minus prior cumulative {previous.tag}",
                    ),
                )
            previous = observation
    return sorted(quarters.values(), key=lambda quarter: (quarter.period_end, quarter.unit))


def ttm_value(
    facts: list[FundamentalFact],
    metric: str,
    as_of: date,
    *,
    end_on_or_before: date | None = None,
) -> tuple[Decimal | None, date | None, date | None]:
    quarters = discrete_quarters(facts, metric, as_of)
    if end_on_or_before:
        quarters = [quarter for quarter in quarters if quarter.period_end <= end_on_or_before]
    if not quarters:
        return None, None, None
    # Never mix units; use the unit of the most recent observation.
    latest_unit = quarters[-1].unit
    quarters = [quarter for quarter in quarters if quarter.unit == latest_unit]
    latest_four = quarters[-4:]
    if len(latest_four) != 4:
        return None, None, None
    gaps = [
        (current.period_end - previous.period_end).days
        for previous, current in zip(latest_four[:-1], latest_four[1:], strict=True)
    ]
    if any(gap < 60 or gap > 130 for gap in gaps):
        return None, None, None
    return (
        sum((quarter.value for quarter in latest_four), Decimal(0)),
        latest_four[-1].period_end,
        max(quarter.filed for quarter in latest_four),
    )


def build_ttm(
    facts: list[FundamentalFact],
    as_of: date,
    *,
    end_on_or_before: date | None = None,
    end_by_metric: dict[str, date] | None = None,
) -> TTMFundamentals:
    values: dict[str, Decimal | None] = {}
    ends: dict[str, date] = {}
    filed_dates: dict[str, date] = {}
    for metric in FLOW_METRICS:
        value, period_end, filed = ttm_value(
            facts,
            metric,
            as_of,
            end_on_or_before=(end_by_metric or {}).get(metric, end_on_or_before),
        )
        values[metric] = value
        if period_end:
            ends[metric] = period_end
        if filed:
            filed_dates[metric] = filed
    values["free_cash_flow"] = (
        free_cash_flow(values["operating_cash_flow"], values["capital_expenditures"])
        if ends.get("operating_cash_flow") == ends.get("capital_expenditures")
        else None
    )
    da_formula: str | None = None
    if values["depreciation_amortization"] is not None:
        da_formula = "reported combined depreciation and amortization"
    elif (
        values["depreciation"] is not None
        and values["amortization"] is not None
        and ends.get("depreciation") == ends.get("amortization")
    ):
        values["depreciation_amortization"] = (
            values["depreciation"] + values["amortization"]  # type: ignore[operator]
        )
        ends["depreciation_amortization"] = ends["depreciation"]
        filed_dates["depreciation_amortization"] = max(
            filed_dates["depreciation"], filed_dates["amortization"]
        )
        da_formula = "depreciation + amortization of intangible assets"
    values["ebit"] = values["operating_income"]
    if values["ebit"] is not None:
        ends["ebit"] = ends["operating_income"]
        filed_dates["ebit"] = filed_dates["operating_income"]
    ebitda_formula: str | None = None
    values["ebitda"] = None
    if (
        values["operating_income"] is not None
        and values["depreciation_amortization"] is not None
        and ends.get("operating_income") == ends.get("depreciation_amortization")
    ):
        values["ebitda"] = ebitda(values["operating_income"], values["depreciation_amortization"])
        ebitda_formula = f"operating income (EBIT) + D&A ({da_formula})"
    else:
        bridge_metrics = (
            "net_income",
            "interest_expense",
            "tax_expense",
            "depreciation_amortization",
        )
        bridge_ends = {ends.get(metric) for metric in bridge_metrics}
        if all(values[metric] is not None for metric in bridge_metrics) and len(bridge_ends) == 1:
            values["ebitda"] = sum(
                (values[metric] for metric in bridge_metrics),  # type: ignore[arg-type]
                Decimal(0),
            )
            ebitda_formula = "net income + interest expense + tax expense + D&A"
    if values["free_cash_flow"] is not None:
        ends["free_cash_flow"] = ends["operating_cash_flow"]
        filed_dates["free_cash_flow"] = max(
            filed_dates["operating_cash_flow"], filed_dates["capital_expenditures"]
        )
    if values["ebitda"] is not None:
        ebitda_sources = (
            ("operating_income", "depreciation_amortization")
            if values["operating_income"] is not None
            and ends.get("operating_income") == ends.get("depreciation_amortization")
            else ("net_income", "interest_expense", "tax_expense", "depreciation_amortization")
        )
        ends["ebitda"] = ends[ebitda_sources[0]]
        filed_dates["ebitda"] = max(filed_dates[metric] for metric in ebitda_sources)
    return TTMFundamentals(
        period_end=max(ends.values()) if ends else None,
        available_date=max(filed_dates.values()) if filed_dates else None,
        metric_period_ends=ends,
        metric_available_dates=filed_dates,
        ebitda_formula=ebitda_formula,
        **values,
    )


def balance_sheet_as_of(
    facts: list[FundamentalFact], as_of: date, *, end_on_or_before: date | None = None
) -> BalanceSheetSnapshot:
    values: dict[str, Decimal | None] = {}
    ends: list[date] = []
    for metric in INSTANT_METRICS:
        eligible = [
            fact
            for fact in facts
            if fact.metric == metric
            and fact.filed <= as_of
            and (end_on_or_before is None or fact.period_end <= end_on_or_before)
        ]
        selected = max(eligible, key=lambda item: (item.period_end, item.filed), default=None)
        values[metric] = selected.value if selected else None
        if selected:
            ends.append(selected.period_end)
    selected_shares = shares_outstanding_as_of(
        facts, as_of, end_on_or_before=end_on_or_before
    )
    if selected_shares:
        ends.append(selected_shares.period_end)
    total_debt, debt_sources = debt_as_of(facts, as_of, end_on_or_before=end_on_or_before)
    ends.extend(source.period_end for source in debt_sources)
    return BalanceSheetSnapshot(
        period_end=max(ends) if ends else None,
        cash=values["cash"],
        total_debt=total_debt,
        current_assets=values["current_assets"],
        current_liabilities=values["current_liabilities"],
        total_assets=values["total_assets"],
        total_equity=values["total_equity"],
        shares_outstanding=selected_shares.value if selected_shares else None,
    )


def shares_outstanding_as_of(
    facts: list[FundamentalFact],
    as_of: date,
    *,
    end_on_or_before: date | None = None,
) -> FundamentalFact | None:
    """Select a recent, point-in-time share count with deterministic taxonomy priority.

    Measurement-period recency wins first.  For otherwise equivalent observations,
    the SEC DEI entity share count wins over the legacy US-GAAP fallback.  Both the
    measurement and filing must be recent enough to avoid valuing current equity with
    a historical pre-split count repeated in a later filing.
    """

    eligible = [
        fact
        for fact in facts
        if fact.metric == "shares_outstanding"
        and fact.unit == "shares"
        and fact.value > 0
        and fact.period_end <= as_of
        and fact.filed <= as_of
        and (end_on_or_before is None or fact.period_end <= end_on_or_before)
    ]
    selected = max(
        eligible,
        key=lambda fact: (
            fact.period_end,
            fact.filed,
            SHARE_TAXONOMY_PRIORITY.get(fact.taxonomy, -1),
            fact.accession_number or "",
            fact.tag,
            fact.value,
        ),
        default=None,
    )
    if selected is None:
        return None
    if (
        as_of - selected.filed > MAX_SHARES_OUTSTANDING_AGE
        or as_of - selected.period_end > MAX_SHARES_OUTSTANDING_AGE
    ):
        return None
    return selected


def debt_as_of(
    facts: list[FundamentalFact],
    as_of: date,
    *,
    end_on_or_before: date | None = None,
) -> tuple[Decimal | None, tuple[FundamentalFact, ...]]:
    """Select the newest reliable total-debt representation available point in time.

    A directly reported combined debt concept and the sum of current plus non-current
    debt are competing representations.  Recency wins between them; concept priority
    must never allow a stale direct fact to suppress newer, period-aligned components.
    """

    def eligible(metric: str) -> list[FundamentalFact]:
        return [
            fact
            for fact in facts
            if fact.metric == metric
            and fact.filed <= as_of
            and (end_on_or_before is None or fact.period_end <= end_on_or_before)
        ]

    direct = max(
        eligible("total_debt"), key=lambda item: (item.period_end, item.filed), default=None
    )
    candidates: list[tuple[date, date, Decimal, tuple[FundamentalFact, ...]]] = []
    if direct is not None:
        candidates.append((direct.period_end, direct.filed, direct.value, (direct,)))

    debt_parts: dict[str, dict[date, FundamentalFact]] = {}
    for metric in ("debt_current", "debt_noncurrent"):
        latest_by_end: dict[date, FundamentalFact] = {}
        for fact in sorted(eligible(metric), key=lambda item: item.filed):
            latest_by_end[fact.period_end] = fact
        debt_parts[metric] = latest_by_end
    common_ends = debt_parts["debt_current"].keys() & debt_parts["debt_noncurrent"].keys()
    if common_ends:
        debt_end = max(common_ends)
        sources = (
            debt_parts["debt_current"][debt_end],
            debt_parts["debt_noncurrent"][debt_end],
        )
        candidates.append(
            (
                debt_end,
                max(source.filed for source in sources),
                sum(s.value for s in sources),
                sources,
            )
        )

    if not candidates:
        return None, ()
    _, _, value, sources = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return value, sources


def calculate_fundamental_metrics(
    current: TTMFundamentals,
    prior_year: TTMFundamentals,
    balance: BalanceSheetSnapshot,
    prior_balance: BalanceSheetSnapshot,
    price: Decimal | None,
) -> FundamentalMetrics:
    market_cap = (
        price * balance.shares_outstanding
        if price is not None and balance.shares_outstanding is not None and price > 0
        else None
    )
    tax_rate = effective_tax_rate(current.tax_expense, current.net_income)
    ev = enterprise_value(market_cap, balance.total_debt, balance.cash)
    return FundamentalMetrics(
        revenue_growth=growth_rate(current.revenue, prior_year.revenue),
        eps_growth=growth_rate(current.eps_diluted, prior_year.eps_diluted),
        operating_cash_flow_growth=growth_rate(
            current.operating_cash_flow, prior_year.operating_cash_flow
        ),
        operating_cash_flow_positive=(
            current.operating_cash_flow > 0 if current.operating_cash_flow is not None else None
        ),
        operating_margin=safe_ratio(current.operating_income, current.revenue),
        effective_tax_rate=tax_rate,
        roic=roic(
            current.operating_income,
            tax_rate,
            invested_capital(balance),
            invested_capital(prior_balance),
        ),
        debt_to_ebitda=debt_to_ebitda(balance.total_debt, current.ebitda),
        market_cap=market_cap,
        pe=price_to_earnings(price, current.eps_diluted),
        enterprise_value=ev,
        ev_to_ebitda=ev_to_ebitda(ev, current.ebitda),
        ev_to_ebit=ev_to_ebit(ev, current.ebit),
        fcf_yield=fcf_yield(current.free_cash_flow, market_cap),
    )


def current_and_prior_ttm(
    facts: list[FundamentalFact], as_of: date
) -> tuple[TTMFundamentals, TTMFundamentals]:
    current = build_ttm(facts, as_of)
    cutoffs = {
        metric: period_end - timedelta(days=300)
        for metric, period_end in current.metric_period_ends.items()
        if metric in FLOW_METRICS
    }
    prior = build_ttm(facts, as_of, end_by_metric=cutoffs) if cutoffs else TTMFundamentals()
    return current, prior
