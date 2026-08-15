from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.data.qualification import (
    QualificationStatus,
    qualify_daily_history,
    qualify_intraday_history,
)
from trading_system.models.market_data import BarTimeframe, MarketDataBar


def _bar(
    symbol: str,
    timestamp: datetime,
    timeframe: BarTimeframe,
) -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=100,
        trade_count=10,
        vwap=Decimal("100.5"),
    )


def _session_bars(
    symbol: str,
    session: date,
    timeframe: BarTimeframe = BarTimeframe.MINUTES_15,
) -> list[MarketDataBar]:
    opening, closing = regular_session_bounds(session)
    current = opening
    bars = []
    while current < closing:
        bars.append(_bar(symbol, current, timeframe))
        current += timeframe.duration
    return bars


def _database(tmp_path, bars: list[MarketDataBar]) -> Database:
    database = Database(tmp_path / "qualification.sqlite3")
    database.initialize()
    database.upsert_bars(bars)
    return database


def test_daily_qualification_finds_internal_gap_despite_coverage_metadata(tmp_path) -> None:
    sessions = (date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6))
    database = _database(
        tmp_path,
        [
            _bar(
                "AAA",
                datetime.combine(session, datetime.min.time(), tzinfo=UTC),
                BarTimeframe.DAY_1,
            )
            for session in (sessions[0], sessions[2])
        ],
    )
    database.set_sync_value(
        "daily_history_coverage",
        "AAA",
        {
            "start": "2026-05-01T00:00:00+00:00",
            "end_exclusive": "2026-05-08T00:00:00+00:00",
            "feed": "iex",
            "adjustment": "all",
        },
    )

    result = qualify_daily_history(
        database, ["AAA"], sessions[0], sessions[-1], warmup_sessions=0
    )

    assert result.missing_sessions == 1
    assert result.symbols_with_internal_gaps == 1
    assert result.internal_missing_sessions == 1
    assert result.edge_or_lifecycle_missing_sessions == 0
    assert result.coverage_metadata_mismatches == 1
    assert result.details[0].session == sessions[1]
    assert result.details[0].status is QualificationStatus.MISSING_SESSION
    assert result.details[0].internal_gap


def test_daily_qualification_accepts_early_close_as_one_daily_session(tmp_path) -> None:
    early_close = date(2025, 11, 28)
    database = _database(
        tmp_path,
        [
            _bar(
                "AAA",
                datetime.combine(early_close, datetime.min.time(), tzinfo=UTC),
                BarTimeframe.DAY_1,
            )
        ],
    )

    result = qualify_daily_history(
        database, ["AAA"], early_close, early_close, warmup_sessions=0
    )

    assert result.calendar_sessions == 1
    assert result.complete_sessions == 1
    assert result.missing_sessions == 0


def test_complete_15m_regular_session_is_deterministic(tmp_path) -> None:
    session = date(2026, 7, 1)
    database = _database(tmp_path, _session_bars("AAA", session))

    first = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )
    second = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )

    assert first == second
    assert first.expected_bars == first.bars_present == 26
    assert first.complete_sessions == 1
    assert first.details == ()


def test_missing_15m_session_is_reported(tmp_path) -> None:
    session = date(2026, 7, 1)
    database = _database(tmp_path, [])

    result = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )

    assert result.missing_sessions == 1
    assert result.missing_bars == 26
    assert result.details[0].status is QualificationStatus.MISSING_SESSION


@pytest.mark.parametrize("missing_indexes", [(5,), (5, 6, 17)])
def test_missing_15m_bars_are_structurally_partial_but_cause_unknown(
    tmp_path, missing_indexes
) -> None:
    session = date(2026, 7, 1)
    bars = _session_bars("AAA", session)
    database = _database(
        tmp_path, [bar for index, bar in enumerate(bars) if index not in missing_indexes]
    )
    before = database.bar_count(BarTimeframe.MINUTES_15)

    result = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )

    detail = result.details[0]
    assert result.partial_sessions == 1
    assert result.unknown_market_activity_sessions == 1
    assert result.missing_bars == len(missing_indexes)
    assert detail.structural_status is QualificationStatus.PARTIAL_SESSION
    assert detail.status is QualificationStatus.UNKNOWN_MARKET_ACTIVITY
    assert len(detail.missing_timestamps) == len(missing_indexes)
    assert database.bar_count(BarTimeframe.MINUTES_15) == before


def test_early_close_15m_uses_official_short_session(tmp_path) -> None:
    session = date(2025, 11, 28)
    database = _database(tmp_path, _session_bars("AAA", session))

    result = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )

    assert result.expected_bars == 14
    assert result.complete_sessions == 1


def test_after_hours_bar_is_extra_not_regular_coverage(tmp_path) -> None:
    session = date(2026, 7, 1)
    regular = _session_bars("AAA", session)
    _, closing = regular_session_bounds(session)
    database = _database(
        tmp_path,
        [*regular, _bar("AAA", closing + timedelta(minutes=15), BarTimeframe.MINUTES_15)],
    )

    result = qualify_intraday_history(
        database, ["AAA"], session, session, BarTimeframe.MINUTES_15
    )

    assert result.bars_present == 26
    assert result.extra_bars == 1
    assert result.extra_or_invalid_sessions == 1
    assert result.details[0].status is QualificationStatus.EXTRA_OR_INVALID
