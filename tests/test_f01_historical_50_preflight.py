"""Focused offline checks for the historical planning census."""

import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reports"))
import f01_historical_50_preflight_run as preflight  # noqa: E402


def test_daily_dates_use_new_york_and_invalid_bars_are_separate():
    seen, valid, invalid = preflight.bar_projection(
        [
            ("2026-01-03T01:00:00+00:00", "3", "4", "2", "4", "100"),
            ("2026-01-05T05:00:00+00:00", 3, 4, 2, 4, 0),
            ("2026-01-06T05:00:00+00:00", 3, 4, 2, 5, 100),
        ]
    )
    assert seen == {date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)}
    assert valid == {date(2026, 1, 2)}
    assert invalid == 2


def test_daily_bar_requires_a_zoned_source_and_finite_prices():
    with pytest.raises(ValueError, match="UNZONED"):
        preflight.bar_projection([("2026-01-02T00:00:00", 3, 4, 2, 4, 100)])
    assert not preflight.valid_bar(("stamp", 3, float("inf"), 2, 4, 100))


def test_annual_indexed_census_counts_daily_labels_and_honors_cutoff():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE bars(symbol TEXT, timeframe TEXT, timestamp TEXT)")
        db.execute(
            "CREATE INDEX ix_bars_symbol_timeframe_timestamp ON bars(symbol,timeframe,timestamp)"
        )
        db.executemany(
            "INSERT INTO bars VALUES (?,?,?)",
            [
                ("A", "1d", "2019-12-30"),
                ("A", "1d", "2019-12-31"),
                ("B", "1d", "2019-12-31"),
                ("C", "1m", "2019-12-31"),
                ("A", "1d", "2026-11-20"),
                ("B", "1d", "2026-11-21"),
            ],
        )
        result = preflight.annual_symbol_bars(db)
    assert result["2019"] == {"rows": 3, "symbols": 2, "first": "2019-12-30", "last": "2019-12-31"}
    assert result["2026"]["rows"] == result["2026"]["symbols"] == 1
    assert result["2020"]["first"] is None
    sessions = preflight.preceding_sessions(date(2020, 1, 1), 300)
    assert len(sessions) == 300 and sessions[-1] == date(2019, 12, 31)


def test_filing_float_window_avoids_post_2022_comparative():
    indexed = {}
    facts = []
    for accession, filed, value in [("one", "2022-08-01", 3_000_000), ("two", "2023-08-01", 7)]:
        facts.append(
            {"accn": accession, "filed": filed, "end": "2022-06-01", "form": "10-K", "val": value}
        )
        indexed[accession] = (
            date.fromisoformat(filed),
            "10-K",
            datetime.fromisoformat(filed).replace(tzinfo=UTC),
        )
    payload = {"facts": {"dei": {"EntityPublicFloat": {"units": {"USD": facts}}}}}
    actual = preflight.public_float(payload, indexed)
    assert actual["accession"] == "one" and actual["reported_public_float_usd"] == 3_000_000
    facts.append(facts[0] | {"val": 99})
    assert preflight.public_float(payload, indexed) is None


def test_evidenced_ticker_reuse_cannot_promote_current_cik():
    row = {
        "symbol": "JATT",
        "research_cik": "0001855644",
        "category": "TICKER_REUSE",
        "observed_2022_candidate": None,
        "dated_security_points": [],
    }
    filings = {
        "companyfacts_availability": "LOCAL_PROVIDER_SNAPSHOT_PRESENT",
        "period_counts": {"2020-2026": {"missing_original_shard_keys": []}},
        "lifecycle_index_events": [{"form": "25"}],
    }
    source = {
        "company_snapshot": {"JATT": {"cik": "0002112446"}},
        "daily": {
            "JATT": {
                "2020-2026": {
                    "first_date": "2021-09-10",
                    "last_date": "2026-10-23",
                    "observed_dates": 3,
                }
            }
        },
    }
    result = preflight.qualify_candidate(row, filings, source, source)
    assert result["research_cik"] == "0001855644"
    assert result["historical_security_id"] is None
    assert "LOCAL_SYMBOL_POINTS_TO_DIFFERENT_CIK" in result["reasons"]
