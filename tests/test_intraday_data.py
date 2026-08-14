import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    intraday_warmup_start,
    is_regular_session_timestamp,
    regular_session_bounds,
)
from trading_system.data.sync import DataSynchronizer
from trading_system.models.market_data import BarTimeframe, MarketDataBar


def _bar(
    timestamp: datetime,
    timeframe: BarTimeframe,
    *,
    symbol: str = "AAPL",
    close: str = "101",
) -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=100,
        trade_count=10,
        vwap=Decimal("100.5"),
    )


def test_legacy_daily_table_migrates_without_data_loss(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE daily_bars (
        symbol TEXT NOT NULL,timestamp TEXT NOT NULL,open TEXT NOT NULL,high TEXT NOT NULL,
        low TEXT NOT NULL,close TEXT NOT NULL,volume INTEGER NOT NULL,trade_count INTEGER,
        vwap TEXT,PRIMARY KEY(symbol,timestamp))"""
    )
    connection.execute(
        "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?)",
        ("AAPL", "2026-07-01T00:00:00+00:00", "100", "102", "99", "101", 100, 10, "100.5"),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()

    bars = database.bars_available_as_of("AAPL", date(2026, 7, 1))
    assert len(bars) == 1
    assert bars[0].timeframe is BarTimeframe.DAY_1
    with database.connect() as migrated:
        assert migrated.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 1
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 1


def test_all_timeframes_share_timestamp_without_collision_and_upsert_idempotently(
    tmp_path,
) -> None:
    database = Database(tmp_path / "bars.sqlite3")
    database.initialize()
    timestamp = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    bars = [_bar(timestamp, timeframe) for timeframe in BarTimeframe]

    first = database.upsert_bars_with_stats(bars)
    second = database.upsert_bars_with_stats(bars)
    corrected = database.upsert_bars_with_stats(
        [_bar(timestamp, BarTimeframe.MINUTES_15, close="101.5")]
    )

    assert first["bars_inserted"] == 4
    assert second["bars_inserted"] == second["bars_updated"] == 0
    assert second["bars_unchanged"] == 4
    assert corrected["bars_updated"] == 1
    for timeframe in BarTimeframe:
        selected = database.bars_between(
            ["AAPL"],
            timestamp,
            timestamp + timedelta(days=1),
            timeframe=timeframe,
        )
        assert len(selected) == 1
        assert selected[0].timeframe is timeframe


class IntradayAlpaca:
    def __init__(self, bars: list[MarketDataBar]) -> None:
        self.available = bars
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, BarTimeframe]] = []
        self.last_bar_diagnostics = {"invalid_bars": 0}

    def bars(self, symbols, start, end, *, timeframe, batch_size):
        normalized = BarTimeframe(timeframe)
        selected_symbols = tuple(symbols)
        self.calls.append((selected_symbols, start, end, normalized))
        return [
            bar
            for bar in self.available
            if bar.symbol in selected_symbols
            and bar.timeframe is normalized
            and start <= bar.timestamp < end
        ]


def test_intraday_sync_is_incremental_chunked_and_timeframe_isolated(tmp_path) -> None:
    database = Database(tmp_path / "sync.sqlite3")
    database.initialize()
    opening = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    provider_bars = [
        _bar(opening + timedelta(minutes=offset), timeframe)
        for timeframe in (
            BarTimeframe.MINUTES_5,
            BarTimeframe.MINUTES_15,
            BarTimeframe.HOUR_1,
        )
        for offset in (0, 15, 30, 45)
    ]
    alpaca = IntradayAlpaca(provider_bars)
    sync = DataSynchronizer(
        database,
        alpaca,  # type: ignore[arg-type]
        None,
        intraday_overlap_bars=2,
        intraday_symbol_batch_size=1,
        intraday_request_window_days=1,
    )
    end = opening + timedelta(hours=2)

    first = sync.sync_intraday(
        ["AAPL"],
        ["5m", "15m", "1h"],
        opening,
        end,
    )
    call_count = len(alpaca.calls)
    second = sync.sync_intraday(
        ["AAPL"],
        ["5m", "15m", "1h"],
        opening,
        end,
    )

    assert first["bars_inserted"] == 12
    assert second["bars_inserted"] == second["bars_updated"] == 0
    assert second["bars_unchanged"] > 0
    assert len(alpaca.calls) == call_count * 2
    assert {call[3] for call in alpaca.calls} == {
        BarTimeframe.MINUTES_5,
        BarTimeframe.MINUTES_15,
        BarTimeframe.HOUR_1,
    }


def test_sync_regular_session_filter_and_request_window_paging(tmp_path) -> None:
    database = Database(tmp_path / "sessions.sqlite3")
    database.initialize()
    premarket = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    regular = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    alpaca = IntradayAlpaca(
        [
            _bar(premarket, BarTimeframe.MINUTES_15),
            _bar(regular, BarTimeframe.MINUTES_15),
        ]
    )
    sync = DataSynchronizer(
        database,
        alpaca,  # type: ignore[arg-type]
        None,
        intraday_request_window_days=1,
    )

    result = sync.sync_intraday(
        ["AAPL"],
        ["15m"],
        datetime(2026, 6, 30, tzinfo=UTC),
        datetime(2026, 7, 3, tzinfo=UTC),
        incremental=False,
        extended_hours=False,
    )

    assert result["request_batches"] == 3
    assert result["bars_downloaded"] == 2
    assert result["bars_inserted"] == 1
    stored = database.bars_available_as_of(
        "AAPL", date(2026, 7, 1), timeframe=BarTimeframe.MINUTES_15
    )
    assert [bar.timestamp for bar in stored] == [regular]

    extended = sync.sync_intraday(
        ["AAPL"],
        ["15m"],
        datetime(2026, 6, 30, tzinfo=UTC),
        datetime(2026, 7, 3, tzinfo=UTC),
        incremental=False,
        extended_hours=True,
    )
    assert extended["bars_inserted"] == 1
    stored = database.bars_available_as_of(
        "AAPL", date(2026, 7, 1), timeframe=BarTimeframe.MINUTES_15
    )
    assert [bar.timestamp for bar in stored] == [premarket, regular]


def test_xnys_regular_bounds_are_dst_aware() -> None:
    winter_open, _ = regular_session_bounds(date(2026, 1, 5))
    summer_open, _ = regular_session_bounds(date(2026, 7, 1))

    assert winter_open.hour == 14
    assert summer_open.hour == 13
    assert is_regular_session_timestamp(summer_open)
    assert not is_regular_session_timestamp(summer_open - timedelta(minutes=1))


def test_intraday_warmup_start_precedes_requested_session_and_covers_bars() -> None:
    requested = date(2026, 7, 1)
    start = intraday_warmup_start(
        requested, BarTimeframe.MINUTES_15, 50, extended_hours=False
    )

    requested_open, _ = regular_session_bounds(requested)
    assert start < requested_open
    assert is_regular_session_timestamp(start)


class FailsMiddleWindowOnce(IntradayAlpaca):
    def __init__(self, bars: list[MarketDataBar]) -> None:
        super().__init__(bars)
        self.failed = False

    def bars(self, symbols, start, end, *, timeframe, batch_size):
        if not self.failed and start.date() == date(2026, 7, 2):
            self.failed = True
            raise TimeoutError("transient provider timeout")
        return super().bars(
            symbols, start, end, timeframe=timeframe, batch_size=batch_size
        )


def test_interrupted_backfill_resumes_from_last_durable_window(tmp_path) -> None:
    database = Database(tmp_path / "resume.sqlite3")
    database.initialize()
    bars = [
        _bar(
            datetime(2026, 7, day, 13, 30, tzinfo=UTC),
            BarTimeframe.MINUTES_15,
        )
        for day in (1, 2, 6)
    ]
    provider = FailsMiddleWindowOnce(bars)
    sync = DataSynchronizer(
        database,
        provider,  # type: ignore[arg-type]
        None,
        intraday_request_window_days=1,
        intraday_overlap_bars=1,
    )
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 7, tzinfo=UTC)

    first = sync.sync_intraday(["AAPL"], ["15m"], start, end, incremental=False)
    second = sync.sync_intraday(["AAPL"], ["15m"], start, end, incremental=True)

    assert first["errors"] == 1
    assert second["errors"] == 0
    assert second["bars_inserted"] == 2
    stored = database.bars_between(
        ["AAPL"], start, end, timeframe=BarTimeframe.MINUTES_15
    )
    assert [bar.timestamp.date() for bar in stored] == [
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 6),
    ]


def test_invalid_bar_is_counted_and_not_persisted(tmp_path) -> None:
    database = Database(tmp_path / "invalid.sqlite3")
    database.initialize()
    invalid = _bar(datetime(2026, 7, 1, 13, 30, tzinfo=UTC), BarTimeframe.MINUTES_5)
    invalid = invalid.model_copy(update={"high": Decimal("98")})

    result = database.upsert_bars_with_stats([invalid])

    assert result["invalid_bars"] == 1
    assert result["bars_inserted"] == 0
    assert database.bar_inventory() == []
