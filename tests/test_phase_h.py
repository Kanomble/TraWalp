from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_system import cli as cli_module
from trading_system.backtest import engine as engine_module
from trading_system.backtest import preflight as preflight_module
from trading_system.backtest.diagnostics import aggregate_profit_capture
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.first_hour_pullback import plan_first_hour_pullback
from trading_system.backtest.intraday_diagnostics import (
    add_intraday_forward_diagnostics,
)
from trading_system.backtest.intraday_hybrid import (
    export_intraday_hybrid_comparison,
    paired_intraday_hybrid_effects,
)
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import (
    RESEARCH_FAMILY_RUNS,
    ResearchLifecycle,
    lifecycle_for_preset,
)
from trading_system.cli import _parser, _research_cost_cases, _symbols_in_json
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
    def __init__(self, records: dict[date, tuple[ScreenRecord, ...]]) -> None:
        self.records = records

    def screen(self, session: date) -> ScreenReport:
        records = self.records.get(session, ())
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2024-01-01T00:00:00+00:00",
            analyzed_count=len(records),
            eligible_count=len(records),
            records=records,
        )


def _score(value: float) -> ScoreBreakdown:
    return ScoreBreakdown(name="fixture", score=value, factors=(), available_factor_count=1)


def _record(score: float = 80) -> ScreenRecord:
    return ScreenRecord(
        symbol="AAA",
        name="AAA",
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
            quality=_score(score),
            valuation=_score(score),
            opportunity=_score(score),
            timing=_score(score),
            total=score,
        ),
    )


def _bar(
    timestamp: datetime,
    *,
    opening: str = "100",
    high: str = "102",
    low: str = "98",
    close: str = "100",
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
        vwap=Decimal(close),
    )


def _daily(session: date) -> DailyBar:
    return DailyBar(
        symbol="AAA",
        timestamp=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000,
    )


def _warmup() -> list[DailyBar]:
    bars: list[DailyBar] = []
    for session in (date(2023, 12, 29), date(2024, 1, 2)):
        opening, _ = regular_session_bounds(session)
        for index in range(26):
            bars.append(_bar(opening + index * BarTimeframe.MINUTES_15.duration))
    return bars


def _entry_session(session: date) -> list[DailyBar]:
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    return [
        _bar(opening + index * duration, high="100.5", low="99.5", close="100")
        for index in range(4)
    ] + [
        _bar(opening + 4 * duration, high="100.6", low="99.9", close="100.1"),
        _bar(opening + 5 * duration, high="100.2", low="99", close="99.2"),
        _bar(opening + 6 * duration, high="100.4", low="99.1", close="99.8"),
        _bar(
            opening + 7 * duration,
            opening="100",
            high="100.8",
            low="99.1",
            close="100.4",
        ),
    ]


def _config():
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
                    "slippage_bps": 0,
                    "commission_bps": 0,
                }
            )
        }
    )


def _run_pullback(
    tmp_path: Path,
    preset: PositionManagementPreset,
    bars: list[DailyBar],
    *,
    later_session: bool = False,
    score_at_entry_close: float | None = None,
) -> BacktestResult:
    database = Database(tmp_path / f"{preset.value}.sqlite3")
    database.initialize()
    sessions = [date(2024, 1, 2), date(2024, 1, 3)]
    if later_session:
        sessions.append(date(2024, 1, 4))
    database.upsert_bars([_daily(session) for session in sessions])
    native = [*_warmup(), *bars]
    if later_session:
        opening, _ = regular_session_bounds(date(2024, 1, 4))
        native.append(_bar(opening, high="101", low="99", close="100"))
    database.upsert_bars(native)
    records = {date(2024, 1, 2): (_record(),)}
    if score_at_entry_close is not None:
        records[date(2024, 1, 3)] = (_record(score_at_entry_close),)
    return BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens(records),
    ).run(
        sessions[0],
        sessions[-1],
        preset=preset,
    )


def _position(**updates) -> BacktestPosition:
    opening, _ = regular_session_bounds(date(2024, 1, 3))
    values = {
        "position_id": "fixture",
        "symbol": "AAA",
        "signal_date": date(2024, 1, 2),
        "entry_date": date(2024, 1, 3),
        "exit_date": date(2024, 1, 3),
        "entry_timestamp": opening,
        "exit_timestamp": opening + 2 * BarTimeframe.MINUTES_15.duration,
        "entry_reference_price": 100,
        "entry_price": 100,
        "exit_reference_price": 90,
        "exit_price": 90,
        "initial_quantity": 10,
        "execution_legs": 1,
        "holding_days": 1,
        "gross_pnl": -100,
        "net_pnl": -100,
        "position_return": -0.1,
        "gross_market_return": -0.1,
        "transaction_cost": 0,
        "slippage": 0,
        "exit_reason": "atr_trailing_stop",
        "maximum_favorable_excursion": 0.01,
        "maximum_adverse_excursion": -0.1,
        "profit_giveback": 0.11,
    }
    values.update(updates)
    return BacktestPosition(**values)


def test_phase_h_registry_and_hybrid_family_are_exact() -> None:
    assert lifecycle_for_preset(PositionManagementPreset.INTRADAY_DYNAMIC) is (
        ResearchLifecycle.CHAMPION_CONTROL
    )
    for preset in (
        PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
    ):
        assert lifecycle_for_preset(preset) is ResearchLifecycle.ARCHIVED_RESEARCH
    for preset in (
        PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
    ):
        assert lifecycle_for_preset(preset) is ResearchLifecycle.ACTIVE_RESEARCH
    assert RESEARCH_FAMILY_RUNS[StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID] == (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        ),
        (
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.INTRADAY_DYNAMIC,
        ),
    )


def test_f5_preset_is_exactly_f0_management_and_f4_stays_distinct() -> None:
    config = _config()

    def resolve(preset: PositionManagementPreset):
        return position_management_preset(
            config.position_management,
            preset,
            legacy_max_holding_days=config.backtest.max_holding_days,
        )

    f0 = resolve(PositionManagementPreset.INTRADAY_DYNAMIC)
    assert resolve(PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY) == f0
    assert (
        resolve(PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT)
        == f0
    )
    f4 = resolve(PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK)
    assert f4.stop_loss.percent == pytest.approx(0.0075)
    assert not f4.atr_trailing_stop.enabled
    assert f0.stop_loss.percent == pytest.approx(0.03)
    assert f0.atr_trailing_stop.enabled
    assert f0.partial_take_profit.levels[0].profit == pytest.approx(0.015)
    assert f0.signal_decay.minimum_score_ratio == pytest.approx(0.75)


def test_f5_and_f4_share_exact_entry_but_management_diverges(tmp_path: Path) -> None:
    session = date(2024, 1, 3)
    bars = _entry_session(session)
    f4 = _run_pullback(tmp_path, PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK, bars)
    f5 = _run_pullback(
        tmp_path,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        bars,
    )
    assert f5.positions[0].entry_timestamp == f4.positions[0].entry_timestamp
    assert f4.positions[0].exit_reason == "stop_loss"
    assert f5.positions[0].exit_reason == "end_of_backtest"
    assert f5.positions[0].initial_stop_price == pytest.approx(97)
    assert f5.positions[0].stop_distance_pct == pytest.approx(0.03)
    assert f5.positions[0].swing_high_candidate_timestamp is None


def test_f5_ignores_f4_swing_exit_and_session_close_and_can_hold_overnight(
    tmp_path: Path,
) -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        *_entry_session(session),
        _bar(opening + 8 * duration, high="101", low="99.5", close="100.8"),
        _bar(opening + 9 * duration, high="100.9", low="99.5", close="100.2"),
        _bar(opening + 10 * duration, high="100.4", low="99.5", close="100"),
    ]
    result = _run_pullback(
        tmp_path,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        bars,
        later_session=True,
    )
    position = result.positions[0]
    assert position.exit_reason == "end_of_backtest"
    assert position.exit_date == date(2024, 1, 4)
    assert position.swing_high_confirmed is False
    assert all(trade.exit_reason != "session_close" for trade in result.trades)
    assert all(trade.exit_reason != "confirmed_swing_high" for trade in result.trades)


def test_f5_partial_executes_once_and_runner_uses_f0_path(tmp_path: Path) -> None:
    session = date(2024, 1, 3)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    bars = [
        *_entry_session(session),
        _bar(
            opening + 8 * duration,
            opening="100.5",
            high="101.6",
            low="100.2",
            close="101.2",
        ),
        _bar(
            opening + 9 * duration,
            opening="101.2",
            high="102",
            low="101",
            close="101.5",
        ),
    ]
    result = _run_pullback(
        tmp_path,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        bars,
    )
    partials = [trade for trade in result.trades if trade.exit_reason == "partial_take_profit"]
    assert len(partials) == 1
    assert result.positions[0].execution_legs == 2
    assert result.research_diagnostics["runner_positions"] == 1


def test_f5_score_decay_matches_f0_configuration(tmp_path: Path) -> None:
    result = _run_pullback(
        tmp_path,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        _entry_session(date(2024, 1, 3)),
        later_session=True,
        score_at_entry_close=50,
    )
    assert result.positions[0].exit_reason == "signal_decay"


@pytest.mark.parametrize(
    ("bars_transform", "reason"),
    [
        (lambda bars: [*bars[:3], *bars[4:]], "incomplete_first_hour"),
        (lambda bars: bars[:-1], "missing_pullback_execution_bar"),
    ],
)
def test_f5_missing_required_native_bar_skips_without_synthesis(
    tmp_path: Path,
    bars_transform,
    reason: str,
) -> None:
    bars = bars_transform(_entry_session(date(2024, 1, 3)))
    result = _run_pullback(
        tmp_path,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        bars,
    )
    assert result.positions == ()
    assert result.skipped_entries[reason] == 1


def test_intraday_forward_diagnostics_are_causal_and_use_both_references(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "forward.sqlite3")
    database.initialize()
    position = _position()
    assert position.exit_timestamp is not None
    exit_bar = _bar(
        position.exit_timestamp,
        opening="90",
        high="200",
        low="1",
        close="150",
    )
    next_bar = _bar(
        position.exit_timestamp + BarTimeframe.MINUTES_15.duration,
        opening="90",
        high="96",
        low="89",
        close="95",
    )
    database.upsert_bars([exit_bar, next_bar])
    diagnosed = add_intraday_forward_diagnostics(position, database)
    one = diagnosed.intraday_forward_diagnostics["next_1_native_bar"]
    assert one["resolved"] is True
    assert one["post_exit_return"] == pytest.approx(95 / 90 - 1)
    assert one["post_exit_mfe"] == pytest.approx(96 / 90 - 1)
    assert one["counterfactual_hold_return"] == pytest.approx(95 / 100 - 1)
    assert one["counterfactual_hold_mfe"] == pytest.approx(96 / 100 - 1)
    assert one["post_exit_mfe"] < 1  # unsafe same-exit-bar high=200 was excluded
    assert diagnosed.model_copy(update={"intraday_forward_diagnostics": {}}).net_pnl == (
        position.net_pnl
    )


def test_intraday_forward_gap_is_unresolved_and_exit_reason_aggregation_uses_it(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "forward-gap.sqlite3")
    database.initialize()
    position = _position()
    assert position.exit_timestamp is not None
    database.upsert_bars(
        [
            _bar(
                position.exit_timestamp + BarTimeframe.MINUTES_15.duration,
                high="101",
                low="90",
                close="100",
            )
        ]
    )
    diagnosed = add_intraday_forward_diagnostics(position, database)
    assert diagnosed.intraday_forward_diagnostics["next_2_native_bars"]["resolved"] is False
    aggregate = aggregate_profit_capture([diagnosed])[0]
    assert aggregate.positions == 1
    assert aggregate.losers == 1
    assert aggregate.average_holding_minutes == pytest.approx(30)
    assert aggregate.intraday_counterfactual_hold["next_1_native_bar"]["observations"] == 1
    assert aggregate.intraday_counterfactual_hold["next_2_native_bars"]["observations"] == 0


def test_cost_stress_is_opt_in_and_cli_family_is_explicit() -> None:
    baseline = _parser().parse_args(
        [
            "compare-strategies",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--include",
            "research-intraday-hybrid",
        ]
    )
    stressed = _parser().parse_args(
        [
            "compare-strategies",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--include",
            "research-intraday-hybrid",
            "--cost-stress",
        ]
    )
    assert baseline.cost_stress is False
    assert _research_cost_cases(baseline.cost_stress) == ()
    assert stressed.cost_stress is True
    assert _research_cost_cases(stressed.cost_stress) == (
        ("2X", 10, 0),
        ("3X", 15, 0),
        ("COMMISSION", 5, 5),
    )


def test_preflight_daily_failure_stops_candidate_discovery_and_recommends_sync(
    monkeypatch,
) -> None:
    report = SimpleNamespace(
        bars_present=0,
        qualification_start=date(2022, 10, 24),
        symbols_with_internal_gaps=0,
        symbols_with_edge_or_lifecycle_gaps=0,
        internal_missing_sessions=0,
        edge_or_lifecycle_missing_sessions=0,
        provider_range_verified_symbols=0,
        structurally_complete_symbols=0,
        coverage_metadata_mismatches=0,
        model_dump=lambda **_: {"bars_present": 0},
    )
    database = SimpleNamespace(
        list_tradable_companies=lambda: [],
        unresolved_sec_identity_conflict_symbols=lambda: set(),
        bar_sessions=lambda *_: [],
        sync_values=lambda *_: {},
    )
    monkeypatch.setattr(preflight_module, "qualify_daily_history", lambda *a, **k: report)
    monkeypatch.setattr(
        preflight_module,
        "qualify_historical_screen_start",
        lambda *a, **k: {
            "failure_reasons": ["SPY benchmark warmup is missing"],
            "benchmark_warmup_complete": False,
            "benchmark_missing_warmup_sessions": 300,
            "screenable_symbol_count_at_start": 0,
            "symbols_with_complete_initial_warmup": 0,
            "symbols_rejected_initially_for_insufficient_history": 0,
        },
    )
    monkeypatch.setattr(
        preflight_module,
        "prepare_strategy_comparison",
        lambda *a, **k: pytest.fail("candidate discovery must not run after Daily failure"),
    )
    output, candidates = preflight_module.build_compare_preflight(
        database,
        _config(),
        date(2024, 1, 2),
        date(2024, 1, 3),
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )
    assert output["daily_pit_candidate_discovery_status"] == "NOT_COMPLETE"
    assert output["dataset_ready_for_local_compare"] is False
    assert "sync-daily-history" in output["recommended_manual_sync_daily_history_command"]
    assert candidates["candidate_symbols"] == []
    assert candidates["discovery_complete"] is False


def test_compare_preflight_cli_never_invokes_network_or_backtest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "preflight.json"
    candidates = tmp_path / "preflight_intraday_candidates.json"
    payload = {
        "resolved_research_family": "research_intraday_hybrid",
        "daily_pit_candidate_discovery_status": "COMPLETE",
        "dataset_ready_for_local_compare": True,
        "intraday": {"candidate_symbol_count": 1, "candidate_sessions": 1},
        "recommended_manual_sync_daily_history_command": None,
        "recommended_manual_sync_intraday_command": None,
    }
    candidate_payload = {"candidate_symbols": [{"symbol": "AAA"}]}
    fake_database = SimpleNamespace(initialize=lambda: None)
    fake_settings = SimpleNamespace(
        strategy=SimpleNamespace(
            storage=SimpleNamespace(database_path=tmp_path, reports_path=tmp_path)
        )
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: fake_settings)
    monkeypatch.setattr(cli_module, "Database", lambda *_: fake_database)
    monkeypatch.setattr(
        cli_module,
        "build_compare_preflight",
        lambda *a, **k: (payload, candidate_payload),
    )

    def export(*args, **kwargs):
        output.write_text(json.dumps(payload), encoding="utf-8")
        candidates.write_text(json.dumps(candidate_payload), encoding="utf-8")
        return {"preflight": output, "intraday_candidates": candidates}

    monkeypatch.setattr(cli_module, "export_compare_preflight", export)
    monkeypatch.setattr(
        cli_module,
        "_synchronizer",
        lambda *a, **k: pytest.fail("network synchronizer must not be constructed"),
    )
    monkeypatch.setattr(
        engine_module.BacktestEngine,
        "run",
        lambda *a, **k: pytest.fail("BacktestEngine.run must not be called"),
    )
    status = cli_module.main(
        [
            "compare-preflight",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--include",
            "research-intraday-hybrid",
            "--output-stem",
            "fixture",
        ]
    )
    assert status == 0
    assert _symbols_in_json(candidate_payload) == {"AAA"}


def _empty_result(
    preset: PositionManagementPreset,
    positions=(),
    diagnostics=None,
    *,
    variant: StrategyVariant = StrategyVariant.FULL,
):
    return BacktestResult.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 3),
        actual_start=date(2024, 1, 2),
        actual_end=date(2024, 1, 3),
        generated_at="2024-01-04T00:00:00+00:00",
        strategy_variant=variant,
        position_management_preset=preset,
        initial_capital=10_000.0,
        configuration={"backtest": {"slippage_bps": 5, "commission_bps": 0}},
        metrics=PerformanceMetrics(number_of_trades=0),
        position_metrics=PositionMetrics(
            positions_opened=len(positions),
            positions_closed=len(positions),
            winning_positions=sum(position.net_pnl > 0 for position in positions),
            losing_positions=sum(position.net_pnl < 0 for position in positions),
            breakeven_positions=sum(position.net_pnl == 0 for position in positions),
        ),
        execution_metrics=ExecutionMetrics(
            execution_legs=0,
            winning_execution_legs=0,
            losing_execution_legs=0,
            breakeven_execution_legs=0,
        ),
        trades=(),
        positions=tuple(positions),
        equity_curve=(),
        research_diagnostics=diagnostics or {"candidate_events": []},
    )


def test_f5_paired_selection_split_is_deterministic() -> None:
    winner = _position(position_id="winner", net_pnl=10, gross_pnl=12, position_return=0.01)
    loser = _position(
        position_id="loser",
        signal_date=date(2024, 1, 4),
        net_pnl=-5,
        gross_pnl=-4,
        position_return=-0.005,
    )
    events = [
        {
            "symbol": "AAA",
            "signal_session": winner.signal_date.isoformat(),
            "opening_above_ema": True,
            "first_hour_complete": True,
            "pullback_candidate_count": 1,
            "pullback_confirmed": True,
            "executed": True,
        },
        {
            "symbol": "AAA",
            "signal_session": loser.signal_date.isoformat(),
            "opening_above_ema": False,
            "first_hour_complete": True,
            "pullback_candidate_count": 0,
            "pullback_confirmed": False,
            "executed": False,
        },
    ]
    comparison = StrategyComparison.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 5),
        actual_start=date(2024, 1, 2),
        actual_end=date(2024, 1, 5),
        generated_at="2024-01-06T00:00:00+00:00",
        variants=(
            _empty_result(PositionManagementPreset.INTRADAY_DYNAMIC, [winner, loser]),
            _empty_result(PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
            _empty_result(
                PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
                [winner],
                {"candidate_events": events},
            ),
        ),
        shared_screen_sessions=2,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )
    first = paired_intraday_hybrid_effects(comparison)
    assert first == paired_intraday_hybrid_effects(comparison)
    f5 = first["F5/C-intraday-first-hour-pullback-f0-management"]
    assert f5["f0_entered_f5_skipped"] == 1
    assert f5["f0_losers_skipped_by_f5"] == 1
    assert f5["f0_where_f5_filter_would_pass"]["count"] == 1


def test_hybrid_export_records_opt_in_cost_state_and_never_overwrites(
    tmp_path: Path,
) -> None:
    comparison = StrategyComparison.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 3),
        actual_start=date(2024, 1, 2),
        actual_end=date(2024, 1, 3),
        generated_at="2024-01-04T00:00:00+00:00",
        variants=(
            _empty_result(PositionManagementPreset.INTRADAY_DYNAMIC),
            _empty_result(PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
            _empty_result(
                PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT
            ),
            _empty_result(
                PositionManagementPreset.INTRADAY_DYNAMIC,
                variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
            ),
        ),
        shared_screen_sessions=2,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
        research_diagnostics={},
    )
    paths = export_intraday_hybrid_comparison(
        comparison,
        {},
        [],
        tmp_path,
        stem="hybrid",
        cost_stress_requested=False,
    )
    payload = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    assert payload["cost_stress_requested"] is False
    assert {row["strategy"] for row in payload["strategies"]} == {
        "F0/C-intraday-dynamic",
        "F3/C-intraday-thesis-recovery",
        "F5/C-intraday-first-hour-pullback-f0-management",
        "F-intraday/F-intraday-dynamic",
    }
    assert payload["cost_stress_executed"] is False
    assert paths["cost_stress"].read_text(encoding="utf-8").strip() == "strategy"
    with pytest.raises(FileExistsError):
        export_intraday_hybrid_comparison(
            comparison,
            {},
            [],
            tmp_path,
            stem="hybrid",
            cost_stress_requested=False,
        )


def test_shared_planner_still_requires_contiguous_confirmation() -> None:
    session = date(2024, 1, 3)
    bars = _entry_session(session)
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    gapped = [bar for bar in bars if bar.timestamp != opening + 6 * duration]
    plan = plan_first_hour_pullback(session, gapped, _warmup())
    assert plan.failure_reason == "no_confirmed_pullback"
