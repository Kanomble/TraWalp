"""Market-data and asset models independent from Alpaca SDK objects."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DailyBar(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)


class TradableAsset(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    name: str
    exchange: str | None = None
    tradable: bool
    fractionable: bool
    shortable: bool = False


class MarketSnapshot(BaseModel):
    """Time-sensitive market observation kept separate from completed history."""

    model_config = ConfigDict(frozen=True)
    symbol: str
    observed_at: datetime
    latest_trade_price: Decimal | None = Field(default=None, gt=0)
    latest_trade_timestamp: datetime | None = None
    daily_bar: DailyBar | None = None
    previous_daily_bar: DailyBar | None = None
