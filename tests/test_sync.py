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


class IncrementalSec:
    def __init__(self) -> None:
        self.accession = "0001"
        self.fact_calls = 0
        self.submission_calls = 0
        self.ticker_calls = 0
        self.fail_symbols: set[str] = set()
        self.unavailable_submissions: set[str] = set()
        self.unavailable_companyfacts: set[str] = set()
        self.index_calls: list[tuple[int, int, bool]] = []

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

    def filing_index(self, year: int, quarter: int, *, current: bool) -> str:
        self.index_calls.append((year, quarter, current))
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

    def filing_index(self, year: int, quarter: int, *, current: bool) -> str:
        self.index_calls.append((year, quarter, current))
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
    sec.filing_index = lambda *_args, **_kwargs: _master_index(  # type: ignore[method-assign]
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
    assert second["sec_requests_total"] == 2
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
    assert database.sync_value("sec_change_detection", "xbrl_index") is None


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


def test_malformed_change_source_fails_without_advancing_cursor_or_fetching_companies(
    tmp_path,
) -> None:
    database = Database(tmp_path / "malformed-index.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    sec = IncrementalSec()
    sec.filing_index = lambda *_args, **_kwargs: "malformed"  # type: ignore[method-assign]
    sync = DataSynchronizer(database, None, sec)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="required headers"):
        sync.sync_sec_incremental()

    assert sec.submission_calls == sec.fact_calls == 0
    assert database.sync_value("sec_change_detection", "xbrl_index") is None


def test_cross_quarter_cursor_fetches_archived_and_current_indexes(tmp_path) -> None:
    database = Database(tmp_path / "catchup.sqlite3")
    database.initialize()
    _seed_assets(database, "TEST")
    database.set_sync_value("sec_accessions", "0000001234", ["old-accession"])
    database.set_sync_value(
        "sec_change_detection", "xbrl_index", {"last_data_received": "2026-03-30"}
    )
    sec = IncrementalSec()

    def filing_index(year: int, quarter: int, *, current: bool) -> str:
        sec.index_calls.append((year, quarter, current))
        if current:
            return _master_index(("0000001234", "10-Q", "q2-accession"), received="April 1, 2026")
        return _master_index(("0000001234", "10-K", "q1-accession"), received="March 31, 2026")

    sec.filing_index = filing_index  # type: ignore[method-assign]
    sec.accession = "q2-accession"
    sync = DataSynchronizer(
        database,
        None,
        sec,
        clock=lambda: datetime(2026, 4, 2, 12, tzinfo=UTC),
    )  # type: ignore[arg-type]

    result = sync.sync_sec_incremental()

    assert sec.index_calls == [(2026, 1, False), (2026, 2, True)]
    assert result["change_detection_requests"] == 2
    assert result["companies_checked"] == 1
    assert set(database.sync_value("sec_accessions", "0000001234")) >= {
        "old-accession",
        "q1-accession",
        "q2-accession",
    }
    assert database.sync_value("sec_change_detection", "xbrl_index") == {
        "last_data_received": "2026-04-01"
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
    sec.filing_index = lambda *_args, **_kwargs: _master_index(  # type: ignore[method-assign]
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
