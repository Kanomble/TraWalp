from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.coverage import (
    data_qualification_classification,
    expected_native_15m_timestamps,
    warmup_coverage_diagnostics,
)
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.intraday_isolation import (
    IN_SAMPLE_RESEARCH_NOTICE,
    annotate_intraday_isolation_coverage,
    export_intraday_isolation_comparison,
    paired_intraday_isolation_effects,
)
from trading_system.backtest.presets import position_management_preset
from trading_system.cli import _parser
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    ExecutionMetrics,
    PerformanceMetrics,
    PositionManagementPreset,
    PositionMetrics,
    StrategyComparison,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


class FixtureScreens:
    def __init__(self, records_by_date: dict[date, tuple[ScreenRecord, ...]]) -> None:
        self.records_by_date = records_by_date

    def screen(self, session: date) -> ScreenReport:
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


def _score(name: str, value: float) -> ScoreBreakdown:
    return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)


def _record(symbol: str = "AAA", *, score: float = 80) -> ScreenRecord:
    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=date(2024, 1, 2),
        sic="3571",
        eligible=True,
        fundamentals=FundamentalMetrics(operating_cash_flow_positive=True),
        technical=TechnicalSnapshot(
            market_session=date(2024, 1, 2),
            price=110,
            sma20=100,
            rsi_recovery=True,
            momentum5=0.01,
            atr14=2,
            relative_volume=1.3,
        ),
        scores=StockScores(
            quality=_score("quality", score),
            valuation=_score("valuation", score),
            opportunity=_score("opportunity", score),
            timing=_score("timing", score),
            total=score,
        ),
    )


def _daily(symbol: str, session: date) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        timestamp=datetime(session.year, session.month, session.day, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000,
    )


def _intraday(
    timestamp: datetime,
    *,
    opening: str = "100",
    high: str = "100.5",
    low: str = "98.5",
    close: str = "99",
) -> DailyBar:
    return DailyBar(
        symbol="AAA",
        timeframe=BarTimeframe.MINUTES_15,
        timestamp=timestamp,
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000,
        vwap=Decimal("99.5"),
    )


def _config(*, slippage_bps: float = 0):
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
                    "slippage_bps": slippage_bps,
                    "commission_bps": 0,
                }
            ),
            "intraday": strategy.intraday.model_copy(update={"warmup_bars": 14}),
        }
    )


def _phase_f_database(tmp_path, opening_bars: list[DailyBar]) -> tuple[Database, date, date]:
    signal_session = date(2024, 1, 2)
    entry_session = date(2024, 1, 3)
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = Database(tmp_path / "phase_f.sqlite3")
    database.initialize()
    database.upsert_bars(
        [_daily("AAA", signal_session), _daily("AAA", entry_session)]
    )
    prior_open, _ = regular_session_bounds(signal_session)
    warmup = [
        _intraday(
            prior_open + index * BarTimeframe.MINUTES_15.duration,
            high="101",
            low="99",
            close="100",
        )
        for index in range(14)
    ]
    database.upsert_bars([*warmup, *opening_bars])
    return database, signal_session, entry_session


def _run_phase_f(tmp_path, bars, preset):
    database, signal, entry = _phase_f_database(tmp_path, bars)
    return BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens({signal: (_record(),)}),
    ).run(signal, entry, preset=preset)


def _position(
    position_id: str,
    *,
    symbol: str = "AAA",
    signal: date = date(2024, 1, 12),
    exit_session: date = date(2024, 1, 12),
    gross_return: float = -0.01,
    net_return: float = -0.02,
    pnl: float = -20,
    exit_reason: str = "atr_trailing_stop",
) -> BacktestPosition:
    entry_timestamp, _ = regular_session_bounds(exit_session)
    return BacktestPosition(
        position_id=position_id,
        symbol=symbol,
        signal_date=signal,
        entry_date=exit_session,
        exit_date=exit_session,
        entry_timestamp=entry_timestamp,
        exit_timestamp=entry_timestamp,
        entry_reference_price=100,
        entry_price=100.05,
        exit_reference_price=100 * (1 + gross_return),
        exit_price=99,
        initial_quantity=10,
        execution_legs=1,
        holding_days=1,
        gross_pnl=pnl,
        net_pnl=pnl,
        position_return=net_return,
        gross_market_return=gross_return,
        transaction_cost=0,
        slippage=1,
        exit_reason=exit_reason,
        maximum_favorable_excursion=0.01,
        maximum_adverse_excursion=-0.02,
        profit_giveback=0.03,
    )


def _empty_result(preset: PositionManagementPreset, **updates) -> BacktestResult:
    values = {
        "requested_start": date(2024, 1, 2),
        "requested_end": date(2024, 1, 3),
        "actual_start": date(2024, 1, 2),
        "actual_end": date(2024, 1, 3),
        "generated_at": "2024-01-04T00:00:00+00:00",
        "strategy_variant": StrategyVariant.FULL,
        "position_management_preset": preset,
        "initial_capital": 10_000.0,
        "configuration": {"backtest": {"slippage_bps": 5, "commission_bps": 0}},
        "metrics": PerformanceMetrics(number_of_trades=0),
        "position_metrics": PositionMetrics(
            positions_opened=0,
            positions_closed=0,
            winning_positions=0,
            losing_positions=0,
            breakeven_positions=0,
        ),
        "execution_metrics": ExecutionMetrics(
            execution_legs=0,
            winning_execution_legs=0,
            losing_execution_legs=0,
            breakeven_execution_legs=0,
        ),
        "trades": (),
        "positions": (),
        "equity_curve": (),
        "research_diagnostics": {"candidate_events": []},
    }
    values.update(updates)
    return BacktestResult.model_construct(**values)


def _comparison(*results: BacktestResult) -> StrategyComparison:
    return StrategyComparison.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 3),
        actual_start=date(2024, 1, 2),
        actual_end=date(2024, 1, 3),
        generated_at="2024-01-04T00:00:00+00:00",
        variants=tuple(results),
        shared_screen_sessions=1,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION,
        data_qualification={
            "intraday": {
                "15m": {
                    "missing_sessions": 0,
                    "partial_sessions": 0,
                    "unknown_market_activity_sessions": 0,
                }
            }
        },
        research_diagnostics={},
    )


def test_intraday_isolation_family_is_exact_and_opt_in() -> None:
    isolation = engine_module._comparison_runs(
        StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION
    )
    all_runs = engine_module._comparison_runs(StrategyComparisonKind.ALL)

    assert isolation == (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        ),
    )
    assert all(preset not in {item[1] for item in isolation[1:]} for _, preset in all_runs)
    assert engine_module._comparison_label(
        StrategyVariant.FULL,
        PositionManagementPreset.INTRADAY_DYNAMIC,
        StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION,
    ) == "F0/C-intraday-dynamic"
    parsed = _parser().parse_args(
        [
            "compare-strategies",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--include",
            "research-intraday-isolation",
        ]
    )
    assert parsed.include == "research-intraday-isolation"


def test_f0_f1_f2_resolve_identical_frozen_intraday_management() -> None:
    config = _config()
    resolved = [
        position_management_preset(
            config.position_management,
            preset,
            legacy_max_holding_days=config.backtest.max_holding_days,
        )
        for preset in (
            PositionManagementPreset.INTRADAY_DYNAMIC,
            PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
            PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        )
    ]
    assert resolved[0] == resolved[1] == resolved[2]
    assert resolved[0].atr_trailing_stop.minimum_completed_bars_before_activation == 0


def test_f1_gross_loss_cooldown_uses_exact_next_xnys_session() -> None:
    losing = _position("loss")
    winner = _position("winner", gross_return=0.01, net_return=-0.01)
    zero = _position("zero", gross_return=0.0, net_return=-0.01)

    assert BacktestEngine._gross_loss_cooldown_blocked(losing, date(2024, 1, 16))
    assert not BacktestEngine._gross_loss_cooldown_blocked(losing, date(2024, 1, 17))
    assert not BacktestEngine._gross_loss_cooldown_blocked(winner, date(2024, 1, 16))
    assert not BacktestEngine._gross_loss_cooldown_blocked(zero, date(2024, 1, 16))
    cost_changed = losing.model_copy(update={"position_return": -0.20, "net_pnl": -200})
    assert BacktestEngine._gross_loss_cooldown_blocked(
        cost_changed, date(2024, 1, 16)
    )


def test_f1_cooldown_is_symbol_specific_and_allows_next_ranked_symbol(tmp_path) -> None:
    database = Database(tmp_path / "candidate.sqlite3")
    database.initialize()
    engine = BacktestEngine(database, _config())
    engine.current_preset = PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN
    engine._candidate_events = []
    report = FixtureScreens(
        {date(2024, 1, 12): (_record("AAA", score=90), _record("BBB", score=80))}
    ).screen(date(2024, 1, 12))
    trackers = {"AAA": engine_module._ReentryTracker(_position("loss"))}

    orders = engine._entry_orders(
        report,
        StrategyVariant.FULL,
        {},
        Counter(),
        execution_session=date(2024, 1, 16),
        reentry_trackers=trackers,
    )

    assert [order.record.symbol for order in orders] == ["BBB"]
    aaa = next(event for event in engine._candidate_events if event["symbol"] == "AAA")
    bbb = next(event for event in engine._candidate_events if event["symbol"] == "BBB")
    assert aaa["cooldown_blocked"] is True
    assert aaa["previous_same_symbol_gross_return"] == -0.01
    assert aaa["previous_same_symbol_net_return"] == -0.02
    assert aaa["next_xnys_session_after_previous_exit"] == "2024-01-16"
    assert aaa["cooldown_reason"] == "negative_gross_return_next_xnys_session"
    assert bbb["cooldown_blocked"] is False


def test_f2_entry_bar_atr_stop_is_identical_to_f0_and_has_no_reentry(tmp_path) -> None:
    entry_open, _ = regular_session_bounds(date(2024, 1, 3))
    bars = [_intraday(entry_open, low="97.5", close="99")]
    f0 = _run_phase_f(tmp_path / "f0", bars, PositionManagementPreset.INTRADAY_DYNAMIC)
    f2 = _run_phase_f(
        tmp_path / "f2",
        bars,
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    )

    assert f0.positions[0].exit_reason == f2.positions[0].exit_reason == "atr_trailing_stop"
    assert f0.positions[0].exit_timestamp == f2.positions[0].exit_timestamp == entry_open
    assert f2.positions[0].baseline_first_bar_trail_exit_occurred is True
    assert f2.positions[0].opening_gate_evaluated is False
    assert len(f2.positions) == 1


def test_f2_non_green_survivor_exits_at_0945_open_without_lookahead(tmp_path) -> None:
    entry_open, _ = regular_session_bounds(date(2024, 1, 3))
    execution = entry_open + BarTimeframe.MINUTES_15.duration
    result = _run_phase_f(
        tmp_path,
        [
            _intraday(entry_open, close="99"),
            _intraday(
                execution,
                opening="99.25",
                high="120",
                low="80",
                close="110",
            ),
        ],
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    )
    position = result.positions[0]

    assert position.entry_timestamp == entry_open
    assert position.exit_reason == "opening_bar_fail"
    assert position.exit_timestamp == execution
    assert position.exit_reference_price == 99.25
    assert position.opening_gate_evaluated is True
    assert position.opening_gate_triggered is True
    assert position.opening_gate_executable is True
    assert position.opening_gate_actual_timestamp == entry_open
    assert position.opening_gate_open == 100
    assert position.opening_gate_close == 99
    assert position.opening_gate_volume == 1_000
    assert position.opening_gate_vwap == 99.5


def test_f2_green_survivor_continues_f0(tmp_path) -> None:
    entry_open, _ = regular_session_bounds(date(2024, 1, 3))
    execution = entry_open + BarTimeframe.MINUTES_15.duration
    result = _run_phase_f(
        tmp_path,
        [
            _intraday(entry_open, close="100.25"),
            _intraday(execution, opening="100.25", low="99", close="100.5"),
        ],
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    )
    position = result.positions[0]

    assert position.opening_gate_passed is True
    assert position.opening_gate_triggered is False
    assert position.exit_reason != "opening_bar_fail"


def test_f2_missing_opening_bar_falls_back_to_f0_without_synthesis(tmp_path) -> None:
    entry_open, _ = regular_session_bounds(date(2024, 1, 3))
    execution = entry_open + BarTimeframe.MINUTES_15.duration
    result = _run_phase_f(
        tmp_path,
        [_intraday(execution, opening="99", close="98.75")],
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    )
    position = result.positions[0]

    assert position.entry_timestamp == execution
    assert position.opening_gate_evaluable is False
    assert position.opening_gate_failure_reason == "missing_opening_bar"
    assert position.exit_reason != "opening_bar_fail"


def test_f2_missing_execution_bar_falls_back_to_f0_without_future_exit(tmp_path) -> None:
    entry_open, _ = regular_session_bounds(date(2024, 1, 3))
    future = entry_open + 2 * BarTimeframe.MINUTES_15.duration
    result = _run_phase_f(
        tmp_path,
        [
            _intraday(entry_open, close="99"),
            _intraday(future, opening="100", low="99", close="100"),
        ],
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
    )
    position = result.positions[0]

    assert position.opening_gate_evaluable is True
    assert position.opening_gate_triggered is True
    assert position.opening_gate_executable is False
    assert position.opening_gate_failure_reason == "missing_execution_bar"
    assert position.exit_reason != "opening_bar_fail"
    assert position.exit_timestamp == future


def test_warmup_qualification_uses_actual_strictly_prior_native_bars() -> None:
    entry = datetime(2024, 1, 3, 14, 30, tzinfo=UTC)
    prior = [
        _intraday(entry - timedelta(minutes=15 * (index + 1))) for index in range(50)
    ]
    sufficient = warmup_coverage_diagnostics(
        [*prior, _intraday(entry), _intraday(entry + timedelta(minutes=15))],
        entry,
        required_bars=50,
    )
    insufficient = warmup_coverage_diagnostics(prior[:49], entry, required_bars=50)

    assert sufficient["warmup_available_native_bars"] == 50
    assert sufficient["warmup_sufficient"] is True
    assert sufficient["latest_pre_entry_warmup_timestamp"] < entry
    assert insufficient["warmup_available_native_bars"] == 49
    assert insufficient["warmup_sufficient"] is False


def test_warmup_expected_gaps_are_diagnostic_only() -> None:
    entry, _ = regular_session_bounds(date(2024, 1, 4))
    timestamps = [
        *expected_native_15m_timestamps(date(2024, 1, 2)),
        *expected_native_15m_timestamps(date(2024, 1, 3)),
    ]
    bars = [
        _intraday(timestamp)
        for index, timestamp in enumerate(timestamps)
        if index not in {0, 10}
    ]
    diagnostics = warmup_coverage_diagnostics(bars, entry, required_bars=50)

    assert diagnostics["warmup_available_native_bars"] == 50
    assert diagnostics["warmup_expected_timestamp_gap_count"] > 0
    assert diagnostics["warmup_sufficient"] is True


def test_data_and_economic_qualification_are_separate(tmp_path) -> None:
    candidate_events = [
        {
            "entry_opportunity_required": True,
            "entry_bar_present": False,
        }
    ]
    results = [
        _empty_result(
            preset,
            research_diagnostics={"candidate_events": candidate_events},
        )
        for preset in (
            PositionManagementPreset.INTRADAY_DYNAMIC,
            PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
            PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        )
    ]
    database = Database(tmp_path / "coverage.sqlite3")
    database.initialize()
    annotated, rows = annotate_intraday_isolation_coverage(
        database, _comparison(*results)
    )

    assert rows[0]["candidate_entry_opportunity_qualified"] is False
    assert rows[0]["full_session_strict_qualified"] is True
    assert rows[0]["executed_trade_path_qualified"] is True
    assert rows[0]["indicator_warmup_qualified"] is True
    assert rows[0]["data_qualification_classification"] == "PROVISIONALLY QUALIFIED"
    assert rows[0]["economic_support_classification"] == "NOT SUPPORTED"
    assert (
        annotated.data_qualification["intraday_isolation"]["in_sample_research_notice"]
        == IN_SAMPLE_RESEARCH_NOTICE
    )
    assert data_qualification_classification(
        candidate_sessions=100,
        entry_bar_missing=5,
        incomplete_trade_paths=0,
        insufficient_warmups=0,
    ) == "NOT QUALIFIED"


def test_paired_effects_are_deterministic_and_separate_path_effect() -> None:
    baseline_blocked = _position("f0-blocked", pnl=-10, net_return=-0.01)
    baseline_gate = _position(
        "f0-gate",
        symbol="BBB",
        signal=date(2024, 1, 16),
        exit_session=date(2024, 1, 16),
        pnl=20,
        net_return=0.02,
    )
    f2_gate = baseline_gate.model_copy(
        update={
            "position_id": "f2-gate",
            "net_pnl": 5,
            "position_return": 0.005,
            "exit_reason": "opening_bar_fail",
        }
    )
    f0 = _empty_result(
        PositionManagementPreset.INTRADAY_DYNAMIC,
        positions=(baseline_blocked, baseline_gate),
    )
    f1 = _empty_result(
        PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
        research_diagnostics={
            "candidate_events": [
                {
                    "symbol": "AAA",
                    "signal_session": baseline_blocked.signal_date.isoformat(),
                    "cooldown_blocked": True,
                }
            ]
        },
    )
    f2 = _empty_result(
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        positions=(baseline_blocked, f2_gate),
    )
    comparison = _comparison(f0, f1, f2)

    first = paired_intraday_isolation_effects(comparison)
    second = paired_intraday_isolation_effects(comparison)

    assert first == second
    assert first["F1/C-intraday-loss-cooldown"]["direct_cooldown_effect"] == 10
    assert first["F2/C-intraday-opening-survivor-gate"]["direct_exit_pnl_effect"] == -15
    assert "future_portfolio_path_effect" in first[
        "F2/C-intraday-opening-survivor-gate"
    ]


def test_intraday_isolation_export_is_non_overwriting(tmp_path) -> None:
    comparison = _comparison(
        _empty_result(PositionManagementPreset.INTRADAY_DYNAMIC),
        _empty_result(PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN),
        _empty_result(PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE),
    )
    paths = export_intraday_isolation_comparison(
        comparison,
        {"BASE": comparison},
        [],
        tmp_path,
        stem="intraday_isolation_fixture",
    )

    assert set(paths) == {
        "summary_csv",
        "summary_json",
        "positions",
        "execution_legs",
        "diagnostics",
        "coverage",
        "paired_effects",
        "cost_stress",
    }
    assert IN_SAMPLE_RESEARCH_NOTICE in paths["summary_json"].read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_intraday_isolation_comparison(
            comparison,
            {"BASE": comparison},
            [],
            tmp_path,
            stem="intraday_isolation_fixture",
        )
