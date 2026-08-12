from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    effective_trading_session,
    latest_completed_trading_session,
)
from trading_system.models.market_data import DailyBar
from trading_system.strategy.screener import Screener


def test_latest_completed_session_before_and_after_regular_close() -> None:
    morning = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    after_close = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)
    assert latest_completed_trading_session(morning) == date(2026, 8, 7)
    assert latest_completed_trading_session(after_close) == date(2026, 8, 10)


def test_effective_session_resolves_weekends_and_caps_future_dates() -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    assert effective_trading_session(date(2026, 8, 9), now) == date(2026, 8, 7)
    assert effective_trading_session(date(2026, 8, 10), now) == date(2026, 8, 7)


def test_market_debug_excludes_current_incomplete_daily_bar(tmp_path) -> None:
    database = Database(tmp_path / "market.sqlite3")
    database.initialize()
    common = {
        "symbol": "MSFT",
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "volume": 1000,
    }
    database.upsert_bars(
        [
            DailyBar(
                **common,
                timestamp=datetime(2026, 8, 7, 4, 0, tzinfo=UTC),
                close=Decimal("101"),
            ),
            # Simulates a provider exposing a still-incomplete current-session daily bar.
            DailyBar(
                **common,
                timestamp=datetime(2026, 8, 10, 4, 0, tzinfo=UTC),
                close=Decimal("150"),
            ),
        ]
    )
    load_settings.cache_clear()
    debug = Screener(database, load_settings().strategy).debug_market(
        "MSFT",
        date(2026, 8, 10),
        now=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    )
    assert debug.effective_market_session == date(2026, 8, 7)
    assert debug.actual_latest_bar_session == date(2026, 8, 7)
    assert debug.latest_completed_close == 101
    assert debug.bar_count == 1


def test_screen_report_as_of_is_effective_completed_session(tmp_path) -> None:
    database = Database(tmp_path / "empty.sqlite3")
    database.initialize()
    load_settings.cache_clear()
    report = Screener(database, load_settings().strategy).run(
        date(2026, 8, 10), now=datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    )
    assert report.requested_as_of == date(2026, 8, 10)
    assert report.as_of == report.effective_market_session == date(2026, 8, 7)
