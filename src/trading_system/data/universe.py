"""Pure universe filters used after data acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_system.config import UniverseConfig

FINANCIAL_SIC_RANGES = ((6000, 6799),)
REIT_SIC_CODES = {6798}


@dataclass(frozen=True)
class UniverseSnapshot:
    symbol: str
    latest_price: Decimal | None
    average_price_20d: Decimal | None
    average_volume_20d: Decimal | None
    shares_outstanding: Decimal | None
    sic: str | None

    @property
    def average_dollar_volume_20d(self) -> Decimal | None:
        if self.average_price_20d is None or self.average_volume_20d is None:
            return None
        return self.average_price_20d * self.average_volume_20d

    @property
    def estimated_market_cap(self) -> Decimal | None:
        if self.latest_price is None or self.shares_outstanding is None:
            return None
        return self.latest_price * self.shares_outstanding


def is_financial_or_reit(sic: str | None, *, exclude_reits: bool = True) -> bool:
    if not sic or not sic.isdigit():
        return False
    number = int(sic)
    if number in REIT_SIC_CODES:
        return exclude_reits
    return any(lower <= number <= upper for lower, upper in FINANCIAL_SIC_RANGES)


def is_reit(sic: str | None) -> bool:
    return bool(sic and sic.isdigit() and int(sic) in REIT_SIC_CODES)


def passes_universe_filters(snapshot: UniverseSnapshot, config: UniverseConfig) -> bool:
    """Require rather than invent every value needed by the configured filters."""

    if snapshot.latest_price is None or snapshot.latest_price < Decimal(str(config.min_price)):
        return False
    market_cap = snapshot.estimated_market_cap
    if market_cap is None or market_cap < Decimal(str(config.min_market_cap)):
        return False
    dollar_volume = snapshot.average_dollar_volume_20d
    if dollar_volume is None or dollar_volume < Decimal(str(config.min_avg_dollar_volume_20d)):
        return False
    if config.exclude_reits and is_reit(snapshot.sic):
        return False
    return not (config.exclude_financials and is_financial_or_reit(snapshot.sic))
