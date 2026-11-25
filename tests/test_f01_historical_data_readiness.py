"""Targeted, offline checks for the Task 9.5 audit."""

import hashlib
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "reports")]

from f01_alpaca_historical_sip_coverage import populate  # noqa: E402
from f01_readiness_coverage import (  # noqa: E402
    filing_gaps,
    fundamental_rows,
    market_gaps,
    market_proxy,
    security_rows,
)
from f01_readiness_inventory import (  # noqa: E402
    LEGACY,
    SIP,
    U0,
    digest,
    encoding,
    read_database,
    tables,
)
from f01_readiness_requirements import requirements  # noqa: E402
from run_f01_historical_data_readiness import (  # noqa: E402
    CONFIG,
    acquisition,
    load_strategy_config,
)

from trading_system.data.canonical_facts import fingerprint  # noqa: E402
from trading_system.data.market_sessions import trading_sessions_between  # noqa: E402


def bars(*days):
    return set(days), set(days), Counter(), "digest"


def test_read_only_sqlite_pin_and_hash(tmp_path):
    path = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE candidate (value TEXT)")
        db.execute("INSERT INTO candidate VALUES ('observed')")
        db.commit()
    before = path.read_bytes()
    binding = {}
    with read_database(path, binding, hashlib.sha256(before).hexdigest()) as db:
        assert tables(db) == {"candidate"}
        assert db.execute("SELECT value FROM candidate").fetchone() == ("observed",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden (payload TEXT)")
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    with (
        pytest.raises(ValueError, match="DATABASE_HASH_MISMATCH"),
        read_database(path, {}, "0" * 64),
    ):
        pytest.fail("Bad hash was accepted")


def test_calendar_excludes_holiday_and_weekend():
    assert trading_sessions_between(date(2010, 1, 1), date(2010, 1, 5)) == [
        date(2010, 1, 4),
        date(2010, 1, 5),
    ]


def test_duplicate_bar_excluded_and_variants_kept_separate():
    row = ("2016-01-04T05:00:00+00:00", 9, 11, 8, 10, 100)
    assert populate([row])[1] == {"2016-01-04"}
    assert populate([row, row])[1] == set()
    days = ["2009-12-31", "2016-01-04", "2016-01-05"]
    stores = {
        (LEGACY, "SPY"): bars(*days),
        (SIP, "SPY"): bars(days[1]),
        (("forward", "alpaca", "sip", "raw"), "SPY"): bars(days[2]),
        (("forward", "alpaca", "iex", "all"), "SPY"): bars(days[2]),
    }
    ledger, windows, stats = market_gaps(stores, days, {"SPY": ""}, 1)
    assert stats["spy_missing_reference"] == 2
    assert stats["spy_warmup_300_missing"] == 1
    assert [item["start"] for item in windows] == [days[0], days[2]]
    assert [item["gap_id"] for item in windows] == [item["gap_id"] for item in ledger]


def test_market_proxy_never_bridges_missing_or_different_series():
    days = ["2016-01-04", "2016-01-05", "2016-01-06", "2016-01-07"]
    stores = {(source, symbol): bars() for source in (LEGACY, SIP, U0) for symbol in ("SPY", "AAA")}
    stores[LEGACY, "SPY"] = bars(*days)
    stores[LEGACY, "AAA"] = bars(days[0], days[2], days[3])
    stores[SIP, "AAA"] = bars(*days)
    result = market_proxy(stores, days, ["AAA"], 1)
    assert list(result["legacy"].values()) == [0, 0, 0, 1]
    assert sum(result["forward"].values()) == 0


def test_cik_is_not_a_security_interval():
    row = {
        "cik": "0000000001",
        "symbol": "AAA",
        "blocking_reasons": ["VALID_POINT_ONLY"],
        "dated_security_points": [
            {
                "valid_from": "2020-01-02",
                "valid_to": "2020-01-03",
                "epoch_id": "proper-epoch",
                "evidence_hash": "witness",
            }
        ],
    }
    scope, point, benchmark = security_rows([row], {}, "2010-01-01", "2020-01-03")
    assert scope["security_id"] == ""
    assert point["security_id"] == "proper-epoch"
    assert point["status"] == "DATED_WITNESS_ONLY"
    assert benchmark["symbol"] == "SPY" and benchmark["cik"] == ""


def test_acceptance_and_storage_never_infer_historical_pit():
    key = ("0000000001", "2010", "total_assets")
    counts = {
        key: Counter(candidates=1, acceptance=1),
        **{(key[0], key[1], m): Counter() for m in ("shares_outstanding",)},
    }
    from collections import defaultdict

    all_rows = list(
        fundamental_rows(
            {key[0]: "AAA"},
            [2010],
            {key: {"observed": "1"}},
            defaultdict(Counter, counts),
            {key: {"f01-sec-isolated-research-historical50-v1": 1}},
        )
    )
    row = next(r for r in all_rows if r["metric"] == key[2])
    assert row["research_usable"] and row["acceptance_bound_candidates"] == 1
    assert row["stored_canonical_facts_historical50"] == 1
    assert row["historical_pit_qualified"] is False


def test_acquisition_reuses_exact_accession_and_is_deterministic():
    first = {
        "cik": "0000000001",
        "accession": "0000000001-10-000001",
        "symbol_label": "AAA",
        "filed": "2010-01-10",
        "form": "10-K",
        "acceptance_utc": "2010-01-10T14:00:00+00:00",
        "cache_directories": [],
        "byte_check": {},
        "published_dts_ready": False,
        "published_extraction_complete": False,
        "problems": [],
        "original_status": "MISSING_IN_SCANNED_CACHES",
    }
    second = first | {"accession": "0000000001-10-000002", "original_status": "LOCAL_UNVERIFIED"}
    groups = [{"batch_id": "previous", "accessions": [first, second]}]
    _, blocks, _ = filing_gaps([first, second], groups, set())
    plan = acquisition([], [], blocks, {"universe": "bound"}, {})
    _, reversed_blocks, _ = filing_gaps([second, first], groups, set())
    assert plan == acquisition([], [], reversed_blocks, {"universe": "bound"}, {})
    assert plan["request_estimates"]["sec_accessions"] == 1
    assert plan["authorization"]["network"] is False
    assert plan["plan_fingerprint"] == fingerprint(
        {k: v for k, v in plan.items() if k != "plan_fingerprint"}
    )


def test_champion_requirements_trace_to_existing_concrete_code():
    matrix = requirements(load_strategy_config(CONFIG))
    assert matrix["strategy"] == "F/configured/C1"
    assert (matrix["minimum_market_history"], matrix["daily_load_preroll"]) == (300, 320)
    exact = date.fromisoformat(matrix["start_2010_warmup"])
    assert len(trading_sessions_between(exact, date(2009, 12, 31))) == 320
    assert all((ROOT / path).is_file() for row in matrix["inputs"] for path in row["consumers"])


def test_manifest_bytes_stable_under_key_reordering(tmp_path):
    first = tmp_path / "manifest-a.json"
    second = tmp_path / "manifest-b.json"
    first.write_bytes(encoding({"outputs": {"b.csv": "b", "a.csv": "a"}, "scope": "D6"}))
    second.write_bytes(encoding({"scope": "D6", "outputs": {"a.csv": "a", "b.csv": "b"}}))
    assert first.read_bytes() == second.read_bytes()
    assert digest(first) == digest(second)
