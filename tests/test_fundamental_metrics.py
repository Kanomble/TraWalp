from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading_system.fundamentals.debug import debug_fundamentals
from trading_system.fundamentals.metrics import (
    balance_sheet_as_of,
    build_ttm,
    calculate_fundamental_metrics,
    debt_to_ebitda,
    enterprise_value,
    ev_to_ebit,
    ev_to_ebitda,
    fcf_yield,
    free_cash_flow,
    growth_rate,
    price_to_earnings,
    roic,
)
from trading_system.fundamentals.quality import analyze_fundamentals
from trading_system.models.fundamentals import (
    BalanceSheetSnapshot,
    FundamentalFact,
    TTMFundamentals,
)


def fact(
    metric: str,
    value: str,
    start: date | None,
    end: date,
    filed: date,
    *,
    unit: str = "USD",
    fiscal_period: str = "Q1",
) -> FundamentalFact:
    return FundamentalFact(
        cik="0000000001",
        symbol="TEST",
        metric=metric,
        tag=metric,
        value=Decimal(value),
        unit=unit,
        period_start=start,
        period_end=end,
        filed=filed,
        fiscal_year=end.year,
        fiscal_period=fiscal_period,
        form="10-K" if fiscal_period == "FY" else "10-Q",
        accession_number=f"{metric}-{end}-{filed}",
    )


def cumulative_year(metric: str = "revenue") -> list[FundamentalFact]:
    start = date(2024, 1, 1)
    return [
        fact(metric, "100", start, date(2024, 3, 31), date(2024, 5, 5)),
        fact(metric, "250", start, date(2024, 6, 30), date(2024, 8, 5), fiscal_period="Q2"),
        fact(metric, "400", start, date(2024, 9, 30), date(2024, 11, 5), fiscal_period="Q3"),
        fact(metric, "600", start, date(2024, 12, 31), date(2025, 2, 15), fiscal_period="FY"),
    ]


def test_ttm_derives_discrete_quarters_from_cumulative_filings() -> None:
    ttm = build_ttm(cumulative_year(), date(2025, 2, 16))
    assert ttm.revenue == Decimal("600")
    assert ttm.period_end == date(2024, 12, 31)
    assert ttm.available_date == date(2025, 2, 15)


def test_ttm_does_not_see_annual_filing_early() -> None:
    ttm = build_ttm(cumulative_year(), date(2025, 2, 14))
    assert ttm.revenue is None
    assert ttm.available_date is None


def test_derived_fcf_requires_matching_ttm_periods() -> None:
    ocf = cumulative_year("operating_cash_flow")
    capex = cumulative_year("capital_expenditures")[:-1]
    ttm = build_ttm([*ocf, *capex], date(2025, 2, 16))
    assert ttm.operating_cash_flow == Decimal("600")
    assert ttm.capital_expenditures is None
    assert ttm.free_cash_flow is None


def test_fcf_growth_and_negative_comparisons() -> None:
    assert free_cash_flow(Decimal("120"), Decimal("40")) == Decimal("80")
    assert free_cash_flow(Decimal("120"), None) is None
    assert growth_rate(Decimal("120"), Decimal("100")) == pytest.approx(0.2)
    assert growth_rate(Decimal("10"), Decimal("-1")) is None


def test_roic_and_invalid_invested_capital() -> None:
    assert roic(Decimal("100"), 0.2, Decimal("400"), Decimal("600")) == pytest.approx(0.16)
    assert roic(Decimal("100"), 0.2, Decimal("-10"), Decimal("10")) is None


def test_valuation_metrics_reject_negative_earnings_and_ebitda() -> None:
    assert price_to_earnings(Decimal("20"), Decimal("2")) == 10
    assert price_to_earnings(Decimal("20"), Decimal("-2")) is None
    assert debt_to_ebitda(Decimal("100"), Decimal("-1")) is None
    assert ev_to_ebitda(Decimal("1000"), Decimal("0")) is None
    assert ev_to_ebit(Decimal("1000"), Decimal("100")) == 10
    assert enterprise_value(Decimal("1000"), Decimal("200"), Decimal("50")) == Decimal("1150")
    assert fcf_yield(Decimal("100"), Decimal("1000")) == pytest.approx(0.1)


def test_balance_falls_back_to_current_plus_noncurrent_debt_point_in_time() -> None:
    facts = [
        fact("debt_current", "20", None, date(2024, 3, 31), date(2024, 5, 5)),
        fact("debt_noncurrent", "80", None, date(2024, 3, 31), date(2024, 5, 5)),
        fact("debt_noncurrent", "70", None, date(2024, 6, 30), date(2024, 8, 5)),
    ]
    may = balance_sheet_as_of(facts, date(2024, 5, 6))
    august = balance_sheet_as_of(facts, date(2024, 8, 6))
    assert may.total_debt == Decimal("100")
    assert august.total_debt == Decimal("100")


def test_newer_debt_components_override_stale_direct_total() -> None:
    facts = [
        fact("total_debt", "0", None, date(2022, 5, 31), date(2022, 6, 21)),
        fact("debt_current", "20", None, date(2024, 6, 30), date(2024, 8, 5)),
        fact("debt_noncurrent", "80", None, date(2024, 6, 30), date(2024, 8, 5)),
    ]
    snapshot = balance_sheet_as_of(facts, date(2024, 8, 6))
    assert snapshot.total_debt == Decimal("100")


def test_complete_fundamental_metric_bundle() -> None:
    current = TTMFundamentals(
        revenue=Decimal("1200"),
        operating_income=Decimal("240"),
        net_income=Decimal("150"),
        eps_diluted=Decimal("3"),
        operating_cash_flow=Decimal("220"),
        capital_expenditures=Decimal("40"),
        free_cash_flow=Decimal("180"),
        depreciation_amortization=Decimal("60"),
        ebitda=Decimal("300"),
        tax_expense=Decimal("50"),
    )
    prior = TTMFundamentals(
        revenue=Decimal("1000"),
        eps_diluted=Decimal("2"),
        operating_cash_flow=Decimal("200"),
    )
    balance = BalanceSheetSnapshot(
        cash=Decimal("100"),
        total_debt=Decimal("300"),
        total_equity=Decimal("800"),
        shares_outstanding=Decimal("100"),
    )
    prior_balance = BalanceSheetSnapshot(
        cash=Decimal("80"), total_debt=Decimal("320"), total_equity=Decimal("700")
    )
    metrics = calculate_fundamental_metrics(current, prior, balance, prior_balance, Decimal("20"))
    assert metrics.revenue_growth == pytest.approx(0.2)
    assert metrics.operating_margin == pytest.approx(0.2)
    assert metrics.market_cap == Decimal("2000")
    assert metrics.pe == pytest.approx(20 / 3)
    assert metrics.enterprise_value == Decimal("2200")
    assert metrics.ev_to_ebitda == pytest.approx(2200 / 300)
    assert metrics.fcf_yield == pytest.approx(0.09)


def test_point_in_time_analysis_does_not_invent_missing_history() -> None:
    filed = date(2024, 5, 5)
    end = date(2024, 3, 31)
    facts = [
        fact("shares_outstanding", "100", None, end, filed, unit="shares"),
        fact("cash", "50", None, end, filed),
        fact("total_debt", "200", None, end, filed),
        fact("total_equity", "500", None, end, filed),
    ]
    before = analyze_fundamentals(facts, date(2024, 5, 4), Decimal("20"))
    after = analyze_fundamentals(facts, date(2024, 5, 6), Decimal("20"))
    assert before.market_cap is None
    assert after.market_cap == Decimal("2000")
    assert after.roic is None


def test_ebitda_derived_from_period_aligned_depreciation_and_amortization() -> None:
    facts = []
    for metric, value in (
        ("operating_income", "100"),
        ("depreciation", "20"),
        ("amortization", "5"),
    ):
        for quarter, (start, end) in enumerate(
            (
                (date(2024, 1, 1), date(2024, 3, 31)),
                (date(2024, 4, 1), date(2024, 6, 30)),
                (date(2024, 7, 1), date(2024, 9, 30)),
                (date(2024, 10, 1), date(2024, 12, 31)),
            ),
            start=1,
        ):
            facts.append(
                fact(
                    metric,
                    value,
                    start,
                    end,
                    end + timedelta(days=40),
                    fiscal_period=f"Q{quarter}",
                )
            )
    ttm = build_ttm(facts, date(2025, 2, 15))
    assert ttm.depreciation_amortization == Decimal("100")
    assert ttm.ebit == Decimal("400")
    assert ttm.ebitda == Decimal("500")
    assert ttm.ebitda_formula.startswith("operating income")
    debug = debug_fundamentals("TEST", facts, date(2025, 2, 15))
    ebitda_debug = next(item for item in debug.items if item.name == "EBITDA")
    assert ebitda_debug.value == Decimal("500")
    assert "operating_income" in ebitda_debug.xbrl_concepts
    assert "D&A" in ebitda_debug.formula


def test_missing_da_leaves_ebitda_unavailable_without_blocking_ebit() -> None:
    ttm = TTMFundamentals(operating_income=Decimal("100"), ebit=Decimal("100"))
    assert ttm.ebitda is None
    assert ev_to_ebitda(Decimal("1000"), ttm.ebitda) is None
    assert ev_to_ebit(Decimal("1000"), ttm.ebit) == 10
