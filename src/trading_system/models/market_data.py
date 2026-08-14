"""Market-data and asset models independent from Alpaca SDK objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BarTimeframe(StrEnum):
    MINUTES_5 = "5m"
    MINUTES_15 = "15m"
    HOUR_1 = "1h"
    DAY_1 = "1d"

    @property
    def duration(self) -> timedelta:
        return {
            self.MINUTES_5: timedelta(minutes=5),
            self.MINUTES_15: timedelta(minutes=15),
            self.HOUR_1: timedelta(hours=1),
            self.DAY_1: timedelta(days=1),
        }[self]

    @property
    def intraday(self) -> bool:
        return self is not self.DAY_1


SUPPORTED_BAR_TIMEFRAMES = tuple(BarTimeframe)


class MarketDataBar(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    timeframe: BarTimeframe = BarTimeframe.DAY_1
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        return value.astimezone(UTC)

# Backward-compatible import name. Existing Daily code receives timeframe="1d" by default.
DailyBar = MarketDataBar


def validate_market_bar(bar: MarketDataBar) -> None:
    if bar.high < max(bar.open, bar.close, bar.low):
        raise ValueError("bar high must be >= open, close, and low")
    if bar.low > min(bar.open, bar.close, bar.high):
        raise ValueError("bar low must be <= open, close, and high")


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
