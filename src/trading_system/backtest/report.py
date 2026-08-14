"""Atomic JSON/CSV exports and compact terminal summaries for Milestone 4."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    ExecutionMetrics,
    PerformanceMetrics,
    PositionMetrics,
    StrategyComparison,
    StrategyComparisonKind,
)


def export_backtest(result: BacktestResult, output_directory: Path) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"backtest_{result.requested_start.isoformat()}_{result.requested_end.isoformat()}_"
        f"{result.strategy_variant.value}_{result.position_management_preset.value}"
    )
    paths = {
        "json": output_directory / f"{stem}.json",
        "trades": output_directory / f"{stem}_trades.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
        "positions": output_directory / f"{stem}_positions.csv",
        "post_exit": output_directory / f"{stem}_post_exit_analysis.csv",
        "equity": output_directory / f"{stem}_equity.csv",
    }
    _atomic_text(paths["json"], json.dumps(result.model_dump(mode="json"), indent=2))
    trade_rows = [trade.model_dump(mode="json") for trade in result.trades]
    position_rows = [position.model_dump(mode="json") for position in result.positions]
    post_exit_fields = _post_exit_fields()
    post_exit_rows = [
        {field: row[field] for field in post_exit_fields} for row in position_rows
    ]
    equity_rows = [point.model_dump(mode="json") for point in result.equity_curve]
    _atomic_csv(paths["trades"], trade_rows, _trade_fields())
    _atomic_csv(paths["execution_legs"], trade_rows, _trade_fields())
    _atomic_csv(paths["positions"], position_rows, _position_fields())
    _atomic_csv(paths["post_exit"], post_exit_rows, post_exit_fields)
    _atomic_csv(paths["equity"], equity_rows, _equity_fields())
    return paths


def export_comparison(comparison: StrategyComparison, output_directory: Path) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{comparison.comparison_kind}_comparison_"
        f"{comparison.requested_start}_{comparison.requested_end}"
    )
    paths = {
        "json": output_directory / f"{stem}.json",
        "csv": output_directory / f"{stem}.csv",
        "positions": output_directory / f"{stem}_positions.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
        "post_exit": output_directory / f"{stem}_post_exit_analysis.csv",
    }
    _atomic_text(paths["json"], json.dumps(comparison.model_dump(mode="json"), indent=2))
    rows = []
    position_rows = []
    leg_rows = []
    post_exit_rows = []
    for result in comparison.variants:
        metrics = result.metrics.model_dump(mode="json")
        position_metrics = result.position_metrics.model_dump(mode="json")
        execution_metrics = result.execution_metrics.model_dump(mode="json")
        label = _comparison_result_label(comparison, result)
        rows.append(
            {
                "strategy": label,
                "score_variant": result.strategy_variant.value,
                "position_management": result.position_management_preset.value,
                **metrics,
                **position_metrics,
                **execution_metrics,
            }
        )
        for position in result.positions:
            position_row = position.model_dump(mode="json")
            position_rows.append({"strategy": label, **position_row})
            post_exit_rows.append(
                {
                    "strategy": label,
                    **{
                        field: position_row[field]
                        for field in _post_exit_fields()
                    },
                }
            )
        leg_rows.extend(
            {"strategy": label, **trade.model_dump(mode="json")}
            for trade in result.trades
        )
    _atomic_csv(
        paths["csv"],
        rows,
        [
            "strategy",
            "score_variant",
            "position_management",
            *PerformanceMetrics.model_fields,
            *PositionMetrics.model_fields,
            *ExecutionMetrics.model_fields,
        ],
    )
    _atomic_csv(paths["positions"], position_rows, ["strategy", *_position_fields()])
    _atomic_csv(paths["execution_legs"], leg_rows, ["strategy", *_trade_fields()])
    _atomic_csv(
        paths["post_exit"],
        post_exit_rows,
        ["strategy", *_post_exit_fields()],
    )
    return paths


def format_backtest_summary(result: BacktestResult) -> str:
    metrics = result.metrics
    benchmark = (
        _percent(result.benchmark.total_return) if result.benchmark.available else "unavailable"
    )
    lines = [
        f"TraWalp backtest variant {result.strategy_variant.value} "
        f"/ position management {result.position_management_preset.value}",
        f"Period: {result.actual_start} through {result.actual_end}",
        f"Sessions: {len(result.equity_curve)} | Positions: "
        f"{result.position_metrics.positions_closed} | Execution legs: "
        f"{result.execution_metrics.execution_legs}",
        f"Return: {_percent(metrics.total_return)} | CAGR: {_percent(metrics.cagr)}",
        f"Sharpe: {_number(metrics.sharpe_ratio)} | "
        f"Max drawdown: {_percent(metrics.maximum_drawdown)}",
        f"Position win rate: {_percent(result.position_metrics.position_win_rate)} | "
        f"Position profit factor: {_number(result.position_metrics.position_profit_factor)}",
        f"Execution-leg win rate: "
        f"{_percent(result.execution_metrics.execution_leg_win_rate)} "
        f"(legacy win_rate={_percent(metrics.win_rate)})",
        f"SPY return: {benchmark}",
    ]
    if result.exits_by_reason:
        lines.append("Exit summary:")
        lines.extend(
            f"  {reason:<24} {count}" for reason, count in result.exits_by_reason.items()
        )
    if result.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)
    return "\n".join(lines)


def format_comparison_table(comparison: StrategyComparison) -> str:
    header = (
        "Strategy                 Return    MaxDD     Pos  PosWin   PosPF  "
        "AvgWin   AvgLoss  MFE     MAE      Capture  Giveback  NeverStop  "
        "ProfitStop  Post5d   PostMFE5"
    )
    rows = [header]
    for result in comparison.variants:
        metric = result.metrics
        position = result.position_metrics
        label = _comparison_result_label(comparison, result)
        rows.append(
            f"{label:<24} "
            f"{_percent(metric.total_return):<9} "
            f"{_percent(metric.maximum_drawdown):<9} "
            f"{position.positions_closed:<4} "
            f"{_percent(position.position_win_rate):<8} "
            f"{_number(position.position_profit_factor):<6} "
            f"{_percent(position.average_position_win):<8} "
            f"{_percent(position.average_position_loss):<8} "
            f"{_percent(position.average_mfe):<7} "
            f"{_percent(position.average_mae):<8} "
            f"{_percent(position.average_profit_capture):<8} "
            f"{_percent(position.average_profit_giveback):<9} "
            f"{_percent(position.never_profitable_stop_rate):<10} "
            f"{_percent(position.profitable_then_stopped_rate):<11} "
            f"{_percent(position.average_post_exit_return_5d):<8} "
            f"{_percent(position.average_post_exit_mfe_5d)}"
        )
    rows.append(f"Shared point-in-time screens: {comparison.shared_screen_sessions}")
    if comparison.skipped_strategies:
        rows.append("Skipped strategies:")
        rows.extend(
            f"  {strategy}: {reason}"
            for strategy, reason in comparison.skipped_strategies.items()
        )
    if comparison.warnings:
        rows.append(f"Warnings: {len(comparison.warnings)} (see JSON report)")
    return "\n".join(rows)


def _comparison_result_label(comparison: StrategyComparison, result: BacktestResult) -> str:
    if comparison.comparison_kind is StrategyComparisonKind.POSITION_MANAGEMENT:
        return result.position_management_preset.value
    if comparison.comparison_kind is StrategyComparisonKind.SCORE_VARIANTS:
        return result.strategy_variant.value
    return f"{result.strategy_variant.value}/{result.position_management_preset.value}"


def _atomic_text(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _atomic_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(_csv_row(row) for row in rows)
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _csv_row(row: dict) -> dict:
    return {
        key: json.dumps(value, separators=(",", ":"))
        if isinstance(value, (dict, list, tuple))
        else value
        for key, value in row.items()
    }


def _trade_fields() -> list[str]:
    return list(BacktestTrade.model_fields)


def _position_fields() -> list[str]:
    return list(BacktestPosition.model_fields)


def _post_exit_fields() -> list[str]:
    identity = ["position_id", "symbol", "exit_date", "exit_reason", "exit_reference_price"]
    forward = [
        field
        for field in BacktestPosition.model_fields
        if field.startswith("post_exit_")
    ]
    return [*identity, *forward]


def _equity_fields() -> list[str]:
    return [
        "date",
        "cash",
        "market_value",
        "portfolio_equity",
        "active_positions",
        "exposure",
        "session_exposure",
        "end_of_day_exposure",
        "realized_pnl",
        "unrealized_pnl",
    ]


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"
