"""Central environment and strategy configuration."""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_system.models.market_data import BarTimeframe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STRATEGY_PATH = PROJECT_ROOT / "config" / "strategy.yaml"


class UniverseConfig(BaseModel):
    min_price: float = Field(5.0, gt=0)
    min_market_cap: float = Field(1_000_000_000, gt=0)
    min_avg_dollar_volume_20d: float = Field(10_000_000, gt=0)
    exclude_financials: bool = True
    exclude_reits: bool = True
    market_data_days: int = Field(320, ge=300)
    market_data_feed: Literal["iex", "sip"] = "iex"
    market_data_adjustment: Literal["all", "split"] = "all"


class StorageConfig(BaseModel):
    database_path: Path = Path("data/trading_system.sqlite3")
    reports_path: Path = Path("reports")


class SecConfig(BaseModel):
    request_interval_seconds: float = Field(0.11, ge=0.1)
    timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(4, ge=0)
    companyfacts_unavailable_ttl_days: int = Field(7, ge=1, le=90)


class PeerConfig(BaseModel):
    min_peer_count: int = Field(8, ge=2)


class DataQualityConfig(BaseModel):
    min_available_quality_metrics: int = Field(4, ge=1, le=6)
    min_available_valuation_metrics: int = Field(2, ge=1, le=3)
    min_market_history_days: int = Field(300, ge=252)


class FilterConfig(BaseModel):
    min_quality_score: float = Field(65, ge=0, le=100)
    min_valuation_score: float = Field(55, ge=0, le=100)
    min_total_score: float = Field(70, ge=0, le=100)
    require_positive_ocf: bool = True


class PortfolioConfig(BaseModel):
    max_positions: int = Field(5, ge=1)
    max_position_pct: float = Field(0.20, gt=0, le=1)
    max_sector_positions: int = Field(2, ge=1)


class RiskConfig(BaseModel):
    risk_per_trade: float = Field(0.01, gt=0, le=1)
    atr_stop_multiple: float = Field(2.0, gt=0)
    max_stop_loss_pct: float = Field(0.10, gt=0, lt=1)


class BacktestConfig(BaseModel):
    initial_capital: float = Field(100_000, gt=0)
    slippage_bps: float = Field(5, ge=0)
    commission_bps: float = Field(0, ge=0)
    profit_target_pct: float = Field(0.12, gt=0)
    max_holding_days: int = Field(10, ge=1)
    min_total_score: float = Field(75, ge=0, le=100)
    min_quality_score: float = Field(70, ge=0, le=100)
    min_valuation_score: float = Field(60, ge=0, le=100)
    min_opportunity_score: float = Field(60, ge=0, le=100)
    min_timing_score: float = Field(55, ge=0, le=100)
    min_relative_volume: float = Field(1.2, gt=0)


class StopLossConfig(BaseModel):
    """Fixed stop; ``percent=None`` retains the legacy ATR-sized entry stop."""

    enabled: bool = True
    percent: float | None = Field(default=None, gt=0, lt=1)


class TakeProfitConfig(BaseModel):
    """Fixed target; ``percent=None`` retains ``backtest.profit_target_pct``."""

    enabled: bool = True
    percent: float | None = Field(default=None, gt=0)


class TrailingStopConfig(BaseModel):
    enabled: bool = False
    activation_profit: float = Field(0.01, ge=0)
    trailing_distance: float = Field(0.006, gt=0, lt=1)


class AtrTrailingStopConfig(BaseModel):
    enabled: bool = False
    atr_period: int = Field(14, ge=2)
    atr_multiplier: float = Field(1.0, gt=0)
    activation_profit: float = Field(0.0, ge=0)
    minimum_completed_bars_before_activation: int = Field(0, ge=0)


class ProfitLockConfig(BaseModel):
    """Research-only R milestones; disabled by every production preset."""

    enabled: bool = False
    break_even_activation_r: float = Field(1.0, gt=0)
    one_r_activation_r: float = Field(2.0, gt=0)
    locked_profit_r: float = Field(1.0, gt=0)


class SignalDecayConfig(BaseModel):
    enabled: bool = False
    minimum_score_ratio: float = Field(0.75, gt=0, le=1)


class PartialTakeProfitLevel(BaseModel):
    profit: float = Field(gt=0)
    sell_fraction: float = Field(gt=0, le=1)
    quantity_basis: Literal["current", "original"] = "current"


class PartialTakeProfitConfig(BaseModel):
    enabled: bool = False
    levels: list[PartialTakeProfitLevel] = Field(
        default_factory=lambda: [PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)]
    )

    @model_validator(mode="after")
    def validate_levels(self) -> PartialTakeProfitConfig:
        profits = [level.profit for level in self.levels]
        if len(profits) != len(set(profits)) or profits != sorted(profits):
            raise ValueError("Partial take-profit levels must be unique and sorted by profit")
        return self


class MaxHoldConfig(BaseModel):
    enabled: bool = True
    days: int | None = Field(default=None, ge=1)
    mode: Literal["hard", "review", "disabled"] = "hard"
    review_minimum_score_ratio: float = Field(0.75, gt=0, le=1)


class PortfolioRotationConfig(BaseModel):
    enabled: bool = False
    minimum_score_improvement: float = Field(0.15, gt=0)
    minimum_holding_days: int = Field(1, ge=0)


class ReentryConfig(BaseModel):
    enabled: bool = True
    cooldown_days: int = Field(0, ge=0)


class IntradaySyncConfig(BaseModel):
    incremental: bool = True
    overlap_bars: int = Field(2, ge=0, le=100)
    symbol_batch_size: int = Field(25, ge=1, le=200)
    request_window_days: int = Field(7, ge=1, le=31)


class IntradayConfig(BaseModel):
    enabled: bool = False
    timeframes: list[BarTimeframe] = Field(default_factory=lambda: [BarTimeframe.MINUTES_15])
    extended_hours: bool = False
    warmup_bars: int = Field(50, ge=1)
    sync: IntradaySyncConfig = IntradaySyncConfig()

    @model_validator(mode="after")
    def validate_timeframes(self) -> IntradayConfig:
        if any(not timeframe.intraday for timeframe in self.timeframes):
            raise ValueError("intraday.timeframes may contain only 5m, 15m, and 1h")
        if len(self.timeframes) != len(set(self.timeframes)):
            raise ValueError("intraday.timeframes must be unique")
        return self


class PositionManagementConfig(BaseModel):
    """Composable position rules. Defaults reproduce the original backtester."""

    bar_timeframe: BarTimeframe = BarTimeframe.DAY_1
    stop_loss: StopLossConfig = StopLossConfig()
    take_profit: TakeProfitConfig = TakeProfitConfig()
    trailing_stop: TrailingStopConfig = TrailingStopConfig()
    atr_trailing_stop: AtrTrailingStopConfig = AtrTrailingStopConfig()
    profit_lock: ProfitLockConfig = ProfitLockConfig()
    signal_decay: SignalDecayConfig = SignalDecayConfig()
    partial_take_profit: PartialTakeProfitConfig = PartialTakeProfitConfig()
    max_hold: MaxHoldConfig = MaxHoldConfig()
    portfolio_rotation: PortfolioRotationConfig = PortfolioRotationConfig()
    reentry: ReentryConfig = ReentryConfig()


class TechnicalConfig(BaseModel):
    rsi_oversold: float = Field(30, ge=0, le=100)
    rsi_recovery_min: float = Field(35, ge=0, le=100)
    rsi_recovery_max: float = Field(60, ge=0, le=100)
    rsi_recovery_lookback: int = Field(10, ge=1)
    sma_slope_lookback: int = Field(5, ge=1)

    @model_validator(mode="after")
    def validate_rsi_thresholds(self) -> TechnicalConfig:
        if not self.rsi_oversold < self.rsi_recovery_min < self.rsi_recovery_max:
            raise ValueError("RSI thresholds must satisfy oversold < recovery_min < recovery_max")
        return self


class LossAwareRecoveryScreenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_recovery_from_63d_low: float = Field(0.05, ge=0)
    max_drawdown_126d_floor: float = Field(-0.40, ge=-1, le=0)
    structural_momentum126_threshold: float = Field(-0.25, ge=-1, le=0)
    structural_sma200_distance_threshold: float = Field(-0.15, ge=-1, le=0)


class TrendPullbackScreenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_drawdown_63d: float = Field(-0.20, ge=-1, le=0)
    max_drawdown_63d: float = Field(-0.05, ge=-1, le=0)
    min_momentum126: float = 0.0
    require_price_above_sma200: bool = True
    require_price_above_sma20: bool = True
    min_momentum5: float = 0.0
    require_sma20_rising: bool = True

    @model_validator(mode="after")
    def validate_pullback_range(self) -> TrendPullbackScreenConfig:
        if self.min_drawdown_63d > self.max_drawdown_63d:
            raise ValueError("trend pullback drawdown minimum must not exceed maximum")
        return self


class QualityValueMomentumScreenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_drawdown_52w: float = Field(-0.10, ge=-1, le=0)
    min_momentum126: float = 0.0
    require_price_above_sma50: bool = True
    require_price_above_sma200: bool = True
    require_sma20_rising: bool = True


class ScreenStrategiesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loss_aware_recovery: LossAwareRecoveryScreenConfig = LossAwareRecoveryScreenConfig()
    trend_pullback: TrendPullbackScreenConfig = TrendPullbackScreenConfig()
    quality_value_momentum: QualityValueMomentumScreenConfig = QualityValueMomentumScreenConfig()


class WeightedFactors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> WeightedFactors:
        values = [value for value in self.__dict__.values() if isinstance(value, (int, float))]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Score weights must be finite")
        if abs(sum(values) - 1.0) > 1e-9:
            raise ValueError("Score weights must sum to 1.0")
        if any(value < 0 for value in values):
            raise ValueError("Score weights cannot be negative")
        return self


class TotalWeights(WeightedFactors):
    quality: float
    valuation: float
    opportunity: float
    timing: float


class QualityWeights(WeightedFactors):
    revenue_growth: float
    eps_growth: float
    operating_cash_flow: float
    operating_margin: float
    roic: float
    debt_to_ebitda: float


class ValuationWeights(WeightedFactors):
    relative_pe: float
    relative_ev_ebitda: float
    fcf_yield: float


class OpportunityWeights(WeightedFactors):
    drawdown_52w: float
    medium_term_weakness: float
    volatility: float


class TimingWeights(WeightedFactors):
    rsi_recovery: float
    moving_average_recovery: float
    momentum: float
    relative_volume: float


class ScoreConfig(BaseModel):
    total: TotalWeights
    quality: QualityWeights
    valuation: ValuationWeights
    opportunity: OpportunityWeights
    timing: TimingWeights
    winsor_lower_quantile: float = Field(0.05, ge=0, lt=0.5)
    winsor_upper_quantile: float = Field(0.95, gt=0.5, le=1)
    drawdown_curve: list[tuple[float, float]] = [
        (-0.80, 0),
        (-0.50, 30),
        (-0.35, 100),
        (-0.20, 90),
        (-0.10, 30),
        (0.0, 0),
    ]
    operating_margin_curve: list[tuple[float, float]] = [
        (-0.10, 0),
        (0.00, 25),
        (0.10, 60),
        (0.20, 85),
        (0.35, 100),
    ]
    revenue_growth_curve: list[tuple[float, float]] = [
        (-0.20, 0),
        (0.00, 35),
        (0.05, 55),
        (0.15, 80),
        (0.30, 100),
    ]
    eps_growth_curve: list[tuple[float, float]] = [
        (-0.30, 0),
        (0.00, 35),
        (0.10, 60),
        (0.25, 85),
        (0.50, 100),
    ]
    operating_cash_flow_growth_curve: list[tuple[float, float]] = [
        (-0.20, 0),
        (0.00, 40),
        (0.10, 65),
        (0.25, 85),
        (0.50, 100),
    ]
    roic_curve: list[tuple[float, float]] = [
        (-0.10, 0),
        (0.00, 20),
        (0.10, 60),
        (0.20, 85),
        (0.35, 100),
    ]
    relative_multiple_curve: list[tuple[float, float]] = [
        (0.40, 100),
        (0.75, 80),
        (1.00, 55),
        (1.50, 20),
        (2.00, 0),
    ]
    fcf_yield_curve: list[tuple[float, float]] = [
        (-0.05, 0),
        (0.00, 25),
        (0.04, 60),
        (0.08, 85),
        (0.15, 100),
    ]
    debt_to_ebitda_curve: list[tuple[float, float]] = [
        (0.0, 100),
        (1.0, 90),
        (2.0, 70),
        (3.0, 45),
        (5.0, 10),
        (8.0, 0),
    ]
    medium_term_weakness_curve: list[tuple[float, float]] = [
        (-0.60, 0),
        (-0.25, 90),
        (-0.10, 100),
        (0.00, 30),
        (0.20, 0),
    ]
    volatility_curve: list[tuple[float, float]] = [
        (0.00, 25),
        (0.15, 75),
        (0.30, 100),
        (0.50, 50),
        (1.00, 0),
    ]
    relative_volume_curve: list[tuple[float, float]] = [
        (0.50, 0),
        (1.00, 40),
        (1.20, 70),
        (1.50, 100),
        (3.00, 100),
    ]

    @model_validator(mode="after")
    def validate_curve(self) -> ScoreConfig:
        curves = [value for name, value in self.__dict__.items() if name.endswith("_curve")]
        for curve in curves:
            x_values = [point[0] for point in curve]
            if len(curve) < 2 or curve != sorted(curve) or len(set(x_values)) != len(x_values):
                raise ValueError("Score curve x values must be ascending and contain 2+ points")
            if any(not 0 <= score <= 100 for _, score in curve):
                raise ValueError("Curve scores must be within 0..100")
        return self


class StrategyConfig(BaseModel):
    """Validated YAML strategy tree; later milestones own score details."""

    model_config = ConfigDict(extra="allow")
    universe: UniverseConfig = UniverseConfig()
    storage: StorageConfig = StorageConfig()
    sec: SecConfig = SecConfig()
    peers: PeerConfig
    scores: ScoreConfig
    data_quality: DataQualityConfig
    technical: TechnicalConfig = TechnicalConfig()
    screen_strategies: ScreenStrategiesConfig = ScreenStrategiesConfig()
    filters: FilterConfig
    portfolio: PortfolioConfig = PortfolioConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()
    intraday: IntradayConfig = IntradayConfig()
    position_management: PositionManagementConfig = PositionManagementConfig()


class Settings(BaseModel):
    strategy: StrategyConfig
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    sec_user_agent: str | None = None
    trading_mode: Literal["paper"] = "paper"
    enable_order_submission: bool = False

    @model_validator(mode="after")
    def reject_unsafe_order_mode(self) -> Settings:
        if self.enable_order_submission and self.trading_mode != "paper":
            raise ValueError("Order submission is only permitted in paper mode")
        return self

    def require_alpaca_credentials(self) -> tuple[str, str]:
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        return self.alpaca_api_key, self.alpaca_secret_key

    def require_sec_user_agent(self) -> str:
        if not self.sec_user_agent or "@" not in self.sec_user_agent:
            raise ValueError("SEC_USER_AGENT must identify a person/company and contact email")
        return self.sec_user_agent


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:  # JSON is valid YAML and keeps bootstrap errors actionable.
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Install PyYAML to read strategy configuration") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid strategy YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Strategy config must contain a mapping: {path}")
    return data


@lru_cache(maxsize=4)
def load_settings(strategy_path: str | Path | None = None) -> Settings:
    config_path = (
        DEFAULT_STRATEGY_PATH
        if strategy_path is None
        else Path(strategy_path).expanduser().resolve()
    )
    config_base = (
        config_path.parent.parent
        if config_path.parent.name.casefold() == "config"
        else config_path.parent
    )
    load_dotenv(config_base / ".env")
    strategy = StrategyConfig.model_validate(_read_yaml(config_path))
    storage = strategy.storage
    strategy = strategy.model_copy(
        update={
            "storage": storage.model_copy(
                update={
                    "database_path": _resolve_config_path(storage.database_path, config_base),
                    "reports_path": _resolve_config_path(storage.reports_path, config_base),
                }
            )
        }
    )
    return Settings(
        strategy=strategy,
        alpaca_api_key=os.getenv("ALPACA_API_KEY") or None,
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY") or None,
        sec_user_agent=os.getenv("SEC_USER_AGENT") or None,
        trading_mode=os.getenv("TRADING_MODE", "paper").lower(),
        enable_order_submission=os.getenv("ENABLE_ORDER_SUBMISSION", "false").lower()
        in {"1", "true", "yes"},
    )


def _resolve_config_path(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()
