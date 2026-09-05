"""Registered point-in-time market-regime capacity research for Strategy F."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

from trading_system.backtest.engine import (
    BacktestEngine,
    EntryCapacityTrace,
    prepare_strategy_comparison,
)
from trading_system.backtest.market_regime import (
    MarketRegimeCapacitySchedule,
    MarketRegimeState,
)
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import (
    F_REGIME_CAPACITY_RESEARCH_FAMILY,
    F_REGIME_CAPACITY_RESEARCH_STATUS,
    F_REGIME_CAPACITY_RESEARCH_VARIANTS,
    FROZEN_CHAMPION_F,
    FRegimeCapacityResearchVariant,
)
from trading_system.backtest.universe_provenance import audit_universe_provenance
from trading_system.backtest.validation import (
    CANONICAL_COST_STRESS_CASES,
    _cost_row,
    _field_union,
    calendar_stability,
    chronological_subperiod_analysis,
    strategy_summary,
    symbol_and_leave_one_out,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    StrategyComparison,
    StrategyComparisonKind,
)

SESSION_RETURN_ATTRIBUTION = (
    "Equity-curve/session attribution: each close-to-close portfolio-equity return is assigned "
    "to the SPY regime calculated at that session's completed close; returns within each regime "
    "are compounded chronologically. This is not exit-day position-PnL attribution."
)


@dataclass(frozen=True)
class FRegimeCapacityResearchBundle:
    """Complete evidence bundle for two controls and two adaptive hypotheses."""

    requested_start: date
    requested_end: date
    results: dict[str, BacktestResult]
    schedules: dict[str, MarketRegimeCapacitySchedule]
    traces: dict[str, tuple[EntryCapacityTrace, ...]]
    metric_rows: list[dict[str, Any]]
    regime_diagnostic_rows: list[dict[str, Any]]
    regime_summary_rows: list[dict[str, Any]]
    monthly_rows: list[dict[str, Any]]
    yearly_rows: list[dict[str, Any]]
    chronological_subperiod_rows: list[dict[str, Any]]
    entry_rank_rows: list[dict[str, Any]]
    cost_rows: list[dict[str, Any]]
    symbol_concentration_rows: list[dict[str, Any]]
    concentration: dict[str, Any]
    time_stability: dict[str, Any]
    universe_provenance: dict[str, Any]
    period_classification: str


def regime_strategy_config(
    config: StrategyConfig,
    identity: FRegimeCapacityResearchVariant,
) -> StrategyConfig:
    """Copy F/configured and set only the registered static/hard capacity ceiling."""

    if identity not in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
        raise ValueError("unregistered F regime-capacity identity")
    return config.model_copy(
        update={
            "portfolio": config.portfolio.model_copy(
                update={"max_positions": identity.configured_hard_max_positions}
            )
        }
    )


def regime_diagnostic_rows(
    identity: FRegimeCapacityResearchVariant,
    result: BacktestResult,
    schedule: MarketRegimeCapacitySchedule,
    traces: tuple[EntryCapacityTrace, ...],
) -> list[dict[str, Any]]:
    """Join immutable regime decisions to engine-observed entry-capacity events."""

    by_session = {trace.signal_session: trace for trace in traces}
    rows: list[dict[str, Any]] = []
    for point in result.equity_curve:
        decision = schedule.decision(point.date)
        trace = by_session.get(point.date)
        open_before = trace.open_positions_before_entries if trace is not None else None
        open_after = trace.open_positions_after_entries if trace is not None else None
        rows.append(
            {
                "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
                "strategy": identity.label,
                "session": point.date.isoformat(),
                "execution_session": (
                    trace.execution_session.isoformat()
                    if trace is not None and trace.execution_session is not None
                    else None
                ),
                "regime": decision.regime.value,
                "target_capacity": decision.target_capacity,
                "open_positions_before_entries": open_before,
                "open_positions_after_entries": open_after,
                "open_positions_at_signal_selection": (
                    trace.open_positions_at_signal if trace is not None else None
                ),
                "available_slots": (trace.available_slots if trace is not None else None),
                "spy_close": decision.spy_close,
                "spy_sma200": decision.spy_sma200,
                "spy_momentum126": decision.spy_momentum126,
                "regime_reason": decision.reason,
                "orders_created": trace.orders_created if trace is not None else 0,
                "entries_opened": trace.entries_opened if trace is not None else 0,
                "capacity_blocked_candidates": (
                    trace.capacity_blocked_candidates if trace is not None else 0
                ),
                "entry_selection_performed": trace is not None,
            }
        )
    return rows


def regime_summary_row(
    identity: FRegimeCapacityResearchVariant,
    result: BacktestResult,
    schedule: MarketRegimeCapacitySchedule,
    diagnostic_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize adaptive regimes with equity-session rather than exit-day attribution."""

    if not identity.adaptive:
        raise ValueError("regime summary applies only to adaptive variants")
    decisions = [schedule.decision(point.date) for point in result.equity_curve]
    session_returns: dict[MarketRegimeState, list[float]] = {
        MarketRegimeState.RISK_ON: [],
        MarketRegimeState.RISK_OFF: [],
        MarketRegimeState.UNAVAILABLE: [],
    }
    exposure: dict[MarketRegimeState, list[float]] = {state: [] for state in session_returns}
    open_positions: dict[MarketRegimeState, list[int]] = {state: [] for state in session_returns}
    previous_equity = result.initial_capital
    for point, decision in zip(result.equity_curve, decisions, strict=True):
        session_return = point.portfolio_equity / previous_equity - 1 if previous_equity else 0.0
        previous_equity = point.portfolio_equity
        if decision.regime in session_returns:
            session_returns[decision.regime].append(session_return)
            exposure[decision.regime].append(point.exposure)
            open_positions[decision.regime].append(point.active_positions)

    states = [decision.regime for decision in decisions]
    available_states = [
        state
        for state in states
        if state in {MarketRegimeState.RISK_ON, MarketRegimeState.RISK_OFF}
    ]
    total = len(states)
    risk_on_count = states.count(MarketRegimeState.RISK_ON)
    risk_off_count = states.count(MarketRegimeState.RISK_OFF)
    return {
        "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
        "strategy": identity.label,
        "sessions_total": total,
        "sessions_risk_on": risk_on_count,
        "sessions_risk_off": risk_off_count,
        "sessions_unavailable": states.count(MarketRegimeState.UNAVAILABLE),
        "risk_on_percentage": risk_on_count / total if total else None,
        "risk_off_percentage": risk_off_count / total if total else None,
        "entries_risk_on": sum(
            row["entries_opened"]
            for row in diagnostic_rows
            if row["regime"] == MarketRegimeState.RISK_ON.value
        ),
        "entries_risk_off": sum(
            row["entries_opened"]
            for row in diagnostic_rows
            if row["regime"] == MarketRegimeState.RISK_OFF.value
        ),
        "entries_unavailable": sum(
            row["entries_opened"]
            for row in diagnostic_rows
            if row["regime"] == MarketRegimeState.UNAVAILABLE.value
        ),
        "return_during_risk_on": _compound(session_returns[MarketRegimeState.RISK_ON]),
        "return_during_risk_off": _compound(session_returns[MarketRegimeState.RISK_OFF]),
        "max_drawdown_risk_on": _drawdown_from_session_returns(
            session_returns[MarketRegimeState.RISK_ON]
        ),
        "max_drawdown_risk_off": _drawdown_from_session_returns(
            session_returns[MarketRegimeState.RISK_OFF]
        ),
        "average_exposure_risk_on": _mean_or_none(exposure[MarketRegimeState.RISK_ON]),
        "average_exposure_risk_off": _mean_or_none(exposure[MarketRegimeState.RISK_OFF]),
        "average_open_positions_risk_on": _mean_or_none(open_positions[MarketRegimeState.RISK_ON]),
        "average_open_positions_risk_off": _mean_or_none(
            open_positions[MarketRegimeState.RISK_OFF]
        ),
        "number_of_regime_switches": sum(
            previous != current
            for previous, current in zip(available_states, available_states[1:], strict=False)
        ),
        "return_attribution_method": SESSION_RETURN_ATTRIBUTION,
        "regime_switch_method": (
            "state changes across consecutive available RISK_ON/RISK_OFF classifications; "
            "UNAVAILABLE observations are skipped"
        ),
    }


def regime_entry_rank_analysis_rows(
    identity: FRegimeCapacityResearchVariant,
    result: BacktestResult,
    schedule: MarketRegimeCapacitySchedule,
) -> list[dict[str, Any]]:
    """Group unchanged candidate ranks by the PIT regime on their signal session."""

    regimes = (
        (MarketRegimeState.RISK_ON, MarketRegimeState.RISK_OFF, MarketRegimeState.UNAVAILABLE)
        if identity.adaptive
        else (MarketRegimeState.STATIC_CONTROL,)
    )
    grouped: dict[tuple[MarketRegimeState, int], list[BacktestPosition]] = {}
    for position in result.positions:
        if position.daily_candidate_rank is None:
            continue
        regime = schedule.decision(position.signal_date).regime
        grouped.setdefault((regime, position.daily_candidate_rank), []).append(position)
    maximum_rank = max(5, max((rank for _, rank in grouped), default=0))
    rows: list[dict[str, Any]] = []
    for regime in regimes:
        for rank in range(1, maximum_rank + 1):
            positions = grouped.get((regime, rank), [])
            pnls = [position.net_pnl for position in positions]
            rows.append(
                {
                    "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
                    "strategy": identity.label,
                    "regime_at_entry": regime.value,
                    "candidate_rank": rank,
                    "positions": len(positions),
                    "win_rate": (
                        sum(position.net_pnl > 0 for position in positions) / len(positions)
                        if positions
                        else None
                    ),
                    "average_return": (
                        mean(position.position_return for position in positions)
                        if positions
                        else None
                    ),
                    "net_pnl": sum(pnls),
                    "profit_factor": _profit_factor(pnls),
                    "average_holding_period": (
                        mean(position.holding_days for position in positions) if positions else None
                    ),
                    "MFE": (
                        mean(position.maximum_favorable_excursion for position in positions)
                        if positions
                        else None
                    ),
                    "MAE": (
                        mean(position.maximum_adverse_excursion for position in positions)
                        if positions
                        else None
                    ),
                }
            )
    return rows


def regime_metric_rows(
    results: dict[str, BacktestResult],
    schedules: dict[str, MarketRegimeCapacitySchedule],
) -> list[dict[str, Any]]:
    """Build required metrics plus explicit C1/C5 deltas without winner selection."""

    _require_complete_variant_set(results)
    rows: list[dict[str, Any]] = []
    for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
        result = results[identity.research_id]
        observations = [point.active_positions for point in result.equity_curve]
        decisions = [
            schedules[identity.research_id].decision(point.date) for point in result.equity_curve
        ]
        row = strategy_summary(identity.label, result)
        row.update(
            {
                "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
                "research_id": identity.research_id,
                "research_status": F_REGIME_CAPACITY_RESEARCH_STATUS,
                "capacity_mode": "ADAPTIVE_C1_C5" if identity.adaptive else "STATIC_CONTROL",
                "configured_hard_max_positions": identity.configured_hard_max_positions,
                "modeled_slippage_cost": row["slippage_cost"],
                "modeled_commission_cost": row["commission_cost"],
                "unique_traded_symbols": len({position.symbol for position in result.positions}),
                "average_simultaneous_positions": mean(observations) if observations else None,
                "median_simultaneous_positions": median(observations) if observations else None,
                "maximum_simultaneous_positions": max(observations, default=0),
                "sessions_at_effective_capacity": sum(
                    observed >= decision.target_capacity
                    for observed, decision in zip(observations, decisions, strict=True)
                ),
                "sessions_above_effective_capacity": sum(
                    observed > decision.target_capacity
                    for observed, decision in zip(observations, decisions, strict=True)
                ),
            }
        )
        rows.append(row)

    controls = {
        row["research_id"]: row
        for row in rows
        if row["research_id"] in {"CONTROL-C1", "CONTROL-C5"}
    }
    delta_metrics = {
        "return": "total_return",
        "max_drawdown": "max_drawdown",
        "sharpe": "sharpe",
        "exposure": "exposure",
        "turnover": "turnover",
    }
    for row in rows:
        for suffix, control_id in (("C1", "CONTROL-C1"), ("C5", "CONTROL-C5")):
            for delta_name, metric_name in delta_metrics.items():
                row[f"{delta_name}_delta_vs_{suffix}"] = (
                    _difference(row[metric_name], controls[control_id][metric_name])
                    if row["capacity_mode"] == "ADAPTIVE_C1_C5"
                    else None
                )
    return rows


def run_f_regime_capacity_research(
    database: Database,
    config: StrategyConfig,
    requested_start: date,
    requested_end: date,
) -> FRegimeCapacityResearchBundle:
    """Run the fixed local-only family and canonical full-portfolio cost reruns."""

    _validate_research_config(config, requested_start, requested_end)
    preparation = prepare_strategy_comparison(
        database,
        config,
        requested_start,
        requested_end,
        comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F,
    )
    spy_bars = database.bars_available_as_of("SPY", requested_end)
    schedules = {
        identity.research_id: MarketRegimeCapacitySchedule(spy_bars, identity.rule)
        for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS
    }

    results, traces = _run_family(
        database,
        config,
        preparation.screen_source,
        schedules,
        requested_start,
        requested_end,
    )
    cost_results: dict[str, dict[str, BacktestResult]] = {"BASELINE": results}
    for case, slippage_bps, commission_bps in CANONICAL_COST_STRESS_CASES:
        cost_config = config.model_copy(
            update={
                "backtest": config.backtest.model_copy(
                    update={
                        "slippage_bps": slippage_bps,
                        "commission_bps": commission_bps,
                    }
                )
            }
        )
        cost_results[case], _ = _run_family(
            database,
            cost_config,
            preparation.screen_source,
            schedules,
            requested_start,
            requested_end,
        )
    cost_rows = _regime_cost_rows(cost_results)

    diagnostics: list[dict[str, Any]] = []
    regime_summaries: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    chronological_rows: list[dict[str, Any]] = []
    entry_rank_rows: list[dict[str, Any]] = []
    concentration_rows: list[dict[str, Any]] = []
    concentration: dict[str, Any] = {}
    time_stability: dict[str, Any] = {}
    for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
        result = results[identity.research_id]
        schedule = schedules[identity.research_id]
        identity_diagnostics = regime_diagnostic_rows(
            identity, result, schedule, traces[identity.research_id]
        )
        diagnostics.extend(identity_diagnostics)
        if identity.adaptive:
            regime_summaries.append(
                regime_summary_row(identity, result, schedule, identity_diagnostics)
            )
        entry_rank_rows.extend(regime_entry_rank_analysis_rows(identity, result, schedule))

        comparison = _single_result_comparison(result)
        identity_monthly, monthly_summary = calendar_stability(comparison, "month")
        identity_yearly, yearly_summary = calendar_stability(comparison, "year")
        identity_subperiods = chronological_subperiod_analysis(comparison)
        _, identity_concentration, _ = symbol_and_leave_one_out(comparison)
        _tag_rows(identity_monthly, identity)
        _tag_rows(identity_yearly, identity)
        _tag_rows(identity_subperiods, identity)
        monthly_rows.extend(identity_monthly)
        yearly_rows.extend(identity_yearly)
        chronological_rows.extend(identity_subperiods)
        time_stability[identity.label] = {
            "monthly": monthly_summary[FROZEN_CHAMPION_F.label],
            "yearly": yearly_summary[FROZEN_CHAMPION_F.label],
        }
        summary = identity_concentration[FROZEN_CHAMPION_F.label]
        concentration[identity.label] = summary
        concentration_rows.append(_concentration_row(identity, summary))

    return FRegimeCapacityResearchBundle(
        requested_start=requested_start,
        requested_end=requested_end,
        results=results,
        schedules=schedules,
        traces=traces,
        metric_rows=regime_metric_rows(results, schedules),
        regime_diagnostic_rows=diagnostics,
        regime_summary_rows=regime_summaries,
        monthly_rows=monthly_rows,
        yearly_rows=yearly_rows,
        chronological_subperiod_rows=chronological_rows,
        entry_rank_rows=entry_rank_rows,
        cost_rows=cost_rows,
        symbol_concentration_rows=concentration_rows,
        concentration=concentration,
        time_stability=time_stability,
        universe_provenance=audit_universe_provenance(database, requested_start, requested_end),
        period_classification=(
            "DEVELOPMENT_OVERLAP"
            if requested_start <= FROZEN_CHAMPION_F.development_cutoff
            else "POST_DEVELOPMENT_RESEARCH_NOT_CHAMPION_OOS"
        ),
    )


def export_f_regime_capacity_research(
    bundle: FRegimeCapacityResearchBundle,
    output_directory: Path,
    *,
    stem: str,
) -> dict[str, Path]:
    """Export the required non-overwriting research artifacts."""

    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": output_directory / f"{stem}_summary.json",
        "summary_csv": output_directory / f"{stem}_summary.csv",
        "metrics": output_directory / f"{stem}_metrics.csv",
        "regime_diagnostics": output_directory / f"{stem}_regime_diagnostics.csv",
        "regime_summary": output_directory / f"{stem}_regime_summary.csv",
        "monthly": output_directory / f"{stem}_monthly.csv",
        "yearly": output_directory / f"{stem}_yearly.csv",
        "chronological_subperiods": output_directory / f"{stem}_chronological_subperiods.csv",
        "entry_rank_analysis": output_directory / f"{stem}_entry_rank_analysis.csv",
        "cost_stress": output_directory / f"{stem}_cost_stress.csv",
        "symbol_concentration": output_directory / f"{stem}_symbol_concentration.csv",
        "positions": output_directory / f"{stem}_positions.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"F regime capacity research export already exists: {existing[0]}")

    payload = {
        "report_type": "f_configured_point_in_time_regime_capacity_research",
        "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
        "research_status": F_REGIME_CAPACITY_RESEARCH_STATUS,
        "automatic_winner_selection": False,
        "frozen_champion_unchanged": True,
        "frozen_champion": {
            "strategy": FROZEN_CHAMPION_F.label,
            "max_positions": 1,
            "unchanged": True,
        },
        "requested_start": bundle.requested_start.isoformat(),
        "requested_end": bundle.requested_end.isoformat(),
        "period_classification": bundle.period_classification,
        "regime_variants": {
            "REGIME_SMA200": {
                "label": "F-regime-SPY-SMA200-C1-C5",
                "risk_on_rule": "SPY close > SMA200",
                "risk_on_capacity": 5,
                "risk_off_capacity": 1,
            },
            "REGIME_SMA200_MOM126": {
                "label": "F-regime-SPY-SMA200-MOM126-C1-C5",
                "risk_on_rule": "SPY close > SMA200 AND momentum126 > 0",
                "momentum_definition": "close.pct_change(periods=126, fill_method=None)",
                "risk_on_capacity": 5,
                "risk_off_capacity": 1,
            },
        },
        "controls": {
            "CONTROL_C1": {"label": "F-regime-control-C1", "target_capacity": 1},
            "CONTROL_C5": {"label": "F-regime-control-C5", "target_capacity": 5},
        },
        "point_in_time_semantics": (
            "A screen on session T uses only the completed local SPY Daily bar history through T; "
            "the resulting capacity gates orders for T+1. Missing bar/warmup is UNAVAILABLE/C1."
        ),
        "capacity_transition_semantics": (
            "target_capacity gates new entries only. A C5-to-C1 transition never liquidates an "
            "existing position; configured Daily management remains solely responsible for exits."
        ),
        "regime_diagnostics_semantics": (
            "session is signal session T. open_positions_at_signal_selection and available_slots "
            "describe the close-T allocation decision; execution_session, "
            "open_positions_before_entries, open_positions_after_entries, and entries_opened "
            "describe its T+1 open execution. The final liquidation-only session has "
            "entry_selection_performed=false and null entry-state fields."
        ),
        "return_attribution_method": SESSION_RETURN_ATTRIBUTION,
        "invariants": {
            "strategy": FROZEN_CHAMPION_F.label,
            "screen": "unchanged",
            "candidate_ranking": "unchanged",
            "entry_rules": "unchanged",
            "configured_daily_management": "unchanged",
            "risk_per_trade": "unchanged",
            "max_position_pct": "unchanged",
            "max_sector_positions": "unchanged",
            "regime_layer_can_exit": False,
            "backtest_network_access": False,
            "cost_stress_methodology": "FULL_PORTFOLIO_RERUN",
        },
        "metrics": bundle.metric_rows,
        "regime_summary": bundle.regime_summary_rows,
        "time_stability": bundle.time_stability,
        "symbol_concentration": bundle.concentration,
        "cost_stress": {
            "methodology": "FULL_PORTFOLIO_RERUN",
            "scenarios": bundle.cost_rows,
        },
        "survivorship_status": bundle.universe_provenance["survivorship_status"],
        "universe_provenance_status": bundle.universe_provenance["universe_provenance"],
        "universe_provenance": bundle.universe_provenance,
        "warnings": list(
            dict.fromkeys(
                warning for result in bundle.results.values() for warning in result.warnings
            )
        ),
        "methodology_note": (
            "Historical research hypothesis only. The report compares registered variants and "
            "does not select or promote a winner."
        ),
    }
    _atomic_text(paths["summary_json"], json.dumps(payload, indent=2))
    summary_fields = [
        "strategy",
        "research_id",
        "capacity_mode",
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe",
        "sortino",
        "position_profit_factor",
        "expectancy",
        "win_rate",
        "positions",
        "execution_legs",
        "average_holding_period",
        "exposure",
        "end_of_day_exposure",
        "turnover",
        "slippage_cost",
        "modeled_slippage_cost",
        "commission_cost",
        "modeled_commission_cost",
        "unique_traded_symbols",
        "average_simultaneous_positions",
        "median_simultaneous_positions",
        "maximum_simultaneous_positions",
        "sessions_at_effective_capacity",
        "return_delta_vs_C1",
        "return_delta_vs_C5",
        "max_drawdown_delta_vs_C1",
        "max_drawdown_delta_vs_C5",
        "sharpe_delta_vs_C1",
        "sharpe_delta_vs_C5",
        "exposure_delta_vs_C1",
        "exposure_delta_vs_C5",
        "turnover_delta_vs_C1",
        "turnover_delta_vs_C5",
    ]
    _atomic_csv(
        paths["summary_csv"],
        [{field: row.get(field) for field in summary_fields} for row in bundle.metric_rows],
        summary_fields,
    )
    _atomic_csv(paths["metrics"], bundle.metric_rows, _field_union(bundle.metric_rows))
    _atomic_csv(
        paths["regime_diagnostics"],
        bundle.regime_diagnostic_rows,
        _field_union(bundle.regime_diagnostic_rows),
    )
    _atomic_csv(
        paths["regime_summary"],
        bundle.regime_summary_rows,
        _field_union(bundle.regime_summary_rows),
    )
    _atomic_csv(paths["monthly"], bundle.monthly_rows, _field_union(bundle.monthly_rows))
    _atomic_csv(paths["yearly"], bundle.yearly_rows, _field_union(bundle.yearly_rows))
    _atomic_csv(
        paths["chronological_subperiods"],
        bundle.chronological_subperiod_rows,
        _field_union(bundle.chronological_subperiod_rows),
    )
    _atomic_csv(
        paths["entry_rank_analysis"],
        bundle.entry_rank_rows,
        _field_union(bundle.entry_rank_rows),
    )
    _atomic_csv(paths["cost_stress"], bundle.cost_rows, _field_union(bundle.cost_rows))
    _atomic_csv(
        paths["symbol_concentration"],
        bundle.symbol_concentration_rows,
        _field_union(bundle.symbol_concentration_rows),
    )
    position_rows = _tagged_model_rows(bundle, "positions")
    trade_rows = _tagged_model_rows(bundle, "trades")
    _atomic_csv(
        paths["positions"],
        position_rows,
        [
            "strategy",
            "regime_at_entry",
            "target_capacity_at_entry_signal",
            *BacktestPosition.model_fields,
        ],
    )
    _atomic_csv(
        paths["execution_legs"],
        trade_rows,
        [
            "strategy",
            "regime_at_entry",
            "target_capacity_at_entry_signal",
            *BacktestTrade.model_fields,
        ],
    )
    return paths


def format_f_regime_capacity_research_summary(bundle: FRegimeCapacityResearchBundle) -> str:
    """Format a compact handoff without interpreting a historical winner."""

    lines = [
        "F/configured PIT regime-capacity research complete (historical hypothesis only).",
        f"Period classification: {bundle.period_classification}.",
        "Strategy                                  Return     MaxDD      Exposure",
    ]
    for row in bundle.metric_rows:
        lines.append(
            f"{row['strategy']:<40} "
            f"{_format_percent(row['total_return']):>9} "
            f"{_format_percent(row['max_drawdown']):>9} "
            f"{_format_percent(row['exposure']):>9}"
        )
    lines.append("No winner was selected; F/configured max_positions=1 remains frozen.")
    return "\n".join(lines)


def _run_family(
    database: Database,
    config: StrategyConfig,
    screen_source: Any,
    schedules: dict[str, MarketRegimeCapacitySchedule],
    start: date,
    end: date,
) -> tuple[dict[str, BacktestResult], dict[str, tuple[EntryCapacityTrace, ...]]]:
    results: dict[str, BacktestResult] = {}
    traces: dict[str, tuple[EntryCapacityTrace, ...]] = {}
    for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
        variant_config = regime_strategy_config(config, identity)
        engine = BacktestEngine(
            database,
            variant_config,
            screen_source=screen_source,
            entry_capacity_provider=schedules[identity.research_id],
        )
        result = engine.run(
            start,
            end,
            variant=FROZEN_CHAMPION_F.variant,
            preset=FROZEN_CHAMPION_F.preset,
        )
        if (
            result.strategy_variant is not FROZEN_CHAMPION_F.variant
            or result.position_management_preset is not FROZEN_CHAMPION_F.preset
            or result.configuration["portfolio"]["max_positions"]
            != identity.configured_hard_max_positions
        ):
            raise ValueError(f"{identity.label} did not preserve its registered identity")
        results[identity.research_id] = result
        traces[identity.research_id] = engine.entry_capacity_traces
    return results, traces


def _regime_cost_rows(
    cost_results: dict[str, dict[str, BacktestResult]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, results in cost_results.items():
        _require_complete_variant_set(results)
        for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
            row = _cost_row(case, identity.label, results[identity.research_id])
            row.update(
                {
                    "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
                    "research_id": identity.research_id,
                    "capacity_mode": ("ADAPTIVE_C1_C5" if identity.adaptive else "STATIC_CONTROL"),
                }
            )
            rows.append(row)
    return rows


def _single_result_comparison(result: BacktestResult) -> StrategyComparison:
    return StrategyComparison(
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        actual_start=result.actual_start,
        actual_end=result.actual_end,
        generated_at=result.generated_at,
        variants=(result,),
        shared_screen_sessions=len(result.equity_curve),
        comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F,
        warnings=result.warnings,
    )


def _tag_rows(rows: list[dict[str, Any]], identity: FRegimeCapacityResearchVariant) -> None:
    for row in rows:
        row.update(
            {
                "strategy": identity.label,
                "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
                "research_id": identity.research_id,
                "capacity_mode": ("ADAPTIVE_C1_C5" if identity.adaptive else "STATIC_CONTROL"),
            }
        )


def _concentration_row(
    identity: FRegimeCapacityResearchVariant,
    summary: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "research_family": F_REGIME_CAPACITY_RESEARCH_FAMILY,
        "strategy": identity.label,
        "best_contributor": summary.get("best_contributing_symbol"),
        "worst_contributor": summary.get("worst_contributing_symbol"),
        "top_1_concentration": summary.get("top_1_pnl_concentration"),
        "top_3_concentration": summary.get("top_3_pnl_concentration"),
        "top_5_concentration": summary.get("top_5_pnl_concentration"),
        "post_hoc_only": True,
    }
    for prefix, key in (
        ("without_best", "without_best_contributor"),
        ("without_top_two_positive", "without_top_two_positive_contributors"),
    ):
        for field, value in summary.get(key, {}).items():
            row[f"{prefix}_{field}"] = value
    return row


def _tagged_model_rows(
    bundle: FRegimeCapacityResearchBundle,
    attribute: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for identity in F_REGIME_CAPACITY_RESEARCH_VARIANTS:
        schedule = bundle.schedules[identity.research_id]
        for item in getattr(bundle.results[identity.research_id], attribute):
            decision = schedule.decision(item.signal_date)
            rows.append(
                {
                    "strategy": identity.label,
                    "regime_at_entry": decision.regime.value,
                    "target_capacity_at_entry_signal": decision.target_capacity,
                    **item.model_dump(mode="json"),
                }
            )
    return rows


def _validate_research_config(
    config: StrategyConfig, requested_start: date, requested_end: date
) -> None:
    if requested_start > requested_end:
        raise ValueError("F regime capacity research start must not be after end")
    if config.portfolio.max_positions != 1:
        raise ValueError("F regime capacity research requires frozen champion max_positions=1")
    if config.portfolio.max_position_pct != 1.0 or config.risk.risk_per_trade != 0.01:
        raise ValueError(
            "F regime capacity research requires frozen max_position_pct=1.0 and "
            "risk_per_trade=0.01"
        )
    if config.backtest.slippage_bps != 5 or config.backtest.commission_bps != 0:
        raise ValueError("F regime capacity research requires the canonical 5 bps / 0 bps baseline")


def _require_complete_variant_set(results: dict[str, BacktestResult]) -> None:
    expected = [item.research_id for item in F_REGIME_CAPACITY_RESEARCH_VARIANTS]
    if len(results) != len(expected) or set(results) != set(expected):
        raise ValueError(f"F regime capacity results must contain exactly {expected}")


def _compound(returns: list[float]) -> float | None:
    if not returns:
        return None
    wealth = 1.0
    for value in returns:
        wealth *= 1 + value
    return wealth - 1


def _drawdown_from_session_returns(returns: list[float]) -> float | None:
    if not returns:
        return None
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    return drawdown


def _mean_or_none(values: list[float] | list[int]) -> float | None:
    return mean(values) if values else None


def _profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    return gross_profit / gross_loss if gross_loss else None


def _difference(value: float | int | None, baseline: float | int | None) -> float | int | None:
    if value is None or baseline is None:
        return None
    return value - baseline


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"
