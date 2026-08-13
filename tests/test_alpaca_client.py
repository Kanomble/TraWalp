from datetime import UTC, datetime
from types import SimpleNamespace

from alpaca.data.enums import Adjustment

from trading_system.data.alpaca_client import AlpacaDataClient


class HistoricalClient:
    def __init__(self, bars=None) -> None:
        self.request = None
        self.bars = bars

    def get_stock_bars(self, request):
        self.request = request
        bars = self.bars or [
            SimpleNamespace(
                timestamp=datetime(2024, 1, 2, tzinfo=UTC),
                open=10,
                high=12,
                low=9,
                close=11,
                volume=100,
                trade_count=5,
                vwap=10.5,
            )
        ]
        return SimpleNamespace(data={"TEST": bars})

    def get_stock_snapshot(self, request):
        self.request = request
        bar = SimpleNamespace(
            timestamp=datetime(2024, 1, 2, tzinfo=UTC),
            open=10,
            high=12,
            low=9,
            close=11,
            volume=100,
            trade_count=5,
            vwap=10.5,
        )
        return {
            "AAA": SimpleNamespace(
                latest_trade=SimpleNamespace(
                    price=11.25, timestamp=datetime(2024, 1, 3, tzinfo=UTC)
                ),
                daily_bar=bar,
                previous_daily_bar=None,
            ),
            # BBB is intentionally omitted to exercise partial provider responses.
        }


def test_daily_bars_request_split_adjusted_data() -> None:
    historical = HistoricalClient()
    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=historical,  # type: ignore[arg-type]
        trading_client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    bars = client.daily_bars(
        ["test"], datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC)
    )
    assert historical.request.adjustment == Adjustment.ALL
    assert bars[0].symbol == "TEST"
    assert str(bars[0].close) == "11"


def test_adjustment_policy_is_explicitly_configurable() -> None:
    historical = HistoricalClient()
    client = AlpacaDataClient(
        "key",
        "secret",
        adjustment=Adjustment.SPLIT,
        historical_client=historical,  # type: ignore[arg-type]
        trading_client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    client.daily_bars(["TEST"], datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert historical.request.adjustment == Adjustment.SPLIT


def test_zero_trade_placeholder_bar_normalizes_zero_vwap_to_missing() -> None:
    placeholder = SimpleNamespace(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=10,
        high=10,
        low=10,
        close=10,
        volume=0,
        trade_count=0,
        vwap=0.0,
    )
    historical = HistoricalClient([placeholder])
    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=historical,  # type: ignore[arg-type]
        trading_client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    bars = client.daily_bars(
        ["TEST"], datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC)
    )
    assert len(bars) == 1
    assert bars[0].volume == 0
    assert bars[0].vwap is None


def test_invalid_zero_vwap_with_trading_activity_is_skipped(caplog) -> None:
    invalid = SimpleNamespace(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=10,
        high=12,
        low=9,
        close=11,
        volume=100,
        trade_count=5,
        vwap=0.0,
    )
    valid = SimpleNamespace(
        timestamp=datetime(2024, 1, 3, tzinfo=UTC),
        open=11,
        high=13,
        low=10,
        close=12,
        volume=120,
        trade_count=6,
        vwap=11.5,
    )
    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=HistoricalClient([invalid, valid]),  # type: ignore[arg-type]
        trading_client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    bars = client.daily_bars(
        ["TEST"], datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)
    )
    assert [bar.timestamp for bar in bars] == [valid.timestamp]
    assert "Skipped invalid Alpaca bars=1" in caplog.text


def test_stock_snapshots_use_one_multi_symbol_request_and_map_available_symbols() -> None:
    historical = HistoricalClient()
    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=historical,  # type: ignore[arg-type]
        trading_client=SimpleNamespace(),  # type: ignore[arg-type]
    )

    snapshots = client.stock_snapshots(["bbb", "aaa"])

    assert historical.request.symbol_or_symbols == ["AAA", "BBB"]
    assert [snapshot.symbol for snapshot in snapshots] == ["AAA"]
    assert str(snapshots[0].latest_trade_price) == "11.25"
    assert snapshots[0].daily_bar is not None
    assert snapshots[0].daily_bar.symbol == "AAA"
