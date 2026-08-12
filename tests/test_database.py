from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.models.fundamentals import FundamentalFact
from trading_system.models.market_data import DailyBar


def fact(filed: date, value: str, accession: str) -> FundamentalFact:
    return FundamentalFact(
        cik="0000001234",
        symbol="TEST",
        metric="revenue",
        tag="Revenues",
        value=Decimal(value),
        unit="USD",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
        filed=filed,
        fiscal_year=2024,
        fiscal_period="Q1",
        form="10-Q",
        accession_number=accession,
    )


def test_point_in_time_query_prevents_lookahead(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    database.upsert_facts([fact(date(2024, 5, 5), "100", "original")])
    assert database.facts_available_as_of("TEST", date(2024, 4, 20)) == []
    available = database.facts_available_as_of("TEST", date(2024, 5, 10))
    assert [item.value for item in available] == [Decimal("100")]


def test_amendment_is_not_visible_before_its_filing_date(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    database.upsert_facts(
        [fact(date(2024, 5, 5), "100", "original"), fact(date(2024, 6, 1), "110", "amended")]
    )
    may = database.facts_available_as_of("TEST", date(2024, 5, 31))
    june = database.facts_available_as_of("TEST", date(2024, 6, 2))
    assert [item.value for item in may] == [Decimal("100")]
    assert [item.value for item in june] == [Decimal("100"), Decimal("110")]


def test_bar_upsert_is_idempotent_and_applies_correction(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    timestamp = datetime(2024, 1, 2, tzinfo=UTC)
    common = dict(
        symbol="TEST",
        timestamp=timestamp,
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("9"),
        volume=100,
    )
    database.upsert_bars([DailyBar(**common, close=Decimal("11"))])
    database.upsert_bars([DailyBar(**common, close=Decimal("11.5"))])
    assert database.latest_bar_timestamp("TEST") == timestamp
    with database.connect() as connection:
        rows = connection.execute("SELECT close FROM daily_bars").fetchall()
    assert len(rows) == 1
    assert rows[0]["close"] == "11.5"


def test_bars_available_as_of_excludes_future_prices(tmp_path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    common = dict(
        symbol="TEST",
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("9"),
        volume=100,
    )
    database.upsert_bars(
        [
            DailyBar(
                **common,
                timestamp=datetime(2024, 1, 2, tzinfo=UTC),
                close=Decimal("11"),
            ),
            DailyBar(
                **common,
                timestamp=datetime(2024, 1, 3, tzinfo=UTC),
                close=Decimal("12"),
            ),
        ]
    )
    available = database.bars_available_as_of("TEST", date(2024, 1, 2))
    assert len(available) == 1
    assert available[0].close == Decimal("11")
