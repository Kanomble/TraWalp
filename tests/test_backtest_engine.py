from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.engine import BacktestEngine, HistoricalScreenSource
from trading_system.backtest.report import export_backtest, export_comparison
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.backtest import StrategyVariant
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact, FundamentalMetrics
from trading_system.models.market_data import DailyBar, MarketSnapshot, TradableAsset
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

    monkeypatch.setattr(engine_module, "HistoricalScreenSource", lambda *_args: source)
    comparison = engine_module.compare_strategies(
        database,
        _config(),
        sessions[0],
        sessions[-1],
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
    paths = export_comparison(comparison, tmp_path / "reports")
    assert all(path.exists() for path in paths.values())


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
    assert "entry_reference_price" in paths["trades"].read_text(encoding="utf-8")
    assert "portfolio_equity" in paths["equity"].read_text(encoding="utf-8")
