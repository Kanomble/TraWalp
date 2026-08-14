"""Compact diagnostics for historical Daily coverage and warmup integrity."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.market_data import BarTimeframe


def warmup_coverage_at(
    database: Database,
    symbols: Iterable[str],
    backtest_start: date,
    required_sessions: int,
) -> dict[str, int | float | str]:
    """Measure Daily bars strictly before the first backtest session."""

    if required_sessions < 1:
        raise ValueError("required_sessions must be positive")
    normalized = sorted({symbol.upper() for symbol in symbols if symbol.strip()})
    counts = database.bar_counts_before(normalized, backtest_start)
    values = [counts.get(symbol, 0) for symbol in normalized]
    enough = sum(value >= required_sessions for value in values)
    almost = sum(250 <= value < required_sessions for value in values)
    short = sum(0 < value < 250 for value in values)
    missing = sum(value == 0 for value in values)
    return {
        "backtest_start": backtest_start.isoformat(),
        "required_prior_sessions": required_sessions,
        "universe_symbols": len(normalized),
        "symbols_with_required_history": enough,
        "percentage_with_required_history": enough / len(normalized) if normalized else 0.0,
        "symbols_with_250_to_required_minus_1": almost,
        "symbols_with_less_than_250": short,
        "symbols_with_no_prior_history": missing,
    }


def boundary_integrity_check(
    database: Database,
    symbols: Iterable[str],
    boundary: date,
    *,
    extreme_jump_ratio: float = 5.0,
) -> dict[str, object]:
    """Check a compact window around an old/new Daily-history transition."""

    if extreme_jump_ratio <= 1:
        raise ValueError("extreme_jump_ratio must be greater than one")
    normalized = sorted({symbol.upper() for symbol in symbols if symbol.strip()})
    start = datetime.combine(boundary - timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(boundary + timedelta(days=11), datetime.min.time(), tzinfo=UTC)
    bars = database.bars_between(
        normalized, start, end, timeframe=BarTimeframe.DAY_1
    )
    grouped = defaultdict(list)
    for bar in bars:
        grouped[bar.symbol].append(bar)

    official = trading_sessions_between(boundary - timedelta(days=10), boundary)
    prior_sessions = [session for session in official if session < boundary]
    expected_previous = prior_sessions[-1] if prior_sessions else None
    boundary_is_session = boundary in official
    duplicate_timestamps = 0
    unsorted_symbols = 0
    pairs_checked = 0
    missing_previous = 0
    missing_boundary = 0
    extreme_jumps: list[dict[str, object]] = []
    for symbol, symbol_bars in grouped.items():
        timestamps = [bar.timestamp for bar in symbol_bars]
        duplicate_timestamps += len(timestamps) - len(set(timestamps))
        if timestamps != sorted(timestamps):
            unsorted_symbols += 1
        before = [bar for bar in symbol_bars if bar.timestamp.date() < boundary]
        after = [bar for bar in symbol_bars if bar.timestamp.date() >= boundary]
        if not before or not after:
            continue
        pairs_checked += 1
        previous = before[-1]
        following = after[0]
        if expected_previous is not None and previous.timestamp.date() != expected_previous:
            missing_previous += 1
        if boundary_is_session and following.timestamp.date() != boundary:
            missing_boundary += 1
        ratio = max(float(previous.close), float(following.close)) / min(
            float(previous.close), float(following.close)
        )
        if ratio >= extreme_jump_ratio and len(extreme_jumps) < 20:
            extreme_jumps.append(
                {
                    "symbol": symbol,
                    "previous_session": previous.timestamp.date().isoformat(),
                    "boundary_session": following.timestamp.date().isoformat(),
                    "close_ratio": round(ratio, 6),
                }
            )
    return {
        "boundary": boundary.isoformat(),
        "symbols_requested": len(normalized),
        "symbols_with_transition_pair": pairs_checked,
        "duplicate_timestamps": duplicate_timestamps,
        "unsorted_symbols": unsorted_symbols,
        "missing_expected_previous_session": missing_previous,
        "missing_expected_boundary_session": missing_boundary,
        "extreme_adjustment_jumps": len(extreme_jumps),
        "extreme_adjustment_jump_samples": extreme_jumps,
    }
