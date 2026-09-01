from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest import preflight as preflight_module
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import (
    comparison_strategy_label,
    research_family_runs,
    research_metadata,
)
from trading_system.cli import _parser
from trading_system.config import StopLossConfig, TakeProfitConfig, load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import candidate_requirements_from_report
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.backtest import (
    PositionManagementPreset,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot

KIND = StrategyComparisonKind.RESEARCH_F_ENTRY
F_ENTRY_PRESET = PositionManagementPreset.F_FIRST_HOUR_PULLBACK_CONFIGURED
F_ENTRY_LABEL = "F-entry/F-first-hour-pullback-configured"


class FixtureScreens:
    def __init__(self, records_by_session: dict[date, tuple[ScreenRecord, ...]]) -> None:
        self.records_by_session = records_by_session

    def screen(self, session: date) -> ScreenReport:
        records = self.records_by_session.get(session, ())
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2024-01-01T00:00:00+00:00",
            analyzed_count=len(records),
            eligible_count=len(records),
            records=records,
        )


def _config():
    load_settings.cache_clear()
    strategy = load_settings().strategy
    position_management = strategy.position_management.model_copy(
        update={
            "bar_timeframe": BarTimeframe.DAY_1,
            "stop_loss": StopLossConfig(enabled=True, percent=0.03),
            "take_profit": TakeProfitConfig(enabled=True, percent=0.02),
        }
    )
    return strategy.model_copy(
        update={
            "position_management": position_management,
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
            ),
        }
    )


def _score(name: str, value: float = 80) -> ScoreBreakdown:
    return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)


def _record(signal: date, symbol: str = "AAA") -> ScreenRecord:
    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=signal,
        sic="3571",
        eligible=True,
        fundamentals=FundamentalMetrics(operating_cash_flow_positive=True),
        technical=TechnicalSnapshot(
            market_session=signal,
            price=110,
            sma20=105,
            sma50=102,
            sma200=100,
            sma20_rising=True,
            rsi_recovery=True,
            momentum5=0.02,
            momentum126=0.15,
            relative_volume=1.3,
            drawdown_52w=-0.05,
            atr14=1.5,
        ),
        scores=StockScores(
            quality=_score("quality"),
            valuation=_score("valuation"),
            opportunity=_score("opportunity"),
            timing=_score("timing"),
            total=80,
        ),
    )


def _daily(
    session: date,
    *,
    opening: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> DailyBar:
    return DailyBar(
        symbol="AAA",
        timestamp=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000,
    )


def _intraday(
    timestamp: datetime,
    *,
    opening: str = "100",
    high: str = "100.5",
    low: str = "99.5",
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


def _warmup() -> list[DailyBar]:
    bars: list[DailyBar] = []
    for session in (date(2023, 12, 29), date(2024, 1, 2)):
        opening, _ = regular_session_bounds(session)
        bars.extend(
            _intraday(opening + index * BarTimeframe.MINUTES_15.duration)
            for index in range(26)
        )
    return bars


def _entry_session(session: date) -> list[DailyBar]:
    opening, _ = regular_session_bounds(session)
    duration = BarTimeframe.MINUTES_15.duration
    return [
        *(
            _intraday(opening + index * duration, low=str(99.5 + index * 0.1))
            for index in range(4)
        ),
        _intraday(opening + 4 * duration, high="100.6", low="99.9", close="100.1"),
        _intraday(opening + 5 * duration, high="100.2", low="99", close="99.2"),
        _intraday(opening + 6 * duration, high="100.4", low="99.1", close="99.8"),
        _intraday(
            opening + 7 * duration,
            opening="100",
            high="120",
            low="80",
            close="100",
        ),
    ]


def _resolve(config, preset: PositionManagementPreset):
    return position_management_preset(
        config.position_management,
        preset,
        legacy_max_holding_days=config.backtest.max_holding_days,
    )


def test_f_entry_registry_composes_unique_f_entry_with_configured_management(tmp_path) -> None:
    runs = research_family_runs(KIND)
    configured_run = (
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.CONFIGURED,
    )
    f_entry_run = (StrategyVariant.QUALITY_VALUE_MOMENTUM, F_ENTRY_PRESET)

    assert runs == (configured_run, f_entry_run)
    assert [comparison_strategy_label(KIND, *run) for run in runs] == [
        "F/configured",
        F_ENTRY_LABEL,
    ]
    assert research_metadata(*f_entry_run).research_id == "F-ENTRY-FIRST-HOUR-CONFIGURED"
    assert F_ENTRY_LABEL not in {
        "F/configured",
        "F5/C-intraday-first-hour-pullback-f0-management",
        "F-intraday/F-intraday-dynamic",
    }
    assert F_ENTRY_PRESET in engine_module.FIRST_HOUR_PULLBACK_ENTRY_PRESETS
    assert F_ENTRY_PRESET in engine_module.ENTRY_ONLY_INTRADAY_PRESETS

    config = _config()
    assert _resolve(config, F_ENTRY_PRESET) == _resolve(
        config, PositionManagementPreset.CONFIGURED
    )
    engine = BacktestEngine(Database(tmp_path / "identity.sqlite3"), config)
    engine.position_management = _resolve(config, F_ENTRY_PRESET)
    snapshot = engine._configuration_snapshot(
        StrategyVariant.QUALITY_VALUE_MOMENTUM, F_ENTRY_PRESET
    )
    assert snapshot["execution"]["entry"].startswith("existing first-hour pullback planner")
    assert snapshot["execution"]["position_management_timeframe"] == "1d"

    args = _parser().parse_args(
        [
            "compare-strategies",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-05",
            "--include",
            "research-f-entry",
            "--strict-intraday-coverage",
        ]
    )
    assert args.include == "research-f-entry"


def test_f_configured_and_f5_compositions_are_unchanged(tmp_path) -> None:
    config = _config()
    configured = _resolve(config, PositionManagementPreset.CONFIGURED)
    f0 = _resolve(config, PositionManagementPreset.INTRADAY_DYNAMIC)

    assert PositionManagementPreset.CONFIGURED not in (
        engine_module.FIRST_HOUR_PULLBACK_ENTRY_PRESETS
    )
    engine = BacktestEngine(Database(tmp_path / "controls.sqlite3"), config)
    engine.position_management = configured
    configured_snapshot = engine._configuration_snapshot(
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.CONFIGURED,
    )
    assert configured_snapshot["execution"]["entry"] == (
        "next available portfolio session open"
    )
    assert configured_snapshot["execution"]["position_management_timeframe"] == "1d"

    assert (
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT
        in engine_module.FIRST_HOUR_PULLBACK_ENTRY_PRESETS
    )
    assert _resolve(
        config,
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
    ) == f0
    assert _resolve(config, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY) == f0


def test_f_entry_uses_pullback_entry_then_only_daily_configured_exits(tmp_path) -> None:
    signal = date(2024, 1, 2)
    entry = date(2024, 1, 3)
    exit_session = date(2024, 1, 4)
    database = Database(tmp_path / "f-entry-execution.sqlite3")
    database.initialize()
    database.upsert_bars(
        [
            _daily(signal),
            _daily(entry, high="120", low="80"),
            _daily(exit_session, high="103", low="99", close="102"),
            *_warmup(),
            *_entry_session(entry),
        ]
    )

    result = BacktestEngine(
        database,
        _config(),
        screen_source=FixtureScreens({signal: (_record(signal),)}),
    ).run(
        signal,
        exit_session,
        variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
        preset=F_ENTRY_PRESET,
    )

    position = result.positions[0]
    opening, _ = regular_session_bounds(entry)
    assert position.entry_timestamp == opening + 7 * BarTimeframe.MINUTES_15.duration
    assert position.pullback_confirmed is True
    assert position.initial_stop_price == pytest.approx(97)
    assert position.exit_date == exit_session
    assert position.exit_reason == "take_profit"
    assert position.exit_timestamp == _daily(exit_session).timestamp
    assert result.configuration["execution"]["position_management_timeframe"] == "1d"
    assert all(
        trade.exit_reason not in {"atr_trailing_stop", "partial_take_profit"}
        for trade in result.trades
    )


def test_f_entry_preflight_requires_entry_sessions_and_warmup_not_holding_chain(
    tmp_path, monkeypatch
) -> None:
    sessions = (date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5))
    source = SimpleNamespace(
        screen=lambda session: SimpleNamespace(
            records=(SimpleNamespace(symbol="AAA"),) if session == sessions[0] else ()
        )
    )
    monkeypatch.setattr(
        engine_module,
        "evaluate_variant_entry",
        lambda record, variant, config: SimpleNamespace(eligible=True),
    )
    config = _config()
    runs = research_family_runs(KIND)
    requirements = engine_module.determine_intraday_comparison_requirements(
        config, runs, source, sessions
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.timeframe is BarTimeframe.MINUTES_15
    assert requirement.candidate_execution_sessions == (("AAA", sessions[1]),)
    assert requirement.requested_end.date() == sessions[1]
    assert requirement.candidate_path_requirements[0].runs == (
        (StrategyVariant.QUALITY_VALUE_MOMENTUM, F_ENTRY_PRESET),
    )
    assert requirement.candidate_path_requirements[0].requires_holding_sessions is False

    database = Database(tmp_path / "f-entry-preflight.sqlite3")
    database.initialize()
    preparation = SimpleNamespace(runs=runs, intraday_requirements=requirements)
    labels = [comparison_strategy_label(KIND, *run) for run in runs]
    intraday = preflight_module._intraday_preflight(
        database,
        config,
        preparation,
        labels,
        sessions[0],
        sessions[-1],
    )
    assert [item["requirement_type"] for item in intraday["required_sessions"]] == [
        "candidate_session"
    ]
    assert intraday["missing_required_first_hour_sessions"] == 1
    assert intraday["candidate_path_qualification"]["warmup_details"][0][
        "required_native_bars"
    ] == config.intraday.warmup_bars

    candidate_report = preflight_module._candidate_report(
        sessions[0], sessions[-1], KIND, preparation, intraday, None
    )
    assert candidate_report["potential_position_ranges"] == []
    assert candidate_report["required_sessions"] == [
        {
            "symbol": "AAA",
            "execution_session": sessions[1].isoformat(),
            "timeframe": "15m",
            "candidate_paths": [F_ENTRY_LABEL],
            "requirement_type": "candidate_session",
        }
    ]
    physical = candidate_requirements_from_report(
        candidate_report,
        start=sessions[0],
        end=sessions[-1],
        timeframes=(BarTimeframe.MINUTES_15,),
    )
    assert [(item.symbol, item.session, item.requirement_type) for item in physical] == [
        ("AAA", sessions[1], "candidate_session")
    ]
