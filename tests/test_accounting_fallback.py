"""Small offline tests for original-filing Accounting fallback candidates."""

from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.fundamentals.accounting_fallback import (
    OriginalAmount,
    cash_ppe,
    closed_financing,
    flow_fallback,
    operating_bridge,
)
from trading_system.models.fundamentals import FundamentalFact


def test_capex_uses_cash_ppe_and_rejects_broader_or_financing_descriptions():
    assert (
        cash_ppe(
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "Purchases of property and equipment",
            Decimal("20"),
        )
        == 20
    )
    assert (
        cash_ppe(
            "PaymentsToAcquireProductiveAssets",
            "Purchases related to property and equipment and intangible assets",
            Decimal("22"),
        )
        is None
    )
    assert (
        cash_ppe(
            "CapitalExpendituresIncurredButNotYetPaid",
            "Purchases of property and equipment",
            Decimal("22"),
        )
        is None
    )


def test_operating_bridge_requires_both_exact_reconciliations():
    assert operating_bridge(46, 100, 54, -4, [20, 30]) == 50
    assert operating_bridge(46, 100, 54, -4, [20, 29]) is None
    assert operating_bridge(47, 100, 54, -4, [20, 30]) is None


def test_debt_requires_separate_finance_leases_and_closed_debt_perimeter():
    current, later, leases = map(Decimal, ("10", "20", "5"))
    assert (
        closed_financing(current, later, leases, other_borrowings_absent=True, leases_disjoint=True)
        == 35
    )
    assert (
        closed_financing(
            current, later, leases, other_borrowings_absent=True, leases_disjoint=False
        )
        is None
    )
    assert (
        closed_financing(
            current, later, leases, other_borrowings_absent=False, leases_disjoint=True
        )
        is None
    )
    assert (
        closed_financing(current, later, None, other_borrowings_absent=True, leases_disjoint=True)
        is None
    )


def original_quarters():
    bounds = ("2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01")
    ends = ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")
    return [
        OriginalAmount(
            FundamentalFact(
                cik="0000000123",
                symbol="TEST",
                metric="operating_income",
                tag="Revenues",
                value=Decimal("500"),
                unit="USD",
                filed=date(2026, 1, 1),
                period_start=date.fromisoformat(start),
                period_end=date.fromisoformat(end),
                accession_number="0000000123-26-000001",
                form="10-K",
            ),
            Decimal("100"),
            datetime(2026, 1, 1, 16, tzinfo=UTC),
            f"proof-{index}",
        )
        for index, (start, end) in enumerate(zip(bounds, ends, strict=True))
    ]


def resolve(rows, *, known=None, quarter=None, cutoff=None):
    return flow_fallback(
        rows,
        cik="0000000123",
        as_of=date(2026, 1, 2),
        decision_at=cutoff or datetime(2026, 1, 2, 21, tzinfo=UTC),
        required_end=date(2025, 12, 31),
        qualified_value=known,
        qualified_quarters=quarter or {},
    )


def test_ttm_resolver_preserves_values_and_respects_acceptance_and_quarters():
    rows = original_quarters()
    result = resolve(rows)
    assert result.value == 400
    assert result == resolve(list(reversed(rows)))
    assert result.quarters[-1][1].references == ("proof-3",)
    assert resolve(rows, known=Decimal("0")) is None
    assert resolve(rows, quarter={date(2025, 6, 30): Decimal("99")}) is None
    assert resolve(rows, cutoff=datetime(2026, 1, 1, 15, tzinfo=UTC)) is None


def test_conflicting_quarter_facts_and_single_missing_quarter_stay_open():
    rows = original_quarters()
    conflicting = OriginalAmount(rows[-1].anchor, Decimal("105"), rows[-1].acceptance, "other")
    assert resolve(rows + [conflicting]) is None
    assert resolve(rows[:-1]) is None
