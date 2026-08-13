"""Typed, serializable Milestone-4 backtest results."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrategyVariant(StrEnum):
    QUALITY_VALUE = "A"
    QUALITY_VALUE_OPPORTUNITY = "B"
    FULL = "C"


class BacktestTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    signal_date: date
    entry_date: date
    entry_reference_price: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_date: date
    exit_reference_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    position_value: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    quality_score: float
    valuation_score: float
    opportunity_score: float | None = None
    timing_score: float | None = None
    total_score: float
    exit_reason: str
    pnl: float
    return_pct: float
    slippage: float = Field(ge=0)
    transaction_cost: float = Field(ge=0)
    holding_days: int = Field(ge=1)
    strategy_variant: StrategyVariant


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    cash: float = Field(ge=-1e-6)
    market_value: float = Field(ge=0)
    portfolio_equity: float = Field(ge=0)
    active_positions: int = Field(ge=0)
    exposure: float = Field(ge=0)
    realized_pnl: float = 0
    unrealized_pnl: float = 0


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_return: float | None = None
    cagr: float | None = None
    maximum_drawdown: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    win_rate: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    profit_factor: float | None = None
    expectancy_per_trade: float | None = None
    number_of_trades: int = Field(ge=0)
    average_holding_period: float | None = None
    portfolio_turnover: float | None = None
    exposure: float | None = None


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = "SPY"
    available: bool
    first_date: date | None = None
    last_date: date | None = None
    total_return: float | None = None
    cagr: float | None = None
    maximum_drawdown: float | None = None
    warning: str | None = None


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    generated_at: str
    strategy_variant: StrategyVariant
    initial_capital: float
    configuration: dict
    metrics: PerformanceMetrics
    benchmark: BenchmarkResult
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    skipped_entries: dict[str, int] = Field(default_factory=dict)
    data_diagnostics: dict = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class StrategyComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    generated_at: str
    variants: tuple[BacktestResult, ...]
    shared_screen_sessions: int = Field(ge=0)
    warnings: tuple[str, ...] = ()
