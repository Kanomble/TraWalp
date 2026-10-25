"""Daily critical-state preflight. Validation never changes economic state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum

from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
from trading_system.models.market_data import BarTimeframe, DailyBar, validate_market_bar


class DataIntegrityMode(StrEnum):
    LEGACY_RESEARCH = "LEGACY_RESEARCH"
    STRICT = "STRICT"


class DataIntegrityError(RuntimeError):
    """Machine-readable session failure, never a trade or a no-trade result."""

    def __init__(
        self,
        code: str,
        *,
        session: date,
        symbol: str | None = None,
        role: str = "SESSION",
        position_entry_session: date | None = None,
        holding_session_number: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.session = session
        self.symbol = symbol
        self.role = role
        self.position_entry_session = position_entry_session
        self.holding_session_number = holding_session_number

    def as_dict(self) -> dict:
        return {
            "status": "DATA_INTEGRITY_FAILURE",
            "code": self.code,
            "session": self.session.isoformat(),
            "symbol": self.symbol,
            "role": self.role,
            "position_entry_session": (
                self.position_entry_session.isoformat() if self.position_entry_session else None
            ),
            "holding_session_number": self.holding_session_number,
        }


def validate_session_position_data(
    session: date,
    *,
    bars: Mapping[str, DailyBar],
    held_positions: Mapping[str, date],
    pending_entries: Mapping[str, date],
    previous_processed_session: date | None,
    now: datetime,
) -> None:
    """Validate an entire completed session BEFORE any economic state transition.

    held_positions maps symbol -> entry session; pending_entries maps symbol ->
    SIGNAL session (execution must be the next official session). The previous
    processed session is required; None explicitly denotes inception only.
    Deterministic failure order: calendar, then sorted held, then sorted pending.
    No prices, counters, orders or journal records are changed here. Callers must
    discard the whole session on error; skipping, deferring and stale marks are
    forbidden. Missing OHLC cannot establish gap/intrabar stop or target outcomes.
    """
    if not trading_sessions_between(session, session):
        raise DataIntegrityError("SESSION_NOT_OFFICIAL", session=session)
    if now.tzinfo is None or now.utcoffset() is None:
        raise DataIntegrityError("SESSION_CLOCK_INVALID", session=session)
    if now.astimezone(UTC) < regular_session_bounds(session)[1]:
        raise DataIntegrityError("SESSION_NOT_COMPLETE", session=session)
    if previous_processed_session is not None and (
        previous_processed_session >= session
        or trading_sessions_between(previous_processed_session, session)
        != [previous_processed_session, session]
    ):
        raise DataIntegrityError("OFFICIAL_SESSION_SEQUENCE_GAP", session=session)

    for symbol in sorted(held_positions):
        entry = held_positions[symbol]
        if entry > session or not trading_sessions_between(entry, entry):
            raise DataIntegrityError(
                "HELD_POSITION_STATE_INVALID", session=session, symbol=symbol, role="HELD_POSITION"
            )
        _validate_bar(
            session,
            symbol,
            bars.get(symbol),
            "HELD_POSITION",
            entry,
            len(trading_sessions_between(entry, session)),
        )
    for symbol in sorted(pending_entries):
        signal = pending_entries[symbol]
        interval = trading_sessions_between(signal, session) if signal < session else []
        if len(interval) != 2 or interval != [signal, session]:
            raise DataIntegrityError(
                "PENDING_ENTRY_SESSION_MISMATCH",
                session=session,
                symbol=symbol,
                role="PENDING_ENTRY",
            )
        _validate_bar(session, symbol, bars.get(symbol), "PENDING_ENTRY")


def _validate_bar(session, symbol, bar, role, entry=None, holding_number=None):
    def fail(suffix):
        raise DataIntegrityError(
            f"{role}_BAR_{suffix}",
            session=session,
            symbol=symbol,
            role=role,
            position_entry_session=entry,
            holding_session_number=holding_number,
        )

    if bar is None:
        fail("MISSING")
    if not isinstance(bar, DailyBar):
        fail("INVALID")
    try:
        # Revalidate even model_construct/model_copy inputs, including finite/positive OHLC.
        checked = DailyBar.model_validate(bar.model_dump())
        validate_market_bar(checked)
        if checked.symbol != symbol or checked.timeframe is not BarTimeframe.DAY_1:
            fail("INVALID")
        # A zero-volume placeholder does not establish executable OHLC observations.
        if checked.volume <= 0:
            fail("INVALID")
    except (ValueError, TypeError, AttributeError):
        fail("INVALID")
    if checked.timestamp.date() != session:
        fail("STALE")
