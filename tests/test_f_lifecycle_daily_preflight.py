"""Bounded SQLite fixtures for local lifecycle coverage; no historical research jobs."""

import csv
import json
import socket
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
from test_backtest_features import _facts
from test_f_lifecycle_v2 import bar, record

from trading_system import cli
from trading_system.backtest import lifecycle_daily_preflight as daily
from trading_system.backtest.engine import BacktestEngine, CachedScreenSource
from trading_system.backtest.lifecycle_validation import _prepare
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, TradableAsset


@pytest.fixture
def config():
    load_settings.cache_clear()
    return load_settings().strategy


def market(tmp_path, config, start, end, *, missing=()):
    db = Database(tmp_path / "daily.sqlite3")
    db.initialize()
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="ABBV", name="ABBV", sic="2834"))
    db.upsert_assets([TradableAsset(symbol="ABBV", name="ABBV", tradable=True, fractionable=True)])
    warmup = daily_warmup_start(start, required_daily_warmup_sessions(config))
    history = trading_sessions_between(warmup, end)
    db.upsert_bars(
        [
            bar(symbol, session, 100 + i / 10).model_copy(update={"volume": 2_000_000})
            for symbol in ("ABBV", "SPY")
            for i, session in enumerate(history)
            if (symbol, session) not in missing
        ]
    )
    return db


def screens(monkeypatch, signals):
    calls = []

    class Source:
        def screen(self, session):
            calls.append(session)
            return SimpleNamespace(records=signals.get(session, ()))

    source = CachedScreenSource(Source())
    monkeypatch.setattr(
        daily,
        "prepare_strategy_comparison",
        lambda *args, **kwargs: SimpleNamespace(screen_source=source),
    )
    return calls


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def forbidden(*args, **kwargs):
    raise AssertionError("Network, sync, mutation or backtest is forbidden in the Daily preflight")


@pytest.fixture(autouse=True)
def no_network_or_backtest(monkeypatch):
    monkeypatch.setattr(BacktestEngine, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)


def test_abbv_internal_gap_and_four_reports(tmp_path, monkeypatch, config):
    start, end, gap = date(2022, 3, 4), date(2022, 3, 9), date(2022, 3, 8)
    db = market(tmp_path, config, start, end, missing={("ABBV", gap)})
    screens(monkeypatch, {start: [record("ABBV")]})
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert bundle.report["daily_qualified"] is True
    assert bundle.report["lifecycle_daily_qualified"] is False
    assert bundle.report["required_symbol_sessions"] == 3
    assert bundle.report["present_symbol_sessions"] == 2
    assert bundle.report["missing_symbol_sessions"] == 1
    assert bundle.report["missing_by_symbol"] == {"ABBV": 1}
    assert bundle.report["missing_by_session"] == {"2022-03-08": 1}
    assert bundle.report["missing_by_variant"] == dict.fromkeys(daily.HOLDING_LIMITS, 1)
    paths = daily.export_f_lifecycle_daily_preflight(bundle, tmp_path, stem="abbv")
    assert set(paths) == {
        "preflight.json",
        "daily_requirements.csv",
        "missing_symbol_sessions.csv",
        "candidate_summary.csv",
    }
    missing = read_csv(paths["missing_symbol_sessions.csv"])
    assert len(missing) == 1
    assert missing[0] == {
        "symbol": "ABBV",
        "required_session": "2022-03-08",
        "first_signal_session_requiring_it": "2022-03-04",
        "first_entry_session_requiring_it": "2022-03-07",
        "max_holding_day_requiring_it": "2",
        "required_by_variants": "L0,L1,L2,L3,L4,L5,L6",
        "status": "LOCAL_DAILY_MISSING",
        "candidate_requirement_count": "1",
    }
    assert [row["status"] for row in read_csv(paths["daily_requirements.csv"])] == [
        "REQUIRED_PRESENT",
        "LOCAL_DAILY_MISSING",
        "REQUIRED_PRESENT",
    ]
    assert read_csv(paths["candidate_summary.csv"])[0]["missing_sessions"] == "1"
    assert json.loads(paths["preflight.json"].read_text()) == bundle.report
    with pytest.raises(FileExistsError):
        daily.export_f_lifecycle_daily_preflight(bundle, tmp_path, stem="abbv")


@pytest.fixture
def thirty_sessions(tmp_path, monkeypatch, config):
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 3, 1))[:32]
    db = market(tmp_path, config, sessions[0], sessions[-1])
    screens(monkeypatch, {sessions[0]: [record("ABBV")]})
    return daily.build_f_lifecycle_daily_preflight(db, config, sessions[0], sessions[-1]), sessions


def test_complete_thirty_sessions_qualified(thirty_sessions):
    bundle, sessions = thirty_sessions
    assert bundle.report["lifecycle_daily_qualified"] is True
    assert bundle.report["max_required_holding_sessions"] == 30
    assert (
        bundle.report["required_symbol_sessions"] == bundle.report["present_symbol_sessions"] == 30
    )
    assert bundle.report["missing_symbol_sessions"] == 0
    rows = list(bundle.daily_requirements())
    assert rows[0]["required_session"] == sessions[1].isoformat()
    assert rows[0]["holding_day_if_entered"] == 1
    assert rows[-1]["required_session"] == sessions[30].isoformat()
    assert all(row["required_session"] != sessions[31].isoformat() for row in rows)


@pytest.mark.parametrize(
    "variant,maximum",
    [("L0", 10), ("L1", 15), ("L2", 20), ("L3", 30), ("L4", 20), ("L5", 20), ("L6", 20)],
)
def test_variant_holding_day_requirements(thirty_sessions, variant, maximum):
    bundle, _ = thirty_sessions
    assert [
        row["holding_day_if_entered"]
        for row in bundle.daily_requirements()
        if variant in row["required_by_variants"].split(",")
    ] == list(range(1, maximum + 1))


@pytest.mark.parametrize(
    "start,end,entry",
    [
        (date(2022, 3, 4), date(2022, 3, 9), date(2022, 3, 7)),
        (date(2024, 1, 12), date(2024, 1, 16), date(2024, 1, 16)),
    ],
)
def test_weekends_and_exchange_holiday_not_required(
    tmp_path, monkeypatch, config, start, end, entry
):
    db = market(tmp_path, config, start, end)
    screens(monkeypatch, {start: [record("ABBV")]})
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    rows = list(bundle.daily_requirements())
    assert rows[0]["entry_session"] == entry.isoformat()
    assert [row["required_session"] for row in rows] == [
        s.isoformat() for s in trading_sessions_between(entry, end)
    ]
    assert bundle.report["missing_symbol_sessions"] == 0


def test_research_end_caps_horizon_and_last_session_has_no_entry(tmp_path, monkeypatch, config):
    start, end = date(2024, 1, 4), date(2024, 1, 7)  # End on Sunday.
    db = market(tmp_path, config, start, end)
    calls = screens(monkeypatch, {start: [record("ABBV")], date(2024, 1, 5): [record("BBB")]})
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert calls == [start]
    assert bundle.report["candidate_count"] == 1
    assert [row["required_session"] for row in bundle.daily_requirements()] == ["2024-01-05"]


def test_missing_dedup_unions_variants_and_counts_candidates(tmp_path, monkeypatch, config):
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 3, 1))[:31]
    start, end = sessions[0], sessions[-1]
    db = market(tmp_path, config, start, end, missing={("ABBV", end)})
    screens(monkeypatch, {start: [record("ABBV")], sessions[20]: [record("ABBV")]})
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert bundle.report["candidate_requirement_count"] == 40
    assert bundle.report["required_symbol_sessions"] == 30
    assert len(bundle.missing) == 1
    row = bundle.missing[0]
    assert row["candidate_requirement_count"] == 2
    assert row["first_signal_session_requiring_it"] == start.isoformat()
    assert row["first_entry_session_requiring_it"] == sessions[1].isoformat()
    assert row["max_holding_day_requiring_it"] == 30
    assert row["required_by_variants"] == "L0,L1,L2,L3,L4,L5,L6"
    assert bundle.report["missing_by_variant"] == dict.fromkeys(daily.HOLDING_LIMITS, 1)


def test_all_candidates_canonical_f_gate_ranking_and_batched_keys(tmp_path, monkeypatch, config):
    start, end = date(2024, 1, 2), date(2024, 1, 3)
    db = market(tmp_path, config, start, end)
    candidates = [record(f"X{i:03d}") for i in range(405)]
    blocked = record("BLOCKED")
    blocked = blocked.model_copy(
        update={"technical": blocked.technical.model_copy(update={"momentum126": -0.2})}
    )
    screens(monkeypatch, {start: [*reversed(candidates), blocked]})
    monkeypatch.setattr(Database, "bars_on_session", forbidden)
    monkeypatch.setattr(Database, "bars_available_as_of", forbidden)
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert config.portfolio.max_positions == 1
    assert bundle.report["candidate_count"] == bundle.report["candidate_symbol_count"] == 405
    assert bundle.report["coverage_sql_queries"] == 2
    assert [row["symbol"] for row in bundle.candidates] == [r.symbol for r in candidates]
    assert [row["candidate_rank"] for row in bundle.candidates] == list(range(1, 406))
    assert bundle.report["missing_symbol_sessions"] == 405


def test_real_pit_screen_future_presence_and_filings_do_not_change_signal(
    tmp_path, monkeypatch, config
):
    start, end = date(2024, 3, 4), date(2024, 3, 5)
    # Minimal accounting/peer fixture; production F evaluator and technical gates stay real.
    config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(
                update={"min_quality_score": 0, "min_valuation_score": 0}
            ),
            "data_quality": config.data_quality.model_copy(
                update={"min_available_valuation_metrics": 1}
            ),
        }
    )
    db = market(tmp_path, config, start, end)
    db.upsert_facts(_facts("ABBV", "0000000001"))
    with db.read_only() as connection:
        original_data = list(connection.iterdump())
    with monkeypatch.context() as guard:
        guard.setattr(Database, "initialize", forbidden)
        guard.setattr(Database, "connect", forbidden)
        before = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    with db.read_only() as connection:
        assert list(connection.iterdump()) == original_data
    assert before.report["candidate_count"] == 1
    assert before.report["lifecycle_daily_qualified"] is True
    # A future filing and future price presence must not rewrite eligibility on T.
    future = _facts("ABBV", "0000000001")[-1].model_copy(
        update={"filed": end, "value": Decimal(1), "accession_number": "future-shares"}
    )
    db.upsert_facts([future])
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='ABBV' AND timestamp>=?", (end.isoformat(),)
        )
    after = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert [{key: row[key] for key in daily.CANDIDATE_FIELDS} for row in before.candidates] == [
        {key: row[key] for key in daily.CANDIDATE_FIELDS} for row in after.candidates
    ]
    assert after.report["missing_symbol_sessions"] == 1
    assert after.report["lifecycle_daily_qualified"] is False
    # Without any filing known by T, the real PIT source must reject the symbol.
    with db.connect() as connection:
        connection.execute("DELETE FROM fundamental_facts WHERE filed<=?", (start.isoformat(),))
    no_pit = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert no_pit.report["candidate_count"] == 0


@pytest.mark.parametrize("qualified", [True, False])
def test_cli_local_only_no_db_changes_no_backtest(tmp_path, monkeypatch, config, qualified):
    start, end = date(2024, 1, 2), date(2024, 1, 3)
    db = market(tmp_path, config, start, end, missing=() if qualified else {("ABBV", end)})
    screens(monkeypatch, {start: [record("ABBV")]})
    settings = SimpleNamespace(
        strategy=config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"database_path": db.path, "reports_path": tmp_path}
                )
            }
        )
    )
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)  # All shared readers must use mode=ro.
    with db.read_only() as connection:
        before = list(connection.iterdump())
    config_before = settings.strategy.model_dump()
    command = [
        "preflight-f-lifecycle-daily",
        "--start",
        str(start),
        "--end",
        str(end),
        "--output-stem",
        "local",
    ]
    assert cli.main(command) == (0 if qualified else 1)
    payload = json.loads((tmp_path / "local_preflight.json").read_text())
    assert payload["local_only"] is True
    assert payload["network_accessed"] is payload["backtest_executed"] is False
    assert payload["lifecycle_daily_qualified"] is qualified
    with db.read_only() as connection:
        assert list(connection.iterdump()) == before
    assert settings.strategy.model_dump() == config_before
    assert set(vars(cli._parser().parse_args(command))) == {
        "command",
        "start",
        "end",
        "output_stem",
        "config",
        "verbose",
    }
    monkeypatch.setattr(cli, "build_f_lifecycle_daily_preflight", forbidden)
    assert cli.main(command) == 1  # Existing stem refuses before any discovery.


@pytest.mark.parametrize("remove", ["warmup", "portfolio"])
def test_shared_daily_qualification_failure_exports_incomplete_report(
    tmp_path, monkeypatch, config, remove
):
    start, end = date(2024, 1, 2), date(2024, 1, 3)
    db = market(tmp_path, config, start, end)
    with db.connect() as connection:
        if remove == "warmup":
            connection.execute("DELETE FROM bars WHERE symbol='SPY' AND timestamp<?", (str(start),))
        else:
            connection.execute("DELETE FROM bars WHERE timestamp>=?", (str(end),))
    monkeypatch.setattr(daily, "prepare_strategy_comparison", forbidden)
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert bundle.report["daily_qualified"] is bundle.report["lifecycle_daily_qualified"] is False
    assert bundle.report["candidate_discovery_complete"] is False
    assert bundle.report["daily_qualification"]["failure_reasons"]
    with pytest.raises(ValueError, match="Daily qualification failed"):
        _prepare(db, config, start, end)
    paths = daily.export_f_lifecycle_daily_preflight(bundle, tmp_path, stem="unqualified")
    assert read_csv(paths["daily_requirements.csv"]) == []
    with paths["daily_requirements.csv"].open() as handle:
        assert next(csv.reader(handle)) == daily.REQUIREMENT_FIELDS


def test_intraday_bar_does_not_satisfy_daily_requirement(tmp_path, monkeypatch, config):
    start, end = date(2024, 1, 2), date(2024, 1, 3)
    db = market(tmp_path, config, start, end, missing={("ABBV", end)})
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT 'ABBV',?,timestamp,open,high,low,close,"
            "volume,trade_count,vwap "
            "FROM bars WHERE symbol='SPY' AND timestamp>=?",
            (BarTimeframe.MINUTES_15.value, str(end)),
        )
    screens(monkeypatch, {start: [record("ABBV")]})
    bundle = daily.build_f_lifecycle_daily_preflight(db, config, start, end)
    assert bundle.report["missing_symbol_sessions"] == 1


def test_invalid_dates_and_output_stem(tmp_path, config):
    db = market(tmp_path, config, date(2024, 1, 2), date(2024, 1, 3))
    with pytest.raises(ValueError, match="start must not be after end"):
        daily.build_f_lifecycle_daily_preflight(db, config, date(2024, 1, 3), date(2024, 1, 2))
    with pytest.raises(ValueError, match="plain file stem"):
        daily.research_output_paths(tmp_path, "../escape", daily_preflight=True)
