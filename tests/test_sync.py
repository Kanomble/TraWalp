import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from trading_system.data.database import Database
from trading_system.data.sec_client import SecResourceNotFound
from trading_system.data.sync import (
    DataSynchronizer,
    classify_unmapped_asset,
    parse_daily_index_directory,
    parse_daily_master_index,
    parse_filing_index,
)
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


class AssetSnapshotAlpaca:
    def __init__(self, symbols: tuple[str, ...] = ("AAA", "CCC", "DDD")) -> None:
        self.symbols = symbols

    def list_tradable_us_equities(self) -> list[TradableAsset]:
        return [
            TradableAsset(
                symbol=symbol,
                name=f"{symbol} Current",
                exchange="NASDAQ",
                tradable=True,
                fractionable=True,
            )
            for symbol in self.symbols
        ]


class FailingAssetSnapshotAlpaca:
    def list_tradable_us_equities(self) -> list[TradableAsset]:
        raise RuntimeError("Alpaca assets unavailable")


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


def test_sync_assets_reconciles_full_snapshot_and_reports_metrics(tmp_path) -> None:
    database = Database(tmp_path / "sync-assets.sqlite3")
    database.initialize()
    _seed_assets(database, "AAA", "BBB", "CCC")
    sync = DataSynchronizer(
        database,
        AssetSnapshotAlpaca(),  # type: ignore[arg-type]
        None,
    )

    first = sync.sync_assets()
    second = sync.sync_assets()

    assert first["assets_received"] == first["assets_upserted"] == 3
    assert first["assets_deactivated"] == 1
    assert first["tradable_assets_after"] == 3
    assert first["records_updated"] == 4
    assert first["errors"] == 0
    assert second["assets_deactivated"] == 0
    assert second["records_updated"] == 3
    assert database.list_tradable_asset_symbols() == ["AAA", "CCC", "DDD"]
    state = database.dataset_states()["asset_universe"]
    assert state["assets_received"] == 3
    assert state["assets_deactivated"] == 0
    assert state["tradable_assets_after"] == 3


@pytest.mark.parametrize(
    "alpaca",
    [FailingAssetSnapshotAlpaca(), AssetSnapshotAlpaca(())],
)
def test_failed_or_empty_asset_snapshot_does_not_change_universe(tmp_path, alpaca) -> None:
    database = Database(tmp_path / "failed-assets.sqlite3")
    database.initialize()
    _seed_assets(database, "AAA", "BBB", "CCC")
    before = database.list_tradable_assets()
    sync = DataSynchronizer(database, alpaca, None)  # type: ignore[arg-type]

    with pytest.raises((RuntimeError, ValueError)):
        sync.sync_assets()

    assert database.list_tradable_assets() == before
    assert database.dataset_states()["asset_universe"]["status"] == "failed"


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
        "filings": {"recent": {"accessionNumber": [accession], "form": ["10-Q"]}},
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


def _master_index(*filings: tuple[str, str, str], received: str = "August 12, 2026") -> str:
    lines = [
        "Description:           Master Index of EDGAR Dissemination Feed",
        f"Last Data Received:    {received}",
        "",
        "CIK|Company Name|Form Type|Date Filed|Filename",
        "-" * 80,
    ]
    lines.extend(
        f"{int(cik)}|Test Corp|{form}|2026-08-12|edgar/data/{int(cik)}/{accession}.txt"
        for cik, form, accession in filings
    )
    return "\n".join(lines)


def _daily_directory(*index_dates: date, extra_names: tuple[str, ...] = ()) -> dict:
    return {
        "directory": {
            "item": [
                *({"name": f"master.{item:%Y%m%d}.idx"} for item in index_dates),
                *({"name": name} for name in extra_names),
            ]
        }
    }


class IncrementalSec:
    def __init__(self) -> None:
        self.accession = "0001"
        self.fact_calls = 0
        self.submission_calls = 0
        self.ticker_calls = 0
        self.fail_symbols: set[str] = set()
        self.unavailable_submissions: set[str] = set()
        self.unavailable_companyfacts: set[str] = set()
        self.directory_calls: list[tuple[int, int]] = []
        self.index_calls: list[tuple[int, int, str]] = []

    def ticker_to_cik(self) -> dict[str, str]:
        self.ticker_calls += 1
        return {"BAD": "0000000002", "TEST": "0000001234"}

    def submissions(self, cik: str) -> dict:
        self.submission_calls += 1
        if cik in self.unavailable_submissions:
            raise SecResourceNotFound("submissions", cik, f"submissions/{cik}")
        if cik in self.fail_symbols:
            raise RuntimeError("SEC unavailable")
        return _submission(self.accession)

    def company_facts(self, cik: str) -> dict:
        self.fact_calls += 1
        if cik in self.unavailable_companyfacts:
            raise SecResourceNotFound("companyfacts", cik, f"companyfacts/{cik}")
        payload = _company_facts(self.accession, 110 if self.accession == "0002" else 100)
        payload["cik"] = int(cik)
        return payload

    def daily_master_index_directory(self, year: int, quarter: int) -> dict:
        self.directory_calls.append((year, quarter))
        available = (datetime.now(UTC).date(), date(2026, 8, 12))
        return _daily_directory(
            *(
                item
                for item in available
                if item.year == year and (item.month - 1) // 3 + 1 == quarter
            )
        )

    def daily_master_index(self, year: int, quarter: int, filing_date: str) -> str:
        self.index_calls.append((year, quarter, filing_date))
        return _master_index(
            ("0000000002", "10-Q", self.accession),
            ("0000001234", "10-Q", self.accession),
        )


class IdentityConflictSec(IncrementalSec):
    proposed_cik = "0001826011"
    preceding_cik = "0001000000"
    following_cik = "0003000000"

    def __init__(self) -> None:
        super().__init__()
        self.corrected = False

    def ticker_to_cik(self) -> dict[str, str]:
        self.ticker_calls += 1
        return {
            "PARA": "0000813828" if self.corrected else self.proposed_cik,
            **({"BNZI": self.proposed_cik} if self.corrected else {}),
            "PSKY": "0002041610",
            "GOODA": self.preceding_cik,
            "GOODZ": self.following_cik,
        }

    def daily_master_index(self, year: int, quarter: int, filing_date: str) -> str:
        self.index_calls.append((year, quarter, filing_date))
        return _master_index(
            (self.proposed_cik, "10-Q", self.accession),
            (self.preceding_cik, "10-Q", self.accession),
            (self.following_cik, "10-Q", self.accession),
        )


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


def test_ticker_reuse_identity_conflict_is_quarantined_without_losing_other_work(
    tmp_path, caplog
) -> None:
    database = Database(tmp_path / "identity-conflict.sqlite3")
    database.initialize()
    _seed_assets(database, "PARA", "PSKY", "GOODA", "GOODZ", "BNZI")
    database.upsert_company(
        CompanyIdentity(cik="0000813828", symbol="PARA", name="Paramount Global")
    )
    database.upsert_company(
        CompanyIdentity(cik="0002041610", symbol="PSKY", name="Paramount Skydance Corp")
    )
    for cik in ("0000813828", "0002041610"):
        database.set_sync_value("sec_accessions", cik, ["old-accession"])
    old_payload = _company_facts("old-accession", 90)
    old_payload["cik"] = 813828
    database.upsert_facts(parse_company_facts(old_payload, "PARA"))
    database.upsert_bars(
        [
            DailyBar(
                symbol="PARA",
                timestamp=datetime(2026, 8, 12, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=100,
            )
        ]
    )
    before_facts = database.facts_available_as_of("PARA", date(2026, 8, 13))
    before_bar = database.latest_bar_timestamp("PARA")
    sec = IdentityConflictSec()

    with caplog.at_level(logging.WARNING):
        result = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["identity_conflicts"] == 1
    assert result["identity_conflict_sample"] == ["PARA"]
    assert result["database_failures"] == result["errors"] == 0
    assert database.dataset_states()["sec"]["status"] == "partial"
    conflict_state = database.unresolved_sec_identity_conflicts()["PARA"]
    assert conflict_state["existing_cik"] == "0000813828"
    assert conflict_state["proposed_cik"] == sec.proposed_cik
    assert conflict_state["source"] == "exact_sec_ticker"
    assert conflict_state["status"] == "unresolved"
    assert conflict_state["detected_at"] == conflict_state["last_seen_at"]
    assert database.company_symbol_to_cik()["PARA"] == "0000813828"
    assert database.company_symbol_to_cik()["PSKY"] == "0002041610"
    assert database.company_symbol_to_cik()["GOODA"] == sec.preceding_cik
    assert database.company_symbol_to_cik()["GOODZ"] == sec.following_cik
    assert database.facts_available_as_of("PARA", date(2026, 8, 13)) == before_facts
    assert database.latest_bar_timestamp("PARA") == before_bar
    assert database.sync_value("sec_accessions", sec.proposed_cik) is None
    assert database.sync_value("sec_accessions", sec.preceding_cik) == [sec.accession]
    assert database.sync_value("sec_accessions", sec.following_cik) == [sec.accession]
    assert sec.submission_calls == sec.fact_calls == 2
    conflicts = [record for record in caplog.records if "identity conflict" in record.message]
    assert len(conflicts) == 1
    assert conflicts[0].exc_info is None

    sec.corrected = True
    corrected = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert corrected["identity_conflicts"] == corrected["errors"] == 0
    assert database.unresolved_sec_identity_conflicts() == {}
    assert database.company_symbol_to_cik()["BNZI"] == sec.proposed_cik
    assert database.sync_value("sec_accessions", sec.proposed_cik) == [sec.accession]
    assert sec.submission_calls == sec.fact_calls == 3


def test_stale_alias_cannot_rename_an_existing_cik(tmp_path) -> None:
    database = Database(tmp_path / "stale-alias.sqlite3")
    database.initialize()
    _seed_assets(database, "NEW.A")
    database.upsert_company(CompanyIdentity(cik="0000001234", symbol="OLD", name="Existing Issuer"))
    database.set_sync_value("sec_accessions", "0000001234", ["old-accession"])
    sec = IncrementalSec()
    sec.ticker_to_cik = lambda: {"NEW-A": "0000001234"}  # type: ignore[method-assign]
    sec.daily_master_index = lambda *_args, **_kwargs: _master_index(  # type: ignore[method-assign]
        ("0000001234", "10-Q", "new-accession")
    )

    result = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["sec_ticker_alias_symbols"] == 1
    assert result["identity_conflicts"] == 1
    assert result["database_failures"] == result["errors"] == 0
    assert database.company_symbol_to_cik() == {"OLD": "0000001234"}
    assert database.sync_value("sec_accessions", "0000001234") == ["old-accession"]
    assert sec.submission_calls == sec.fact_calls == 0


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
    assert sec.submission_calls == 1
    assert sec.ticker_calls == 2
    assert len(sec.index_calls) == 2
    assert second["submissions_requests"] == 0
    assert second["companyfacts_requests"] == 0
    assert second["sec_requests_total"] == 3
    assert second["daily_index_directory_requests"] == 1
    assert second["daily_master_index_requests"] == 1
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
    assert result["parse_failures"] == 1
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
    assert result["database_failures"] == 1
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
    assert result["request_failures"] == 1
    assert database.dataset_states()["sec"]["status"] == "partial"
    assert database.sync_value("sec_change_detection", "daily_master_index") is None


def test_companyfacts_not_found_is_expected_and_negative_cached(tmp_path, caplog) -> None:
    database = Database(tmp_path / "unavailable.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.unavailable_companyfacts.add("0000001234")
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    sync = DataSynchronizer(database, None, sec, clock=lambda: now)  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO):
        first = sync.sync_sec_incremental()
        second = sync.sync_sec_incremental()

    assert first["companyfacts_unavailable"] == 1
    assert first["errors"] == first["request_failures"] == 0
    assert second["companyfacts_unavailable"] == 0
    assert second["negative_cache_hits"] == 1
    assert second["submissions_requests"] == second["companyfacts_requests"] == 0
    assert sec.submission_calls == sec.fact_calls == 1
    status = database.sync_value("sec_companyfacts_status", "0000001234")
    assert status == {
        "status": "unavailable",
        "last_checked_at": now.isoformat(),
        "last_submission_accession": "0001",
        "last_http_status": 404,
    }
    unavailable_logs = [
        record for record in caplog.records if "companyfacts unavailable" in record.message
    ]
    assert len(unavailable_logs) == 1
    assert unavailable_logs[0].levelno == logging.INFO
    assert unavailable_logs[0].exc_info is None
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_new_accession_and_full_sync_override_companyfacts_negative_cache(tmp_path) -> None:
    database = Database(tmp_path / "negative-invalidation.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.unavailable_companyfacts.add("0000001234")
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    sync = DataSynchronizer(database, None, sec, clock=lambda: now)  # type: ignore[arg-type]
    sync.sync_sec_incremental()

    sec.accession = "0002"
    changed = sync.sync_sec_incremental()
    assert changed["companyfacts_unavailable"] == 1
    assert sec.fact_calls == 2

    sec.unavailable_companyfacts.clear()
    full = sync.sync_sec_full()
    assert full["companies_updated"] == 1
    assert sec.fact_calls == 3
    assert database.sync_value("sec_companyfacts_status", "0000001234") is None


def test_expired_companyfacts_negative_cache_is_rechecked(tmp_path) -> None:
    database = Database(tmp_path / "negative-expired.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.unavailable_companyfacts.add("0000001234")
    current = [datetime(2026, 8, 13, 12, tzinfo=UTC)]
    sync = DataSynchronizer(
        database,
        None,
        sec,
        clock=lambda: current[0],
        companyfacts_unavailable_ttl=timedelta(days=7),
    )  # type: ignore[arg-type]
    sync.sync_sec_incremental()

    current[0] += timedelta(days=8)
    result = sync.sync_sec_incremental()

    assert result["companyfacts_unavailable"] == 1
    assert sec.fact_calls == 2


def test_submissions_not_found_is_expected_and_cached(tmp_path) -> None:
    database = Database(tmp_path / "submissions-unavailable.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.unavailable_submissions.add("0000001234")
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    sync = DataSynchronizer(database, None, sec, clock=lambda: now)  # type: ignore[arg-type]

    first = sync.sync_sec_incremental()
    second = sync.sync_sec_incremental()

    assert first["submissions_unavailable"] == 1
    assert first["errors"] == 0
    assert second["negative_cache_hits"] == 1
    assert second["submissions_requests"] == 0
    assert database.sync_value("sec_submissions_status", "0000001234")["last_http_status"] == 404


def test_xbrl_index_parser_keeps_parser_supported_forms_and_rejects_malformed() -> None:
    parsed = parse_filing_index(
        _master_index(
            ("1234", "10-Q", "q-accession"),
            ("1234", "8-K", "k-accession"),
            ("1234", "4", "ownership-accession"),
        )
    )

    assert parsed.last_data_received == date(2026, 8, 12)
    assert parsed.accessions_by_cik == {"0000001234": {"q-accession"}}
    with pytest.raises(ValueError, match="required headers"):
        parse_filing_index("not an EDGAR index")


def test_daily_directory_parser_accepts_only_valid_master_files_in_requested_quarter() -> None:
    payload = _daily_directory(
        date(2026, 7, 2),
        date(2026, 7, 1),
        date(2026, 7, 1),
        extra_names=(
            "company.20260701.idx",
            "form.20260701.idx",
            "crawler.20260701.idx",
            "sitemap.20260701.xml",
            "master.20260230.idx",
            "master.20260401.idx.gz",
            "master.20261001.idx",
            "master.20250701.idx",
        ),
    )
    payload["directory"]["item"].append(
        {"name": "master.20260703.idx", "type": "dir"}
    )
    parsed = parse_daily_index_directory(
        payload, 2026, 3
    )

    assert [item.filename for item in parsed] == [
        "master.20260701.idx",
        "master.20260702.idx",
    ]
    assert [item.index_date for item in parsed] == [date(2026, 7, 1), date(2026, 7, 2)]


def test_daily_directory_parser_rejects_unsafe_metadata_shape() -> None:
    with pytest.raises(ValueError, match="item list"):
        parse_daily_index_directory({"directory": {}}, 2026, 3)
    with pytest.raises(ValueError, match="malformed item"):
        parse_daily_index_directory({"directory": {"item": [None]}}, 2026, 3)


def test_daily_master_parser_extracts_relevant_rows_and_canonical_accessions() -> None:
    payload = _master_index(
        ("1234", "10-Q", "0000001234-26-000001"),
        ("1234", "8-K", "0000001234-26-000002"),
        ("42", "10-K/A", "0000000042-26-000003"),
        ("1234", "10-Q", "0000001234-26-000001"),
    )

    parsed = parse_daily_master_index(payload, date(2026, 8, 12))

    assert parsed.index_date == date(2026, 8, 12)
    assert parsed.accessions_by_cik == {
        "0000000042": {"0000000042-26-000003"},
        "0000001234": {"0000001234-26-000001"},
    }
    assert {(item.cik, item.form, item.filed, item.filename) for item in parsed.entries} == {
        (
            "0000000042",
            "10-K/A",
            date(2026, 8, 12),
            "edgar/data/42/0000000042-26-000003.txt",
        ),
        (
            "0000001234",
            "10-Q",
            date(2026, 8, 12),
            "edgar/data/1234/0000001234-26-000001.txt",
        ),
    }


def test_daily_master_parser_accepts_semantically_equivalent_sec_header() -> None:
    payload = "\n".join(
        [
            "Description: Daily Master Index",
            "\ufeff CIK | Company Name | Form Type | Date Filed | File Name ",
            "-" * 80,
            "1234|Test Corp|10-Q|2026-08-12|"
            "edgar/data/1234/0000001234-26-000001.txt",
        ]
    )

    parsed = parse_daily_master_index(payload, date(2026, 8, 12))

    assert parsed.accessions_by_cik == {
        "0000001234": {"0000001234-26-000001"}
    }


def test_daily_master_parser_identifies_html_response_explicitly() -> None:
    with pytest.raises(ValueError, match="2026-08-12 returned HTML"):
        parse_daily_master_index(
            "<!DOCTYPE html><html><title>SEC response</title></html>",
            date(2026, 8, 12),
        )


@pytest.mark.parametrize(
    "row",
    [
        "1234|Missing fields|10-Q|2026-08-12",
        "not-a-cik|Test Corp|10-Q|2026-08-12|edgar/data/1234/a.txt",
        "1234|Test Corp|10-Q|not-a-date|edgar/data/1234/a.txt",
        "1234|Test Corp|10-Q|2026-08-12|edgar/data/9999/a.txt",
    ],
)
def test_daily_master_parser_fails_closed_for_malformed_relevant_rows(row: str) -> None:
    payload = "\n".join(
        ["CIK|Company Name|Form Type|Date Filed|Filename", "-" * 80, row]
    )
    with pytest.raises(ValueError, match="Malformed SEC daily master row"):
        parse_daily_master_index(payload, date(2026, 8, 12))


def test_malformed_change_source_fails_without_advancing_cursor_or_fetching_companies(
    tmp_path,
) -> None:
    database = Database(tmp_path / "malformed-index.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.daily_master_index = lambda *_args, **_kwargs: "malformed"  # type: ignore[method-assign]
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="missing its delimited header"):
        sync.sync_sec_incremental()

    assert sec.submission_calls == sec.fact_calls == 0
    assert database.sync_value("sec_change_detection", "daily_master_index") is None


def test_cross_quarter_cursor_discovers_and_fetches_daily_master_indexes(tmp_path) -> None:
    database = Database(tmp_path / "catchup.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    database.set_sync_value("sec_accessions", "0000001234", ["old-accession"])
    database.set_sync_value(
        "sec_change_detection", "xbrl_index", {"last_data_received": "2026-03-30"}
    )
    sec = IncrementalSec()

    def directory(year: int, quarter: int) -> dict:
        sec.directory_calls.append((year, quarter))
        return _daily_directory(
            date(2026, 3, 31) if quarter == 1 else date(2026, 4, 1)
        )

    def master_index(year: int, quarter: int, filing_date: str) -> str:
        sec.index_calls.append((year, quarter, filing_date))
        if filing_date == "20260401":
            return _master_index(("0000001234", "10-Q", "q2-accession"))
        return _master_index(("0000001234", "10-K", "q1-accession"))

    sec.daily_master_index_directory = directory  # type: ignore[method-assign]
    sec.daily_master_index = master_index  # type: ignore[method-assign]
    sec.accession = "q2-accession"
    sync = DataSynchronizer(
        database,
        None,
        sec,
        clock=lambda: datetime(2026, 4, 2, 12, tzinfo=UTC),
    )  # type: ignore[arg-type]

    result = sync.sync_sec_incremental()

    assert sec.directory_calls == [(2026, 1), (2026, 2)]
    assert sec.index_calls == [(2026, 1, "20260331"), (2026, 2, "20260401")]
    assert result["change_detection_requests"] == 4
    assert result["legacy_cursor_bootstrap"] is True
    assert result["companies_checked"] == 1
    assert set(database.sync_value("sec_accessions", "0000001234")) >= {
        "old-accession",
        "q1-accession",
        "q2-accession",
    }
    assert database.sync_value("sec_change_detection", "daily_master_index") == {
        "last_processed_date": "2026-04-01"
    }
    assert database.sync_value("sec_change_detection", "xbrl_index") == {
        "last_data_received": "2026-03-30"
    }


@pytest.mark.parametrize(
    ("symbol", "name", "category"),
    [
        ("SPY", "SPDR S&P 500 ETF Trust", "etf_or_fund"),
        ("ACME.WS", "Acme Redeemable Warrants", "warrant"),
        ("ACME.U", "Acme Units", "unit"),
        ("ACME.RT", "Acme Rights", "rights"),
        ("ACME.PRA", "Acme Series A Preferred Stock", "preferred"),
        ("XYZY", "Foreign Issuer Sponsored ADR", "depositary_or_foreign"),
        ("ACME", "Acme Common Stock", "unclassified"),
    ],
)
def test_unmapped_asset_classification(symbol, name, category) -> None:
    asset = TradableAsset(
        symbol=symbol,
        name=name,
        exchange="NYSE",
        tradable=True,
        fractionable=False,
    )
    assert classify_unmapped_asset(asset) == category


def test_sync_reports_unmapped_categories_and_dot_hyphen_ticker_alias(tmp_path) -> None:
    database = Database(tmp_path / "universe-diagnostics.sqlite3")
    database.initialize()
    database.upsert_assets(
        [
            TradableAsset(
                symbol=symbol,
                name=name,
                exchange="NYSE",
                tradable=True,
                fractionable=False,
            )
            for symbol, name in (
                ("TEST", "Test Common Stock"),
                ("BRK.B", "Berkshire Class B"),
                ("FUND", "Example Index ETF"),
                ("ACME.WS", "Acme Redeemable Warrant"),
                ("MYST", "Mystery Security"),
            )
        ]
    )
    sec = IncrementalSec()
    sec.ticker_to_cik = lambda: {  # type: ignore[method-assign]
        "TEST": "0000001234",
        "BRK-B": "0000009999",
    }
    sec.daily_master_index = lambda *_args, **_kwargs: _master_index(  # type: ignore[method-assign]
        ("0000001234", "10-Q", "0001"),
        ("0000009999", "10-Q", "0001"),
    )

    result = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["universe_symbols"] == 5
    assert result["sec_mapped_symbols"] == 2
    assert result["sec_mapped_ciks"] == 2
    assert result["sec_ticker_alias_symbols"] == 1
    assert result["sec_unmapped_symbols"] == result["missing_cik_mappings"] == 3
    assert result["unmapped_etf_or_fund"] == 1
    assert result["unmapped_warrant"] == 1
    assert result["unmapped_otc_exchange"] == 0
    assert result["unmapped_unclassified"] == 1


def test_full_sync_deduplicates_multiple_symbols_for_one_cik(tmp_path) -> None:
    database = Database(tmp_path / "cik-deduplication.sqlite3")
    database.initialize()
    _seed_assets(database, "CLASSA", "CLASSB")
    sec = IncrementalSec()
    sec.ticker_to_cik = lambda: {  # type: ignore[method-assign]
        "CLASSA": "0000001234",
        "CLASSB": "0000001234",
    }

    result = DataSynchronizer(database, None, sec).sync_sec_full()  # type: ignore[arg-type]

    assert result["sec_mapped_symbols"] == 2
    assert result["sec_mapped_ciks"] == result["companies_checked"] == 1
    assert sec.submission_calls == sec.fact_calls == 1


def test_timeout_is_classified_as_real_request_failure(tmp_path) -> None:
    database = Database(tmp_path / "timeout.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()

    def timeout(_cik: str) -> dict:
        sec.submission_calls += 1
        raise requests.Timeout("timed out")

    sec.submissions = timeout  # type: ignore[method-assign]
    result = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["errors"] == 1
    assert result["request_failures"] == 1
    assert result["timeout_failures"] == 1
    assert result["companyfacts_unavailable"] == 0


@pytest.mark.parametrize(
    ("failure", "counter"),
    [
        (requests.HTTPError("429", response=requests.Response()), "rate_limit_failures"),
        (requests.HTTPError("503", response=requests.Response()), "server_failures"),
        (requests.ConnectionError("disconnected"), "connection_failures"),
        (ValueError("invalid JSON"), "json_failures"),
    ],
)
def test_real_sec_request_failures_have_structured_counters(
    tmp_path, failure: Exception, counter: str
) -> None:
    if isinstance(failure, requests.HTTPError):
        failure.response.status_code = int(str(failure))
    database = Database(tmp_path / f"{counter}.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.submissions = lambda _cik: (_ for _ in ()).throw(failure)  # type: ignore[method-assign]

    result = DataSynchronizer(database, None, sec).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["errors"] == result["request_failures"] == 1
    assert result[counter] == 1


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


class OperationalAlpaca(SnapshotAlpaca):
    def __init__(self) -> None:
        super().__init__()
        self.bar_batches: list[list[str]] = []

    def daily_bars(self, symbols, _start, _end) -> list[DailyBar]:
        batch = list(symbols)
        self.bar_batches.append(batch)
        return [
            DailyBar(
                symbol=symbol,
                timestamp=datetime(2026, 8, 12, tzinfo=UTC),
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=Decimal("11"),
                volume=100,
            )
            for symbol in batch
        ]


def test_reconciled_assets_drive_sec_market_and_bar_universes(tmp_path) -> None:
    database = Database(tmp_path / "reconciled-downstream.sqlite3")
    database.initialize()
    _seed_assets(database, "AAA", "BBB")
    for index, symbol in enumerate(("AAA", "BBB"), start=1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    historical_bbb = DailyBar(
        symbol="BBB",
        timestamp=datetime(2026, 8, 11, tzinfo=UTC),
        open=Decimal("9"),
        high=Decimal("11"),
        low=Decimal("8"),
        close=Decimal("10"),
        volume=90,
    )
    database.upsert_bars([historical_bbb])
    database.reconcile_assets(
        [
            TradableAsset(
                symbol="AAA",
                name="AAA Current",
                exchange="NASDAQ",
                tradable=True,
                fractionable=True,
            )
        ]
    )

    sec = IncrementalSec()
    sec.ticker_to_cik = lambda: {  # type: ignore[method-assign]
        "AAA": "0000000001",
        "BBB": "0000000002",
    }
    sec_result = DataSynchronizer(database, None, sec).sync_sec_full()  # type: ignore[arg-type]
    alpaca = OperationalAlpaca()
    operational = DataSynchronizer(database, alpaca, None)  # type: ignore[arg-type]
    market_result = operational.refresh_market()
    bar_result = operational.sync_historical_bars()

    assert sec_result["universe_symbols"] == sec_result["companies_checked"] == 1
    assert sec.submission_calls == sec.fact_calls == 1
    assert market_result["symbols_requested"] == 1
    assert bar_result["symbols_checked"] == 1
    assert alpaca.batches == alpaca.bar_batches == [["AAA"]]
    assert database.bars_available_as_of("BBB", date(2026, 8, 13)) == [historical_bbb]


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


def test_operational_market_and_bar_updates_skip_identity_conflicts(tmp_path) -> None:
    database = Database(tmp_path / "operational-identity-quarantine.sqlite3")
    database.initialize()
    _seed_assets(database, "AAA", "BBB")
    for index, symbol in enumerate(("AAA", "BBB"), start=1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    database.upsert_bars(
        [
            DailyBar(
                symbol="AAA",
                timestamp=datetime(2026, 8, 11, tzinfo=UTC),
                open=Decimal("9"),
                high=Decimal("11"),
                low=Decimal("8"),
                close=Decimal("10"),
                volume=90,
            )
        ]
    )
    database.set_sync_value(
        "sec_identity_conflicts",
        "AAA",
        {
            "symbol": "AAA",
            "existing_cik": "0000000001",
            "proposed_cik": "0000009999",
            "source": "exact_sec_ticker",
            "status": "unresolved",
            "detected_at": "2026-08-13T12:00:00+00:00",
            "last_seen_at": "2026-08-13T12:00:00+00:00",
        },
    )
    existing_aaa_bars = database.bars_available_as_of("AAA", date(2026, 8, 13))
    alpaca = OperationalAlpaca()
    sync = DataSynchronizer(
        database,
        alpaca,
        None,
        market_data_batch_size=1,  # type: ignore[arg-type]
    )

    market = sync.refresh_market()
    bars = sync.sync_historical_bars()

    assert market["identity_conflicts_skipped"] == 1
    assert market["identity_conflict_sample"] == ["AAA"]
    assert market["symbols_requested"] == market["symbols_updated"] == 1
    assert market["errors"] == 0
    assert alpaca.batches == [["BBB"]]
    assert database.latest_market_snapshot("AAA") is None
    assert database.latest_market_snapshot("BBB") is not None
    assert database.dataset_states()["market_snapshot"]["status"] == "success"
    assert bars["identity_conflicts_skipped"] == 1
    assert bars["identity_conflict_sample"] == ["AAA"]
    assert bars["symbols_checked"] == bars["records_updated"] == 1
    assert bars["errors"] == 0
    assert alpaca.bar_batches == [["BBB"]]
    assert database.bars_available_as_of("AAA", date(2026, 8, 13)) == existing_aaa_bars
    assert database.dataset_states()["historical_bars"]["status"] == "success"


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
