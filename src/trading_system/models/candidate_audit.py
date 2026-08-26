"""Serializable historical candidate-funnel and data-coverage diagnostics."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trading_system.models.backtest import StrategyVariant


class FailureCategory(StrEnum):
    DATA_QUALITY = "data_quality"
    STRATEGY_REJECTION = "strategy_rejection"
    PORTFOLIO_BLOCKER = "portfolio_blocker"
    EXECUTION_BLOCKER = "execution_blocker"
    OTHER = "other"


class DistributionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int = Field(ge=0)
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p25: float | None = None
    p75: float | None = None
    p90: float | None = None
    maximum: float | None = None


class CandidateAuditSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    universe_total: int = Field(ge=0)
    identity_valid: int = Field(ge=0)
    identity_conflict: int = Field(ge=0)
    after_static_filters: int = Field(ge=0)
    market_history_available: int = Field(ge=0)
    valid_price: int = Field(ge=0)
    liquidity_pass: int = Field(ge=0)
    market_cap_pass: int = Field(ge=0)
    fundamental_data_available: int = Field(ge=0)
    positive_operating_cash_flow_pass: int = Field(ge=0)
    quality_score_available: int = Field(ge=0)
    quality_threshold_pass: int = Field(ge=0)
    valuation_score_available: int = Field(ge=0)
    valuation_threshold_pass: int = Field(ge=0)
    opportunity_score_available: int = Field(ge=0)
    opportunity_threshold_pass: int = Field(ge=0)
    timing_score_available: int = Field(ge=0)
    timing_threshold_pass: int = Field(ge=0)
    total_score_available: int = Field(ge=0)
    total_score_pass: int = Field(ge=0)
    price_above_sma20_pass: int = Field(ge=0)
    reached_recovery_gate: int = Field(ge=0)
    recovery_gate_pass: int = Field(ge=0)
    variant_gate_pass: int = Field(default=0, ge=0)
    eligible_candidates: int = Field(ge=0)
    ranked_candidates: int = Field(ge=0)
    portfolio_eligible: int = Field(default=0, ge=0)
    entry_orders_created: int = Field(default=0, ge=0)
    actual_entries: int = Field(default=0, ge=0)
    companies_requiring_fundamentals: int = Field(default=0, ge=0)
    companies_with_valid_pit_fundamentals: int = Field(default=0, ge=0)
    companies_with_incomplete_pit_fundamentals: int = Field(default=0, ge=0)
    companies_without_pit_fundamentals: int = Field(default=0, ge=0)
    pit_fundamental_coverage_pct: float | None = None
    data_quality_failures: int = Field(default=0, ge=0)
    strategy_rejections: int = Field(default=0, ge=0)
    other_failures: int = Field(default=0, ge=0)
    passed_via_rsi: int = Field(default=0, ge=0)
    passed_via_momentum: int = Field(default=0, ge=0)
    passed_via_relative_volume: int = Field(default=0, ge=0)
    failed_all_recovery_triggers: int = Field(default=0, ge=0)
    first_failure_reasons: dict[str, int] = Field(default_factory=dict)
    stage_incoming: dict[str, int] = Field(default_factory=dict)
    stage_rejected: dict[str, int] = Field(default_factory=dict)
    fundamental_metric_available: dict[str, int] = Field(default_factory=dict)
    fundamental_metric_missing: dict[str, int] = Field(default_factory=dict)
    technical_metric_available: dict[str, int] = Field(default_factory=dict)
    technical_metric_missing: dict[str, int] = Field(default_factory=dict)
    relative_volume_diagnostics: dict[str, int] = Field(default_factory=dict)
    portfolio_blockers: dict[str, int] = Field(default_factory=dict)
    execution_blockers: dict[str, int] = Field(default_factory=dict)


class CandidateAuditMonthly(BaseModel):
    model_config = ConfigDict(frozen=True)

    month: str
    screens: int = Field(ge=1)
    universe_observations: int = Field(ge=0)
    market_history_available: int = Field(ge=0)
    pit_fundamental_coverage_pct: float | None = None
    candidates_before_recovery: int = Field(ge=0)
    recovery_passes: int = Field(ge=0)
    eligible_candidates: int = Field(ge=0)
    entry_orders_created: int = Field(ge=0)
    actual_entries: int = Field(ge=0)
    primary_blocker: str | None = None
    failure_reasons: dict[str, int] = Field(default_factory=dict)
    failure_rates_at_stage: dict[str, float] = Field(default_factory=dict)
    portfolio_blockers: dict[str, int] = Field(default_factory=dict)
    score_distributions: dict[str, DistributionSummary] = Field(default_factory=dict)
    threshold_distance_distributions: dict[str, DistributionSummary] = Field(default_factory=dict)
    fundamental_metric_coverage_pct: dict[str, float] = Field(default_factory=dict)
    technical_metric_coverage_pct: dict[str, float] = Field(default_factory=dict)


class CandidateFailureSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    month: str
    reason: str
    category: FailureCategory
    rejected: int = Field(ge=0)
    incoming_at_stage: int = Field(ge=0)
    rejection_rate_at_stage: float | None = None


class CandidateNearMiss(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    symbol: str
    failed_at: str
    failure_detail: str | None = None
    blocking_reasons: tuple[str, ...] = ()
    failure_category: FailureCategory
    distance_to_threshold: float | None = None
    total_score: float | None = None
    quality_score: float | None = None
    valuation_score: float | None = None
    opportunity_score: float | None = None
    timing_score: float | None = None
    price_above_sma20: bool | None = None
    rsi_recovery: bool | None = None
    momentum5_above_zero: bool | None = None
    relative_volume: float | None = None
    relative_volume_above_threshold: bool | None = None
    variant_score: float | None = None
    technical_evidence: dict[str, float | bool | None] = Field(default_factory=dict)


class CandidateAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    symbol: str
    variant_score: float
    status: str
    order_created: bool = False
    entry_executed: bool = False
    execution_date: date | None = None
    blocker: str | None = None
    quality_score: float | None = None
    valuation_score: float | None = None
    opportunity_score: float | None = None
    timing_score: float | None = None
    price_above_sma20: bool | None = None
    rsi_recovery: bool | None = None
    momentum5_above_zero: bool | None = None
    relative_volume: float | None = None
    relative_volume_above_threshold: bool | None = None
    technical_evidence: dict[str, float | bool | None] = Field(default_factory=dict)


class PointInTimeSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    screen_date: date
    facts_available: int = Field(ge=0)
    latest_filing_date: date | None = None
    latest_period_end: date | None = None


class CandidateAuditResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    generated_at: str
    strategy_variant: StrategyVariant
    configuration: dict
    sessions: tuple[CandidateAuditSession, ...]
    monthly_summary: tuple[CandidateAuditMonthly, ...]
    failure_reasons: tuple[CandidateFailureSummary, ...]
    near_misses: tuple[CandidateNearMiss, ...]
    candidates: tuple[CandidateAuditEvent, ...]
    data_coverage: dict
    score_distributions: dict[str, dict[str, DistributionSummary]]
    recovery_gate_analysis: dict[str, int]
    portfolio_blockers: dict[str, int]
    execution_blockers: dict[str, int]
    period_comparison: tuple[dict, ...] = ()
    first_eligible_candidate_date: date | None = None
    first_entry_signal_date: date | None = None
    first_entry_date: date | None = None
    first_candidate_transition: dict = Field(default_factory=dict)
    candidate_symbols: tuple[str, ...] = ()
    entry_symbols: tuple[str, ...] = ()
    near_miss_symbols: tuple[str, ...] = ()
    pit_samples: tuple[PointInTimeSample, ...] = ()
    classification: str
    classification_evidence: dict = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    performance_diagnostics: dict = Field(default_factory=dict)
