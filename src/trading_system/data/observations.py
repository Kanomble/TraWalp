"""Local qualification of an observed Daily bar; storage permits placeholders."""

from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe, validate_market_bar

CURRENT_OBSERVATION_CONTRACT = "current-session-observation/v1"


def current_session_observation(bar, session) -> str:
    """No stale fallback, inferred sessions, synthetic prices or provider access."""
    try:
        regular_session_bounds(session)
    except ValueError:
        return "SESSION_NOT_OFFICIAL"
    if bar is None:
        return "OBSERVATION_ABSENT"
    if bar.timeframe != BarTimeframe.DAY_1 or bar.timestamp.date() != session:
        return "OBSERVATION_WRONG_SESSION"
    try:
        validate_market_bar(bar)
        if any(
            not value.is_finite() or value <= 0
            for value in (bar.open, bar.high, bar.low, bar.close)
        ):
            return "OBSERVATION_INVALID"
        if bar.volume <= 0:
            return "OBSERVATION_ZERO_VOLUME"
    except (AttributeError, TypeError, ValueError):
        return "OBSERVATION_INVALID"
    return "OBSERVATION_PRESENT"
