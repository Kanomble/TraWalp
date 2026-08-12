"""NYSE regular-session boundaries for completed daily-bar analysis."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import exchange_calendars as xcals
import pandas as pd


def latest_completed_trading_session(now: datetime | None = None) -> date:
    """Return the latest XNYS session whose official regular close has passed."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_utc = current.astimezone(UTC)
    calendar = xcals.get_calendar("XNYS")
    candidate = calendar.date_to_session(pd.Timestamp(current_utc.date()), direction="previous")
    if candidate.date() == current_utc.date() and current_utc < calendar.session_close(candidate):
        candidate = calendar.previous_session(candidate)
    return candidate.date()


def effective_trading_session(requested_as_of: date, now: datetime | None = None) -> date:
    """Resolve a requested research date to a completed XNYS regular session."""

    current = now or datetime.now(UTC)
    completed = latest_completed_trading_session(current)
    capped = min(requested_as_of, completed)
    calendar = xcals.get_calendar("XNYS")
    return calendar.date_to_session(pd.Timestamp(capped), direction="previous").date()


def full_history_request_window(session: date, trading_days: int) -> tuple[datetime, datetime]:
    """Return the explicit Alpaca [start, end) window for a completed-session history."""

    calendar = xcals.get_calendar("XNYS")
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
