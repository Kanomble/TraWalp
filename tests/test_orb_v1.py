"""Synthetic native-bar specification tests; no historical study or external data."""

import csv
import json
import socket
import sqlite3
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from trading_system import cli
from trading_system.backtest import orb_v1_research
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.orb_v1 import ORB_V1, OrbCandidate, simulate_orb_session
from trading_system.backtest.orb_v1_data import (
    discover_orb_universe,
    orb_remediation_warmup,
    orb_requirements_report,
    prepare_orb_data,
)
from trading_system.backtest.orb_v1_research import (
    ENTRY_BUCKETS,
    build_diagnostics,
    entry_bucket,
    run_orb_v1,
    simulate_prepared_orb,
    trade_metrics,
)
from trading_system.backtest.research_registry import (
    INDEPENDENT_RESEARCH_FAMILIES,
    RESEARCH_FAMILY_STATUS,
    ResearchStatus,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    IntradayRequirementStatus,
    _expected_timestamps,
    _observation_payload,
    candidate_requirements_from_report,
)
from trading_system.data.market_sessions import (
    daily_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, MarketDataBar, TradableAsset

SESSION = date(2025, 5, 2)
TF = BarTimeframe.MINUTES_15


def forbidden(*args, **kwargs):
    raise AssertionError("ORB must remain local, read-only and independent")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)


@pytest.fixture
def config():
    return load_settings().strategy.model_copy(deep=True)


def candidate(symbol="AAA", session=SESSION):
    previous = trading_sessions_between(session - timedelta(days=7), session)[-2]
    return OrbCandidate(session, symbol, previous, 1, 100, 20_000_000)


def bar(index, *, symbol="AAA", session=SESSION, opening=100, high=101, low=99, close=100):
    return MarketDataBar(
        symbol=symbol,
        timeframe=TF,
        timestamp=regular_session_bounds(session)[0] + timedelta(minutes=15 * index),
        open=Decimal(str(opening)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=1000,
    )


def native(symbol="AAA", session=SESSION):
    size = len(_expected_timestamps(session, TF, extended_hours=False))
    rows = [
        bar(index, symbol=symbol, session=session, opening=103, high=104, low=102, close=103)
        for index in range(size)
    ]
    rows[0] = bar(0, symbol=symbol, session=session)
    rows[1] = bar(1, symbol=symbol, session=session, opening=100, high=103, low=100, close=102)
    return rows


def test_exact_frozen_definition_and_independent_active_registry():
    assert asdict(ORB_V1) == {
        "research_family": "research-orb-v1",
        "research_id": "ORB-V1-15M-LONG",
        "status": "ACTIVE",
        "universe_name": "ORB_LIQUID_TOP100_US_EQUITY",
        "top_n": 100,
        "daily_lookback_sessions": 20,
        "timeframe": "15m",
        "opening_range_minutes": 15,
        "direction": "LONG",
        "extended_hours": False,
        "slippage_bps": 5.0,
        "commission_bps": 0.0,
        "signal": "FIRST_COMPLETED_CLOSE_STRICTLY_ABOVE_OPENING_RANGE_HIGH",
        "entry": "NEXT_ACTUAL_NATIVE_BAR_OPEN",
        "stop": "FROZEN_OPENING_RANGE_LOW",
        "exits": ("STOP", "SESSION_CLOSE"),
        "profit_target": None,
        "maximum_attempts_per_symbol_session": 1,
        "overnight": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
    }
    with pytest.raises(FrozenInstanceError):
        ORB_V1.top_n = 101
    assert INDEPENDENT_RESEARCH_FAMILIES[ORB_V1.research_family] == (ORB_V1,)
    assert RESEARCH_FAMILY_STATUS[ORB_V1.research_family] == ResearchStatus.ACTIVE


@pytest.mark.parametrize("close", [100, 101])
def test_intrabar_high_and_close_equality_do_not_trigger(close):
    rows = [bar(0), bar(1, high=105, close=close)]
    event = simulate_orb_session(candidate(), rows)
    assert event["status"] == "NO_BREAKOUT"
    assert event["signal_status"] is None
    assert event["opening_range_high"] == 101
    assert event["opening_range_low"] == 99


def test_first_strict_close_decision_at_completion_entry_next_actual_open_and_costs():
    rows = native()
    rows[1] = bar(1, high=110, close=101)
    rows[2] = bar(2, opening=101, high=103, low=100, close=102)
    # An actual later native bar is used; the simulator never manufactures missing index 3.
    rows.pop(3)
    rows[3] = bar(4, opening=104, high=105, low=102, close=103)
    event = simulate_orb_session(candidate(), rows)
    assert event["breakout_bar_timestamp"] == rows[2].timestamp.isoformat()
    assert event["decision_timestamp"] == (rows[2].timestamp + TF.duration).isoformat()
    assert event["entry_timestamp"] == rows[3].timestamp.isoformat()
    assert event["entry_reference_price"] == 104
    assert event["entry_fill_price"] == pytest.approx(104 * 1.0005)
    assert event["exit_fill_price"] == pytest.approx(103 * 0.9995)
    assert event["net_return"] == pytest.approx((103 * 0.9995) / (104 * 1.0005) - 1)
    assert event["return_R"] == pytest.approx((103 * 0.9995 - 104 * 1.0005) / 5)
    assert event["modeled_execution_cost"] == pytest.approx(
        event["gross_return"] - event["net_return"]
    )


def test_final_bar_signal_has_no_next_entry():
    rows = [bar(index) for index in range(26)]
    rows[-1] = bar(25, high=103, close=102)
    event = simulate_orb_session(candidate(), rows)
    assert event["status"] == "NO_ENTRY_NO_NEXT_BAR"
    assert event["signal_status"] == "BREAKOUT_CONFIRMED"
    assert event["entry_timestamp"] is None


@pytest.mark.parametrize("opening", [98, 99])
def test_invalid_risk_consumes_only_attempt_even_when_later_signal_would_be_valid(opening):
    rows = native()
    rows[2] = bar(2, opening=opening, high=104, low=opening, close=103)
    event = simulate_orb_session(candidate(), rows)
    assert event["status"] == "INVALID_RISK_GEOMETRY"
    assert event["initial_R"] <= 0
    assert event["entry_fill_price"] is None
    assert event["exit_timestamp"] is None


@pytest.mark.parametrize(
    "index,opening,low,reference",
    [
        (2, 103, 99, 99),  # stop is already active on entry bar
        (5, 98, 97, 98),  # real gap open, no fill at stale stop
        (5, 99, 98, 99),
        (5, 103, 99, 99),  # inclusive intrabar touch
        (25, 103, 98, 99),  # final bar stop precedes close
    ],
)
def test_frozen_stop_precedence_and_no_reentry(index, opening, low, reference):
    rows = native()
    rows[index] = bar(index, opening=opening, high=150, low=low, close=104)
    event = simulate_orb_session(candidate(), rows)
    assert event["stop_price"] == event["opening_range_low"] == 99
    assert event["initial_R"] == 4
    assert event["status"] == "EXECUTED_STOP"
    assert event["exit_reason"] == "STOP"
    assert event["exit_reference_price"] == reference
    assert event["exit_fill_price"] == pytest.approx(reference * 0.9995)
    assert event["exit_timestamp"] == rows[index].timestamp.isoformat()
    assert event["MFE"] < 0.02  # never includes the unknown post-stop 150 high


@pytest.mark.parametrize("session", [SESSION, date(2025, 7, 3), date(2025, 1, 2)])
def test_no_stop_exits_final_regular_close_no_overnight_including_early_close_and_dst(session):
    rows = native(session=session)
    event = simulate_orb_session(candidate(session=session), rows)
    assert event["status"] == "EXECUTED_SESSION_CLOSE"
    assert event["exit_reason"] == "SESSION_CLOSE"
    assert event["exit_timestamp"] == regular_session_bounds(session)[1].isoformat()
    assert event["holding_minutes"] == (len(rows) - 2) * 15
    assert event["MFE"] == pytest.approx(104 / 103 - 1)
    assert event["MAE"] == pytest.approx(102 / 103 - 1)


@pytest.mark.parametrize(
    "malformation", ["duplicate", "5m", "premarket", "invalid_ohlc", "no_opening"]
)
def test_native_contract_rejects_invalid_inputs(malformation):
    rows = native()
    if malformation == "duplicate":
        rows.append(rows[0])
    elif malformation == "5m":
        rows[0] = rows[0].model_copy(update={"timeframe": BarTimeframe.MINUTES_5})
    elif malformation == "premarket":
        rows.insert(0, bar(-1))
    elif malformation == "invalid_ohlc":
        rows[0] = rows[0].model_copy(update={"low": Decimal(200)})
    else:
        rows.pop(0)
    with pytest.raises(ValueError):
        simulate_orb_session(candidate(), rows)


def seed_market(tmp_path, symbols=("AAA",), *, complete_native=True):
    database = Database(tmp_path / "orb.sqlite3")
    database.initialize()
    daily_sessions = trading_sessions_between(date(2025, 4, 1), SESSION)[-21:]
    assert len(daily_sessions[:-1]) == 20
    daily = []
    for index, symbol in enumerate(symbols, 1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="6798")
        )  # REIT SIC must not be excluded
        database.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    tradable=True,
                    fractionable=False,
                    asset_class="US_EQUITY",
                )
            ]
        )
        daily.extend(
            MarketDataBar(
                symbol=symbol,
                timestamp=datetime.combine(session, datetime.min.time(), UTC),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=200_000,
            )
            for session in daily_sessions
        )
        if complete_native:
            database.upsert_bars(native(symbol=symbol))
    database.upsert_bars(daily)
    return database


def absence(database, symbol="AAA", status="PROVIDER_CONFIRMED_ABSENT", **updates):
    payload = _observation_payload(
        symbol,
        TF,
        SESSION,
        feed="iex",
        adjustment="all",
        extended_hours=False,
        status=IntradayRequirementStatus(status),
        missing_timestamps=_expected_timestamps(SESSION, TF, extended_hours=False),
        error=None,
    )
    payload.update(updates)
    database.set_sync_value(PROVIDER_OBSERVATION_SOURCE, f"15m|{symbol}|{SESSION}", payload)


def test_top100_t_minus_one_deterministic_ties_and_no_absence_substitution(tmp_path, config):
    symbols = tuple(f"S{i:03d}" for i in range(101))
    database = seed_market(tmp_path, symbols, complete_native=False)
    # T's giant close/volume is invisible to the T universe, visible only from T+1.
    database.upsert_bars(
        [
            MarketDataBar(
                symbol="S100",
                timestamp=datetime(2025, 5, 2, tzinfo=UTC),
                open=1000,
                high=1001,
                low=999,
                close=1000,
                volume=2_000_000,
            )
        ]
    )
    for symbol in symbols:
        absence(database, symbol)
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    assert [row.symbol for row in prepared.candidates] == list(symbols[:100])
    assert [row.daily_universe_rank for row in prepared.candidates] == list(range(1, 101))
    assert all(
        row.previous_close == 100 and row.average_dollar_volume_20d == 20_000_000
        for row in prepared.candidates
    )
    assert prepared.daily_qualification["us_equity_assets_considered"] == 101
    assert prepared.ready
    with NativeEntrySessions() as spool:
        events = simulate_prepared_orb(prepared, spool)
    assert len(events) == 100
    assert {row["status"] for row in events} == {"PROVIDER_CONFIRMED_ABSENT"}
    assert "S100" not in {row["symbol"] for row in events}
    tomorrow = date(2025, 5, 5)
    following = discover_orb_universe(database, config, tomorrow, tomorrow)
    assert following.candidates[0].symbol == "S100"
    assert following.candidates[0].previous_close == 1000
    assert (
        following.candidates[0].average_dollar_volume_20d == (19 * 20_000_000 + 2_000_000_000) / 20
    )


@pytest.mark.parametrize("end", [SESSION, date(2025, 5, 5)])
def test_asset_scope_ignores_names_and_sec_identity_and_loads_assets_once(
    tmp_path, config, monkeypatch, end
):
    equities = ("AAPL", "MSFT", "SPY", "QQQ", "GLD", "IBIT")
    database = seed_market(tmp_path, equities + ("COIN", "UNCLASSIFIED", "INACTIVE", "OPTION"))
    with database.connect() as connection:
        connection.execute("DELETE FROM companies")
        connection.execute("UPDATE assets SET name='ETF Trust Fund' WHERE symbol='AAPL'")
        connection.execute("UPDATE assets SET name='Operating Company' WHERE symbol='SPY'")
        connection.execute("UPDATE assets SET asset_class='CRYPTO' WHERE symbol='COIN'")
        connection.execute("UPDATE assets SET asset_class='UNKNOWN' WHERE symbol='UNCLASSIFIED'")
        connection.execute("UPDATE assets SET asset_class='US_OPTION' WHERE symbol='OPTION'")
        connection.execute("UPDATE assets SET tradable=0 WHERE symbol='INACTIVE'")
    database.set_sync_value("sec_identity_conflicts", "AAPL", {"status": "unresolved"})
    for method in (
        "list_tradable_companies",
        "company_symbol_to_cik",
        "unresolved_sec_identity_conflict_symbols",
    ):
        monkeypatch.setattr(Database, method, forbidden)
    original = Database.list_tradable_assets
    calls = []

    def counted(self):
        calls.append(self.path)
        return original(self)

    monkeypatch.setattr(Database, "list_tradable_assets", counted)
    summary, paths = run_orb_v1(
        database, config, SESSION, end, tmp_path, stem="asset_scope", preflight=True
    )
    assert calls == [database.path]
    evidence = summary["daily_qualification"]
    assert evidence["tradable_assets_total"] == 9
    assert evidence["us_equity_assets_considered"] == 6
    assert evidence["crypto_assets_excluded"] == 1
    assert evidence["unknown_asset_class_excluded"] == 1
    assert evidence["other_asset_classes_excluded"] == 1
    assert evidence["sessions"][0]["eligible_after_daily_window"] == 6
    assert evidence["sessions"][0]["liquid_survivors"] == 6
    assert evidence["sessions"][0]["selected"] == 6
    scope = summary["universe_definition"]
    assert scope["asset_universe"] == "CURRENT_ALPACA_TRADABLE_US_EQUITY"
    assert scope["security_scope"] == "ALPACA_TRADABLE_US_EQUITY"
    assert scope["individual_stocks_only"] is False
    assert scope["etfs_etps_may_be_included"] is True
    assert scope["direct_crypto_allowed"] is False
    assert scope["execution_eligibility_germany"] == "NOT_EVALUATED"
    assert scope["sec_identity_required"] is False
    assert any("German/EU broker" in warning for warning in summary["warnings"])
    manifest = json.loads(paths["orb_candidates"].read_text())
    expected_symbols = sorted(equities) * len(trading_sessions_between(SESSION, end))
    assert [row["symbol"] for row in manifest["candidates"]] == expected_symbols
    assert {row["average_dollar_volume_20d"] for row in manifest["candidates"]} == {20_000_000}
    # Names are neither eligibility nor fingerprint inputs, even when they sound like products.
    with database.connect() as connection:
        connection.execute("UPDATE assets SET name='Crypto ETF Stock Trust Fund'")
    again = discover_orb_universe(database, config, SESSION, end)
    assert [row.symbol for row in again.candidates] == expected_symbols
    assert again.source_fingerprint == manifest["source_fingerprint"]
    assert len(calls) == 2


def test_asset_class_filter_precedes_top100_and_absence_never_replaces_rank101(tmp_path, config):
    equities = ("SPY",) + tuple(f"Z{i:03}" for i in range(100))
    database = seed_market(tmp_path, equities + ("COIN", "MYSTERY"), complete_native=False)
    with database.connect() as connection:
        connection.execute("UPDATE assets SET asset_class='CRYPTO' WHERE symbol='COIN'")
        connection.execute("UPDATE assets SET asset_class='UNKNOWN' WHERE symbol='MYSTERY'")
        connection.execute("UPDATE bars SET volume=999999999 WHERE symbol IN ('COIN','MYSTERY')")
    for symbol in equities:
        absence(database, symbol)
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    assert prepared.ready
    assert [row.symbol for row in prepared.candidates] == list(equities[:100])
    assert [row.daily_universe_rank for row in prepared.candidates] == list(range(1, 101))
    assert prepared.daily_qualification["sessions"][0]["liquid_survivors"] == 101
    assert len(prepared.coverage) == 100
    assert {row["status"] for row in prepared.coverage} == {"PROVIDER_CONFIRMED_ABSENT"}
    assert equities[100] not in {row.symbol for row in prepared.candidates}


def test_daily_thresholds_exact_boundary_and_missing_warmup_not_forward_filled(tmp_path, config):
    database = seed_market(tmp_path, ("AAA", "BBB"), complete_native=False)
    config.universe.min_price = 100
    config.universe.min_avg_dollar_volume_20d = 20_000_000
    assert len(discover_orb_universe(database, config, SESSION, SESSION).candidates) == 2
    missing = daily_warmup_start(SESSION, 10)
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE timeframe='1d' AND timestamp LIKE ?",
            (missing.isoformat() + "%",),
        )
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    assert not prepared.ready
    assert not prepared.candidates
    assert prepared.daily_qualification["sessions"][0]["unavailable_daily_windows"] == 2
    with pytest.raises(ValueError, match="ORB_DATA_UNAVAILABLE"):
        run_orb_v1(
            database,
            config,
            SESSION,
            SESSION,
            tmp_path,
            stem="missing_daily",
            rediscover_candidates=True,
        )


@pytest.mark.parametrize("status", ["LOCAL_MISSING_FETCHABLE", "PROVIDER_CHECK_FAILED"])
def test_local_missing_or_provider_failure_blocks_validation(tmp_path, config, status):
    database = seed_market(tmp_path, complete_native=False)
    if status == "PROVIDER_CHECK_FAILED":
        absence(database, status=status)
    summary, paths = run_orb_v1(
        database, config, SESSION, SESSION, tmp_path, stem="preflight", preflight=True
    )
    assert not summary["ready_for_local_validation"]
    assert json.loads(paths["intraday_requirements"].read_text())["discovery_complete"]
    assert next(csv.DictReader(paths["coverage"].open()))["status"] == status
    with pytest.raises(ValueError, match="ORB_DATA_UNAVAILABLE"):
        run_orb_v1(
            database, config, SESSION, SESSION, tmp_path, stem="blocked", rediscover_candidates=True
        )
    assert not (tmp_path / "blocked_summary.json").exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"feed": "sip"},
        {"extended_hours": True},
        {"adjustment": "split"},
        {"missing_timestamps": []},
    ],
)
def test_absence_evidence_must_match_requested_feed_and_missing_timestamps(
    tmp_path, config, updates
):
    database = seed_market(tmp_path, complete_native=False)
    absence(database, **updates)
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    assert prepared.coverage[0]["status"] == "LOCAL_MISSING_FETCHABLE"
    assert not prepared.ready


def test_complete_present_partial_absent_coverage_report_and_spool_only_simulation(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path, ("AAA", "BBB", "CCC"), complete_native=False)
    database.upsert_bars(native("AAA"))
    database.upsert_bars([bar(0, symbol="BBB")])
    absence(database, "BBB")
    absence(database, "CCC")
    with NativeEntrySessions(cache_limit=1) as spool:
        prepared = prepare_orb_data(database, config, SESSION, SESSION, native_sessions=spool)
        assert prepared.ready
        assert prepared.performance["native_bars_loaded"] == 27
        assert (
            prepared.performance["coverage_sql_queries"] == 2
        )  # observations + one native SELECT, no unused Daily close lookup
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        monkeypatch.setattr(Database, "bars_between", forbidden)
        events = simulate_prepared_orb(prepared, spool)
        assert spool.peak_cache_size == 1
    assert [row["status"] for row in events] == [
        "EXECUTED_SESSION_CLOSE",
        "PROVIDER_CONFIRMED_ABSENT",
        "PROVIDER_CONFIRMED_ABSENT",
    ]
    assert events[1]["opening_range_high"] is None  # partial absent session is wholly unobservable
    trades, metrics, _, concentration, by_day = build_diagnostics(prepared, events)
    assert len(trades) == 1
    assert metrics["eligible_symbol_sessions"] == 3
    assert metrics["opening_ranges_available"] == metrics["breakout_signals"] == 1
    assert metrics["percentage_of_eligible_sessions_breaking_out"] == pytest.approx(100 / 3)
    assert concentration["unique_traded_symbols"] == 1
    assert by_day == [{"session": str(SESSION), "signals": 1, "trades": 1}]


def test_preflight_does_not_inspect_signals_and_manifest_is_sync_compatible(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path)
    monkeypatch.setattr(orb_v1_research, "simulate_orb_session", forbidden)
    monkeypatch.setattr(orb_v1_research, "build_diagnostics", forbidden)
    summary, paths = run_orb_v1(
        database, config, SESSION, SESSION, tmp_path, stem="pre", preflight=True
    )
    manifest = json.loads(paths["intraday_requirements"].read_text())
    requirements = candidate_requirements_from_report(
        manifest, start=SESSION, end=SESSION, timeframes=(TF,)
    )
    assert [(row.symbol, row.session, row.timeframe) for row in requirements] == [
        ("AAA", SESSION, TF)
    ]
    assert summary["metrics"] == summary["stability"] == {}
    assert summary["ready_for_local_validation"]
    assert manifest["native_warmup_bars"] == 0
    assert orb_remediation_warmup(manifest, config, (TF,), False) == 0


@pytest.mark.parametrize("change", ["feed", "timeframe", "extended", "manifest"])
def test_orb_remediation_rejects_contract_drift_and_legacy_warmup_unchanged(
    tmp_path, config, change
):
    database = seed_market(tmp_path)
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    manifest = orb_requirements_report(prepared, config)
    timeframes, extended = (TF,), False
    if change == "feed":
        manifest["market_data_feed"] = "SIP"
    elif change == "timeframe":
        timeframes = (BarTimeframe.MINUTES_5,)
    elif change == "extended":
        extended = True
    else:
        manifest["native_warmup_bars"] = 50
    with pytest.raises(ValueError, match="ORB remediation"):
        orb_remediation_warmup(manifest, config, timeframes, extended)
    assert (
        orb_remediation_warmup(
            {"research_family": "research-f-intraday-entry-quality"}, config, (TF,), False
        )
        == config.intraday.warmup_bars
    )


@pytest.mark.parametrize("command", ["preflight-orb-v1", "validate-orb-v1"])
def test_cli_local_read_only_no_initialization_or_network(tmp_path, config, monkeypatch, command):
    database = seed_market(tmp_path)
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path = database.path
    settings.strategy.storage.reports_path = tmp_path
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)
    original_connect = sqlite3.connect
    connections = []

    def read_only_connect(path, *args, **kwargs):
        assert str(path).endswith("?mode=ro") and kwargs.get("uri") is True
        connections.append(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    assert (
        cli.main(
            [command, "--start", str(SESSION), "--end", str(SESSION), "--output-stem", command]
            + (["--rediscover-candidates"] if command == "validate-orb-v1" else [])
        )
        == 0
    )
    assert connections
    summary = json.loads((tmp_path / f"{command}_summary.json").read_text())
    assert summary["market_data_feed"] == "IEX"
    assert summary["consolidated_market_data"] is False
    assert summary["extended_hours"] is False
    assert summary["automatic_champion_selection"] is False
    assert summary["portfolio_strategy_defined"] is False
    if command == "validate-orb-v1":
        assert summary["performance"]["sqlite_query_count_orb_simulation"] == 0
        assert len(list(tmp_path.glob(f"{command}_*.csv"))) == 8
        assert summary["coverage"]["coverage_by_month"]["2025-05"]["coverage_ratio"] == 1
        assert summary["metrics"]["executed_trades"] == 1
        assert not any(
            word in json.dumps(summary).lower() for word in ("cagr", "sharpe", "equity_curve")
        )


@pytest.mark.parametrize(
    "clock,bucket",
    [
        ("09:45", 0),
        ("10:15", 0),
        ("10:30", 1),
        ("11:45", 1),
        ("12:00", 2),
        ("13:45", 2),
        ("14:00", 3),
        ("15:45", 3),
    ],
)
def test_frozen_entry_time_buckets(clock, bucket):
    assert entry_bucket(f"2025-05-02T{clock}:00-04:00") == ENTRY_BUCKETS[bucket]


def test_metric_arithmetic_uses_independent_unit_notional_returns():
    rows = []
    for value, risk in ((0.02, 0.5), (-0.01, -0.25), (0, 0)):
        rows.append(
            {
                "net_return": value,
                "gross_return": value + 0.001,
                "return_R": risk,
                "MFE": 0.03,
                "MAE": -0.02,
                "holding_minutes": 30,
                "modeled_execution_cost": 0.001,
            }
        )
    metrics = trade_metrics(rows)
    assert metrics["mean_net_return"] == metrics["expectancy"] == pytest.approx(0.01 / 3)
    assert metrics["median_net_return"] == 0
    assert metrics["profit_factor"] == 2
    assert metrics["win_rate"] == metrics["loss_rate"] == pytest.approx(1 / 3)
    assert metrics["average_win"] == 0.02
    assert metrics["average_loss"] == -0.01
    assert metrics["modeled_execution_cost"] == 0.003
    assert trade_metrics([])["mean_R"] is None
    json.dumps(trade_metrics([]), allow_nan=False)


def test_existing_report_refused_before_discovery(tmp_path, config, monkeypatch):
    (tmp_path / "existing_summary.json").write_text("historical artifact")
    monkeypatch.setattr(orb_v1_research, "prepare_orb_data", forbidden)
    with pytest.raises(FileExistsError):
        run_orb_v1(
            Database(tmp_path / "unused.db"), config, SESSION, SESSION, tmp_path, stem="existing"
        )
    assert (tmp_path / "existing_summary.json").read_text() == "historical artifact"


@pytest.mark.parametrize(
    "field,value", [("min_price", 100.01), ("min_avg_dollar_volume_20d", 20_000_001)]
)
def test_empty_liquid_universe_is_reported_without_portfolio_metrics(
    tmp_path, config, field, value
):
    database = seed_market(tmp_path)
    setattr(config.universe, field, value)
    summary, paths = run_orb_v1(
        database, config, SESSION, SESSION, tmp_path, stem="empty", rediscover_candidates=True
    )
    assert summary["ready_for_local_validation"]
    assert summary["metrics"]["eligible_symbol_sessions"] == 0
    assert summary["metrics"]["mean_net_return"] is None
    assert summary["concentration"]["top_1_symbol_share_of_total_net_pnl"] is None
    assert summary["coverage"]["coverage_ratio"] is None
    assert paths["trades"].read_text().count("\n") == 1
    assert paths["symbol_concentration"].read_text().strip().endswith("rank,share_of_total_net_pnl")


def test_all_event_counters_and_signed_concentration(tmp_path, config):
    symbols = ("ABSENT", "CLOSE", "INVALID", "NEXT", "NONE", "STOP")
    database = seed_market(tmp_path, symbols, complete_native=False)
    absence(database, "ABSENT")
    rows = native("CLOSE")
    rows[-1] = bar(25, symbol="CLOSE", opening=103, high=111, low=102, close=110)
    database.upsert_bars(rows)
    rows = native("INVALID")
    rows[2] = bar(2, symbol="INVALID", opening=99, high=104, low=99, close=103)
    database.upsert_bars(rows)
    rows = [bar(index, symbol="NEXT") for index in range(26)]
    rows[-1] = bar(25, symbol="NEXT", high=104, close=103)
    database.upsert_bars(rows)
    database.upsert_bars([bar(index, symbol="NONE") for index in range(26)])
    rows = native("STOP")
    rows[3] = bar(3, symbol="STOP", opening=103, high=104, low=99, close=103)
    database.upsert_bars(rows)
    summary, paths = run_orb_v1(
        database, config, SESSION, SESSION, tmp_path, stem="counters", rediscover_candidates=True
    )
    metrics = summary["metrics"]
    expected = {
        "eligible_symbol_sessions": 6,
        "observable_intraday_symbol_sessions": 5,
        "opening_ranges_available": 5,
        "breakout_signals": 4,
        "executed_trades": 2,
        "no_breakout_sessions": 1,
        "no_next_bar_events": 1,
        "invalid_risk_geometry": 1,
        "stop_exits": 1,
        "session_close_exits": 1,
        "signals_per_day": 4,
        "trades_per_day": 2,
        "percentage_of_breakouts_stopped": 25,
    }
    assert {key: metrics[key] for key in expected} == expected
    assert summary["coverage"]["provider_absent_symbol_sessions"] == 1
    assert summary["coverage"]["coverage_ratio"] == 5 / 6
    assert summary["concentration"]["top_1_symbol_share_of_total_net_pnl"] > 1
    assert summary["concentration"]["top_5_symbol_share_of_total_net_pnl"] == 1
    assert summary["stability"]["monthly"][0]["executed_trades"] == 2
    assert summary["stability"]["yearly"][0]["executed_trades"] == 2
    assert sum(row["executed_trades"] for row in summary["stability"]["entry_time_buckets"]) == 2
    with paths["orb_events"].open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 6


@pytest.mark.parametrize(
    "family,expected_warmup", [("research-orb-v1", 0), ("research-f-intraday-entry-quality", 50)]
)
def test_mocked_remediation_cli_uses_orb_contract_without_altering_existing_reports(
    tmp_path, config, monkeypatch, family, expected_warmup
):
    database = seed_market(tmp_path)
    prepared = prepare_orb_data(database, config, SESSION, SESSION)
    payload = orb_requirements_report(prepared, config)
    payload["research_family"] = family
    manifest = tmp_path / "requirements.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path = database.path
    settings.strategy.storage.reports_path = tmp_path
    settings.strategy.intraday.warmup_bars = 50
    settings.strategy.intraday.extended_hours = True
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    calls = []

    def stub_remediation(*args, **kwargs):
        calls.append(kwargs)
        return {
            **dict.fromkeys(
                (
                    "candidate_symbol_sessions_required",
                    "required_present_count",
                    "required_local_missing_count",
                    "provider_confirmed_absent_count",
                    "provider_check_failed_count",
                    "fetch_attempted_count",
                    "fetch_success_count",
                    "fetch_failed_count",
                ),
                0,
            ),
            "qualification_status": "READY",
        }

    monkeypatch.setattr(cli, "remediate_candidate_intraday_coverage", stub_remediation)
    monkeypatch.setattr(cli, "export_intraday_remediation_report", lambda *_args, **_kwargs: {})
    # Dispatch only: remediation and all provider construction are replaced with local stubs.
    assert (
        cli.main(
            [
                "sync-intraday",
                "--start",
                str(SESSION),
                "--end",
                str(SESSION),
                "--timeframes",
                "15m",
                "--candidates-report",
                str(manifest),
                "--candidate-gaps-only",
                "--output-stem",
                "stub",
            ]
        )
        == 0
    )
    assert calls[0]["warmup_bars"] == expected_warmup
    assert calls[0]["extended_hours"] is (family != "research-orb-v1")


def test_public_backtest_exports_retain_the_exact_existing_objects():
    import trading_system.backtest as api
    from trading_system.backtest import engine, position_manager

    for name in api.__all__:
        source = (
            engine
            if name in {"BacktestEngine", "compare_position_management", "compare_strategies"}
            else position_manager
        )
        assert getattr(api, name) is getattr(source, name)
    with pytest.raises(AttributeError):
        _ = api.not_a_public_api


def test_orb_module_imports_do_not_load_f_alpha_or_rejected_runners():
    code = """
import sys
import trading_system.backtest.orb_v1_research
for module in ('trading_system.champion', 'trading_system.backtest.engine',
               'trading_system.backtest.screen_strategies',
               'trading_system.backtest.lifecycle_research',
               'trading_system.backtest.intraday_entry_research',
               'trading_system.backtest.intraday_risk_research',
               'trading_system.backtest.regime_capacity_research'):
    assert module not in sys.modules, module
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
