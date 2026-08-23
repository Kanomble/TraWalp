"""Phase-H F0/F3/F5 diagnostics and non-overwriting research exports."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

from trading_system.backtest.coverage import data_qualification_classification
from trading_system.backtest.intraday_isolation import economic_support_classification
from trading_system.backtest.report import (
    _atomic_csv,
    _atomic_text,
    _position_fields,
    _trade_fields,
)
from trading_system.backtest.research_registry import research_strategy_label
from trading_system.backtest.validation import annotate_trade_path_coverage
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    PositionManagementPreset,
    StrategyComparison,
    StrategyVariant,
)

DEVELOPMENT_RESEARCH_NOTICE = (
    "2025-05-01 through 2026-08-12 has already informed hypothesis construction. "
    "F3/F5 results on that segment are development research evidence, not automatically "
    "out-of-sample evidence. Earlier data is labeled historical_extension."
)

_HYBRID_PRESETS = {
    PositionManagementPreset.INTRADAY_DYNAMIC,
    PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
    PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
}


def intraday_hybrid_label(preset: PositionManagementPreset) -> str:
    if preset not in _HYBRID_PRESETS:
        raise ValueError(f"Not an intraday-hybrid preset: {preset}")
    return research_strategy_label(StrategyVariant.FULL, preset)


def annotate_intraday_hybrid_coverage(
    database: Database,
    comparison: StrategyComparison,
) -> tuple[StrategyComparison, list[dict[str, Any]]]:
    variants: list[BacktestResult] = []
    rows: list[dict[str, Any]] = []
    structural = comparison.data_qualification.get("intraday", {}).get("15m", {})
    full_session = not any(
        structural.get(field, 0)
        for field in (
            "missing_sessions",
            "partial_sessions",
            "unknown_market_activity_sessions",
        )
    )
    for result in comparison.variants:
        label = intraday_hybrid_label(result.position_management_preset)
        annotated, _ = annotate_trade_path_coverage(
            database, result, strategy_label=label
        )
        events = annotated.research_diagnostics.get("candidate_events", [])
        required = [
            event for event in events if event.get("entry_opportunity_required") is True
        ]
        entry_present = sum(event.get("entry_bar_present") is True for event in required)
        entry_missing = len(required) - entry_present
        complete_paths = sum(
            position.trade_path_complete is True for position in annotated.positions
        )
        incomplete_paths = len(annotated.positions) - complete_paths
        sufficient_warmups = sum(
            position.warmup_sufficient is True for position in annotated.positions
        )
        insufficient_warmups = len(annotated.positions) - sufficient_warmups
        row = {
            "strategy": label,
            "full_session_strict_qualified": full_session,
            "candidate_entry_opportunity_qualified": entry_missing == 0,
            "executed_trade_path_qualified": incomplete_paths == 0,
            "indicator_warmup_qualified": insufficient_warmups == 0,
            "candidate_sessions": len(required),
            "entry_bar_present": entry_present,
            "entry_bar_missing": entry_missing,
            "positions": len(annotated.positions),
            "complete_positions": complete_paths,
            "incomplete_positions": incomplete_paths,
            "warmup_sufficient_positions": sufficient_warmups,
            "warmup_insufficient_positions": insufficient_warmups,
            "economic_support_classification": economic_support_classification(
                annotated
            ),
            "data_qualification_classification": data_qualification_classification(
                candidate_sessions=len(required),
                entry_bar_missing=entry_missing,
                incomplete_trade_paths=incomplete_paths,
                insufficient_warmups=insufficient_warmups,
            ),
        }
        rows.append(row)
        variants.append(annotated)
    qualification = dict(comparison.data_qualification)
    qualification["intraday_hybrid"] = {
        "methodology_notice": DEVELOPMENT_RESEARCH_NOTICE,
        "strategies": rows,
    }
    return comparison.model_copy(
        update={"variants": tuple(variants), "data_qualification": qualification}
    ), rows


def paired_intraday_hybrid_effects(comparison: StrategyComparison) -> dict[str, Any]:
    results = {
        intraday_hybrid_label(result.position_management_preset): result
        for result in comparison.variants
    }
    f0 = results["F0/C-intraday-dynamic"]
    f3 = results["F3/C-intraday-thesis-recovery"]
    f5 = results["F5/C-intraday-first-hour-pullback-f0-management"]
    return {
        "F3/C-intraday-thesis-recovery": _paired_f3(f0, f3),
        "F5/C-intraday-first-hour-pullback-f0-management": _paired_f5(f0, f5),
    }


def _paired_f3(f0: BacktestResult, f3: BacktestResult) -> dict[str, Any]:
    events = [
        event
        for event in f3.research_diagnostics.get("candidate_events", [])
        if event.get("thesis_recovery_blocked") is True
    ]
    keys = {(event["symbol"], date.fromisoformat(event["signal_session"])) for event in events}
    blocked = [
        position for position in f0.positions if (position.symbol, position.signal_date) in keys
    ]
    direct = -sum(position.net_pnl for position in blocked)
    total = sum(position.net_pnl for position in f3.positions) - sum(
        position.net_pnl for position in f0.positions
    )
    return {
        "baseline_trades_blocked": len(blocked),
        "blocked_baseline_trade_pnl": sum(position.net_pnl for position in blocked),
        "blocked_baseline_winners": sum(position.net_pnl > 0 for position in blocked),
        "blocked_baseline_losers": sum(position.net_pnl < 0 for position in blocked),
        "direct_thesis_gate_effect": direct,
        "subsequent_portfolio_path_effect": total - direct,
        "total_pnl_difference": total,
    }


def _paired_f5(f0: BacktestResult, f5: BacktestResult) -> dict[str, Any]:
    events = {
        (event["symbol"], date.fromisoformat(event["signal_session"])): event
        for event in f5.research_diagnostics.get("candidate_events", [])
    }
    f0_by_key = {
        (position.symbol, position.signal_date): position for position in f0.positions
    }
    partial_ids = {
        trade.position_id
        for trade in f0.trades
        if trade.exit_reason == "partial_take_profit"
    }
    comparable = [position for key, position in f0_by_key.items() if key in events]
    passed = [
        position
        for position in comparable
        if events[(position.symbol, position.signal_date)].get("executed") is True
    ]
    failed = [position for position in comparable if position not in passed]
    return {
        "daily_c_candidates_evaluated": len(events),
        "f5_ema_passes": sum(event.get("opening_above_ema") is True for event in events.values()),
        "f5_complete_first_hours": sum(
            event.get("first_hour_complete") is True for event in events.values()
        ),
        "f5_pullback_candidates": sum(
            int(event.get("pullback_candidate_count", 0)) for event in events.values()
        ),
        "f5_confirmed_pullbacks": sum(
            event.get("pullback_confirmed") is True for event in events.values()
        ),
        "f5_entries": sum(event.get("executed") is True for event in events.values()),
        "f0_entered_f5_skipped": len(failed),
        "f0_winners_skipped_by_f5": sum(position.net_pnl > 0 for position in failed),
        "f0_losers_skipped_by_f5": sum(position.net_pnl < 0 for position in failed),
        "f0_runners_skipped_by_f5": sum(
            position.position_id in partial_ids for position in failed
        ),
        "aggregate_net_pnl_of_f0_trades_skipped_by_f5": sum(
            position.net_pnl for position in failed
        ),
        "aggregate_gross_pnl_of_f0_trades_skipped_by_f5": sum(
            position.gross_pnl for position in failed
        ),
        "f0_where_f5_filter_would_pass": _position_split(passed),
        "f0_where_f5_filter_would_fail": _position_split(failed),
    }


def _position_split(positions: list[BacktestPosition]) -> dict[str, Any]:
    profits = sum(position.net_pnl for position in positions if position.net_pnl > 0)
    losses = abs(sum(position.net_pnl for position in positions if position.net_pnl < 0))
    return {
        "count": len(positions),
        "winners": sum(position.net_pnl > 0 for position in positions),
        "losers": sum(position.net_pnl < 0 for position in positions),
        "net_pnl": sum(position.net_pnl for position in positions),
        "gross_pnl": sum(position.gross_pnl for position in positions),
        "average_return": _average(position.position_return for position in positions),
        "profit_factor": profits / losses if losses else None,
        "average_mfe": _average(position.maximum_favorable_excursion for position in positions),
        "average_mae": _average(position.maximum_adverse_excursion for position in positions),
    }


def intraday_hybrid_summary_rows(
    comparison: StrategyComparison,
    paired: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        label = intraday_hybrid_label(result.position_management_preset)
        holdings = [_holding_minutes(position) for position in result.positions]
        mfes = [position.maximum_favorable_excursion for position in result.positions]
        maes = [position.maximum_adverse_excursion for position in result.positions]
        monthly: dict[str, float] = defaultdict(float)
        symbols: dict[str, float] = defaultdict(float)
        for position in result.positions:
            monthly[position.exit_date.strftime("%Y-%m")] += position.net_pnl
            symbols[position.symbol] += position.net_pnl
        ordered_symbols = sorted(symbols.items(), key=lambda item: (-item[1], item[0]))
        diagnostics = result.research_diagnostics
        rows.append(
            {
                "strategy": label,
                "total_return": result.metrics.total_return,
                "max_drawdown": result.metrics.maximum_drawdown,
                "profit_factor": result.position_metrics.position_profit_factor,
                "expectancy": result.metrics.expectancy_per_trade,
                "sharpe": result.metrics.sharpe_ratio,
                "sortino": result.metrics.sortino_ratio,
                "win_rate": result.position_metrics.position_win_rate,
                "positions": result.position_metrics.positions_closed,
                "execution_legs": result.execution_metrics.execution_legs,
                "turnover": result.metrics.portfolio_turnover,
                "modeled_execution_costs": sum(
                    trade.slippage + trade.transaction_cost for trade in result.trades
                ),
                "average_holding_minutes": _average(holdings),
                "median_holding_minutes": median(holdings) if holdings else None,
                "mean_mfe": _average(mfes),
                "median_mfe": median(mfes) if mfes else None,
                "mean_mae": _average(maes),
                "median_mae": median(maes) if maes else None,
                "monthly_net_pnl_contribution": json.dumps(dict(sorted(monthly.items()))),
                "symbol_net_pnl_contribution": json.dumps(dict(sorted(symbols.items()))),
                "top_1_symbol_pnl": ordered_symbols[0][1] if ordered_symbols else None,
                "partial_targets": diagnostics.get("partial_target_count", 0),
                "runners": diagnostics.get("runner_positions", 0),
                "thesis_recovery_blocks": diagnostics.get("thesis_recovery_blocks", 0),
                "recovered_reentries": diagnostics.get("recovered_reentries", 0),
                "f5_c_candidates": diagnostics.get("f5_c_candidates", 0),
                "f5_opening_ema_passes": diagnostics.get("f5_opening_ema_passes", 0),
                "f5_complete_first_hours": diagnostics.get("f5_complete_first_hours", 0),
                "f5_pullback_candidates": diagnostics.get("f5_pullback_candidates", 0),
                "f5_confirmed_pullbacks": diagnostics.get("f5_confirmed_pullbacks", 0),
                "f5_executed_trades": diagnostics.get("f5_executed_trades", 0),
                "f5_exit_reason_distribution": json.dumps(
                    dict(sorted(result.exits_by_reason.items()))
                ),
                "f5_average_entry_time_utc_minutes": diagnostics.get(
                    "f5_average_entry_minutes_after_midnight_utc"
                ),
                "paired_selection_diagnostics": json.dumps(paired.get(label, {})),
                "period_segments": json.dumps(_period_segments(result)),
            }
        )
    return rows


def _period_segments(result: BacktestResult) -> dict[str, Any]:
    boundary = date(2025, 5, 1)
    groups = {
        "full_requested_period": list(result.positions),
        "historical_extension": [
            position for position in result.positions if position.exit_date < boundary
        ],
        "development_reference": [
            position for position in result.positions if position.exit_date >= boundary
        ],
    }
    return {
        name: {
            "positions": len(items),
            "net_pnl": sum(position.net_pnl for position in items),
            "average_return": _average(position.position_return for position in items),
        }
        for name, items in groups.items()
    }


def export_intraday_hybrid_comparison(
    comparison: StrategyComparison,
    cost_comparisons: dict[str, StrategyComparison],
    coverage_rows: list[dict[str, Any]],
    output_directory: Path,
    *,
    stem: str,
    cost_stress_requested: bool,
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_csv": output_directory / f"{stem}_summary.csv",
        "summary_json": output_directory / f"{stem}_summary.json",
        "positions": output_directory / f"{stem}_positions.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
        "diagnostics": output_directory / f"{stem}_diagnostics.json",
        "paired_effects": output_directory / f"{stem}_paired_effects.csv",
        "coverage": output_directory / f"{stem}_coverage.csv",
        "cost_stress": output_directory / f"{stem}_cost_stress.csv",
        "exit_reasons": output_directory / f"{stem}_exit_reasons.csv",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Intraday-hybrid export already exists: {existing[0]}")
    paired = paired_intraday_hybrid_effects(comparison)
    summary_rows = intraday_hybrid_summary_rows(comparison, paired)
    exit_rows = [
        {
            "strategy": intraday_hybrid_label(result.position_management_preset),
            **diagnostic.model_dump(mode="json"),
        }
        for result in comparison.variants
        for diagnostic in result.profit_capture_by_exit_reason
    ]
    cost_rows = (
        _cost_rows(cost_comparisons)
        + _path_preserving_cost_rows(comparison, intraday_hybrid_label)
        if cost_stress_requested
        else []
    )
    payload = {
        "report_type": "intraday_hybrid_historical_research",
        "methodology_notice": DEVELOPMENT_RESEARCH_NOTICE,
        "automatic_strategy_promotion": False,
        "cost_stress_requested": cost_stress_requested,
        "cost_stress_executed": bool(cost_stress_requested and cost_comparisons),
        "requested_period": [
            comparison.requested_start.isoformat(),
            comparison.requested_end.isoformat(),
        ],
        "strategies": summary_rows,
        "coverage": coverage_rows,
        "paired_effects": paired,
        "exit_reason_diagnostics": exit_rows,
    }
    diagnostics = {
        "methodology_notice": DEVELOPMENT_RESEARCH_NOTICE,
        "cost_stress_requested": cost_stress_requested,
        "cost_stress_executed": bool(cost_stress_requested and cost_comparisons),
        "strategies": comparison.research_diagnostics,
        "coverage": coverage_rows,
        "paired_effects": paired,
    }
    _atomic_text(paths["summary_json"], json.dumps(payload, indent=2))
    _atomic_csv(paths["summary_csv"], summary_rows, _field_union(summary_rows))
    _atomic_csv(
        paths["positions"],
        [
            {
                "strategy": intraday_hybrid_label(result.position_management_preset),
                **position.model_dump(mode="json"),
            }
            for result in comparison.variants
            for position in result.positions
        ],
        ["strategy", *_position_fields()],
    )
    _atomic_csv(
        paths["execution_legs"],
        [
            {
                "strategy": intraday_hybrid_label(result.position_management_preset),
                **trade.model_dump(mode="json"),
            }
            for result in comparison.variants
            for trade in result.trades
        ],
        ["strategy", *_trade_fields()],
    )
    _atomic_text(paths["diagnostics"], json.dumps(diagnostics, indent=2))
    _atomic_csv(paths["coverage"], coverage_rows, _field_union(coverage_rows))
    paired_rows = [{"strategy": label, **values} for label, values in paired.items()]
    _atomic_csv(paths["paired_effects"], paired_rows, _field_union(paired_rows))
    _atomic_csv(paths["cost_stress"], cost_rows, _field_union(cost_rows))
    _atomic_csv(paths["exit_reasons"], exit_rows, _field_union(exit_rows))
    return paths


def _cost_rows(comparisons: dict[str, StrategyComparison]) -> list[dict[str, Any]]:
    return [
        {
            "cost_case": case,
            "strategy": intraday_hybrid_label(result.position_management_preset),
            "slippage_bps": result.configuration["backtest"]["slippage_bps"],
            "commission_bps": result.configuration["backtest"]["commission_bps"],
            "total_return": result.metrics.total_return,
            "profit_factor": result.position_metrics.position_profit_factor,
            "expectancy": result.metrics.expectancy_per_trade,
            "max_drawdown": result.metrics.maximum_drawdown,
            "modeled_costs": sum(
                trade.slippage + trade.transaction_cost for trade in result.trades
            ),
            "turnover": result.metrics.portfolio_turnover,
        }
        for case, comparison in comparisons.items()
        for result in comparison.variants
    ]


def _path_preserving_cost_rows(
    comparison: StrategyComparison,
    labeler: Callable[[PositionManagementPreset], str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        legs: dict[str, list] = defaultdict(list)
        for trade in result.trades:
            legs[trade.position_id].append(trade)
        path = [
            (
                position.position_id,
                position.entry_timestamp.isoformat() if position.entry_timestamp else None,
                tuple(
                    (
                        leg.execution_leg_id,
                        leg.exit_timestamp.isoformat() if leg.exit_timestamp else None,
                        leg.exit_reason,
                        leg.quantity,
                    )
                    for leg in legs[position.position_id]
                ),
            )
            for position in result.positions
        ]
        path_hash = hashlib.sha256(json.dumps(path, sort_keys=True).encode()).hexdigest()
        for case, slippage_bps, commission_bps in (
            ("2X_PATH_PRESERVING", 10.0, 0.0),
            ("3X_PATH_PRESERVING", 15.0, 0.0),
            ("COMMISSION_PATH_PRESERVING", 5.0, 5.0),
        ):
            pnls: list[float] = []
            costs = 0.0
            for position in result.positions:
                entry_fill = position.entry_reference_price * (1 + slippage_bps / 10_000)
                entry_commission = entry_fill * position.initial_quantity * commission_bps / 10_000
                pnl = -entry_fill * position.initial_quantity - entry_commission
                costs += (
                    (entry_fill - position.entry_reference_price) * position.initial_quantity
                    + entry_commission
                )
                for leg in legs[position.position_id]:
                    exit_fill = leg.exit_reference_price * (1 - slippage_bps / 10_000)
                    exit_commission = exit_fill * leg.quantity * commission_bps / 10_000
                    pnl += exit_fill * leg.quantity - exit_commission
                    costs += (leg.exit_reference_price - exit_fill) * leg.quantity + exit_commission
                pnls.append(pnl)
            profits = sum(value for value in pnls if value > 0)
            losses = abs(sum(value for value in pnls if value < 0))
            rows.append(
                {
                    "cost_case": case,
                    "strategy": labeler(result.position_management_preset),
                    "slippage_bps": slippage_bps,
                    "commission_bps": commission_bps,
                    "total_return": sum(pnls) / result.initial_capital,
                    "profit_factor": profits / losses if losses else None,
                    "expectancy": mean(pnls) if pnls else None,
                    "max_drawdown": None,
                    "modeled_costs": costs,
                    "turnover": result.metrics.portfolio_turnover,
                    "path_preserving_cost_stress": True,
                    "execution_path_unchanged": True,
                    "execution_path_hash": path_hash,
                }
            )
    return rows


def _holding_minutes(position: BacktestPosition) -> float:
    if position.entry_timestamp is not None and position.exit_timestamp is not None:
        return max((position.exit_timestamp - position.entry_timestamp).total_seconds() / 60, 0.0)
    return float(position.holding_days * 390)


def _average(values) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return sum(selected) / len(selected) if selected else None


def _field_union(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    return fields or ["strategy"]
