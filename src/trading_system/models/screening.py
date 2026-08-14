"""Persistable, explainable daily-screen models."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import DailyBar
from trading_system.models.scores import StockScores
from trading_system.models.signals import TechnicalSnapshot


class ScreenRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    name: str
    as_of: date
    sic: str | None = None
    peer_group: str | None = None
    rank: int | None = Field(default=None, ge=1)
    eligible: bool = False
    exclusion_reasons: tuple[str, ...] = ()
    data_warnings: tuple[str, ...] = ()
    average_dollar_volume_20d: float | None = None
    industry_medians: dict[str, float | None] = Field(default_factory=dict)
    fundamentals: FundamentalMetrics
    technical: TechnicalSnapshot
    scores: StockScores
    # Lightweight provenance used by the historical candidate audit.  These
    # values describe only information available at ``as_of`` and deliberately
    # avoid retaining complete bar/fact dictionaries in historical reports.
    market_history_count: int = Field(default=0, ge=0)
    pit_fact_count: int = Field(default=0, ge=0)
    estimated_market_cap: float | None = Field(default=None, ge=0)
    latest_pit_filing_date: date | None = None
    latest_pit_period_end: date | None = None


class ScreenReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    as_of: date
    requested_as_of: date | None = None
    effective_market_session: date | None = None
    generated_at: str
    analyzed_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    identity_conflicts_excluded: int = Field(default=0, ge=0)
    identity_conflict_sample: tuple[str, ...] = ()
    records: tuple[ScreenRecord, ...]


class PeerDebug(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    sic: str | None = None
    exact_peer_count: int = Field(ge=0)
    three_digit_peer_count: int = Field(ge=0)
    two_digit_peer_count: int = Field(ge=0)
    selected_group: str | None = None
    selected_peer_count: int = Field(ge=0)
    valid_pe_count: int = Field(ge=0)
    valid_ev_ebitda_count: int = Field(ge=0)
    valid_ev_ebit_count: int = Field(ge=0)
    median_pe: float | None = None
    median_ev_ebitda: float | None = None
    median_ev_ebit: float | None = None
    minimum_peer_count: int = Field(ge=2)


class MarketDebug(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    requested_as_of: date
    effective_market_session: date
    actual_latest_bar_session: date | None = None
    requested_alpaca_start: datetime
    requested_alpaca_end_exclusive: datetime
    feed: str
    adjustment: str
    bar_count: int = Field(ge=0)
    last_bars: tuple[DailyBar, ...]
    latest_completed_close: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    momentum5: float | None = None
    momentum20: float | None = None
    momentum63: float | None = None
    high_52w: float | None = None
    drawdown_52w: float | None = None
    atr14: float | None = None
    average_volume20: float | None = None
    relative_volume: float | None = None
