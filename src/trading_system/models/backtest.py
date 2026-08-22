"""Typed, serializable Milestone-4 backtest results."""

from __future__ import annotations

from datetime import date, datetime
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
    BASELINE_FIXED_STOP = "baseline-fixed-stop"
    FIXED_STOP_MAX_HOLD = "fixed-stop-max-hold"
    FIXED_STOP_TAKE_PROFIT = "fixed-stop-take-profit"
    FIXED_STOP_ATR_TRAILING = "fixed-stop-atr-trailing"
    FIXED_STOP_PARTIAL_ATR = "fixed-stop-partial-atr"
    D1_SWING_PROFIT_LOCK = "D1-swing-profit-lock"
    D2_SWING_RUNNER = "D2-swing-runner"
    D3_INTRADAY_TRAIL_GUARD = "D3-intraday-trail-guard"
    D4_INTRADAY_CONFIRMED_ENTRY = "D4-intraday-confirmed-entry"
    D5_HYBRID_CONFIRMED_SWING = "D5-hybrid-confirmed-swing"


class StrategyComparisonKind(StrEnum):
    ALL = "all"
    SCORE_VARIANTS = "score_variants"
    POSITION_MANAGEMENT = "position_management"
    RESEARCH_D1_D5 = "research_d1_d5"
    EXTENDED_VALIDATION = "extended_validation"


class StopLossClassification(StrEnum):
    NEVER_PROFITABLE = "never_profitable"
    PROFITABLE_THEN_STOPPED = "profitable_then_stopped"
    GAP_THROUGH_STOP = "gap_through_stop"
    NORMAL_STOP = "normal_stop"


class ScoreObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    total_score: float
    quality_score: float | None = None
    valuation_score: float | None = None
    opportunity_score: float | None = None
    timing_score: float | None = None


class EntryTriggerInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    price_above_sma20: bool = False
    rsi_recovery: bool = False
    momentum5_above_zero: bool = False
    relative_volume_above_threshold: bool = False


class BacktestTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    signal_date: date
    entry_date: date
    entry_timestamp: datetime | None = None
    entry_reference_price: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_date: date
    exit_timestamp: datetime | None = None
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
    position_id: str = ""
    execution_leg_id: str = ""
    daily_candidate_rank: int | None = Field(default=None, ge=1)
    daily_candidate_count: int | None = Field(default=None, ge=1)
    daily_candidate_score: float | None = None
    daily_candidate_variant: StrategyVariant | None = None
    confirmation_required: bool = False
    confirmation_bar_expected_timestamp: datetime | None = None
    confirmation_bar_timestamp: datetime | None = None
    confirmation_bar_present: bool = False
    confirmation_open: float | None = None
    confirmation_high: float | None = None
    confirmation_low: float | None = None
    confirmation_close: float | None = None
    confirmation_volume: int | None = Field(default=None, ge=0)
    confirmation_vwap: float | None = None
    confirmation_passed: bool | None = None
    confirmation_failure_reason: str | None = None
    intended_entry_timestamp: datetime | None = None
    actual_entry_timestamp: datetime | None = None
    entry_delayed_from_open: bool = False
    execution_bar_present: bool = False
    trail_guard_enabled: bool = False
    completed_bars_before_trail_arm: int | None = Field(default=None, ge=0)
    trail_armed_timestamp: datetime | None = None
    trail_armed_reference_price: float | None = None
    atr_at_trail_activation: float | None = None
    mfe_at_trail_activation: float | None = None
    initial_risk_per_share_R: float | None = Field(default=None, gt=0)
    maximum_mfe_in_R: float | None = None
    profit_lock_state: str | None = None
    profit_lock_activation_timestamp: datetime | None = None
    break_even_lock_timestamp: datetime | None = None
    one_r_lock_timestamp: datetime | None = None
    active_profit_lock_stop: float | None = None
    cooldown_applied: bool = False
    cooldown_blocked: bool = False
    cooldown_reason: str | None = None
    previous_position_net_return: float | None = None
    intraday_session_status: str | None = None
    opening_bar_complete: bool | None = None
    execution_bar_complete: bool | None = None
    gap_affected_trade: bool = False


class BacktestPosition(BaseModel):
    """One economic position, potentially composed of multiple exit execution legs."""

    model_config = ConfigDict(frozen=True)

    position_id: str
    symbol: str
    signal_date: date
    entry_date: date
    exit_date: date
    entry_timestamp: datetime | None = None
    exit_timestamp: datetime | None = None
    entry_reference_price: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_reference_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    initial_quantity: float = Field(gt=0)
    execution_legs: int = Field(ge=1)
    holding_days: int = Field(ge=1)
    gross_pnl: float
    net_pnl: float
    position_return: float
    transaction_cost: float = Field(ge=0)
    slippage: float = Field(ge=0)
    exit_reason: str
    maximum_favorable_excursion: float
    maximum_adverse_excursion: float
    profit_capture_ratio: float | None = None
    profit_giveback: float
    stop_loss_classification: StopLossClassification | None = None
    entry_score: float | None = None
    exit_score: float | None = None
    minimum_score_during_trade: float | None = None
    maximum_score_during_trade: float | None = None
    minimum_score_ratio: float | None = None
    maximum_score_ratio: float | None = None
    score_change_absolute: float | None = None
    score_change_percent: float | None = None
    quality_score: float | None = None
    valuation_score: float | None = None
    opportunity_score: float | None = None
    timing_score: float | None = None
    score_history: tuple[ScoreObservation, ...] = ()
    is_reentry: bool = False
    previous_exit_date: date | None = None
    days_since_previous_exit: int | None = Field(default=None, ge=0)
    previous_exit_reason: str | None = None
    previous_position_return: float | None = None
    previous_position_mfe: float | None = None
    previous_position_mae: float | None = None
    previous_entry_score: float | None = None
    current_entry_score: float | None = None
    score_change_since_previous_entry: float | None = None
    entry_triggers: EntryTriggerInfo = EntryTriggerInfo()
    fresh_trigger_since_previous_exit: bool | None = None
    post_exit_return_1d: float | None = None
    post_exit_return_3d: float | None = None
    post_exit_return_5d: float | None = None
    post_exit_return_10d: float | None = None
    post_exit_mfe_1d: float | None = None
    post_exit_mfe_3d: float | None = None
    post_exit_mfe_5d: float | None = None
    post_exit_mfe_10d: float | None = None
    post_exit_mae_1d: float | None = None
    post_exit_mae_3d: float | None = None
    post_exit_mae_5d: float | None = None
    post_exit_mae_10d: float | None = None
    daily_candidate_rank: int | None = Field(default=None, ge=1)
    daily_candidate_count: int | None = Field(default=None, ge=1)
    daily_candidate_score: float | None = None
    daily_candidate_variant: StrategyVariant | None = None
    confirmation_required: bool = False
    confirmation_bar_expected_timestamp: datetime | None = None
    confirmation_bar_timestamp: datetime | None = None
    confirmation_bar_present: bool = False
    confirmation_open: float | None = None
    confirmation_high: float | None = None
    confirmation_low: float | None = None
    confirmation_close: float | None = None
    confirmation_volume: int | None = Field(default=None, ge=0)
    confirmation_vwap: float | None = None
    confirmation_passed: bool | None = None
    confirmation_failure_reason: str | None = None
    intended_entry_timestamp: datetime | None = None
    actual_entry_timestamp: datetime | None = None
    entry_delayed_from_open: bool = False
    execution_bar_present: bool = False
    trail_guard_enabled: bool = False
    completed_bars_before_trail_arm: int | None = Field(default=None, ge=0)
    trail_armed_timestamp: datetime | None = None
    trail_armed_reference_price: float | None = None
    atr_at_trail_activation: float | None = None
    mfe_at_trail_activation: float | None = None
    initial_risk_per_share_R: float | None = Field(default=None, gt=0)
    maximum_mfe_in_R: float | None = None
    profit_lock_state: str | None = None
    profit_lock_activation_timestamp: datetime | None = None
    break_even_lock_timestamp: datetime | None = None
    one_r_lock_timestamp: datetime | None = None
    active_profit_lock_stop: float | None = None
    cooldown_applied: bool = False
    cooldown_blocked: bool = False
    cooldown_reason: str | None = None
    previous_position_net_return: float | None = None
    intraday_session_status: str | None = None
    opening_bar_complete: bool | None = None
    execution_bar_complete: bool | None = None
    gap_affected_trade: bool = False
    trade_path_complete: bool | None = None
    trade_path_missing_bar_count: int | None = Field(default=None, ge=0)
    trade_path_missing_timestamps: tuple[str, ...] = ()
    gap_before_exit: bool | None = None
    gap_after_exit_only: bool | None = None
    missing_opening_bar_affected_entry: bool | None = None


class ExecutionMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_legs: int = Field(ge=0)
    winning_execution_legs: int = Field(ge=0)
    losing_execution_legs: int = Field(ge=0)
    breakeven_execution_legs: int = Field(ge=0)
    execution_leg_win_rate: float | None = None
    execution_leg_loss_rate: float | None = None


class PositionMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    positions_opened: int = Field(ge=0)
    positions_closed: int = Field(ge=0)
    winning_positions: int = Field(ge=0)
    losing_positions: int = Field(ge=0)
    breakeven_positions: int = Field(ge=0)
    position_win_rate: float | None = None
    position_loss_rate: float | None = None
    average_position_return: float | None = None
    average_position_win: float | None = None
    average_position_loss: float | None = None
    best_position: float | None = None
    worst_position: float | None = None
    average_position_holding_period: float | None = None
    median_position_holding_period: float | None = None
    gross_position_profit: float = 0
    gross_position_loss: float = 0
    position_profit_factor: float | None = None
    average_mfe: float | None = None
    average_mae: float | None = None
    average_profit_capture: float | None = None
    average_profit_giveback: float | None = None
    never_profitable_stop_rate: float | None = None
    profitable_then_stopped_rate: float | None = None
    never_profitable_stop_positions: int = Field(default=0, ge=0)
    profitable_then_stopped_positions: int = Field(default=0, ge=0)
    gap_through_stop_positions: int = Field(default=0, ge=0)
    average_post_exit_return_5d: float | None = None
    average_post_exit_mfe_5d: float | None = None
    reentry_positions: int = Field(default=0, ge=0)
    reentries_without_fresh_trigger: int = Field(default=0, ge=0)


class ExitReasonDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_reason: str
    positions: int = Field(ge=0)
    average_mfe: float | None = None
    average_return: float | None = None
    average_capture: float | None = None
    average_giveback: float | None = None


class StopLossDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    classification: StopLossClassification
    positions: int = Field(ge=0)
    average_mfe: float | None = None
    average_mae: float | None = None
    average_holding_period: float | None = None
    average_loss: float | None = None


class PostExitReasonDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_reason: str
    positions: int = Field(ge=0)
    observations_1d: int = Field(default=0, ge=0)
    observations_3d: int = Field(default=0, ge=0)
    observations_5d: int = Field(default=0, ge=0)
    observations_10d: int = Field(default=0, ge=0)
    average_return_1d: float | None = None
    average_return_3d: float | None = None
    average_return_5d: float | None = None
    average_return_10d: float | None = None
    median_return_5d: float | None = None
    positive_forward_rate_5d: float | None = None
    negative_forward_rate_5d: float | None = None
    gained_over_3pct_rate_5d: float | None = None
    average_mfe_5d: float | None = None
    average_mae_5d: float | None = None


class EntryScoreDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    group: str
    positions: int = Field(ge=0)
    average_total_score: float | None = None
    average_quality_score: float | None = None
    average_valuation_score: float | None = None
    average_opportunity_score: float | None = None
    average_timing_score: float | None = None


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
    positions: tuple[BacktestPosition, ...] = ()
    position_metrics: PositionMetrics = PositionMetrics(
        positions_opened=0,
        positions_closed=0,
        winning_positions=0,
        losing_positions=0,
        breakeven_positions=0,
    )
    execution_metrics: ExecutionMetrics = ExecutionMetrics(
        execution_legs=0,
        winning_execution_legs=0,
        losing_execution_legs=0,
        breakeven_execution_legs=0,
    )
    profit_capture_by_exit_reason: tuple[ExitReasonDiagnostics, ...] = ()
    stop_loss_diagnostics: tuple[StopLossDiagnostics, ...] = ()
    post_exit_by_reason: tuple[PostExitReasonDiagnostics, ...] = ()
    entry_score_diagnostics: tuple[EntryScoreDiagnostics, ...] = ()
    equity_curve: tuple[EquityPoint, ...]
    skipped_entries: dict[str, int] = Field(default_factory=dict)
    data_diagnostics: dict = Field(default_factory=dict)
    performance_diagnostics: dict = Field(default_factory=dict)
    annualized_metrics_reliable: bool = False
    warnings: tuple[str, ...] = ()
    exits_by_reason: dict[str, int] = Field(default_factory=dict)
    strict_coverage_sensitivity: bool = False
    research_diagnostics: dict = Field(default_factory=dict)


class IntradayPrefetchTimeframe(BaseModel):
    """Compact, serializable diagnostics for one comparison timeframe."""

    model_config = ConfigDict(frozen=True)

    candidate_symbols: int = Field(ge=0)
    already_complete_symbols: int = Field(ge=0)
    sync_requested_symbols: int = Field(ge=0)
    warmup_bars: int = Field(ge=1)
    extended_hours: bool
    bars_added: int = Field(default=0, ge=0)
    provider_requests: int = Field(default=0, ge=0)
    failure_reasons: tuple[str, ...] = ()


class IntradayPrefetch(BaseModel):
    """Preparation metadata kept alongside a strategy comparison report."""

    model_config = ConfigDict(frozen=True)

    required: bool = False
    enabled: bool = False
    candidate_symbols: int = Field(default=0, ge=0)
    timeframes: dict[str, IntradayPrefetchTimeframe] = Field(default_factory=dict)


class StrategyComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    generated_at: str
    variants: tuple[BacktestResult, ...]
    shared_screen_sessions: int = Field(ge=0)
    comparison_kind: StrategyComparisonKind = StrategyComparisonKind.SCORE_VARIANTS
    skipped_strategies: dict[str, str] = Field(default_factory=dict)
    intraday_prefetch: IntradayPrefetch = IntradayPrefetch()
    data_qualification: dict = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    strict_coverage_sensitivity: bool = False
    research_diagnostics: dict = Field(default_factory=dict)
