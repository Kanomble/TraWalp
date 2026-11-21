"""Small in-memory probes of the opt-in Canonical research reader."""

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import permutations

import pytest

from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.fundamentals.quality import accounting_state_as_of
from trading_system.fundamentals.semantic import (
    ISSUER_SCOPE,
    PARENT_INCOME,
    Amendment,
    DebtCoverage,
    Evidence,
    ReportingContext,
    research_accounting_as_of,
)
from trading_system.models.fundamentals import FundamentalFact

CIK = "0001467373"
DATE = date(2024, 6, 28)
ACCN = "0001467373-24-000196"


def fact(metric, tag, value, **overrides):
    values = {
        "cik": CIK,
        "symbol": "ACN",
        "metric": metric,
        "tag": tag,
        "unit": "USD",
        "value": Decimal(value),
        "period_end": date(2024, 5, 31),
        "filed": date(2024, 6, 20),
        "form": "10-Q",
        "accession_number": ACCN,
    } | overrides
    return FundamentalFact(**values)


def research(facts, as_of=DATE, **options):
    return research_accounting_as_of(facts, as_of, cik=CIK, **options)


def aggregate(value, **overrides):
    return fact("total_debt", "DebtLongtermAndShorttermCombinedAmount", value, **overrides)


def components():
    return (
        fact("debt_current", "LongTermDebtCurrent", 10_762),
        fact("debt_noncurrent", "LongTermDebtNoncurrent", 91_831),
    )


def test_acn_aggregate_scope_is_order_independent_and_legacy_unchanged():
    narrow = fact("total_debt", "LongTermDebtAndCapitalLeaseObligations", 68_878_000)
    combined = aggregate(1_678_903_000)
    current = fact("debt_current", "DebtCurrent", 1_610_025_000)
    cases = [research(rows) for rows in permutations((narrow, combined, current))]
    assert all(case == cases[0] for case in cases)
    assert cases[0].state.balance.total_debt == Decimal(1_678_903_000)
    assert cases[0].current_balance["total_debt"].sources == (combined,)
    assert (
        accounting_state_as_of([narrow, combined, current], DATE).balance.total_debt == 68_878_000
    )


def test_debt_requires_complete_pair_and_explicit_rounding_evidence():
    current, noncurrent = components()
    rounded = aggregate(102_600)
    coverage = DebtCoverage(current, noncurrent, "reviewed debt note")
    proof = Evidence(
        contexts={
            current: ReportingContext(CIK, ISSUER_SCOPE, "current context", Decimal(1)),
            noncurrent: ReportingContext(CIK, ISSUER_SCOPE, "noncurrent context", Decimal(1)),
            rounded: ReportingContext(CIK, ISSUER_SCOPE, "rounded disclosure", Decimal(100)),
        },
        debt_coverage=(coverage,),
    )
    assert research([current]).state.balance.total_debt is None
    assert research([current, noncurrent]).state.balance.total_debt is None
    assert research([current, noncurrent], evidence=proof).state.balance.total_debt == 102_593
    assert (
        research([rounded, current, noncurrent], evidence=proof)
        .current_balance["total_debt"]
        .reason
        == "RECONCILED_ROUNDED_AGGREGATE"
    )
    equal = aggregate(102_593)
    assert (
        research([equal, current, noncurrent], evidence=proof).state.balance.total_debt == 102_593
    )
    inconsistent = aggregate(102_550)
    assert (
        research([inconsistent, current, noncurrent], evidence=proof).state.balance.total_debt
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "EUR"},
        {"period_end": date(2024, 2, 29)},
        {"accession_number": "0001467373-24-000107"},
        {"cik": "0000320193"},
        {"tag": "LongTermDebt"},
        {"filed": date(2024, 6, 19)},
    ],
)
def test_debt_components_cannot_cross_boundaries(change):
    current, noncurrent = components()
    noncurrent = noncurrent.model_copy(update=change)
    proof = Evidence(
        contexts={
            current: ReportingContext(CIK, ISSUER_SCOPE, "current context"),
            noncurrent: ReportingContext(noncurrent.cik, ISSUER_SCOPE, "other context"),
        },
        debt_coverage=(DebtCoverage(current, noncurrent, "candidate review"),),
    )
    assert research([noncurrent, current], evidence=proof).state.balance.total_debt is None


def test_unresolved_debt_conflict_does_not_fall_back_to_older_period():
    old = aggregate(100, period_end=date(2024, 2, 29), filed=date(2024, 3, 21))
    latest = aggregate(123)
    conflicting = aggregate(124, accession_number="0001467373-24-000197")
    for order in permutations([old, latest, conflicting]):
        result = research(order)
        assert result.state.balance.total_debt is None
        assert result.current_balance["total_debt"].reason == "UNRESOLVED_DEBT_TOTALS"


def test_acceptance_cutoff_and_reviewed_amendment():
    original = aggregate(50)
    amendment = aggregate(60, accession_number="0001467373-24-000197", form="10-Q/A")
    unknown = aggregate(70, accession_number="0001467373-24-000198")
    wrong_scope = aggregate(80, accession_number="0001467373-24-000199")
    cutoff = date(2024, 6, 20)
    facts = [original, amendment, unknown, wrong_scope]
    acceptance = {
        original.accession_number: datetime(2024, 6, 20, 14, tzinfo=UTC),
        amendment.accession_number: datetime(2024, 6, 20, 21, tzinfo=UTC),
    }
    proof = Evidence(
        contexts={wrong_scope: ReportingContext(CIK, "segment", "original iXBRL context")},
        acceptance=acceptance,
        amendments={
            amendment.accession_number: Amendment(original.accession_number, "10-Q/A link")
        },
    )
    early = research(
        facts, cutoff, decision_at=datetime(2024, 6, 20, 20, tzinfo=UTC), evidence=proof
    )
    assert early.state.balance.total_debt == 50
    assert {reason for reason, _ in early.excluded} == {
        "AFTER_ACCEPTANCE_CUTOFF",
        "ACCEPTANCE_UNAVAILABLE",
        "INCOMPATIBLE_REPORTING_CONTEXT",
    }
    later = research(
        facts, cutoff, decision_at=datetime(2024, 6, 20, 22, tzinfo=UTC), evidence=proof
    )
    assert later.state.balance.total_debt == 60
    assert later.current_balance["total_debt"].references == ("10-Q/A link",)
    assert research(facts, date(2024, 6, 19), evidence=proof).state.balance.total_debt is None


def quarters(metric, tag, value, year):
    starts = ((1, 1), (4, 1), (7, 1), (10, 1))
    ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    return [
        fact(
            metric,
            tag,
            value,
            period_start=date(year, *start),
            period_end=date(year, *end),
            filed=date(year + (end[0] == 12), end[0] % 12 + 1, 15),
            accession_number=f"{CIK}-{year % 100:02d}-{index:06d}",
        )
        for index, (start, end) in enumerate(zip(starts, ends, strict=True), 1)
    ]


def test_parent_and_consolidated_income_have_separate_current_and_prior_windows():
    rows = [
        fact
        for year in (2023, 2024)
        for metric, tag, base in (
            ("net_income", "NetIncomeLoss", 10),
            ("net_income", "ProfitLoss", 20),
            ("tax_expense", "IncomeTaxExpenseBenefit", 5),
        )
        for fact in quarters(metric, tag, base + year - 2023, year)
    ]
    result = research(rows, date(2025, 2, 1))
    assert result == research(reversed(rows), date(2025, 2, 1))
    assert result.current_flows[PARENT_INCOME].value == 44
    assert result.prior_flows[PARENT_INCOME].value == 40
    assert result.state.current.net_income == 84
    assert result.state.prior.net_income == 80
    assert result.metrics().effective_tax_rate == pytest.approx(24 / 108)
    only_parent = research([row for row in rows if row.tag != "ProfitLoss"], date(2025, 2, 1))
    assert only_parent.state.current.net_income is None
    assert only_parent.metrics().effective_tax_rate is None


def test_total_ocf_does_not_accept_continuing_operations_or_later_filing():
    total = quarters("operating_cash_flow", "NetCashProvidedByUsedInOperatingActivities", 10, 2024)
    continuing = total[1].model_copy(
        update={"tag": "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"}
    )
    late_total = total[1].model_copy(update={"filed": date(2025, 2, 2)})
    rows = [total[0], continuing, total[2], total[3], late_total]
    assert research(rows, date(2025, 2, 1)).state.current.operating_cash_flow is None
    assert research(rows, date(2025, 2, 2)).state.current.operating_cash_flow == 40


def test_new_interest_alias_is_usable_and_equal_observations_count_once():
    source = quarters("interest_expense", "InterestExpenseNonoperating", 3, 2024)
    raw = [
        {
            "start": f.period_start.isoformat(),
            "end": f.period_end.isoformat(),
            "filed": f.filed.isoformat(),
            "val": str(f.value),
            "accn": f.accession_number,
            "form": f.form,
        }
        for f in source
    ]
    payload = {
        "cik": CIK,
        "facts": {
            "us-gaap": {
                "InterestExpenseNonoperating": {"units": {"USD": raw + [raw[0]]}},
                "InterestExpenseNonOperating": {"units": {"USD": [raw[0]]}},
            }
        },
    }
    facts = parse_company_facts(payload, "ACN", preserve_observations=True)
    end = date(2025, 2, 1)
    result = research(facts, end)
    assert research(reversed(facts), end) == result
    assert result.state.current.interest_expense == 12
    assert {row.tag for row in result.current_flows["interest_expense"].quarters[0][1].sources} == {
        "InterestExpenseNonoperating",
        "InterestExpenseNonOperating",
    }
    conflicting = facts[0].model_copy(update={"value": Decimal("4")})
    assert research([*facts, conflicting], end).state.current.interest_expense is None


def test_contested_quarter_cannot_be_replaced_with_stale_ttm():
    rows = quarters("net_income", "ProfitLoss", 15, 2024)
    cumulative = rows[1].model_copy(
        update={"period_start": rows[0].period_start, "value": Decimal(35)}
    )
    result = research([*rows, cumulative], date(2025, 2, 1))
    assert result.state.current.net_income is None
    assert result.current_flows["net_income"].quarters[1][1].reason == "UNRESOLVED_QUARTER_CONTEXT"
