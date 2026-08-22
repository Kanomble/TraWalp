from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.engine import BacktestEngine, HistoricalScreenSource
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.report import (
    export_backtest,
    export_comparison,
    export_research_comparison,
)
from trading_system.config import (
    MaxHoldConfig,
    PartialTakeProfitConfig,
    PartialTakeProfitLevel,
    PositionManagementConfig,
    SignalDecayConfig,
    StopLossConfig,
    TakeProfitConfig,
    load_settings,
)
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    intraday_session_bounds,
    intraday_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.data.sync import DataSynchronizer
from trading_system.models.backtest import (
    BacktestPosition,
    PositionManagementPreset,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact, FundamentalMetrics
from trading_system.models.market_data import (
    BarTimeframe,
    DailyBar,
    MarketSnapshot,
    TradableAsset,
)
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def _score(name: str, value: float) -> ScoreBreakdown:
    return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)


def _record(
    symbol: str = "AAA",
    *,
    sic: str = "3571",
    quality: float = 90,
    valuation: float = 80,
    opportunity: float = 70,
    timing: float = 60,
) -> ScreenRecord:
    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=date(2024, 1, 5),
        sic=sic,
        eligible=True,
        fundamentals=FundamentalMetrics(operating_cash_flow_positive=True),
        technical=TechnicalSnapshot(
            market_session=date(2024, 1, 5),
            price=110,
            sma20=100,
            rsi_recovery=True,
            momentum5=0.01,
            atr14=5,
            relative_volume=1.3,
        ),
        scores=StockScores(
            quality=_score("quality", quality),
            valuation=_score("valuation", valuation),
            opportunity=_score("opportunity", opportunity),
            timing=_score("timing", timing),
            total=80,
        ),
    )


class FixtureScreens:
    def __init__(self, records_by_date: dict[date, tuple[ScreenRecord, ...]]) -> None:
        self.records_by_date = records_by_date
        self.calls: list[date] = []

    def screen(self, session: date) -> ScreenReport:
        self.calls.append(session)
        records = self.records_by_date.get(session, ())
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2024-01-01T00:00:00+00:00",
            analyzed_count=len(records),
            eligible_count=len(records),
            records=records,
        )


def _bar(
    symbol: str,
    session: date,
    *,
    opening: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        timestamp=datetime(session.year, session.month, session.day, tzinfo=UTC),
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000_000,
    )


def _intraday_bar(
    symbol: str,
    timestamp: datetime,
    *,
    opening: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        timeframe=BarTimeframe.MINUTES_15,
        timestamp=timestamp,
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000,
    )


def _config(**backtest_updates):
    load_settings.cache_clear()
    strategy = load_settings().strategy
    return strategy.model_copy(
        update={
            "backtest": strategy.backtest.model_copy(
                update={
                    "min_total_score": 0,
                    "min_quality_score": 0,
                    "min_valuation_score": 0,
                    "min_opportunity_score": 0,
                    "min_timing_score": 0,
                    **backtest_updates,
                }
            )
        }
    )


def _database(tmp_path, bars: list[DailyBar]) -> Database:
    database = Database(tmp_path / "backtest.sqlite3")
    database.initialize()
    database.upsert_bars(bars)
    return database


def _required_intraday_bars(requirement, symbols=None) -> list[DailyBar]:
    requested = symbols or requirement.symbols
    output: list[DailyBar] = []
    for session in trading_sessions_between(
        requirement.requested_start.date(), requirement.comparison_sessions[-1]
    ):
        opening, closing = intraday_session_bounds(
            session, extended_hours=requirement.extended_hours
        )
        timestamp = opening
        while timestamp < closing:
            if requirement.requested_start <= timestamp < requirement.requested_end:
                output.extend(
                    DailyBar(
                        symbol=symbol,
                        timeframe=requirement.timeframe,
                        timestamp=timestamp,
                        open=Decimal("100"),
                        high=Decimal("101"),
                        low=Decimal("99"),
                        close=Decimal("100"),
                        volume=1_000,
                    )
                    for symbol in requested
                )
            timestamp += requirement.timeframe.duration
    return output


class FillingIntradaySynchronizer:
    def __init__(self, database: Database, requirement, *, provide_data: bool = True) -> None:
        self.database = database
        self.requirement = requirement
        self.provide_data = provide_data
        self.calls = []

    def sync_intraday(
        self, symbols, timeframes, start, end, *, incremental, extended_hours
    ):
        selected = tuple(symbols)
        self.calls.append(
            (selected, tuple(timeframes), start, end, incremental, extended_hours)
        )
        bars = (
            _required_intraday_bars(self.requirement, selected)
            if self.provide_data
            else []
        )
        stats = self.database.upsert_bars_with_stats(bars)
        return {
            **stats,
            "request_batches": 1,
            "errors": 0,
        }


def test_signal_friday_enters_monday_open_with_fractional_size_and_costs(tmp_path) -> None:
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    tuesday = date(2024, 1, 9)
    database = _database(
        tmp_path,
        [
            _bar("AAA", friday),
            _bar("AAA", monday, opening="110", high="112", low="108", close="111"),
            _bar("AAA", tuesday, opening="111", high="112", low="110", close="111"),
        ],
    )
    fractional = _record().model_copy(
        update={"technical": _record().technical.model_copy(update={"atr14": 3})}
    )
    source = FixtureScreens({friday: (fractional,)})
    config = _config(max_holding_days=2, slippage_bps=5, commission_bps=10)

    result = BacktestEngine(database, config, screen_source=source).run(friday, tuesday)

    trade = result.trades[0]
    assert trade.signal_date == friday
    assert trade.entry_date == monday
    assert trade.entry_reference_price == 110
    assert trade.entry_price == pytest.approx(110.055)
    assert trade.exit_date == tuesday
    assert trade.exit_reason == "time_exit"
    assert trade.exit_price == pytest.approx(111 * 0.9995)
    assert trade.quantity % 1 != 0
    assert trade.transaction_cost > 0
    assert trade.slippage > 0
    assert trade.position_value <= result.initial_capital * config.portfolio.max_position_pct
    assert all(point.cash >= 0 for point in result.equity_curve)
    assert result.equity_curve[-1].realized_pnl == pytest.approx(trade.pnl)
    assert result.equity_curve[-1].unrealized_pnl == 0


def test_explicit_legacy_preset_preserves_legacy_exit_reason_names(tmp_path) -> None:
    sessions = [date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})

    result = BacktestEngine(database, _config(max_holding_days=2), screen_source=source).run(
        sessions[0], sessions[-1], preset=PositionManagementPreset.LEGACY
    )

    assert result.trades[0].exit_reason == "time_exit"


@pytest.mark.parametrize(
    ("high", "low", "expected"),
    [("101", "89", "stop_loss"), ("113", "99", "profit_target"), ("113", "89", "stop_loss")],
)
def test_stop_target_and_conservative_same_bar_policy(
    tmp_path, high: str, low: str, expected: str
) -> None:
    first = date(2024, 1, 2)
    second = date(2024, 1, 3)
    database = _database(
        tmp_path,
        [_bar("AAA", first), _bar("AAA", second, high=high, low=low)],
    )
    source = FixtureScreens({first: (_record(),)})
    config = _config(slippage_bps=0, profit_target_pct=0.12)

    result = BacktestEngine(database, config, screen_source=source).run(first, second)

    assert result.trades[0].exit_reason == expected
    assert result.trades[0].entry_date == result.trades[0].exit_date == second
    assert result.metrics.exposure > 0
    assert result.metrics.end_of_day_exposure == 0
    if expected == "stop_loss":
        assert result.trades[0].exit_reference_price == pytest.approx(90)
    else:
        assert result.trades[0].exit_reference_price == pytest.approx(112)


def test_missing_atr_skips_entry_and_last_session_liquidates(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    invalid = _record().model_copy(
        update={"technical": TechnicalSnapshot(price=110, sma20=100, rsi_recovery=True)}
    )
    source = FixtureScreens({sessions[0]: (invalid,), sessions[1]: (_record(),)})

    result = BacktestEngine(database, _config(), screen_source=source).run(
        sessions[0], sessions[-1]
    )

    assert result.skipped_entries["invalid_atr"] == 1
    assert result.trades[0].entry_date == sessions[-1]
    assert result.trades[0].exit_reason == "end_of_backtest"


def test_max_positions_sector_cap_and_duplicate_prevention(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    symbols = ("AAA", "BBB", "CCC")
    database = _database(
        tmp_path,
        [_bar(symbol, session) for symbol in symbols for session in sessions],
    )
    strategy = _config(max_holding_days=10)
    strategy = strategy.model_copy(
        update={
            "portfolio": strategy.portfolio.model_copy(
                update={"max_positions": 3, "max_sector_positions": 1}
            )
        }
    )
    source = FixtureScreens(
        {
            sessions[0]: (
                _record("AAA", sic="35"),
                _record("BBB", sic="35"),
                _record("CCC", sic="28"),
            ),
            sessions[1]: (_record("AAA", sic="35"),),
        }
    )

    result = BacktestEngine(database, strategy, screen_source=source).run(sessions[0], sessions[-1])

    assert {trade.symbol for trade in result.trades} == {"AAA", "CCC"}
    assert result.skipped_entries["max_sector_positions"] == 1
    assert max(point.active_positions for point in result.equity_curve) == 2


def test_insufficient_cash_skips_later_order_without_leverage(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3)]
    database = _database(
        tmp_path,
        [_bar(symbol, session) for symbol in ("AAA", "BBB") for session in sessions],
    )
    strategy = _config(commission_bps=0)
    strategy = strategy.model_copy(
        update={
            "portfolio": strategy.portfolio.model_copy(
                update={"max_positions": 2, "max_position_pct": 1}
            ),
            "risk": strategy.risk.model_copy(update={"risk_per_trade": 1}),
        }
    )
    source = FixtureScreens({sessions[0]: (_record("AAA", sic="35"), _record("BBB", sic="28"))})

    result = BacktestEngine(database, strategy, screen_source=source).run(sessions[0], sessions[-1])

    assert result.skipped_entries["insufficient_cash"] == 1
    assert len(result.trades) == 1
    assert min(point.cash for point in result.equity_curve) >= 0


def test_historical_screen_source_never_reads_current_snapshot(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "snapshot-guard.sqlite3")
    database.initialize()
    database.upsert_assets(
        [TradableAsset(symbol="AAA", name="AAA", exchange="NYSE", tradable=True, fractionable=True)]
    )
    database.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA"))
    database.upsert_bars([_bar("AAA", date(2024, 1, 2))])
    database.upsert_market_snapshots(
        [
            MarketSnapshot(
                symbol="AAA",
                observed_at=datetime(2024, 1, 2, 20, tzinfo=UTC),
                latest_trade_price=Decimal("999"),
                latest_trade_timestamp=datetime(2024, 1, 2, 20, tzinfo=UTC),
            )
        ]
    )

    def forbidden(_symbol):
        raise AssertionError("current snapshot leaked into historical screen")

    monkeypatch.setattr(database, "latest_market_snapshot", forbidden)
    report = HistoricalScreenSource(database, _config()).screen(date(2024, 1, 2))

    assert report.records[0].technical.price == 100


def test_variants_change_only_component_mix_and_share_recovery_gate() -> None:
    config = _config()
    record = _record()

    scores = [
        engine_module._variant_entry_score(record, variant, config)[0]
        for variant in StrategyVariant
    ]

    assert scores[0] == pytest.approx((90 * 0.4 + 80 * 0.3) / 0.7)
    assert scores[1] == pytest.approx((90 * 0.4 + 80 * 0.3 + 70 * 0.2) / 0.9)
    assert scores[2] == pytest.approx(80)
    no_recovery = record.model_copy(
        update={
            "technical": record.technical.model_copy(
                update={"rsi_recovery": False, "momentum5": -0.1, "relative_volume": 1.0}
            )
        }
    )
    assert all(
        engine_module._variant_entry_score(no_recovery, variant, config)
        == (None, "recovery_signal_required")
        for variant in StrategyVariant
    )


def test_short_horizon_and_common_gate_are_explicit_in_result_metadata(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    result = BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens({sessions[0]: (_record(),)}),
    ).run(sessions[0], sessions[-1])

    assert result.annualized_metrics_reliable is False
    assert any("annualized metrics are unstable" in warning for warning in result.warnings)
    assert result.configuration["common_recovery_gate"]["applies_to"] == ["A", "B", "C"]


def test_filing_and_future_bar_never_leak_backward(tmp_path) -> None:
    database = Database(tmp_path / "pit.sqlite3")
    database.initialize()
    fact = FundamentalFact(
        cik="0000000001",
        symbol="AAA",
        metric="revenue",
        tag="Revenues",
        value=Decimal("100"),
        unit="USD",
        period_end=date(2024, 3, 31),
        filed=date(2024, 5, 5),
        form="10-Q",
        accession_number="one",
    )
    database.upsert_facts([fact])
    database.upsert_bars(
        [_bar("AAA", date(2024, 4, 19), close="100"), _bar("AAA", date(2024, 5, 6), close="999")]
    )

    assert database.facts_available_as_of("AAA", date(2024, 4, 20)) == []
    assert database.facts_available_as_of("AAA", date(2024, 5, 10)) == [fact]
    early_bars = database.bars_available_as_of("AAA", date(2024, 4, 20))
    assert [bar.close for bar in early_bars] == [Decimal("100")]


def test_strategy_comparison_reuses_each_session_screen(monkeypatch, tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})

    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    comparison = engine_module.compare_strategies(
        database,
        _config(),
        sessions[0],
        sessions[-1],
        comparison_kind=StrategyComparisonKind.SCORE_VARIANTS,
        data_qualification={"daily": {"missing_sessions": 0}},
        clock=lambda: datetime(2024, 1, 5, tzinfo=UTC),
    )

    assert comparison.shared_screen_sessions == 2
    assert source.calls == sessions[:-1]
    assert [result.strategy_variant for result in comparison.variants] == list(StrategyVariant)
    assert all(result.actual_start == sessions[0] for result in comparison.variants)
    assert all(
        result.configuration["backtest"] == comparison.variants[0].configuration["backtest"]
        for result in comparison.variants
    )
    assert comparison.data_qualification["daily"]["missing_sessions"] == 0
    output = tmp_path / "reports"
    old_reference = output / "all_comparison_2024-01-02_2024-01-04.csv"
    output.mkdir()
    old_reference.write_text("original\n", encoding="utf-8")
    paths = export_comparison(comparison, output, stem="post_audit", overwrite=False)
    assert all(path.exists() for path in paths.values())
    assert old_reference.read_text(encoding="utf-8") == "original\n"
    assert '"data_qualification"' in paths["json"].read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        export_comparison(comparison, output, stem="post_audit", overwrite=False)

    research = comparison.model_copy(
        update={"comparison_kind": StrategyComparisonKind.RESEARCH_D1_D5}
    )
    strict = research.model_copy(update={"strict_coverage_sensitivity": True})
    research_paths = export_research_comparison(
        research,
        strict,
        {
            "BASELINE": research,
            "2X_SLIPPAGE": research,
            "3X_SLIPPAGE": research,
            "COMMISSION_SENSITIVITY": research,
        },
        output,
        stem="d1_d5_test",
    )
    assert all(path.exists() for path in research_paths.values())
    assert "strict_coverage_sensitivity" in research_paths[
        "diagnostics"
    ].read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        export_research_comparison(
            research,
            strict,
            {"BASELINE": research},
            output,
            stem="d1_d5_test",
        )


def test_unified_strategy_comparison_includes_score_and_position_strategies(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)

    comparison = engine_module.compare_strategies(
        database,
        _config(),
        sessions[0],
        sessions[-1],
        comparison_kind=StrategyComparisonKind.ALL,
        clock=lambda: datetime(2024, 1, 5, tzinfo=UTC),
    )

    assert comparison.comparison_kind is StrategyComparisonKind.ALL
    assert len(comparison.variants) == 13
    assert comparison.shared_screen_sessions == 2
    assert source.calls == sessions[:-1]
    assert comparison.skipped_strategies == {
        "C/intraday-dynamic": (
            "Missing historical 15m bars for: AAA. Run: "
            "python -m trading_system.cli sync-intraday --symbols AAA "
            "--start 2024-01-02 --end 2024-01-04 --timeframes 15m"
        )
    }
    labels = {
        (result.strategy_variant.value, result.position_management_preset.value)
        for result in comparison.variants
    }
    assert ("A", "configured") in labels
    assert ("C", "dynamic-hold") in labels
    assert ("C", "atr-trailing") in labels

    paths = export_comparison(comparison, tmp_path / "reports")
    csv_text = paths["csv"].read_text(encoding="utf-8")
    assert "score_variant,position_management" in csv_text
    assert "C/atr-trailing,C,atr-trailing" in csv_text


def test_score_variant_comparison_does_not_initialize_intraday_sync(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    preparation = engine_module.prepare_strategy_comparison(
        database,
        _config(),
        sessions[0],
        sessions[-1],
        comparison_kind=StrategyComparisonKind.SCORE_VARIANTS,
    )
    factory_calls = 0

    def synchronizer_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("daily comparison must not initialize Alpaca")

    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, _config(), preparation, assessments, synchronizer_factory
    )
    comparison = engine_module.compare_strategies(
        database,
        _config(),
        sessions[0],
        sessions[-1],
        comparison_kind=StrategyComparisonKind.SCORE_VARIANTS,
        preparation=preparation,
        intraday_prefetch=prefetch,
    )

    assert preparation.intraday_requirements == ()
    assert factory_calls == 0
    assert prefetch.required is False
    assert len(comparison.variants) == 3


def test_intraday_prefetch_uses_only_pit_candidates_and_reuses_shared_screens(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(
        tmp_path,
        [_bar(symbol, session) for symbol in ("AAA", "BBB") for session in sessions],
    )
    source = FixtureScreens(
        {
            sessions[0]: (_record("AAA"), _record("BBB", quality=0)),
            sessions[1]: (_record("AAA"),),
        }
    )
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config(min_quality_score=1)
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    requirement = preparation.intraday_requirements[0]
    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    synchronizer = FillingIntradaySynchronizer(database, requirement)
    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessments, lambda: synchronizer
    )
    comparison = engine_module.compare_strategies(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        intraday_prefetch=prefetch,
    )

    assert requirement.timeframe is BarTimeframe.MINUTES_15
    assert requirement.symbols == ("AAA",)
    assert requirement.requested_start == intraday_warmup_start(
        sessions[1], BarTimeframe.MINUTES_15, config.intraday.warmup_bars
    )
    assert synchronizer.calls[0][0] == ("AAA",)
    assert synchronizer.calls[0][1] == (BarTimeframe.MINUTES_15,)
    assert synchronizer.calls[0][4] is True
    assert prefetch.timeframes["15m"].bars_added > 0
    assert comparison.skipped_strategies == {}
    assert source.calls == sessions[:-1]


def test_intraday_prefetch_never_expands_candidates_to_universe(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    universe = [f"U{index:03d}" for index in range(100)]
    database = _database(
        tmp_path,
        [_bar(symbol, session) for symbol in universe for session in sessions],
    )
    database.upsert_assets(
        TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=True)
        for symbol in universe
    )
    candidates = universe[:3]
    source = FixtureScreens({sessions[0]: tuple(_record(symbol) for symbol in candidates)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config()
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    requirement = preparation.intraday_requirements[0]
    assessment = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    synchronizer = FillingIntradaySynchronizer(database, requirement)
    engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessment, lambda: synchronizer
    )

    assert len(universe) == 100
    assert requirement.symbols == tuple(candidates)
    assert synchronizer.calls[0][0] == tuple(candidates)


def test_complete_intraday_candidate_coverage_needs_no_provider(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config()
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    database.upsert_bars(_required_intraday_bars(preparation.intraday_requirements[0]))
    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    factory_called = False

    def synchronizer_factory():
        nonlocal factory_called
        factory_called = True
        raise AssertionError("complete local coverage must not initialize Alpaca")

    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessments, synchronizer_factory
    )
    comparison = engine_module.compare_strategies(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        intraday_prefetch=prefetch,
    )

    assert assessments[0].complete_symbols == ("AAA",)
    assert assessments[0].sync_symbols == ()
    assert factory_called is False
    assert prefetch.timeframes["15m"].already_complete_symbols == 1
    assert prefetch.timeframes["15m"].sync_requested_symbols == 0
    assert "C/intraday-dynamic" not in comparison.skipped_strategies


class RecordingIntradayProvider:
    def __init__(self, bars: list[DailyBar]) -> None:
        self.bars_available = bars
        self.calls = []
        self.last_bar_diagnostics = {"invalid_bars": 0}

    def bars(self, symbols, start, end, *, timeframe, batch_size):
        selected = tuple(symbols)
        self.calls.append((selected, start, end, timeframe, batch_size))
        return [
            bar
            for bar in self.bars_available
            if bar.symbol in selected
            and bar.timeframe is timeframe
            and start <= bar.timestamp < end
        ]


def test_partial_intraday_history_uses_existing_incremental_sync(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config()
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    requirement = preparation.intraday_requirements[0]
    all_bars = _required_intraday_bars(requirement)
    first_execution = dict(requirement.first_execution_sessions)["AAA"]
    database.upsert_bars([bar for bar in all_bars if bar.timestamp.date() >= first_execution])
    provider = RecordingIntradayProvider(all_bars)
    synchronizer = DataSynchronizer(
        database,
        provider,  # type: ignore[arg-type]
        None,
        intraday_overlap_bars=2,
        intraday_request_window_days=31,
    )
    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessments, lambda: synchronizer
    )

    assert assessments[0].sync_symbols == ("AAA",)
    assert provider.calls
    requested_duration = sum(
        (end - start for _, start, end, _, _ in provider.calls),
        start=timedelta(),
    )
    assert requested_duration < requirement.requested_end - requirement.requested_start
    assert prefetch.timeframes["15m"].failure_reasons == ()


def test_missing_intraday_warmup_is_prefetched_before_backtest(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record(),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config()
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    requirement = preparation.intraday_requirements[0]
    all_bars = _required_intraday_bars(requirement)
    execution_session = dict(requirement.first_execution_sessions)["AAA"]
    database.upsert_bars(
        [bar for bar in all_bars if bar.timestamp.date() >= execution_session]
    )
    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    assert "warmup" in dict(assessments[0].incomplete_reasons)["AAA"]
    synchronizer = FillingIntradaySynchronizer(database, requirement)
    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessments, lambda: synchronizer
    )
    comparison = engine_module.compare_strategies(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        intraday_prefetch=prefetch,
    )

    assert prefetch.timeframes["15m"].failure_reasons == ()
    assert "C/intraday-dynamic" not in comparison.skipped_strategies


def test_provider_without_candidate_data_skips_only_intraday_run(
    monkeypatch, tmp_path
) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("XYZ", session) for session in sessions])
    source = FixtureScreens({sessions[0]: (_record("XYZ"),)})
    monkeypatch.setattr(engine_module, "HistoricalFeatureScreenSource", lambda *_args: source)
    config = _config()
    preparation = engine_module.prepare_strategy_comparison(
        database, config, sessions[0], sessions[-1]
    )
    assessments = engine_module.assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    synchronizer = FillingIntradaySynchronizer(
        database, preparation.intraday_requirements[0], provide_data=False
    )
    prefetch = engine_module.prefetch_comparison_intraday_data(
        database, config, preparation, assessments, lambda: synchronizer
    )
    comparison = engine_module.compare_strategies(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        intraday_prefetch=prefetch,
    )

    assert prefetch.timeframes["15m"].failure_reasons == (
        "provider returned no 15m data for XYZ",
    )
    assert comparison.skipped_strategies == {
        "C/intraday-dynamic": "provider returned no 15m data for XYZ"
    }
    assert len(comparison.variants) == 13


def test_intraday_requirement_planner_keeps_multiple_timeframes_separate(
    monkeypatch,
) -> None:
    sessions = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    source = FixtureScreens({sessions[0]: (_record("AAA"),)})
    config = _config()
    original = position_management_preset

    def multi_timeframe_preset(base, preset, *, legacy_max_holding_days):
        resolved = original(
            base, preset, legacy_max_holding_days=legacy_max_holding_days
        )
        if preset is PositionManagementPreset.DYNAMIC_HOLD:
            return resolved.model_copy(
                update={"bar_timeframe": BarTimeframe.MINUTES_5}
            )
        if preset is PositionManagementPreset.INTRADAY_DYNAMIC:
            return resolved.model_copy(update={"bar_timeframe": BarTimeframe.HOUR_1})
        return resolved

    monkeypatch.setattr(engine_module, "position_management_preset", multi_timeframe_preset)
    requirements = engine_module.determine_intraday_comparison_requirements(
        config,
        (
            (StrategyVariant.FULL, PositionManagementPreset.DYNAMIC_HOLD),
            (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        ),
        source,
        sessions,
    )

    assert [item.timeframe for item in requirements] == [
        BarTimeframe.HOUR_1,
        BarTimeframe.MINUTES_5,
    ]
    assert all(item.symbols == ("AAA",) for item in requirements)
    assert requirements[0].requested_start != requirements[1].requested_start
    assert source.calls == list(sessions[:-1])


@pytest.mark.parametrize(
    "timeframe",
    [BarTimeframe.MINUTES_5, BarTimeframe.MINUTES_15, BarTimeframe.HOUR_1],
)
def test_intraday_dynamic_uses_real_position_bars_and_daily_screening(
    tmp_path, timeframe: BarTimeframe
) -> None:
    signal_session = date(2024, 1, 2)
    entry_session = date(2024, 1, 3)
    final_session = date(2024, 1, 4)
    database = _database(
        tmp_path,
        [
            _bar("AAA", signal_session),
            _bar("AAA", entry_session, high="102", low="98", close="99"),
            _bar("AAA", final_session, opening="99", high="100", low="98", close="99"),
        ],
    )
    duration = timeframe.duration
    history: list[DailyBar] = []
    for session in trading_sessions_between(date(2023, 12, 1), signal_session):
        opening, closing = regular_session_bounds(session)
        timestamp = opening
        while timestamp < closing:
            history.append(
                DailyBar(
                    symbol="AAA",
                    timeframe=timeframe,
                    timestamp=timestamp,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=100,
                )
            )
            timestamp += duration
    opening, _ = regular_session_bounds(entry_session)
    monitoring = [
        DailyBar(
            symbol="AAA",
            timeframe=timeframe,
            timestamp=opening,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=100,
        ),
        DailyBar(
            symbol="AAA",
            timeframe=timeframe,
            timestamp=opening + duration,
            open=Decimal("101"),
            high=Decimal("101"),
            low=Decimal("98.5"),
            close=Decimal("99"),
            volume=100,
        ),
        DailyBar(
            symbol="AAA",
            timeframe=timeframe,
            timestamp=opening + duration * 2,
            open=Decimal("99"),
            high=Decimal("200"),
            low=Decimal("99"),
            close=Decimal("200"),
            volume=100,
        ),
    ]
    database.upsert_bars([*history, *monitoring])
    base = _config(slippage_bps=0, commission_bps=0)
    config = base.model_copy(
        update={
            "position_management": base.position_management.model_copy(
                update={"bar_timeframe": timeframe}
            ),
            "intraday": base.intraday.model_copy(update={"warmup_bars": 14}),
        }
    )
    source = FixtureScreens({signal_session: (_record(),)})

    result = BacktestEngine(database, config, screen_source=source).run(
        signal_session,
        final_session,
        preset=PositionManagementPreset.INTRADAY_DYNAMIC,
    )

    assert len(result.positions) == 1
    assert result.trades[0].exit_reason == "atr_trailing_stop"
    assert result.trades[0].exit_reference_price == pytest.approx(99)
    assert result.trades[0].entry_timestamp == opening
    assert result.trades[0].exit_timestamp == opening + duration
    assert result.positions[0].maximum_favorable_excursion < 0.02
    assert source.calls == [signal_session, entry_session]
    assert result.configuration["execution"]["screening_timeframe"] == "1d"
    assert result.configuration["execution"]["position_management_timeframe"] == timeframe
    assert any(f"position management timeframe: {timeframe}" in item for item in result.warnings)
    assert not any("daily OHLC cannot order" in item for item in result.warnings)


@pytest.mark.parametrize(
    ("opening_present", "opening_close", "execution_present", "expected_reason"),
    [
        (True, "101", True, None),
        (True, "100", True, "confirmation_rejected"),
        (False, "101", True, "missing_confirmation_bar"),
        (True, "101", False, "missing_execution_bar"),
    ],
)
def test_d4_uses_only_canonical_opening_bar_and_exact_0945_execution(
    tmp_path,
    opening_present: bool,
    opening_close: str,
    execution_present: bool,
    expected_reason: str | None,
) -> None:
    signal_session, entry_session, final_session = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    )
    database = _database(
        tmp_path,
        [_bar("AAA", session) for session in (signal_session, entry_session, final_session)],
    )
    signal_open, _ = regular_session_bounds(signal_session)
    entry_open, _ = regular_session_bounds(entry_session)
    final_open, _ = regular_session_bounds(final_session)
    intraday = [_intraday_bar("AAA", signal_open)]
    if opening_present:
        intraday.append(
            _intraday_bar("AAA", entry_open, opening="100", close=opening_close)
        )
    if execution_present:
        intraday.append(
            _intraday_bar(
                "AAA",
                entry_open + timedelta(minutes=15),
                opening="107",
                high="108",
                low="106",
                close="107",
            )
        )
    intraday.extend(
        [
            _intraday_bar("AAA", final_open),
            _intraday_bar("AAA", final_open + timedelta(minutes=15)),
        ]
    )
    database.upsert_bars(intraday)
    base = _config(slippage_bps=0, commission_bps=0)
    config = base.model_copy(
        update={"intraday": base.intraday.model_copy(update={"warmup_bars": 1})}
    )
    source = FixtureScreens({signal_session: (_record(),)})

    result = BacktestEngine(database, config, screen_source=source).run(
        signal_session,
        final_session,
        preset=PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY,
    )

    if expected_reason is None:
        position = result.positions[0]
        assert position.entry_timestamp == entry_open + timedelta(minutes=15)
        assert position.entry_reference_price == 107
        assert position.confirmation_bar_timestamp == entry_open
        assert position.confirmation_passed is True
        assert position.entry_delayed_from_open is True
    else:
        assert result.positions == ()
        assert result.skipped_entries[expected_reason] == 1
        if not opening_present:
            assert "confirmation_rejected" not in result.skipped_entries


@pytest.mark.parametrize(
    ("a_state", "expected_symbol"),
    [
        ("red", "BBB"),
        ("green", "AAA"),
        ("missing_execution", "BBB"),
        ("all_red", None),
    ],
)
def test_d5_ranks_only_confirmed_execution_eligible_candidates_by_daily_score(
    tmp_path, a_state: str, expected_symbol: str | None
) -> None:
    signal_session, entry_session, final_session = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    )
    symbols = ("AAA", "BBB", "CCC")
    database = _database(
        tmp_path,
        [
            _bar(symbol, session)
            for symbol in symbols
            for session in (signal_session, entry_session, final_session)
        ],
    )
    entry_open, _ = regular_session_bounds(entry_session)
    intraday: list[DailyBar] = []
    for symbol in symbols:
        green = symbol != "AAA" or a_state == "green"
        if a_state == "all_red":
            green = False
        intraday.append(
            _intraday_bar(
                symbol,
                entry_open,
                opening="100",
                close="101" if green else "99",
            )
        )
        if not (symbol == "AAA" and a_state == "missing_execution"):
            intraday.append(
                _intraday_bar(
                    symbol,
                    entry_open + timedelta(minutes=15),
                    opening="100",
                )
            )
    database.upsert_bars(intraday)
    records = (
        _record("AAA", quality=95, valuation=95, opportunity=95, timing=95),
        _record("BBB", quality=90, valuation=90, opportunity=90, timing=90),
        _record("CCC", quality=85, valuation=85, opportunity=85, timing=85),
    )
    source = FixtureScreens({signal_session: records})
    before_counts = database.storage_report()["row_counts"]

    result = BacktestEngine(
        database,
        _config(slippage_bps=0, commission_bps=0),
        screen_source=source,
    ).run(
        signal_session,
        final_session,
        preset=PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
    )
    assert database.storage_report()["row_counts"] == before_counts

    if expected_symbol is None:
        assert result.positions == ()
    else:
        assert result.positions[0].symbol == expected_symbol
        expected_rank = {"AAA": 1, "BBB": 2, "CCC": 3}[expected_symbol]
        assert result.positions[0].daily_candidate_rank == expected_rank
        assert result.positions[0].daily_candidate_count == 3


def test_negative_reentry_cooldown_is_one_xnys_session_and_winner_is_exempt() -> None:
    loser = BacktestPosition.model_construct(
        exit_date=date(2024, 1, 5), position_return=-0.01
    )
    winner = BacktestPosition.model_construct(
        exit_date=date(2024, 1, 5), position_return=0.01
    )

    assert BacktestEngine._negative_cooldown_blocked(loser, date(2024, 1, 8)) is True
    assert BacktestEngine._negative_cooldown_blocked(loser, date(2024, 1, 9)) is False
    assert BacktestEngine._negative_cooldown_blocked(winner, date(2024, 1, 8)) is False
    assert BacktestEngine._negative_cooldown_blocked(None, date(2024, 1, 8)) is False


def test_d3_strict_coverage_excludes_incomplete_pending_session_without_lookup_error(
    tmp_path,
) -> None:
    signal_session, entry_session, final_session = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    )
    database = _database(
        tmp_path,
        [_bar("AAA", session) for session in (signal_session, entry_session, final_session)],
    )
    entry_open, _ = regular_session_bounds(entry_session)
    database.upsert_bars([_intraday_bar("AAA", entry_open)])
    source = FixtureScreens({signal_session: (_record(),)})

    result = BacktestEngine(
        database,
        _config(),
        screen_source=source,
        strict_coverage_sensitivity=True,
        intraday_session_statuses={("AAA", entry_session): "PARTIAL_SESSION"},
    ).run(
        signal_session,
        final_session,
        preset=PositionManagementPreset.D3_INTRADAY_TRAIL_GUARD,
    )

    assert result.positions == ()
    assert result.skipped_entries["strict_incomplete_session"] == 1
    assert result.strict_coverage_sensitivity is True


def test_research_family_is_opt_in_ordered_and_has_no_a_d_variant() -> None:
    all_runs = engine_module._comparison_runs(StrategyComparisonKind.ALL)
    research = engine_module._comparison_runs(StrategyComparisonKind.RESEARCH_D1_D5)
    labels = [
        engine_module.research_strategy_label(variant, preset)
        for variant, preset in research
    ]

    assert all(preset not in engine_module.RESEARCH_PRESETS for _, preset in all_runs)
    assert labels == [
        "A/configured",
        "B/configured",
        "C/configured",
        "C/intraday-dynamic",
        "D1/C-swing-profit-lock",
        "D2/C-swing-runner",
        "D3/C-intraday-trail-guard",
        "D4/C-intraday-confirmed-entry",
        "D5/C-hybrid-confirmed-swing",
        "D1/B-swing-profit-lock",
        "D5/B-hybrid-confirmed-swing",
    ]
    assert not any(label.startswith("D") and "/A-" in label for label in labels)

    extended = engine_module._comparison_runs(
        StrategyComparisonKind.EXTENDED_VALIDATION
    )
    extended_labels = [
        engine_module.research_strategy_label(variant, preset)
        for variant, preset in extended
    ]
    assert extended_labels == [
        "B/configured",
        "C/configured",
        "D1/C-swing-profit-lock",
        "C/intraday-dynamic",
        "D2/C-swing-runner",
        "D1/B-swing-profit-lock",
    ]
    assert all(
        "D3/" not in label and "D4/" not in label and "D5/" not in label
        for label in extended_labels
    )


def test_intraday_close_rule_uses_last_native_bar_price_and_timestamp(tmp_path) -> None:
    signal_session = date(2024, 1, 2)
    entry_session = date(2024, 1, 3)
    final_session = date(2024, 1, 4)
    database = _database(
        tmp_path,
        [
            _bar("AAA", signal_session),
            _bar("AAA", entry_session, high="110", low="89", close="90"),
            _bar("AAA", final_session),
        ],
    )
    timeframe = BarTimeframe.MINUTES_15
    previous_open, _ = regular_session_bounds(signal_session)
    entry_open, _ = regular_session_bounds(entry_session)
    last_timestamp = entry_open + timeframe.duration
    database.upsert_bars(
        [
            DailyBar(
                symbol="AAA",
                timeframe=timeframe,
                timestamp=previous_open,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=100,
            ),
            DailyBar(
                symbol="AAA",
                timeframe=timeframe,
                timestamp=entry_open,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=100,
            ),
            DailyBar(
                symbol="AAA",
                timeframe=timeframe,
                timestamp=last_timestamp,
                open=Decimal("107"),
                high=Decimal("109"),
                low=Decimal("106"),
                close=Decimal("108"),
                volume=100,
            ),
        ]
    )
    weak = _record(quality=40, valuation=40, opportunity=40, timing=40)
    weak = weak.model_copy(
        update={
            "technical": weak.technical.model_copy(
                update={"price": 90, "sma20": 100, "rsi_recovery": False, "momentum5": -0.1}
            )
        }
    )
    base = _config(slippage_bps=0, commission_bps=0)
    config = base.model_copy(
        update={
            "position_management": PositionManagementConfig(
                bar_timeframe=timeframe,
                stop_loss=StopLossConfig(enabled=True, percent=0.03),
                take_profit=TakeProfitConfig(enabled=False),
                signal_decay=SignalDecayConfig(enabled=True, minimum_score_ratio=0.75),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            ),
            "intraday": base.intraday.model_copy(update={"warmup_bars": 1}),
        }
    )
    source = FixtureScreens(
        {signal_session: (_record(),), entry_session: (weak,)}
    )

    result = BacktestEngine(database, config, screen_source=source).run(
        signal_session, final_session
    )

    trade = result.trades[0]
    assert trade.exit_reason == "signal_decay"
    assert trade.exit_reference_price == 108
    assert trade.exit_timestamp == last_timestamp
    assert trade.exit_timestamp >= trade.entry_timestamp


def test_intraday_backtest_refuses_incomplete_warmup_without_daily_fallback(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    opening, _ = regular_session_bounds(sessions[1])
    database.upsert_bars(
        [
            DailyBar(
                symbol="AAA",
                timeframe=BarTimeframe.MINUTES_15,
                timestamp=opening,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=100,
            )
        ]
    )

    with pytest.raises(ValueError, match="including configured warmup history") as error:
        BacktestEngine(
            database,
            _config(),
            screen_source=FixtureScreens({sessions[0]: (_record(),)}),
        ).run(
            sessions[0],
            sessions[-1],
            preset=PositionManagementPreset.INTRADAY_DYNAMIC,
        )

    assert "sync-intraday" in str(error.value)
    assert "--timeframes 15m" in str(error.value)


def test_backtest_reports_export_structured_json_trades_and_equity(tmp_path) -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    result = BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens({sessions[0]: (_record(),)}),
        clock=lambda: datetime(2024, 1, 4, tzinfo=UTC),
    ).run(sessions[0], sessions[-1])

    paths = export_backtest(result, tmp_path / "reports")

    assert all(path.exists() for path in paths.values())
    assert '"strategy_variant": "C"' in paths["json"].read_text(encoding="utf-8")
    assert '"data_diagnostics"' in paths["json"].read_text(encoding="utf-8")
    assert '"strategy"' in paths["json"].read_text(encoding="utf-8")
    assert '"positions"' in paths["json"].read_text(encoding="utf-8")
    assert '"position_metrics"' in paths["json"].read_text(encoding="utf-8")
    assert "entry_reference_price" in paths["trades"].read_text(encoding="utf-8")
    assert "position_id" in paths["positions"].read_text(encoding="utf-8")
    assert "execution_leg_id" in paths["execution_legs"].read_text(encoding="utf-8")
    assert "post_exit_return_5d" in paths["post_exit"].read_text(encoding="utf-8")
    assert "portfolio_equity" in paths["equity"].read_text(encoding="utf-8")


def test_dynamic_hold_can_remain_open_beyond_ten_sessions(tmp_path) -> None:
    sessions = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
        date(2024, 1, 10),
        date(2024, 1, 11),
        date(2024, 1, 12),
        date(2024, 1, 16),
        date(2024, 1, 17),
        date(2024, 1, 18),
    ]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    strategy = _config(max_holding_days=10).model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=False),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )
    result = BacktestEngine(
        database, strategy, screen_source=FixtureScreens({sessions[0]: (_record(),)})
    ).run(sessions[0], sessions[-1])

    assert result.trades[0].holding_days > 10
    assert result.trades[0].exit_reason == "end_of_backtest"


def test_signal_exit_can_reenter_only_through_fresh_ranking(tmp_path) -> None:
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    weak = _record(quality=40, valuation=40, opportunity=40, timing=40)
    weak = weak.model_copy(
        update={
            "technical": weak.technical.model_copy(
                update={"price": 90, "sma20": 100, "rsi_recovery": False, "momentum5": -0.1}
            )
        }
    )
    strategy = _config().model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=False),
                signal_decay=SignalDecayConfig(enabled=True, minimum_score_ratio=0.75),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )
    source = FixtureScreens(
        {
            sessions[0]: (_record(),),
            sessions[1]: (weak,),
            sessions[2]: (_record(),),
        }
    )

    result = BacktestEngine(database, strategy, screen_source=source).run(
        sessions[0], sessions[-1]
    )

    assert [trade.symbol for trade in result.trades] == ["AAA", "AAA"]
    assert result.trades[0].exit_reason == "signal_decay"
    assert result.trades[1].entry_date == sessions[-1]
    second_position = result.positions[1]
    assert second_position.is_reentry is True
    assert second_position.previous_exit_date == sessions[1]
    assert second_position.days_since_previous_exit == 2
    assert second_position.previous_exit_reason == "signal_decay"
    assert second_position.previous_position_return == pytest.approx(
        result.positions[0].position_return
    )
    assert second_position.fresh_trigger_since_previous_exit is True


def test_reentry_without_trigger_reset_is_diagnosed_as_not_fresh(tmp_path) -> None:
    sessions = [date(2024, 1, day) for day in (2, 3, 4)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    weak_but_still_triggered = _record(
        quality=40, valuation=40, opportunity=40, timing=40
    )
    strategy = _config().model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=False),
                signal_decay=SignalDecayConfig(enabled=True, minimum_score_ratio=0.75),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )
    source = FixtureScreens(
        {sessions[0]: (_record(),), sessions[1]: (weak_but_still_triggered,)}
    )

    result = BacktestEngine(database, strategy, screen_source=source).run(
        sessions[0], sessions[-1]
    )

    assert len(result.positions) == 2
    assert result.positions[1].is_reentry is True
    assert result.positions[1].fresh_trigger_since_previous_exit is False
    assert result.position_metrics.reentry_positions == 1
    assert result.position_metrics.reentries_without_fresh_trigger == 1


def test_partial_exit_applies_cost_model_once_per_fill_and_leaves_remainder(tmp_path) -> None:
    sessions = [date(2024, 1, day) for day in (2, 3, 4)]
    database = _database(
        tmp_path,
        [
            _bar("AAA", sessions[0]),
            _bar("AAA", sessions[1], high="102", low="99", close="101"),
            _bar("AAA", sessions[2], close="101"),
        ],
    )
    strategy = _config(slippage_bps=5, commission_bps=10).model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=False),
                partial_take_profit=PartialTakeProfitConfig(
                    enabled=True,
                    levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
                ),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )
    result = BacktestEngine(
        database, strategy, screen_source=FixtureScreens({sessions[0]: (_record(),)})
    ).run(sessions[0], sessions[-1])

    assert len(result.trades) == 2
    partial, remainder = result.trades
    assert partial.is_partial_exit is True
    assert partial.quantity == pytest.approx(remainder.quantity)
    assert partial.exit_reason == "partial_take_profit"
    assert partial.transaction_cost > 0 and remainder.transaction_cost > 0
    assert partial.position_id == remainder.position_id
    assert partial.execution_leg_id != remainder.execution_leg_id
    assert result.execution_metrics.execution_legs == 2
    assert result.position_metrics.positions_opened == 1
    assert result.position_metrics.positions_closed == 1
    assert result.position_metrics.winning_positions == 1
    assert result.position_metrics.position_win_rate == 1
    assert len(result.positions) == 1
    assert result.positions[0].execution_legs == 2


def test_score_diagnostics_use_only_available_session_screens(tmp_path) -> None:
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5)]
    database = _database(tmp_path, [_bar("AAA", session) for session in sessions])
    low = _record(quality=60, valuation=60, opportunity=60, timing=60)
    high = _record(quality=90, valuation=90, opportunity=90, timing=90)
    strategy = _config().model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=False),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )
    source = FixtureScreens(
        {sessions[0]: (_record(),), sessions[1]: (low,), sessions[2]: (high,)}
    )

    result = BacktestEngine(database, strategy, screen_source=source).run(
        sessions[0], sessions[-1]
    )
    position = result.positions[0]

    assert [point.date for point in position.score_history] == sessions[:-1]
    assert position.minimum_score_during_trade == 60
    assert position.maximum_score_during_trade == 90
    assert position.exit_score == 90


def test_forward_bars_change_only_post_exit_diagnostics(tmp_path) -> None:
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10)]
    prefix = [
        _bar("AAA", sessions[0]),
        _bar("AAA", sessions[1], high="103", low="99", close="102"),
    ]
    rising = prefix + [
        _bar(
            "AAA",
            session,
            opening=str(103 + index),
            high=str(104 + index),
            low="101",
            close=str(103 + index),
        )
        for index, session in enumerate(sessions[2:])
    ]
    falling = prefix + [
        _bar("AAA", session, high="101", low=str(98 - index), close=str(99 - index))
        for index, session in enumerate(sessions[2:])
    ]
    strategy = _config(slippage_bps=0).model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=True, percent=0.02),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )

    def run(name: str, bars: list[DailyBar]):
        database = Database(tmp_path / f"{name}.sqlite3")
        database.initialize()
        database.upsert_bars(bars)
        source = FixtureScreens({sessions[0]: (_record(),)})
        return BacktestEngine(database, strategy, screen_source=source).run(
            sessions[0], sessions[-1]
        )

    rising_result = run("rising", rising)
    falling_result = run("falling", falling)

    assert rising_result.trades == falling_result.trades
    assert rising_result.equity_curve == falling_result.equity_curve
    # The take-profit bar's low may occur after the full exit and therefore must
    # not be retroactively counted as position MAE on daily OHLC data.
    assert rising_result.positions[0].maximum_adverse_excursion == 0
    assert rising_result.positions[0].post_exit_return_5d > 0
    assert falling_result.positions[0].post_exit_return_5d < 0


def test_full_take_profit_does_not_use_unknown_later_intrabar_low(tmp_path) -> None:
    signal, exit_session, final_session = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    )
    database = _database(
        tmp_path,
        [
            _bar("AAA", signal),
            _bar(
                "AAA",
                exit_session,
                opening="100",
                high="103",
                low="98",
                close="101",
            ),
            _bar("AAA", final_session),
        ],
    )
    strategy = _config(slippage_bps=0).model_copy(
        update={
            "position_management": PositionManagementConfig(
                stop_loss=StopLossConfig(enabled=False),
                take_profit=TakeProfitConfig(enabled=True, percent=0.02),
                max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
            )
        }
    )

    result = BacktestEngine(
        database,
        strategy,
        screen_source=FixtureScreens({signal: (_record(),)}),
    ).run(signal, final_session)

    assert result.positions[0].exit_reason == "take_profit"
    assert result.positions[0].maximum_favorable_excursion == pytest.approx(0.02)
    assert result.positions[0].maximum_adverse_excursion == pytest.approx(0)


def test_fixed_stop_baselines_isolate_only_requested_exit_rule() -> None:
    config = _config()
    baseline = position_management_preset(
        config.position_management,
        PositionManagementPreset.BASELINE_FIXED_STOP,
        legacy_max_holding_days=10,
    )
    max_hold = position_management_preset(
        config.position_management,
        PositionManagementPreset.FIXED_STOP_MAX_HOLD,
        legacy_max_holding_days=10,
    )
    take_profit = position_management_preset(
        config.position_management,
        PositionManagementPreset.FIXED_STOP_TAKE_PROFIT,
        legacy_max_holding_days=10,
    )
    atr = position_management_preset(
        config.position_management,
        PositionManagementPreset.FIXED_STOP_ATR_TRAILING,
        legacy_max_holding_days=10,
    )

    assert baseline.stop_loss.percent == 0.03
    assert max_hold.stop_loss == take_profit.stop_loss == atr.stop_loss == baseline.stop_loss
    assert not baseline.signal_decay.enabled
    assert max_hold.max_hold.enabled and max_hold.max_hold.mode == "hard"
    assert take_profit.take_profit.enabled and take_profit.take_profit.percent == 0.02
    assert atr.atr_trailing_stop.enabled
