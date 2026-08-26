"""Technical snapshot consumed by explainable timing scores."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class TechnicalSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    market_session: date | None = None
    price: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    sma20_rising: bool | None = None
    rsi14: float | None = None
    rsi_recovery: bool | None = None
    momentum5: float | None = None
    momentum20: float | None = None
    momentum20_improving: bool | None = None
    momentum63: float | None = None
    momentum126: float | None = None
    volatility: float | None = None
    atr14: float | None = None
    relative_volume: float | None = None
    drawdown_52w: float | None = None
    drawdown_63d: float | None = None
    recovery_from_63d_low: float | None = None
    max_drawdown_126d: float | None = None
    sma200_distance: float | None = None
