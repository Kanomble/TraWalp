"""No-lookahead 15-minute state machines for the Phase-G F4 research preset."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.technical.indicators import ema

F4_EMA_PERIOD = 20
F4_STOP_DISTANCE_PCT = 0.0075
_BAR_DURATION = BarTimeframe.MINUTES_15.duration


@dataclass(frozen=True, slots=True)
class FirstHourPullbackPlan:
    entry_timestamp: datetime | None
    confirmation_bar: DailyBar | None
    failure_reason: str | None
    diagnostics: dict[str, Any]

    @property
    def executable(self) -> bool:
        return self.entry_timestamp is not None and self.failure_reason is None


def plan_first_hour_pullback(
    session: date,
    session_bars: Sequence[DailyBar],
    prior_native_bars: Sequence[DailyBar],
) -> FirstHourPullbackPlan:
    """Plan the first eligible F4 entry using completed canonical bars only."""

    opening, closing = regular_session_bounds(session)
    regular = {
        bar.timestamp: bar
        for bar in session_bars
        if opening <= bar.timestamp < closing
    }
    first_hour_timestamps = tuple(opening + index * _BAR_DURATION for index in range(4))
    first_hour = [regular.get(timestamp) for timestamp in first_hour_timestamps]
    diagnostics: dict[str, Any] = {
        "opening_bar_timestamp": opening.isoformat(),
        "opening_open": None,
        "opening_high": None,
        "opening_low": None,
        "opening_close": None,
        "opening_ema20": None,
        "opening_above_ema": None,
        "first_hour_complete": all(bar is not None for bar in first_hour),
        "first_hour_open": None,
        "first_hour_high": None,
        "first_hour_low": None,
        "first_hour_close": None,
        "ema20_at_1030": None,
        "pullback_candidate_count": 0,
        "pullback_candidate_timestamp": None,
        "pullback_candidate_low": None,
        "pullback_confirmation_timestamp": None,
        "pullback_confirmation_close": None,
        "pullback_confirmed": False,
        "intended_entry_timestamp": None,
        "actual_entry_timestamp": None,
        "execution_failure_reason": None,
    }
    if any(bar is None for bar in first_hour):
        diagnostics["execution_failure_reason"] = "incomplete_first_hour"
        return FirstHourPullbackPlan(None, None, "incomplete_first_hour", diagnostics)

    complete_first_hour = [bar for bar in first_hour if bar is not None]
    opening_bar = complete_first_hour[0]
    diagnostics.update(
        {
            "opening_bar_timestamp": opening_bar.timestamp.isoformat(),
            "opening_open": float(opening_bar.open),
            "opening_high": float(opening_bar.high),
            "opening_low": float(opening_bar.low),
            "opening_close": float(opening_bar.close),
            "first_hour_open": float(opening_bar.open),
            "first_hour_high": max(float(bar.high) for bar in complete_first_hour),
            "first_hour_low": min(float(bar.low) for bar in complete_first_hour),
            "first_hour_close": float(complete_first_hour[-1].close),
        }
    )
    prior = sorted(
        (bar for bar in prior_native_bars if bar.timestamp < opening),
        key=lambda bar: bar.timestamp,
    )
    opening_ema = _latest_ema([*prior, opening_bar])
    diagnostics["opening_ema20"] = opening_ema
    if opening_ema is None:
        diagnostics["execution_failure_reason"] = "insufficient_ema_warmup"
        return FirstHourPullbackPlan(None, None, "insufficient_ema_warmup", diagnostics)
    diagnostics["opening_above_ema"] = float(opening_bar.close) >= opening_ema
    diagnostics["ema20_at_1030"] = _latest_ema([*prior, *complete_first_hour])
    if not diagnostics["opening_above_ema"]:
        diagnostics["execution_failure_reason"] = "opening_below_ema"
        return FirstHourPullbackPlan(None, None, "opening_below_ema", diagnostics)

    post_hour = [
        regular[timestamp]
        for timestamp in sorted(regular)
        if timestamp >= opening + 4 * _BAR_DURATION
    ]
    previous = complete_first_hour[-1]
    candidate: DailyBar | None = None
    for bar in post_hour:
        contiguous = bar.timestamp == previous.timestamp + _BAR_DURATION
        if not contiguous:
            candidate = None
            previous = bar
            continue
        if candidate is not None:
            if float(bar.low) >= float(candidate.low) and float(bar.close) > float(
                candidate.close
            ):
                intended = bar.timestamp + _BAR_DURATION
                diagnostics.update(
                    {
                        "pullback_candidate_timestamp": candidate.timestamp.isoformat(),
                        "pullback_candidate_low": float(candidate.low),
                        "pullback_confirmation_timestamp": bar.timestamp.isoformat(),
                        "pullback_confirmation_close": float(bar.close),
                        "pullback_confirmed": True,
                        "intended_entry_timestamp": intended.isoformat(),
                    }
                )
                if intended not in regular:
                    diagnostics["execution_failure_reason"] = (
                        "missing_pullback_execution_bar"
                    )
                    return FirstHourPullbackPlan(
                        None,
                        bar,
                        "missing_pullback_execution_bar",
                        diagnostics,
                    )
                return FirstHourPullbackPlan(intended, bar, None, diagnostics)
            candidate = None
        if float(bar.low) < float(previous.low):
            candidate = bar
            diagnostics["pullback_candidate_count"] += 1
            diagnostics["pullback_candidate_timestamp"] = bar.timestamp.isoformat()
            diagnostics["pullback_candidate_low"] = float(bar.low)
        previous = bar

    diagnostics["execution_failure_reason"] = "no_confirmed_pullback"
    return FirstHourPullbackPlan(None, None, "no_confirmed_pullback", diagnostics)


def _latest_ema(bars: Sequence[DailyBar]) -> float | None:
    if len(bars) < F4_EMA_PERIOD:
        return None
    values = pd.Series([float(bar.close) for bar in bars], dtype=float)
    result = ema(values, F4_EMA_PERIOD).iloc[-1]
    return None if pd.isna(result) else float(result)


@dataclass(slots=True)
class SwingHighDetector:
    """One-bar confirmation detector that never bridges a provider gap."""

    previous_bar: DailyBar
    candidate_bar: DailyBar | None = None
    confirmation_bar: DailyBar | None = None
    intended_exit_timestamp: datetime | None = None
    execution_bar_missing: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def observe_completed_bar(self, bar: DailyBar) -> datetime | None:
        if self.intended_exit_timestamp is not None:
            return self.intended_exit_timestamp
        if bar.timestamp != self.previous_bar.timestamp + _BAR_DURATION:
            self.candidate_bar = None
            self.previous_bar = bar
            return None
        if self.candidate_bar is not None:
            if float(bar.high) <= float(self.candidate_bar.high) and float(
                bar.close
            ) < float(self.candidate_bar.close):
                self.confirmation_bar = bar
                self.intended_exit_timestamp = bar.timestamp + _BAR_DURATION
                self.diagnostics.update(
                    {
                        "swing_high_candidate_timestamp": (
                            self.candidate_bar.timestamp.isoformat()
                        ),
                        "swing_high_candidate_high": float(self.candidate_bar.high),
                        "swing_high_confirmation_timestamp": bar.timestamp.isoformat(),
                        "swing_high_confirmed": True,
                        "intended_exit_timestamp": (
                            self.intended_exit_timestamp.isoformat()
                        ),
                    }
                )
                self.previous_bar = bar
                return self.intended_exit_timestamp
            self.candidate_bar = None
        if float(bar.high) > float(self.previous_bar.high):
            self.candidate_bar = bar
            self.diagnostics.update(
                {
                    "swing_high_candidate_timestamp": bar.timestamp.isoformat(),
                    "swing_high_candidate_high": float(bar.high),
                }
            )
        self.previous_bar = bar
        return None

    def exit_due(self, timestamp: datetime) -> bool:
        return bool(
            self.intended_exit_timestamp is not None
            and timestamp >= self.intended_exit_timestamp
        )
