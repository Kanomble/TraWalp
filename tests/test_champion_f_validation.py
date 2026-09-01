import json
from datetime import UTC, date, datetime

import pytest

from trading_system.backtest.report import export_backtest
from trading_system.backtest.research_registry import (
    comparison_strategy_label,
    research_family_runs,
)
from trading_system.backtest.validation import (
    CANONICAL_COST_STRESS_CASES,
    ChampionFValidationBundle,
    calendar_stability,
    chronological_subperiod_analysis,
    cost_stress_rows,
    drawdown_diagnostics,
    export_champion_f_validation,
    symbol_and_leave_one_out,
)
from trading_system.cli import _parser
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BenchmarkResult,
    EquityPoint,
    ExecutionMetrics,
    PerformanceMetrics,
    PositionManagementPreset,
    PositionMetrics,
    StrategyComparison,
    StrategyComparisonKind,
    StrategyVariant,
)


def _position(position_id: str, symbol: str, pnl: float) -> BacktestPosition:
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
    )


def _result(
    *,
    total_return: float = 0.10,
    slippage_bps: float = 5,
    commission_bps: float = 0,
    positions: tuple[BacktestPosition, ...] = (),
    equity_curve: tuple[EquityPoint, ...] = (),
) -> BacktestResult:
    return BacktestResult.model_construct(
        requested_start=date(2024, 1, 2),
        requested_end=date(2026, 8, 12),
        actual_start=date(2024, 1, 2),
        actual_end=date(2026, 8, 12),
        generated_at="2026-08-31T00:00:00+00:00",
        strategy_variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
        position_management_preset=PositionManagementPreset.CONFIGURED,
        initial_capital=100.0,
        configuration={
            "backtest": {
                "slippage_bps": slippage_bps,
                "commission_bps": commission_bps,
            }
        },
        metrics=PerformanceMetrics(
            total_return=total_return,
            cagr=0.04,
            maximum_drawdown=-0.05,
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
            number_of_trades=len(positions),
            portfolio_turnover=2.0,
            exposure=0.25,
            end_of_day_exposure=0.20,
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
            position_profit_factor=2.0,
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


def _comparison(result: BacktestResult) -> StrategyComparison:
    return StrategyComparison.model_construct(
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        actual_start=result.actual_start,
        actual_end=result.actual_end,
        generated_at=result.generated_at,
        variants=(result,),
        shared_screen_sessions=len(result.equity_curve),
        comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F,
        warnings=result.warnings,
    )


def test_champion_f_registry_resolves_only_f_configured_and_keeps_distinct_labels() -> None:
    assert research_family_runs(StrategyComparisonKind.RESEARCH_CHAMPION_F) == (
        (
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.CONFIGURED,
        ),
    )
    assert (
        comparison_strategy_label(
            StrategyComparisonKind.RESEARCH_CHAMPION_F,
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.CONFIGURED,
        )
        == "F/configured"
    )
    assert "F-intraday" in comparison_strategy_label(
        StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.INTRADAY_DYNAMIC,
    )
    assert "F-entry" in comparison_strategy_label(
        StrategyComparisonKind.RESEARCH_F_ENTRY,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.F_FIRST_HOUR_PULLBACK_CONFIGURED,
    )


def test_cli_accepts_exact_f_baseline_and_champion_validation_selectors() -> None:
    parser = _parser()
    baseline = parser.parse_args(
        [
            "backtest",
            "--start",
            "2024-01-02",
            "--end",
            "2026-08-12",
            "--variant",
            "F",
            "--strategy",
            "configured",
            "--output-stem",
            "fresh_baseline",
        ]
    )
    validation = parser.parse_args(
        [
            "validate-extended",
            "--target",
            "champion-f",
            "--start",
            "2024-01-02",
            "--end",
            "2026-08-12",
            "--output-stem",
            "fresh_validation",
        ]
    )

    assert (baseline.variant, baseline.strategy, baseline.output_stem) == (
        "F",
        "configured",
        "fresh_baseline",
    )
    assert (validation.target, validation.output_stem) == (
        "champion-f",
        "fresh_validation",
    )


def test_backtest_fresh_stem_refuses_to_overwrite(tmp_path) -> None:
    result = _result()

    paths = export_backtest(
        result,
        tmp_path,
        stem="fresh_f_baseline",
        overwrite=False,
    )

    assert paths["json"].name == "fresh_f_baseline.json"
    assert paths["equity"].name == "fresh_f_baseline_equity.csv"
    with pytest.raises(FileExistsError):
        export_backtest(
            result,
            tmp_path,
            stem="fresh_f_baseline",
            overwrite=False,
        )


def test_champion_cost_stress_rows_keep_canonical_assumptions_and_identity() -> None:
    comparisons = {"BASELINE": _comparison(_result())}
    for case, slippage_bps, commission_bps in CANONICAL_COST_STRESS_CASES:
        comparisons[case] = _comparison(
            _result(
                total_return=0.08,
                slippage_bps=slippage_bps,
                commission_bps=commission_bps,
            )
        )

    rows = cost_stress_rows(comparisons)

    assert [row["cost_case"] for row in rows] == [
        "BASELINE",
        "2X_SLIPPAGE",
        "3X_SLIPPAGE",
        "COMMISSION_SENSITIVITY",
    ]
    assert {(row["slippage_bps"], row["commission_bps"]) for row in rows} == {
        (5, 0),
        (10.0, 0.0),
        (15.0, 0.0),
        (5.0, 5.0),
    }
    assert all(row["strategy"] == "F/configured" for row in rows)


def test_champion_loso_and_symbol_concentration_are_explicitly_post_hoc() -> None:
    result = _result(positions=(_position("P1", "AAA", 20), _position("P2", "BBB", -5)))

    symbol_rows, concentration, loso_rows = symbol_and_leave_one_out(_comparison(result))

    aaa = next(row for row in symbol_rows if row["symbol"] == "AAA")
    without_aaa = next(row for row in loso_rows if row["scenario"] == "WITHOUT_AAA")
    assert aaa["share_of_total_net_pnl"] == pytest.approx(20 / 15)
    assert concentration["F/configured"]["top_5_pnl_concentration"] == pytest.approx(1.0)
    assert without_aaa["removed_symbol_pnl_contribution"] == 20
    assert without_aaa["position_count_difference"] == -1
    assert without_aaa["post_hoc_only"] is True
    assert without_aaa["loso_sharpe"] is None


def test_champion_calendar_subperiod_and_drawdown_views_use_equity_path() -> None:
    points = tuple(
        EquityPoint(
            date=session,
            cash=equity,
            market_value=0,
            portfolio_equity=equity,
            active_positions=0,
            exposure=0.2,
            end_of_day_exposure=0.1,
        )
        for session, equity in (
            (date(2024, 1, 2), 100),
            (date(2024, 12, 31), 110),
            (date(2025, 1, 2), 105),
            (date(2025, 12, 31), 115),
            (date(2026, 1, 2), 100),
            (date(2026, 8, 12), 120),
        )
    )
    result = _result(total_return=0.20, equity_curve=points)
    comparison = _comparison(result)

    yearly, summary = calendar_stability(comparison, "year")
    thirds = chronological_subperiod_analysis(comparison)
    drawdown = drawdown_diagnostics(result)

    assert [row["period"] for row in yearly] == ["2024", "2025", "2026"]
    assert yearly[0]["return"] == pytest.approx(0.10)
    assert yearly[1]["max_drawdown"] == pytest.approx(105 / 110 - 1)
    assert summary["F/configured"]["positive_years"] == 3
    assert [row["period"] for row in thirds] == ["THIRD_1", "THIRD_2", "THIRD_3"]
    assert drawdown["maximum_drawdown"] == pytest.approx(100 / 115 - 1)
    assert drawdown["drawdown_start"] == "2025-12-31"
    assert drawdown["drawdown_trough"] == "2026-01-02"
    assert drawdown["recovery_date"] == "2026-08-12"


def test_champion_export_uses_fresh_stem_and_explicit_research_identity(tmp_path) -> None:
    result = _result()
    comparison = _comparison(result)
    bundle = ChampionFValidationBundle(
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        comparison=comparison,
        cost_comparisons={"BASELINE": comparison},
        strategy_summary={"strategy": "F/configured"},
        monthly_rows=[],
        yearly_rows=[],
        chronological_subperiod_rows=[],
        time_stability={},
        symbol_rows=[],
        concentration={},
        leave_one_out_rows=[],
        path_cost_rows=[],
        full_cost_rows=[
            {
                "cost_case": "BASELINE",
                "slippage_bps": 5,
                "commission_bps": 0,
            }
        ],
        drawdown={},
    )

    paths = export_champion_f_validation(bundle, tmp_path, stem="fresh_f_validation")
    payload = json.loads(paths["robustness_summary"].read_text(encoding="utf-8"))

    assert paths["cost_stress"].name == "fresh_f_validation_cost_stress.csv"
    assert payload["strategy"] == {
        "label": "F/configured",
        "variant": "F",
        "position_management": "configured",
    }
    assert payload["research_status"] == "current historical research champion"
    assert payload["live_trading_designation"] is False
    with pytest.raises(FileExistsError):
        export_champion_f_validation(bundle, tmp_path, stem="fresh_f_validation")
