"""Typed, serializable Milestone-4 backtest results."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrategyVariant(StrEnum):
    QUALITY_VALUE = "A"
    QUALITY_VALUE_OPPORTUNITY = "B"
    FULL = "C"


class PositionManagementPreset(StrEnum):
    CONFIGURED = "configured"
    LEGACY = "legacy"
    DYNAMIC_HOLD = "dynamic-hold"
    TAKE_PROFIT = "take-profit"
    ATR_TRAILING = "atr-trailing"
    PARTIAL_PROFIT = "partial-profit"
    INTRADAY_DYNAMIC = "intraday-dynamic"


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
    stop_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
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
    entry_score: float | None = None
    exit_score: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    highest_price_during_trade: float | None = Field(default=None, gt=0)
    lowest_price_during_trade: float | None = Field(default=None, gt=0)
    maximum_favorable_excursion: float | None = None
    maximum_adverse_excursion: float | None = None
    fees: float | None = Field(default=None, ge=0)
    slippage_cost: float | None = Field(default=None, ge=0)
    is_partial_exit: bool = False
    partial_level: int | None = Field(default=None, ge=0)


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    cash: float = Field(ge=-1e-6)
    market_value: float = Field(ge=0)
    portfolio_equity: float = Field(ge=0)
    active_positions: int = Field(ge=0)
    exposure: float = Field(ge=0)
    session_exposure: float = Field(default=0, ge=0)
    end_of_day_exposure: float = Field(default=0, ge=0)
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
    loss_rate: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    profit_factor: float | None = None
    expectancy_per_trade: float | None = None
    number_of_trades: int = Field(ge=0)
    average_holding_period: float | None = None
    median_holding_period: float | None = None
    trades_per_month: float | None = None
    portfolio_turnover: float | None = None
    trading_costs: float = Field(default=0, ge=0)
    slippage_costs: float = Field(default=0, ge=0)
    best_trade: float | None = None
    worst_trade: float | None = None
    exposure: float | None = None
    end_of_day_exposure: float | None = None


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
    position_management_preset: PositionManagementPreset = PositionManagementPreset.LEGACY
    initial_capital: float
    configuration: dict
    metrics: PerformanceMetrics
    benchmark: BenchmarkResult
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    skipped_entries: dict[str, int] = Field(default_factory=dict)
    data_diagnostics: dict = Field(default_factory=dict)
    performance_diagnostics: dict = Field(default_factory=dict)
    annualized_metrics_reliable: bool = False
    warnings: tuple[str, ...] = ()
    exits_by_reason: dict[str, int] = Field(default_factory=dict)


class StrategyComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    generated_at: str
    variants: tuple[BacktestResult, ...]
    shared_screen_sessions: int = Field(ge=0)
    comparison_kind: str = "score_variants"
    warnings: tuple[str, ...] = ()
