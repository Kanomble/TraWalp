"""Small, testable adapter around alpaca-py market-data and asset clients."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from trading_system.models.market_data import DailyBar, TradableAsset

LOGGER = logging.getLogger(__name__)


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


class AlpacaDataClient:
    """Read-only Alpaca adapter. It contains no order submission methods."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: DataFeed = DataFeed.IEX,
        adjustment: Adjustment = Adjustment.ALL,
        historical_client: StockHistoricalDataClient | None = None,
        trading_client: TradingClient | None = None,
    ) -> None:
        self.historical = historical_client or StockHistoricalDataClient(api_key, secret_key)
        self.feed = feed
        self.adjustment = adjustment
        # Explicit paper=True is defense in depth, though only read operations are exposed.
        self.trading = trading_client or TradingClient(api_key, secret_key, paper=True)

    def list_tradable_us_equities(self) -> list[TradableAsset]:
        request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = self.trading.get_all_assets(request)
        return sorted(
            (
                TradableAsset(
                    symbol=asset.symbol,
                    name=asset.name,
                    exchange=str(
                        asset.exchange.value if hasattr(asset.exchange, "value") else asset.exchange
                    ),
                    tradable=asset.tradable,
                    fractionable=asset.fractionable,
                    shortable=asset.shortable,
                )
                for asset in assets
                if asset.tradable and asset.status == AssetStatus.ACTIVE
            ),
            key=lambda asset: asset.symbol,
        )

    def daily_bars(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        batch_size: int = 200,
    ) -> list[DailyBar]:
        """Fetch adjusted daily bars and isolate malformed provider observations."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        output: list[DailyBar] = []
        for batch in _chunks(normalized, batch_size):
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                adjustment=self.adjustment,
                feed=self.feed,
            )
            response = self.historical.get_stock_bars(request)
            normalized_vwap_count = 0
            normalized_vwap_symbols: set[str] = set()
            invalid_count = 0
            invalid_samples: list[str] = []
            for symbol in batch:
                for bar in response.data.get(symbol, []):
                    try:
                        volume = int(bar.volume)
                        trade_count = int(bar.trade_count) if bar.trade_count is not None else None
                        vwap = Decimal(str(bar.vwap)) if bar.vwap is not None else None
                        if vwap == 0 and volume == 0 and trade_count in (None, 0):
                            # Alpaca may emit a flat placeholder bar for a session without
                            # trades. VWAP is mathematically unavailable, not zero.
                            vwap = None
                            normalized_vwap_count += 1
                            normalized_vwap_symbols.add(symbol)
                        output.append(
                            DailyBar(
                                symbol=symbol,
                                timestamp=bar.timestamp,
                                open=Decimal(str(bar.open)),
                                high=Decimal(str(bar.high)),
                                low=Decimal(str(bar.low)),
                                close=Decimal(str(bar.close)),
                                volume=volume,
                                trade_count=trade_count,
                                vwap=vwap,
                            )
                        )
                    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
                        invalid_count += 1
                        if len(invalid_samples) < 3:
                            invalid_samples.append(
                                f"symbol={symbol} timestamp={getattr(bar, 'timestamp', None)} "
                                f"error={type(exc).__name__}: {exc}"
                            )
            if normalized_vwap_count:
                LOGGER.info(
                    "Normalized unavailable zero VWAP bars=%d symbols=%d batch_symbols=%d",
                    normalized_vwap_count,
                    len(normalized_vwap_symbols),
                    len(batch),
                )
            if invalid_count:
                LOGGER.warning(
                    "Skipped invalid Alpaca bars=%d batch_symbols=%d samples=%s",
                    invalid_count,
                    len(batch),
                    invalid_samples,
                )
        return sorted(output, key=lambda bar: (bar.symbol, bar.timestamp))
