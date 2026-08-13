"""Pure, deterministic performance calculations for simulated portfolios."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from trading_system.models.backtest import BacktestTrade, EquityPoint, PerformanceMetrics


def calculate_metrics(
    equity_curve: Sequence[EquityPoint],
    trades: Sequence[BacktestTrade],
    initial_capital: float,
    *,
    annualization: int = 252,
) -> PerformanceMetrics:
    if not equity_curve:
        return PerformanceMetrics(number_of_trades=len(trades))
    equities = np.asarray([point.portfolio_equity for point in equity_curve], dtype=float)
    total_return = equities[-1] / initial_capital - 1
    elapsed_days = (equity_curve[-1].date - equity_curve[0].date).days
    cagr = (
        (equities[-1] / initial_capital) ** (365.25 / elapsed_days) - 1
        if elapsed_days > 0 and equities[-1] > 0
        else None
    )
    peaks = np.maximum.accumulate(np.concatenate(([initial_capital], equities)))
    drawdowns = np.concatenate(([initial_capital], equities)) / peaks - 1
    maximum_drawdown = float(drawdowns.min())
    returns = equities[1:] / equities[:-1] - 1 if len(equities) > 1 else np.asarray([])
    sharpe = _annualized_ratio(returns, returns, annualization)
    downside_deviation = (
        float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2))) if len(returns) else 0.0
    )
    sortino = (
        float(np.mean(returns) / downside_deviation * math.sqrt(annualization))
        if downside_deviation > 0
        else None
    )
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    elapsed_months = elapsed_days / (365.25 / 12) if elapsed_days > 0 else None
    average_equity = float(equities.mean()) if len(equities) else initial_capital
    traded_notional = sum(
        trade.position_value + trade.exit_price * trade.quantity for trade in trades
    )
    return PerformanceMetrics(
        total_return=float(total_return),
        cagr=float(cagr) if cagr is not None else None,
        maximum_drawdown=maximum_drawdown,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        win_rate=len(wins) / len(trades) if trades else None,
        loss_rate=len(losses) / len(trades) if trades else None,
        average_win=(sum(trade.return_pct for trade in wins) / len(wins) if wins else None),
        average_loss=(sum(trade.return_pct for trade in losses) / len(losses) if losses else None),
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        expectancy_per_trade=(sum(trade.pnl for trade in trades) / len(trades) if trades else None),
        number_of_trades=len(trades),
        average_holding_period=(
            sum(trade.holding_days for trade in trades) / len(trades) if trades else None
        ),
        median_holding_period=(
            float(np.median([trade.holding_days for trade in trades])) if trades else None
        ),
        trades_per_month=(len(trades) / elapsed_months if elapsed_months else None),
        portfolio_turnover=(traded_notional / average_equity if average_equity > 0 else None),
        trading_costs=sum(trade.transaction_cost for trade in trades),
        slippage_costs=sum(trade.slippage for trade in trades),
        best_trade=max((trade.return_pct for trade in trades), default=None),
        worst_trade=min((trade.return_pct for trade in trades), default=None),
        exposure=(
            sum(point.exposure for point in equity_curve) / len(equity_curve)
            if equity_curve
            else None
        ),
        end_of_day_exposure=(
            sum(point.end_of_day_exposure for point in equity_curve) / len(equity_curve)
            if equity_curve
            else None
        ),
    )


def maximum_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    peaks = np.maximum.accumulate(array)
    return float((array / peaks - 1).min())


def _annualized_ratio(
    returns: np.ndarray, deviation_sample: np.ndarray, annualization: int
) -> float | None:
    if len(returns) < 2 or len(deviation_sample) < 2:
        return None
    deviation = float(np.std(deviation_sample, ddof=1))
    if not math.isfinite(deviation) or deviation == 0:
        return None
    return float(np.mean(returns) / deviation * math.sqrt(annualization))
