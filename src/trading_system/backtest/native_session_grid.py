"""Cached regular native timestamp grids and strict validation; no strategy rules."""

from functools import lru_cache

from trading_system.data.intraday_remediation import _expected_timestamps
from trading_system.models.market_data import BarTimeframe, validate_market_bar


@lru_cache(maxsize=2048)
def expected_native_timestamps(session, timeframe, extended_hours):
    """Immutable, bounded pure-calendar cache; contains no research outcomes or prices."""
    return _expected_timestamps(session, timeframe, extended_hours=extended_hours)


def validate_native_session(symbol, session, bars, *, error_prefix="NATIVE"):
    """Reject invalid/duplicate/off-grid bars; never repair or resample them."""
    expected = frozenset(expected_native_timestamps(session, BarTimeframe.MINUTES_15, False))
    seen = set()
    for bar in bars:
        validate_market_bar(bar)
        if (
            bar.symbol != symbol
            or bar.timeframe != BarTimeframe.MINUTES_15
            or bar.timestamp not in expected
            or bar.timestamp in seen
        ):
            raise ValueError(
                f"{error_prefix}_INVALID_NATIVE_BAR: {symbol} {session} {bar.timestamp}"
            )
        seen.add(bar.timestamp)
