"""Two completed native opening bars; decision and execution are separate observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe, DailyBar

F_INTRADAY_ENTRY_RESEARCH_FAMILY = "research-f-intraday-entry-quality"


@dataclass(frozen=True, slots=True)
class EntryQualityPreset:
    research_id: str
    label: str
    opening_weakness_veto: bool = False


F_INTRADAY_ENTRY_VARIANTS = (
    EntryQualityPreset("F-INTRADAY-ENTRY-I0", "F-entry-control"),
    EntryQualityPreset("F-INTRADAY-ENTRY-I1", "F-entry-opening-weakness-veto", True),
)


class EntryQualityStatus(StrEnum):
    PASSED = "PASSED"
    VETO = "OPENING_WEAKNESS_VETO"
    UNAVAILABLE = "INTRADAY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class OpeningDecision:
    status: EntryQualityStatus
    decision_timestamp: datetime
    last_15m_close: float | None = None
    session_vwap_to_date: float | None = None
    previous_daily_close: float | None = None
    reason: str | None = None


def opening_weakness_decision(bars, session: date, previous_daily_close) -> OpeningDecision:
    opening, _ = regular_session_bounds(session)
    decision_at = opening + timedelta(minutes=30)
    # Provider timestamps mark starts. Only these two completed bars can influence the veto.
    selected = [
        b
        for b in bars
        if b.timeframe is BarTimeframe.MINUTES_15
        and b.timestamp in {opening, opening + timedelta(minutes=15)}
    ]
    selected.sort(key=lambda b: b.timestamp)
    if len(selected) != 2 or len({b.timestamp for b in selected}) != 2:
        return OpeningDecision(
            EntryQualityStatus.UNAVAILABLE, decision_at, reason="missing_or_duplicate_opening_bars"
        )
    if previous_daily_close is None or previous_daily_close <= 0:
        return OpeningDecision(
            EntryQualityStatus.UNAVAILABLE, decision_at, reason="missing_previous_daily_close"
        )
    # Native volume-weighted prices only; HLC3 is not silently substituted for true VWAP.
    if sum(b.volume for b in selected) <= 0 or any(
        b.vwap is None for b in selected if b.volume > 0
    ):
        return OpeningDecision(
            EntryQualityStatus.UNAVAILABLE, decision_at, reason="missing_native_vwap_or_volume"
        )
    vwap = sum(float(b.vwap) * b.volume for b in selected if b.volume > 0) / sum(
        b.volume for b in selected
    )
    close = float(selected[-1].close)
    status = (
        EntryQualityStatus.VETO
        if close < previous_daily_close and close < vwap
        else EntryQualityStatus.PASSED
    )
    return OpeningDecision(status, decision_at, close, vwap, previous_daily_close)


def next_executable_bar(bars, decision: OpeningDecision, session: date) -> DailyBar | None:
    if decision.status is not EntryQualityStatus.PASSED:
        return None
    _, closing = regular_session_bounds(session)
    return next(
        iter(
            sorted(
                (
                    b
                    for b in bars
                    if b.timeframe is BarTimeframe.MINUTES_15
                    and decision.decision_timestamp <= b.timestamp < closing
                ),
                key=lambda b: b.timestamp,
            )
        ),
        None,
    )


def entry_session_range(daily_bar: DailyBar, bars, entry_bar: DailyBar) -> DailyBar:
    """Daily management over the actually observable post-entry range, never pre-entry lows.

    This is an ephemeral Daily observation, not a stored/synthetic native intraday bar.
    Qualification requires complete native coverage of the remainder of the entry session.
    The existing conservative Daily stop/target priority is applied once to this range.
    """
    _, closing = regular_session_bounds(entry_bar.timestamp.date())
    remaining = sorted(
        (b for b in bars if entry_bar.timestamp <= b.timestamp < closing), key=lambda b: b.timestamp
    )
    return daily_bar.model_copy(
        update={
            "open": entry_bar.open,
            "high": max(daily_bar.close, max(b.high for b in remaining)),
            "low": min(daily_bar.close, min(b.low for b in remaining)),
            "close": daily_bar.close,
        }
    )


def missing_session_timestamps(bars, session: date) -> list[datetime]:
    opening, closing = regular_session_bounds(session)
    present = {b.timestamp for b in bars if b.timeframe is BarTimeframe.MINUTES_15}
    expected = [
        opening + timedelta(minutes=15 * i)
        for i in range(int((closing - opening).total_seconds() // 900))
    ]
    return [timestamp for timestamp in expected if timestamp not in present]
