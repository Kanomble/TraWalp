"""Small, testable adapter around alpaca-py market-data and asset clients."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from trading_system.models.market_data import (
    BarTimeframe,
    DailyBar,
    MarketDataBar,
    MarketSnapshot,
    TradableAsset,
    validate_market_bar,
)

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
        self.last_bar_diagnostics: dict[str, int] = {"invalid_bars": 0}
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
        """Backward-compatible adjusted Daily-bar request."""

        return self.bars(
            symbols,
            start,
            end,
            timeframe=BarTimeframe.DAY_1,
            batch_size=batch_size,
        )

    def bars(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        timeframe: BarTimeframe | str,
        batch_size: int = 200,
    ) -> list[MarketDataBar]:
        """Fetch provider-native bars; alpaca-py handles page tokens and retry codes."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        normalized_timeframe = BarTimeframe(timeframe)
        output: list[MarketDataBar] = []
        total_invalid = 0
        for batch in _chunks(normalized, batch_size):
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=_alpaca_timeframe(normalized_timeframe),
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
                        normalized_bar = MarketDataBar(
                            symbol=symbol,
                            timeframe=normalized_timeframe,
                            timestamp=bar.timestamp,
                            open=Decimal(str(bar.open)),
                            high=Decimal(str(bar.high)),
                            low=Decimal(str(bar.low)),
                            close=Decimal(str(bar.close)),
                            volume=volume,
                            trade_count=trade_count,
                            vwap=vwap,
                        )
                        validate_market_bar(normalized_bar)
                        output.append(normalized_bar)
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
            total_invalid += invalid_count
        self.last_bar_diagnostics = {"invalid_bars": total_invalid}
        return sorted(output, key=lambda bar: (bar.symbol, bar.timestamp))

    def stock_snapshots(self, symbols: Iterable[str]) -> list[MarketSnapshot]:
        """Fetch one multi-symbol snapshot request and isolate malformed symbols."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return []
        response = self.historical.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=normalized, feed=self.feed)
        )
        observed_at = datetime.now(UTC)
        output: list[MarketSnapshot] = []
        for symbol, snapshot in response.items():
            normalized_symbol = str(symbol).upper()
            try:
                latest_trade = getattr(snapshot, "latest_trade", None)
                price = (
                    Decimal(str(latest_trade.price))
                    if latest_trade is not None and latest_trade.price is not None
                    else None
                )
                output.append(
                    MarketSnapshot(
                        symbol=normalized_symbol,
                        observed_at=observed_at,
                        latest_trade_price=price,
                        latest_trade_timestamp=(
                            latest_trade.timestamp if latest_trade is not None else None
                        ),
                        daily_bar=_snapshot_bar(
                            normalized_symbol, getattr(snapshot, "daily_bar", None)
                        ),
                        previous_daily_bar=_snapshot_bar(
                            normalized_symbol, getattr(snapshot, "previous_daily_bar", None)
                        ),
                    )
                )
            except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
                LOGGER.warning(
                    "Skipped invalid Alpaca snapshot symbol=%s error=%s: %s",
                    normalized_symbol,
                    type(exc).__name__,
                    exc,
                )
        return sorted(output, key=lambda item: item.symbol)


def _snapshot_bar(symbol: str, bar: object | None) -> DailyBar | None:
    if bar is None:
        return None
    volume = int(bar.volume)  # type: ignore[attr-defined]
    raw_trade_count = getattr(bar, "trade_count", None)
    trade_count = int(raw_trade_count) if raw_trade_count is not None else None
    raw_vwap = getattr(bar, "vwap", None)
    vwap = Decimal(str(raw_vwap)) if raw_vwap is not None else None
    if vwap == 0 and volume == 0 and trade_count in (None, 0):
        vwap = None
    return DailyBar(
        symbol=symbol,
        timestamp=bar.timestamp,  # type: ignore[attr-defined]
        open=Decimal(str(bar.open)),  # type: ignore[attr-defined]
        high=Decimal(str(bar.high)),  # type: ignore[attr-defined]
        low=Decimal(str(bar.low)),  # type: ignore[attr-defined]
        close=Decimal(str(bar.close)),  # type: ignore[attr-defined]
        volume=volume,
        trade_count=trade_count,
        vwap=vwap,
    )


def _alpaca_timeframe(timeframe: BarTimeframe) -> TimeFrame:
    return {
        BarTimeframe.MINUTES_5: TimeFrame(5, TimeFrameUnit.Minute),
        BarTimeframe.MINUTES_15: TimeFrame(15, TimeFrameUnit.Minute),
        BarTimeframe.HOUR_1: TimeFrame.Hour,
        BarTimeframe.DAY_1: TimeFrame.Day,
    }[timeframe]
