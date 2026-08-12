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
    filters: FilterConfig


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
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"Strategy config must contain a mapping: {path}")
    return data


@lru_cache(maxsize=4)
def load_settings(strategy_path: str | Path = "config/strategy.yaml") -> Settings:
    load_dotenv()
    strategy = StrategyConfig.model_validate(_read_yaml(Path(strategy_path)))
    return Settings(
        strategy=strategy,
        alpaca_api_key=os.getenv("ALPACA_API_KEY") or None,
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY") or None,
        sec_user_agent=os.getenv("SEC_USER_AGENT") or None,
        trading_mode=os.getenv("TRADING_MODE", "paper").lower(),
        enable_order_submission=os.getenv("ENABLE_ORDER_SUBMISSION", "false").lower()
        in {"1", "true", "yes"},
    )
