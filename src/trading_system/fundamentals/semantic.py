"""Explicit Canonical research accounting from admitted, issuer-level SEC observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from trading_system.fundamentals.metrics import (
    QuarterlyValue,
    build_ttm,
    shares_outstanding_as_of,
    ttm_from_quarters,
)
from trading_system.fundamentals.quality import FundamentalAccountingState, attach_market_price
from trading_system.models.fundamentals import BalanceSheetSnapshot, FundamentalFact

POLICY = "F01_RESEARCH_SEMANTIC_FACTS_V1"
ISSUER_SCOPE = "ISSUER_AGGREGATE"
TOTAL_DEBT = "DebtLongtermAndShorttermCombinedAmount"
DEBT_COMPONENTS = (
    ("LongTermDebtCurrent", "LongTermDebtNoncurrent"),
    (
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    ),
    ("DebtCurrent", "LongTermDebtAndCapitalLeaseObligations"),
)
DEBT_METRICS = frozenset({"debt_current", "debt_noncurrent", "total_debt"})
PARENT_INCOME = "parent_net_income"

# Each group is one series. Different groups never supply quarters to one another.
FLOW_SERIES = {
    "revenue": (
        ("RevenueFromContractWithCustomerExcludingAssessedTax",),
        ("Revenues",),
        ("SalesRevenueNet",),
    ),
    "operating_income": (("OperatingIncomeLoss",),),
    "net_income": (("ProfitLoss",),),  # Consolidated numerator for tax and EBITDA.
    PARENT_INCOME: (("NetIncomeLoss",),),
    "eps_diluted": (("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),),
    "operating_cash_flow": (("NetCashProvidedByUsedInOperatingActivities",),),
    "capital_expenditures": (
        ("PaymentsToAcquirePropertyPlantAndEquipment",),
        ("PaymentsForAdditionsToPropertyPlantAndEquipment",),
    ),
    "depreciation_amortization": (("DepreciationDepletionAndAmortization",),),
    "depreciation": (("Depreciation",),),
    "amortization": (("AmortizationOfIntangibleAssets",),),
    "tax_expense": (("IncomeTaxExpenseBenefit",),),
    "interest_expense": (
        ("InterestExpenseNonoperating", "InterestExpenseNonOperating"),
        ("InterestExpense",),
    ),
}
BALANCE_TAGS = {
    "cash": "CashAndCashEquivalentsAtCarryingValue",
    "current_assets": "AssetsCurrent",
    "current_liabilities": "LiabilitiesCurrent",
    "total_assets": "Assets",
    "total_equity": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
}


@dataclass(frozen=True)
class ReportingContext:
    """Evidence for an exact fact's entity/dimensions, plus optional XBRL rounding."""

    cik: str
    scope: str
    reference: str
    rounding_increment: Decimal | None = None

    def __post_init__(self):
        increment = self.rounding_increment
        if (
            not self.reference.strip()
            or not self.scope.strip()
            or (increment is not None and (not increment.is_finite() or increment <= 0))
        ):
            raise ValueError("Invalid reporting-context evidence")


@dataclass(frozen=True)
class Amendment:
    supersedes: str
    reference: str


@dataclass(frozen=True)
class DebtCoverage:
    """Reviewed evidence that these exact components exhaust Total Debt without overlap."""

    current: FundamentalFact
    noncurrent: FundamentalFact
    reference: str


@dataclass(frozen=True)
class Evidence:
    contexts: Mapping[FundamentalFact, ReportingContext] = field(default_factory=dict)
    acceptance: Mapping[str, datetime] = field(default_factory=dict)
    amendments: Mapping[str, Amendment] = field(default_factory=dict)
    debt_coverage: tuple[DebtCoverage, ...] = ()


@dataclass(frozen=True)
class Selection:
    reason: str
    value: Decimal | None = None
    sources: tuple[FundamentalFact, ...] = ()
    candidates: tuple[FundamentalFact, ...] = ()
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeriesWindow:
    tags: tuple[str, ...]
    value: Decimal | None
    period_end: date | None
    filed: date | None
    quarters: tuple[tuple[date, Selection], ...]
    reason: str


@dataclass(frozen=True)
class ResearchAccounting:
    cik: str
    as_of: date
    decision_at: datetime | None
    state: FundamentalAccountingState
    current_flows: Mapping[str, SeriesWindow]
    prior_flows: Mapping[str, SeriesWindow]
    current_balance: Mapping[str, Selection]
    prior_balance: Mapping[str, Selection]
    excluded: tuple[tuple[str, FundamentalFact], ...]
    eligible_facts: tuple[FundamentalFact, ...]
    evidence: Evidence
    policy: str = POLICY

    def metrics(self, price: Decimal | None = None):
        """Use existing arithmetic with research-scope and period checks for tax/ROIC."""
        result = attach_market_price(self.state, price)
        ends = self.state.current.metric_period_ends
        tax_aligned = "net_income" in ends and ends["net_income"] == ends.get("tax_expense")
        operating_aligned = tax_aligned and ends["net_income"] == ends.get("operating_income")
        margin_aligned = "revenue" in ends and ends["revenue"] == ends.get("operating_income")
        return result.model_copy(
            update={
                "effective_tax_rate": result.effective_tax_rate if tax_aligned else None,
                "roic": result.roic if operating_aligned else None,
                "operating_margin": result.operating_margin if margin_aligned else None,
            }
        )


def _ordered(facts: Iterable[FundamentalFact]) -> tuple[FundamentalFact, ...]:
    # Ordering is for the audit trail only. None of these fields resolve a value conflict.
    copies = {fact.model_dump_json(): fact for fact in facts}
    return tuple(copies[key] for key in sorted(copies))


class _ResearchFacts:
    def __init__(self, facts, cik, as_of, decision_at, evidence):
        self.evidence = evidence
        if decision_at is not None and (
            decision_at.utcoffset() is None
            or decision_at.astimezone(ZoneInfo("America/New_York")).date() != as_of
        ):
            raise ValueError("decision_at must be timezone-aware and on the New York session date")
        self.facts = []
        self.excluded = []
        for fact in _ordered(facts):
            reason = self._unavailable(fact, cik, as_of, decision_at)
            if reason:
                self.excluded.append((reason, fact))
            else:
                self.facts.append(fact)

    def _unavailable(self, fact, cik, as_of, decision_at):
        if fact.cik != cik:
            return "CIK_MISMATCH"
        if not fact.accession_number:
            return "MISSING_ACCESSION"
        context = self.evidence.contexts.get(fact)
        if context and (context.cik != cik or context.scope != ISSUER_SCOPE):
            return "INCOMPATIBLE_REPORTING_CONTEXT"
        if not fact.value.is_finite():
            return "INVALID_FACT_VALUE"
        if fact.filed > as_of or fact.period_end > as_of:
            return "AFTER_DATE_CUTOFF"
        if decision_at is not None:
            accepted = self.evidence.acceptance.get(fact.accession_number)
            if accepted is None or accepted.utcoffset() is None:
                return "ACCEPTANCE_UNAVAILABLE"
            if accepted > decision_at:
                return "AFTER_ACCEPTANCE_CUTOFF"
        return None

    def resolve(self, facts, *, tags=None):
        candidates = _ordered(facts)
        if not candidates:
            return Selection("NO_ELIGIBLE_OBSERVATION")
        latest = max(fact.filed for fact in candidates)
        contenders = tuple(fact for fact in candidates if fact.filed == latest)
        references = []
        if len({fact.value for fact in contenders}) > 1:
            removed = set()
            for amendment in contenders:
                link = self.evidence.amendments.get(amendment.accession_number)
                if not link or not link.reference.strip() or not amendment.form.endswith("/A"):
                    continue
                for original in contenders:
                    if self._supersedes(amendment, original, link):
                        removed.add(original)
                        references.append(link.reference)
            contenders = tuple(fact for fact in contenders if fact not in removed)
        if len({fact.value for fact in contenders}) != 1:
            return Selection("UNRESOLVED_EQUIVALENT_OBSERVATIONS", candidates=candidates)
        reason = "PROVEN_AMENDMENT" if references else "LATEST_COMPARABLE_OBSERVATION"
        if tags and not all(fact.tag in tags for fact in contenders):
            raise ValueError("Observation outside the selected accounting series")
        return Selection(
            reason, contenders[0].value, contenders, candidates, tuple(sorted(set(references)))
        )

    def _supersedes(self, amendment, original, link):
        if (
            not amendment.accession_number
            or amendment.accession_number == original.accession_number
            or original.accession_number != link.supersedes
            or amendment.form.removesuffix("/A") != original.form.removesuffix("/A")
        ):
            return False
        a = self.evidence.acceptance.get(amendment.accession_number)
        b = self.evidence.acceptance.get(original.accession_number)
        if not a or not b:
            return True
        return a.utcoffset() is not None and b.utcoffset() is not None and a > b

    def series(self, metric):
        tags = FLOW_SERIES[metric]
        normalized = "net_income" if metric == PARENT_INCOME else metric
        unit = "USD/shares" if metric == "eps_diluted" else "USD"
        flows = [
            fact
            for fact in self.facts
            if fact.metric == normalized
            and fact.taxonomy == "us-gaap"
            and fact.unit == unit
            and fact.period_start is not None
            and 60 <= (fact.period_end - fact.period_start).days <= 400
        ]
        groups = [(group, [fact for fact in flows if fact.tag in group]) for group in tags]
        # At the same economic end the explicit purpose-specific group order is used.
        group, observations = max(
            groups, key=lambda pair: max((fact.period_end for fact in pair[1]), default=date.min)
        )
        periods = defaultdict(list)
        for fact in observations:
            periods[fact.period_start, fact.period_end].append(fact)
        slots = {key: self.resolve(rows, tags=group) for key, rows in periods.items()}
        return group, _quarters(slots)

    def debt(self, cutoff):
        rows = [
            item
            for item in self.facts
            if item.metric in DEBT_METRICS
            and item.taxonomy == "us-gaap"
            and item.unit == "USD"
            and item.period_start is None
            and item.period_end <= cutoff
        ]
        if not rows:
            return Selection("NO_DEBT_OBSERVATIONS")
        period = max(item.period_end for item in rows)
        latest = _ordered(item for item in rows if item.period_end == period)
        combined = self.resolve(item for item in latest if item.tag == TOTAL_DEBT)
        families = frozenset(DEBT_COMPONENTS)
        pairs = [
            (coverage, coverage.current.value + coverage.noncurrent.value)
            for coverage in self.evidence.debt_coverage
            if self._valid_debt_coverage(coverage, latest, families)
        ]
        component_facts = _ordered(
            item for coverage, _ in pairs for item in (coverage.current, coverage.noncurrent)
        )
        references = tuple(
            sorted(
                {
                    reference
                    for coverage, _ in pairs
                    for reference in (
                        coverage.reference,
                        self.evidence.contexts[coverage.current].reference,
                        self.evidence.contexts[coverage.noncurrent].reference,
                    )
                }
            )
        )
        totals = {value for _, value in pairs}
        if len(totals) > 1 or combined.reason == "UNRESOLVED_EQUIVALENT_OBSERVATIONS":
            return Selection("UNRESOLVED_DEBT_TOTALS", candidates=latest, references=references)
        components = next(iter(totals), None)
        if combined.value is not None:
            if components is None or combined.value == components:
                return Selection(
                    "REPORTED_TOTAL_DEBT",
                    combined.value,
                    _ordered((*combined.sources, *component_facts)),
                    latest,
                    tuple(sorted(set(combined.references + references))),
                )
            increments = [
                self.evidence.contexts[source].rounding_increment
                if source in self.evidence.contexts
                else None
                for source in combined.sources
            ]
            component_increments = [
                self.evidence.contexts[item].rounding_increment for item in component_facts
            ]
            if all(step is not None for step in component_increments) and all(
                increment is not None
                and sum(step for step in component_increments if step is not None) < increment
                and abs(components - combined.value) <= increment / 2
                for increment in increments
            ):
                return Selection(
                    "RECONCILED_ROUNDED_AGGREGATE",
                    components,
                    component_facts,
                    latest,
                    tuple(
                        sorted(
                            {
                                *references,
                                *(
                                    self.evidence.contexts[item].reference
                                    for item in combined.sources
                                ),
                            }
                        )
                    ),
                )
            return Selection("UNRESOLVED_DEBT_TOTALS", candidates=latest, references=references)
        if components is not None:
            return Selection(
                "VERIFIED_COMPLETE_COMPONENTS", components, component_facts, latest, references
            )
        return Selection("DEBT_COMPONENTS_OR_SCOPE_UNVERIFIED", candidates=latest)

    def _valid_debt_coverage(self, coverage, latest, families):
        current, noncurrent = coverage.current, coverage.noncurrent
        if (
            not coverage.reference.strip()
            or current not in latest
            or noncurrent not in latest
            or not current.accession_number
            or current.accession_number != noncurrent.accession_number
            or (current.filed, current.form) != (noncurrent.filed, noncurrent.form)
            or (current.tag, noncurrent.tag) not in families
        ):
            return False
        for item in (current, noncurrent):
            context = self.evidence.contexts.get(item)
            if context is None or context.scope != ISSUER_SCOPE or context.cik != item.cik:
                return False
            same_concept = self.resolve(row for row in latest if row.tag == item.tag)
            if item not in same_concept.sources:
                return False
        return True

    def balance(self, as_of, cutoff):
        def instant(metric, tag):
            observations = [
                fact
                for fact in self.facts
                if fact.metric == metric
                and fact.tag == tag
                and fact.taxonomy == "us-gaap"
                and fact.unit == "USD"
                and fact.period_start is None
                and fact.period_end <= cutoff
            ]
            latest = max((fact.period_end for fact in observations), default=None)
            return self.resolve(fact for fact in observations if fact.period_end == latest)

        decisions = {metric: instant(metric, tag) for metric, tag in BALANCE_TAGS.items()}
        decisions["total_debt"] = self.debt(cutoff)
        share_scopes = {
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
        }
        shares = [
            f
            for f in self.facts
            if f.metric == "shares_outstanding"
            and f.unit == "shares"
            and f.value > 0
            and f.period_start is None
            and f.period_end <= cutoff
            and (f.taxonomy, f.tag) in share_scopes
        ]
        ranked = shares_outstanding_as_of(shares, as_of, end_on_or_before=cutoff)
        selected = Selection("NO_FRESH_REPORTED_SHARES", candidates=_ordered(shares))
        if ranked:
            # Production applies fixed DEI precedence after reporting and filing recency.
            selected = self.resolve(
                f
                for f in shares
                if (f.period_end, f.filed, f.taxonomy)
                == (ranked.period_end, ranked.filed, ranked.taxonomy)
            )
        decisions["shares_outstanding"] = selected
        ends = [fact.period_end for item in decisions.values() for fact in item.sources]
        state = BalanceSheetSnapshot(
            period_end=max(ends, default=None),
            **{metric: item.value for metric, item in decisions.items()},
        )
        return state, decisions


def _quarters(slots: Mapping[tuple[date, date], Selection]) -> dict[date, Selection]:
    direct, derived = defaultdict(list), defaultdict(list)
    cumulative = defaultdict(list)
    for (start, end), chosen in slots.items():
        cumulative[start].append((end, chosen))
        if (end - start).days <= 120:
            direct[end].append((start, chosen))
    for observations in cumulative.values():
        for (before, left), (end, right) in pairwise(sorted(observations)):
            if not 60 <= (end - before).days <= 120:
                continue
            value = (
                right.value - left.value
                if left.value is not None and right.value is not None
                else None
            )
            derived[end].append(
                (
                    before + timedelta(days=1),
                    Selection(
                        "CUMULATIVE_DIFFERENCE"
                        if value is not None
                        else "UNRESOLVED_CUMULATIVE_INPUT",
                        value,
                        _ordered((*right.sources, *left.sources)) if value is not None else (),
                        _ordered((*right.candidates, *left.candidates)),
                        tuple(sorted(set(right.references + left.references))),
                    ),
                )
            )
    quarters = {}
    for end in sorted({end for _, end in slots}):
        candidates = direct.get(end) or derived.get(end, [])
        comparison = direct.get(end, []) + derived.get(end, [])
        contrary = any(
            first_start == second_start
            and first.value is not None
            and second.value is not None
            and first.value != second.value
            and max(item.filed for item in first.sources)
            == max(item.filed for item in second.sources)
            for first_start, first in direct.get(end, [])
            for second_start, second in derived.get(end, [])
        )
        if not candidates:
            quarters[end] = Selection(
                "MISSING_CUMULATIVE_PREDECESSOR",
                candidates=_ordered(
                    fact
                    for (_, stop), chosen in slots.items()
                    if stop == end
                    for fact in chosen.candidates
                ),
            )
        elif contrary or len({(start, item.value) for start, item in candidates}) != 1:
            quarters[end] = Selection(
                "UNRESOLVED_QUARTER_CONTEXT",
                candidates=_ordered(fact for _, item in comparison for fact in item.candidates),
            )
        else:
            decisions = [item for _, item in candidates]
            quarters[end] = Selection(
                decisions[0].reason,
                decisions[0].value,
                _ordered(fact for item in decisions for fact in item.sources),
                _ordered(fact for _, item in comparison for fact in item.candidates),
                tuple(sorted({ref for item in decisions for ref in item.references})),
            )
    return quarters


def _window(series, cutoff):
    tags, quarters = series
    selected = tuple((end, item) for end, item in quarters.items() if end <= cutoff)[-4:]
    if any(item.value is None for _, item in selected):
        return SeriesWindow(tags, None, None, None, selected, "UNRESOLVED_QUARTER_INPUT")
    values = [
        QuarterlyValue(
            end,
            max(f.filed for f in item.sources),
            item.value,
            item.sources[0].unit,
            item.sources,
            item.reason,
        )
        for end, item in selected
    ]
    value, end, filed = ttm_from_quarters(values)
    return SeriesWindow(
        tags, value, end, filed, selected, "COHERENT_TTM" if value is not None else "INCOMPLETE_TTM"
    )


def research_accounting_as_of(
    facts: Iterable[FundamentalFact],
    as_of: date,
    *,
    cik: str,
    evidence: Evidence | None = None,
    decision_at: datetime | None = None,
) -> ResearchAccounting:
    """Opt-in policy for a bound CIK and a single issuer-aggregate USD fact stream.

    Unannotated Companyfacts are treated as provider issuer aggregates, with no
    inference about original filing dimensions or historical provider availability.
    """
    book = _ResearchFacts(facts, cik, as_of, decision_at, evidence or Evidence())
    series = {metric: book.series(metric) for metric in FLOW_SERIES}
    current_flows = {metric: _window(entries, as_of) for metric, entries in series.items()}
    prior_flows = {
        metric: _window(series[metric], chosen.period_end - timedelta(days=300))
        for metric, chosen in current_flows.items()
        if chosen.period_end is not None
    }

    def ttm(windows):
        def flow_ttm(_facts, metric, _as_of, *, end_on_or_before=None):
            chosen = windows.get(metric)
            return (chosen.value, chosen.period_end, chosen.filed) if chosen else (None, None, None)

        return build_ttm([], as_of, flow_ttm=flow_ttm)

    balance, current_balance = book.balance(as_of, as_of)
    prior_balance, prior_trace = (
        book.balance(as_of, balance.period_end - timedelta(days=300))
        if balance.period_end
        else (BalanceSheetSnapshot(), {})
    )
    state = FundamentalAccountingState(ttm(current_flows), ttm(prior_flows), balance, prior_balance)
    return ResearchAccounting(
        cik=cik,
        as_of=as_of,
        decision_at=decision_at,
        state=state,
        current_flows=current_flows,
        prior_flows=prior_flows,
        current_balance=current_balance,
        prior_balance=prior_trace,
        excluded=tuple(book.excluded),
        eligible_facts=tuple(book.facts),
        evidence=book.evidence,
    )
