"""Provider-native intraday coverage helpers shared by simulation and reports."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
from trading_system.models.market_data import BarTimeframe, DailyBar


def expected_native_15m_timestamps(session: date) -> tuple[datetime, ...]:
    opening, closing = regular_session_bounds(session)
    timestamps: list[datetime] = []
    current = opening
    while current < closing:
        timestamps.append(current)
        current += BarTimeframe.MINUTES_15.duration
    return tuple(timestamps)


def warmup_coverage_diagnostics(
    bars: Sequence[DailyBar],
    entry_timestamp: datetime,
    *,
    required_bars: int,
) -> dict[str, Any]:
    """Qualify actual provider bars; structural gaps remain diagnostic only."""

    if required_bars <= 0:
        raise ValueError("required_bars must be positive")
    entry = entry_timestamp.astimezone(UTC)
    prior = sorted(
        (bar for bar in bars if bar.timestamp.astimezone(UTC) < entry),
        key=lambda bar: bar.timestamp,
    )
    selected = prior[-required_bars:]
    actual = {bar.timestamp.astimezone(UTC) for bar in selected}
    expected: set[datetime] = set()
    if selected:
        earliest = selected[0].timestamp.astimezone(UTC)
        latest = selected[-1].timestamp.astimezone(UTC)
        for session in trading_sessions_between(earliest.date(), latest.date()):
            expected.update(
                timestamp
                for timestamp in expected_native_15m_timestamps(session)
                if earliest <= timestamp <= latest
            )
    return {
        "warmup_required_bars": required_bars,
        "warmup_available_native_bars": len(prior),
        "warmup_sufficient": len(prior) >= required_bars,
        "earliest_warmup_timestamp": (
            selected[0].timestamp.astimezone(UTC) if selected else None
        ),
        "latest_pre_entry_warmup_timestamp": (
            selected[-1].timestamp.astimezone(UTC) if selected else None
        ),
        "warmup_expected_timestamp_gap_count": len(expected - actual),
    }


def data_qualification_classification(
    *,
    candidate_sessions: int,
    entry_bar_missing: int,
    incomplete_trade_paths: int,
    insufficient_warmups: int,
) -> str:
    if incomplete_trade_paths or insufficient_warmups:
        return "NOT QUALIFIED"
    if entry_bar_missing == 0:
        return "QUALIFIED"
    provisional_limit = max(1, math.ceil(candidate_sessions * 0.01))
    if entry_bar_missing <= provisional_limit:
        return "PROVISIONALLY QUALIFIED"
    return "NOT QUALIFIED"
