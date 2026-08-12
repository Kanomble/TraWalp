from datetime import UTC, datetime
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer
from trading_system.models.market_data import DailyBar, TradableAsset


class Alpaca:
    def __init__(self) -> None:
        self.starts: list[datetime] = []

    def list_tradable_us_equities(self) -> list[TradableAsset]:
        return [
            TradableAsset(
                symbol="TEST",
                name="Test Corp",
                exchange="NASDAQ",
                tradable=True,
                fractionable=True,
            )
        ]

    def daily_bars(self, symbols, start, _end) -> list[DailyBar]:
        assert list(symbols) == ["TEST"]
        self.starts.append(start)
        return [
            DailyBar(
                symbol="TEST",
                timestamp=datetime(2024, 6, 3, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=100,
            )
        ]


class Sec:
    def __init__(self) -> None:
        self.submission_calls = 0
        self.fact_calls = 0

    def ticker_to_cik(self) -> dict[str, str]:
        return {"TEST": "0000001234"}

    def submissions(self, _cik: str) -> dict:
        self.submission_calls += 1
        return {"name": "Test Corp", "sic": "3571", "sicDescription": "Computers"}

    def company_facts(self, _cik: str) -> dict:
        self.fact_calls += 1
        return {"cik": 1234, "facts": {"us-gaap": {}}}


class BatchAlpaca:
    def list_tradable_us_equities(self) -> list[TradableAsset]:
        return [
            TradableAsset(
                symbol=symbol,
                name=f"{symbol} Corp",
                exchange="NASDAQ",
                tradable=True,
                fractionable=True,
            )
            for symbol in ("BAD", "GOOD")
        ]

    def daily_bars(self, symbols, _start, _end) -> list[DailyBar]:
        symbol = list(symbols)[0]
        if symbol == "BAD":
            raise RuntimeError("provider failure")
        return [
            DailyBar(
                symbol=symbol,
                timestamp=datetime(2024, 6, 3, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=100,
            )
        ]


class BatchSec(Sec):
    def ticker_to_cik(self) -> dict[str, str]:
        return {"BAD": "0000000001", "GOOD": "0000000002"}

    def submissions(self, cik: str) -> dict:
        self.submission_calls += 1
        return {"name": f"Company {cik}", "sic": "3571", "sicDescription": "Computers"}

    def company_facts(self, cik: str) -> dict:
        self.fact_calls += 1
        return {"cik": int(cik), "facts": {"us-gaap": {}}}


def test_sync_caches_sec_and_updates_market_data_incrementally(tmp_path) -> None:
    database = Database(tmp_path / "sync.sqlite3")
    database.initialize()
    alpaca = Alpaca()
    sec = Sec()
    sync = DataSynchronizer(database, alpaca, sec)  # type: ignore[arg-type]

    first = sync.sync(["TEST"])
    second = sync.sync(["TEST"])

    assert first["assets"] == 1
    assert first["bars"] == second["bars"] == 1
    assert first["errors"] == second["errors"] == 0
    assert sec.submission_calls == sec.fact_calls == 1
    assert alpaca.starts[1] < datetime(2024, 6, 3, tzinfo=UTC)


def test_sync_persists_successful_market_batch_after_another_batch_fails(tmp_path) -> None:
    database = Database(tmp_path / "batch-sync.sqlite3")
    database.initialize()
    sync = DataSynchronizer(
        database,
        BatchAlpaca(),  # type: ignore[arg-type]
        BatchSec(),  # type: ignore[arg-type]
        market_data_batch_size=1,
    )

    result = sync.sync()

    assert result["market_symbols"] == 2
    assert result["errors"] == 1
    assert result["bars"] == 1
    assert database.latest_bar_timestamp("BAD") is None
    assert database.latest_bar_timestamp("GOOD") == datetime(2024, 6, 3, tzinfo=UTC)
