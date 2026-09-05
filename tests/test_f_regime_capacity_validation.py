import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.market_regime import (
    MarketRegimeCapacitySchedule,
    MarketRegimeState,
    RegimeCapacityRule,
)
from trading_system.backtest.regime_capacity_validation import (
    export_f_regime_capacity_research,
    regime_strategy_config,
    run_f_regime_capacity_research,
)
from trading_system.backtest.research_registry import (
    F_REGIME_CAPACITY_RESEARCH_FAMILY,
    F_REGIME_CAPACITY_RESEARCH_STATUS,
    FROZEN_CHAMPION_F,
    f_regime_capacity_research_variants,
)
from trading_system.cli import _parser
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.backtest import PositionManagementPreset, StrategyVariant
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def _bar(symbol: str, session: date, close: float = 100.0) -> DailyBar:
    price = Decimal(str(close))
    return DailyBar(
        symbol=symbol,
        timestamp=datetime(session.year, session.month, session.day, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1_000_000,
    )


def _spy_bars(closes: list[float], start: date = date(2023, 1, 1)) -> list[DailyBar]:
    return [_bar("SPY", start + timedelta(days=index), close) for index, close in enumerate(closes)]


def _last_decision(closes: list[float], rule: RegimeCapacityRule):
    bars = _spy_bars(closes)
    return MarketRegimeCapacitySchedule(bars, rule).decision(bars[-1].timestamp.date())


def test_regime_sma200_capacity_five_only_strictly_above_average() -> None:
    risk_on = _last_decision([100.0] * 199 + [101.0], RegimeCapacityRule.REGIME_SMA200)
    equal = _last_decision([100.0] * 200, RegimeCapacityRule.REGIME_SMA200)

    assert risk_on.regime is MarketRegimeState.RISK_ON
    assert risk_on.target_capacity == 5
    assert equal.regime is MarketRegimeState.RISK_OFF
    assert equal.target_capacity == 1


@pytest.mark.parametrize(
    ("closes", "expected_state", "expected_capacity"),
    [
        ([100.0 + index / 100 for index in range(200)], MarketRegimeState.RISK_ON, 5),
        (
            [100.0] * 73 + [101.0] + [100.0] * 125 + [101.0],
            MarketRegimeState.RISK_OFF,
            1,
        ),
        (
            [100.0] * 73 + [90.0] + [100.0] * 125 + [99.0],
            MarketRegimeState.RISK_OFF,
            1,
        ),
    ],
)
def test_regime_sma200_momentum126_requires_both_conditions(
    closes: list[float],
    expected_state: MarketRegimeState,
    expected_capacity: int,
) -> None:
    decision = _last_decision(closes, RegimeCapacityRule.REGIME_SMA200_MOM126)

    assert decision.regime is expected_state
    assert decision.target_capacity == expected_capacity


def test_missing_regime_warmup_is_explicit_and_conservative() -> None:
    decision = _last_decision([100.0] * 199, RegimeCapacityRule.REGIME_SMA200)

    assert decision.regime is MarketRegimeState.UNAVAILABLE
    assert decision.target_capacity == 1
    assert "fewer than 200" in decision.reason


@pytest.mark.parametrize("future_close", [1.0, 1_000.0])
def test_future_spy_bar_does_not_change_prior_session_decision(future_close: float) -> None:
    bars = _spy_bars([100.0] * 199 + [101.0])
    session = bars[-1].timestamp.date()
    before = MarketRegimeCapacitySchedule(bars, RegimeCapacityRule.REGIME_SMA200).decision(session)
    after = MarketRegimeCapacitySchedule(
        [*bars, _bar("SPY", session + timedelta(days=1), future_close)],
        RegimeCapacityRule.REGIME_SMA200,
    ).decision(session)

    assert after == before


def test_registered_family_and_cli_are_fixed_without_tuning_flags() -> None:
    identities = f_regime_capacity_research_variants()
    parsed = _parser().parse_args(
        [
            "validate-f-regime-capacity",
            "--start",
            "2022-01-03",
            "--end",
            "2026-08-12",
            "--output-stem",
            "regime_v1",
        ]
    )

    assert F_REGIME_CAPACITY_RESEARCH_FAMILY == "research-f-regime-capacity"
    assert F_REGIME_CAPACITY_RESEARCH_STATUS == "historical research hypothesis"
    assert [identity.label for identity in identities] == [
        "F-regime-control-C1",
        "F-regime-control-C5",
        "F-regime-SPY-SMA200-C1-C5",
        "F-regime-SPY-SMA200-MOM126-C1-C5",
    ]
    assert {identity.configured_hard_max_positions for identity in identities} == {1, 5}
    assert parsed.command == "validate-f-regime-capacity"
    assert vars(parsed).keys() >= {"start", "end", "output_stem"}
    assert not vars(parsed).keys() & {"sma_length", "momentum_length", "capacity", "threshold"}


def _eligible_report(session: date, symbols: tuple[str, ...]) -> ScreenReport:
    records = tuple(
        SimpleNamespace(symbol=symbol, eligible=True, sic=f"{index + 10}")
        for index, symbol in enumerate(symbols)
    )
    return ScreenReport.model_construct(
        as_of=session,
        requested_as_of=session,
        effective_market_session=session,
        generated_at="2026-09-03T00:00:00+00:00",
        analyzed_count=len(records),
        eligible_count=len(records),
        records=records,
    )


def _hard_capacity_five_config():
    load_settings.cache_clear()
    base = load_settings().strategy
    return base.model_copy(
        update={
            "portfolio": base.portfolio.model_copy(
                update={"max_positions": 5, "max_sector_positions": 10}
            )
        }
    )


def _stub_entry_evaluation(monkeypatch) -> None:
    monkeypatch.setattr(
        engine_module,
        "evaluate_variant_entry",
        lambda record, variant, config: SimpleNamespace(
            eligible=True,
            first_failure=None,
            score=100.0 - ord(record.symbol[0]),
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "_entry_triggers",
        lambda record, config: SimpleNamespace(),
    )


def test_capacity_reduction_keeps_existing_positions_and_creates_no_entries(
    monkeypatch, tmp_path
) -> None:
    _stub_entry_evaluation(monkeypatch)
    session = date(2026, 8, 12)
    positions = {
        symbol: SimpleNamespace(sector=str(index))
        for index, symbol in enumerate(("P1", "P2", "P3", "P4"))
    }
    engine = BacktestEngine(
        Database(tmp_path / "unused.sqlite3"),
        _hard_capacity_five_config(),
        entry_capacity_provider=lambda _: 1,
    )

    orders = engine._entry_orders(
        _eligible_report(session, ("AAA", "BBB")),
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        positions,
        Counter(),
        execution_session=date(2026, 8, 13),
    )

    assert orders == []
    assert len(positions) == 4
    trace = engine._entry_capacity_trace_by_session[session]
    assert trace.target_capacity == 1
    assert trace.available_slots == 0
    assert trace.orders_created == 0


def test_capacity_increase_releases_four_slots_to_existing_ranking(monkeypatch, tmp_path) -> None:
    _stub_entry_evaluation(monkeypatch)
    session = date(2026, 8, 12)
    engine = BacktestEngine(
        Database(tmp_path / "unused.sqlite3"),
        _hard_capacity_five_config(),
        entry_capacity_provider=lambda _: 5,
    )
    positions = {"HELD": SimpleNamespace(sector="99")}

    orders = engine._entry_orders(
        _eligible_report(session, ("AAA", "BBB", "CCC", "DDD", "EEE")),
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        positions,
        Counter(),
        execution_session=date(2026, 8, 13),
    )

    assert len(orders) == 4
    assert [order.daily_candidate_rank for order in orders] == [1, 2, 3, 4]
    assert engine._entry_capacity_trace_by_session[session].capacity_blocked_candidates == 1


def _score(name: str, value: float) -> ScoreBreakdown:
    return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)


def _record(symbol: str, session: date, sic: str) -> ScreenRecord:
    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=session,
        sic=sic,
        eligible=True,
        fundamentals=FundamentalMetrics(operating_cash_flow_positive=True),
        technical=TechnicalSnapshot(
            market_session=session,
            price=100,
            sma20=90,
            sma50=85,
            sma200=80,
            sma20_rising=True,
            rsi_recovery=True,
            momentum5=0.01,
            momentum126=0.20,
            atr14=5,
            relative_volume=1.3,
            drawdown_52w=-0.02,
        ),
        scores=StockScores(
            quality=_score("quality", 90),
            valuation=_score("valuation", 80),
            opportunity=_score("opportunity", 70),
            timing=_score("timing", 60),
            total=80,
        ),
    )


class _FixtureScreens:
    def __init__(self, records: dict[date, tuple[ScreenRecord, ...]]) -> None:
        self.records = records

    def screen(self, session: date) -> ScreenReport:
        records = self.records.get(session, ())
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2026-09-03T00:00:00+00:00",
            analyzed_count=len(records),
            eligible_count=len(records),
            records=records,
        )


@pytest.mark.parametrize(
    ("rule", "capacity"),
    [(RegimeCapacityRule.CONTROL_C1, 1), (RegimeCapacityRule.CONTROL_C5, 5)],
)
def test_static_controls_exactly_match_normal_configured_runs(tmp_path, rule, capacity) -> None:
    sessions = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    symbols = ("AAA", "BBB", "CCC", "DDD", "EEE")
    database = Database(tmp_path / f"control_{capacity}.sqlite3")
    database.initialize()
    database.upsert_bars(
        [
            _bar(symbol, session, 100 + index)
            for index, session in enumerate(sessions)
            for symbol in symbols
        ]
    )
    records = {
        sessions[0]: tuple(
            _record(symbol, sessions[0], str(10 + index)) for index, symbol in enumerate(symbols)
        )
    }
    load_settings.cache_clear()
    base = load_settings().strategy
    identity = next(item for item in f_regime_capacity_research_variants() if item.rule is rule)
    config = regime_strategy_config(base, identity).model_copy(
        update={
            "portfolio": regime_strategy_config(base, identity).portfolio.model_copy(
                update={"max_sector_positions": 5}
            )
        }
    )

    def fixed_clock() -> datetime:
        return datetime(2026, 9, 3, tzinfo=UTC)

    normal = BacktestEngine(
        database,
        config,
        screen_source=_FixtureScreens(records),
        clock=fixed_clock,
    ).run(
        sessions[0],
        sessions[-1],
        variant=FROZEN_CHAMPION_F.variant,
        preset=PositionManagementPreset.CONFIGURED,
    )
    controlled = BacktestEngine(
        database,
        config,
        screen_source=_FixtureScreens(records),
        entry_capacity_provider=MarketRegimeCapacitySchedule.static(rule),
        clock=fixed_clock,
    ).run(
        sessions[0],
        sessions[-1],
        variant=FROZEN_CHAMPION_F.variant,
        preset=PositionManagementPreset.CONFIGURED,
    )

    assert normal.position_metrics.positions_closed == capacity
    assert controlled.position_metrics.positions_closed == normal.position_metrics.positions_closed
    assert controlled.execution_metrics.execution_legs == normal.execution_metrics.execution_legs
    assert controlled.metrics.total_return == pytest.approx(normal.metrics.total_return)
    assert controlled.metrics.maximum_drawdown == pytest.approx(normal.metrics.maximum_drawdown)
    assert controlled.equity_curve == normal.equity_curve
    assert [
        (position.symbol, position.signal_date, position.entry_date)
        for position in controlled.positions
    ] == [
        (position.symbol, position.signal_date, position.entry_date)
        for position in normal.positions
    ]


def test_small_local_research_exports_all_required_artifacts_and_metadata(tmp_path) -> None:
    requested_start = date(2024, 1, 2)
    requested_end = date(2024, 1, 4)
    history_start = date(2023, 5, 1)
    history_days = (requested_end - history_start).days + 1
    database = Database(tmp_path / "regime_export.sqlite3")
    database.initialize()
    database.upsert_bars(
        _spy_bars(
            [100.0 + index / 100 for index in range(history_days)],
            start=history_start,
        )
    )
    load_settings.cache_clear()

    bundle = run_f_regime_capacity_research(
        database,
        load_settings().strategy,
        requested_start,
        requested_end,
    )
    paths = export_f_regime_capacity_research(bundle, tmp_path, stem="regime_v1")
    payload = json.loads(paths["summary_json"].read_text(encoding="utf-8"))

    assert set(paths) == {
        "summary_json",
        "summary_csv",
        "metrics",
        "regime_diagnostics",
        "regime_summary",
        "monthly",
        "yearly",
        "chronological_subperiods",
        "entry_rank_analysis",
        "cost_stress",
        "symbol_concentration",
        "positions",
        "execution_legs",
    }
    assert all(path.exists() for path in paths.values())
    assert len(bundle.results) == 4
    assert len(bundle.cost_rows) == 16
    assert len(bundle.regime_summary_rows) == 2
    assert {
        "strategy",
        "session",
        "regime",
        "target_capacity",
        "open_positions_before_entries",
        "open_positions_after_entries",
        "available_slots",
        "spy_close",
        "spy_sma200",
        "spy_momentum126",
        "regime_reason",
        "entries_opened",
        "capacity_blocked_candidates",
    } <= bundle.regime_diagnostic_rows[0].keys()
    assert all(
        key in bundle.metric_rows[2]
        for key in (
            "return_delta_vs_C1",
            "return_delta_vs_C5",
            "max_drawdown_delta_vs_C1",
            "sharpe_delta_vs_C5",
            "exposure_delta_vs_C1",
            "turnover_delta_vs_C5",
        )
    )
    assert all(row["return_attribution_method"] for row in bundle.regime_summary_rows)
    assert {row["candidate_rank"] for row in bundle.entry_rank_rows} >= {1, 2, 3, 4, 5}
    assert all(row["post_hoc_only"] is True for row in bundle.symbol_concentration_rows)
    assert payload["research_family"] == "research-f-regime-capacity"
    assert payload["research_status"] == "historical research hypothesis"
    assert payload["automatic_winner_selection"] is False
    assert payload["frozen_champion_unchanged"] is True
    assert payload["survivorship_status"] == "NOT_SURVIVORSHIP_CLEAN"
    assert payload["universe_provenance_status"] == "CURRENT_UNIVERSE_ONLY"
    assert payload["regime_variants"]["REGIME_SMA200"]["risk_on_capacity"] == 5
    assert payload["regime_variants"]["REGIME_SMA200_MOM126"]["risk_off_capacity"] == 1
    with pytest.raises(FileExistsError):
        export_f_regime_capacity_research(bundle, tmp_path, stem="regime_v1")
