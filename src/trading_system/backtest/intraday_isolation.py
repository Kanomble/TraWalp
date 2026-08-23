"""Phase-F intraday isolation diagnostics and non-overwriting research exports."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from trading_system.backtest.coverage import data_qualification_classification
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

IN_SAMPLE_RESEARCH_NOTICE = (
    "This period has already informed hypothesis construction. "
    "Results are in-sample research evidence and must not be labeled OOS."
)


def intraday_isolation_label(preset: PositionManagementPreset) -> str:
    allowed = {
        PositionManagementPreset.INTRADAY_DYNAMIC,
        PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    }
    if preset not in allowed:
        raise ValueError(f"Not an intraday-isolation preset: {preset}")
    return research_strategy_label(StrategyVariant.FULL, preset)


def economic_support_classification(result: BacktestResult) -> str:
    checks = (
        result.metrics.total_return is not None and result.metrics.total_return > 0,
        result.position_metrics.position_profit_factor is not None
        and result.position_metrics.position_profit_factor > 1,
        result.metrics.expectancy_per_trade is not None
        and result.metrics.expectancy_per_trade > 0,
    )
    supported = sum(checks)
    if supported == len(checks):
        return "STRONG SUPPORT"
    if supported:
        return "MIXED SUPPORT"
    return "NOT SUPPORTED"


def annotate_intraday_isolation_coverage(
    database: Database,
    comparison: StrategyComparison,
) -> tuple[StrategyComparison, list[dict[str, Any]]]:
    variants: list[BacktestResult] = []
    coverage_rows: list[dict[str, Any]] = []
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
        label = intraday_isolation_label(result.position_management_preset)
        annotated, _ = annotate_trade_path_coverage(
            database, result, strategy_label=label
        )
        events = annotated.research_diagnostics.get("candidate_events", [])
        required_events = [
            event for event in events if event.get("entry_opportunity_required") is True
        ]
        entry_present = sum(event.get("entry_bar_present") is True for event in required_events)
        entry_missing = len(required_events) - entry_present
        complete_paths = sum(
            position.trade_path_complete is True for position in annotated.positions
        )
        incomplete_paths = len(annotated.positions) - complete_paths
        warmup_sufficient = sum(
            position.warmup_sufficient is True for position in annotated.positions
        )
        position_insufficient_warmups = len(annotated.positions) - warmup_sufficient
        unresolved_candidate_warmups = sum(
            event.get("execution_failure_reason") == "insufficient_intraday_warmup"
            for event in required_events
        )
        qualification_insufficient_warmups = (
            position_insufficient_warmups + unresolved_candidate_warmups
        )
        coverage_rows.append(
            {
                "strategy": label,
                "full_session_strict_qualified": full_session,
                "candidate_entry_opportunity_qualified": entry_missing == 0,
                "executed_trade_path_qualified": incomplete_paths == 0,
                "indicator_warmup_qualified": qualification_insufficient_warmups == 0,
                "candidate_sessions": len(required_events),
                "entry_bar_present": entry_present,
                "entry_bar_missing": entry_missing,
                "percentage_entry_qualified": (
                    entry_present / len(required_events) if required_events else None
                ),
                "positions": len(annotated.positions),
                "complete_positions": complete_paths,
                "incomplete_positions": incomplete_paths,
                "missing_timestamps_before_exit": sum(
                    position.trade_path_missing_bar_count or 0
                    for position in annotated.positions
                ),
                "warmup_sufficient_positions": warmup_sufficient,
                "warmup_insufficient_positions": position_insufficient_warmups,
                "candidate_warmup_unresolved": unresolved_candidate_warmups,
                "economic_support_classification": economic_support_classification(
                    annotated
                ),
                "data_qualification_classification": data_qualification_classification(
                    candidate_sessions=len(required_events),
                    entry_bar_missing=entry_missing,
                    incomplete_trade_paths=incomplete_paths,
                    insufficient_warmups=qualification_insufficient_warmups,
                ),
            }
        )
        variants.append(annotated)
    qualification = dict(comparison.data_qualification)
    qualification["intraday_isolation"] = {
        "in_sample_research_notice": IN_SAMPLE_RESEARCH_NOTICE,
        "strategies": coverage_rows,
    }
    return comparison.model_copy(
        update={"variants": tuple(variants), "data_qualification": qualification}
    ), coverage_rows


def paired_intraday_isolation_effects(comparison: StrategyComparison) -> dict[str, Any]:
    results = {
        intraday_isolation_label(result.position_management_preset): result
        for result in comparison.variants
    }
    f0 = results["F0/C-intraday-dynamic"]
    return {
        "F1/C-intraday-loss-cooldown": _paired_f1(
            f0, results["F1/C-intraday-loss-cooldown"]
        ),
        "F2/C-intraday-opening-survivor-gate": _paired_f2(
            f0, results["F2/C-intraday-opening-survivor-gate"]
        ),
    }


def _position_key(position: BacktestPosition) -> tuple[str, date, datetime | None]:
    return position.symbol, position.signal_date, position.entry_timestamp


def _paired_f1(f0: BacktestResult, f1: BacktestResult) -> dict[str, Any]:
    blocked_events = [
        event
        for event in f1.research_diagnostics.get("candidate_events", [])
        if event.get("cooldown_blocked") is True
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
    total = sum(position.net_pnl for position in f1.positions) - sum(
        position.net_pnl for position in f0.positions
    )
    f0_keys = {_position_key(position) for position in f0.positions}
    f1_keys = {_position_key(position) for position in f1.positions}
    return {
        "baseline_trades_blocked": len(blocked),
        "cooldown_block_events": len(blocked_events),
        "blocked_baseline_trade_pnl": sum(position.net_pnl for position in blocked),
        "blocked_winners": sum(position.net_pnl > 0 for position in blocked),
        "blocked_losers": sum(position.net_pnl < 0 for position in blocked),
        "direct_cooldown_effect": direct,
        "subsequent_portfolio_path_effect": total - direct,
        "subsequent_portfolio_path_differences": len(f0_keys ^ f1_keys),
        "total_pnl_difference": total,
        "blocked_positions": [position.position_id for position in blocked],
    }


def _paired_f2(f0: BacktestResult, f2: BacktestResult) -> dict[str, Any]:
    baseline = {_position_key(position): position for position in f0.positions}
    variant = {_position_key(position): position for position in f2.positions}
    common = sorted(set(baseline) & set(variant), key=str)
    affected = [key for key in common if variant[key].exit_reason == "opening_bar_fail"]
    direct = sum(variant[key].net_pnl - baseline[key].net_pnl for key in affected)
    total = sum(position.net_pnl for position in f2.positions) - sum(
        position.net_pnl for position in f0.positions
    )
    unchanged = sum(
        baseline[key].exit_reason == variant[key].exit_reason
        and baseline[key].exit_timestamp == variant[key].exit_timestamp
        and abs(baseline[key].net_pnl - variant[key].net_pnl) <= 1e-9
        for key in common
    )
    return {
        "paired_positions": len(common),
        "positions_unchanged": unchanged,
        "opening_bar_fail_positions": len(affected),
        "baseline_return_of_affected_positions": sum(
            baseline[key].position_return for key in affected
        ),
        "f2_return_of_affected_positions": sum(
            variant[key].position_return for key in affected
        ),
        "direct_exit_pnl_effect": direct,
        "future_portfolio_path_effect": total - direct,
        "future_portfolio_path_differences": len(set(baseline) ^ set(variant)),
        "total_pnl_difference": total,
        "affected_positions": [variant[key].position_id for key in affected],
    }


def intraday_isolation_summary_rows(
    comparison: StrategyComparison,
    paired: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        label = intraday_isolation_label(result.position_management_preset)
        diagnostics = result.research_diagnostics
        partial_ids = {
            trade.position_id
            for trade in result.trades
            if trade.exit_reason == "partial_take_profit"
        }
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
            "modeled_costs": sum(
                trade.slippage + trade.transaction_cost for trade in result.trades
            ),
            "same_entry_bar_exits": diagnostics.get("same_entry_bar_final_exits", 0),
            "same_entry_bar_losses": diagnostics.get("same_entry_bar_losses", 0),
            "first_bar_survivors": diagnostics.get("first_bar_survivors", 0),
            "survivor_win_rate": diagnostics.get("survivor_win_rate"),
            "partial_targets": diagnostics.get("partial_target_count", 0),
            "runners": diagnostics.get("runner_positions", 0),
            "runner_pnl": sum(
                position.net_pnl
                for position in result.positions
                if position.position_id in partial_ids
            ),
            "cooldown_blocks": diagnostics.get("cooldown_blocks", 0),
            "opening_gate_evaluations": diagnostics.get("opening_gate_evaluations", 0),
            "green_survivors": diagnostics.get("opening_gate_green_survivors", 0),
            "non_green_survivors": diagnostics.get(
                "opening_gate_non_green_survivors", 0
            ),
            "opening_bar_fail_exits": diagnostics.get("opening_bar_fail_exits", 0),
        }
        row.update(paired.get(label, {}))
        rows.append(row)
    return rows


def isolation_cost_stress_rows(
    comparisons: dict[str, StrategyComparison],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, comparison in comparisons.items():
        for result in comparison.variants:
            rows.append(
                {
                    "cost_case": case,
                    "strategy": intraday_isolation_label(
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
                    "path_preserving_cost_stress": False,
                }
            )
    return rows


def isolation_path_preserving_cost_rows(
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
        path_hash = hashlib.sha256(
            json.dumps(path, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for case, slippage_bps, commission_bps in (
            ("2X_SLIPPAGE", 10.0, 0.0),
            ("3X_SLIPPAGE", 15.0, 0.0),
            ("COMMISSION_SENSITIVITY", 5.0, 5.0),
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
            rows.append(
                {
                    "cost_case": case,
                    "strategy": intraday_isolation_label(
                        result.position_management_preset
                    ),
                    "slippage_bps": slippage_bps,
                    "commission_bps": commission_bps,
                    "total_return": sum(pnls) / result.initial_capital,
                    "profit_factor": _profit_factor(pnls),
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


def export_intraday_isolation_comparison(
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
        "coverage": output_directory / f"{stem}_coverage.csv",
        "paired_effects": output_directory / f"{stem}_paired_effects.csv",
        "cost_stress": output_directory / f"{stem}_cost_stress.csv",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Intraday isolation export already exists: {existing[0]}")
    paired = paired_intraday_isolation_effects(comparison)
    summary_rows = intraday_isolation_summary_rows(comparison, paired)
    full_cost_rows = (
        isolation_cost_stress_rows(cost_comparisons) if cost_stress_requested else []
    )
    path_cost_rows = (
        isolation_path_preserving_cost_rows(comparison) if cost_stress_requested else []
    )
    summary_payload = {
        "report_type": "intraday_edge_isolation_research",
        "methodology_notice": IN_SAMPLE_RESEARCH_NOTICE,
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
        "methodology_notice": IN_SAMPLE_RESEARCH_NOTICE,
        "cost_stress_requested": cost_stress_requested,
        "cost_stress_executed": bool(cost_stress_requested and cost_comparisons),
        "strategies": comparison.research_diagnostics,
        "coverage": coverage_rows,
        "paired_effects": paired,
        "path_preserving_cost_stress": path_cost_rows,
    }
    _atomic_text(paths["summary_json"], json.dumps(summary_payload, indent=2))
    _atomic_csv(paths["summary_csv"], summary_rows, _field_union(summary_rows))
    _atomic_csv(
        paths["positions"],
        [
            {
                "strategy": intraday_isolation_label(
                    result.position_management_preset
                ),
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
                "strategy": intraday_isolation_label(
                    result.position_management_preset
                ),
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
    all_cost_rows = [*full_cost_rows, *path_cost_rows]
    _atomic_csv(paths["cost_stress"], all_cost_rows, _field_union(all_cost_rows))
    return paths


def _profit_factor(pnls: Sequence[float]) -> float | None:
    profit = sum(value for value in pnls if value > 0)
    loss = abs(sum(value for value in pnls if value < 0))
    return profit / loss if loss else None


def _field_union(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    return fields or ["strategy"]
