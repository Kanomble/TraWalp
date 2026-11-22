"""Read one approved issuer's Canonical accounting evidence at a decision timestamp."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trading_system.fundamentals.accounting_fallback import (
    DEBT_BRIDGE,
    OPERATING_BRIDGE,
    PPE_CASH,
    OriginalAmount,
    flow_fallback,
)
from trading_system.fundamentals.semantic import (
    Evidence,
    ResearchAccounting,
    Selection,
    research_accounting_as_of,
    ttm_from_windows,
)
from trading_system.models.fundamentals import FundamentalFact

CONTRACT = "F01_CANONICAL_RESEARCH_ACCOUNTING_READER_V1"
RULES = {DEBT_BRIDGE, PPE_CASH, OPERATING_BRIDGE}
FLOW_RULES = {"capital_expenditures": PPE_CASH, "operating_income": OPERATING_BRIDGE}
UNRESOLVED = {
    "UNRESOLVED_EQUIVALENT_OBSERVATIONS",
    "UNRESOLVED_QUARTER_CONTEXT",
    "UNRESOLVED_AMENDMENT_LINEAGE",
}
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ResearchRead:
    base: ResearchAccounting
    accounting: ResearchAccounting


class CanonicalResearchReader:
    """Issuer facts are the intersection of Canonical rows and exact original witnesses."""

    def __init__(
        self,
        facts: Iterable[FundamentalFact],
        *,
        cik: str,
        evidence: Evidence,
        originals: Mapping[FundamentalFact, dict],
        flows: Mapping[str, list[OriginalAmount]],
        debts: Mapping[FundamentalFact, OriginalAmount],
        amendments: Mapping[str, tuple[tuple[date, datetime], ...]],
        reviewed_rules: frozenset[str],
    ):
        if not reviewed_rules <= RULES:
            raise ValueError("UNKNOWN_RESEARCH_FALLBACK_RULE")
        self.cik = cik
        self.facts = tuple(fact for fact in facts if fact.cik == cik and fact in originals)
        self.evidence = replace(evidence, strict_amendments=True)
        self.flows = flows
        self.debts = debts
        self.amendments = amendments
        self.rules = reviewed_rules

    def read(self, decision_at: datetime) -> ResearchRead:
        if decision_at.utcoffset() is None:
            raise ValueError("TIMEZONE_AWARE_DECISION_REQUIRED")
        as_of = decision_at.astimezone(NY).date()
        base = research_accounting_as_of(
            self.facts,
            as_of,
            cik=self.cik,
            evidence=self.evidence,
            decision_at=decision_at,
        )
        windows = dict(base.current_flows)
        current, prior = base.current_balance["total_debt"], base.prior_balance.get("total_debt")
        if DEBT_BRIDGE in self.rules:
            current = self._debt(base.current_balance, decision_at) or current
            prior = self._debt(base.prior_balance, decision_at) or prior
        asset = base.current_balance["total_assets"]
        dates = {item.period_end for item in asset.sources}
        required_end = next(iter(dates)) if len(dates) == 1 else None
        for metric, rule in FLOW_RULES.items():
            if rule not in self.rules:
                continue
            existing = windows[metric]
            blocked = {
                end
                for end, q in existing.quarters
                if q.reason in UNRESOLVED or any(item.form.endswith("/A") for item in q.candidates)
            }
            blocked.update(
                end for end, accepted in self.amendments.get(metric, ()) if accepted <= decision_at
            )
            fallback = flow_fallback(
                self.flows.get(metric, []),
                cik=self.cik,
                as_of=as_of,
                decision_at=decision_at,
                required_end=required_end,
                qualified_value=existing.value,
                qualified_quarters={
                    end: q.value for end, q in existing.quarters if q.value is not None
                },
                blocked_quarters=frozenset(blocked),
            )
            if fallback:
                windows[metric] = fallback
        if (
            windows == base.current_flows
            and current == base.current_balance["total_debt"]
            and (prior == base.prior_balance.get("total_debt"))
        ):
            return ResearchRead(base, base)
        ttm = ttm_from_windows(windows, as_of)
        old = base.state.current
        if old.ebitda is not None:
            ttm = ttm.model_copy(
                update={
                    "ebitda": old.ebitda,
                    "ebitda_formula": old.ebitda_formula,
                    "metric_period_ends": ttm.metric_period_ends
                    | {"ebitda": old.metric_period_ends["ebitda"]},
                    "metric_available_dates": ttm.metric_available_dates
                    | {"ebitda": old.metric_available_dates["ebitda"]},
                }
            )
        state = replace(
            base.state,
            current=ttm,
            balance=base.state.balance.model_copy(update={"total_debt": current.value}),
            prior_balance=base.state.prior_balance.model_copy(
                update={"total_debt": prior.value if prior is not None else None}
            ),
        )
        return ResearchRead(
            base,
            replace(
                base,
                state=state,
                current_flows=windows,
                current_balance=base.current_balance | {"total_debt": current},
                prior_balance=base.prior_balance
                | ({"total_debt": prior} if prior is not None else {}),
            ),
        )

    def _debt(self, balance, cutoff):
        if not balance or balance["total_debt"].reason not in {
            "DEBT_COMPONENTS_OR_SCOPE_UNVERIFIED",
            "NO_DEBT_OBSERVATIONS",
        }:
            return None
        assets = balance["total_assets"].sources
        if len(assets) != 1 or (proof := self.debts.get(assets[0])) is None:
            return None
        period = proof.anchor.period_end
        candidates = {item.period_end for item in balance["total_debt"].candidates}
        if (
            proof.acceptance > cutoff
            or (candidates and candidates != {period})
            or any(
                period == end and accepted <= cutoff
                for end, accepted in self.amendments.get("total_debt", ())
            )
        ):
            return None
        return Selection(
            "ORIGINAL_FALLBACK", proof.value, (proof.anchor,), references=(proof.reference,)
        )


def values(result: ResearchAccounting, *, require_aligned_roic=False):
    ttm, balance = result.state.current, result.state.balance
    derived = result.metrics()
    roic = derived.roic
    if require_aligned_roic and roic is not None:
        for scope in (result.current_balance, result.prior_balance):
            asset_periods = {fact.period_end for fact in scope["total_assets"].sources}
            if any(
                {fact.period_end for fact in scope[name].sources} != asset_periods
                for name in ("total_debt", "total_equity", "cash")
            ):
                roic = None
    return {
        "total_debt": balance.total_debt,
        "operating_income": ttm.operating_income,
        "capital_expenditures": ttm.capital_expenditures,
        "operating_cash_flow": ttm.operating_cash_flow,
        "ebitda": ttm.ebitda,
        "fcf": ttm.free_cash_flow,
        "debt_to_ebitda": derived.debt_to_ebitda,
        "roic": roic,
    }
