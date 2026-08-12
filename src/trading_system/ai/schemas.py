"""Stable, machine-readable schema for AI candidate exports."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class HoldingPeriod(BaseModel):
    model_config = ConfigDict(frozen=True)

    min: int = Field(5, ge=1)
    max: int = Field(10, ge=1)


class AIStrategy(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "short_term_long_swing"
    holding_period_days: HoldingPeriod = HoldingPeriod()
    description: str = "Candidate export for manual AI ranking"


class AIInstructions(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective: str = "Rank these candidates for a 1-2 week long-only swing-trading strategy."
    criteria: tuple[str, ...] = (
        "momentum",
        "trend quality",
        "fundamental quality",
        "volatility",
        "downside risk",
    )
    rules: tuple[str, ...] = (
        "Use only supplied data.",
        "Do not invent missing information.",
        "Rank candidates relative to each other.",
    )


class AIScoreSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    quality: float | None = None
    valuation: float | None = None
    opportunity: float | None = None
    timing: float | None = None


class AITechnicalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    return_5d: float | None = None
    return_20d: float | None = None
    return_63d: float | None = None
    return_126d: float | None = None
    rsi_14: float | None = None
    rsi_recovery: bool | None = None
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    above_sma_20: bool | None = None
    above_sma_50: bool | None = None
    above_sma_200: bool | None = None
    sma_20_rising: bool | None = None
    momentum_20_improving: bool | None = None
    volume_ratio_20d: float | None = None


class AIFundamentalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None
    operating_cash_flow_growth_yoy: float | None = None
    operating_cash_flow_positive: bool | None = None
    operating_margin: float | None = None
    effective_tax_rate: float | None = None
    roic: float | None = None
    debt_to_ebitda: float | None = None
    market_cap: float | None = None
    price_to_earnings: float | None = None
    enterprise_value: float | None = None
    ev_to_ebitda: float | None = None
    ev_to_ebit: float | None = None
    free_cash_flow_yield: float | None = None


class AIRiskMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    annualized_volatility_20d: float | None = None
    atr_14: float | None = None
    atr_pct: float | None = None
    drawdown_from_52w_high: float | None = None


class AICandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    company_name: str
    as_of: date
    market_session: date | None = None
    price: float | None = None
    quant_score: float
    rank: int = Field(ge=1)
    average_dollar_volume_20d: float | None = None
    scores: AIScoreSummary
    technical: AITechnicalMetrics
    fundamentals: AIFundamentalMetrics
    risk: AIRiskMetrics
    data_warnings: tuple[str, ...] = ()


class AICandidateExport(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    screen_as_of: date
    strategy: AIStrategy = AIStrategy()
    ai_instructions: AIInstructions = AIInstructions()
    candidate_count: int = Field(ge=1)
    candidates: tuple[AICandidate, ...]
