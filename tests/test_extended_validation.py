from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest import validation
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    PerformanceMetrics,
    PositionManagementPreset,
    PositionMetrics,
    StrategyComparison,
    StrategyVariant,
)
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, MarketDataBar, TradableAsset


def _bar(symbol: str, timestamp: datetime, timeframe=BarTimeframe.MINUTES_15):
    return MarketDataBar(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=100,
        trade_count=10,
        vwap=Decimal("100"),
    )


def _session_bars(symbol: str, session: date) -> list[MarketDataBar]:
    opening, closing = regular_session_bounds(session)
    bars = []
    current = opening
    while current < closing:
        bars.append(_bar(symbol, current))
        current += timedelta(minutes=15)
    return bars


def _position(
    position_id: str,
    symbol: str,
    pnl: float,
    *,
    signal_date: date = date(2025, 5, 1),
    entry_timestamp: datetime | None = None,
    exit_timestamp: datetime | None = None,
    exit_reason: str = "max_hold",
) -> BacktestPosition:
    entry_timestamp = entry_timestamp or datetime(2025, 5, 2, 13, 30, tzinfo=UTC)
    exit_timestamp = exit_timestamp or datetime(2025, 5, 2, 14, 0, tzinfo=UTC)
    return BacktestPosition(
        position_id=position_id,
        symbol=symbol,
        signal_date=signal_date,
        entry_date=entry_timestamp.date(),
        exit_date=exit_timestamp.date(),
        entry_timestamp=entry_timestamp,
        exit_timestamp=exit_timestamp,
        entry_reference_price=100,
        entry_price=100.05,
        exit_reference_price=101,
        exit_price=100.9495,
        initial_quantity=10,
        execution_legs=1,
        holding_days=1,
        gross_pnl=pnl,
        net_pnl=pnl,
        position_return=pnl / 1_000.5,
        transaction_cost=0,
        slippage=1,
        exit_reason=exit_reason,
        maximum_favorable_excursion=0.03,
        maximum_adverse_excursion=-0.01,
        profit_giveback=0.03 - pnl / 1_000.5,
    )


def _result(
    positions: list[BacktestPosition],
    *,
    variant: StrategyVariant = StrategyVariant.FULL,
    preset: PositionManagementPreset = PositionManagementPreset.D1_SWING_PROFIT_LOCK,
    trades: tuple[BacktestTrade, ...] = (),
    total_return: float | None = None,
    profit_factor: float | None = None,
    expectancy: float | None = None,
) -> BacktestResult:
    pnls = [item.net_pnl for item in positions]
    return BacktestResult.model_construct(
        strategy_variant=variant,
        position_management_preset=preset,
        initial_capital=10_000.0,
        positions=tuple(positions),
        trades=trades,
        metrics=PerformanceMetrics(
            total_return=sum(pnls) / 10_000 if total_return is None else total_return,
            maximum_drawdown=-0.01,
            expectancy_per_trade=(
                (sum(pnls) / len(pnls) if pnls else None)
                if expectancy is None
                else expectancy
            ),
            number_of_trades=len(trades),
            portfolio_turnover=1.0,
        ),
        position_metrics=PositionMetrics(
            positions_opened=len(positions),
            positions_closed=len(positions),
            winning_positions=sum(item.net_pnl > 0 for item in positions),
            losing_positions=sum(item.net_pnl < 0 for item in positions),
            breakeven_positions=sum(item.net_pnl == 0 for item in positions),
            position_profit_factor=(
                validation._profit_factor(pnls)
                if profit_factor is None
                else profit_factor
            ),
        ),
        research_diagnostics={},
        configuration={"backtest": {"slippage_bps": 5, "commission_bps": 0}},
    )


def _comparison(*results: BacktestResult) -> StrategyComparison:
    return StrategyComparison.model_construct(
        actual_start=date(2025, 5, 1),
        actual_end=date(2025, 5, 31),
        variants=tuple(results),
    )


def test_earliest_qualified_validation_start_shifts_only_until_exact_warmup(
    tmp_path,
) -> None:
    requested = date(2025, 5, 1)
    next_session = trading_sessions_between(requested, date(2025, 5, 5))[1]
    warmup = trading_sessions_between(daily_warmup_start(requested, 300), requested)
    database = Database(tmp_path / "qualified.sqlite3")
    database.initialize()
    database.upsert_assets(
        [TradableAsset(symbol="AAA", name="AAA", tradable=True, fractionable=True)]
    )
    database.upsert_company(
        CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571")
    )
    daily_bars = [
        _bar(
            symbol,
            datetime.combine(session, datetime.min.time(), tzinfo=UTC),
            BarTimeframe.DAY_1,
        )
        for symbol in ("AAA", "SPY")
        for session in warmup
        if symbol == "SPY" or session != warmup[0]
    ]
    daily_bars.extend(
        _bar(
            symbol,
            datetime.combine(next_session, datetime.min.time(), tzinfo=UTC),
            BarTimeframe.DAY_1,
        )
        for symbol in ("AAA", "SPY")
    )
    database.upsert_bars(daily_bars)
    load_settings.cache_clear()

    result = validation.qualify_validation_start(
        database, load_settings().strategy, requested, next_session
    )

    assert result["qualified"] is True
    assert result["actual_first_qualified_screen_session"] == next_session.isoformat()
    assert result["required_prior_sessions"] == 300
    assert result["symbols_rejected_insufficient_market_history"] == 0


@pytest.mark.parametrize(
    ("missing_index", "complete", "after_only"),
    [(1, False, False), (20, True, True)],
)
def test_trade_path_coverage_distinguishes_before_and_after_exit_gap(
    tmp_path, missing_index, complete, after_only
) -> None:
    session = date(2025, 5, 2)
    bars = _session_bars("AAA", session)
    database = Database(tmp_path / f"path-{missing_index}.sqlite3")
    database.initialize()
    database.upsert_bars(
        [bar for index, bar in enumerate(bars) if index != missing_index]
    )
    position = _position(
        "P1",
        "AAA",
        10,
        signal_date=date(2025, 5, 1),
        entry_timestamp=bars[0].timestamp,
        exit_timestamp=bars[2].timestamp,
    )
    result = _result([position], preset=PositionManagementPreset.INTRADAY_DYNAMIC)

    annotated, rows = validation.annotate_trade_path_coverage(database, result)

    assert annotated.positions[0].trade_path_complete is complete
    assert annotated.positions[0].gap_before_exit is (not complete)
    assert annotated.positions[0].gap_after_exit_only is after_only
    assert rows[0]["trade_path_missing_bar_count"] == (0 if complete else 1)


def test_paired_d1_analysis_separates_direct_and_path_effect() -> None:
    shared_control = _position("C1", "AAA", 10)
    shared_d1 = _position("D1", "AAA", 30, exit_reason="profit_lock")
    control_only = _position("C2", "BBB", -5, signal_date=date(2025, 5, 5))
    d1_only = _position("D2", "CCC", 15, signal_date=date(2025, 5, 6))

    result = validation.paired_d1_analysis(
        _result([shared_control, control_only]), _result([shared_d1, d1_only])
    )

    assert result["paired_positions"] == 1
    assert result["changed_exit_positions"] == 1
    assert result["direct_exit_management_pnl_effect"] == pytest.approx(20)
    assert result["total_closed_position_pnl_difference"] == pytest.approx(40)
    assert result["subsequent_portfolio_path_pnl_effect"] == pytest.approx(20)
    assert result["subsequent_portfolio_path_changes"] == 2


def test_leave_one_symbol_out_flags_single_symbol_dependency() -> None:
    result = _result([_position("P1", "AAA", 20), _position("P2", "BBB", -5)])

    _, summaries, rows = validation.symbol_and_leave_one_out(_comparison(result))

    label = "D1/C-swing-profit-lock"
    assert summaries[label]["best_contributing_symbol"] == "AAA"
    assert summaries[label]["profitability_disappears_without_best"] is True
    without_aaa = next(row for row in rows if row["scenario"] == "WITHOUT_AAA")
    assert without_aaa["sum_net_pnl"] == -5


def test_bootstrap_seed_is_deterministic() -> None:
    result = _result([_position("P1", "AAA", 20), _position("P2", "BBB", -5)])

    first = validation.bootstrap_uncertainty(result, seed=7, resamples=500)
    second = validation.bootstrap_uncertainty(result, seed=7, resamples=500)

    assert first == second
    assert first["probability_bootstrap_mean_gt_zero"] == pytest.approx(0.758)


def test_path_preserving_cost_stress_keeps_execution_path() -> None:
    position = _position("P1", "AAA", 9)
    trade = BacktestTrade(
        symbol="AAA",
        signal_date=position.signal_date,
        entry_date=position.entry_date,
        entry_timestamp=position.entry_timestamp,
        entry_reference_price=100,
        entry_price=100.05,
        exit_date=position.exit_date,
        exit_timestamp=position.exit_timestamp,
        exit_reference_price=101,
        exit_price=100.9495,
        quantity=10,
        position_value=1_000.5,
        quality_score=80,
        valuation_score=80,
        total_score=80,
        exit_reason="max_hold",
        pnl=9,
        return_pct=0.009,
        slippage=1.005,
        transaction_cost=0,
        holding_days=1,
        strategy_variant=StrategyVariant.FULL,
        net_pnl=9,
        position_id="P1",
        execution_leg_id="P1-L01",
    )
    result = _result([position], trades=(trade,))

    rows = validation.path_preserving_cost_stress(_comparison(result))

    assert len({row["execution_path_hash"] for row in rows}) == 1
    assert all(row["execution_path_unchanged"] for row in rows)
    assert rows[1]["total_return"] < rows[0]["total_return"]


def test_reference_regression_is_exact_and_detects_drift() -> None:
    results = []
    mappings = (
        (StrategyVariant.FULL, PositionManagementPreset.CONFIGURED, "C/configured"),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.D1_SWING_PROFIT_LOCK,
            "D1/C-swing-profit-lock",
        ),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.D2_SWING_RUNNER,
            "D2/C-swing-runner",
        ),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.INTRADAY_DYNAMIC,
            "C/intraday-dynamic",
        ),
    )
    for variant, preset, label in mappings:
        expected = validation.REFERENCE_EXPECTATIONS[label]
        results.append(
            _result(
                [],
                variant=variant,
                preset=preset,
                total_return=expected["total_return"],
                profit_factor=expected.get("position_profit_factor"),
            )
        )

    assert validation.reference_regression(_comparison(*results))["passed"] is True
    drifted = results[0].model_copy(
        update={
            "metrics": results[0].metrics.model_copy(update={"total_return": 0.0})
        }
    )
    assert (
        validation.reference_regression(_comparison(drifted, *results[1:]))[
            "passed"
        ]
        is False
    )


def test_export_refuses_to_overwrite_before_accessing_bundle_payload(tmp_path) -> None:
    existing = tmp_path / "extended_validation_2025-05-01_2026-04-30_summary.csv"
    existing.write_text("existing\n", encoding="utf-8")
    bundle = SimpleNamespace(
        requested_start=date(2025, 5, 1),
        requested_end=date(2026, 4, 30),
        reference_start=date(2026, 5, 1),
        reference_end=date(2026, 8, 12),
    )

    with pytest.raises(FileExistsError, match="already exists"):
        validation.export_extended_validation(bundle, tmp_path)
    assert existing.read_text(encoding="utf-8") == "existing\n"


def test_sparse_sensitivity_rows_export_with_deterministic_field_union() -> None:
    rows = [
        {"strategy": "C/intraday-dynamic", "sensitivity": "NATIVE"},
        {
            "strategy": "C/intraday-dynamic",
            "sensitivity": "STRICT_TRADE_PATH",
            "post_hoc_position_filter": True,
        },
    ]

    assert validation._field_union(rows) == [
        "strategy",
        "sensitivity",
        "post_hoc_position_filter",
    ]


def test_oos_qualification_failure_stops_before_screen_preparation(monkeypatch) -> None:
    monkeypatch.setattr(
        validation,
        "qualify_validation_start",
        lambda *_args: {"qualified": False, "failure_reason": "missing warmup"},
    )
    monkeypatch.setattr(
        validation,
        "prepare_strategy_comparison",
        lambda *_args, **_kwargs: pytest.fail("screen preparation must not run"),
    )
    load_settings.cache_clear()

    with pytest.raises(ValueError, match="missing warmup"):
        validation.run_extended_validation(
            SimpleNamespace(),
            load_settings().strategy,
            date(2025, 5, 1),
            date(2026, 4, 30),
            date(2026, 5, 1),
            date(2026, 8, 12),
        )


def test_d2_promotion_and_d1b_robustness_rules_are_conservative() -> None:
    def summary(total_return, profit_factor, expectancy, drawdown=-0.02):
        return {
            "total_return": total_return,
            "position_profit_factor": profit_factor,
            "expectancy": expectancy,
            "max_drawdown": drawdown,
            "runners": 2,
            "largest_runner_pnl_share": 0.9,
        }

    summaries = {
        "C/configured": summary(0.02, 1.4, 0.2),
        "D1/C-swing-profit-lock": summary(0.03, 1.8, 0.3),
        "D2/C-swing-runner": summary(0.04, 1.2, 0.1),
        "C/intraday-dynamic": summary(0.02, 1.3, 0.1),
        "B/configured": summary(0.03, 1.5, 0.3, -0.01),
        "D1/B-swing-profit-lock": summary(0.01, 1.1, 0.1, -0.03),
    }
    costs = [
        {
            "cost_case": case,
            "strategy": strategy,
            "total_return": 0.01,
        }
        for case in ("2X_SLIPPAGE", "3X_SLIPPAGE")
        for strategy in ("D1/C-swing-profit-lock", "C/intraday-dynamic")
    ]
    decisions = validation.classify_validation(
        summaries,
        {"direct_exit_management_pnl_effect": 1},
        {
            "D1/C-swing-profit-lock": {
                "profitability_disappears_without_best": False
            },
            "C/intraday-dynamic": {
                "profitability_disappears_without_best": False
            },
        },
        {
            "D1/C-swing-profit-lock": {
                "positive_months": 3,
                "negative_months": 2,
            }
        },
        costs,
        [{"sensitivity": "STRICT_TRADE_PATH", "total_return": 0.01}],
    )

    assert decisions["D2/C"] == "INCONCLUSIVE"
    assert decisions["D1/B"] == "WORSE THAN B"
