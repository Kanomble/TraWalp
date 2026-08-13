from datetime import date

import pytest

from trading_system.backtest.metrics import calculate_metrics, maximum_drawdown
from trading_system.models.backtest import BacktestTrade, EquityPoint, StrategyVariant


def _trade(pnl: float, return_pct: float, holding_days: int) -> BacktestTrade:
    return BacktestTrade(
        symbol=f"T{holding_days}",
        signal_date=date(2024, 1, 1),
        entry_date=date(2024, 1, 2),
        entry_reference_price=10,
        entry_price=10,
        exit_date=date(2024, 1, 2 + holding_days),
        exit_reference_price=10 * (1 + return_pct),
        exit_price=10 * (1 + return_pct),
        quantity=10,
        position_value=100,
        stop_price=9,
        target_price=11.2,
        quality_score=80,
        valuation_score=80,
        opportunity_score=80,
        timing_score=80,
        total_score=80,
        exit_reason="test",
        pnl=pnl,
        return_pct=return_pct,
        slippage=0,
        transaction_cost=0,
        holding_days=holding_days,
        strategy_variant=StrategyVariant.FULL,
    )


def test_performance_metrics_are_hand_verifiable() -> None:
    curve = [
        EquityPoint(
            date=date(2024, 1, 2),
            cash=50,
            market_value=50,
            portfolio_equity=100,
            active_positions=1,
            exposure=0.5,
        ),
        EquityPoint(
            date=date(2024, 1, 3),
            cash=0,
            market_value=120,
            portfolio_equity=120,
            active_positions=1,
            exposure=1,
        ),
        EquityPoint(
            date=date(2025, 1, 2),
            cash=90,
            market_value=0,
            portfolio_equity=90,
            active_positions=0,
            exposure=0,
        ),
    ]
    trades = [_trade(20, 0.2, 2), _trade(-10, -0.1, 4)]

    metrics = calculate_metrics(curve, trades, 100)

    assert metrics.total_return == pytest.approx(-0.1)
    assert metrics.cagr == pytest.approx(-0.1, abs=0.001)
    assert metrics.maximum_drawdown == pytest.approx(-0.25)
    assert metrics.win_rate == 0.5
    assert metrics.average_win == 0.2
    assert metrics.average_loss == -0.1
    assert metrics.profit_factor == 2
    assert metrics.expectancy_per_trade == 5
    assert metrics.number_of_trades == 2
    assert metrics.average_holding_period == 3
    assert metrics.portfolio_turnover == pytest.approx(410 / (310 / 3))
    assert metrics.exposure == 0.5
    assert metrics.sharpe_ratio is not None
    assert metrics.sortino_ratio == pytest.approx(-2.244994, rel=1e-5)


def test_undefined_metrics_remain_none_instead_of_misleading_zero() -> None:
    curve = [
        EquityPoint(
            date=date(2024, 1, 2),
            cash=100,
            market_value=0,
            portfolio_equity=100,
            active_positions=0,
            exposure=0,
        ),
        EquityPoint(
            date=date(2024, 1, 3),
            cash=100,
            market_value=0,
            portfolio_equity=100,
            active_positions=0,
            exposure=0,
        ),
    ]

    metrics = calculate_metrics(curve, [], 100)

    assert metrics.sharpe_ratio is None
    assert metrics.sortino_ratio is None
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.expectancy_per_trade is None
    assert maximum_drawdown([]) is None
