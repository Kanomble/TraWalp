"""Synthetic, offline specification of the independent frozen reversal hypothesis."""

import socket
import sqlite3
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from trading_system import cli
from trading_system.backtest import native_session_grid
from trading_system.backtest.intraday_reversal_v1 import (
    REVERSAL_V1,
    execute_signal,
    rank_first_hour,
)
from trading_system.backtest.intraday_reversal_v1_data import (
    discover_reversal_universe,
    prepare_reversal_data,
)
from trading_system.backtest.intraday_reversal_v1_research import (
    build_diagnostics,
    simulate_prepared_reversal,
    trade_metrics,
)
from trading_system.backtest.liquid_universe import LiquidityCandidate
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.native_session_grid import expected_native_timestamps
from trading_system.backtest.research_definitions import ORB_V1
from trading_system.backtest.research_registry import (
    INDEPENDENT_RESEARCH_FAMILIES,
    RESEARCH_FAMILY_STATUS,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    IntradayRequirementStatus,
    _observation_payload,
)
from trading_system.data.market_sessions import (
    daily_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.market_data import BarTimeframe, MarketDataBar, TradableAsset

SESSION = date(2025, 5, 2)
TF = BarTimeframe.MINUTES_15


def forbidden(*args, **kwargs):
    raise AssertionError("Synthetic research must remain local and isolated")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)


@pytest.fixture
def config():
    return load_settings().strategy.model_copy(deep=True)


def candidate(symbol="S000", rank=1, session=SESSION):
    previous = trading_sessions_between(session - timedelta(days=7), session)[-2]
    return LiquidityCandidate(session, symbol, previous, rank, 100, 20_000_000)


def bar(index, symbol="S000", session=SESSION, opening=100, close=100, high=None, low=None):
    return MarketDataBar(
        symbol=symbol,
        timeframe=TF,
        timestamp=regular_session_bounds(session)[0] + timedelta(minutes=15 * index),
        open=Decimal(str(opening)),
        close=Decimal(str(close)),
        high=Decimal(str(max(opening, close) + 1 if high is None else high)),
        low=Decimal(str(min(opening, close) - 1 if low is None else low)),
        volume=1000,
    )


def native(symbol="S000", session=SESSION, first_close=90):
    rows = [
        bar(i, symbol, session) for i in range(len(expected_native_timestamps(session, TF, False)))
    ]
    rows[3] = bar(3, symbol, session, close=first_close)
    return rows


def cross_section(size=100, session=SESSION, *, tied=False):
    candidates = [candidate(f"S{i:03}", i + 1, session) for i in range(size)]
    data = {
        row.symbol: native(row.symbol, session, first_close=90 if tied else 90 + i / 10)
        for i, row in enumerate(candidates)
    }
    return candidates, data


def seed_market(tmp_path, symbols=None, *, complete_native=True, session=SESSION):
    symbols = symbols if symbols is not None else tuple(f"S{i:03}" for i in range(100))
    database = Database(tmp_path / "reversal.sqlite3")
    database.initialize()
    database.upsert_assets(
        [
            TradableAsset(
                symbol=symbol,
                name=symbol,
                tradable=True,
                fractionable=False,
                asset_class="US_EQUITY",
            )
            for symbol in symbols
        ]
    )
    days = trading_sessions_between(daily_warmup_start(session, 21), session)
    assert len(days[:-1]) == 20
    database.upsert_bars(
        [
            MarketDataBar(
                symbol=symbol,
                timestamp=datetime.combine(day, datetime.min.time(), UTC),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=200_000,
            )
            for symbol in symbols
            for day in days
        ]
    )
    if complete_native:
        database.upsert_bars(
            [
                row
                for i, symbol in enumerate(symbols)
                for row in native(symbol, session, first_close=90 + i / 10)
            ]
        )
    return database


def absence(database, symbol, *, session=SESSION, missing=None, status="PROVIDER_CONFIRMED_ABSENT"):
    payload = _observation_payload(
        symbol,
        TF,
        session,
        feed="iex",
        adjustment="all",
        extended_hours=False,
        status=IntradayRequirementStatus(status),
        missing_timestamps=expected_native_timestamps(session, TF, False)
        if missing is None
        else missing,
        error=None,
    )
    database.set_sync_value(PROVIDER_OBSERVATION_SOURCE, f"15m|{symbol}|{session}", payload)


def test_definition_is_one_immutable_independent_hypothesis():
    assert asdict(REVERSAL_V1) == {
        "research_family": "research-intraday-reversal-v1",
        "research_id": "INTRADAY-REVERSAL-V1-15M-LONG",
        "status": "REJECTED",
        "universe_name": "REVERSAL_LIQUID_TOP100_US_EQUITY",
        "top_n": 100,
        "daily_lookback_sessions": 20,
        "timeframe": "15m",
        "extended_hours": False,
        "first_hour_bars": 4,
        "first_hour_definition": "09:30_OPEN_TO_10:15_BAR_CLOSE_AT_10:30_ET",
        "first_hour_return": "FOURTH_NATIVE_CLOSE / FIRST_NATIVE_OPEN - 1",
        "minimum_cross_section": 80,
        "signal_percent": 10,
        "signal_count_rule": "MAX_1_FLOOR_OBSERVABLE_COUNT_TIMES_0.10",
        "ranking": "FIRST_HOUR_RETURN_ASC_SYMBOL_ASC",
        "direction": "LONG",
        "entry": "NEXT_ACTUAL_NATIVE_BAR_OPEN",
        "exits": ("SESSION_CLOSE",),
        "stop": None,
        "profit_target": None,
        "slippage_bps": 5.0,
        "commission_bps": 0.0,
        "maximum_attempts_per_symbol_session": 1,
        "overnight": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
    }
    with pytest.raises(FrozenInstanceError):
        REVERSAL_V1.minimum_cross_section = 79
    assert INDEPENDENT_RESEARCH_FAMILIES[REVERSAL_V1.research_family] == (REVERSAL_V1,)
    assert RESEARCH_FAMILY_STATUS[REVERSAL_V1.research_family] == "REJECTED"
    assert RESEARCH_FAMILY_STATUS[ORB_V1.research_family] == ORB_V1.status == "REJECTED"
    assert ORB_V1.exits == ("STOP", "SESSION_CLOSE") and ORB_V1.slippage_bps == 5


@pytest.mark.parametrize("size,signals", [(79, 0), (80, 8), (83, 8), (97, 9), (100, 10)])
def test_cross_section_threshold_floor_and_weakest_selection(size, signals):
    candidates, data = cross_section(size)
    rows, diagnostic = rank_first_hour(candidates, data)
    assert diagnostic["signals"] == signals
    assert [row["symbol"] for row in rows if row["signal"]] == [f"S{i:03}" for i in range(signals)]
    assert rows[-1]["signal"] is False
    if size < 80:
        assert {row["status"] for row in rows} == {"SESSION_UNOBSERVABLE_CROSS_SECTION"}


def test_ties_use_symbol_and_no_absolute_return_gate():
    candidates, data = cross_section(100, tied=True)
    for symbol, rows in data.items():
        rows[3] = bar(3, symbol, close=110)  # even positive relative losers qualify
    rows, diagnostic = rank_first_hour(list(reversed(candidates)), data)
    selected = sorted(row["symbol"] for row in rows if row["signal"])
    assert selected == [f"S{i:03}" for i in range(10)]
    assert diagnostic["median_first_hour_return"] == pytest.approx(0.1)
    assert diagnostic["std_first_hour_return"] == 0


def test_first_hour_formula_completion_and_future_prices_do_not_change_decision():
    candidates, data = cross_section(80)
    data["S000"][0] = bar(0, opening=120, close=110)
    data["S000"][1] = bar(1, opening=110, close=109)
    data["S000"][2] = bar(2, opening=109, close=108)
    data["S000"][3] = bar(3, opening=108, close=90)
    rows, stats = rank_first_hour(candidates, data)
    assert rows[0]["first_hour_return"] == -0.25
    assert rows[0]["first_hour_open"] == 120 and rows[0]["first_hour_close"] == 90
    assert rows[0]["decision_timestamp"] == data["S000"][4].timestamp.isoformat()
    assert rows[0]["entry_reference"] is None
    for symbol in data:
        data[symbol] = data[symbol][:4] + [bar(4, symbol, close=10000), bar(25, symbol, close=2)]
    assert rank_first_hour(candidates, data) == (rows, stats)


@pytest.mark.parametrize("missing", range(4))
def test_every_first_hour_native_bar_is_required_and_later_bars_cannot_replace_it(missing):
    candidates, data = cross_section(80)
    data["S000"].pop(missing)
    rows, stats = rank_first_hour(candidates, data)
    assert rows[0]["first_hour_return"] is None
    assert stats["cross_section_size"] == 79
    assert stats["signals"] == 0
    assert not any(row["signal"] for row in rows)


@pytest.mark.parametrize("session", [SESSION, date(2025, 7, 3), date(2025, 1, 2)])
def test_next_native_entry_session_close_costs_excursions_and_no_stop_or_target(session):
    candidates, data = cross_section(80, session)
    rows, _ = rank_first_hour(candidates, data)
    holding = data["S000"]
    holding[4] = bar(4, session=session, opening=102, close=100, high=300, low=1)
    holding[-1] = bar(len(holding) - 1, session=session, close=105)
    event = execute_signal(rows[0], holding)
    assert event["entry_reference"] == 102  # never the first-hour close of 90
    assert event["entry_timestamp"] == holding[4].timestamp.isoformat()
    assert event["exit_timestamp"] == regular_session_bounds(session)[1].isoformat()
    assert event["exit_reference"] == 105 and event["exit_reason"] == "SESSION_CLOSE"
    assert event["entry_fill"] == pytest.approx(102 * 1.0005)
    assert event["exit_fill"] == pytest.approx(105 * 0.9995)
    assert event["gross_return"] == pytest.approx(105 / 102 - 1)
    assert event["net_return"] == pytest.approx(105 * 0.9995 / (102 * 1.0005) - 1)
    assert event["modeled_cost"] == pytest.approx(event["gross_return"] - event["net_return"])
    assert event["MFE"] == pytest.approx(300 / 102 - 1)
    assert event["MAE"] == pytest.approx(1 / 102 - 1)
    assert event["holding_minutes"] == (150 if session.month == 7 else 330)
    assert event["status"] == "EXECUTED_SESSION_CLOSE"


def test_next_actual_native_bar_and_no_next_bar_terminal_event():
    candidates, data = cross_section(80)
    row = rank_first_hour(candidates, data)[0][0]
    sparse = data["S000"][:4] + [bar(6, opening=111), bar(25, close=112)]
    event = execute_signal(row, sparse)
    assert event["entry_timestamp"] == bar(6).timestamp.isoformat()
    assert event["entry_reference"] == 111
    no_entry = execute_signal(row, data["S000"][:4])
    assert no_entry["status"] == "NO_ENTRY_NO_NEXT_BAR" and no_entry["net_return"] is None


def test_us_equity_scope_is_sec_independent_before_top100_ranking(tmp_path, config, monkeypatch):
    equities = ("AAPL", "SPY", "GLD") + tuple(f"S{i:03}" for i in range(98))
    other = ("AAA_CRYPTO", "AAA_UNKNOWN", "AAA_OPTION", "AAA_INACTIVE")
    db = seed_market(tmp_path, equities + other, complete_native=False)
    with db.connect() as connection:
        connection.executemany(
            "UPDATE assets SET asset_class=? WHERE symbol=?",
            [("CRYPTO", other[0]), ("UNKNOWN", other[1]), ("US_OPTION", other[2])],
        )
        connection.execute("UPDATE assets SET tradable=0 WHERE symbol=?", (other[3],))
        connection.execute("UPDATE assets SET name='ETF Fund Trust' WHERE symbol='AAPL'")
        connection.execute("UPDATE assets SET name='Operating Company' WHERE symbol='SPY'")
        connection.execute("UPDATE bars SET volume=999999999 WHERE symbol LIKE 'AAA_%'")
    for method in (
        "list_tradable_companies",
        "company_symbol_to_cik",
        "unresolved_sec_identity_conflict_symbols",
    ):
        monkeypatch.setattr(Database, method, forbidden)
    original, calls = Database.list_tradable_assets, []

    def counted(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Database, "list_tradable_assets", counted)
    prepared = discover_reversal_universe(db, config, SESSION, SESSION)
    assert calls == [1]
    assert [row.symbol for row in prepared.candidates] == sorted(equities)[:100]
    assert {row.average_dollar_volume_20d for row in prepared.candidates} == {20_000_000}
    assert prepared.daily_qualification["crypto_assets_excluded"] == 1
    assert prepared.daily_qualification["unknown_asset_class_excluded"] == 1


def test_t_daily_cannot_change_top100_and_absent_candidate_never_replaced(tmp_path, config):
    symbols = tuple(f"S{i:03}" for i in range(101))
    db = seed_market(tmp_path, symbols)
    with db.connect() as connection:
        connection.execute("DELETE FROM bars WHERE symbol='S000' AND timeframe='15m'")
        connection.execute(
            "UPDATE bars SET volume=999999999 WHERE symbol='S100' "
            "AND timeframe='1d' AND timestamp LIKE '2025-05-02%'"
        )
    absence(db, "S000")
    with NativeEntrySessions() as spool:
        prepared = prepare_reversal_data(db, config, SESSION, SESSION, native_sessions=spool)
        assert [row.symbol for row in prepared.candidates] == list(symbols[:100])
        events, sessions = simulate_prepared_reversal(prepared, spool)
        assert len(events) == 100 and sessions[0]["cross_section_size"] == 99
        assert sessions[0]["signals"] == 9
        assert "S100" not in {row["symbol"] for row in events}
    tomorrow = date(2025, 5, 5)
    assert discover_reversal_universe(db, config, tomorrow, tomorrow).candidates[0].symbol == "S100"


def test_later_absence_keeps_first_hour_rank_and_does_not_replace_signal(tmp_path, config):
    db = seed_market(tmp_path)
    missing = native()[5].timestamp
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='S000' AND timeframe='15m' AND timestamp=?",
            (missing.isoformat(),),
        )
    absence(db, "S000", missing=(missing,))
    with NativeEntrySessions() as spool:
        prepared = prepare_reversal_data(db, config, SESSION, SESSION, native_sessions=spool)
        events, sessions = simulate_prepared_reversal(prepared, spool)
    assert sessions[0]["cross_section_size"] == 100 and sessions[0]["signals"] == 10
    assert events[0]["signal"] is True and events[0]["cross_section_rank"] == 1
    assert events[0]["status"] == "PROVIDER_CONFIRMED_ABSENT"
    assert events[0]["net_return"] is None
    assert events[10]["signal"] is False and sessions[0]["executed_trades"] == 9


def test_batched_coverage_cached_grid_bounded_spool_and_zero_sql_simulation(
    tmp_path, config, monkeypatch
):
    db = seed_market(tmp_path)
    original, calls = native_session_grid._expected_timestamps, []

    def counted(session, timeframe, *, extended_hours):
        calls.append(session)
        return original(session, timeframe, extended_hours=extended_hours)

    expected_native_timestamps.cache_clear()
    monkeypatch.setattr(native_session_grid, "_expected_timestamps", counted)
    monkeypatch.setattr(Database, "iter_entry_coverage_batches", forbidden)
    with NativeEntrySessions(cache_limit=2) as spool:
        prepared = prepare_reversal_data(db, config, SESSION, SESSION, native_sessions=spool)
        assert (
            prepared.performance["coverage_sql_queries"] == 2
        )  # observations + one indexed native batch
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        events, sessions = simulate_prepared_reversal(prepared, spool)
        assert len(events) == 100 and sessions[0]["executed_trades"] == 10
        assert spool.peak_cache_size == 2 and spool.reads == 100
        assert calls == [SESSION]
        trades, metrics, tables = build_diagnostics(prepared, events, sessions)
        assert metrics["signals"] == 10 and len(trades) == 10
        assert tables["signal_rank_buckets"][0]["executed_trades"] == 2
        assert [row["executed_trades"] for row in tables["signal_rank_buckets"]] == [2, 3, 5]
        assert not {"CAGR", "Sharpe", "portfolio_drawdown", "return_R"}.intersection(metrics)


def test_metrics_use_independent_returns_and_empty_samples_are_explicit():
    base = {
        key: 0
        for key in (
            "first_hour_return",
            "cross_section_percentile",
            "MFE",
            "MAE",
            "holding_minutes",
            "modeled_cost",
        )
    }
    metrics = trade_metrics(
        [
            {**base, "gross_return": 0.02, "net_return": 0.019},
            {**base, "gross_return": -0.01, "net_return": -0.011},
        ]
    )
    assert metrics["gross_expectancy"] == 0.005
    assert metrics["net_expectancy"] == pytest.approx(0.004)
    assert metrics["gross_profit_factor"] == 2
    assert metrics["net_profit_factor"] == pytest.approx(0.019 / 0.011)
    assert metrics["win_rate"] == 0.5
    assert trade_metrics([])["net_expectancy"] is None
