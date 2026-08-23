"""Phase-G F0/F3/F4 diagnostics and non-overwriting research exports."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
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
    "This period has already informed hypothesis construction. "
    "F3/F4 results are development research evidence, not out-of-sample evidence."
)

_NEXT_PRESETS = {
    PositionManagementPreset.INTRADAY_DYNAMIC,
    PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
    PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
}


def intraday_next_label(preset: PositionManagementPreset) -> str:
    if preset not in _NEXT_PRESETS:
        raise ValueError(f"Not an intraday-next preset: {preset}")
    return research_strategy_label(StrategyVariant.FULL, preset)


def annotate_intraday_next_coverage(
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
        label = intraday_next_label(result.position_management_preset)
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
            "percentage_entry_qualified": (
                entry_present / len(required) if required else None
            ),
            "positions": len(annotated.positions),
            "complete_positions": complete_paths,
            "incomplete_positions": incomplete_paths,
            "missing_timestamps_before_exit": sum(
                position.trade_path_missing_bar_count or 0
                for position in annotated.positions
            ),
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
    qualification["intraday_next"] = {
        "development_research_notice": DEVELOPMENT_RESEARCH_NOTICE,
        "strategies": rows,
    }
    return comparison.model_copy(
        update={"variants": tuple(variants), "data_qualification": qualification}
    ), rows


def paired_intraday_next_effects(comparison: StrategyComparison) -> dict[str, Any]:
    results = {
        intraday_next_label(result.position_management_preset): result
        for result in comparison.variants
    }
    f0 = results["F0/C-intraday-dynamic"]
    return {
        "F3/C-intraday-thesis-recovery": _paired_f3(
            f0, results["F3/C-intraday-thesis-recovery"]
        ),
        "F4/C-intraday-first-hour-pullback": _paired_f4(
            f0, results["F4/C-intraday-first-hour-pullback"]
        ),
    }


def _paired_f3(f0: BacktestResult, f3: BacktestResult) -> dict[str, Any]:
    blocked_events = [
        event
        for event in f3.research_diagnostics.get("candidate_events", [])
        if event.get("thesis_recovery_blocked") is True
    ]
    blocked_keys = {
        (event["symbol"], date.fromisoformat(event["signal_session"]))
        for event in blocked_events
    }
    blocked = [
        position
        for position in f0.positions
        if (position.symbol, position.signal_date) in blocked_keys
    ]
    direct = -sum(position.net_pnl for position in blocked)
    total = sum(position.net_pnl for position in f3.positions) - sum(
        position.net_pnl for position in f0.positions
    )
    f0_path = {
        (position.symbol, position.signal_date, position.entry_timestamp)
        for position in f0.positions
    }
    f3_path = {
        (position.symbol, position.signal_date, position.entry_timestamp)
        for position in f3.positions
    }
    return {
        "baseline_trades_blocked": len(blocked),
        "blocked_baseline_trade_pnl": sum(position.net_pnl for position in blocked),
        "blocked_baseline_winners": sum(position.net_pnl > 0 for position in blocked),
        "blocked_baseline_losers": sum(position.net_pnl < 0 for position in blocked),
        "direct_thesis_gate_effect": direct,
        "subsequent_portfolio_path_effect": total - direct,
        "subsequent_portfolio_path_differences": len(f0_path ^ f3_path),
        "total_pnl_difference": total,
        "blocked_position_ids": [position.position_id for position in blocked],
    }


def _paired_f4(f0: BacktestResult, f4: BacktestResult) -> dict[str, Any]:
    events = {
        (event["symbol"], date.fromisoformat(event["signal_session"])): event
        for event in f4.research_diagnostics.get("candidate_events", [])
    }
    partial_ids = {
        trade.position_id
        for trade in f0.trades
        if trade.exit_reason == "partial_take_profit"
    }
    f0_by_key = {
        (position.symbol, position.signal_date): position for position in f0.positions
    }
    candidate_comparisons = []
    for key, event in sorted(events.items()):
        baseline = f0_by_key.get(key)
        candidate_comparisons.append(
            {
                "symbol": key[0],
                "signal_session": key[1].isoformat(),
                "f0_entered": baseline is not None,
                "f0_position_return": (
                    baseline.position_return if baseline is not None else None
                ),
                "f0_hit_partial_target": (
                    baseline.position_id in partial_ids if baseline is not None else False
                ),
                "f0_runner": (
                    baseline.position_id in partial_ids if baseline is not None else False
                ),
                "f4_ema_passed": event.get("opening_above_ema") is True,
                "f4_found_pullback": event.get("pullback_confirmed") is True,
                "f4_entered": event.get("executed") is True,
            }
        )
    comparable = [
        position
        for position in f0.positions
        if (position.symbol, position.signal_date) in events
    ]
    skipped = [
        position
        for position in comparable
        if not events[(position.symbol, position.signal_date)].get("executed", False)
    ]
    return {
        "paired_candidate_positions": len(comparable),
        "f0_winners_skipped_by_f4": sum(position.net_pnl > 0 for position in skipped),
        "f0_losers_avoided_by_f4": sum(position.net_pnl < 0 for position in skipped),
        "f0_runner_trades_skipped_by_f4": sum(
            position.position_id in partial_ids for position in skipped
        ),
        "gross_pnl_of_skipped_f0_trades": sum(position.gross_pnl for position in skipped),
        "f4_total_pnl_difference": sum(position.net_pnl for position in f4.positions)
        - sum(position.net_pnl for position in f0.positions),
        "candidate_comparisons": candidate_comparisons,
    }


def intraday_next_summary_rows(
    comparison: StrategyComparison,
    paired: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        label = intraday_next_label(result.position_management_preset)
        holding = [_holding_minutes(position) for position in result.positions]
        monthly: dict[str, float] = defaultdict(float)
        for position in result.positions:
            monthly[position.exit_date.strftime("%Y-%m")] += position.net_pnl
        diagnostics = result.research_diagnostics
        row = {
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
            "average_holding_minutes": mean(holding) if holding else None,
            "median_holding_minutes": median(holding) if holding else None,
            "mean_mfe": result.position_metrics.average_mfe,
            "mean_mae": result.position_metrics.average_mae,
            "monthly_net_pnl_contribution": json.dumps(dict(sorted(monthly.items()))),
            "same_entry_bar_exits": diagnostics.get("same_entry_bar_final_exits", 0),
            "first_bar_survivors": diagnostics.get("first_bar_survivors", 0),
            "partial_targets": diagnostics.get("partial_target_count", 0),
            "runners": diagnostics.get("runner_positions", 0),
            "thesis_recovery_blocks": diagnostics.get("thesis_recovery_blocks", 0),
            "recovered_reentries": diagnostics.get("recovered_reentries", 0),
            "opening_ema_passes": diagnostics.get("f4_opening_ema_passes", 0),
            "c_candidates": diagnostics.get("f4_c_candidates", 0),
            "complete_first_hours": diagnostics.get("f4_complete_first_hours", 0),
            "pullback_candidates": diagnostics.get("f4_pullback_candidates", 0),
            "confirmed_pullbacks": diagnostics.get("f4_confirmed_pullbacks", 0),
            "executed_trades": diagnostics.get("f4_executed_trades", 0),
            "stop_exits": diagnostics.get("f4_stop_exits", 0),
            "confirmed_swing_high_exits": diagnostics.get(
                "f4_confirmed_swing_high_exits", 0
            ),
            "session_close_exits": diagnostics.get("f4_session_close_exits", 0),
            "average_entry_time_utc_minutes": diagnostics.get(
                "f4_average_entry_minutes_after_midnight_utc"
            ),
        }
        row.update(paired.get(label, {}))
        rows.append(row)
    return rows


def _holding_minutes(position: BacktestPosition) -> float:
    if position.entry_timestamp is not None and position.exit_timestamp is not None:
        return max(
            (position.exit_timestamp - position.entry_timestamp).total_seconds() / 60,
            0.0,
        )
    return float(position.holding_days * 390)


def intraday_next_cost_stress_rows(
    comparisons: dict[str, StrategyComparison],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, comparison in comparisons.items():
        for result in comparison.variants:
            rows.append(
                {
                    "cost_case": case,
                    "strategy": intraday_next_label(
                        result.position_management_preset
                    ),
                    "slippage_bps": result.configuration["backtest"]["slippage_bps"],
                    "commission_bps": result.configuration["backtest"][
                        "commission_bps"
                    ],
                    "total_return": result.metrics.total_return,
                    "profit_factor": result.position_metrics.position_profit_factor,
                    "expectancy": result.metrics.expectancy_per_trade,
                    "max_drawdown": result.metrics.maximum_drawdown,
                    "modeled_costs": sum(
                        trade.slippage + trade.transaction_cost
                        for trade in result.trades
                    ),
                    "turnover": result.metrics.portfolio_turnover,
                }
            )
    return rows


def intraday_next_path_preserving_cost_rows(
    comparison: StrategyComparison,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        legs: dict[str, list] = defaultdict(list)
        for trade in result.trades:
            legs[trade.position_id].append(trade)
        path = [
            (
                position.position_id,
                position.entry_timestamp.isoformat()
                if position.entry_timestamp is not None
                else None,
                tuple(
                    (
                        leg.execution_leg_id,
                        leg.exit_timestamp.isoformat()
                        if leg.exit_timestamp is not None
                        else None,
                        leg.exit_reason,
                        leg.quantity,
                    )
                    for leg in legs[position.position_id]
                ),
            )
            for position in result.positions
        ]
        path_hash = hashlib.sha256(
            json.dumps(path, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for case, slippage_bps, commission_bps in (
            ("2X_PATH_PRESERVING", 10.0, 0.0),
            ("3X_PATH_PRESERVING", 15.0, 0.0),
            ("COMMISSION_PATH_PRESERVING", 5.0, 5.0),
        ):
            pnls: list[float] = []
            total_costs = 0.0
            for position in result.positions:
                entry_fill = position.entry_reference_price * (1 + slippage_bps / 10_000)
                entry_commission = (
                    entry_fill
                    * position.initial_quantity
                    * commission_bps
                    / 10_000
                )
                pnl = -entry_fill * position.initial_quantity - entry_commission
                costs = (
                    (entry_fill - position.entry_reference_price)
                    * position.initial_quantity
                    + entry_commission
                )
                for leg in legs[position.position_id]:
                    exit_fill = leg.exit_reference_price * (1 - slippage_bps / 10_000)
                    exit_commission = exit_fill * leg.quantity * commission_bps / 10_000
                    pnl += exit_fill * leg.quantity - exit_commission
                    costs += (
                        (leg.exit_reference_price - exit_fill) * leg.quantity
                        + exit_commission
                    )
                pnls.append(pnl)
                total_costs += costs
            profits = sum(value for value in pnls if value > 0)
            losses = abs(sum(value for value in pnls if value < 0))
            rows.append(
                {
                    "cost_case": case,
                    "strategy": intraday_next_label(
                        result.position_management_preset
                    ),
                    "slippage_bps": slippage_bps,
                    "commission_bps": commission_bps,
                    "total_return": sum(pnls) / result.initial_capital,
                    "profit_factor": profits / losses if losses else None,
                    "expectancy": mean(pnls) if pnls else None,
                    "max_drawdown": None,
                    "modeled_costs": total_costs,
                    "turnover": result.metrics.portfolio_turnover,
                    "path_preserving_cost_stress": True,
                    "execution_path_unchanged": True,
                    "execution_path_hash": path_hash,
                }
            )
    return rows


def export_intraday_next_comparison(
    comparison: StrategyComparison,
    cost_comparisons: dict[str, StrategyComparison],
    coverage_rows: list[dict[str, Any]],
    output_directory: Path,
    *,
    stem: str,
    cost_stress_requested: bool = False,
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
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Intraday-next export already exists: {existing[0]}")
    paired = paired_intraday_next_effects(comparison)
    summary_rows = intraday_next_summary_rows(comparison, paired)
    payload = {
        "report_type": "intraday_next_development_research",
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
                "strategy": intraday_next_label(result.position_management_preset),
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
                "strategy": intraday_next_label(result.position_management_preset),
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
    cost_rows = (
        [
            *intraday_next_cost_stress_rows(cost_comparisons),
            *intraday_next_path_preserving_cost_rows(comparison),
        ]
        if cost_stress_requested
        else []
    )
    _atomic_csv(paths["cost_stress"], cost_rows, _field_union(cost_rows))
    return paths


def _field_union(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    return fields or ["strategy"]
