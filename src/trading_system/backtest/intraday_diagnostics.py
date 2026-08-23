"""Causally safe provider-native intraday forward diagnostics.

These helpers run only after a position is final.  They never mutate orders,
positions, portfolio state, or strategy decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.backtest import BacktestPosition
from trading_system.models.market_data import BarTimeframe, DailyBar

_DURATION = BarTimeframe.MINUTES_15.duration
_FIXED_WINDOWS = {
    "next_1_native_bar": 1,
    "next_2_native_bars": 2,
    "next_4_native_bars": 4,
}


def add_intraday_forward_diagnostics(
    position: BacktestPosition,
    database: Database,
) -> BacktestPosition:
    """Attach exact-continuity 15m diagnostics from the next safe bar onward.

    The exit bar is excluded for every exit type.  This conservative convention
    avoids using unknowable post-stop high/low ordering and is also used for
    open/close exits so all exit reasons share one auditable reference frame.
    """

    if position.exit_timestamp is None or position.entry_timestamp is None:
        return position
    opening, closing = regular_session_bounds(position.exit_timestamp.date())
    start = position.exit_timestamp + _DURATION
    semantic = "next_canonical_15m_bar_after_exit_bar; exit_bar_excluded"
    if start < opening:
        start = opening
    available = {
        bar.timestamp: bar
        for bar in database.bars_between(
            [position.symbol],
            max(start, opening),
            closing,
            timeframe=BarTimeframe.MINUTES_15,
        )
        if opening <= bar.timestamp < closing
    }
    diagnostics: dict[str, dict[str, Any]] = {}
    for name, count in _FIXED_WINDOWS.items():
        expected = tuple(start + index * _DURATION for index in range(count))
        diagnostics[name] = _window_diagnostics(position, expected, available, closing)
    remainder: list[datetime] = []
    current = start
    while current < closing:
        remainder.append(current)
        current += _DURATION
    diagnostics["remainder_regular_session"] = _window_diagnostics(
        position, tuple(remainder), available, closing
    )
    return position.model_copy(
        update={
            "intraday_forward_start_semantic": semantic,
            "intraday_forward_diagnostics": diagnostics,
        }
    )


def _window_diagnostics(
    position: BacktestPosition,
    expected: tuple[datetime, ...],
    available: dict[datetime, DailyBar],
    closing: datetime,
) -> dict[str, Any]:
    out_of_session = tuple(timestamp for timestamp in expected if timestamp >= closing)
    missing = tuple(timestamp for timestamp in expected if timestamp not in available)
    resolved = bool(expected) and not missing and not out_of_session
    base: dict[str, Any] = {
        "exact_canonical_continuity_required": True,
        "resolved": resolved,
        "expected_timestamps": [timestamp.isoformat() for timestamp in expected],
        "missing_timestamps": [timestamp.isoformat() for timestamp in missing],
        "failure_reason": None,
        "post_exit_return": None,
        "post_exit_mfe": None,
        "post_exit_mae": None,
        "counterfactual_hold_return": None,
        "counterfactual_hold_mfe": None,
        "counterfactual_hold_mae": None,
    }
    if not expected:
        base["failure_reason"] = "no_causally_safe_regular_session_bar_remaining"
        return base
    if out_of_session:
        base["failure_reason"] = "horizon_extends_beyond_regular_session"
        return base
    if missing:
        base["failure_reason"] = "missing_canonical_native_bar"
        return base
    bars = [available[timestamp] for timestamp in expected]
    base.update(_returns(bars, position.exit_reference_price, "post_exit"))
    base.update(
        _returns(bars, position.entry_reference_price, "counterfactual_hold")
    )
    return base


def _returns(bars: list[DailyBar], reference: float, prefix: str) -> dict[str, float]:
    return {
        f"{prefix}_return": float(bars[-1].close) / reference - 1,
        f"{prefix}_mfe": max(float(bar.high) for bar in bars) / reference - 1,
        f"{prefix}_mae": min(float(bar.low) for bar in bars) / reference - 1,
    }
