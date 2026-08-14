from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.config import DataQualityConfig, load_settings
from trading_system.data.daily_history import boundary_integrity_check, warmup_coverage_at
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.sync import DataSynchronizer, _bar_edge_ranges
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, DailyBar, TradableAsset


def _bar(symbol: str, session: date, close: str = "100") -> DailyBar:
    value = Decimal(close)
    return DailyBar(
        symbol=symbol,
        timestamp=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=1_000_000,
    )


class DailyAlpaca:
    feed = "iex"
    adjustment = "all"

    def __init__(self, bars: list[DailyBar]) -> None:
        self.available = bars
        self.calls: list[tuple[tuple[str, ...], datetime, datetime]] = []
        self.last_bar_diagnostics = {"invalid_bars": 0}

    def daily_bars(self, symbols, start, end):
        selected = tuple(symbols)
        self.calls.append((selected, start, end))
        return [
            bar
            for bar in self.available
            if bar.symbol in selected and start <= bar.timestamp < end
        ]


def test_backward_gap_detection() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    earliest = datetime(2025, 4, 21, tzinfo=UTC)
    latest = datetime(2026, 8, 12, tzinfo=UTC)
    end = datetime(2026, 8, 13, tzinfo=UTC)

    ranges = _bar_edge_ranges(
        earliest, latest, BarTimeframe.DAY_1, start, end, overlap_bars=0
    )

    assert ranges == [(start, earliest)]


def test_forward_gap_detection() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    earliest = start
    latest = datetime(2026, 8, 10, tzinfo=UTC)
    end = datetime(2026, 8, 13, tzinfo=UTC)

    ranges = _bar_edge_ranges(
        earliest, latest, BarTimeframe.DAY_1, start, end, overlap_bars=0
    )

    assert ranges == [(datetime(2026, 8, 11, tzinfo=UTC), end)]


def test_backward_and_forward_gaps_are_both_planned() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    earliest = datetime(2025, 4, 21, tzinfo=UTC)
    latest = datetime(2026, 8, 10, tzinfo=UTC)
    end = datetime(2026, 8, 13, tzinfo=UTC)

    ranges = _bar_edge_ranges(
        earliest, latest, BarTimeframe.DAY_1, start, end, overlap_bars=0
    )

    assert ranges == [
        (start, earliest),
        (datetime(2026, 8, 11, tzinfo=UTC), end),
    ]


def test_verified_range_uses_only_fixed_correction_overlap() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2026, 8, 13, tzinfo=UTC)

    ranges = _bar_edge_ranges(
        datetime(2025, 6, 1, tzinfo=UTC),
        datetime(2025, 7, 1, tzinfo=UTC),
        BarTimeframe.DAY_1,
        start,
        end,
        overlap_bars=2,
        previously_verified=True,
    )

    assert ranges == [(datetime(2026, 8, 11, tzinfo=UTC), end)]


def test_historical_daily_backfill_is_idempotent_and_resumable(tmp_path) -> None:
    database = Database(tmp_path / "daily.sqlite3")
    database.initialize()
    bars = [
        _bar("AAPL", date(2024, 1, 2), "90"),
        _bar("AAPL", date(2025, 4, 21), "100"),
    ]
    database.upsert_bars([bars[-1]])
    provider = DailyAlpaca(bars)
    synchronizer = DataSynchronizer(
        database,
        provider,  # type: ignore[arg-type]
        None,
        daily_overlap_bars=1,
    )

    first = synchronizer.sync_daily_history(
        ["AAPL"], date(2024, 1, 1), date(2025, 4, 21), include_benchmark=False
    )
    second = synchronizer.sync_daily_history(
        ["AAPL"], date(2024, 1, 1), date(2025, 4, 21), include_benchmark=False
    )

    assert first["bars_inserted"] == 1
    assert second["bars_inserted"] == second["bars_updated"] == 0
    assert database.bar_count() == 2
    assert database.sync_value("daily_history_coverage", "AAPL")["start"].startswith(
        "2024-01-01"
    )


def test_spy_first_complete_target_request_fills_internal_benchmark_gap(tmp_path) -> None:
    database = Database(tmp_path / "spy-gap.sqlite3")
    database.initialize()
    sessions = [date(2025, 4, 17), date(2025, 4, 21), date(2025, 4, 22)]
    provider = DailyAlpaca([_bar("SPY", session) for session in sessions])
    database.upsert_bars([_bar("SPY", sessions[0]), _bar("SPY", sessions[-1])])
    synchronizer = DataSynchronizer(database, provider, None)  # type: ignore[arg-type]

    result = synchronizer.sync_daily_history(
        [], sessions[0], sessions[-1], include_benchmark=True
    )

    assert result["bars_inserted"] == 1
    assert provider.calls == [
        (
            ("SPY",),
            datetime(2025, 4, 17, tzinfo=UTC),
            datetime(2025, 4, 23, tzinfo=UTC),
        )
    ]
    stored_sessions = [
        bar.timestamp.date()
        for bar in database.bars_available_as_of("SPY", sessions[-1])
    ]
    assert stored_sessions == sessions


def test_warmup_coverage_distinguishes_299_from_300_prior_sessions(tmp_path) -> None:
    database = Database(tmp_path / "coverage.sqlite3")
    database.initialize()
    sessions = trading_sessions_between(date(2023, 1, 1), date(2025, 1, 31))
    backtest_start = sessions[350]
    database.upsert_bars(
        [
            *[_bar("AAA", session) for session in sessions[50:350]],
            *[_bar("BBB", session) for session in sessions[51:350]],
        ]
    )

    report = warmup_coverage_at(database, ["AAA", "BBB", "CCC"], backtest_start, 300)

    assert report["symbols_with_required_history"] == 1
    assert report["symbols_with_250_to_required_minus_1"] == 1
    assert report["symbols_with_no_prior_history"] == 1


def test_warmup_bars_affect_features_but_not_backtest_period(tmp_path) -> None:
    database = Database(tmp_path / "warmup.sqlite3")
    database.initialize()
    sessions = trading_sessions_between(date(2023, 1, 1), date(2025, 3, 31))
    prior = sessions[-302:-2]
    start, end = sessions[-2:]
    database.upsert_assets(
        [TradableAsset(symbol="AAA", name="AAA", tradable=True, fractionable=True)]
    )
    database.upsert_company(
        CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571")
    )
    database.upsert_bars(
        [
            _bar("AAA", session, str(100 + index / 100))
            for index, session in enumerate([*prior, start, end])
        ]
    )
    load_settings.cache_clear()
    strategy = load_settings().strategy
    source = HistoricalFeatureScreenSource(database, strategy, start, start)

    screen = source.screen(start)
    result = BacktestEngine(database, strategy, screen_source=source).run(start, end)

    assert screen.records[0].market_history_count >= 300
    assert "insufficient_market_history" not in screen.records[0].exclusion_reasons
    assert result.actual_start == start
    assert result.requested_start == start
    assert result.equity_curve[0].date == start


def test_spy_warmup_does_not_change_requested_benchmark_return(tmp_path) -> None:
    database = Database(tmp_path / "spy.sqlite3")
    database.initialize()
    start = date(2025, 4, 21)
    end = date(2025, 4, 22)
    database.upsert_bars([_bar("SPY", start, "100"), _bar("SPY", end, "110")])
    load_settings.cache_clear()
    engine = BacktestEngine(database, load_settings().strategy)
    before = engine._benchmark(start, end)  # noqa: SLF001

    database.upsert_bars([_bar("SPY", start - timedelta(days=7), "50")])
    after = engine._benchmark(start, end)  # noqa: SLF001

    assert before.available and after.available
    assert before.total_return == pytest.approx(0.1)
    assert after.total_return == before.total_return
    assert after.first_date == start


def test_boundary_integrity_reports_sorted_unique_transition(tmp_path) -> None:
    database = Database(tmp_path / "boundary.sqlite3")
    database.initialize()
    boundary = date(2025, 4, 21)
    database.upsert_bars(
        [
            _bar("AAA", date(2025, 4, 17), "99"),
            _bar("AAA", boundary, "100"),
        ]
    )

    report = boundary_integrity_check(database, ["AAA"], boundary)

    assert report["symbols_with_transition_pair"] == 1
    assert report["duplicate_timestamps"] == 0
    assert report["unsorted_symbols"] == 0
    assert report["missing_expected_previous_session"] == 0
    assert report["missing_expected_boundary_session"] == 0
    assert report["extreme_adjustment_jumps"] == 0


def test_required_daily_warmup_keeps_configured_300_session_requirement() -> None:
    load_settings.cache_clear()
    strategy = load_settings().strategy.model_copy(
        update={"data_quality": DataQualityConfig(min_market_history_days=300)}
    )

    assert required_daily_warmup_sessions(strategy) == 300
