from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer
from trading_system.models.market_data import TradableAsset


def _daily_master(index_date: date, *rows: tuple[str, str, str]) -> str:
    lines = [
        "Description: Daily Master Index of EDGAR Dissemination Feed",
        "CIK|Company Name|Form Type|Date Filed|Filename",
        "-" * 80,
    ]
    lines.extend(
        f"{int(cik)}|Company {int(cik)}|{form}|{index_date.isoformat()}|"
        f"edgar/data/{int(cik)}/{accession}.txt"
        for cik, form, accession in rows
    )
    return "\n".join(lines)


class DailyIndexSec:
    def __init__(
        self,
        indexes: dict[date, str],
        ticker_map: dict[str, str] | None = None,
    ) -> None:
        self.indexes = indexes
        self.ticker_map = ticker_map or {"AAA": "0000000001"}
        self.directory_calls: list[tuple[int, int]] = []
        self.master_calls: list[date] = []
        self.submission_calls: list[str] = []
        self.companyfacts_calls: list[str] = []
        self.directory_failure: tuple[int, int] | None = None
        self.master_failure: date | None = None
        self.submission_failure: str | None = None

    def ticker_to_cik(self) -> dict[str, str]:
        return self.ticker_map

    def daily_master_index_directory(self, year: int, quarter: int) -> dict[str, Any]:
        self.directory_calls.append((year, quarter))
        if self.directory_failure == (year, quarter):
            raise RuntimeError("directory unavailable")
        names = [
            {"name": f"master.{item:%Y%m%d}.idx"}
            for item in self.indexes
            if item.year == year and (item.month - 1) // 3 + 1 == quarter
        ]
        return {"directory": {"item": names}}

    def daily_master_index(self, year: int, quarter: int, filing_date: str) -> str:
        index_date = datetime.strptime(filing_date, "%Y%m%d").date()
        assert year == index_date.year
        assert quarter == (index_date.month - 1) // 3 + 1
        self.master_calls.append(index_date)
        if self.master_failure == index_date:
            raise RuntimeError("daily master unavailable")
        return self.indexes[index_date]

    def filing_index(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("incremental sync requested the obsolete rolling XBRL index")

    def submissions(self, cik: str) -> dict[str, Any]:
        self.submission_calls.append(cik)
        if self.submission_failure == cik:
            raise RuntimeError("submissions unavailable")
        accessions = sorted(
            {
                accession
                for payload in self.indexes.values()
                for row in payload.splitlines()
                if row.startswith(f"{int(cik)}|")
                for accession in [row.rsplit("/", 1)[-1].removesuffix(".txt")]
            }
        )
        return {
            "name": f"Company {int(cik)}",
            "sic": "3571",
            "sicDescription": "Computers",
            "filings": {
                "recent": {
                    "accessionNumber": accessions,
                    "form": ["10-Q"] * len(accessions),
                }
            },
        }

    def company_facts(self, cik: str) -> dict[str, Any]:
        self.companyfacts_calls.append(cik)
        return {"cik": int(cik), "facts": {"us-gaap": {}}}


def _database(tmp_path, *symbols: str) -> Database:
    database = Database(tmp_path / "sec-daily.sqlite3")
    database.initialize()
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
    return database


def _clock(day: date):
    return lambda: datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def test_legacy_cursor_bootstraps_daily_indexes_without_reading_rolling_xbrl(tmp_path) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection", "xbrl_index", {"last_data_received": "2026-08-19"}
    )
    database.set_sync_value("sec_accessions", "0000000001", ["known"])
    sec = DailyIndexSec(
        {
            date(2026, 8, 18): _daily_master(
                date(2026, 8, 18), ("1", "10-Q", "known")
            ),
            date(2026, 8, 20): _daily_master(
                date(2026, 8, 20), ("1", "8-K", "ignored")
            ),
            date(2026, 8, 21): _daily_master(
                date(2026, 8, 21), ("1", "10-Q", "new-accession")
            ),
        }
    )

    result = DataSynchronizer(
        database, None, sec, clock=_clock(date(2026, 8, 23))
    ).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["change_detection_source"] == "sec_daily_master_index"
    assert result["cursor_before"] == "2026-08-19"
    assert result["cursor_after"] == "2026-08-21"
    assert result["legacy_cursor_bootstrap"] is True
    assert result["daily_indexes_discovered"] == result["daily_indexes_scanned"] == 3
    assert result["daily_indexes_new"] == 2
    assert result["relevant_filings_detected"] == 2
    assert result["change_candidates"] == 1
    assert database.sync_value("sec_change_detection", "daily_master_index") == {
        "last_processed_date": "2026-08-21"
    }
    assert database.sync_value("sec_change_detection", "xbrl_index") == {
        "last_data_received": "2026-08-19"
    }


def test_existing_daily_cursor_precedes_legacy_and_weekend_does_not_advance_to_today(
    tmp_path,
) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection", "daily_master_index", {"last_processed_date": "2026-08-21"}
    )
    database.set_sync_value(
        "sec_change_detection", "xbrl_index", {"last_data_received": "2026-08-22"}
    )
    database.set_sync_value("sec_accessions", "0000000001", ["known"])
    sec = DailyIndexSec(
        {
            date(2026, 8, 21): _daily_master(
                date(2026, 8, 21), ("1", "10-Q", "known")
            )
        }
    )

    result = DataSynchronizer(
        database, None, sec, clock=_clock(date(2026, 8, 23))
    ).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["legacy_cursor_bootstrap"] is False
    assert result["cursor_before"] == result["cursor_after"] == "2026-08-21"
    assert result["daily_indexes_new"] == result["change_candidates"] == 0
    assert sec.submission_calls == sec.companyfacts_calls == []
    assert database.dataset_states()["sec"]["status"] == "success"


def test_missing_accession_state_alone_does_not_trigger_company_level_requests(tmp_path) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection", "daily_master_index", {"last_processed_date": "2026-08-21"}
    )
    sec = DailyIndexSec(
        {
            date(2026, 8, 21): _daily_master(
                date(2026, 8, 21), ("1", "8-K", "irrelevant")
            )
        }
    )

    result = DataSynchronizer(
        database, None, sec, clock=_clock(date(2026, 8, 23))
    ).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["daily_indexes_new"] == result["change_candidates"] == 0
    assert result["submissions_requests"] == result["companyfacts_requests"] == 0
    assert sec.submission_calls == sec.companyfacts_calls == []


@pytest.mark.parametrize(
    ("cursor", "today", "available", "expected_directories"),
    [
        (
            date(2026, 6, 29),
            date(2026, 7, 2),
            (date(2026, 6, 30), date(2026, 7, 1)),
            [(2026, 2), (2026, 3)],
        ),
        (
            date(2026, 12, 29),
            date(2027, 1, 3),
            (date(2026, 12, 31), date(2027, 1, 2)),
            [(2026, 4), (2027, 1)],
        ),
    ],
)
def test_daily_discovery_crosses_quarter_and_year_boundaries_oldest_first(
    tmp_path, cursor, today, available, expected_directories
) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection",
        "daily_master_index",
        {"last_processed_date": cursor.isoformat()},
    )
    database.set_sync_value("sec_accessions", "0000000001", ["known"])
    sec = DailyIndexSec(
        {item: _daily_master(item, ("1", "10-Q", "known")) for item in reversed(available)}
    )

    result = DataSynchronizer(database, None, sec, clock=_clock(today)).sync_sec_incremental()  # type: ignore[arg-type]

    assert sec.directory_calls == expected_directories
    assert sec.master_calls == list(available)
    assert result["cursor_after"] == available[-1].isoformat()
    assert result["daily_index_directories_checked"] == 2


@pytest.mark.parametrize("failure", ["directory", "fetch", "parse"])
def test_discovery_or_listed_index_failure_does_not_advance_cursor(tmp_path, failure) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection", "daily_master_index", {"last_processed_date": "2026-08-19"}
    )
    day = date(2026, 8, 20)
    sec = DailyIndexSec({day: _daily_master(day, ("1", "10-Q", "new"))})
    if failure == "directory":
        sec.directory_failure = (2026, 3)
    elif failure == "fetch":
        sec.master_failure = day
    else:
        sec.indexes[day] = "malformed"

    with pytest.raises((RuntimeError, ValueError)):
        DataSynchronizer(
            database, None, sec, clock=_clock(date(2026, 8, 21))
        ).sync_sec_incremental()  # type: ignore[arg-type]

    assert database.sync_value("sec_change_detection", "daily_master_index") == {
        "last_processed_date": "2026-08-19"
    }
    assert database.dataset_states()["sec"]["status"] == "failed"
    assert sec.submission_calls == sec.companyfacts_calls == []


def test_company_update_failure_leaves_daily_cursor_at_previous_safe_date(tmp_path) -> None:
    database = _database(tmp_path, "AAA")
    database.set_sync_value(
        "sec_change_detection", "daily_master_index", {"last_processed_date": "2026-08-19"}
    )
    day = date(2026, 8, 20)
    sec = DailyIndexSec({day: _daily_master(day, ("1", "10-Q", "new"))})
    sec.submission_failure = "0000000001"

    result = DataSynchronizer(
        database, None, sec, clock=_clock(date(2026, 8, 21))
    ).sync_sec_incremental()  # type: ignore[arg-type]

    assert result["errors"] == 1
    assert result["cursor_after"] == "2026-08-19"
    assert database.sync_value("sec_change_detection", "daily_master_index") == {
        "last_processed_date": "2026-08-19"
    }


def test_overlap_is_idempotent_and_aggregates_multiple_ciks(tmp_path) -> None:
    database = _database(tmp_path, "AAA", "BBB")
    database.set_sync_value(
        "sec_change_detection", "daily_master_index", {"last_processed_date": "2026-08-19"}
    )
    day = date(2026, 8, 20)
    sec = DailyIndexSec(
        {
            day: _daily_master(
                day,
                ("1", "10-Q", "a-new"),
                ("1", "10-Q", "a-new"),
                ("2", "20-F", "b-new"),
            )
        },
        {"AAA": "0000000001", "BBB": "0000000002"},
    )
    sync = DataSynchronizer(database, None, sec, clock=_clock(date(2026, 8, 21)))  # type: ignore[arg-type]

    first = sync.sync_sec_incremental()
    second = sync.sync_sec_incremental()

    assert first["change_candidates"] == first["companies_checked"] == 2
    assert second["change_candidates"] == second["companies_checked"] == 0
    assert sec.master_calls == [day, day]
    assert sec.submission_calls == ["0000000001", "0000000002"]
    assert database.sync_value("sec_accessions", "0000000001") == ["a-new"]
    assert database.sync_value("sec_accessions", "0000000002") == ["b-new"]


def test_full_sec_sync_does_not_use_daily_or_rolling_change_detection(tmp_path) -> None:
    database = _database(tmp_path, "AAA")
    sec = DailyIndexSec({})
    sec.daily_master_index_directory = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("full sync used daily discovery")
    )

    result = DataSynchronizer(database, None, sec).sync_sec_full()  # type: ignore[arg-type]

    assert result["mode"] == "full"
    assert result["companies_checked"] == 1
    assert sec.submission_calls == sec.companyfacts_calls == ["0000000001"]
