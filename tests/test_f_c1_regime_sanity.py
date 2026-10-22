"""Focused offline tests for diagnostic attribution, never parameter experiments."""

import socket
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from trading_system.backtest.regime_sanity import (
    ReadOnlyDatabase,
    attribution_tables,
    calendar_metrics,
    candidate_aggregation,
    offline_guard,
    spy_regimes,
)
from trading_system.data.market_sessions import trading_sessions_between


def test_regime_uses_completed_exact_t_prefix_and_never_future_bars():
    sessions = trading_sessions_between(date(2020, 1, 2), date(2022, 1, 6))
    t = sessions[-3]
    bars = [(s, 100 + i * 0.1) for i, s in enumerate(sessions)]
    prefix = [(s, c) for s, c in bars if s <= t]
    future_changed = [(s, c if s <= t else 0.01) for s, c in bars]
    expected = spy_regimes(prefix, [t])[t]
    assert spy_regimes(bars, [t])[t] == expected
    assert spy_regimes(future_changed, [t])[t] == expected
    assert expected["regime_c"] == "BULL"
    assert expected["spy_sma200"] == pytest.approx(sum(c for _, c in prefix[-200:]) / 200)
    assert expected["spy_momentum126"] == pytest.approx(prefix[-1][1] / prefix[-127][1] - 1)


def test_regime_equality_is_weak_and_missing_exact_session_is_rejected():
    sessions = trading_sessions_between(date(2020, 1, 2), date(2022, 1, 3))
    bars = [(s, 100) for s in sessions]
    assert spy_regimes(bars, [sessions[-1]])[sessions[-1]]["regime_c"] == "WEAK"
    with pytest.raises(ValueError, match="Incomplete SPY"):
        spy_regimes(bars[:-1], [sessions[-1]])
    with pytest.raises(ValueError, match="Missing SPY trailing session"):
        spy_regimes(bars[:-10] + bars[-9:], [sessions[-1]])


def test_year_return_uses_prior_year_equity_including_cross_year_unrealized_pnl():
    curve = [
        SimpleNamespace(date=date(2022, 12, 30), portfolio_equity=110, exposure=0.2, end_of_day_exposure=0.2),
        SimpleNamespace(date=date(2023, 1, 3), portfolio_equity=99, exposure=0.2, end_of_day_exposure=0.2),
        SimpleNamespace(date=date(2023, 1, 4), portfolio_equity=121, exposure=0.2, end_of_day_exposure=0),
    ]
    p = SimpleNamespace(entry_date=date(2022, 12, 30), exit_date=date(2023, 1, 4),
                        net_pnl=21, position_return=0.21, holding_days=3)
    result = SimpleNamespace(equity_curve=curve, positions=[p], initial_capital=100)
    row = calendar_metrics(result, date(2023, 1, 1), date(2023, 12, 31))
    assert row["return"] == pytest.approx(0.1)
    assert row["max_drawdown"] == pytest.approx(-0.1)
    assert row["equity_change_net_pnl"] == 11
    assert row["realized_net_pnl"] == 21
    assert row["trades_entered"] == 0
    assert row["trades"] == 1


def test_candidate_counts_include_zero_sessions():
    rows = [{"final_eligible_f_candidates": n} for n in (0, 0, 1, 3, 6)]
    counts = candidate_aggregation(rows)
    assert counts["median_eligible_candidates_per_session"] == 1
    assert counts["mean_eligible_candidates_per_session"] == 2
    assert counts["sessions_zero_candidates"] == 2
    assert counts["sessions_exactly_one_candidate"] == 1
    assert counts["sessions_more_than_one_candidate"] == 2


def test_entry_cohort_and_current_session_regime_remain_separate():
    d1, d2, d3 = date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)
    regimes = {d: {"regime_a": a, "regime_b": b, "regime_c": c}
               for d, a, b, c in ((d1, "ABOVE_SMA200", "POSITIVE_MOM126", "BULL"),
                                  (d2, "BELOW_EQUAL_SMA200", "NON_POSITIVE_MOM126", "WEAK"),
                                  (d3, "ABOVE_SMA200", "NON_POSITIVE_MOM126", "MIXED"))}
    position = SimpleNamespace(position_id="p1", symbol="AAA", signal_date=d1,
                               entry_date=d2, exit_date=d3, net_pnl=5,
                               position_return=0.05, holding_days=2)
    points = [SimpleNamespace(date=d, portfolio_equity=e, exposure=0.2,
                              end_of_day_exposure=0.2, active_positions=n)
              for d, e, n in ((d1, 100, 0), (d2, 99, 1), (d3, 105, 0))]
    result = SimpleNamespace(positions=[position], equity_curve=points, initial_capital=100)
    observer = SimpleNamespace(entry_context={(d1, "AAA"): {"candidate_rank": 1}})
    entries, sessions, summary = attribution_tables(result, regimes, observer)
    assert entries[0]["entry_known_regime_c"] == "BULL"
    assert sessions[1]["net_pnl"] == -1
    assert sessions[2]["net_pnl"] == 6
    by_regime = {r["regime"]: r for r in summary
                 if r["period"] == "ALL" and r["dimension"] == "regime_c"}
    assert by_regime["BULL"]["entry_cohort_net_pnl"] == 5
    assert by_regime["WEAK"]["net_pnl_session_mtm"] == -1
    assert by_regime["MIXED"]["net_pnl_session_mtm"] == 6
    assert by_regime["MIXED"]["trades_entered"] == 0


def test_runtime_guard_denies_http_raw_network_and_database_writes(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE bars (symbol TEXT)")
        c.execute("INSERT INTO bars VALUES ('SPY')")
    database = ReadOnlyDatabase(path)
    with offline_guard() as counts:
        with database.connect() as c:
            assert c.execute("SELECT symbol FROM bars").fetchone()[0] == "SPY"
            with pytest.raises(sqlite3.DatabaseError):
                c.execute("DELETE FROM bars")
        with pytest.raises(RuntimeError, match="READ_ONLY"):
            sqlite3.connect(path)
        with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
            requests.get("https://offline.invalid")
        with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
            socket.create_connection(("127.0.0.1", 1))
        assert counts["network_calls"] == 0
        assert counts["blocked_network_attempts"] == 2
        assert counts["blocked_database_write_attempts"] == 2
