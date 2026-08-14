"""NYSE regular-session boundaries for completed daily-bar analysis."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from trading_system.models.market_data import BarTimeframe


@lru_cache(maxsize=1)
def _xnys():
    return xcals.get_calendar("XNYS")


def latest_completed_trading_session(now: datetime | None = None) -> date:
    """Return the latest XNYS session whose official regular close has passed."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_utc = current.astimezone(UTC)
    calendar = _xnys()
    candidate = calendar.date_to_session(pd.Timestamp(current_utc.date()), direction="previous")
    if candidate.date() == current_utc.date() and current_utc < calendar.session_close(candidate):
        candidate = calendar.previous_session(candidate)
    return candidate.date()


def effective_trading_session(requested_as_of: date, now: datetime | None = None) -> date:
    """Resolve a requested research date to a completed XNYS regular session."""

    current = now or datetime.now(UTC)
    completed = latest_completed_trading_session(current)
    capped = min(requested_as_of, completed)
    calendar = _xnys()
    return calendar.date_to_session(pd.Timestamp(capped), direction="previous").date()


def trading_sessions_between(start: date, end: date) -> list[date]:
    """Return official XNYS sessions in an inclusive research interval."""

    if start > end:
        raise ValueError("start must not be after end")
    calendar = _xnys()
    return [session.date() for session in calendar.sessions_in_range(start, end)]


def full_history_request_window(session: date, trading_days: int) -> tuple[datetime, datetime]:
    """Return the explicit Alpaca [start, end) window for a completed-session history."""

    calendar = _xnys()
    end_session = calendar.date_to_session(pd.Timestamp(session), direction="previous")
    sessions = calendar.sessions_in_range(
        pd.Timestamp(session - timedelta(days=max(500, trading_days * 2))), end_session
    )
    if len(sessions) < trading_days:
        raise ValueError(f"Calendar returned only {len(sessions)} sessions")
    start_session = sessions[-trading_days].date()
    return (
        datetime.combine(start_session, time.min, tzinfo=UTC),
        datetime.combine(session + timedelta(days=1), time.min, tzinfo=UTC),
    )


def regular_session_bounds(session: date) -> tuple[datetime, datetime]:
    """Return DST-aware XNYS regular-session [open, close) boundaries in UTC."""

    calendar = _xnys()
    normalized = calendar.date_to_session(pd.Timestamp(session), direction="none")
    opening = calendar.session_open(normalized).to_pydatetime().astimezone(UTC)
    closing = calendar.session_close(normalized).to_pydatetime().astimezone(UTC)
    return opening, closing


def intraday_session_bounds(
    session: date, *, extended_hours: bool = False
) -> tuple[datetime, datetime]:
    """Return the provider query window for one US equity session in UTC.

    Alpaca's extended-hours equity coverage is treated as 04:00 through 20:00 New
    York time.  Converting localized boundaries instead of applying fixed UTC
    offsets keeps the window correct across daylight-saving transitions.
    """

    if not extended_hours:
        return regular_session_bounds(session)
    eastern = ZoneInfo("America/New_York")
    opening = datetime.combine(session, time(4), tzinfo=eastern).astimezone(UTC)
    closing = datetime.combine(session, time(20), tzinfo=eastern).astimezone(UTC)
    return opening, closing


def intraday_warmup_start(
    start_session: date,
    timeframe: BarTimeframe | str,
    warmup_bars: int,
    *,
    extended_hours: bool = False,
) -> datetime:
    """Return a conservative session-aligned start covering indicator warmup bars."""

    if warmup_bars < 1:
        raise ValueError("warmup_bars must be positive")
    normalized = BarTimeframe(timeframe)
    if not normalized.intraday:
        raise ValueError("intraday warmup requires 5m, 15m, or 1h")
    minutes = 16 * 60 if extended_hours else 6 * 60 + 30
    duration_minutes = max(int(normalized.duration.total_seconds() // 60), 1)
    bars_per_session = (minutes + duration_minutes - 1) // duration_minutes
    sessions_needed = (warmup_bars + bars_per_session - 1) // bars_per_session
    calendar = _xnys()
    anchor = calendar.date_to_session(pd.Timestamp(start_session), direction="previous")
    sessions = calendar.sessions_window(anchor, -(sessions_needed + 1))
    first_session = sessions[0].date()
    return intraday_session_bounds(
        first_session, extended_hours=extended_hours
    )[0]


def is_regular_session_timestamp(timestamp: datetime) -> bool:
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    current = timestamp.astimezone(UTC)
    calendar = _xnys()
    try:
        session = calendar.minute_to_session(pd.Timestamp(current), direction="none")
    except ValueError:
        return False
    opening = calendar.session_open(session).to_pydatetime().astimezone(UTC)
    closing = calendar.session_close(session).to_pydatetime().astimezone(UTC)
    return opening <= current < closing
