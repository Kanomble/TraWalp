"""Atomic JSON/CSV exports and compact terminal summaries for Milestone 4."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

from trading_system.data.qualification import DataQualificationReport, QualificationDetail
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
from trading_system.models.candidate_audit import (
    CandidateAuditEvent,
    CandidateAuditMonthly,
    CandidateAuditResult,
    CandidateAuditSession,
    CandidateFailureSummary,
    CandidateNearMiss,
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


def export_comparison(
    comparison: StrategyComparison,
    output_directory: Path,
    *,
    stem: str | None = None,
    overwrite: bool = True,
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = stem or (
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
    if not overwrite:
        existing = [path for path in paths.values() if path.exists()]
        if existing:
            raise FileExistsError(f"Comparison export already exists: {existing[0]}")
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


def export_research_comparison(
    comparison: StrategyComparison,
    strict_comparison: StrategyComparison,
    cost_comparisons: dict[str, StrategyComparison],
    output_directory: Path,
    *,
    stem: str,
) -> dict[str, Path]:
    """Atomically export the D1-D5 baseline and its predeclared diagnostics."""

    if comparison.comparison_kind is not StrategyComparisonKind.RESEARCH_D1_D5:
        raise ValueError("Research export requires the research-d1-d5 comparison family")
    output_directory.mkdir(parents=True, exist_ok=True)
    research_paths = {
        "diagnostics": output_directory / f"{stem}_diagnostics.json",
        "monthly": output_directory / f"{stem}_monthly.csv",
        "symbol_concentration": output_directory / f"{stem}_symbol_concentration.csv",
        "cost_stress": output_directory / f"{stem}_cost_stress.csv",
        "strict_coverage": output_directory / f"{stem}_strict_coverage.csv",
    }
    base_paths = {
        "json": output_directory / f"{stem}.json",
        "csv": output_directory / f"{stem}.csv",
        "positions": output_directory / f"{stem}_positions.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
        "post_exit": output_directory / f"{stem}_post_exit_analysis.csv",
    }
    existing = [path for path in (*base_paths.values(), *research_paths.values()) if path.exists()]
    if existing:
        raise FileExistsError(f"Research comparison export already exists: {existing[0]}")

    paths = export_comparison(
        comparison, output_directory, stem=stem, overwrite=True
    )
    monthly_rows = _research_monthly_rows(comparison)
    symbol_rows, symbol_summary = _research_symbol_rows(comparison)
    cost_rows = _research_cost_rows(cost_comparisons)
    strict_rows = _strict_coverage_rows(strict_comparison)
    diagnostics = {
        "report_type": "d1_d5_strategy_research_diagnostics",
        "strict_coverage_sensitivity_ranked": False,
        "strategies": comparison.research_diagnostics,
        "symbol_concentration_summary": symbol_summary,
        "cost_scenarios": list(cost_comparisons),
        "strict_coverage_sensitivity": {
            "enabled": strict_comparison.strict_coverage_sensitivity,
            "strategies": strict_comparison.research_diagnostics,
        },
    }
    _atomic_text(research_paths["diagnostics"], json.dumps(diagnostics, indent=2))
    _atomic_csv(
        research_paths["monthly"],
        monthly_rows,
        [
            "strategy",
            "month",
            "positions",
            "return_contribution",
            "win_rate",
            "profit_factor",
            "average_position_return",
        ],
    )
    _atomic_csv(
        research_paths["symbol_concentration"],
        symbol_rows,
        [
            "strategy",
            "symbol",
            "positions",
            "total_net_pnl_contribution",
            "sum_position_returns",
            "wins",
            "losses",
            "same_bar_stopouts",
            "partial_exits",
        ],
    )
    _atomic_csv(
        research_paths["cost_stress"],
        cost_rows,
        [
            "cost_case",
            "strategy",
            "slippage_bps",
            "commission_bps",
            "total_return",
            "max_drawdown",
            "profit_factor",
            "expectancy",
            "turnover",
            "transaction_costs",
            "slippage_costs",
            "total_costs",
        ],
    )
    _atomic_csv(
        research_paths["strict_coverage"],
        strict_rows,
        [
            "strategy",
            "strict_coverage_sensitivity",
            "positions",
            "total_return",
            "max_drawdown",
            "profit_factor",
            "expectancy",
            "confirmation_pass_rate",
            "same_entry_bar_final_exits",
            "coverage_exclusions",
        ],
    )
    paths.update(research_paths)
    return paths


def _research_monthly_rows(comparison: StrategyComparison) -> list[dict]:
    rows: list[dict] = []
    for result in comparison.variants:
        by_month: dict[str, list[BacktestPosition]] = defaultdict(list)
        for position in result.positions:
            by_month[position.exit_date.strftime("%Y-%m")].append(position)
        label = _comparison_result_label(comparison, result)
        year = comparison.requested_start.year
        month_number = comparison.requested_start.month
        months: list[str] = []
        while (year, month_number) <= (
            comparison.requested_end.year,
            comparison.requested_end.month,
        ):
            months.append(f"{year:04d}-{month_number:02d}")
            month_number += 1
            if month_number == 13:
                year += 1
                month_number = 1
        for month in months:
            positions = by_month.get(month, [])
            wins = [position for position in positions if position.net_pnl > 0]
            losses = [position for position in positions if position.net_pnl < 0]
            gross_profit = sum(position.net_pnl for position in wins)
            gross_loss = abs(sum(position.net_pnl for position in losses))
            rows.append(
                {
                    "strategy": label,
                    "month": month,
                    "positions": len(positions),
                    "return_contribution": sum(
                        position.net_pnl for position in positions
                    )
                    / result.initial_capital,
                    "win_rate": len(wins) / len(positions) if positions else None,
                    "profit_factor": (
                        gross_profit / gross_loss if gross_loss > 0 else None
                    ),
                    "average_position_return": (
                        sum(position.position_return for position in positions)
                        / len(positions)
                        if positions
                        else None
                    ),
                }
            )
    return rows


def _research_symbol_rows(
    comparison: StrategyComparison,
) -> tuple[list[dict], dict[str, dict]]:
    rows: list[dict] = []
    summaries: dict[str, dict] = {}
    for result in comparison.variants:
        label = _comparison_result_label(comparison, result)
        by_symbol: dict[str, list[BacktestPosition]] = defaultdict(list)
        partials: dict[str, int] = defaultdict(int)
        for position in result.positions:
            by_symbol[position.symbol].append(position)
        for trade in result.trades:
            if trade.exit_reason == "partial_take_profit":
                partials[trade.symbol] += 1
        contributions: dict[str, float] = {}
        for symbol in sorted(by_symbol):
            positions = by_symbol[symbol]
            contribution = sum(position.net_pnl for position in positions)
            contributions[symbol] = contribution
            rows.append(
                {
                    "strategy": label,
                    "symbol": symbol,
                    "positions": len(positions),
                    "total_net_pnl_contribution": contribution,
                    "sum_position_returns": sum(
                        position.position_return for position in positions
                    ),
                    "wins": sum(position.net_pnl > 0 for position in positions),
                    "losses": sum(position.net_pnl < 0 for position in positions),
                    "same_bar_stopouts": sum(
                        position.entry_timestamp is not None
                        and position.exit_timestamp == position.entry_timestamp
                        and position.exit_reason
                        in {"stop_loss", "trailing_stop", "atr_trailing_stop", "profit_lock"}
                        for position in positions
                    ),
                    "partial_exits": partials[symbol],
                }
            )
        ranked = sorted(contributions.items(), key=lambda item: (-item[1], item[0]))
        total = sum(contributions.values())
        summaries[label] = {
            "best_contributing_symbol": ranked[0][0] if ranked else None,
            "worst_contributing_symbol": ranked[-1][0] if ranked else None,
            "top_1_pnl_share": ranked[0][1] / total if ranked and total else None,
            "top_3_pnl_share": (
                sum(value for _, value in ranked[:3]) / total if total else None
            ),
        }
    return rows, summaries


def _research_cost_rows(
    comparisons: dict[str, StrategyComparison],
) -> list[dict]:
    rows: list[dict] = []
    for cost_case, comparison in comparisons.items():
        for result in comparison.variants:
            config = result.configuration["backtest"]
            transaction_costs = sum(trade.transaction_cost for trade in result.trades)
            slippage_costs = sum(trade.slippage for trade in result.trades)
            rows.append(
                {
                    "cost_case": cost_case,
                    "strategy": _comparison_result_label(comparison, result),
                    "slippage_bps": config["slippage_bps"],
                    "commission_bps": config["commission_bps"],
                    "total_return": result.metrics.total_return,
                    "max_drawdown": result.metrics.maximum_drawdown,
                    "profit_factor": result.position_metrics.position_profit_factor,
                    "expectancy": result.metrics.expectancy_per_trade,
                    "turnover": result.metrics.portfolio_turnover,
                    "transaction_costs": transaction_costs,
                    "slippage_costs": slippage_costs,
                    "total_costs": transaction_costs + slippage_costs,
                }
            )
    return rows


def _strict_coverage_rows(comparison: StrategyComparison) -> list[dict]:
    selected = {
        "D3/C-intraday-trail-guard",
        "D4/C-intraday-confirmed-entry",
        "D5/C-hybrid-confirmed-swing",
    }
    rows: list[dict] = []
    for result in comparison.variants:
        label = _comparison_result_label(comparison, result)
        if label not in selected:
            continue
        diagnostics = result.research_diagnostics
        rows.append(
            {
                "strategy": label,
                "strict_coverage_sensitivity": True,
                "positions": result.position_metrics.positions_closed,
                "total_return": result.metrics.total_return,
                "max_drawdown": result.metrics.maximum_drawdown,
                "profit_factor": result.position_metrics.position_profit_factor,
                "expectancy": result.metrics.expectancy_per_trade,
                "confirmation_pass_rate": diagnostics.get("confirmation_pass_rate"),
                "same_entry_bar_final_exits": diagnostics.get(
                    "same_entry_bar_final_exits"
                ),
                "coverage_exclusions": diagnostics.get(
                    "strict_coverage_exclusions", 0
                ),
            }
        )
    return rows


def export_data_qualification(
    reports: dict[str, DataQualificationReport],
    output_directory: Path,
    *,
    stem: str,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write one bounded qualification JSON and an intraday deviation manifest."""

    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "data_qualification": output_directory / f"{stem}_data_qualification.json",
        "gap_manifest": output_directory / f"{stem}_gap_manifest.csv",
    }
    if not overwrite:
        existing = [path for path in paths.values() if path.exists()]
        if existing:
            raise FileExistsError(f"Qualification export already exists: {existing[0]}")
    payload = {
        key: report.model_dump(mode="json") for key, report in sorted(reports.items())
    }
    _atomic_text(paths["data_qualification"], json.dumps(payload, indent=2))
    rows = []
    for key, report in sorted(reports.items()):
        if not report.timeframe.intraday:
            continue
        rows.extend(
            {"qualification": key, **detail.model_dump(mode="json")}
            for detail in report.details
        )
    _atomic_csv(
        paths["gap_manifest"],
        rows,
        ["qualification", *list(QualificationDetail.model_fields)],
    )
    return paths


def export_candidate_audit(
    audit: CandidateAuditResult, output_directory: Path
) -> dict[str, Path]:
    """Persist the bounded audit summary and reusable intraday candidate set."""

    output_directory.mkdir(parents=True, exist_ok=True)
    stem = f"candidate_audit_{audit.requested_start}_{audit.requested_end}"
    paths = {
        "json": output_directory / f"{stem}.json",
        "sessions": output_directory / f"{stem}_sessions.csv",
        "monthly": output_directory / f"{stem}_monthly.csv",
        "failures": output_directory / f"{stem}_failures.csv",
        "near_misses": output_directory / f"{stem}_near_misses.csv",
        "candidates": output_directory / f"{stem}_candidates.csv",
        "intraday_candidates": output_directory / f"{stem}_intraday_candidates.json",
        "entry_symbols": output_directory / f"{stem}_entry_symbols.json",
        "near_miss_symbols": output_directory / f"{stem}_near_miss_symbols.json",
    }
    _atomic_text(paths["json"], json.dumps(audit.model_dump(mode="json"), indent=2))
    session_rows = [item.model_dump(mode="json") for item in audit.sessions]
    monthly_rows = [item.model_dump(mode="json") for item in audit.monthly_summary]
    failure_rows = [item.model_dump(mode="json") for item in audit.failure_reasons]
    near_miss_rows = [item.model_dump(mode="json") for item in audit.near_misses]
    candidate_rows = [item.model_dump(mode="json") for item in audit.candidates]
    _atomic_csv(paths["sessions"], session_rows, list(CandidateAuditSession.model_fields))
    _atomic_csv(
        paths["monthly"],
        monthly_rows,
        list(CandidateAuditMonthly.model_fields),
    )
    _atomic_csv(
        paths["failures"],
        failure_rows,
        list(CandidateFailureSummary.model_fields),
    )
    _atomic_csv(
        paths["near_misses"],
        near_miss_rows,
        list(CandidateNearMiss.model_fields),
    )
    _atomic_csv(
        paths["candidates"],
        candidate_rows,
        list(CandidateAuditEvent.model_fields),
    )
    _atomic_text(
        paths["intraday_candidates"],
        json.dumps(
            {
                "report_type": "historical_candidate_audit_intraday_candidates",
                "requested_start": audit.requested_start.isoformat(),
                "requested_end": audit.requested_end.isoformat(),
                "selection": "all symbols eligible at least once in the production entry funnel",
                "candidate_count": len(audit.candidate_symbols),
                "candidates": [{"symbol": symbol} for symbol in audit.candidate_symbols],
            },
            indent=2,
        ),
    )
    for key, symbols in (
        ("entry_symbols", audit.entry_symbols),
        ("near_miss_symbols", audit.near_miss_symbols),
    ):
        _atomic_text(
            paths[key],
            json.dumps(
                {
                    "report_type": key,
                    "symbols": [{"symbol": symbol} for symbol in symbols],
                },
                indent=2,
            ),
        )
    return paths


def format_candidate_audit_summary(audit: CandidateAuditResult) -> str:
    lines = [
        f"TraWalp historical candidate audit / variant {audit.strategy_variant.value}",
        f"Period: {audit.actual_start} through {audit.actual_end}",
        f"Classification: {audit.classification}",
        f"First eligible candidate: {audit.first_eligible_candidate_date or 'none'} | "
        f"First entry: {audit.first_entry_date or 'none'}",
        f"Unique candidate symbols: {len(audit.candidate_symbols)} | "
        f"entry symbols: {len(audit.entry_symbols)} | near-miss symbols: "
        f"{len(audit.near_miss_symbols)}",
        "",
        "Month    Screens  PITCov   PreRec  Recovery  Eligible  Entries  Primary blocker",
    ]
    for item in audit.monthly_summary:
        coverage = (
            "N/A"
            if item.pit_fundamental_coverage_pct is None
            else f"{item.pit_fundamental_coverage_pct:>6.1%}"
        )
        lines.append(
            f"{item.month:<8} {item.screens:>7}  {coverage:>6} "
            f"{item.candidates_before_recovery:>7} {item.recovery_passes:>9} "
            f"{item.eligible_candidates:>9} {item.actual_entries:>8}  "
            f"{item.primary_blocker or 'none'}"
        )
    if audit.portfolio_blockers:
        lines.extend(["", f"Portfolio blockers: {audit.portfolio_blockers}"])
    lines.extend(
        [
            "",
            "Counts are symbol-session observations; PIT and forward trading logic are unchanged.",
        ]
    )
    return "\n".join(lines)


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
        lines.append("Execution-leg exit summary:")
        lines.extend(
            f"  {reason:<24} {count}" for reason, count in result.exits_by_reason.items()
        )
    if result.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)
    return "\n".join(lines)


def format_comparison_table(comparison: StrategyComparison) -> str:
    qualification = format_data_qualification_header(comparison.data_qualification)
    header = (
        "Strategy                 Return    MaxDD     Pos  PosWin   PosPF  "
        "AvgWin   AvgLoss  MFE     MAE      Capture  Giveback  NeverStop  "
        "ProfitStop  Post5d   PostMFE5"
    )
    rows = [*(qualification.splitlines() if qualification else ()), "", header]
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
    prefetch = comparison.intraday_prefetch
    if not prefetch.required:
        rows.append("Intraday prefetch: not required")
    else:
        state = "enabled" if prefetch.enabled else "disabled"
        rows.append(
            f"Intraday prefetch: {state} | candidate symbols: "
            f"{prefetch.candidate_symbols}"
        )
        for timeframe, details in prefetch.timeframes.items():
            rows.append(
                f"  {timeframe}: complete={details.already_complete_symbols} "
                f"sync={details.sync_requested_symbols} bars_added={details.bars_added}"
            )
    if comparison.skipped_strategies:
        rows.append("Skipped strategies:")
        rows.extend(
            f"  {strategy}: {reason}"
            for strategy, reason in comparison.skipped_strategies.items()
        )
    if comparison.warnings:
        rows.append(f"Warnings: {len(comparison.warnings)} (see JSON report)")
    return "\n".join(rows)


def format_data_qualification_header(metadata: dict) -> str:
    if not metadata:
        return ""
    daily = metadata.get("daily", {})
    lines = [
        "Data qualification",
        "Daily:",
        f"  symbols checked: {daily.get('symbols_checked', 0)}",
        f"  expected sessions: {daily.get('sessions_expected', 0)}",
        f"  missing sessions: {daily.get('missing_sessions', 0)}",
        f"  unresolved gaps: {daily.get('unresolved_gaps', 0)}",
    ]
    for key, intraday in sorted(metadata.get("intraday", {}).items()):
        lines.extend(
            [
                f"Intraday {key}:",
                f"  candidate symbols: {intraday.get('symbols_checked', 0)}",
                f"  expected sessions: {intraday.get('sessions_expected', 0)}",
                f"  complete sessions: {intraday.get('complete_sessions', 0)}",
                f"  missing sessions: {intraday.get('missing_sessions', 0)}",
                f"  partial sessions: {intraday.get('partial_sessions', 0)}",
                "  unknown sessions: "
                f"{intraday.get('unknown_market_activity_sessions', 0)}",
                f"  missing bars: {intraday.get('missing_bars', 0)}",
            ]
        )
    return "\n".join(lines)


def _comparison_result_label(comparison: StrategyComparison, result: BacktestResult) -> str:
    if comparison.comparison_kind is StrategyComparisonKind.POSITION_MANAGEMENT:
        return result.position_management_preset.value
    if comparison.comparison_kind is StrategyComparisonKind.SCORE_VARIANTS:
        return result.strategy_variant.value
    if comparison.comparison_kind in {
        StrategyComparisonKind.RESEARCH_D1_D5,
        StrategyComparisonKind.EXTENDED_VALIDATION,
    }:
        from trading_system.backtest.engine import research_strategy_label

        return research_strategy_label(
            result.strategy_variant, result.position_management_preset
        )
    if (
        comparison.comparison_kind
        is StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION
    ):
        from trading_system.backtest.intraday_isolation import intraday_isolation_label

        return intraday_isolation_label(result.position_management_preset)
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
