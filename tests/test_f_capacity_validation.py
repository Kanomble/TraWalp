import json
from collections import Counter
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.capacity_validation import (
    FCapacityResearchBundle,
    capacity_metric_rows,
    capacity_strategy_config,
    capacity_utilization_row,
    entry_rank_analysis_rows,
    export_f_capacity_research,
)
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.research_registry import (
    F_CAPACITY_RESEARCH_FAMILY,
    FROZEN_CHAMPION_F,
    f_capacity_research_variants,
    research_family_runs,
)
from trading_system.backtest.validation import CANONICAL_COST_STRESS_CASES
from trading_system.cli import _parser
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BenchmarkResult,
    EquityPoint,
    ExecutionMetrics,
    PerformanceMetrics,
    PositionManagementPreset,
    PositionMetrics,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.screening import ScreenReport


def _position(
    position_id: str,
    symbol: str,
    pnl: float,
    rank: int | None,
) -> BacktestPosition:
    return BacktestPosition(
        position_id=position_id,
        symbol=symbol,
        signal_date=date(2024, 1, 2),
        entry_date=date(2024, 1, 3),
        exit_date=date(2024, 1, 5),
        entry_timestamp=datetime(2024, 1, 3, 14, 30, tzinfo=UTC),
        exit_timestamp=datetime(2024, 1, 5, 21, 0, tzinfo=UTC),
        entry_reference_price=100,
        entry_price=100.05,
        exit_reference_price=101,
        exit_price=100.95,
        initial_quantity=10,
        execution_legs=1,
        holding_days=3,
        gross_pnl=pnl,
        net_pnl=pnl,
        position_return=pnl / 1_000.5,
        transaction_cost=0,
        slippage=1,
        exit_reason="max_hold",
        maximum_favorable_excursion=0.03,
        maximum_adverse_excursion=-0.01,
        profit_giveback=0.01,
        daily_candidate_rank=rank,
        daily_candidate_count=5,
        daily_candidate_score=100 - (rank or 0),
        daily_candidate_variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
    )


def _result(
    capacity: int,
    *,
    total_return: float = 0.10,
    exposure: float = 0.20,
    positions: tuple[BacktestPosition, ...] = (),
    active_positions: tuple[int, ...] = (0, 1, 1),
) -> BacktestResult:
    equity_curve = tuple(
        EquityPoint(
            date=date(2024, 1, 2 + index),
            cash=100,
            market_value=0,
            portfolio_equity=100 * (1 + total_return * index / max(len(active_positions) - 1, 1)),
            active_positions=value,
            exposure=exposure,
            session_exposure=exposure,
            end_of_day_exposure=exposure,
        )
        for index, value in enumerate(active_positions)
    )
    return BacktestResult.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2026, 8, 12),
        actual_start=date(2024, 1, 2),
        actual_end=date(2026, 8, 12),
        generated_at="2026-09-01T00:00:00+00:00",
        strategy_variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
        position_management_preset=PositionManagementPreset.CONFIGURED,
        initial_capital=100.0,
        configuration={
            "portfolio": {
                "max_positions": capacity,
                "max_position_pct": 1.0,
                "max_sector_positions": 2,
            },
            "risk": {"risk_per_trade": 0.01},
            "backtest": {"slippage_bps": 5, "commission_bps": 0},
        },
        metrics=PerformanceMetrics(
            total_return=total_return,
            cagr=total_return / 3,
            maximum_drawdown=-0.05 * capacity,
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
            number_of_trades=len(positions),
            trades_per_month=1.0,
            portfolio_turnover=2.0 * capacity,
            exposure=exposure,
            end_of_day_exposure=exposure,
            best_trade=0.05,
            worst_trade=-0.02,
        ),
        benchmark=BenchmarkResult(
            available=True,
            total_return=0.20,
            cagr=0.07,
            maximum_drawdown=-0.15,
        ),
        trades=(),
        positions=positions,
        position_metrics=PositionMetrics(
            positions_opened=len(positions),
            positions_closed=len(positions),
            winning_positions=sum(item.net_pnl > 0 for item in positions),
            losing_positions=sum(item.net_pnl < 0 for item in positions),
            breakeven_positions=sum(item.net_pnl == 0 for item in positions),
            position_win_rate=(
                sum(item.net_pnl > 0 for item in positions) / len(positions) if positions else None
            ),
            position_profit_factor=2.0,
            average_position_holding_period=3,
            median_position_holding_period=3,
            average_mfe=0.03,
            average_mae=-0.01,
        ),
        execution_metrics=ExecutionMetrics(
            execution_legs=0,
            winning_execution_legs=0,
            losing_execution_legs=0,
            breakeven_execution_legs=0,
        ),
        equity_curve=equity_curve,
        annualized_metrics_reliable=True,
        warnings=("results may have survivorship bias",),
        research_diagnostics={},
    )


def _capacity_results() -> dict[int, BacktestResult]:
    return {
        capacity: _result(
            capacity,
            total_return=0.10 + 0.01 * (capacity - 1),
            exposure=0.20 + 0.05 * (capacity - 1),
            positions=(_position(f"P{capacity}", f"S{capacity}", 10, capacity),),
            active_positions=(0, min(capacity, 1), capacity),
        )
        for capacity in (1, 2, 3, 5)
    }


def test_capacity_family_contains_only_preregistered_f_configured_variants() -> None:
    variants = f_capacity_research_variants()

    assert F_CAPACITY_RESEARCH_FAMILY == "research-f-capacity"
    assert [item.max_positions for item in variants] == [1, 2, 3, 5]
    assert [item.label for item in variants] == [
        "F-capacity-1",
        "F-capacity-2",
        "F-capacity-3",
        "F-capacity-5",
    ]
    assert all(item.variant is StrategyVariant.QUALITY_VALUE_MOMENTUM for item in variants)
    assert all(item.preset is PositionManagementPreset.CONFIGURED for item in variants)
    assert [item.max_positions for item in variants if item.frozen_champion_control] == [1]


def test_capacity_configs_change_only_max_positions_and_leave_champion_unchanged() -> None:
    load_settings.cache_clear()
    base = load_settings().strategy
    base_dump = base.model_dump(mode="json")

    assert base.portfolio.max_positions == 1
    assert FROZEN_CHAMPION_F.label == "F/configured"
    assert research_family_runs(StrategyComparisonKind.RESEARCH_CHAMPION_F) == (
        (StrategyVariant.QUALITY_VALUE_MOMENTUM, PositionManagementPreset.CONFIGURED),
    )
    for identity in f_capacity_research_variants():
        candidate = capacity_strategy_config(base, identity.max_positions)
        candidate_dump = candidate.model_dump(mode="json")
        assert candidate.portfolio.max_positions == identity.max_positions
        candidate_dump["portfolio"]["max_positions"] = 1
        assert candidate_dump == base_dump
    assert base.portfolio.max_positions == 1


def test_cli_exposes_capacity_family_without_changing_forward_target() -> None:
    parser = _parser()
    capacity = parser.parse_args(
        [
            "validate-champion-f-capacity",
            "--start",
            "2024-01-02",
            "--end",
            "2026-08-12",
            "--output-stem",
            "capacity_v1",
        ]
    )
    forward = parser.parse_args(
        [
            "validate-extended",
            "--target",
            "champion-f-forward",
            "--start",
            "2026-08-13",
            "--end",
            "2026-09-01",
            "--output-stem",
            "forward_v2",
        ]
    )

    assert capacity.command == "validate-champion-f-capacity"
    assert capacity.output_stem == "capacity_v1"
    assert forward.target == FROZEN_CHAMPION_F.forward_validation_target


def test_capacity_allows_more_ranked_orders_without_changing_ranking(monkeypatch, tmp_path) -> None:
    records = tuple(
        SimpleNamespace(symbol=symbol, eligible=True, sic=sic)
        for symbol, sic in (("AAA", "10"), ("BBB", "20"), ("CCC", "30"))
    )
    report = ScreenReport.model_construct(
        as_of=date(2026, 8, 13),
        requested_as_of=date(2026, 8, 13),
        effective_market_session=date(2026, 8, 13),
        generated_at="2026-08-13T00:00:00+00:00",
        analyzed_count=3,
        eligible_count=3,
        records=records,
    )
    scores = {"AAA": 100.0, "BBB": 90.0, "CCC": 80.0}
    monkeypatch.setattr(
        engine_module,
        "evaluate_variant_entry",
        lambda record, variant, config: SimpleNamespace(
            eligible=True,
            first_failure=None,
            score=scores[record.symbol],
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "_entry_triggers",
        lambda record, config: SimpleNamespace(),
    )
    load_settings.cache_clear()
    base = load_settings().strategy

    observed = {}
    for capacity in (1, 2, 3):
        engine = BacktestEngine(
            Database(tmp_path / f"unused_{capacity}.sqlite3"),
            capacity_strategy_config(base, capacity),
        )
        orders = engine._entry_orders(
            report,
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            {},
            Counter(),
        )
        observed[capacity] = [
            (order.record.symbol, order.daily_candidate_rank, order.variant_score)
            for order in orders
        ]

    assert observed[1] == [("AAA", 1, 100.0)]
    assert observed[2] == [("AAA", 1, 100.0), ("BBB", 2, 90.0)]
    assert observed[3] == [
        ("AAA", 1, 100.0),
        ("BBB", 2, 90.0),
        ("CCC", 3, 80.0),
    ]


def test_capacity_utilization_uses_explicit_end_of_day_observations() -> None:
    row = capacity_utilization_row(
        "F-capacity-3",
        _result(3, active_positions=(0, 1, 3, 3, 2)),
        3,
    )

    assert row["observation_basis"] == "end_of_day_equity_curve"
    assert row["maximum_simultaneous_positions_observed"] == 3
    assert row["average_simultaneous_positions"] == pytest.approx(1.8)
    assert row["median_simultaneous_positions"] == 2
    assert row["sessions_at_capacity"] == 2
    assert row["capacity_utilization_rate"] == pytest.approx(0.6)
    assert row["sessions_with_0_positions"] == 1
    assert row["sessions_with_3_positions"] == 2


def test_entry_rank_analysis_preserves_rank_and_aggregates_position_outcomes() -> None:
    positions = (
        _position("P1", "AAA", 20, 2),
        _position("P2", "BBB", -5, 2),
        _position("P3", "CCC", 4, None),
    )

    rows = entry_rank_analysis_rows("F-capacity-3", _result(3, positions=positions), 3)
    rank_two = next(row for row in rows if row["candidate_rank_at_entry"] == 2)

    assert rank_two["positions"] == 2
    assert rank_two["win_rate"] == pytest.approx(0.5)
    assert rank_two["net_pnl"] == 15
    assert rank_two["profit_factor"] == pytest.approx(4.0)
    assert [row["candidate_rank_at_entry"] for row in rows[:5]] == [1, 2, 3, 4, 5]


def test_capacity_metrics_include_baseline_deltas_and_canonical_15bps_flags() -> None:
    results = _capacity_results()
    cost_rows = [
        {
            "cost_case": "3X_SLIPPAGE",
            "configured_max_positions": capacity,
            "total_return": 0.02 if capacity != 5 else -0.01,
            "profit_factor": 1.2 if capacity != 5 else 0.9,
            "sharpe": 0.5 if capacity != 5 else -0.1,
        }
        for capacity in (1, 2, 3, 5)
    ]

    rows = capacity_metric_rows(results, cost_rows)
    capacity_two = next(row for row in rows if row["configured_max_positions"] == 2)
    capacity_five = next(row for row in rows if row["configured_max_positions"] == 5)

    assert CANONICAL_COST_STRESS_CASES == (
        ("2X_SLIPPAGE", 10.0, 0.0),
        ("3X_SLIPPAGE", 15.0, 0.0),
        ("COMMISSION_SENSITIVITY", 5.0, 5.0),
    )
    assert capacity_two["return_delta_vs_capacity_1"] == pytest.approx(0.01)
    assert capacity_two["exposure_delta_vs_capacity_1"] == pytest.approx(0.05)
    assert capacity_two["incremental_return_per_incremental_exposure"] == pytest.approx(0.2)
    assert capacity_two["profitable_under_15bps"] is True
    assert capacity_five["profit_factor_above_1_under_15bps"] is False
    assert capacity_five["sharpe_positive_under_15bps"] is False


def test_capacity_export_is_fresh_and_marks_research_limitations(tmp_path) -> None:
    results = _capacity_results()
    cost_rows = [
        {
            "cost_case": "3X_SLIPPAGE",
            "configured_max_positions": capacity,
            "total_return": 0.01,
            "profit_factor": 1.1,
            "sharpe": 0.2,
        }
        for capacity in (1, 2, 3, 5)
    ]
    capacity_rows = capacity_metric_rows(results, cost_rows)
    bundle = FCapacityResearchBundle(
        requested_start=date(2024, 1, 2),
        requested_end=date(2026, 8, 12),
        results=results,
        capacity_rows=capacity_rows,
        utilization_rows=[
            capacity_utilization_row(f"F-capacity-{capacity}", result, capacity)
            for capacity, result in results.items()
        ],
        entry_rank_rows=[
            row
            for capacity, result in results.items()
            for row in entry_rank_analysis_rows(f"F-capacity-{capacity}", result, capacity)
        ],
        monthly_rows=[{"strategy": "F-capacity-1", "period": "2024-01"}],
        yearly_rows=[{"strategy": "F-capacity-1", "period": "2024"}],
        chronological_subperiod_rows=[{"strategy": "F-capacity-1", "period": "THIRD_1"}],
        symbol_rows=[{"strategy": "F-capacity-1", "symbol": "S1"}],
        concentration={},
        time_stability={},
        cost_rows=cost_rows,
        universe_provenance={
            "survivorship_status": "NOT_SURVIVORSHIP_CLEAN",
            "universe_provenance": "CURRENT_UNIVERSE_ONLY",
        },
        period_classification="DEVELOPMENT_OVERLAP",
    )

    paths = export_f_capacity_research(bundle, tmp_path, stem="capacity_v1")
    payload = json.loads(paths["summary_json"].read_text(encoding="utf-8"))

    assert set(paths) == {
        "summary_json",
        "summary_csv",
        "capacity_metrics",
        "capacity_utilization",
        "entry_rank_analysis",
        "monthly",
        "yearly",
        "chronological_subperiods",
        "symbol_concentration",
        "cost_stress",
        "positions",
        "execution_legs",
    }
    assert all(path.exists() for path in paths.values())
    assert payload["automatic_winner_selection"] is False
    assert payload["frozen_champion"] == {
        "strategy": "F/configured",
        "max_positions": 1,
        "unchanged": True,
    }
    assert payload["period_classification"] == "DEVELOPMENT_OVERLAP"
    assert payload["survivorship_status"] == "NOT_SURVIVORSHIP_CLEAN"
    with pytest.raises(FileExistsError):
        export_f_capacity_research(bundle, tmp_path, stem="capacity_v1")
