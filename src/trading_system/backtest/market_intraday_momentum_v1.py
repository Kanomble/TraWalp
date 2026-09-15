"""Frozen SPY morning-sign/closing-half-hour hypothesis; pure native-bar logic."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from trading_system.backtest.native_session_grid import (
    expected_native_timestamps,
    validate_native_session,
)
from trading_system.backtest.research_definitions import MARKET_INTRADAY_MOMENTUM_V1 as MOMENTUM_V1
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe


@dataclass(frozen=True, slots=True)
class MomentumSession:
    session: date
    opening: datetime
    closing: datetime
    morning: tuple[datetime, datetime]
    final: tuple[datetime, datetime]

    @property
    def key(self):
        # The generic spool's middle key is simply session T; no Daily signal date.
        return MOMENTUM_V1.symbol, self.session, self.session

    @property
    def required_timestamps(self):
        return self.morning + self.final


def session_definition(session):
    grid = expected_native_timestamps(session, BarTimeframe.MINUTES_15, False)
    opening, closing = regular_session_bounds(session)
    if len(grid) < 4:
        raise ValueError("MARKET_INTRADAY_MOMENTUM_SESSION_TOO_SHORT")
    return MomentumSession(session, opening, closing, grid[:2], grid[-2:])


class MomentumStatus(StrEnum):
    FIRST_30M_UNOBSERVABLE = "FIRST_30M_UNOBSERVABLE"
    NO_SIGNAL_ZERO_FIRST_30M_RETURN = "NO_SIGNAL_ZERO_FIRST_30M_RETURN"
    SIGNAL = "SIGNAL"
    CLOSING_WINDOW_UNOBSERVABLE = "CLOSING_WINDOW_UNOBSERVABLE"
    EXECUTED_FINAL_REGULAR_SESSION_CLOSE = "EXECUTED_FINAL_REGULAR_SESSION_CLOSE"


EVENT_FIELDS = (
    "session",
    "symbol",
    "morning_observable",
    "closing_window_observable",
    "first_30m_open",
    "first_30m_close",
    "first_30m_return",
    "decision_timestamp",
    "signal",
    "direction",
    "scheduled_entry_timestamp",
    "scheduled_exit_timestamp",
    "final_30m_open",
    "final_30m_close",
    "final_30m_raw_return",
    "directional_hit",
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


def _window(candidate, bars, timestamps):
    window = sorted(
        (bar for bar in bars if bar.timestamp in timestamps), key=lambda bar: bar.timestamp
    )
    validate_native_session(
        MOMENTUM_V1.symbol, candidate.session, window, error_prefix="MARKET_INTRADAY_MOMENTUM"
    )
    return window if len(window) == 2 else None


def observe_morning(candidate, bars):
    """No access to prices outside the first two expected native bars."""
    row = dict.fromkeys(EVENT_FIELDS)
    row.update(
        session=candidate.session.isoformat(),
        symbol=MOMENTUM_V1.symbol,
        morning_observable=False,
        closing_window_observable=False,
        signal=False,
        decision_timestamp=(candidate.morning[-1] + timedelta(minutes=15)).isoformat(),
        scheduled_entry_timestamp=candidate.final[0].isoformat(),
        scheduled_exit_timestamp=candidate.closing.isoformat(),
        status=MomentumStatus.FIRST_30M_UNOBSERVABLE,
        reason="Both first-30m native bars required",
    )
    morning = _window(candidate, bars, candidate.morning)
    if morning is None:
        return row
    opening, closing = morning[0].open, morning[1].close
    # Decimal comparisons preserve the exact sign without an epsilon/float rounding band.
    direction = "LONG" if closing > opening else "SHORT" if closing < opening else None
    row.update(
        morning_observable=True,
        first_30m_open=float(opening),
        first_30m_close=float(closing),
        first_30m_return=float((closing - opening) / opening),
        signal=direction is not None,
        direction=direction,
        status=MomentumStatus.SIGNAL
        if direction
        else MomentumStatus.NO_SIGNAL_ZERO_FIRST_30M_RETURN,
        reason="Morning sign frozen until closing window"
        if direction
        else "Exactly zero morning return",
    )
    return row


def simulate_momentum_session(candidate, bars):
    row = observe_morning(candidate, bars)
    if not row["morning_observable"]:
        return row
    final = _window(candidate, bars, candidate.final)
    if final is None:
        if row["signal"]:
            row.update(
                status=MomentumStatus.CLOSING_WINDOW_UNOBSERVABLE,
                reason="Signal retained; both official closing-window bars required",
            )
        return row
    entry_reference, exit_reference = float(final[0].open), float(final[1].close)
    raw_return = exit_reference / entry_reference - 1
    row.update(
        closing_window_observable=True,
        final_30m_open=entry_reference,
        final_30m_close=exit_reference,
        final_30m_raw_return=raw_return,
    )
    if not row["signal"]:
        return row  # retain the paired zero-morning observation for predictive diagnostics
    long = row["direction"] == "LONG"
    slippage = (MOMENTUM_V1.long_slippage_bps if long else MOMENTUM_V1.short_slippage_bps) / 10_000
    if long:
        entry_fill, exit_fill = entry_reference * (1 + slippage), exit_reference * (1 - slippage)
        gross, net = raw_return, exit_fill / entry_fill - 1
        favorable = max(float(bar.high) for bar in final) / entry_reference - 1
        adverse = min(float(bar.low) for bar in final) / entry_reference - 1
    else:
        entry_fill, exit_fill = entry_reference * (1 - slippage), exit_reference * (1 + slippage)
        gross = (entry_reference - exit_reference) / entry_reference
        net = (entry_fill - exit_fill) / entry_fill
        favorable = (entry_reference - min(float(bar.low) for bar in final)) / entry_reference
        adverse = (entry_reference - max(float(bar.high) for bar in final)) / entry_reference
    row.update(
        entry_timestamp=final[0].timestamp.isoformat(),
        entry_reference=entry_reference,
        entry_fill=entry_fill,
        exit_timestamp=candidate.closing.isoformat(),
        exit_reference=exit_reference,
        exit_fill=exit_fill,
        exit_reason="FINAL_REGULAR_SESSION_CLOSE",
        gross_return=gross,
        net_return=net,
        directional_hit=raw_return > 0 if long else raw_return < 0,
        modeled_cost=gross - net,
        MFE=max(0.0, favorable),
        MAE=min(0.0, adverse),
        holding_minutes=(candidate.closing - final[0].timestamp).total_seconds() / 60,
        status=MomentumStatus.EXECUTED_FINAL_REGULAR_SESSION_CLOSE,
        reason="Frozen morning direction; official final-30m execution",
    )
    return row
