"""Historical candidate funnel collected from the production backtest screen path."""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from trading_system.backtest.engine import (
    BACKTEST_WARNINGS,
    BacktestEngine,
    EntryFilterEvaluation,
    evaluate_variant_entry,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestResult,
    PositionManagementPreset,
    StrategyVariant,
)
from trading_system.models.candidate_audit import (
    CandidateAuditEvent,
    CandidateAuditMonthly,
    CandidateAuditResult,
    CandidateAuditSession,
    CandidateFailureSummary,
    CandidateNearMiss,
    DistributionSummary,
    FailureCategory,
    PointInTimeSample,
)
from trading_system.models.screening import ScreenRecord, ScreenReport

LOGGER = logging.getLogger(__name__)

FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("identity", "identity_valid"),
    ("static_filters", "after_static_filters"),
    ("market_history", "market_history_available"),
    ("valid_price", "valid_price"),
    ("liquidity", "liquidity_pass"),
    ("market_cap", "market_cap_pass"),
    ("pit_fundamentals", "fundamental_data_available"),
    ("positive_operating_cash_flow", "positive_operating_cash_flow_pass"),
    ("quality_score_available", "quality_score_available"),
    ("quality_threshold", "quality_threshold_pass"),
    ("valuation_score_available", "valuation_score_available"),
    ("valuation_threshold", "valuation_threshold_pass"),
    ("opportunity_score_available", "opportunity_score_available"),
    ("opportunity_threshold", "opportunity_threshold_pass"),
    ("timing_score_available", "timing_score_available"),
    ("timing_threshold", "timing_threshold_pass"),
    ("total_score_available", "total_score_available"),
    ("total_threshold", "total_score_pass"),
    ("price_above_sma20", "price_above_sma20_pass"),
    ("recovery_gate", "recovery_gate_pass"),
)
STAGE_INDEX = {stage: index for index, (stage, _) in enumerate(FUNNEL_STAGES)}

FUNDAMENTAL_METRICS = (
    "revenue_growth",
    "eps_growth",
    "operating_cash_flow_growth",
    "operating_cash_flow_positive",
    "operating_margin",
    "roic",
    "debt_to_ebitda",
    "relative_pe",
    "relative_ev_ebitda",
    "fcf_yield",
)
TECHNICAL_METRICS = (
    "sma20",
    "sma20_rising",
    "rsi14",
    "momentum5",
    "relative_volume",
    "atr14",
)
NEAR_MISS_FAILURES = {
    "opportunity_threshold",
    "timing_threshold",
    "total_threshold",
    "price_not_above_sma20",
    "recovery_signal_required",
}


@dataclass
class _SessionState:
    date: date
    values: dict[str, Any]
    portfolio_blockers: Counter[str] = field(default_factory=Counter)
    execution_blockers: Counter[str] = field(default_factory=Counter)


@dataclass
class _CandidateState:
    date: date
    symbol: str
    variant_score: float
    status: str = "eligible"
    order_created: bool = False
    entry_executed: bool = False
    execution_date: date | None = None
    blocker: str | None = None
    quality_score: float | None = None
    valuation_score: float | None = None
    opportunity_score: float | None = None
    timing_score: float | None = None
    price_above_sma20: bool | None = None
    rsi_recovery: bool | None = None
    momentum5_above_zero: bool | None = None
    relative_volume: float | None = None
    relative_volume_above_threshold: bool | None = None


class HistoricalCandidateAuditCollector:
    """Bounded observer that aggregates each PIT cross-section as it is screened."""

    def __init__(
        self,
        config: StrategyConfig,
        variant: StrategyVariant,
        *,
        near_miss_limit: int = 10,
    ) -> None:
        self.config = config
        self.variant = variant
        self.near_miss_limit = near_miss_limit
        self.started = time.perf_counter()
        self.symbols_evaluated = 0
        self._states: dict[date, _SessionState] = {}
        self._candidates: dict[tuple[date, str], _CandidateState] = {}
        self._near_misses: list[CandidateNearMiss] = []
        self._candidate_symbols: set[str] = set()
        self._entry_symbols: set[str] = set()
        self._near_miss_symbols: set[str] = set()
        self._pit_samples: list[PointInTimeSample] = []
        self._current_month: str | None = None
        self._current_score_values: dict[str, list[float]] = defaultdict(list)
        self._current_distance_values: dict[str, list[float]] = defaultdict(list)
        self._monthly_score_summaries: dict[str, dict[str, DistributionSummary]] = {}
        self._monthly_distance_summaries: dict[str, dict[str, DistributionSummary]] = {}
        self._pipeline_inconsistencies: Counter[str] = Counter()

    def observe_screen(
        self,
        report: ScreenReport,
        variant: StrategyVariant,
        config: StrategyConfig,
    ) -> None:
        if variant is not self.variant:
            raise ValueError("candidate audit observer received a different strategy variant")
        first_failures: Counter[str] = Counter()
        stage_rejected: Counter[str] = Counter()
        categories: Counter[FailureCategory] = Counter()
        fundamental_available: Counter[str] = Counter()
        fundamental_missing: Counter[str] = Counter()
        technical_available: Counter[str] = Counter()
        technical_missing: Counter[str] = Counter()
        relative_volume: Counter[str] = Counter()
        recovery: Counter[str] = Counter()
        requiring_fundamentals = 0
        with_facts = 0
        valid_fundamentals = 0
        candidates: list[tuple[ScreenRecord, EntryFilterEvaluation]] = []
        near_misses: list[CandidateNearMiss] = []
        month = report.as_of.strftime("%Y-%m")
        self._roll_month(month)

        for record in report.records:
            self.symbols_evaluated += 1
            evaluation = evaluate_variant_entry(record, variant, config)
            reason = _diagnostic_failure_reason(record, evaluation, config)
            category = _failure_category(reason, record, evaluation)
            if reason is not None:
                first_failures[reason] += 1
                stage_rejected[_failure_stage(reason)] += 1
                categories[category] += 1
            else:
                assert evaluation.weighted_score is not None
                candidates.append((record, evaluation))
                self._candidate_symbols.add(record.symbol)

            if _reached_stage(reason, "pit_fundamentals"):
                requiring_fundamentals += 1
                if record.pit_fact_count > 0:
                    with_facts += 1
                    if (
                        record.scores.quality.score is not None
                        and record.scores.valuation.score is not None
                    ):
                        valid_fundamentals += 1

            self._observe_score_values(record)
            self._observe_threshold_distance(reason, record, evaluation)
            self._observe_coverage(
                record,
                reason,
                fundamental_available,
                fundamental_missing,
                technical_available,
                technical_missing,
                relative_volume,
            )
            if _reached_stage(reason, "recovery_gate"):
                recovery["reached_recovery_gate"] += 1
                if evaluation.rsi_recovery is True:
                    recovery["passed_via_rsi"] += 1
                if evaluation.momentum5_above_zero is True:
                    recovery["passed_via_momentum"] += 1
                if evaluation.relative_volume_above_threshold is True:
                    recovery["passed_via_relative_volume"] += 1
                if evaluation.recovery_gate_pass is True:
                    recovery["passed_recovery_gate"] += 1
                else:
                    recovery["failed_all_recovery_triggers"] += 1

            if reason in NEAR_MISS_FAILURES:
                near_misses.append(_near_miss(report.as_of, record, evaluation, reason, config))

            if (
                record.pit_fact_count > 0
                and len(self._pit_samples) < 25
                and (reason in NEAR_MISS_FAILURES or reason is None)
            ):
                self._pit_samples.append(
                    PointInTimeSample(
                        symbol=record.symbol,
                        screen_date=report.as_of,
                        facts_available=record.pit_fact_count,
                        latest_filing_date=record.latest_pit_filing_date,
                        latest_period_end=record.latest_pit_period_end,
                    )
                )
            if (
                "missing_or_small_market_cap" in record.exclusion_reasons
                and record.estimated_market_cap is not None
                and record.estimated_market_cap >= config.universe.min_market_cap
            ):
                self._pipeline_inconsistencies["market_cap_rejected_after_pass"] += 1
            if (
                record.latest_pit_filing_date is not None
                and record.latest_pit_filing_date > report.as_of
            ):
                self._pipeline_inconsistencies["future_filing_used"] += 1
            if (
                record.technical.market_session is not None
                and record.technical.market_session > report.as_of
            ):
                self._pipeline_inconsistencies["future_market_bar_used"] += 1

        incoming = len(report.records)
        stage_incoming: dict[str, int] = {}
        stage_pass: dict[str, int] = {}
        for stage, field_name in FUNNEL_STAGES:
            stage_incoming[stage] = incoming
            incoming -= stage_rejected[stage]
            if incoming < 0:
                raise AssertionError(f"candidate funnel underflow at {report.as_of} stage={stage}")
            stage_pass[field_name] = incoming
        if incoming != len(candidates):
            raise AssertionError(
                f"candidate funnel conservation failed at {report.as_of}: "
                f"remaining={incoming} eligible={len(candidates)}"
            )

        if requiring_fundamentals != stage_incoming["pit_fundamentals"]:
            raise AssertionError("PIT fundamental denominator does not conserve the funnel")
        incomplete_fundamentals = max(0, with_facts - valid_fundamentals)
        without_fundamentals = max(0, requiring_fundamentals - with_facts)

        values: dict[str, Any] = {
            "date": report.as_of,
            "universe_total": len(report.records),
            **stage_pass,
            "identity_conflict": stage_rejected["identity"],
            "reached_recovery_gate": stage_incoming["recovery_gate"],
            "eligible_candidates": len(candidates),
            "ranked_candidates": len(candidates),
            "companies_requiring_fundamentals": requiring_fundamentals,
            "companies_with_valid_pit_fundamentals": valid_fundamentals,
            "companies_with_incomplete_pit_fundamentals": incomplete_fundamentals,
            "companies_without_pit_fundamentals": without_fundamentals,
            "pit_fundamental_coverage_pct": (
                with_facts / requiring_fundamentals if requiring_fundamentals else None
            ),
            "data_quality_failures": categories[FailureCategory.DATA_QUALITY],
            "strategy_rejections": categories[FailureCategory.STRATEGY_REJECTION],
            "other_failures": categories[FailureCategory.OTHER],
            "passed_via_rsi": recovery["passed_via_rsi"],
            "passed_via_momentum": recovery["passed_via_momentum"],
            "passed_via_relative_volume": recovery["passed_via_relative_volume"],
            "failed_all_recovery_triggers": recovery["failed_all_recovery_triggers"],
            "first_failure_reasons": dict(sorted(first_failures.items())),
            "stage_incoming": stage_incoming,
            "stage_rejected": dict(stage_rejected),
            "fundamental_metric_available": dict(fundamental_available),
            "fundamental_metric_missing": dict(fundamental_missing),
            "technical_metric_available": dict(technical_available),
            "technical_metric_missing": dict(technical_missing),
            "relative_volume_diagnostics": {
                "relative_volume_available": (
                    relative_volume["relative_volume_above_threshold"]
                    + relative_volume["relative_volume_below_threshold"]
                ),
                "relative_volume_missing": relative_volume["relative_volume_missing"],
                "relative_volume_below_threshold": relative_volume[
                    "relative_volume_below_threshold"
                ],
                "relative_volume_above_threshold": relative_volume[
                    "relative_volume_above_threshold"
                ],
            },
        }
        self._states[report.as_of] = _SessionState(report.as_of, values)
        for record, evaluation in candidates:
            assert evaluation.weighted_score is not None
            self._candidates[(report.as_of, record.symbol)] = _CandidateState(
                date=report.as_of,
                symbol=record.symbol,
                variant_score=evaluation.weighted_score,
                quality_score=evaluation.quality_score,
                valuation_score=evaluation.valuation_score,
                opportunity_score=evaluation.opportunity_score,
                timing_score=evaluation.timing_score,
                price_above_sma20=evaluation.price_above_sma20,
                rsi_recovery=evaluation.rsi_recovery,
                momentum5_above_zero=evaluation.momentum5_above_zero,
                relative_volume=record.technical.relative_volume,
                relative_volume_above_threshold=(
                    evaluation.relative_volume_above_threshold
                ),
            )

        near_misses.sort(key=_near_miss_sort_key)
        selected = near_misses[: self.near_miss_limit]
        self._near_misses.extend(selected)
        self._near_miss_symbols.update(item.symbol for item in selected)
        if len(self._states) % 25 == 0:
            LOGGER.info(
                "CANDIDATE AUDIT progress sessions=%d symbols=%d date=%s",
                len(self._states),
                self.symbols_evaluated,
                report.as_of,
            )

    def observe_portfolio_decision(
        self,
        signal_date: date,
        symbol: str,
        outcome: str,
        reason: str | None = None,
    ) -> None:
        state = self._states[signal_date]
        candidate = self._candidates.get((signal_date, symbol))
        if outcome == "order_created":
            state.values["portfolio_eligible"] = state.values.get("portfolio_eligible", 0) + 1
            state.values["entry_orders_created"] = state.values.get(
                "entry_orders_created", 0
            ) + 1
            if candidate is not None:
                candidate.order_created = True
                candidate.status = "entry_order_created"
            return
        blocker = reason or "other"
        state.portfolio_blockers[blocker] += 1
        if candidate is not None:
            candidate.status = "portfolio_blocked"
            candidate.blocker = blocker

    def observe_execution(
        self,
        signal_date: date,
        execution_date: date,
        symbol: str,
        executed: bool,
        reason: str | None = None,
    ) -> None:
        state = self._states[signal_date]
        candidate = self._candidates.get((signal_date, symbol))
        if executed:
            state.values["actual_entries"] = state.values.get("actual_entries", 0) + 1
            self._entry_symbols.add(symbol)
            if candidate is not None:
                candidate.entry_executed = True
                candidate.execution_date = execution_date
                candidate.status = "entry_executed"
            return
        blocker = reason or "entry_rejected"
        state.execution_blockers[blocker] += 1
        if candidate is not None:
            candidate.status = "execution_blocked"
            candidate.blocker = blocker
            candidate.execution_date = execution_date

    def finalize(self, result: BacktestResult) -> CandidateAuditResult:
        sessions = tuple(self._session_model(state) for state in self._states.values())
        monthly = self._monthly(sessions)
        failure_rows = tuple(self._failure_rows(monthly, sessions))
        candidates = tuple(
            CandidateAuditEvent(
                date=item.date,
                symbol=item.symbol,
                variant_score=item.variant_score,
                status=item.status,
                order_created=item.order_created,
                entry_executed=item.entry_executed,
                execution_date=item.execution_date,
                blocker=item.blocker,
                quality_score=item.quality_score,
                valuation_score=item.valuation_score,
                opportunity_score=item.opportunity_score,
                timing_score=item.timing_score,
                price_above_sma20=item.price_above_sma20,
                rsi_recovery=item.rsi_recovery,
                momentum5_above_zero=item.momentum5_above_zero,
                relative_volume=item.relative_volume,
                relative_volume_above_threshold=item.relative_volume_above_threshold,
            )
            for item in sorted(self._candidates.values(), key=lambda item: (item.date, item.symbol))
        )
        first_eligible = next(
            (item.date for item in sessions if item.eligible_candidates > 0), None
        )
        first_signal = next((item.date for item in sessions if item.actual_entries > 0), None)
        execution_dates = [
            item.execution_date
            for item in candidates
            if item.entry_executed and item.execution_date
        ]
        first_entry = min(execution_dates) if execution_dates else None
        transition = _first_transition(sessions, first_eligible)
        aggregate = _aggregate_coverage(sessions)
        category, evidence = _classify_audit(sessions, self._pipeline_inconsistencies)
        warnings = [*BACKTEST_WARNINGS]
        warnings.extend(
            (
                "candidate audit observes the production PIT screen and never feeds diagnostics "
                "back into trading decisions",
                "current tradable universe membership can create survivorship bias in historical "
                "candidate counts",
            )
        )
        if self._pipeline_inconsistencies:
            warnings.append(
                "diagnostic pipeline inconsistencies were observed; see classification_evidence"
            )
        recovery_total = Counter()
        portfolio_total = Counter()
        execution_total = Counter()
        for item in sessions:
            recovery_total.update(
                {
                    "reached_recovery_gate": item.reached_recovery_gate,
                    "passed_recovery_gate": item.recovery_gate_pass,
                    "passed_via_rsi": item.passed_via_rsi,
                    "passed_via_momentum": item.passed_via_momentum,
                    "passed_via_relative_volume": item.passed_via_relative_volume,
                    "failed_all_recovery_triggers": item.failed_all_recovery_triggers,
                }
            )
            portfolio_total.update(item.portfolio_blockers)
            execution_total.update(item.execution_blockers)
        return CandidateAuditResult(
            requested_start=result.requested_start,
            requested_end=result.requested_end,
            actual_start=result.actual_start,
            actual_end=result.actual_end,
            generated_at=datetime.now(UTC).isoformat(),
            strategy_variant=self.variant,
            configuration={
                "backtest_entry": {
                    "min_quality_score": self.config.backtest.min_quality_score,
                    "min_valuation_score": self.config.backtest.min_valuation_score,
                    "min_opportunity_score": self.config.backtest.min_opportunity_score,
                    "min_timing_score": self.config.backtest.min_timing_score,
                    "min_total_score": self.config.backtest.min_total_score,
                    "min_relative_volume": self.config.backtest.min_relative_volume,
                },
                "portfolio": self.config.portfolio.model_dump(mode="json"),
                "filters": self.config.filters.model_dump(mode="json"),
            },
            sessions=sessions,
            monthly_summary=monthly,
            failure_reasons=failure_rows,
            near_misses=tuple(self._near_misses),
            candidates=candidates,
            data_coverage=aggregate,
            score_distributions={
                item.month: item.score_distributions for item in monthly
            },
            recovery_gate_analysis=dict(recovery_total),
            portfolio_blockers=dict(sorted(portfolio_total.items())),
            execution_blockers=dict(sorted(execution_total.items())),
            period_comparison=_period_comparison(sessions, first_signal),
            first_eligible_candidate_date=first_eligible,
            first_entry_signal_date=first_signal,
            first_entry_date=first_entry,
            first_candidate_transition=transition,
            candidate_symbols=tuple(sorted(self._candidate_symbols)),
            entry_symbols=tuple(sorted(self._entry_symbols)),
            near_miss_symbols=tuple(sorted(self._near_miss_symbols)),
            pit_samples=tuple(self._pit_samples),
            classification=category,
            classification_evidence=evidence,
            warnings=tuple(dict.fromkeys(warnings)),
            performance_diagnostics={
                **result.performance_diagnostics,
                "audit_seconds": round(time.perf_counter() - self.started, 6),
                "sessions_processed": len(sessions),
                "symbols_evaluated": self.symbols_evaluated,
                "retained_candidate_records": len(candidates),
                "retained_near_miss_records": len(self._near_misses),
                "retained_pit_samples": len(self._pit_samples),
            },
        )

    def _session_model(self, state: _SessionState) -> CandidateAuditSession:
        return CandidateAuditSession(
            **state.values,
            portfolio_blockers=dict(sorted(state.portfolio_blockers.items())),
            execution_blockers=dict(sorted(state.execution_blockers.items())),
        )

    def _observe_score_values(self, record: ScreenRecord) -> None:
        values = {
            "quality_score": record.scores.quality.score,
            "valuation_score": record.scores.valuation.score,
            "opportunity_score": record.scores.opportunity.score,
            "timing_score": record.scores.timing.score,
            "total_score": record.scores.total,
        }
        for name, value in values.items():
            if value is not None and math.isfinite(value):
                self._current_score_values[name].append(float(value))

    def _observe_threshold_distance(
        self,
        reason: str | None,
        record: ScreenRecord,
        evaluation: EntryFilterEvaluation,
    ) -> None:
        distances = _threshold_distances(record, evaluation, self.config)
        if reason in distances and distances[reason] is not None:
            self._current_distance_values[reason].append(float(distances[reason]))

    def _roll_month(self, month: str) -> None:
        if self._current_month == month:
            return
        self._finish_month()
        self._current_month = month

    def _finish_month(self) -> None:
        if self._current_month is None:
            return
        month = self._current_month
        self._monthly_score_summaries[month] = {
            name: distribution(values)
            for name, values in self._current_score_values.items()
        }
        self._monthly_distance_summaries[month] = {
            name: distribution(values)
            for name, values in self._current_distance_values.items()
        }
        self._current_score_values = defaultdict(list)
        self._current_distance_values = defaultdict(list)
        self._current_month = None

    def _observe_coverage(
        self,
        record: ScreenRecord,
        first_failure: str | None,
        fundamental_available: Counter[str],
        fundamental_missing: Counter[str],
        technical_available: Counter[str],
        technical_missing: Counter[str],
        relative_volume: Counter[str],
    ) -> None:
        screening_relevant = _reached_stage(first_failure, "pit_fundamentals")
        if screening_relevant and record.pit_fact_count > 0:
            factors = {
                factor.name: factor.raw_value
                for breakdown in (record.scores.quality, record.scores.valuation)
                for factor in breakdown.factors
            }
            for name in FUNDAMENTAL_METRICS:
                if name == "operating_cash_flow_positive":
                    value = record.fundamentals.operating_cash_flow_positive
                elif name in {"relative_pe", "relative_ev_ebitda"}:
                    value = factors.get(name)
                else:
                    value = getattr(record.fundamentals, name, None)
                (fundamental_available if value is not None else fundamental_missing)[name] += 1

        if screening_relevant:
            for name in TECHNICAL_METRICS:
                value = getattr(record.technical, name)
                (technical_available if value is not None else technical_missing)[name] += 1
            rv = record.technical.relative_volume
            if rv is None:
                relative_volume["relative_volume_missing"] += 1
            elif rv > self.config.backtest.min_relative_volume:
                relative_volume["relative_volume_above_threshold"] += 1
            else:
                relative_volume["relative_volume_below_threshold"] += 1

    def _monthly(
        self, sessions: tuple[CandidateAuditSession, ...]
    ) -> tuple[CandidateAuditMonthly, ...]:
        self._finish_month()
        grouped: dict[str, list[CandidateAuditSession]] = defaultdict(list)
        for item in sessions:
            grouped[item.date.strftime("%Y-%m")].append(item)
        output: list[CandidateAuditMonthly] = []
        for month, items in grouped.items():
            failures = sum((Counter(item.first_failure_reasons) for item in items), Counter())
            stage_incoming = sum((Counter(item.stage_incoming) for item in items), Counter())
            fundamental_available = sum(
                (Counter(item.fundamental_metric_available) for item in items), Counter()
            )
            fundamental_missing = sum(
                (Counter(item.fundamental_metric_missing) for item in items), Counter()
            )
            technical_available = sum(
                (Counter(item.technical_metric_available) for item in items), Counter()
            )
            technical_missing = sum(
                (Counter(item.technical_metric_missing) for item in items), Counter()
            )
            portfolio = sum((Counter(item.portfolio_blockers) for item in items), Counter())
            requiring = sum(item.companies_requiring_fundamentals for item in items)
            with_facts = sum(
                item.companies_with_valid_pit_fundamentals
                + item.companies_with_incomplete_pit_fundamentals
                for item in items
            )
            output.append(
                CandidateAuditMonthly(
                    month=month,
                    screens=len(items),
                    universe_observations=sum(item.universe_total for item in items),
                    market_history_available=sum(
                        item.market_history_available for item in items
                    ),
                    pit_fundamental_coverage_pct=(
                        with_facts / requiring if requiring else None
                    ),
                    candidates_before_recovery=sum(
                        item.reached_recovery_gate for item in items
                    ),
                    recovery_passes=sum(item.recovery_gate_pass for item in items),
                    eligible_candidates=sum(item.eligible_candidates for item in items),
                    entry_orders_created=sum(item.entry_orders_created for item in items),
                    actual_entries=sum(item.actual_entries for item in items),
                    primary_blocker=(
                        max(failures, key=lambda reason: failures[reason]) if failures else None
                    ),
                    failure_reasons=dict(sorted(failures.items())),
                    failure_rates_at_stage={
                        reason: count / stage_incoming[_failure_stage(reason)]
                        for reason, count in failures.items()
                        if stage_incoming[_failure_stage(reason)]
                    },
                    portfolio_blockers=dict(sorted(portfolio.items())),
                    score_distributions=self._monthly_score_summaries.get(month, {}),
                    threshold_distance_distributions=(
                        self._monthly_distance_summaries.get(month, {})
                    ),
                    fundamental_metric_coverage_pct=_coverage_percentages(
                        fundamental_available, fundamental_missing
                    ),
                    technical_metric_coverage_pct=_coverage_percentages(
                        technical_available, technical_missing
                    ),
                )
            )
        return tuple(output)

    def _failure_rows(
        self,
        monthly: tuple[CandidateAuditMonthly, ...],
        sessions: tuple[CandidateAuditSession, ...],
    ) -> list[CandidateFailureSummary]:
        stage_by_month: dict[str, Counter[str]] = defaultdict(Counter)
        for item in sessions:
            stage_by_month[item.date.strftime("%Y-%m")].update(item.stage_incoming)
        output: list[CandidateFailureSummary] = []
        for item in monthly:
            for reason, rejected in item.failure_reasons.items():
                incoming = stage_by_month[item.month][_failure_stage(reason)]
                output.append(
                    CandidateFailureSummary(
                        month=item.month,
                        reason=reason,
                        category=_category_from_reason(reason),
                        rejected=rejected,
                        incoming_at_stage=incoming,
                        rejection_rate_at_stage=rejected / incoming if incoming else None,
                    )
                )
        return output


def run_candidate_audit(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    variant: StrategyVariant = StrategyVariant.FULL,
    near_miss_limit: int = 10,
) -> CandidateAuditResult:
    """Run a configured backtest while observing its exact historical entry funnel."""

    collector = HistoricalCandidateAuditCollector(
        config, variant, near_miss_limit=near_miss_limit
    )
    result = BacktestEngine(database, config, audit_observer=collector).run(
        start,
        end,
        variant=variant,
        preset=PositionManagementPreset.CONFIGURED,
    )
    return collector.finalize(result)


def distribution(values: list[float] | tuple[float, ...]) -> DistributionSummary:
    clean = np.asarray([value for value in values if math.isfinite(value)], dtype=float)
    if clean.size == 0:
        return DistributionSummary(count=0)
    return DistributionSummary(
        count=int(clean.size),
        mean=float(np.mean(clean)),
        median=float(np.median(clean)),
        p10=float(np.quantile(clean, 0.10)),
        p25=float(np.quantile(clean, 0.25)),
        p75=float(np.quantile(clean, 0.75)),
        p90=float(np.quantile(clean, 0.90)),
        maximum=float(np.max(clean)),
    )


def _diagnostic_failure_reason(
    record: ScreenRecord,
    evaluation: EntryFilterEvaluation,
    config: StrategyConfig,
) -> str | None:
    reason = evaluation.failure_detail or evaluation.first_failure
    if reason == "invalid_or_low_price":
        return "missing_price" if record.technical.price is None else "low_price"
    if reason == "insufficient_liquidity":
        return (
            "missing_liquidity_history"
            if record.average_dollar_volume_20d is None
            else "insufficient_liquidity"
        )
    if reason == "missing_or_small_market_cap":
        return "missing_market_cap" if record.estimated_market_cap is None else "small_market_cap"
    if reason == "positive_operating_cash_flow_required":
        return (
            "missing_operating_cash_flow"
            if record.fundamentals.operating_cash_flow_positive is None
            else "positive_operating_cash_flow_required"
        )
    if reason == "sma20_or_price_unavailable":
        return "missing_sma20_or_price"
    if reason == "recovery_inputs_unavailable":
        return "missing_recovery_inputs"
    return reason


def _failure_stage(reason: str) -> str:
    mapping = {
        "identity_conflict": "identity",
        "reit_excluded": "static_filters",
        "financial_excluded": "static_filters",
        "insufficient_market_history": "market_history",
        "stale_market_data": "market_history",
        "analysis_error": "market_history",
        "missing_price": "valid_price",
        "low_price": "valid_price",
        "invalid_or_low_price": "valid_price",
        "missing_liquidity_history": "liquidity",
        "insufficient_liquidity": "liquidity",
        "missing_market_cap": "market_cap",
        "small_market_cap": "market_cap",
        "missing_or_small_market_cap": "market_cap",
        "universe_filter_failed": "market_cap",
        "no_point_in_time_fundamentals": "pit_fundamentals",
        "missing_operating_cash_flow": "positive_operating_cash_flow",
        "positive_operating_cash_flow_required": "positive_operating_cash_flow",
        "quality_score_unavailable": "quality_score_available",
        "quality_threshold": "quality_threshold",
        "valuation_score_unavailable": "valuation_score_available",
        "valuation_threshold": "valuation_threshold",
        "opportunity_score_unavailable": "opportunity_score_available",
        "opportunity_threshold": "opportunity_threshold",
        "timing_score_unavailable": "timing_score_available",
        "timing_threshold": "timing_threshold",
        "total_score_unavailable": "total_score_available",
        "total_threshold": "total_threshold",
        "missing_sma20_or_price": "price_above_sma20",
        "price_not_above_sma20": "price_above_sma20",
        "missing_recovery_inputs": "recovery_gate",
        "recovery_signal_required": "recovery_gate",
    }
    return mapping.get(reason, "pit_fundamentals")


def _failure_category(
    reason: str | None,
    record: ScreenRecord | None = None,
    evaluation: EntryFilterEvaluation | None = None,
) -> FailureCategory:
    if reason is None:
        return FailureCategory.OTHER
    return _category_from_reason(reason)


def _category_from_reason(reason: str) -> FailureCategory:
    if reason in {
        "identity_conflict",
        "insufficient_market_history",
        "stale_market_data",
        "analysis_error",
        "missing_price",
        "missing_liquidity_history",
        "missing_market_cap",
        "no_point_in_time_fundamentals",
        "missing_operating_cash_flow",
        "quality_score_unavailable",
        "valuation_score_unavailable",
        "opportunity_score_unavailable",
        "timing_score_unavailable",
        "total_score_unavailable",
        "missing_sma20_or_price",
        "missing_recovery_inputs",
    }:
        return FailureCategory.DATA_QUALITY
    if reason in {
        "reit_excluded",
        "financial_excluded",
        "low_price",
        "insufficient_liquidity",
        "small_market_cap",
        "positive_operating_cash_flow_required",
        "quality_threshold",
        "valuation_threshold",
        "opportunity_threshold",
        "timing_threshold",
        "total_threshold",
        "price_not_above_sma20",
        "recovery_signal_required",
    }:
        return FailureCategory.STRATEGY_REJECTION
    return FailureCategory.OTHER


def _reached_stage(reason: str | None, stage: str) -> bool:
    return reason is None or STAGE_INDEX[_failure_stage(reason)] >= STAGE_INDEX[stage]


def _threshold_distances(
    record: ScreenRecord,
    evaluation: EntryFilterEvaluation,
    config: StrategyConfig,
) -> dict[str, float | None]:
    rules = config.backtest
    return {
        "quality_threshold": _distance(evaluation.quality_score, rules.min_quality_score),
        "valuation_threshold": _distance(
            evaluation.valuation_score, rules.min_valuation_score
        ),
        "opportunity_threshold": _distance(
            evaluation.opportunity_score, rules.min_opportunity_score
        ),
        "timing_threshold": _distance(evaluation.timing_score, rules.min_timing_score),
        "total_threshold": _distance(evaluation.weighted_score, rules.min_total_score),
        "price_not_above_sma20": (
            None
            if record.technical.price is None or record.technical.sma20 is None
            else record.technical.price - record.technical.sma20
        ),
        "recovery_signal_required": _distance(
            record.technical.relative_volume, rules.min_relative_volume
        ),
    }


def _distance(value: float | None, threshold: float) -> float | None:
    return None if value is None else float(value - threshold)


def _near_miss(
    session: date,
    record: ScreenRecord,
    evaluation: EntryFilterEvaluation,
    reason: str,
    config: StrategyConfig,
) -> CandidateNearMiss:
    return CandidateNearMiss(
        date=session,
        symbol=record.symbol,
        failed_at=reason,
        failure_category=_category_from_reason(reason),
        distance_to_threshold=_threshold_distances(record, evaluation, config).get(reason),
        total_score=evaluation.weighted_score,
        quality_score=evaluation.quality_score,
        valuation_score=evaluation.valuation_score,
        opportunity_score=evaluation.opportunity_score,
        timing_score=evaluation.timing_score,
        price_above_sma20=evaluation.price_above_sma20,
        rsi_recovery=evaluation.rsi_recovery,
        momentum5_above_zero=evaluation.momentum5_above_zero,
        relative_volume=record.technical.relative_volume,
        relative_volume_above_threshold=evaluation.relative_volume_above_threshold,
    )


def _near_miss_sort_key(item: CandidateNearMiss) -> tuple[int, float, float, str]:
    stage = STAGE_INDEX[_failure_stage(item.failed_at)]
    distance = (
        abs(item.distance_to_threshold)
        if item.distance_to_threshold is not None
        else math.inf
    )
    return (-stage, distance, -(item.total_score or -math.inf), item.symbol)


def _coverage_percentages(
    available: Counter[str], missing: Counter[str]
) -> dict[str, float]:
    names = set(available) | set(missing)
    return {
        name: available[name] / (available[name] + missing[name])
        for name in sorted(names)
        if available[name] + missing[name]
    }


def _aggregate_coverage(sessions: tuple[CandidateAuditSession, ...]) -> dict:
    requiring = sum(item.companies_requiring_fundamentals for item in sessions)
    valid = sum(item.companies_with_valid_pit_fundamentals for item in sessions)
    incomplete = sum(item.companies_with_incomplete_pit_fundamentals for item in sessions)
    without = sum(item.companies_without_pit_fundamentals for item in sessions)
    fundamental_available = sum(
        (Counter(item.fundamental_metric_available) for item in sessions), Counter()
    )
    fundamental_missing = sum(
        (Counter(item.fundamental_metric_missing) for item in sessions), Counter()
    )
    technical_available = sum(
        (Counter(item.technical_metric_available) for item in sessions), Counter()
    )
    technical_missing = sum(
        (Counter(item.technical_metric_missing) for item in sessions), Counter()
    )
    relative_volume = sum(
        (Counter(item.relative_volume_diagnostics) for item in sessions), Counter()
    )
    return {
        "companies_requiring_fundamentals": requiring,
        "companies_with_valid_pit_fundamentals": valid,
        "companies_with_incomplete_pit_fundamentals": incomplete,
        "companies_without_pit_fundamentals": without,
        "pit_fundamental_coverage_pct": (
            (valid + incomplete) / requiring if requiring else None
        ),
        "fundamental_metric_coverage_pct": _coverage_percentages(
            fundamental_available, fundamental_missing
        ),
        "technical_metric_coverage_pct": _coverage_percentages(
            technical_available, technical_missing
        ),
        "relative_volume": {
            key: relative_volume[key]
            for key in (
                "relative_volume_available",
                "relative_volume_missing",
                "relative_volume_below_threshold",
                "relative_volume_above_threshold",
            )
        },
    }


def _first_transition(
    sessions: tuple[CandidateAuditSession, ...], first: date | None
) -> dict:
    if first is None:
        return {}
    index = next(i for i, item in enumerate(sessions) if item.date == first)
    keys = (
        "date",
        "pit_fundamental_coverage_pct",
        "timing_threshold_pass",
        "reached_recovery_gate",
        "recovery_gate_pass",
        "eligible_candidates",
        "actual_entries",
        "relative_volume_diagnostics",
    )
    current = sessions[index].model_dump(mode="json")
    previous = sessions[index - 1].model_dump(mode="json") if index else None
    return {
        "previous_session": (
            {key: previous[key] for key in keys} if previous is not None else None
        ),
        "first_eligible_session": {key: current[key] for key in keys},
    }


def _period_comparison(
    sessions: tuple[CandidateAuditSession, ...], first_entry_signal: date | None
) -> tuple[dict, ...]:
    groups: dict[str, list[CandidateAuditSession]] = defaultdict(list)
    for item in sessions:
        half = "H1" if item.date.month <= 6 else "H2"
        groups[f"{item.date.year}-{half}"].append(item)
        if first_entry_signal is not None:
            label = (
                "pre_first_entry_signal"
                if item.date < first_entry_signal
                else "first_entry_signal_and_after"
            )
            groups[label].append(item)
    output = []
    for label, items in groups.items():
        failures = sum((Counter(item.first_failure_reasons) for item in items), Counter())
        requiring = sum(item.companies_requiring_fundamentals for item in items)
        with_facts = sum(
            item.companies_with_valid_pit_fundamentals
            + item.companies_with_incomplete_pit_fundamentals
            for item in items
        )
        output.append(
            {
                "period": label,
                "start": items[0].date.isoformat(),
                "end": items[-1].date.isoformat(),
                "screens": len(items),
                "pit_fundamental_coverage_pct": with_facts / requiring if requiring else None,
                "candidates_before_recovery": sum(
                    item.reached_recovery_gate for item in items
                ),
                "recovery_passes": sum(item.recovery_gate_pass for item in items),
                "eligible_candidates": sum(item.eligible_candidates for item in items),
                "actual_entries": sum(item.actual_entries for item in items),
                "primary_blocker": max(failures, key=failures.get) if failures else None,
            }
        )
    return tuple(output)


def _classify_audit(
    sessions: tuple[CandidateAuditSession, ...], inconsistencies: Counter[str]
) -> tuple[str, dict]:
    data_failures = sum(item.data_quality_failures for item in sessions)
    strategy_failures = sum(item.strategy_rejections for item in sessions)
    classified = data_failures + strategy_failures
    data_share = data_failures / classified if classified else 0.0
    requiring = sum(item.companies_requiring_fundamentals for item in sessions)
    with_facts = sum(
        item.companies_with_valid_pit_fundamentals
        + item.companies_with_incomplete_pit_fundamentals
        for item in sessions
    )
    pit_coverage = with_facts / requiring if requiring else None
    if inconsistencies:
        category = "D - Bug / Pipeline Inconsistency"
    elif data_share >= 0.5 or (pit_coverage is not None and pit_coverage < 0.5):
        category = "B - Data Limited"
    elif data_share >= 0.05 or (pit_coverage is not None and pit_coverage < 0.9):
        category = "C - Mixed"
    else:
        category = "A - Strategy Genuine"
    return category, {
        "data_quality_failures": data_failures,
        "strategy_rejections": strategy_failures,
        "data_failure_share": data_share,
        "pit_fundamental_coverage_pct": pit_coverage,
        "pipeline_inconsistencies": dict(inconsistencies),
        "classification_rule": (
            "D if an internally inconsistent gate is observed; B if data failures are at least "
            "50% or PIT coverage is below 50%; C if data failures are at least 5% or PIT "
            "coverage below 90%; otherwise A"
        ),
    }
