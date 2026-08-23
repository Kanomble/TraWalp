from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.first_hour_pullback import (
    F4_STOP_DISTANCE_PCT,
    SwingHighDetector,
    plan_first_hour_pullback,
)
from trading_system.backtest.intraday_next import (
    DEVELOPMENT_RESEARCH_NOTICE,
    export_intraday_next_comparison,
    intraday_next_path_preserving_cost_rows,
    paired_intraday_next_effects,
)
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import (
    RESEARCH_FAMILY_RUNS,
    STRATEGY_RESEARCH_REGISTRY,
    ResearchLifecycle,
    lifecycle_for_preset,
)
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


def _bar(
    timestamp: datetime,
    *,
    opening: str = "100",
    high: str = "100.5",
    low: str = "99.5",
    close: str = "100",
    symbol: str = "AAA",
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
        vwap=Decimal(close),
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


def _prior_bars(session: date, *, close: str = "100") -> list[DailyBar]:
    prior_open, _ = regular_session_bounds(date(2024, 1, 2))
    return [
        _bar(prior_open + index * BarTimeframe.MINUTES_15.duration, close=close)
        for index in range(19)
    ]


def _first_hour(session: date, *, opening_close: str = "100") -> list[DailyBar]:
    opening, _ = regular_session_bounds(session)
    return [
        _bar(
            opening + index * BarTimeframe.MINUTES_15.duration,
            low=str(99.5 + index * 0.1),
            close=opening_close if index == 0 else "100",
        )
        for index in range(4)
    ]


def _pullback_sequence(session: date, *, include_execution: bool = True) -> list[DailyBar]:
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        _bar(opening + 4 * duration, high="100.6", low="99.9", close="100.1"),
        _bar(opening + 5 * duration, high="100.2", low="99", close="99.2"),
        _bar(opening + 6 * duration, high="100.4", low="99.1", close="99.8"),
    ]
    if include_execution:
        bars.append(
            _bar(opening + 7 * duration, high="100.8", low="99.4", close="100.4")
        )
    return bars


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
            )
        }
    )


def _position(
    position_id: str,
    *,
    symbol: str = "AAA",
    score: float = 80,
    gross_return: float = -0.01,
    net_pnl: float = -10,
) -> BacktestPosition:
    timestamp, _ = regular_session_bounds(date(2024, 1, 3))
    return BacktestPosition(
        position_id=position_id,
        symbol=symbol,
        signal_date=date(2024, 1, 2),
        entry_date=date(2024, 1, 3),
        exit_date=date(2024, 1, 3),
        entry_timestamp=timestamp,
        exit_timestamp=timestamp,
        entry_reference_price=100,
        entry_price=100,
        exit_reference_price=100 * (1 + gross_return),
        exit_price=100 * (1 + gross_return),
        initial_quantity=10,
        execution_legs=1,
        holding_days=1,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        position_return=net_pnl / 1_000,
        gross_market_return=gross_return,
        transaction_cost=0,
        slippage=0,
        exit_reason="stop_loss",
        maximum_favorable_excursion=0,
        maximum_adverse_excursion=gross_return,
        profit_giveback=-net_pnl / 1_000,
        entry_score=score,
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
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_NEXT,
        data_qualification={"intraday": {"15m": {}}},
        research_diagnostics={},
    )


def test_research_registry_lifecycles_and_exact_families() -> None:
    assert lifecycle_for_preset(PositionManagementPreset.INTRADAY_DYNAMIC) is (
        ResearchLifecycle.CHAMPION_CONTROL
    )
    assert lifecycle_for_preset(PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN) is (
        ResearchLifecycle.ARCHIVED_RESEARCH
    )
    assert lifecycle_for_preset(
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE
    ) is ResearchLifecycle.ARCHIVED_RESEARCH
    assert lifecycle_for_preset(PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY) is (
        ResearchLifecycle.ACTIVE_RESEARCH
    )
    assert lifecycle_for_preset(PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK) is (
        ResearchLifecycle.ARCHIVED_RESEARCH
    )
    assert lifecycle_for_preset(
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT
    ) is ResearchLifecycle.ACTIVE_RESEARCH
    for preset in (
        PositionManagementPreset.D1_SWING_PROFIT_LOCK,
        PositionManagementPreset.D2_SWING_RUNNER,
        PositionManagementPreset.D3_INTRADAY_TRAIL_GUARD,
        PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY,
        PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
    ):
        assert lifecycle_for_preset(preset) is ResearchLifecycle.ARCHIVED_RESEARCH
    for preset in (
        PositionManagementPreset.LEGACY,
        PositionManagementPreset.DYNAMIC_HOLD,
        PositionManagementPreset.TAKE_PROFIT,
        PositionManagementPreset.ATR_TRAILING,
        PositionManagementPreset.PARTIAL_PROFIT,
        PositionManagementPreset.BASELINE_FIXED_STOP,
        PositionManagementPreset.FIXED_STOP_MAX_HOLD,
        PositionManagementPreset.FIXED_STOP_TAKE_PROFIT,
        PositionManagementPreset.FIXED_STOP_ATR_TRAILING,
        PositionManagementPreset.FIXED_STOP_PARTIAL_ATR,
    ):
        assert lifecycle_for_preset(preset) in {
            ResearchLifecycle.ARCHIVED_RESEARCH,
            ResearchLifecycle.LEGACY_COMPATIBILITY,
        }
    assert {item.preset for item in STRATEGY_RESEARCH_REGISTRY} == set(
        PositionManagementPreset
    )
    assert len({item.research_id for item in STRATEGY_RESEARCH_REGISTRY}) == len(
        STRATEGY_RESEARCH_REGISTRY
    )
    assert RESEARCH_FAMILY_RUNS[StrategyComparisonKind.RESEARCH_INTRADAY_NEXT] == (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
        ),
    )
    assert engine_module._comparison_runs(
        StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION
    ) == (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        ),
    )
    all_presets = {
        preset
        for _, preset in engine_module._comparison_runs(StrategyComparisonKind.ALL)
    }
    assert PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY not in all_presets
    assert PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK not in all_presets
    assert (
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT
        not in all_presets
    )
    assert engine_module._comparison_runs(StrategyComparisonKind.RESEARCH_D1_D5)


def test_intraday_next_cli_is_explicit_and_presets_are_frozen() -> None:
    parsed = _parser().parse_args(
        [
            "compare-strategies",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--include",
            "research-intraday-next",
        ]
    )
    assert parsed.include == "research-intraday-next"
    config = _config()
    f0 = position_management_preset(
        config.position_management,
        PositionManagementPreset.INTRADAY_DYNAMIC,
        legacy_max_holding_days=config.backtest.max_holding_days,
    )
    f3 = position_management_preset(
        config.position_management,
        PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
        legacy_max_holding_days=config.backtest.max_holding_days,
    )
    f4 = position_management_preset(
        config.position_management,
        PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
        legacy_max_holding_days=config.backtest.max_holding_days,
    )
    assert f3 == f0
    assert f4.stop_loss.percent == F4_STOP_DISTANCE_PCT
    assert not f4.atr_trailing_stop.enabled
    assert not f4.partial_take_profit.enabled
    assert not f4.signal_decay.enabled


@pytest.mark.parametrize(
    ("gross_return", "previous_score", "current_score", "expected_blocked"),
    [
        (-0.01, 80, 79, True),
        (-0.01, 80, 80, True),
        (-0.01, 80, 81, False),
        (0.01, 80, 70, False),
        (0.0, 80, 70, False),
    ],
)
def test_f3_thesis_recovery_uses_strict_pit_score_and_gross_result(
    gross_return: float,
    previous_score: float,
    current_score: float,
    expected_blocked: bool,
) -> None:
    previous = _position(
        "previous", score=previous_score, gross_return=gross_return, net_pnl=-100
    )
    failed, recovered, blocked, delta = BacktestEngine._thesis_recovery_decision(
        previous, current_score
    )
    assert blocked is expected_blocked
    assert failed is (gross_return < 0)
    assert delta == current_score - previous_score
    if gross_return < 0:
        assert recovered is (current_score > previous_score)
    cost_changed = previous.model_copy(update={"net_pnl": -500, "position_return": -0.5})
    assert BacktestEngine._thesis_recovery_decision(cost_changed, current_score)[2] is (
        expected_blocked
    )


def test_f3_is_symbol_specific_and_records_candidate_diagnostics(tmp_path) -> None:
    database = Database(tmp_path / "f3.sqlite3")
    database.initialize()
    engine = BacktestEngine(database, _config())
    engine.current_preset = PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY
    engine._candidate_events = []
    report = FixtureScreens(
        {date(2024, 1, 4): (_record("AAA", score=80), _record("BBB", score=79))}
    ).screen(date(2024, 1, 4))
    trackers = {"AAA": engine_module._ReentryTracker(_position("loss", score=80))}
    orders = engine._entry_orders(
        report,
        StrategyVariant.FULL,
        {},
        Counter(),
        execution_session=date(2024, 1, 5),
        reentry_trackers=trackers,
    )
    assert [order.record.symbol for order in orders] == ["BBB"]
    aaa = next(event for event in engine._candidate_events if event["symbol"] == "AAA")
    assert aaa["previous_entry_C_score"] == 80
    assert aaa["current_C_score"] == 80
    assert aaa["score_delta"] == 0
    assert aaa["thesis_recovery_blocked"] is True
    assert aaa["previous_same_symbol_position_id"] == "loss"


def test_f3_allowed_reentry_close_becomes_the_new_reference() -> None:
    first_failure = _position("first", score=80, gross_return=-0.01)
    assert BacktestEngine._thesis_recovery_decision(first_failure, 81)[2] is False
    recovered_trade_then_failed = _position("second", score=81, gross_return=-0.01)
    assert BacktestEngine._thesis_recovery_decision(
        recovered_trade_then_failed, 80
    )[2] is True


def test_f4_opening_ema_is_pit_safe_and_equality_passes() -> None:
    session = date(2024, 1, 3)
    bars = [*_first_hour(session), *_pullback_sequence(session)]
    first = plan_first_hour_pullback(session, bars, _prior_bars(session))
    changed_future = list(bars)
    changed_future[1] = changed_future[1].model_copy(update={"close": Decimal("150")})
    second = plan_first_hour_pullback(session, changed_future, _prior_bars(session))
    assert first.diagnostics["opening_ema20"] == pytest.approx(100)
    assert first.diagnostics["opening_above_ema"] is True
    assert second.diagnostics["opening_ema20"] == first.diagnostics["opening_ema20"]
    assert first.entry_timestamp is not None


def test_f4_below_ema_insufficient_ema_and_incomplete_first_hour_reject() -> None:
    session = date(2024, 1, 3)
    below = plan_first_hour_pullback(
        session,
        [*_first_hour(session, opening_close="99"), *_pullback_sequence(session)],
        _prior_bars(session, close="101"),
    )
    insufficient = plan_first_hour_pullback(
        session,
        [*_first_hour(session), *_pullback_sequence(session)],
        _prior_bars(session)[:18],
    )
    incomplete = plan_first_hour_pullback(
        session,
        [*_first_hour(session)[:3], *_pullback_sequence(session)],
        _prior_bars(session),
    )
    assert below.failure_reason == "opening_below_ema"
    assert insufficient.failure_reason == "insufficient_ema_warmup"
    assert incomplete.failure_reason == "incomplete_first_hour"


def test_f4_pullback_entry_is_strictly_after_confirmation_and_missing_is_not_delayed() -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    plan = plan_first_hour_pullback(
        session,
        [*_first_hour(session), *_pullback_sequence(session)],
        _prior_bars(session),
    )
    missing = plan_first_hour_pullback(
        session,
        [*_first_hour(session), *_pullback_sequence(session, include_execution=False)],
        _prior_bars(session),
    )
    assert plan.diagnostics["pullback_candidate_timestamp"] == (
        opening + 5 * duration
    ).isoformat()
    assert plan.diagnostics["pullback_confirmation_timestamp"] == (
        opening + 6 * duration
    ).isoformat()
    assert plan.entry_timestamp == opening + 7 * duration
    assert plan.entry_timestamp > plan.confirmation_bar.timestamp
    assert missing.failure_reason == "missing_pullback_execution_bar"
    assert missing.entry_timestamp is None


def test_f4_gap_resets_pullback_detector() -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        *_first_hour(session),
        _bar(opening + 4 * duration, low="99", close="99.2"),
        _bar(opening + 6 * duration, low="99.1", close="99.8"),
        _bar(opening + 7 * duration, low="99.2", close="100"),
    ]
    plan = plan_first_hour_pullback(session, bars, _prior_bars(session))
    assert plan.failure_reason == "no_confirmed_pullback"


def test_f4_swing_high_confirmation_invalidates_higher_high_and_delays_exit() -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    previous = _bar(opening + 6 * duration, high="100", close="99.8")
    detector = SwingHighDetector(previous)
    first_high = _bar(opening + 7 * duration, high="101", close="100.5")
    higher_high = _bar(opening + 8 * duration, high="102", close="101")
    confirmation = _bar(opening + 9 * duration, high="101.5", close="100.5")
    assert detector.observe_completed_bar(first_high) is None
    assert detector.observe_completed_bar(higher_high) is None
    due = detector.observe_completed_bar(confirmation)
    assert detector.diagnostics["swing_high_candidate_timestamp"] == (
        higher_high.timestamp.isoformat()
    )
    assert due == opening + 10 * duration
    assert due > confirmation.timestamp


def _run_f4(tmp_path, session_bars: list[DailyBar]) -> BacktestResult:
    signal = date(2024, 1, 2)
    entry = date(2024, 1, 3)
    database = Database(tmp_path / "f4.sqlite3")
    database.initialize()
    database.upsert_bars([_daily("AAA", signal), _daily("AAA", entry)])
    database.upsert_bars([*_prior_bars(entry), *session_bars])
    return BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens({signal: (_record(),)}),
    ).run(
        signal,
        entry,
        preset=PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
    )


def test_f4_fixed_stop_is_immediate_without_partial_trail_or_reentry(tmp_path) -> None:
    session = date(2024, 1, 3)
    bars = [*_first_hour(session), *_pullback_sequence(session)]
    entry_bar = bars[-1].model_copy(
        update={"open": Decimal("100"), "low": Decimal("99"), "close": Decimal("99.5")}
    )
    result = _run_f4(tmp_path, [*bars[:-1], entry_bar])
    position = result.positions[0]
    assert position.exit_reason == "stop_loss"
    assert position.exit_timestamp == position.entry_timestamp
    assert position.initial_stop_price == pytest.approx(99.25)
    assert position.stop_distance_pct == F4_STOP_DISTANCE_PCT
    assert result.research_diagnostics["partial_target_count"] == 0
    assert result.research_diagnostics["runner_positions"] == 0
    assert all(trade.exit_reason != "atr_trailing_stop" for trade in result.trades)
    assert len(result.positions) == 1


def test_f4_confirmed_swing_high_exit_and_no_overnight_exposure(tmp_path) -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        *_first_hour(session),
        *_pullback_sequence(session),
        _bar(opening + 8 * duration, high="101", low="99.8", close="100.8"),
        _bar(opening + 9 * duration, opening="100.2", high="100.9", low="99.8", close="100.2"),
        _bar(opening + 10 * duration, opening="100.1", high="120", low="80", close="110"),
    ]
    result = _run_f4(tmp_path, bars)
    position = result.positions[0]
    assert position.exit_reason == "confirmed_swing_high"
    assert position.exit_timestamp == opening + 10 * duration
    assert position.exit_reference_price == pytest.approx(100.1)
    assert position.swing_high_confirmed is True
    assert result.equity_curve[-1].end_of_day_exposure == 0
    assert result.equity_curve[-1].active_positions == 0


def test_f4_session_close_fallback_has_zero_overnight_exposure(tmp_path) -> None:
    session = date(2024, 1, 3)
    bars = [*_first_hour(session), *_pullback_sequence(session)]
    result = _run_f4(tmp_path, bars)
    assert result.positions[0].exit_reason == "session_close"
    assert result.equity_curve[-1].end_of_day_exposure == 0


def test_f4_missing_first_hour_bar_creates_no_trade(tmp_path) -> None:
    session = date(2024, 1, 3)
    bars = [*_first_hour(session)[:3], *_pullback_sequence(session)]
    result = _run_f4(tmp_path, bars)
    assert result.positions == ()
    assert result.skipped_entries["incomplete_first_hour"] == 1


def test_f4_missing_swing_execution_uses_next_native_open_and_records_gap(tmp_path) -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        *_first_hour(session),
        *_pullback_sequence(session),
        _bar(opening + 8 * duration, high="101", low="99.8", close="100.8"),
        _bar(opening + 9 * duration, high="100.9", low="99.8", close="100.2"),
        _bar(
            opening + 11 * duration,
            opening="100.3",
            high="100.5",
            low="99.8",
            close="100.1",
        ),
    ]
    result = _run_f4(tmp_path, bars)
    position = result.positions[0]
    assert position.exit_reason == "confirmed_swing_high"
    assert position.intended_exit_timestamp == opening + 10 * duration
    assert position.exit_timestamp == opening + 11 * duration
    assert position.swing_high_execution_bar_missing is True


def test_f3_paired_effect_and_intraday_next_export_are_deterministic(tmp_path) -> None:
    blocked = _position("f0-blocked", net_pnl=-10)
    f0 = _empty_result(
        PositionManagementPreset.INTRADAY_DYNAMIC,
        positions=(blocked,),
    )
    f3 = _empty_result(
        PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
        research_diagnostics={
            "candidate_events": [
                {
                    "symbol": blocked.symbol,
                    "signal_session": blocked.signal_date.isoformat(),
                    "thesis_recovery_blocked": True,
                }
            ]
        },
    )
    f4 = _empty_result(
        PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
        research_diagnostics={"candidate_events": []},
    )
    comparison = _comparison(f0, f3, f4)
    first = paired_intraday_next_effects(comparison)
    second = paired_intraday_next_effects(comparison)
    assert first == second
    assert first["F3/C-intraday-thesis-recovery"]["direct_thesis_gate_effect"] == 10
    path_rows = intraday_next_path_preserving_cost_rows(comparison)
    by_strategy: dict[str, set[str]] = {}
    for row in path_rows:
        by_strategy.setdefault(row["strategy"], set()).add(row["execution_path_hash"])
        assert row["execution_path_unchanged"] is True
    assert all(len(hashes) == 1 for hashes in by_strategy.values())
    paths = export_intraday_next_comparison(
        comparison,
        {"BASE": comparison},
        [],
        tmp_path,
        stem="intraday_next_fixture",
    )
    assert DEVELOPMENT_RESEARCH_NOTICE in paths["summary_json"].read_text(
        encoding="utf-8"
    )
    with pytest.raises(FileExistsError):
        export_intraday_next_comparison(
            comparison,
            {"BASE": comparison},
            [],
            tmp_path,
            stem="intraday_next_fixture",
        )
