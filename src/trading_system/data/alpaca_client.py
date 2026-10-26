"""Small, testable adapter around alpaca-py market-data and asset clients."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
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
        symbol_mapping: Mapping[str, str] | None = None,
        minimum_retained_ratio: float = 0.5,
    ) -> None:
        # Explicit provider aliases only. Never guess punctuation conversions or
        # apply SEC ticker aliases to market-data requests. The default is identity.
        self.symbol_mapping = {
            key.strip().upper(): value.strip().upper()
            for key, value in (symbol_mapping or {}).items()
        }
        if len(self.symbol_mapping) != len(symbol_mapping or {}):
            raise ValueError("ALPACA_SYMBOL_MAPPING_CANONICAL_COLLISION")
        if any(not key or not value for key, value in self.symbol_mapping.items()) or len(
            set(self.symbol_mapping.values())
        ) != len(self.symbol_mapping):
            raise ValueError("ALPACA_SYMBOL_MAPPING_NOT_ONE_TO_ONE")
        self._canonical_symbols = {value: key for key, value in self.symbol_mapping.items()}
        self.symbol_mapping = MappingProxyType(self.symbol_mapping)
        self._canonical_symbols = MappingProxyType(self._canonical_symbols)
        self.historical = historical_client or StockHistoricalDataClient(api_key, secret_key)
        self.feed = feed
        self.adjustment = adjustment
        self.last_bar_diagnostics: dict = {"invalid_bars": 0}
        if not 0 < minimum_retained_ratio <= 1:
            raise ValueError("minimum_retained_ratio must be within (0, 1]")
        self.minimum_retained_ratio = minimum_retained_ratio
        self.last_asset_diagnostics: dict = {}
        # Explicit paper=True is defense in depth, though only read operations are exposed.
        self.trading = trading_client or TradingClient(
            api_key, secret_key, paper=True, raw_data=True
        )

    def list_tradable_us_equities(self) -> list[TradableAsset]:
        request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = self.trading.get_all_assets(request)
        if not isinstance(assets, list):
            raise ValueError("ALPACA_INVALID_ASSET_SNAPSHOT")
        result, unknown, parsed, invalid = [], set(), 0, 0
        unknown_count = 0
        known_exchanges = {item.value for item in AssetExchange}
        for asset in assets:
            try:
                get = (
                    asset.get
                    if isinstance(asset, Mapping)
                    else lambda key, row=asset: getattr(row, key)
                )
                symbol = get("symbol")
                status = getattr(get("status"), "value", get("status"))
                class_value = (
                    asset.get("class", asset.get("asset_class"))
                    if isinstance(asset, Mapping)
                    else get("asset_class")
                )
                asset_class = getattr(class_value, "value", class_value)
                exchange = getattr(get("exchange"), "value", get("exchange"))
                if (
                    not isinstance(symbol, str)
                    or not symbol
                    or any(char.isspace() for char in symbol)
                ):
                    raise ValueError("invalid symbol")
                if status not in {item.value for item in AssetStatus}:
                    raise ValueError("invalid status")
                if not isinstance(asset_class, str) or not asset_class.strip():
                    raise ValueError("missing asset_class")
                if not isinstance(exchange, str) or not exchange.strip():
                    raise ValueError("missing exchange")
                flags = {key: get(key) for key in ("tradable", "fractionable", "shortable")}
                if any(type(value) is not bool for value in flags.values()):
                    raise ValueError("asset flags must be booleans")
                normalized_asset = TradableAsset(
                    symbol=self._canonical_symbols.get(symbol, symbol),
                    name=get("name") or symbol,
                    exchange=exchange,
                    asset_class=asset_class,
                    **flags,
                )
                parsed += 1
                if exchange not in known_exchanges:
                    unknown.add(exchange)
                    unknown_count += 1
                if flags["tradable"] and status == AssetStatus.ACTIVE.value:
                    result.append(normalized_asset)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                invalid += 1
                LOGGER.warning("Skipped invalid Alpaca asset: %s", exc)
        result.sort(key=lambda asset: asset.symbol)
        self.last_asset_diagnostics = {
            "raw_assets_received": len(assets),
            "assets_parsed": parsed,
            "assets_skipped_invalid": invalid,
            "unknown_exchange_count": unknown_count,
            "unknown_exchange_values": sorted(unknown),
            "tradable_assets_final": len(result),
        }
        if not assets or parsed / len(assets) < self.minimum_retained_ratio:
            raise ValueError("ALPACA_ASSET_PARSE_RETENTION_FAILURE")
        if len({asset.symbol for asset in result}) != len(result):
            raise ValueError("ALPACA_SYMBOL_MAPPING_COLLISION")
        return result

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
        provider_symbols = [self.symbol_mapping.get(symbol, symbol) for symbol in normalized]
        if len(set(provider_symbols)) != len(provider_symbols):
            raise ValueError("ALPACA_SYMBOL_MAPPING_COLLISION")
        normalized_timeframe = BarTimeframe(timeframe)
        output: list[MarketDataBar] = []
        total_invalid = 0
        invalid_symbols: set[str] = set()
        for batch in _chunks(normalized, batch_size):
            provider_batch = [self.symbol_mapping.get(symbol, symbol) for symbol in batch]
            request = StockBarsRequest(
                symbol_or_symbols=provider_batch,
                timeframe=_alpaca_timeframe(normalized_timeframe),
                start=start,
                end=end,
                adjustment=self.adjustment,
                feed=self.feed,
            )
            response = self.historical.get_stock_bars(request)
            if not isinstance(response.data, dict) or set(response.data) - set(provider_batch):
                raise ValueError("ALPACA_UNEXPECTED_BAR_RESPONSE")
            normalized_vwap_count = 0
            normalized_vwap_symbols: set[str] = set()
            invalid_count = 0
            invalid_samples: list[str] = []
            for symbol, provider_symbol in zip(batch, provider_batch, strict=True):
                for bar in response.data.get(provider_symbol, []):
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
                        invalid_symbols.add(symbol)
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
        self.last_bar_diagnostics = {
            "invalid_bars": total_invalid,
            "invalid_symbols": sorted(invalid_symbols),
            "requested_feed": self.feed.value,
            "requested_adjustment": self.adjustment.value,
        }
        return sorted(output, key=lambda bar: (bar.symbol, bar.timestamp))

    def stock_snapshots(self, symbols: Iterable[str]) -> list[MarketSnapshot]:
        """Fetch one multi-symbol snapshot request and isolate malformed symbols."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return []
        provider_symbols = [self.symbol_mapping.get(symbol, symbol) for symbol in normalized]
        if len(set(provider_symbols)) != len(provider_symbols):
            raise ValueError("ALPACA_SYMBOL_MAPPING_COLLISION")
        response = self.historical.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=provider_symbols, feed=self.feed)
        )
        observed_at = datetime.now(UTC)
        output: list[MarketSnapshot] = []
        for symbol, snapshot in response.items():
            normalized_symbol = self._canonical_symbols.get(
                str(symbol).upper(), str(symbol).upper()
            )
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
