"""Frozen extended out-of-sample validation and read-only diagnostics."""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

from trading_system.backtest.coverage import data_qualification_classification
from trading_system.backtest.diagnostics import calculate_position_metrics
from trading_system.backtest.engine import (
    BacktestEngine,
    StrategyComparisonPreparation,
    compare_strategies,
    comparison_intraday_prefetch_metadata,
    evaluate_variant_entry,
    prepare_strategy_comparison,
    research_strategy_label,
)
from trading_system.backtest.report import (
    _atomic_csv,
    _atomic_text,
    _position_fields,
    _trade_fields,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    regular_session_bounds,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.qualification import (
    DataQualificationReport,
    qualify_intraday_history,
)
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    PositionManagementPreset,
    StrategyComparison,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.market_data import BarTimeframe

REFERENCE_EXPECTATIONS = {
    "C/configured": {
        "total_return": 0.01586554966227105,
        "position_profit_factor": 1.696565740494801,
    },
    "D1/C-swing-profit-lock": {
        "total_return": 0.025323865638872345,
        "position_profit_factor": 2.2578910384043063,
    },
    "D2/C-swing-runner": {"total_return": 0.025633627242480195},
    "C/intraday-dynamic": {
        "total_return": 0.037950157828430475,
        "position_profit_factor": 1.8867028546479792,
    },
}
REFERENCE_TOLERANCE = 1e-10
BOOTSTRAP_SEED = 20260820
BOOTSTRAP_RESAMPLES = 10_000


@dataclass(frozen=True)
class ExtendedValidationBundle:
    requested_start: date
    requested_end: date
    actual_start: date
    reference_start: date
    reference_end: date
    daily_qualification: dict[str, Any]
    candidate_discovery: dict[str, Any]
    intraday_qualification: dict[str, Any]
    intraday_gap_rows: list[dict[str, Any]]
    oos: StrategyComparison
    reference: StrategyComparison
    reference_regression: dict[str, Any]
    strict_full_session: BacktestResult
    cost_comparisons: dict[str, StrategyComparison]
    intraday_25bps: BacktestResult
    strategy_summaries: list[dict[str, Any]]
    paired_d1: dict[str, Any]
    monthly_rows: list[dict[str, Any]]
    quarterly_rows: list[dict[str, Any]]
    time_stability: dict[str, Any]
    symbol_rows: list[dict[str, Any]]
    concentration: dict[str, Any]
    leave_one_out_rows: list[dict[str, Any]]
    path_cost_rows: list[dict[str, Any]]
    full_cost_rows: list[dict[str, Any]]
    uncertainty: dict[str, Any]
    trade_path_rows: list[dict[str, Any]]
    intraday_sensitivity: list[dict[str, Any]]
    decisions: dict[str, Any]


def qualify_validation_start(
    database: Database,
    config: StrategyConfig,
    requested_start: date,
    requested_end: date,
) -> dict[str, Any]:
    """Find the first screen with an exact configured Daily warmup in local data."""

    if requested_start > requested_end:
        raise ValueError("requested validation start must not follow end")
    required = required_daily_warmup_sessions(config)
    conflicts = database.unresolved_sec_identity_conflict_symbols()
    companies = [
        company
        for company in database.list_tradable_companies()
        if company.symbol not in conflicts
        and not (
            config.universe.exclude_reits and is_reit(company.sic)
        )
        and not (
            config.universe.exclude_financials
            and is_financial_or_reit(company.sic)
        )
    ]
    symbols = tuple(sorted({company.symbol for company in companies}))
    candidate_sessions = trading_sessions_between(requested_start, requested_end)
    if not candidate_sessions:
        raise ValueError("requested OOS interval contains no XNYS sessions")
    earliest_warmup = daily_warmup_start(candidate_sessions[0], required)
    bars_by_symbol: dict[str, set[date]] = defaultdict(set)
    requested_symbols = (*symbols, "SPY")
    with database.read_only() as connection:
        for offset in range(0, len(requested_symbols), 400):
            batch = requested_symbols[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT symbol,timestamp FROM bars
                WHERE symbol IN ({placeholders}) AND timeframe='1d'
                AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                [
                    *batch,
                    earliest_warmup.isoformat(),
                    (requested_end + timedelta(days=1)).isoformat(),
                ],
            ).fetchall()
            for symbol, timestamp in rows:
                bars_by_symbol[str(symbol)].add(
                    date.fromisoformat(str(timestamp)[:10])
                )

    actual_session: date | None = None
    complete_symbols: list[str] = []
    expected_prior: tuple[date, ...] = ()
    for session in candidate_sessions:
        required_history = tuple(
            trading_sessions_between(
                daily_warmup_start(session, required), session
            )
        )
        if len(required_history) != required:
            continue
        benchmark_complete = set(required_history).issubset(
            bars_by_symbol.get("SPY", set())
        )
        complete = [
            symbol
            for symbol in symbols
            if set(required_history).issubset(bars_by_symbol.get(symbol, set()))
        ]
        # The production screen already rejects each insufficient-history symbol.
        # Qualification therefore requires an exact benchmark warmup and at least
        # one genuinely screenable member; no arbitrary universe-percentage gate.
        if benchmark_complete and complete:
            actual_session = session
            complete_symbols = complete
            expected_prior = required_history
            break
    if actual_session is None:
        return {
            "qualified": False,
            "failure_reason": (
                "no session has a complete SPY warmup and any complete eligible symbol"
            ),
            "requested_validation_start": requested_start.isoformat(),
            "requested_validation_end": requested_end.isoformat(),
            "required_prior_sessions": required,
            "required_daily_warmup_start": earliest_warmup.isoformat(),
            "representative_eligible_symbols": len(symbols),
        }

    internal_gaps = 0
    edge_gaps = 0
    symbols_with_internal_gaps = 0
    expected_set = set(expected_prior)
    for symbol in symbols:
        present = bars_by_symbol.get(symbol, set()) & expected_set
        missing = expected_set - present
        if not missing:
            continue
        if present:
            first_present = min(present)
            last_present = max(present)
            internal = sum(first_present < item < last_present for item in missing)
        else:
            internal = 0
        internal_gaps += internal
        edge_gaps += len(missing) - internal
        symbols_with_internal_gaps += int(internal > 0)
    return {
        "qualified": True,
        "qualification_rule": (
            "exact SPY 300-session completed-screen history plus at least one "
            "current-universe symbol with all required sessions; insufficient symbols "
            "retain the production rejection"
        ),
        "requested_validation_start": requested_start.isoformat(),
        "requested_validation_end": requested_end.isoformat(),
        "required_prior_sessions": required,
        "required_daily_warmup_start": expected_prior[0].isoformat(),
        "actual_first_qualified_screen_session": actual_session.isoformat(),
        "representative_eligible_symbols": len(symbols),
        "symbols_with_complete_required_history": len(complete_symbols),
        "percentage_with_complete_required_history": (
            len(complete_symbols) / len(symbols) if symbols else 0.0
        ),
        "symbols_rejected_insufficient_market_history": len(symbols)
        - len(complete_symbols),
        "internal_daily_gaps": internal_gaps,
        "symbols_with_internal_daily_gaps": symbols_with_internal_gaps,
        "edge_or_lifecycle_daily_gaps": edge_gaps,
        "benchmark_warmup_complete": True,
    }


def discover_intraday_candidate_sessions(
    preparation: StrategyComparisonPreparation,
    config: StrategyConfig,
) -> dict[str, Any]:
    """Return every PIT-eligible C symbol/session before reading intraday bars."""

    rows: dict[tuple[str, date], dict[str, Any]] = {}
    for index, signal_session in enumerate(preparation.sessions[:-1]):
        execution_session = preparation.sessions[index + 1]
        report = preparation.screen_source.screen(signal_session)
        for record in report.records:
            evaluation = evaluate_variant_entry(record, StrategyVariant.FULL, config)
            if not evaluation.eligible:
                continue
            key = (record.symbol.upper(), execution_session)
            rows[key] = {
                "symbol": key[0],
                "signal_session": signal_session.isoformat(),
                "execution_session": execution_session.isoformat(),
                "daily_score": evaluation.score,
            }
    ordered = sorted(rows.values(), key=lambda item: (item["execution_session"], item["symbol"]))
    return {
        "variant": StrategyVariant.FULL.value,
        "candidate_symbols": sorted({row["symbol"] for row in ordered}),
        "candidate_symbol_count": len({row["symbol"] for row in ordered}),
        "candidate_symbol_sessions": len(ordered),
        "sessions": ordered,
    }


def qualify_intraday_candidate_sessions(
    database: Database,
    candidate_discovery: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Classify only the symbol-sessions discovered by causal Daily screens."""

    candidates = candidate_discovery["sessions"]
    symbols = candidate_discovery["candidate_symbols"]
    if not candidates:
        return {
            "candidate_symbols": 0,
            "candidate_symbol_sessions": 0,
            "complete_sessions": 0,
            "partial_sessions": 0,
            "missing_sessions": 0,
            "unknown_market_activity_sessions": 0,
            "expected_bars": 0,
            "present_bars": 0,
            "missing_bars": 0,
            "missing_0930_bars": 0,
            "internal_gaps": 0,
            "edge_or_lifecycle_gaps": 0,
            "full_oos_intraday_qualified": False,
            "full_session_strict_qualified": False,
            "candidate_entry_opportunity_qualified": False,
        }, []
    first = date.fromisoformat(candidates[0]["execution_session"])
    last = date.fromisoformat(candidates[-1]["execution_session"])
    opening, _ = regular_session_bounds(first)
    _, closing = regular_session_bounds(last)
    bars = database.bars_between(
        symbols, opening, closing, timeframe=BarTimeframe.MINUTES_15
    )
    present: dict[tuple[str, date], set[datetime]] = defaultdict(set)
    for bar in bars:
        present[(bar.symbol, bar.timestamp.date())].add(bar.timestamp.astimezone(UTC))

    rows: list[dict[str, Any]] = []
    counters: defaultdict[str, int] = defaultdict(int)
    for candidate in candidates:
        symbol = candidate["symbol"]
        session = date.fromisoformat(candidate["execution_session"])
        expected = _expected_15m_timestamps(session)
        actual = present.get((symbol, session), set()) & set(expected)
        missing = [item for item in expected if item not in actual]
        if not actual:
            structural = "MISSING_SESSION"
            status = "MISSING_SESSION"
        elif missing:
            structural = "PARTIAL_SESSION"
            status = "UNKNOWN_MARKET_ACTIVITY"
        else:
            structural = "COMPLETE"
            status = "COMPLETE"
        internal = 0
        if actual:
            first_actual, last_actual = min(actual), max(actual)
            internal = sum(first_actual < item < last_actual for item in missing)
        edge = len(missing) - internal
        row = {
            **candidate,
            "timeframe": BarTimeframe.MINUTES_15.value,
            "structural_status": structural,
            "status": status,
            "expected_bars": len(expected),
            "present_bars": len(actual),
            "missing_bars": len(missing),
            "missing_0930_bar": bool(expected and expected[0] in missing),
            "internal_gaps": internal,
            "edge_or_lifecycle_gaps": edge,
            "missing_timestamps": [item.isoformat() for item in missing],
        }
        rows.append(row)
        counters[f"{structural.lower()}_sessions"] += 1
        counters["unknown_market_activity_sessions"] += int(
            status == "UNKNOWN_MARKET_ACTIVITY"
        )
        counters["expected_bars"] += len(expected)
        counters["present_bars"] += len(actual)
        counters["missing_bars"] += len(missing)
        counters["missing_0930_bars"] += int(row["missing_0930_bar"])
        counters["internal_gaps"] += internal
        counters["edge_or_lifecycle_gaps"] += edge
    summary = {
        "candidate_symbols": len(symbols),
        "candidate_symbol_sessions": len(rows),
        "complete_sessions": counters["complete_sessions"],
        "partial_sessions": counters["partial_session_sessions"],
        "missing_sessions": counters["missing_session_sessions"],
        "unknown_market_activity_sessions": counters[
            "unknown_market_activity_sessions"
        ],
        "expected_bars": counters["expected_bars"],
        "present_bars": counters["present_bars"],
        "missing_bars": counters["missing_bars"],
        "missing_0930_bars": counters["missing_0930_bars"],
        "internal_gaps": counters["internal_gaps"],
        "edge_or_lifecycle_gaps": counters["edge_or_lifecycle_gaps"],
        "full_oos_intraday_qualified": (
            counters["complete_sessions"] == len(rows)
        ),
        "full_session_strict_qualified": (
            counters["complete_sessions"] == len(rows)
        ),
        "candidate_entry_opportunity_qualified": counters["missing_0930_bars"] == 0,
    }
    return summary, rows


def intraday_session_statuses(
    report: DataQualificationReport,
    symbols: list[str],
    sessions: tuple[date, ...],
) -> dict[tuple[str, date], str]:
    statuses = {
        (symbol, session): "COMPLETE" for symbol in symbols for session in sessions
    }
    for detail in report.details:
        if detail.session in sessions:
            statuses[(detail.symbol, detail.session)] = detail.status.value
    return statuses


def annotate_trade_path_coverage(
    database: Database,
    result: BacktestResult,
    *,
    strategy_label: str = "C/intraday-dynamic",
) -> tuple[BacktestResult, list[dict[str, Any]]]:
    """Attach exact native-bar completeness while each intraday position was open."""

    if not result.positions:
        return result, []
    positions = [
        position
        for position in result.positions
        if position.entry_timestamp is not None and position.exit_timestamp is not None
    ]
    if not positions:
        return result, []
    symbols = sorted({position.symbol for position in positions})
    start = min(position.entry_timestamp for position in positions if position.entry_timestamp)
    final_session = max(position.exit_date for position in positions)
    _, end = regular_session_bounds(final_session)
    bars = database.bars_between(
        symbols, start, end, timeframe=BarTimeframe.MINUTES_15
    )
    actual_by_symbol: dict[str, set[datetime]] = defaultdict(set)
    for bar in bars:
        actual_by_symbol[bar.symbol].add(bar.timestamp.astimezone(UTC))

    updates: dict[str, BacktestPosition] = {}
    rows: list[dict[str, Any]] = []
    for position in positions:
        assert position.entry_timestamp is not None
        assert position.exit_timestamp is not None
        entry = position.entry_timestamp.astimezone(UTC)
        exit_timestamp = position.exit_timestamp.astimezone(UTC)
        expected_open: list[datetime] = []
        for session in trading_sessions_between(position.entry_date, position.exit_date):
            expected_open.extend(
                timestamp
                for timestamp in _expected_15m_timestamps(session)
                if entry <= timestamp <= exit_timestamp
            )
        actual = actual_by_symbol.get(position.symbol, set())
        missing_open = [item for item in expected_open if item not in actual]
        same_session_expected = _expected_15m_timestamps(position.exit_date)
        after_exit_missing = [
            item
            for item in same_session_expected
            if item > exit_timestamp and item not in actual
        ]
        session_open, _ = regular_session_bounds(position.entry_date)
        missing_opening = entry != session_open
        row = {
            "strategy": strategy_label,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "entry_timestamp": entry.isoformat(),
            "exit_timestamp": exit_timestamp.isoformat(),
            "expected_native_15m_timestamps": len(expected_open),
            "trade_path_complete": not missing_open,
            "trade_path_missing_bar_count": len(missing_open),
            "trade_path_missing_timestamps": [
                item.isoformat() for item in missing_open
            ],
            "missing_opening_bar_affected_entry": missing_opening,
            "gap_before_exit": bool(missing_open),
            "gap_after_exit_only": bool(after_exit_missing) and not missing_open,
            "after_exit_missing_timestamps": [
                item.isoformat() for item in after_exit_missing
            ],
        }
        rows.append(row)
        updates[position.position_id] = position.model_copy(
            update={
                "trade_path_complete": row["trade_path_complete"],
                "trade_path_missing_bar_count": row[
                    "trade_path_missing_bar_count"
                ],
                "trade_path_missing_timestamps": tuple(
                    row["trade_path_missing_timestamps"]
                ),
                "gap_before_exit": row["gap_before_exit"],
                "gap_after_exit_only": row["gap_after_exit_only"],
                "missing_opening_bar_affected_entry": missing_opening,
            }
        )
    annotated = tuple(updates.get(item.position_id, item) for item in result.positions)
    return result.model_copy(
        update={
            "positions": annotated,
            "position_metrics": calculate_position_metrics(
                annotated, result.position_metrics.positions_opened
            ),
        }
    ), rows


def annotate_comparison_trade_paths(
    database: Database, comparison: StrategyComparison
) -> tuple[StrategyComparison, list[dict[str, Any]]]:
    variants: list[BacktestResult] = []
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        if result.position_management_preset is PositionManagementPreset.INTRADAY_DYNAMIC:
            result, result_rows = annotate_trade_path_coverage(database, result)
            rows.extend(result_rows)
        variants.append(result)
    return comparison.model_copy(update={"variants": tuple(variants)}), rows


def paired_d1_analysis(
    configured: BacktestResult, d1: BacktestResult
) -> dict[str, Any]:
    """Decompose same-entry D1 exits from the residual max_positions path effect."""

    def key(position: BacktestPosition) -> tuple[str, date, datetime | None]:
        return position.symbol, position.signal_date, position.entry_timestamp

    configured_by_key = {key(position): position for position in configured.positions}
    d1_by_key = {key(position): position for position in d1.positions}
    common = sorted(set(configured_by_key) & set(d1_by_key), key=str)
    details: list[dict[str, Any]] = []
    direct_pnl = 0.0
    direct_return = 0.0
    changed = 0
    for item in common:
        control = configured_by_key[item]
        locked = d1_by_key[item]
        different = (
            control.exit_timestamp != locked.exit_timestamp
            or control.exit_reason != locked.exit_reason
            or abs(control.net_pnl - locked.net_pnl) > 1e-12
        )
        changed += int(different)
        pnl_difference = locked.net_pnl - control.net_pnl
        return_difference = locked.position_return - control.position_return
        direct_pnl += pnl_difference
        direct_return += return_difference
        details.append(
            {
                "symbol": control.symbol,
                "signal_date": control.signal_date.isoformat(),
                "entry_timestamp": (
                    control.entry_timestamp.isoformat()
                    if control.entry_timestamp
                    else None
                ),
                "configured_exit_date": control.exit_date.isoformat(),
                "d1_exit_date": locked.exit_date.isoformat(),
                "configured_exit_reason": control.exit_reason,
                "d1_exit_reason": locked.exit_reason,
                "pnl_difference": pnl_difference,
                "position_return_difference": return_difference,
                "exit_changed": different,
            }
        )
    total_pnl_difference = sum(item.net_pnl for item in d1.positions) - sum(
        item.net_pnl for item in configured.positions
    )
    path_pnl = total_pnl_difference - direct_pnl
    configured_only = sorted(set(configured_by_key) - set(d1_by_key), key=str)
    d1_only = sorted(set(d1_by_key) - set(configured_by_key), key=str)
    return {
        "paired_positions": len(common),
        "changed_exit_positions": changed,
        "identical_positions": len(common) - changed,
        "direct_exit_management_pnl_effect": direct_pnl,
        "direct_exit_management_return_effect": direct_pnl / configured.initial_capital,
        "sum_paired_position_return_difference": direct_return,
        "total_closed_position_pnl_difference": total_pnl_difference,
        "total_closed_position_return_difference": (
            total_pnl_difference / configured.initial_capital
        ),
        "headline_total_return_difference": (
            (d1.metrics.total_return or 0) - (configured.metrics.total_return or 0)
        ),
        "subsequent_portfolio_path_pnl_effect": path_pnl,
        "subsequent_portfolio_path_return_effect": (
            path_pnl / configured.initial_capital
        ),
        "return_decomposition_residual": (
            (d1.metrics.total_return or 0)
            - (configured.metrics.total_return or 0)
            - total_pnl_difference / configured.initial_capital
        ),
        "configured_only_positions": len(configured_only),
        "d1_only_positions": len(d1_only),
        "subsequent_portfolio_path_changes": len(configured_only) + len(d1_only),
        "configured_only_keys": [_position_key_json(item) for item in configured_only],
        "d1_only_keys": [_position_key_json(item) for item in d1_only],
        "pairs": details,
        "decomposition_note": (
            "path effect is the residual closed-position PnL difference after exact same-entry "
            "pairs; it includes future max_positions=1 slot and sizing-path changes"
        ),
    }


def strategy_summary(label: str, result: BacktestResult) -> dict[str, Any]:
    positions = list(result.positions)
    metrics = result.metrics
    position_metrics = result.position_metrics
    research = result.research_diagnostics
    same_bar = [
        item
        for item in positions
        if item.entry_timestamp is not None
        and item.exit_timestamp == item.entry_timestamp
    ]
    runner_positions = {
        trade.position_id
        for trade in result.trades
        if trade.exit_reason == "partial_take_profit"
    }
    runner_final_legs = [
        trade
        for trade in result.trades
        if trade.position_id in runner_positions and not trade.is_partial_exit
    ]
    return {
        "strategy": label,
        "total_return": metrics.total_return,
        "cagr": metrics.cagr,
        "max_drawdown": metrics.maximum_drawdown,
        "sharpe": metrics.sharpe_ratio,
        "sortino": metrics.sortino_ratio,
        "position_profit_factor": position_metrics.position_profit_factor,
        "expectancy": metrics.expectancy_per_trade,
        "win_rate": position_metrics.position_win_rate,
        "loss_rate": position_metrics.position_loss_rate,
        "average_win": position_metrics.average_position_win,
        "average_loss": position_metrics.average_position_loss,
        "best_trade": metrics.best_trade,
        "worst_trade": metrics.worst_trade,
        "positions": position_metrics.positions_closed,
        "execution_legs": result.execution_metrics.execution_legs,
        "trades_per_month": metrics.trades_per_month,
        "average_holding_period": position_metrics.average_position_holding_period,
        "median_holding_period": position_metrics.median_position_holding_period,
        "exposure": metrics.exposure,
        "end_of_day_exposure": metrics.end_of_day_exposure,
        "turnover": metrics.portfolio_turnover,
        "slippage_cost": sum(item.slippage for item in result.trades),
        "commission_cost": sum(item.transaction_cost for item in result.trades),
        "total_modeled_execution_cost": sum(
            item.slippage + item.transaction_cost for item in result.trades
        ),
        "mean_mfe": position_metrics.average_mfe,
        "median_mfe": median(
            [item.maximum_favorable_excursion for item in positions]
        )
        if positions
        else None,
        "mean_mae": position_metrics.average_mae,
        "median_mae": median(
            [item.maximum_adverse_excursion for item in positions]
        )
        if positions
        else None,
        "profit_capture": position_metrics.average_profit_capture,
        "giveback": position_metrics.average_profit_giveback,
        "positions_reaching_1r": research.get("reached_1r_mfe", 0),
        "positions_reaching_2r": research.get("reached_2r_mfe", 0),
        "break_even_lock_activations": research.get(
            "break_even_lock_activations", 0
        ),
        "one_r_lock_activations": research.get("one_r_lock_activations", 0),
        "profit_lock_exits": sum(
            item.exit_reason == "profit_lock" for item in positions
        ),
        "losses_after_1r": research.get("losses_after_1r_mfe", 0),
        "losses_after_2r": research.get("losses_after_2r_mfe", 0),
        "partials": research.get("partial_target_count", 0),
        "runners": len(runner_positions),
        "runner_return": research.get("runner_final_return"),
        "runner_mfe": research.get("runner_mfe"),
        "runner_giveback": research.get("runner_giveback"),
        "runner_net_pnl_contribution": sum(
            item.net_pnl if item.net_pnl is not None else item.pnl
            for item in runner_final_legs
        ),
        "largest_runner_pnl_share": (
            max(
                item.net_pnl if item.net_pnl is not None else item.pnl
                for item in runner_final_legs
            )
            / sum(
                item.net_pnl if item.net_pnl is not None else item.pnl
                for item in runner_final_legs
            )
            if runner_final_legs
            and sum(
                item.net_pnl if item.net_pnl is not None else item.pnl
                for item in runner_final_legs
            )
            > 0
            else None
        ),
        "same_entry_bar_exits": len(same_bar),
        "same_entry_bar_losses": sum(item.net_pnl < 0 for item in same_bar),
        "first_bar_survivors": len(positions) - len(same_bar),
        "survivor_win_rate": (
            sum(item.net_pnl > 0 for item in positions if item not in same_bar)
            / (len(positions) - len(same_bar))
            if len(positions) > len(same_bar)
            else None
        ),
        "partial_targets": research.get("partial_target_count", 0),
        "runner_positions": research.get("runner_positions", 0),
        "return_per_average_exposure": (
            metrics.total_return / metrics.exposure
            if metrics.total_return is not None and metrics.exposure
            else None
        ),
    }


def time_analysis(
    comparison: StrategyComparison, period: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if period not in {"month", "quarter"}:
        raise ValueError("period must be month or quarter")
    rows: list[dict[str, Any]] = []
    stability: dict[str, Any] = {}
    for result in comparison.variants:
        label = _label(result)
        grouped: dict[str, list[BacktestPosition]] = defaultdict(list)
        for position in result.positions:
            key = (
                position.exit_date.strftime("%Y-%m")
                if period == "month"
                else f"{position.exit_date.year}-Q{(position.exit_date.month - 1) // 3 + 1}"
            )
            grouped[key].append(position)
        keys = _period_keys(comparison.actual_start, comparison.actual_end, period)
        positive = negative = 0
        for key in keys:
            positions = grouped.get(key, [])
            pnl = sum(item.net_pnl for item in positions)
            wins = [item for item in positions if item.net_pnl > 0]
            losses = [item for item in positions if item.net_pnl < 0]
            gross_loss = abs(sum(item.net_pnl for item in losses))
            positive += int(bool(positions) and pnl > 0)
            negative += int(bool(positions) and pnl < 0)
            rows.append(
                {
                    "strategy": label,
                    "period": key,
                    "positions": len(positions),
                    "net_pnl_contribution": pnl,
                    "return_contribution": pnl / result.initial_capital,
                    "average_position_return": mean(
                        [item.position_return for item in positions]
                    )
                    if positions
                    else None,
                    "win_rate": len(wins) / len(positions) if positions else None,
                    "profit_factor": (
                        sum(item.net_pnl for item in wins) / gross_loss
                        if gross_loss
                        else None
                    ),
                }
            )
        active = positive + negative
        stability[label] = {
            f"positive_{period}s": positive,
            f"negative_{period}s": negative,
            f"percentage_positive_{period}s": positive / active if active else None,
            f"active_{period}s": active,
            f"calendar_{period}s": len(keys),
        }
    return rows, stability


def symbol_and_leave_one_out(
    comparison: StrategyComparison,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    symbol_rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    leave_rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        label = _label(result)
        grouped: dict[str, list[BacktestPosition]] = defaultdict(list)
        for position in result.positions:
            grouped[position.symbol].append(position)
        contributions = {
            symbol: sum(item.net_pnl for item in positions)
            for symbol, positions in grouped.items()
        }
        ranked = sorted(contributions.items(), key=lambda item: (-item[1], item[0]))
        total = sum(contributions.values())
        for symbol in sorted(grouped):
            positions = grouped[symbol]
            symbol_rows.append(
                {
                    "strategy": label,
                    "symbol": symbol,
                    "positions": len(positions),
                    "net_pnl_contribution": contributions[symbol],
                    "return_contribution": contributions[symbol]
                    / result.initial_capital,
                    "sum_position_returns": sum(
                        item.position_return for item in positions
                    ),
                    "wins": sum(item.net_pnl > 0 for item in positions),
                    "losses": sum(item.net_pnl < 0 for item in positions),
                }
            )
            retained = [item for item in result.positions if item.symbol != symbol]
            leave_rows.append(
                _posthoc_position_row(
                    label,
                    f"WITHOUT_{symbol}",
                    retained,
                    result.initial_capital,
                    removed_symbols=[symbol],
                )
            )
        best = ranked[0][0] if ranked else None
        worst = ranked[-1][0] if ranked else None
        top_two = [symbol for symbol, value in ranked if value > 0][:2]
        without_best = [item for item in result.positions if item.symbol != best]
        without_worst = [item for item in result.positions if item.symbol != worst]
        without_top_two = [
            item for item in result.positions if item.symbol not in set(top_two)
        ]
        best_row = _posthoc_position_row(
            label,
            "WITHOUT_BEST_CONTRIBUTOR",
            without_best,
            result.initial_capital,
            removed_symbols=[best] if best else [],
        )
        worst_row = _posthoc_position_row(
            label,
            "WITHOUT_WORST_CONTRIBUTOR",
            without_worst,
            result.initial_capital,
            removed_symbols=[worst] if worst else [],
        )
        top_two_row = _posthoc_position_row(
            label,
            "WITHOUT_TOP_TWO_POSITIVE_CONTRIBUTORS",
            without_top_two,
            result.initial_capital,
            removed_symbols=top_two,
        )
        leave_rows.extend((best_row, worst_row, top_two_row))
        summaries[label] = {
            "best_contributing_symbol": best,
            "worst_contributing_symbol": worst,
            "top_1_pnl_concentration": (
                ranked[0][1] / total if ranked and total else None
            ),
            "top_3_pnl_concentration": (
                sum(value for _, value in ranked[:3]) / total if total else None
            ),
            "without_best_contributor": best_row,
            "without_worst_contributor": worst_row,
            "without_top_two_positive_contributors": top_two_row,
            "profitability_disappears_without_best": (
                total > 0 and best_row["sum_net_pnl"] <= 0
            ),
        }
    return symbol_rows, summaries, leave_rows


def path_preserving_cost_stress(
    comparison: StrategyComparison,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in comparison.variants:
        label = _label(result)
        legs: dict[str, list] = defaultdict(list)
        for trade in result.trades:
            legs[trade.position_id].append(trade)
        path = [
            (
                position.position_id,
                position.entry_timestamp.isoformat() if position.entry_timestamp else None,
                tuple(
                    (
                        item.execution_leg_id,
                        item.exit_timestamp.isoformat() if item.exit_timestamp else None,
                        item.exit_reason,
                        item.quantity,
                    )
                    for item in legs[position.position_id]
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
                entry_fill = position.entry_reference_price * (
                    1 + slippage_bps / 10_000
                )
                entry_commission = (
                    entry_fill
                    * position.initial_quantity
                    * commission_bps
                    / 10_000
                )
                cost = (
                    (entry_fill - position.entry_reference_price)
                    * position.initial_quantity
                    + entry_commission
                )
                pnl = -entry_fill * position.initial_quantity - entry_commission
                for leg in legs[position.position_id]:
                    exit_fill = leg.exit_reference_price * (
                        1 - slippage_bps / 10_000
                    )
                    exit_commission = (
                        exit_fill * leg.quantity * commission_bps / 10_000
                    )
                    pnl += exit_fill * leg.quantity - exit_commission
                    cost += (
                        (leg.exit_reference_price - exit_fill) * leg.quantity
                        + exit_commission
                    )
                pnls.append(pnl)
                total_costs += cost
            rows.append(
                {
                    "cost_case": case,
                    "strategy": label,
                    "slippage_bps": slippage_bps,
                    "commission_bps": commission_bps,
                    "path_preserving_cost_stress": True,
                    "execution_path_unchanged": True,
                    "execution_path_hash": path_hash,
                    "positions": len(pnls),
                    "total_return": sum(pnls) / result.initial_capital,
                    "profit_factor": _profit_factor(pnls),
                    "expectancy": mean(pnls) if pnls else None,
                    "modeled_costs": total_costs,
                    "turnover": result.metrics.portfolio_turnover,
                }
            )
    return rows


def bootstrap_uncertainty(
    result: BacktestResult,
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    returns = [item.position_return for item in result.positions]
    if not returns:
        return {
            "seed": seed,
            "resamples": resamples,
            "positions": 0,
            "mean_position_return": None,
            "median_position_return": None,
            "bootstrap_mean_2_5_percentile": None,
            "bootstrap_mean_97_5_percentile": None,
            "probability_bootstrap_mean_gt_zero": None,
        }
    rng = random.Random(seed)
    means = sorted(
        mean(rng.choice(returns) for _ in returns) for _ in range(resamples)
    )
    return {
        "seed": seed,
        "resamples": resamples,
        "positions": len(returns),
        "mean_position_return": mean(returns),
        "median_position_return": median(returns),
        "bootstrap_mean_2_5_percentile": _percentile(means, 0.025),
        "bootstrap_mean_97_5_percentile": _percentile(means, 0.975),
        "probability_bootstrap_mean_gt_zero": sum(item > 0 for item in means)
        / len(means),
        "caveat": (
            "position-level resampling is not an independence proof or formal significance "
            "test; returns are serially dependent and portfolio state matters"
        ),
    }


def reference_regression(comparison: StrategyComparison) -> dict[str, Any]:
    results = {_label(result): result for result in comparison.variants}
    checks: dict[str, Any] = {}
    passed = True
    for label, expected in REFERENCE_EXPECTATIONS.items():
        result = results.get(label)
        if result is None:
            checks[label] = {"passed": False, "reason": "strategy missing"}
            passed = False
            continue
        actual = {
            "total_return": result.metrics.total_return,
            "position_profit_factor": result.position_metrics.position_profit_factor,
        }
        differences = {
            field: (
                None
                if actual[field] is None
                else actual[field] - expected_value
            )
            for field, expected_value in expected.items()
        }
        strategy_passed = all(
            difference is not None and abs(difference) <= REFERENCE_TOLERANCE
            for difference in differences.values()
        )
        passed &= strategy_passed
        checks[label] = {
            "passed": strategy_passed,
            "expected": expected,
            "actual": actual,
            "differences": differences,
        }
    return {
        "passed": passed,
        "absolute_tolerance": REFERENCE_TOLERANCE,
        "checks": checks,
    }


def full_cost_stress_rows(
    comparisons: dict[str, StrategyComparison], intraday_25bps: BacktestResult
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, comparison in comparisons.items():
        for result in comparison.variants:
            rows.append(_cost_row(case, _label(result), result))
    rows.append(_cost_row("INTRADAY_25BPS", "C/intraday-dynamic", intraday_25bps))
    return rows


def intraday_sensitivity_rows(
    native: BacktestResult,
    strict_full_session: BacktestResult,
) -> list[dict[str, Any]]:
    trade_path_positions = [
        item for item in native.positions if item.trade_path_complete is True
    ]
    native_row = _sensitivity_row("NATIVE", native.positions, native.initial_capital)
    native_row.update(
        {
            "total_return": native.metrics.total_return,
            "max_drawdown": native.metrics.maximum_drawdown,
            "profit_factor": native.position_metrics.position_profit_factor,
            "expectancy": native.metrics.expectancy_per_trade,
        }
    )
    strict_row = _sensitivity_row(
        "STRICT_FULL_SESSION",
        strict_full_session.positions,
        strict_full_session.initial_capital,
    )
    strict_row.update(
        {
            "total_return": strict_full_session.metrics.total_return,
            "max_drawdown": strict_full_session.metrics.maximum_drawdown,
            "profit_factor": strict_full_session.position_metrics.position_profit_factor,
            "expectancy": strict_full_session.metrics.expectancy_per_trade,
        }
    )
    trade_path_row = _sensitivity_row(
        "STRICT_TRADE_PATH", trade_path_positions, native.initial_capital
    )
    trade_path_row["post_hoc_position_filter"] = True
    trade_path_row["max_drawdown"] = None
    return [native_row, strict_row, trade_path_row]


def classify_validation(
    summaries: dict[str, dict[str, Any]],
    paired: dict[str, Any],
    concentration: dict[str, Any],
    monthly_stability: dict[str, Any],
    full_cost_rows: list[dict[str, Any]],
    intraday_sensitivity: list[dict[str, Any]],
) -> dict[str, Any]:
    d1 = summaries["D1/C-swing-profit-lock"]
    configured = summaries["C/configured"]
    intraday = summaries["C/intraday-dynamic"]
    d2 = summaries["D2/C-swing-runner"]
    b = summaries["B/configured"]
    d1b = summaries["D1/B-swing-profit-lock"]
    cost = {
        (row["cost_case"], row["strategy"]): row for row in full_cost_rows
    }
    trade_path = next(
        row for row in intraday_sensitivity if row["sensitivity"] == "STRICT_TRADE_PATH"
    )
    d1_checks = {
        "positive_return": (d1["total_return"] or 0) > 0,
        "pf_above_one": (d1["position_profit_factor"] or 0) > 1,
        "positive_expectancy": (d1["expectancy"] or 0) > 0,
        "drawdown_not_materially_worse": abs(d1["max_drawdown"] or 0)
        <= abs(configured["max_drawdown"] or 0) + 0.005,
        "positive_at_15bps": (
            cost[("3X_SLIPPAGE", "D1/C-swing-profit-lock")]["total_return"]
            or 0
        )
        > 0,
        "not_single_symbol_dependent": not concentration[
            "D1/C-swing-profit-lock"
        ]["profitability_disappears_without_best"],
        "positive_direct_lock_effect": paired[
            "direct_exit_management_pnl_effect"
        ]
        > 0,
        "monthly_balance": monthly_stability["D1/C-swing-profit-lock"][
            "positive_months"
        ]
        >= monthly_stability["D1/C-swing-profit-lock"]["negative_months"],
    }
    if (
        (d1["total_return"] or 0) <= 0
        and (d1["position_profit_factor"] or 0) <= 1
    ) or (
        (d1["total_return"] or 0) < (configured["total_return"] or 0)
        and abs(d1["max_drawdown"] or 0) >= abs(configured["max_drawdown"] or 0)
    ):
        d1_decision = "NOT SUPPORTED"
    elif sum(d1_checks.values()) >= 6 and all(
        d1_checks[key]
        for key in ("positive_return", "pf_above_one", "positive_expectancy")
    ):
        d1_decision = "STRONG OOS SUPPORT"
    else:
        d1_decision = "MIXED OOS SUPPORT"

    intraday_core = all(
        (
            (intraday["total_return"] or 0) > 0,
            (intraday["position_profit_factor"] or 0) > 1,
            (intraday["expectancy"] or 0) > 0,
        )
    )
    intraday_checks = (
        intraday_core,
        (cost[("2X_SLIPPAGE", "C/intraday-dynamic")]["total_return"] or 0)
        > 0,
        (intraday["total_return"] or 0) * (trade_path["total_return"] or 0) > 0,
        not concentration["C/intraday-dynamic"][
            "profitability_disappears_without_best"
        ],
    )
    if all(intraday_checks):
        intraday_decision = "STRONG OOS SUPPORT"
    elif intraday_core:
        intraday_decision = "MIXED OOS SUPPORT"
    else:
        intraday_decision = "NOT SUPPORTED"

    d2_supported = (
        (d2["total_return"] or 0) > (d1["total_return"] or 0)
        and (d2["position_profit_factor"] or 0)
        >= (d1["position_profit_factor"] or 0) * 0.95
        and (d2["expectancy"] or 0) >= (d1["expectancy"] or 0) * 0.9
        and d2["runners"] >= 2
        and (d2["largest_runner_pnl_share"] or 1) <= 0.8
    )
    if d2_supported:
        d2_decision = "SUPPORTED"
    elif (d2["total_return"] or 0) > 0 and d2["runners"]:
        d2_decision = "INCONCLUSIVE"
    else:
        d2_decision = "NOT SUPPORTED"

    d1b_all_worse = (
        (d1b["total_return"] or 0) < (b["total_return"] or 0)
        and (d1b["position_profit_factor"] or 0)
        < (b["position_profit_factor"] or 0)
        and (d1b["expectancy"] or 0) < (b["expectancy"] or 0)
        and abs(d1b["max_drawdown"] or 0) > abs(b["max_drawdown"] or 0)
    )
    if d1b_all_worse:
        d1b_decision = "WORSE THAN B"
    else:
        improvements = sum(
            (
                (d1b["total_return"] or 0) > (b["total_return"] or 0),
                (d1b["position_profit_factor"] or 0)
                > (b["position_profit_factor"] or 0),
                (d1b["expectancy"] or 0) > (b["expectancy"] or 0),
                abs(d1b["max_drawdown"] or 0) < abs(b["max_drawdown"] or 0),
            )
        )
        d1b_decision = "IMPROVES B" if improvements >= 3 else "NO MATERIAL IMPROVEMENT"
    return {
        "D1/C": d1_decision,
        "C/intraday-dynamic": intraday_decision,
        "D2/C": d2_decision,
        "D1/B": d1b_decision,
        "D1/C_criteria": d1_checks,
    }


def run_extended_validation(
    database: Database,
    config: StrategyConfig,
    requested_start: date,
    requested_end: date,
    reference_start: date,
    reference_end: date,
) -> ExtendedValidationBundle:
    """Run the frozen OOS and reference workflow without any data mutation."""

    if config.backtest.slippage_bps != 5 or config.backtest.commission_bps != 0:
        raise ValueError("extended validation requires frozen 5 bps / 0 bps baseline costs")
    daily = qualify_validation_start(database, config, requested_start, requested_end)
    if not daily.get("qualified"):
        raise ValueError(f"OOS daily qualification failed: {daily['failure_reason']}")
    actual_start = date.fromisoformat(daily["actual_first_qualified_screen_session"])
    preparation = prepare_strategy_comparison(
        database,
        config,
        actual_start,
        requested_end,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
    )
    discovery = discover_intraday_candidate_sessions(preparation, config)
    intraday_summary, intraday_gaps = qualify_intraday_candidate_sessions(
        database, discovery
    )
    symbols = discovery["candidate_symbols"]
    full_intraday = qualify_intraday_history(
        database,
        symbols,
        actual_start,
        requested_end,
        BarTimeframe.MINUTES_15,
        detail_limit=max(1_000, len(symbols) * len(preparation.sessions)),
    )
    statuses = intraday_session_statuses(
        full_intraday, symbols, preparation.sessions
    )
    qualification_metadata = {
        "daily": daily,
        "intraday": {
            "15m_candidate_sessions": intraday_summary,
            "15m_full_symbol_sessions": full_intraday.model_dump(
                mode="json", exclude={"details"}
            ),
        },
    }
    prefetch = comparison_intraday_prefetch_metadata(preparation, enabled=False)
    oos = compare_strategies(
        database,
        config,
        actual_start,
        requested_end,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
        preparation=preparation,
        intraday_prefetch=prefetch,
        data_qualification=qualification_metadata,
        intraday_session_statuses=statuses,
        allow_missing_intraday_data=True,
    )
    oos, trade_path_rows = annotate_comparison_trade_paths(database, oos)

    reference_preparation = prepare_strategy_comparison(
        database,
        config,
        reference_start,
        reference_end,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
    )
    reference = compare_strategies(
        database,
        config,
        reference_start,
        reference_end,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
        preparation=reference_preparation,
        intraday_prefetch=comparison_intraday_prefetch_metadata(
            reference_preparation, enabled=False
        ),
        allow_missing_intraday_data=True,
    )
    regression = reference_regression(reference)
    if not regression["passed"]:
        raise ValueError(
            "research-reference regression failed; OOS interpretation is stopped"
        )

    strict_result = BacktestEngine(
        database,
        config,
        screen_source=preparation.screen_source,
        strict_coverage_sensitivity=True,
        intraday_session_statuses=statuses,
        allow_missing_intraday_data=True,
    ).run(
        actual_start,
        requested_end,
        variant=StrategyVariant.FULL,
        preset=PositionManagementPreset.INTRADAY_DYNAMIC,
    )
    strict_result, _ = annotate_trade_path_coverage(database, strict_result)

    cost_comparisons = {"BASE": oos}
    for case, slippage_bps, commission_bps in (
        ("2X_SLIPPAGE", 10, 0),
        ("3X_SLIPPAGE", 15, 0),
        ("COMMISSION_SENSITIVITY", 5, 5),
    ):
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
        cost_comparisons[case] = compare_strategies(
            database,
            cost_config,
            actual_start,
            requested_end,
            comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
            preparation=preparation,
            intraday_prefetch=prefetch,
            data_qualification=qualification_metadata,
            intraday_session_statuses=statuses,
            allow_missing_intraday_data=True,
        )
    config_25bps = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(
                update={"slippage_bps": 25, "commission_bps": 0}
            )
        }
    )
    intraday_25bps = BacktestEngine(
        database,
        config_25bps,
        screen_source=preparation.screen_source,
        intraday_session_statuses=statuses,
        allow_missing_intraday_data=True,
    ).run(
        actual_start,
        requested_end,
        variant=StrategyVariant.FULL,
        preset=PositionManagementPreset.INTRADAY_DYNAMIC,
    )

    result_map = {_label(result): result for result in oos.variants}
    summaries = [strategy_summary(_label(result), result) for result in oos.variants]
    summary_map = {item["strategy"]: item for item in summaries}
    paired = paired_d1_analysis(
        result_map["C/configured"], result_map["D1/C-swing-profit-lock"]
    )
    monthly_rows, monthly_stability = time_analysis(oos, "month")
    quarterly_rows, quarterly_stability = time_analysis(oos, "quarter")
    symbol_rows, concentration, leave_rows = symbol_and_leave_one_out(oos)
    path_cost_rows = path_preserving_cost_stress(oos)
    full_cost_rows = full_cost_stress_rows(cost_comparisons, intraday_25bps)
    uncertainty = {
        "D1/C-swing-profit-lock": bootstrap_uncertainty(
            result_map["D1/C-swing-profit-lock"]
        ),
        "C/intraday-dynamic": bootstrap_uncertainty(
            result_map["C/intraday-dynamic"]
        ),
    }
    intraday_sensitivity = intraday_sensitivity_rows(
        result_map["C/intraday-dynamic"], strict_result
    )
    decisions = classify_validation(
        summary_map,
        paired,
        concentration,
        monthly_stability,
        full_cost_rows,
        intraday_sensitivity,
    )
    return ExtendedValidationBundle(
        requested_start=requested_start,
        requested_end=requested_end,
        actual_start=actual_start,
        reference_start=reference_start,
        reference_end=reference_end,
        daily_qualification=daily,
        candidate_discovery=discovery,
        intraday_qualification={
            "candidate_sessions": intraday_summary,
            "full_symbol_sessions": full_intraday.model_dump(mode="json"),
        },
        intraday_gap_rows=intraday_gaps,
        oos=oos,
        reference=reference,
        reference_regression=regression,
        strict_full_session=strict_result,
        cost_comparisons=cost_comparisons,
        intraday_25bps=intraday_25bps,
        strategy_summaries=summaries,
        paired_d1=paired,
        monthly_rows=monthly_rows,
        quarterly_rows=quarterly_rows,
        time_stability={
            "monthly": monthly_stability,
            "quarterly": quarterly_stability,
        },
        symbol_rows=symbol_rows,
        concentration=concentration,
        leave_one_out_rows=leave_rows,
        path_cost_rows=path_cost_rows,
        full_cost_rows=full_cost_rows,
        uncertainty=uncertainty,
        trade_path_rows=trade_path_rows,
        intraday_sensitivity=intraday_sensitivity,
        decisions=decisions,
    )


def export_extended_validation(
    bundle: ExtendedValidationBundle, output_directory: Path
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    period = f"{bundle.requested_start}_{bundle.requested_end}"
    paths = {
        "summary_csv": output_directory / f"extended_validation_{period}_summary.csv",
        "summary_json": output_directory / f"extended_validation_{period}_summary.json",
        "positions": output_directory / f"extended_validation_{period}_positions.csv",
        "execution_legs": output_directory
        / f"extended_validation_{period}_execution_legs.csv",
        "monthly": output_directory / "extended_validation_monthly.csv",
        "quarterly": output_directory / "extended_validation_quarterly.csv",
        "symbol_concentration": output_directory
        / "extended_validation_symbol_concentration.csv",
        "leave_one_symbol_out": output_directory
        / "extended_validation_leave_one_symbol_out.csv",
        "cost_stress": output_directory / "extended_validation_cost_stress.csv",
        "path_preserving_cost_stress": output_directory
        / "extended_validation_path_preserving_cost_stress.csv",
        "uncertainty": output_directory / "extended_validation_uncertainty.json",
        "data_qualification": output_directory
        / "extended_validation_data_qualification.json",
        "intraday_missing_data": output_directory
        / "extended_validation_intraday_missing_data.json",
        "intraday_gap_manifest": output_directory
        / "extended_validation_intraday_gap_manifest.csv",
        "trade_path_coverage": output_directory
        / "extended_validation_trade_path_coverage.csv",
        "intraday_sensitivity": output_directory
        / "extended_validation_intraday_sensitivity.csv",
        "reference_regression": output_directory
        / f"research_reference_regression_{bundle.reference_start}_{bundle.reference_end}.json",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Extended validation export already exists: {existing[0]}")
    candidate_coverage = bundle.intraday_qualification["candidate_sessions"]
    intraday_result = next(
        result
        for result in bundle.oos.variants
        if result.position_management_preset is PositionManagementPreset.INTRADAY_DYNAMIC
    )
    incomplete_paths = sum(
        position.trade_path_complete is not True
        for position in intraday_result.positions
    )
    insufficient_warmups = sum(
        position.warmup_sufficient is not True
        for position in intraday_result.positions
    )
    candidate_sessions = candidate_coverage["candidate_symbol_sessions"]
    entry_bar_missing = candidate_coverage["missing_0930_bars"]
    data_classification = data_qualification_classification(
        candidate_sessions=candidate_sessions,
        entry_bar_missing=entry_bar_missing,
        incomplete_trade_paths=incomplete_paths,
        insufficient_warmups=insufficient_warmups,
    )
    economic_classification = bundle.decisions["C/intraday-dynamic"].replace(
        " OOS", ""
    )
    summary_payload = {
        "report_type": "extended_out_of_sample_validation",
        "requested_oos": [
            bundle.requested_start.isoformat(),
            bundle.requested_end.isoformat(),
        ],
        "actual_qualified_oos": [
            bundle.actual_start.isoformat(),
            bundle.requested_end.isoformat(),
        ],
        "strategies": bundle.strategy_summaries,
        "paired_d1": bundle.paired_d1,
        "time_stability": bundle.time_stability,
        "symbol_concentration": bundle.concentration,
        "benchmark": bundle.oos.variants[0].benchmark.model_dump(mode="json"),
        "decisions": bundle.decisions,
        "reference_regression_passed": bundle.reference_regression["passed"],
        "intraday_validation_qualified": bundle.intraday_qualification[
            "candidate_sessions"
        ]["full_oos_intraday_qualified"],
        "full_session_strict_qualified": candidate_coverage[
            "full_session_strict_qualified"
        ],
        "candidate_entry_opportunity_qualified": entry_bar_missing == 0,
        "executed_trade_path_qualified": incomplete_paths == 0,
        "indicator_warmup_qualified": insufficient_warmups == 0,
        "economic_support_classification": economic_classification,
        "data_qualification_classification": data_classification,
    }
    _atomic_text(paths["summary_json"], json.dumps(summary_payload, indent=2))
    _atomic_csv(
        paths["summary_csv"],
        bundle.strategy_summaries,
        list(bundle.strategy_summaries[0]) if bundle.strategy_summaries else ["strategy"],
    )
    position_rows = [
        {"strategy": _label(result), **position.model_dump(mode="json")}
        for result in bundle.oos.variants
        for position in result.positions
    ]
    leg_rows = [
        {"strategy": _label(result), **trade.model_dump(mode="json")}
        for result in bundle.oos.variants
        for trade in result.trades
    ]
    _atomic_csv(paths["positions"], position_rows, ["strategy", *_position_fields()])
    _atomic_csv(
        paths["execution_legs"], leg_rows, ["strategy", *_trade_fields()]
    )
    period_fields = [
        "strategy",
        "period",
        "positions",
        "net_pnl_contribution",
        "return_contribution",
        "average_position_return",
        "win_rate",
        "profit_factor",
    ]
    _atomic_csv(paths["monthly"], bundle.monthly_rows, period_fields)
    _atomic_csv(paths["quarterly"], bundle.quarterly_rows, period_fields)
    _atomic_csv(
        paths["symbol_concentration"],
        bundle.symbol_rows,
        list(bundle.symbol_rows[0]) if bundle.symbol_rows else ["strategy", "symbol"],
    )
    _atomic_csv(
        paths["leave_one_symbol_out"],
        bundle.leave_one_out_rows,
        list(bundle.leave_one_out_rows[0])
        if bundle.leave_one_out_rows
        else ["strategy", "scenario"],
    )
    _atomic_csv(
        paths["cost_stress"],
        bundle.full_cost_rows,
        list(bundle.full_cost_rows[0]),
    )
    _atomic_csv(
        paths["path_preserving_cost_stress"],
        bundle.path_cost_rows,
        list(bundle.path_cost_rows[0]),
    )
    _atomic_text(paths["uncertainty"], json.dumps(bundle.uncertainty, indent=2))
    _atomic_text(
        paths["data_qualification"],
        json.dumps(
            {
                "daily": bundle.daily_qualification,
                "intraday": bundle.intraday_qualification,
                "candidate_discovery": bundle.candidate_discovery,
            },
            indent=2,
        ),
    )
    _atomic_text(
        paths["intraday_missing_data"],
        json.dumps(
            {
                "local_only": True,
                "automatic_repair_attempted": False,
                "summary": bundle.intraday_qualification["candidate_sessions"],
                "affected_sessions": [
                    row
                    for row in bundle.intraday_gap_rows
                    if row["status"] != "COMPLETE"
                ],
            },
            indent=2,
        ),
    )
    _atomic_csv(
        paths["intraday_gap_manifest"],
        [row for row in bundle.intraday_gap_rows if row["status"] != "COMPLETE"],
        list(bundle.intraday_gap_rows[0])
        if bundle.intraday_gap_rows
        else ["symbol", "execution_session"],
    )
    _atomic_csv(
        paths["trade_path_coverage"],
        bundle.trade_path_rows,
        list(bundle.trade_path_rows[0])
        if bundle.trade_path_rows
        else ["strategy", "position_id"],
    )
    _atomic_csv(
        paths["intraday_sensitivity"],
        bundle.intraday_sensitivity,
        _field_union(bundle.intraday_sensitivity),
    )
    _atomic_text(
        paths["reference_regression"],
        json.dumps(
            {
                **bundle.reference_regression,
                "requested_start": bundle.reference_start.isoformat(),
                "requested_end": bundle.reference_end.isoformat(),
                "strategies": [
                    strategy_summary(_label(result), result)
                    for result in bundle.reference.variants
                ],
            },
            indent=2,
        ),
    )
    return paths


def format_extended_validation_summary(bundle: ExtendedValidationBundle) -> str:
    summaries = {item["strategy"]: item for item in bundle.strategy_summaries}
    intraday = summaries["C/intraday-dynamic"]
    trade_path = next(
        row
        for row in bundle.intraday_sensitivity
        if row["sensitivity"] == "STRICT_TRADE_PATH"
    )
    costs = {
        (row["cost_case"], row["strategy"]): row for row in bundle.full_cost_rows
    }
    intraday_10bps = costs[("2X_SLIPPAGE", "C/intraday-dynamic")][
        "total_return"
    ]
    intraday_15bps = costs[("3X_SLIPPAGE", "C/intraday-dynamic")][
        "total_return"
    ]
    return "\n".join(
        [
            "Extended validation complete.",
            f"OOS requested: {bundle.requested_start} -> {bundle.requested_end}",
            f"OOS actual qualified: {bundle.actual_start} -> {bundle.requested_end}",
            f"Reference regression: {'PASS' if bundle.reference_regression['passed'] else 'FAIL'}",
            "OOS results:",
            *[
                f"  {label}: return={_format_percent(summaries[label]['total_return'])} "
                f"PF={_format_number(summaries[label]['position_profit_factor'])} "
                f"positions={summaries[label]['positions']}"
                for label in (
                    "C/configured",
                    "D1/C-swing-profit-lock",
                    "D2/C-swing-runner",
                    "B/configured",
                    "D1/B-swing-profit-lock",
                    "C/intraday-dynamic",
                )
            ],
            "Intraday:",
            f"  native={_format_percent(intraday['total_return'])} "
            f"trade-path-complete={_format_percent(trade_path['total_return'])} "
            f"10bps={_format_percent(intraday_10bps)} "
            f"15bps={_format_percent(intraday_15bps)}",
            "Validation:",
            f"  D1/C: {bundle.decisions['D1/C']}",
            f"  C/intraday-dynamic: {bundle.decisions['C/intraday-dynamic']}",
            f"  D2/C: {bundle.decisions['D2/C']}",
            f"  D1/B: {bundle.decisions['D1/B']}",
        ]
    )


def _label(result: BacktestResult) -> str:
    return research_strategy_label(
        result.strategy_variant, result.position_management_preset
    )


def _expected_15m_timestamps(session: date) -> list[datetime]:
    opening, closing = regular_session_bounds(session)
    timestamps: list[datetime] = []
    current = opening
    while current < closing:
        timestamps.append(current)
        current += timedelta(minutes=15)
    return timestamps


def _position_key_json(item: tuple[str, date, datetime | None]) -> list[str | None]:
    return [
        item[0],
        item[1].isoformat(),
        item[2].isoformat() if item[2] is not None else None,
    ]


def _period_keys(start: date, end: date, period: str) -> list[str]:
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        key = (
            f"{year:04d}-{month:02d}"
            if period == "month"
            else f"{year:04d}-Q{(month - 1) // 3 + 1}"
        )
        if not keys or keys[-1] != key:
            keys.append(key)
        month += 1
        if month == 13:
            year += 1
            month = 1
    return keys


def _posthoc_position_row(
    strategy: str,
    scenario: str,
    positions: list[BacktestPosition],
    initial_capital: float,
    *,
    removed_symbols: list[str],
) -> dict[str, Any]:
    pnls = [item.net_pnl for item in positions]
    return {
        "strategy": strategy,
        "scenario": scenario,
        "removed_symbols": removed_symbols,
        "remaining_positions": len(positions),
        "sum_net_pnl": sum(pnls),
        "approximate_return_contribution": sum(pnls) / initial_capital,
        "profit_factor": _profit_factor(pnls),
        "average_position_return": mean(
            [item.position_return for item in positions]
        )
        if positions
        else None,
        "post_hoc_only": True,
    }


def _profit_factor(pnls: list[float]) -> float | None:
    profit = sum(item for item in pnls if item > 0)
    loss = abs(sum(item for item in pnls if item < 0))
    return profit / loss if loss else None


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    index = (len(values) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _cost_row(case: str, label: str, result: BacktestResult) -> dict[str, Any]:
    return {
        "cost_case": case,
        "strategy": label,
        "slippage_bps": result.configuration["backtest"]["slippage_bps"],
        "commission_bps": result.configuration["backtest"]["commission_bps"],
        "total_return": result.metrics.total_return,
        "profit_factor": result.position_metrics.position_profit_factor,
        "expectancy": result.metrics.expectancy_per_trade,
        "max_drawdown": result.metrics.maximum_drawdown,
        "modeled_costs": sum(
            item.slippage + item.transaction_cost for item in result.trades
        ),
        "turnover": result.metrics.portfolio_turnover,
        "path_preserving_cost_stress": False,
    }


def _sensitivity_row(
    sensitivity: str,
    positions: tuple[BacktestPosition, ...] | list[BacktestPosition],
    initial_capital: float,
) -> dict[str, Any]:
    pnls = [item.net_pnl for item in positions]
    return {
        "strategy": "C/intraday-dynamic",
        "sensitivity": sensitivity,
        "ranked_strategy": False,
        "positions": len(positions),
        "trade_path_complete_positions": sum(
            item.trade_path_complete is True for item in positions
        ),
        "total_return": sum(pnls) / initial_capital,
        "max_drawdown": None,
        "profit_factor": _profit_factor(pnls),
        "expectancy": mean(pnls) if pnls else None,
    }


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3%}"


def _format_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _field_union(rows: list[dict[str, Any]]) -> list[str]:
    """Return deterministic CSV fields even when diagnostic rows are sparse."""

    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    return fields
