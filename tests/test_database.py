from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.models.fundamentals import FundamentalFact
from trading_system.models.market_data import DailyBar, MarketSnapshot


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


def test_sync_state_and_current_market_snapshot_round_trip(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    database.set_sync_value(
        "dataset", "market_snapshot", {"status": "success", "symbols_updated": 2}
    )
    observed_at = datetime(2026, 8, 13, 7, tzinfo=UTC)
    database.upsert_market_snapshots(
        [
            MarketSnapshot(
                symbol="TEST",
                observed_at=observed_at,
                latest_trade_price=Decimal("12.34"),
                latest_trade_timestamp=observed_at,
            )
        ]
    )

    assert database.dataset_states()["market_snapshot"]["symbols_updated"] == 2
    snapshot = database.latest_market_snapshot("TEST")
    assert snapshot is not None
    assert snapshot.latest_trade_price == Decimal("12.34")
    assert snapshot.observed_at == observed_at


def test_storage_report_includes_all_fallback_diagnostics(tmp_path) -> None:
    database = Database(tmp_path / "storage.sqlite3")
    database.initialize()
    database.upsert_facts([fact(date(2024, 5, 5), "100", "original")])
    database.cache_sec_payload("0000001234", "companyfacts", {"large": "payload"})

    report = database.storage_report()

    assert report["file_bytes"] > 0
    assert report["page_size"] > 0
    assert report["page_count"] > 0
    assert report["row_counts"]["fundamental_facts"] == 1
    assert report["raw_sec_cache"][0]["endpoint"] == "companyfacts"
    assert report["raw_sec_cache"][0]["payload_bytes"] > 0
    assert report["object_sizes"] is None or report["object_sizes"]


def test_raw_sec_cleanup_is_guarded_dry_run_and_point_in_time_safe(tmp_path) -> None:
    database_path = tmp_path / "cleanup.sqlite3"
    database = Database(database_path)
    database.initialize()
    structured = fact(date(2024, 5, 5), "100", "original")
    database.upsert_facts([structured])
    database.cache_sec_payload("0000001234", "companyfacts", {"safe": "source"})
    database.cache_sec_payload("0000001234", "submissions", {"filings": {}})
    database.cache_sec_payload("0000009999", "companyfacts", {"only": "source copy"})
    before = database.facts_available_as_of("TEST", date(2024, 5, 10))
    before_file = (database_path.stat().st_size, database_path.stat().st_mtime_ns)

    dry_run = database.cleanup_raw_sec_cache(dry_run=True)

    assert dry_run["total_rows"] == 2
    assert dry_run["safe_rows"] == 1
    assert dry_run["blocked_rows"] == 1
    assert dry_run["deleted_rows"] == 0
    assert (database_path.stat().st_size, database_path.stat().st_mtime_ns) == before_file
    assert database.has_cached_sec_payload("0000001234", "companyfacts")

    result = database.cleanup_raw_sec_cache(dry_run=False)

    assert result["deleted_rows"] == 1
    assert not database.has_cached_sec_payload("0000001234", "companyfacts")
    assert database.has_cached_sec_payload("0000001234", "submissions")
    assert database.has_cached_sec_payload("0000009999", "companyfacts")
    assert database.facts_available_as_of("TEST", date(2024, 5, 10)) == before
    assert result["fundamental_fact_rows"] == 1
    assert result["daily_bar_rows"] == 0
