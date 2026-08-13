"""Atomic JSON/CSV exports and compact terminal summaries for Milestone 4."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from trading_system.models.backtest import (
    BacktestResult,
    BacktestTrade,
    PerformanceMetrics,
    StrategyComparison,
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
        "equity": output_directory / f"{stem}_equity.csv",
    }
    _atomic_text(paths["json"], json.dumps(result.model_dump(mode="json"), indent=2))
    trade_rows = [trade.model_dump(mode="json") for trade in result.trades]
    equity_rows = [point.model_dump(mode="json") for point in result.equity_curve]
    _atomic_csv(paths["trades"], trade_rows, _trade_fields())
    _atomic_csv(paths["equity"], equity_rows, _equity_fields())
    return paths


def export_comparison(comparison: StrategyComparison, output_directory: Path) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{comparison.comparison_kind}_comparison_"
        f"{comparison.requested_start}_{comparison.requested_end}"
    )
    paths = {"json": output_directory / f"{stem}.json", "csv": output_directory / f"{stem}.csv"}
    _atomic_text(paths["json"], json.dumps(comparison.model_dump(mode="json"), indent=2))
    rows = []
    for result in comparison.variants:
        metrics = result.metrics.model_dump(mode="json")
        label = (
            result.position_management_preset.value
            if comparison.comparison_kind == "position_management"
            else result.strategy_variant.value
        )
        rows.append({"strategy": label, **metrics})
    _atomic_csv(paths["csv"], rows, ["strategy", *PerformanceMetrics.model_fields])
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
        f"Sessions: {len(result.equity_curve)} | Trades: {metrics.number_of_trades}",
        f"Return: {_percent(metrics.total_return)} | CAGR: {_percent(metrics.cagr)}",
        f"Sharpe: {_number(metrics.sharpe_ratio)} | "
        f"Max drawdown: {_percent(metrics.maximum_drawdown)}",
        f"Win rate: {_percent(metrics.win_rate)} | Profit factor: {_number(metrics.profit_factor)}",
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
    header = "Strategy          Return    CAGR      Sharpe   MaxDD     WinRate   Trades  PF"
    rows = [header]
    for result in comparison.variants:
        metric = result.metrics
        label = (
            result.position_management_preset.value
            if comparison.comparison_kind == "position_management"
            else result.strategy_variant.value
        )
        rows.append(
            f"{label:<17} "
            f"{_percent(metric.total_return):<9} "
            f"{_percent(metric.cagr):<9} "
            f"{_number(metric.sharpe_ratio):<8} "
            f"{_percent(metric.maximum_drawdown):<9} "
            f"{_percent(metric.win_rate):<9} "
            f"{metric.number_of_trades:<7} "
            f"{_number(metric.profit_factor)}"
        )
    rows.append(f"Shared point-in-time screens: {comparison.shared_screen_sessions}")
    if comparison.warnings:
        rows.append(f"Warnings: {len(comparison.warnings)} (see JSON report)")
    return "\n".join(rows)


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
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _trade_fields() -> list[str]:
    return list(BacktestTrade.model_fields)


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
