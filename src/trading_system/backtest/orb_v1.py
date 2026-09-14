"""The single frozen ORB hypothesis. Pure native-bar simulation, independent of F."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from enum import StrEnum
from functools import lru_cache

from trading_system.backtest.research_definitions import ORB_V1
from trading_system.data.intraday_remediation import _expected_timestamps
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe, validate_market_bar


class OrbStatus(StrEnum):
    NO_BREAKOUT = "NO_BREAKOUT"
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
    NO_ENTRY_NO_NEXT_BAR = "NO_ENTRY_NO_NEXT_BAR"
    INVALID_RISK_GEOMETRY = "INVALID_RISK_GEOMETRY"
    EXECUTED_STOP = "EXECUTED_STOP"
    EXECUTED_SESSION_CLOSE = "EXECUTED_SESSION_CLOSE"
    PROVIDER_CONFIRMED_ABSENT = "PROVIDER_CONFIRMED_ABSENT"


@dataclass(frozen=True, slots=True)
class OrbCandidate:
    session: date
    symbol: str
    previous_session: date
    daily_universe_rank: int
    previous_close: float
    average_dollar_volume_20d: float

    @property
    def key(self):
        return self.symbol, self.previous_session, self.session


EVENT_FIELDS = (
    "session",
    "symbol",
    "daily_universe_rank",
    "previous_close",
    "average_dollar_volume_20d",
    "opening_bar_timestamp",
    "opening_range_high",
    "opening_range_low",
    "opening_range_width",
    "opening_range_width_pct",
    "breakout_bar_timestamp",
    "breakout_close",
    "breakout_distance",
    "breakout_distance_pct",
    "decision_timestamp",
    "entry_timestamp",
    "entry_reference_price",
    "entry_fill_price",
    "stop_price",
    "initial_R",
    "exit_timestamp",
    "exit_reason",
    "exit_reference_price",
    "exit_fill_price",
    "gross_return",
    "net_return",
    "return_R",
    "MFE",
    "MAE",
    "holding_minutes",
    "modeled_execution_cost",
    "execution_cost_per_share",
    "exit_timestamp_semantics",
    "signal_status",
    "status",
    "reason",
)


def event_for(candidate: OrbCandidate) -> dict:
    row = dict.fromkeys(EVENT_FIELDS)
    row.update({key: value for key, value in asdict(candidate).items() if key in row})
    row["session"] = candidate.session.isoformat()
    return row


@lru_cache(maxsize=2048)
def expected_native_timestamps(session, timeframe, extended_hours):
    """Immutable, bounded pure-calendar cache; contains no research outcomes or prices."""
    return _expected_timestamps(session, timeframe, extended_hours=extended_hours)


def validate_native_session(symbol, session, bars):
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
            raise ValueError(f"ORB_INVALID_NATIVE_BAR: {symbol} {session} {bar.timestamp}")
        seen.add(bar.timestamp)


def simulate_orb_session(candidate: OrbCandidate, bars) -> dict:
    """One consumed signal attempt. No database, providers, capital allocation or F logic.

    Callers qualify complete regular-session coverage before simulation. Sparse inputs
    remain useful for testing next-*actual*-bar timing and the no-next-bar event.
    """
    bars = sorted(bars, key=lambda bar: bar.timestamp)
    validate_native_session(candidate.symbol, candidate.session, bars)
    opening, _ = regular_session_bounds(candidate.session)
    if not bars or bars[0].timestamp != opening:
        raise ValueError("ORB_OPENING_RANGE_UNAVAILABLE")
    row = event_for(candidate)
    first = bars[0]
    high, stop = float(first.high), float(first.low)
    row.update(
        opening_bar_timestamp=first.timestamp.isoformat(),
        opening_range_high=high,
        opening_range_low=stop,
        opening_range_width=high - stop,
        opening_range_width_pct=(high - stop) / stop,
        stop_price=stop,
    )
    signal_index = next(
        (index for index in range(1, len(bars)) if float(bars[index].close) > high), None
    )
    if signal_index is None:
        row.update(status=OrbStatus.NO_BREAKOUT, reason="No completed close above opening range")
        return row
    signal = bars[signal_index]
    row.update(
        signal_status=OrbStatus.BREAKOUT_CONFIRMED,
        breakout_bar_timestamp=signal.timestamp.isoformat(),
        breakout_close=float(signal.close),
        breakout_distance=float(signal.close) - high,
        breakout_distance_pct=(float(signal.close) - high) / high,
        decision_timestamp=(signal.timestamp + timedelta(minutes=15)).isoformat(),
    )
    if signal_index + 1 == len(bars):
        row.update(status=OrbStatus.NO_ENTRY_NO_NEXT_BAR, reason="No subsequent native session bar")
        return row
    entry_bar = bars[signal_index + 1]
    entry_reference = float(entry_bar.open)
    risk = entry_reference - stop
    row.update(
        entry_timestamp=entry_bar.timestamp.isoformat(),
        entry_reference_price=entry_reference,
        initial_R=risk,
    )
    if risk <= 0:
        row.update(status=OrbStatus.INVALID_RISK_GEOMETRY, reason="Entry open <= opening range low")
        return row
    slippage = ORB_V1.slippage_bps / 10_000
    entry_fill = entry_reference * (1 + slippage)
    row["entry_fill_price"] = entry_fill
    favorable = adverse = entry_reference
    for bar in bars[signal_index + 1 :]:
        bar_open = float(bar.open)
        if bar_open <= stop or float(bar.low) <= stop:
            exit_reference = bar_open if bar_open <= stop else stop
            # OHLC does not reveal the order of a stop-bar high and stop touch.
            # Include only known pre-exit open/fill, never later stop-bar extremes.
            favorable = max(favorable, bar_open, exit_reference)
            adverse = min(adverse, bar_open, exit_reference)
            exit_timestamp = bar.timestamp
            timestamp_semantics = (
                "ACTUAL_BAR_OPEN" if bar_open <= stop else "STOP_BAR_START_TIME_ESTIMATE"
            )
            exit_reason = "STOP"
            break
        favorable = max(favorable, float(bar.high))
        adverse = min(adverse, float(bar.low))
    else:
        bar = bars[-1]
        exit_reference = float(bar.close)
        exit_timestamp = bar.timestamp + timedelta(minutes=15)
        timestamp_semantics = "FINAL_NATIVE_BAR_COMPLETION"
        exit_reason = "SESSION_CLOSE"
    exit_fill = exit_reference * (1 - slippage)
    gross_return = exit_reference / entry_reference - 1
    net_return = exit_fill / entry_fill - 1
    row.update(
        exit_timestamp=exit_timestamp.isoformat(),
        exit_reason=exit_reason,
        exit_reference_price=exit_reference,
        exit_fill_price=exit_fill,
        gross_return=gross_return,
        net_return=net_return,
        return_R=(exit_fill - entry_fill) / risk,
        MFE=favorable / entry_reference - 1,
        MAE=adverse / entry_reference - 1,
        holding_minutes=(exit_timestamp - entry_bar.timestamp).total_seconds() / 60,
        modeled_execution_cost=gross_return - net_return,
        execution_cost_per_share=(entry_fill - entry_reference) + (exit_reference - exit_fill),
        exit_timestamp_semantics=timestamp_semantics,
        status=OrbStatus.EXECUTED_STOP
        if exit_reason == "STOP"
        else OrbStatus.EXECUTED_SESSION_CLOSE,
        reason="Frozen opening range stop"
        if exit_reason == "STOP"
        else "Final regular-session close",
    )
    return row
