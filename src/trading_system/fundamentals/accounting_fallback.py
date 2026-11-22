"""Opt-in Research fallbacks for amounts supported by original filing evidence."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trading_system.fundamentals.semantic import Selection, SeriesWindow, _quarters, _window
from trading_system.models.fundamentals import FundamentalFact

POLICY = "F01_RESEARCH_ACCOUNTING_FALLBACK_V1"
PPE_CASH = "PPE_CASH_ON_INVESTING_STATEMENT_V1"
OPERATING_BRIDGE = "CLOSED_PRETAX_OPERATING_BRIDGE_V1"
DEBT_BRIDGE = "CLOSED_BORROWINGS_AND_FINANCE_LEASES_V1"


@dataclass(frozen=True)
class OriginalAmount:
    anchor: FundamentalFact
    value: Decimal
    acceptance: datetime
    reference: str


def cash_ppe(tag: str, label: str, value: Decimal) -> Decimal | None:
    cash_tags = {
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    }
    # Require explicit cash PPE wording even if the taxonomy concept has broader scope.
    ppe_labels = {
        "purchases of property and equipment",
        "purchases of property, plant and equipment",
        "purchases of property, plant, and equipment",
        "purchases related to property and equipment",
        "additions to property and equipment",
        "additions to property, plant and equipment",
    }
    if (
        tag in cash_tags
        and label.strip().casefold() in ppe_labels
        and value.is_finite()
        and value >= 0
    ):
        return value
    return None


def operating_bridge(pretax, revenue, expense_subtotal, nonoperating, operating_expenses):
    """Reconciling the full statement isolates the reported net nonoperating result."""
    operating = tuple(operating_expenses)
    if (
        not operating
        or expense_subtotal != sum(operating, Decimal(0)) - nonoperating
        or pretax != revenue - expense_subtotal
    ):
        return None
    return pretax - nonoperating


def closed_financing(current, noncurrent, leases, *, other_borrowings_absent, leases_disjoint):
    """Eligible components must have been bound to the same consolidated carrying scope."""
    values = current, noncurrent, leases
    if (
        other_borrowings_absent
        and leases_disjoint
        and all(value is not None and value.is_finite() and value >= 0 for value in values)
    ):
        return sum(values, Decimal(0))
    return None


def flow_fallback(
    rows: list[OriginalAmount],
    *,
    cik: str,
    as_of: date,
    decision_at: datetime,
    required_end: date | None,
    qualified_value: Decimal | None,
    qualified_quarters: dict[date, Decimal],
    blocked_quarters: frozenset[date] = frozenset(),
) -> SeriesWindow | None:
    """Use a homogeneous proof family and the existing quarter/TTM engine."""
    if (
        qualified_value is not None
        or required_end is None
        or decision_at.utcoffset() is None
        or len({item.anchor.metric for item in rows}) > 1
    ):
        return None
    slots = defaultdict(list)
    for item in rows:
        fact = item.anchor
        if (
            fact.cik == cik
            and fact.unit == "USD"
            and fact.period_start is not None
            and fact.period_start <= fact.period_end <= required_end <= as_of
            and fact.filed <= as_of
            and item.acceptance.utcoffset() is not None
            and item.acceptance <= decision_at
            and item.value.is_finite()
        ):
            slots[fact.period_start, fact.period_end].append(item)
    selected = {}
    for period, group in slots.items():
        latest = max(item.anchor.filed for item in group)
        contenders = [item for item in group if item.anchor.filed == latest]
        values = {item.value for item in contenders}
        selected[period] = Selection(
            "ORIGINAL_FALLBACK" if len(values) == 1 else "UNRESOLVED_ORIGINAL_CONFLICT",
            next(iter(values)) if len(values) == 1 else None,
            tuple(
                sorted({item.anchor for item in contenders}, key=FundamentalFact.model_dump_json)
            ),
            references=tuple(sorted({item.reference for item in contenders})),
        )
    window = _window(((), _quarters(selected)), as_of)
    if window.period_end != required_end or any(
        end in blocked_quarters
        or (end in qualified_quarters and quarter.value != qualified_quarters[end])
        for end, quarter in window.quarters
    ):
        return None
    return window
