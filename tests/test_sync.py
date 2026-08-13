from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer
from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import DailyBar, MarketSnapshot, TradableAsset


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


def test_complete_sync_refreshes_all_sources_and_updates_bars_incrementally(tmp_path) -> None:
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
    assert sec.submission_calls == sec.fact_calls == 2
    assert alpaca.starts[1] < datetime(2024, 6, 3, tzinfo=UTC)
    assert database.cached_sec_payload("0000001234", "companyfacts", max_age=None) is None
    assert database.cached_sec_payload("0000001234", "submissions", max_age=None) is None


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


def _submission(accession: str) -> dict:
    return {
        "name": "Test Corp",
        "sic": "3571",
        "sicDescription": "Computers",
        "filings": {
            "recent": {"accessionNumber": [accession], "form": ["10-Q"]}
        },
    }


def _company_facts(accession: str, value: int = 100) -> dict:
    return {
        "cik": 1234,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-03-31",
                                "val": value,
                                "accn": accession,
                                "fy": 2024,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2024-05-05",
                            }
                        ]
                    }
                }
            }
        },
    }


class IncrementalSec:
    def __init__(self) -> None:
        self.accession = "0001"
        self.fact_calls = 0
        self.ticker_calls = 0
        self.fail_symbols: set[str] = set()

    def ticker_to_cik(self) -> dict[str, str]:
        self.ticker_calls += 1
        return {"BAD": "0000000002", "TEST": "0000001234"}

    def submissions(self, cik: str) -> dict:
        if cik in self.fail_symbols:
            raise RuntimeError("SEC unavailable")
        return _submission(self.accession)

    def company_facts(self, cik: str) -> dict:
        self.fact_calls += 1
        payload = _company_facts(self.accession, 110 if self.accession == "0002" else 100)
        payload["cik"] = int(cik)
        return payload


def _seed_assets(database: Database, *symbols: str) -> None:
    database.upsert_assets(
        [
            TradableAsset(
                symbol=symbol,
                name=f"{symbol} Corp",
                exchange="NASDAQ",
                tradable=True,
                fractionable=True,
            )
            for symbol in symbols
        ]
    )


def test_incremental_sec_skips_unchanged_companyfacts_and_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "incremental.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    first = sync.sync_sec_incremental()
    second = sync.sync_sec_incremental()

    assert first["companies_updated"] == first["facts_processed"] == 1
    assert second["companies_updated"] == second["facts_processed"] == 0
    assert sec.fact_calls == 1
    assert sec.ticker_calls == 1
    assert len(database.facts_available_as_of("TEST", date(2024, 5, 6))) == 1
    assert database.cached_sec_payload("0000001234", "companyfacts", max_age=None) is None
    assert database.cached_sec_payload("0000001234", "submissions", max_age=None) is None


def test_incremental_sec_refreshes_companyfacts_when_accession_changes(tmp_path) -> None:
    database = Database(tmp_path / "changed.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]
    sync.sync_sec_incremental()

    sec.accession = "0002"
    result = sync.sync_sec_incremental()

    assert result["companies_updated"] == 1
    assert result["facts_processed"] == 1
    assert sec.fact_calls == 2
    assert database.known_accession_numbers("0000001234") == {"0001", "0002"}


def test_first_incremental_run_uses_cached_submission_accessions_as_baseline(tmp_path) -> None:
    database = Database(tmp_path / "migration.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    database.upsert_company(
        CompanyIdentity(cik="0000001234", symbol="TEST", name="Test Corp", sic="3571")
    )
    database.cache_sec_payload("0000001234", "submissions", _submission("0001"))
    database.cache_sec_payload("0000001234", "companyfacts", _company_facts("0001"))
    database.upsert_facts(parse_company_facts(_company_facts("0001"), "TEST"))
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    result = sync.sync_sec_incremental()

    assert result["companies_updated"] == result["facts_processed"] == 0
    assert sec.fact_calls == 0
    assert database.sync_value("sec_accessions", "0000001234") == ["0001"]


def test_legacy_raw_companyfacts_without_structured_facts_is_reimported(tmp_path) -> None:
    database = Database(tmp_path / "raw-only.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    database.cache_sec_payload("0000001234", "submissions", _submission("0001"))
    database.cache_sec_payload("0000001234", "companyfacts", _company_facts("0001"))
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    result = sync.sync_sec_incremental()

    assert result["companies_updated"] == 1
    assert result["facts_processed"] == 1
    assert sec.fact_calls == 1
    assert database.has_fundamental_facts("0000001234")


def test_parse_failure_does_not_advance_accession_state_or_cache_payload(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "parse-failure.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    def fail_parse(*_args, **_kwargs):
        raise ValueError("invalid Company Facts")

    monkeypatch.setattr("trading_system.data.sync.parse_company_facts", fail_parse)
    result = sync.sync_sec_incremental()

    assert result["errors"] == 1
    assert database.sync_value("sec_accessions", "0000001234") is None
    assert not database.has_fundamental_facts("0000001234")
    assert not database.has_cached_sec_payload("0000001234", "companyfacts")


def test_structured_upsert_failure_does_not_advance_state_or_cache_payload(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "upsert-failure.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(database, "upsert_sec_company_update", fail_upsert)
    result = sync.sync_sec_incremental()

    assert result["errors"] == 1
    assert database.sync_value("sec_accessions", "0000001234") is None
    assert not database.has_fundamental_facts("0000001234")
    assert not database.has_cached_sec_payload("0000001234", "companyfacts")


def test_incremental_sec_failure_is_recorded_and_other_companies_continue(tmp_path) -> None:
    database = Database(tmp_path / "partial.sqlite3")
    database.initialize()
    _seed_assets(database, "BAD", "TEST")
    sec = IncrementalSec()
    sec.fail_symbols.add("0000000002")
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    result = sync.sync_sec_incremental()

    assert result["companies_checked"] == 2
    assert result["companies_updated"] == 1
    assert result["errors"] == 1
    assert database.dataset_states()["sec"]["status"] == "partial"


class SnapshotAlpaca:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.batches: list[list[str]] = []
        self.missing = missing or set()

    def stock_snapshots(self, symbols) -> list[MarketSnapshot]:
        batch = list(symbols)
        self.batches.append(batch)
        output = []
        for symbol in batch:
            if symbol in self.missing:
                continue
            previous = DailyBar(
                symbol=symbol,
                timestamp=datetime(2026, 8, 12, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=100,
            )
            current = previous.model_copy(
                update={"timestamp": datetime(2099, 1, 1, tzinfo=UTC), "close": Decimal("99")}
            )
            output.append(
                MarketSnapshot(
                    symbol=symbol,
                    observed_at=datetime(2026, 8, 13, 7, tzinfo=UTC),
                    latest_trade_price=Decimal("11.25"),
                    latest_trade_timestamp=datetime(2026, 8, 13, 7, tzinfo=UTC),
                    daily_bar=current,
                    previous_daily_bar=previous,
                )
            )
        return output


class FailingSnapshotAlpaca(SnapshotAlpaca):
    def stock_snapshots(self, symbols) -> list[MarketSnapshot]:
        batch = list(symbols)
        if batch == ["BAD"]:
            self.batches.append(batch)
            raise RuntimeError("snapshot unavailable")
        return super().stock_snapshots(batch)


def test_market_refresh_batches_symbols_maps_results_without_touching_history(tmp_path) -> None:
    database = Database(tmp_path / "snapshots.sqlite3")
    database.initialize()
    _seed_assets(database, "AAA", "BBB", "MISSING")
    for index, symbol in enumerate(("AAA", "BBB", "MISSING"), start=1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    alpaca = SnapshotAlpaca({"MISSING"})
    sync = DataSynchronizer(database, alpaca, None, market_data_batch_size=2)  # type: ignore[arg-type]
    result = sync.refresh_market()

    assert alpaca.batches == [["AAA", "BBB"], ["MISSING"]]
    assert result["symbols_updated"] == 2
    assert result["missing_symbols"] == 1
    assert database.latest_market_snapshot("AAA").latest_trade_price == Decimal("11.25")
    assert database.latest_bar_timestamp("AAA") is None
    assert database.dataset_states()["market_snapshot"]["status"] == "success"


def test_market_refresh_continues_after_failed_batch_and_does_not_mark_success(tmp_path) -> None:
    database = Database(tmp_path / "snapshot-partial.sqlite3")
    database.initialize()
    _seed_assets(database, "BAD", "GOOD")
    for index, symbol in enumerate(("BAD", "GOOD"), start=1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    alpaca = FailingSnapshotAlpaca()
    sync = DataSynchronizer(database, alpaca, None, market_data_batch_size=1)  # type: ignore[arg-type]

    result = sync.refresh_market()

    state = database.dataset_states()["market_snapshot"]
    assert result["errors"] == 1
    assert result["symbols_updated"] == 1
    assert database.latest_market_snapshot("GOOD") is not None
    assert state["status"] == "partial"
    assert state["last_success_at"] is None
