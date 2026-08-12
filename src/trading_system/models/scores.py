"""Explainable metric and score output models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FactorScore(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    raw_value: float | bool | None
    score: float | None = Field(default=None, ge=0, le=100)
    configured_weight: float = Field(ge=0, le=1)
    normalized_available_weight: float = Field(default=0, ge=0, le=1)
    effective_weight: float = Field(default=0, ge=0, le=1)
    explanation: str


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    score: float | None = Field(default=None, ge=0, le=100)
    factors: tuple[FactorScore, ...]
    available_factor_count: int = Field(ge=0)
    minimum_required_factor_count: int = Field(default=1, ge=1)
    reason_score_unavailable: str | None = None


class StockScores(BaseModel):
    model_config = ConfigDict(frozen=True)
    quality: ScoreBreakdown
    valuation: ScoreBreakdown
    opportunity: ScoreBreakdown
    timing: ScoreBreakdown
    total: float | None = Field(default=None, ge=0, le=100)
