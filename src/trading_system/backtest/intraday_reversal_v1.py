"""Independent frozen first-hour reversal hypothesis; pure signal and fill logic."""

from datetime import date, datetime, timedelta
from enum import StrEnum
from statistics import mean, median, pstdev

from trading_system.backtest.native_session_grid import (
    expected_native_timestamps,
    validate_native_session,
)
from trading_system.backtest.research_definitions import INTRADAY_REVERSAL_V1 as REVERSAL_V1
from trading_system.models.market_data import BarTimeframe


class ReversalStatus(StrEnum):
    FIRST_HOUR_UNOBSERVABLE = "FIRST_HOUR_UNOBSERVABLE"
    SESSION_UNOBSERVABLE_CROSS_SECTION = "SESSION_UNOBSERVABLE_CROSS_SECTION"
    NOT_SELECTED = "NOT_SELECTED"
    SIGNAL = "SIGNAL"
    NO_ENTRY_NO_NEXT_BAR = "NO_ENTRY_NO_NEXT_BAR"
    PROVIDER_CONFIRMED_ABSENT = "PROVIDER_CONFIRMED_ABSENT"
    EXECUTED_SESSION_CLOSE = "EXECUTED_SESSION_CLOSE"


EVENT_FIELDS = (
    "session",
    "symbol",
    "daily_liquidity_rank",
    "cross_section_size",
    "cross_section_rank",
    "cross_section_percentile",
    "first_hour_open",
    "first_hour_close",
    "first_hour_return",
    "decision_timestamp",
    "signal",
    "entry_timestamp",
    "entry_reference",
    "entry_fill",
    "exit_timestamp",
    "exit_reference",
    "exit_fill",
    "exit_reason",
    "gross_return",
    "net_return",
    "modeled_cost",
    "MFE",
    "MAE",
    "holding_minutes",
    "status",
    "reason",
)
SESSION_FIELDS = (
    "session",
    "eligible_symbol_sessions",
    "observable_first_hour_symbol_sessions",
    "cross_section_size",
    "status",
    "signals",
    "executed_trades",
    "mean_first_hour_return",
    "median_first_hour_return",
    "std_first_hour_return",
)


def rank_first_hour(candidates, native_by_symbol):
    """Rank only four completed first-hour bars; never inspect subsequent prices.

    Whole-session structural/OHLC validation belongs to coverage preparation. This
    decision function intentionally has no dependency on later-session availability.
    """
    if not candidates:
        return [], None
    session = candidates[0].session
    if any(candidate.session != session for candidate in candidates):
        raise ValueError("REVERSAL_MIXED_SESSIONS")
    grid = expected_native_timestamps(session, BarTimeframe.MINUTES_15, False)
    required = grid[: REVERSAL_V1.first_hour_bars]
    decision = required[-1] + timedelta(minutes=15)
    rows = []
    for candidate in candidates:
        row = dict.fromkeys(EVENT_FIELDS)
        row.update(
            session=session.isoformat(),
            symbol=candidate.symbol,
            daily_liquidity_rank=candidate.daily_universe_rank,
            decision_timestamp=decision.isoformat(),
            signal=False,
            status=ReversalStatus.FIRST_HOUR_UNOBSERVABLE,
            reason="All four first-hour native bars are required",
        )
        first = [
            bar for bar in native_by_symbol.get(candidate.symbol, ()) if bar.timestamp in required
        ]
        validate_native_session(candidate.symbol, session, first, error_prefix="REVERSAL")
        if len(required) == REVERSAL_V1.first_hour_bars and len(first) == len(required):
            first.sort(key=lambda bar: bar.timestamp)
            opening, close = float(first[0].open), float(first[-1].close)
            row.update(
                first_hour_open=opening,
                first_hour_close=close,
                first_hour_return=close / opening - 1,
            )
        rows.append(row)
    observable = sorted(
        (row for row in rows if row["first_hour_return"] is not None),
        key=lambda row: (row["first_hour_return"], row["symbol"]),
    )
    size = len(observable)
    qualified = size >= REVERSAL_V1.minimum_cross_section
    count = max(1, size * REVERSAL_V1.signal_percent // 100) if qualified else 0
    for row in rows:
        row["cross_section_size"] = size
        if not qualified:
            row.update(
                status=ReversalStatus.SESSION_UNOBSERVABLE_CROSS_SECTION,
                reason="Fewer than 80 observable first-hour instruments",
            )
    for rank, row in enumerate(observable, 1):
        row.update(cross_section_rank=rank, cross_section_percentile=rank / size)
        if qualified:
            row.update(
                signal=rank <= count,
                status=ReversalStatus.SIGNAL if rank <= count else ReversalStatus.NOT_SELECTED,
                reason="Bottom 10% first-hour rank" if rank <= count else "Outside bottom 10%",
            )
    returns = [row["first_hour_return"] for row in observable]
    diagnostic = {
        "session": session.isoformat(),
        "eligible_symbol_sessions": len(candidates),
        "observable_first_hour_symbol_sessions": size,
        "cross_section_size": size,
        "status": "CROSS_SECTION_QUALIFIED"
        if qualified
        else ReversalStatus.SESSION_UNOBSERVABLE_CROSS_SECTION,
        "signals": count,
        "executed_trades": 0,
        "mean_first_hour_return": mean(returns) if returns else None,
        "median_first_hour_return": median(returns) if returns else None,
        "std_first_hour_return": pstdev(returns) if returns else None,
    }
    return rows, diagnostic


def execute_signal(event, bars, *, outcome_observable=True):
    """Next actual native open, then final native close. No management or retry.

    Local validation requires full-session coverage for an executable outcome.
    Confirmed partial absence does not remove a first-hour observation from ranking.
    Sparse inputs are supported by this pure function for native-timing tests.
    """
    row = dict(event)
    if not row["signal"]:
        return row
    bars = sorted(bars, key=lambda bar: bar.timestamp)
    validate_native_session(
        row["symbol"], date.fromisoformat(row["session"]), bars, error_prefix="REVERSAL"
    )
    decision = datetime.fromisoformat(row["decision_timestamp"])
    holding = [bar for bar in bars if bar.timestamp >= decision]
    if not holding:
        row.update(status=ReversalStatus.NO_ENTRY_NO_NEXT_BAR, reason="No next native session bar")
        return row
    if not outcome_observable:
        row.update(
            status=ReversalStatus.PROVIDER_CONFIRMED_ABSENT,
            reason="Signal retained; complete native outcome unavailable",
        )
        return row
    entry, final = holding[0], holding[-1]
    entry_reference, exit_reference = float(entry.open), float(final.close)
    slip = REVERSAL_V1.slippage_bps / 10_000
    entry_fill, exit_fill = entry_reference * (1 + slip), exit_reference * (1 - slip)
    gross, net = exit_reference / entry_reference - 1, exit_fill / entry_fill - 1
    exit_timestamp = final.timestamp + timedelta(minutes=15)
    row.update(
        entry_timestamp=entry.timestamp.isoformat(),
        entry_reference=entry_reference,
        entry_fill=entry_fill,
        exit_timestamp=exit_timestamp.isoformat(),
        exit_reference=exit_reference,
        exit_fill=exit_fill,
        exit_reason="SESSION_CLOSE",
        gross_return=gross,
        net_return=net,
        modeled_cost=gross - net,
        MFE=max(0.0, max(float(bar.high) for bar in holding) / entry_reference - 1),
        MAE=min(0.0, min(float(bar.low) for bar in holding) / entry_reference - 1),
        holding_minutes=(exit_timestamp - entry.timestamp).total_seconds() / 60,
        status=ReversalStatus.EXECUTED_SESSION_CLOSE,
        reason="Final regular native close",
    )
    return row
