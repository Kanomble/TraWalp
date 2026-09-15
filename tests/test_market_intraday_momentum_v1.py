"""Offline synthetic specification for the one frozen SPY momentum hypothesis."""

import math
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict
from datetime import date
from decimal import Decimal

import pytest
import requests

from trading_system import cli
from trading_system.backtest.market_intraday_momentum_v1 import (
    MOMENTUM_V1,
    observe_morning,
    session_definition,
    simulate_momentum_session,
)
from trading_system.backtest.market_intraday_momentum_v1_data import prepare_momentum_data
from trading_system.backtest.market_intraday_momentum_v1_manifest import discover_market_sessions
from trading_system.backtest.market_intraday_momentum_v1_research import (
    MAGNITUDE_BUCKETS,
    build_diagnostics,
    core_metrics,
    magnitude_bucket,
    predictive_diagnostics,
    return_statistics,
    simulate_prepared_momentum,
)
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.native_session_grid import expected_native_timestamps
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
from trading_system.models.market_data import BarTimeframe, MarketDataBar, TradableAsset

SESSION = date(2025, 5, 2)
TF = BarTimeframe.MINUTES_15


def forbidden(*args, **kwargs):
    raise AssertionError("Synthetic momentum research must remain local and isolated")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)


@pytest.fixture
def config():
    return load_settings().strategy.model_copy(deep=True)


def bar(index, *, session=SESSION, symbol="SPY", opening=100, close=100, high=None, low=None):
    opening, close = Decimal(str(opening)), Decimal(str(close))
    return MarketDataBar(
        symbol=symbol,
        timeframe=TF,
        timestamp=expected_native_timestamps(session, TF, False)[index],
        open=opening,
        close=close,
        high=max(opening, close) + 1 if high is None else high,
        low=min(opening, close) - 1 if low is None else low,
        volume=1000,
    )


def native(session=SESSION, *, first_close=102, final_close=204):
    return [
        bar(0, session=session, opening=100, close=101),
        bar(1, session=session, opening=101, close=first_close),
        bar(-2, session=session, opening=200, close=201),
        bar(-1, session=session, opening=201, close=final_close),
    ]


def seed_market(
    tmp_path, sessions=(SESSION,), *, stored_class="US_EQUITY", tradable=True, complete=True
):
    db = Database(tmp_path / "momentum.sqlite3")
    db.initialize()
    db.upsert_assets(
        [
            TradableAsset(
                symbol="SPY",
                name="Market proxy",
                tradable=tradable,
                fractionable=False,
                asset_class=stored_class,
            )
        ]
    )
    if complete:
        db.upsert_bars([row for session in sessions for row in native(session)])
    return db


def absence(db, missing, *, session=SESSION, status="PROVIDER_CONFIRMED_ABSENT"):
    payload = _observation_payload(
        "SPY",
        TF,
        session,
        feed="iex",
        adjustment="all",
        extended_hours=False,
        status=IntradayRequirementStatus(status),
        missing_timestamps=missing,
        error=None,
    )
    db.set_sync_value(PROVIDER_OBSERVATION_SOURCE, f"15m|SPY|{session}", payload)


def test_exact_frozen_identity_and_economics():
    assert asdict(MOMENTUM_V1) == {
        "research_family": "research-market-intraday-momentum-v1",
        "research_id": "MARKET-INTRADAY-MOMENTUM-V1-15M-SPY",
        "status": "REJECTED",
        "symbol": "SPY",
        "asset_class": "US_EQUITY",
        "tradable_required": True,
        "calendar": "XNYS",
        "timezone": "America/New_York",
        "timeframe": "15m",
        "extended_hours": False,
        "first_window": "FIRST_TWO_EXPECTED_NATIVE_REGULAR_BARS",
        "first_30m_return": "SECOND_NATIVE_CLOSE / FIRST_NATIVE_OPEN - 1",
        "decision": "SECOND_NATIVE_BAR_COMPLETION",
        "direction_rule": "POSITIVE_LONG_NEGATIVE_SHORT_EXACT_ZERO_NO_SIGNAL",
        "closing_window": "FINAL_TWO_EXPECTED_NATIVE_REGULAR_BARS",
        "entry": "OFFICIAL_PENULTIMATE_NATIVE_BAR_OPEN",
        "exits": ("FINAL_REGULAR_SESSION_CLOSE",),
        "long_slippage_bps": 5.0,
        "short_slippage_bps": 5.0,
        "commission_bps": 0.0,
        "borrow_fee_bps": 0.0,
        "stop": None,
        "profit_target": None,
        "trailing_stop": None,
        "partial_exit": None,
        "maximum_attempts_per_session": 1,
        "overnight": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
        "execution_eligibility_germany": "NOT_EVALUATED",
        "short_execution_eligibility": "NOT_MODELED",
    }
    with pytest.raises(FrozenInstanceError):
        MOMENTUM_V1.symbol = "QQQ"
    assert INDEPENDENT_RESEARCH_FAMILIES[MOMENTUM_V1.research_family] == (MOMENTUM_V1,)
    assert RESEARCH_FAMILY_STATUS[MOMENTUM_V1.research_family] == "REJECTED"
    assert RESEARCH_FAMILY_STATUS["research-orb-v1"] == "REJECTED"
    assert RESEARCH_FAMILY_STATUS["research-intraday-reversal-v1"] == "REJECTED"


@pytest.mark.parametrize(
    "close,direction",
    [
        (102, "LONG"),
        (98, "SHORT"),
        (100, None),
        ("100.0000000000000001", "LONG"),
        ("99.9999999999999999", "SHORT"),
    ],
)
def test_sign_rule_without_epsilon_and_exact_first_two_bars(close, direction):
    candidate = session_definition(SESSION)
    event = observe_morning(candidate, native(first_close=close))
    assert event["direction"] == direction and event["signal"] is (direction is not None)
    assert event["first_30m_return"] == pytest.approx(float((Decimal(str(close)) - 100) / 100))
    assert event["first_30m_open"] == 100 and event["first_30m_close"] == float(close)
    assert (
        event["decision_timestamp"] == expected_native_timestamps(SESSION, TF, False)[2].isoformat()
    )
    assert event["entry_timestamp"] is None
    if direction is None:
        assert event["status"] == "NO_SIGNAL_ZERO_FIRST_30M_RETURN"


@pytest.mark.parametrize("index", range(2, 26))
def test_every_post_10am_bar_is_irrelevant_to_morning_direction(index):
    candidate = session_definition(SESSION)
    rows = [bar(i) for i in range(26)]
    rows[1] = bar(1, close=102)
    expected = observe_morning(candidate, rows)
    rows[index] = bar(index, opening=1000, close=2, high=2000, low=1)
    assert observe_morning(candidate, rows) == expected


@pytest.mark.parametrize("first,final,long", [(102, 204, True), (98, 196, False), (98, 204, False)])
def test_exact_long_short_fills_returns_and_directional_hit(first, final, long):
    candidate = session_definition(SESSION)
    rows = native(first_close=first, final_close=final)
    event = simulate_momentum_session(candidate, rows)
    entry_fill = 200 * (1.0005 if long else 0.9995)
    exit_fill = final * (0.9995 if long else 1.0005)
    expected_gross = final / 200 - 1 if long else (200 - final) / 200
    expected_net = exit_fill / entry_fill - 1 if long else (entry_fill - exit_fill) / entry_fill
    assert event["entry_reference"] == 200 and event["exit_reference"] == final
    assert event["entry_fill"] == pytest.approx(entry_fill)
    assert event["exit_fill"] == pytest.approx(exit_fill)
    assert event["gross_return"] == pytest.approx(expected_gross)
    assert event["net_return"] == pytest.approx(expected_net)
    assert event["modeled_cost"] == pytest.approx(expected_gross - expected_net)
    assert event["directional_hit"] is (final > 200 if long else final < 200)
    assert event["holding_minutes"] == 30
    assert event["entry_timestamp"] == rows[2].timestamp.isoformat()
    assert event["exit_timestamp"] == candidate.closing.isoformat()


def test_zero_signal_retains_predictive_pair_but_no_trade():
    event = simulate_momentum_session(session_definition(SESSION), native(first_close=100))
    assert event["status"] == "NO_SIGNAL_ZERO_FIRST_30M_RETURN"
    assert event["final_30m_raw_return"] == pytest.approx(0.02)
    assert event["net_return"] is None and event["entry_fill"] is None
    assert event["directional_hit"] is None


@pytest.mark.parametrize(
    "index,status",
    [
        (0, "FIRST_30M_UNOBSERVABLE"),
        (1, "FIRST_30M_UNOBSERVABLE"),
        (2, "CLOSING_WINDOW_UNOBSERVABLE"),
        (3, "CLOSING_WINDOW_UNOBSERVABLE"),
    ],
)
def test_missing_required_bar_has_explicit_status_and_no_substitute(index, status):
    rows = native()
    rows.pop(index)
    rows += [bar(i, opening=111, close=112) for i in range(2, 24)]
    event = simulate_momentum_session(session_definition(SESSION), rows)
    assert event["status"] == status and event["net_return"] is None
    assert event["signal"] is (index >= 2)
    assert event["direction"] == ("LONG" if index >= 2 else None)
    assert event["entry_timestamp"] is None


def test_missing_or_extreme_midday_data_does_not_change_trade_and_no_stop_exists():
    candidate = session_definition(SESSION)
    expected = simulate_momentum_session(candidate, native())
    rows = native() + [bar(i, opening=1000, close=2) for i in range(2, 24)]
    assert simulate_momentum_session(candidate, rows) == expected
    rows[2] = bar(-2, opening=200, close=201, high=1000, low=1)
    event = simulate_momentum_session(candidate, rows)
    assert event["exit_reason"] == "FINAL_REGULAR_SESSION_CLOSE"
    assert event["exit_reference"] == 204 and event["holding_minutes"] == 30
    assert event["MFE"] == 4 and event["MAE"] == pytest.approx(1 / 200 - 1)


def test_short_excursions_reverse_sign_without_management():
    rows = native(first_close=98, final_close=196)
    rows[2] = bar(-2, opening=200, close=201, high=300, low=100)
    event = simulate_momentum_session(session_definition(SESSION), rows)
    assert event["MFE"] == 0.5 and event["MAE"] == -0.5
    assert event["exit_reference"] == 196 and event["holding_minutes"] == 30


@pytest.mark.parametrize(
    "day,entry,exit_time",
    [
        (SESSION, "15:30", "16:00"),
        (date(2025, 7, 3), "12:30", "13:00"),
        (date(2025, 1, 2), "15:30", "16:00"),
    ],
)
def test_official_early_closes_and_dst(day, entry, exit_time):
    from zoneinfo import ZoneInfo

    candidate = session_definition(day)
    event = simulate_momentum_session(candidate, native(day))
    local = ZoneInfo("America/New_York")
    assert candidate.final[0].astimezone(local).strftime("%H:%M") == entry
    assert candidate.closing.astimezone(local).strftime("%H:%M") == exit_time
    assert event["holding_minutes"] == 30
    assert event["decision_timestamp"] == (candidate.morning[1] + TF.duration).isoformat()


@pytest.mark.parametrize(
    "stored_class,tradable", [("CRYPTO", True), ("UNKNOWN", True), ("US_EQUITY", False)]
)
def test_proxy_eligibility_fails_closed_with_no_fallback(tmp_path, stored_class, tradable):
    db = seed_market(tmp_path, stored_class=stored_class, tradable=tradable)
    db.upsert_assets(
        [
            TradableAsset(
                symbol="QQQ",
                name="Fallback forbidden",
                tradable=True,
                fractionable=False,
                asset_class="US_EQUITY",
            )
        ]
    )
    with pytest.raises(ValueError, match="SPY_MARKET_PROXY_UNAVAILABLE"):
        discover_market_sessions(db, SESSION, SESSION)


def test_missing_spy_fails_and_daily_sec_inputs_are_never_used(tmp_path, config, monkeypatch):
    db = seed_market(tmp_path)
    for method in (
        "list_tradable_companies",
        "company_symbol_to_cik",
        "iter_bar_value_batches",
        "bars_available_as_of",
        "iter_entry_coverage_batches",
        "iter_native_session_batches",
    ):
        monkeypatch.setattr(Database, method, forbidden)
    original, loads = Database.list_tradable_assets, []

    def counted(self):
        loads.append(1)
        return original(self)

    monkeypatch.setattr(Database, "list_tradable_assets", counted)
    prepared = prepare_momentum_data(db, config, SESSION, SESSION)
    assert prepared.ready and loads == [1]
    assert prepared.performance["native_bars_loaded"] == 4
    with db.connect() as connection:
        connection.execute("DELETE FROM assets WHERE symbol='SPY'")
    with pytest.raises(ValueError, match="SPY_MARKET_PROXY_UNAVAILABLE"):
        discover_market_sessions(db, SESSION, SESSION)


def test_exact_indexed_native_loader_and_zero_sql_simulation(tmp_path, config, monkeypatch):
    days = (SESSION, date(2025, 5, 5))
    db = seed_market(tmp_path, days)
    db.upsert_bars([bar(10), bar(11)])
    statements = []

    class TracedDatabase(Database):
        @contextmanager
        def read_only(self):
            with super().read_only() as connection:
                connection.set_trace_callback(
                    lambda sql: (
                        statements.append(sql)
                        if sql.lstrip().upper().startswith(("WITH", "SELECT"))
                        else None
                    )
                )
                yield connection

    requirements = [
        ("SPY", day, ts) for day in days for ts in session_definition(day).required_timestamps
    ]
    batches = list(
        TracedDatabase(db.path).iter_native_timestamp_batches(
            requirements + requirements, batch_size=3
        )
    )
    assert len(batches) == len(statements) == 3
    assert sum(len(batch) for batch in batches) == 8
    assert all(item[3] is not None for batch in batches for item in batch)
    with db.read_only() as connection:
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statements[0])]
    assert any("SEARCH bars USING INDEX" in row and "timestamp=?" in row for row in plan)
    assert not any("SCAN bars" in row for row in plan)
    with NativeEntrySessions(cache_limit=1) as spool:
        prepared = prepare_momentum_data(db, config, days[0], days[-1], native_sessions=spool)
        assert prepared.performance["native_bars_loaded"] == 8
        assert prepared.performance["coverage_sql_queries"] == 2
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        events = simulate_prepared_momentum(prepared, spool)
        assert len(events) == 2 and all(row["holding_minutes"] == 30 for row in events)
        assert spool.peak_cache_size == 1


def test_predictive_statistics_include_zero_morning_tie_ranks_and_known_line():
    events = [
        {"first_30m_return": x, "final_30m_raw_return": 2 * x + 0.001}
        for x in (-0.02, 0, 0, 0.01, 0.03)
    ]
    result = predictive_diagnostics(events)
    assert result["paired_sessions"] == 5
    assert result["pearson_correlation"] == pytest.approx(1)
    assert result["spearman_rank_correlation"] == pytest.approx(1)
    assert result["ols_slope"] == pytest.approx(2)
    assert result["ols_intercept"] == pytest.approx(0.001)
    assert result["r_squared"] == pytest.approx(1)
    assert predictive_diagnostics([])["pearson_correlation"] is None
    flat = [{"first_30m_return": 0, "final_30m_raw_return": x} for x in (0, 0.01)]
    assert predictive_diagnostics(flat)["spearman_rank_correlation"] is None
    asymmetric_ties = [
        {"first_30m_return": x, "final_30m_raw_return": y}
        for x, y in ((1, 1), (1, 2), (2, 2), (3, 3))
    ]
    assert predictive_diagnostics(asymmetric_ties)["spearman_rank_correlation"] == pytest.approx(
        5 / 6
    )


def test_session_confidence_intervals_and_direction_attribution(tmp_path, config):
    values = [0.01, -0.01, 0.02]
    stats = return_statistics(values)
    expected_sd = math.sqrt(sum((x - sum(values) / 3) ** 2 for x in values) / 2)
    assert stats["standard_deviation"] == pytest.approx(expected_sd)
    assert stats["standard_error"] == pytest.approx(expected_sd / math.sqrt(3))
    assert stats["ci95_lower"] == pytest.approx(sum(values) / 3 - 1.96 * expected_sd / math.sqrt(3))
    assert return_statistics([0.1])["ci95_upper"] is None
    candidate = session_definition(SESSION)
    events = [
        simulate_momentum_session(candidate, native(first_close=close)) for close in (98, 100, 102)
    ]
    metrics = core_metrics(events)
    assert metrics["signals"] == metrics["executed_trades"] == 2
    assert metrics["zero_return_no_signals"] == 1
    assert metrics["long_signals"] == metrics["short_signals"] == 1
    assert metrics["directional_hit_rate"] == 0.5
    assert metrics["gross_profit_factor"] == pytest.approx(1)
    db = seed_market(tmp_path)
    prepared = prepare_momentum_data(db, config, SESSION, SESSION)
    _, _, tables = build_diagnostics(prepared, events)
    assert [row["group"] for row in tables["direction_attribution"]] == ["LONG", "SHORT"]
    assert all(row["executed_trades"] == 1 for row in tables["direction_attribution"])
    assert not any(key in metrics for key in ("CAGR", "Sharpe", "portfolio_MaxDD", "equity_curve"))


@pytest.mark.parametrize(
    "magnitude,index",
    [(0, 0), (0.0025, 0), (0.0025001, 1), (0.005, 1), (0.00501, 2), (0.01, 2), (0.01001, 3)],
)
def test_fixed_magnitude_diagnostics_boundaries(magnitude, index):
    assert magnitude_bucket({"first_30m_return": magnitude}) == MAGNITUDE_BUCKETS[index]
    assert magnitude_bucket({"first_30m_return": -magnitude}) == MAGNITUDE_BUCKETS[index]
