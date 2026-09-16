"""Synthetic-only executable specification for the frozen Daily pair hypothesis."""

import json
import math
import socket
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from statistics import correlation, mean, stdev
from types import SimpleNamespace

import pytest
import requests

from trading_system import cli
from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_research as research
from trading_system.backtest.pairs_stat_arb_v1 import (
    PAIRS_V1,
    PairBar,
    calibration,
    crossing,
    observe,
    signal_direction,
    simulate_prepared_pairs,
    simulate_trade,
)
from trading_system.backtest.pairs_stat_arb_v1_data import (
    company_universe,
    discover_pairs,
    prepare_pairs_data,
)
from trading_system.backtest.pairs_stat_arb_v1_manifest import calendar_window
from trading_system.backtest.pairs_stat_arb_v1_research import (
    core_metrics,
    correlation_bucket,
    diagnostics,
    relationship,
    run_pairs_stat_arb_v1,
    z_bucket,
)
from trading_system.backtest.research_registry import (
    FROZEN_CHAMPION_F,
    INDEPENDENT_RESEARCH_FAMILIES,
    RESEARCH_FAMILY_STATUS,
    validate_champion_config,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.backtest import PositionManagementPreset, StrategyVariant
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import DailyBar, TradableAsset

START = date(2025, 4, 1)
END = date(2025, 4, 10)


def forbidden(*args, **kwargs):
    raise AssertionError("Pairs synthetic tests must stay local and isolated")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)


@pytest.fixture
def config():
    return load_settings().strategy.model_copy(deep=True)


def synthetic(*, end=END, crossing_after=None):
    sessions, days, tail = calendar_window(START, end)
    bars = {}
    base = 100.0
    for index, day in enumerate(sessions):
        base *= math.exp(0.015 * math.sin(index * 1.7))
        spread = 0.002 * math.sin(index * 0.6) if index < 60 else 0.035
        if crossing_after is not None and index >= 60 + crossing_after:
            spread = -0.01
        for symbol, price in (("A", base * math.exp(spread)), ("B", base), ("SPY", base)):
            bars[symbol, day] = PairBar(price * 1.001, price * 1.03, price * 0.97, price, 1_000_000)
    return sessions, days, tail, bars


def identities(symbols=("A", "B"), sic="3571"):
    return [
        {
            "symbol": s,
            "cik": f"{i + 1:010d}",
            "sic": sic,
            "sic2": sic[:2],
            "asset_class": "US_EQUITY",
            "tradable": True,
        }
        for i, s in enumerate(symbols)
    ]


def seed(tmp_path, *, end=END, crossing_after=None):
    sessions, days, tail, bars = synthetic(end=end, crossing_after=crossing_after)
    db = Database(tmp_path / "synthetic_pairs.sqlite3")
    db.initialize()
    db.upsert_assets(
        [
            TradableAsset(
                symbol=s, name=s, tradable=True, fractionable=False, asset_class="US_EQUITY"
            )
            for s in ("A", "B", "SPY")
        ]
    )
    for row in identities():
        db.upsert_company(
            CompanyIdentity(**{k: row[k] for k in ("symbol", "cik", "sic")}, name=row["symbol"])
        )
    db.upsert_bars(
        [
            DailyBar(
                symbol=s, timestamp=datetime.combine(day, datetime.min.time(), UTC), **asdict(bar)
            )
            for (s, day), bar in bars.items()
        ]
    )
    return db, sessions, days, tail, bars


def candidate(bars, sessions, index=60, z=None):
    observation = observe(bars, sessions, index, "A", "B")
    z = observation[1] if z is None else z
    status, a, b = signal_direction(z)
    return {
        "signal_session": str(sessions[index]),
        "symbol_a": "A",
        "symbol_b": "B",
        "sic2": "35",
        "adv20_a": 100_000_000,
        "adv20_b": 100_000_000,
        **calibration(bars, sessions, index, "A", "B"),
        "spread_t": observation[0],
        "entry_z": z,
        "status": status,
        "direction_a": a,
        "direction_b": b,
    }


def test_registry_and_f_isolation(config):
    assert PAIRS_V1.research_id == "PAIRS-STAT-ARB-V1-DAILY"
    assert PAIRS_V1.research_family == "research-pairs-stat-arb-v1"
    assert INDEPENDENT_RESEARCH_FAMILIES[PAIRS_V1.research_family] == (PAIRS_V1,)
    assert RESEARCH_FAMILY_STATUS[PAIRS_V1.research_family] == "ACTIVE"
    for family in (
        "research-orb-v1",
        "research-intraday-reversal-v1",
        "research-market-intraday-momentum-v1",
    ):
        assert RESEARCH_FAMILY_STATUS[family] == "REJECTED"
    assert FROZEN_CHAMPION_F.production_label == "F/configured/C1"
    assert FROZEN_CHAMPION_F.variant == StrategyVariant.QUALITY_VALUE_MOMENTUM
    assert FROZEN_CHAMPION_F.preset == PositionManagementPreset.CONFIGURED
    validate_champion_config(config)
    assert not any(
        "PAIRS" in item.name for cls in (StrategyVariant, PositionManagementPreset) for item in cls
    )
    assert PAIRS_V1.stop is PAIRS_V1.profit_target is PAIRS_V1.partial_exit is None
    assert not PAIRS_V1.portfolio_strategy_defined and not PAIRS_V1.beta_neutral


@pytest.mark.parametrize(
    "change", ["untradable", "crypto", "unknown", "no_company", "no_sic", "invalid_sic", "zero_sic"]
)
def test_company_boundary(tmp_path, change):
    db, *_ = seed(tmp_path)
    with db.connect() as connection:
        if change == "untradable":
            connection.execute("UPDATE assets SET tradable=0 WHERE symbol='A'")
        elif change in {"crypto", "unknown"}:
            connection.execute(
                "UPDATE assets SET asset_class=? WHERE symbol='A'", (change.upper(),)
            )
        elif change == "no_company":
            connection.execute("DELETE FROM companies WHERE symbol='A'")
        else:
            value = {"no_sic": None, "invalid_sic": "XX35", "zero_sic": "0000"}[change]
            connection.execute("UPDATE companies SET sic=? WHERE symbol='A'", (value,))
    assert [row["symbol"] for row in company_universe(db)] == ["B"]
    # SPY is a tradable US_EQUITY but lacks company/SIC identity.


def test_sic_normalization_and_no_industry_blacklist(tmp_path):
    db, *_ = seed(tmp_path)
    with db.connect() as connection:
        connection.execute("UPDATE companies SET sic='6021' WHERE symbol='A'")
        connection.execute("UPDATE companies SET sic='100' WHERE symbol='B'")
    assert [r["sic2"] for r in company_universe(db)] == ["60", "01"]


def test_spy_remains_diagnostic_even_with_company_identity(tmp_path):
    db, *_ = seed(tmp_path)
    db.upsert_company(CompanyIdentity(symbol="SPY", name="SPY", cik="0000000003", sic="3571"))
    assert [row["symbol"] for row in company_universe(db)] == ["A", "B"]


def test_top_five_ties_unordered_pairs_and_same_industry(config):
    sessions, days, _, _ = synthetic(end=START)
    names = identities(("G", "F", "E", "D", "C", "B", "A", "Z"))
    names[-1].update(sic="6021", sic2="60")
    bars = {
        (r["symbol"], day): PairBar(10, 10, 10, 10, 2_000_000) for r in names for day in sessions
    }
    selected, candidates, counts = discover_pairs(names, bars, sessions, days, config)
    assert [r["symbol"] for r in selected[str(START)]["35"]] == list("ABCDE")
    assert len(candidates) == 10
    pairs = [(r["symbol_a"], r["symbol_b"]) for r in candidates]
    assert len(set(pairs)) == 10 and all(a < b for a, b in pairs)
    assert all(r["sic2"] == "35" for r in candidates)
    assert counts["eligible_symbol_sessions"] == 8


def test_price_config_previous_close_and_adv_through_t(config):
    sessions, days, _, _ = synthetic(end=START)
    bars = {
        (symbol, day): PairBar(10, 10, 10, 10, 1_000_000)
        for symbol in ("A", "B")
        for day in sessions
    }
    config.universe.min_price = 10
    config.universe.min_avg_dollar_volume_20d = 10_000_000
    assert len(discover_pairs(identities(), bars, sessions, days, config)[1]) == 1
    config.universe.min_price = 10.01
    assert not discover_pairs(identities(), bars, sessions, days, config)[1]
    config.universe.min_price = 10
    config.universe.min_avg_dollar_volume_20d = 10_000_001
    assert not discover_pairs(identities(), bars, sessions, days, config)[1]
    config.universe.min_avg_dollar_volume_20d = 10_000_000
    bars["A", START] = PairBar(5, 5, 5, 5, 2_000_000)  # T price not min-price observation.
    assert len(discover_pairs(identities(), bars, sessions, days, config)[1]) == 1
    bars["A", START] = replace(bars["A", START], volume=1_999_999)
    assert not discover_pairs(identities(), bars, sessions, days, config)[1]
    del bars["A", sessions[45]]
    assert (
        discover_pairs(identities(), bars, sessions, days, config)[2][
            "unavailable_liquidity_symbol_sessions"
        ]
        == 1
    )


def test_calibration_exact_sixty_sample_std_and_fifty_nine_returns():
    sessions, _, _, bars = synthetic()
    result = calibration(bars, sessions, 60, "A", "B")
    a, b = ([bars[symbol, day].close for day in sessions[:60]] for symbol in ("A", "B"))
    spreads = [math.log(x / y) for x, y in zip(a, b, strict=True)]
    returns = [[math.log(values[i + 1] / values[i]) for i in range(59)] for values in (a, b)]
    assert result["calibration_mean"] == mean(spreads)
    assert result["calibration_std"] == stdev(spreads)
    assert result["correlation"] == correlation(*returns)
    old_observation = observe(bars, sessions, 60, "A", "B")
    for day in sessions[61:]:
        bars["A", day] = PairBar(1e8, 1e8, 1e8, 1e8, 1)
    assert observe(bars, sessions, 60, "A", "B") == old_observation
    bars["A", sessions[60]] = PairBar(1e9, 1e9, 1e9, 1e9, 1)
    assert calibration(bars, sessions, 60, "A", "B") == result


def test_future_data_cannot_change_historical_pair_membership(config):
    sessions, days, _, bars = synthetic()
    original = discover_pairs(identities(), bars, sessions, days, config)
    for day in sessions[61:]:
        bars["A", day] = PairBar(1e6, 1e6, 1e6, 1e6, 1)
    changed = discover_pairs(identities(), bars, sessions, days, config)
    assert original[0][str(START)] == changed[0][str(START)]
    assert original[1][0] == changed[1][0]


@pytest.mark.parametrize("missing", [0, 17, 59])
def test_missing_calibration_cannot_forward_fill(missing):
    sessions, _, _, bars = synthetic()
    del bars["A", sessions[missing]]
    assert (
        calibration(bars, sessions, 60, "A", "B")["calibration_status"] == "INCOMPLETE_CALIBRATION"
    )


def test_zero_variance_and_signal_close_required():
    sessions, _, _, bars = synthetic()
    del bars["A", sessions[60]]
    assert observe(bars, sessions, 60, "A", "B") is None
    for day in sessions[:60]:
        bars["A", day] = bars["B", day]
    assert calibration(bars, sessions, 60, "A", "B")["calibration_status"] == "ZERO_SPREAD_VARIANCE"


@pytest.mark.parametrize("corr,eligible", [(0.70, True), (0.699999999, False), (0.9, True)])
def test_exact_correlation_boundary(monkeypatch, corr, eligible):
    sessions, _, _, bars = synthetic()

    def measured(a, b):
        assert len(a) == len(b) == 59
        return corr

    monkeypatch.setattr(engine, "correlation", measured)
    assert (
        calibration(bars, sessions, 60, "A", "B")["calibration_status"] == "ELIGIBLE"
    ) == eligible


@pytest.mark.parametrize(
    "z,directions",
    [
        (2, ("SHORT", "LONG")),
        (3, ("SHORT", "LONG")),
        (-2, ("LONG", "SHORT")),
        (-3, ("LONG", "SHORT")),
        (1.999999999, (None, None)),
        (-1.999999999, (None, None)),
        (0, (None, None)),
    ],
)
def test_exact_signal_direction(z, directions):
    assert signal_direction(z)[1:] == directions


@pytest.mark.parametrize(
    "z,current,expected",
    [(2, 0, True), (2, -1, True), (2, 1, False), (-2, 0, True), (-2, 1, True), (-2, -1, False)],
)
def test_symmetric_crossing_boundary(z, current, expected):
    assert crossing(z, current) is expected


@pytest.mark.parametrize("z", [2, -2])
def test_exact_fills_weights_cost_and_max_hold(z):
    sessions, _, _, bars = synthetic()
    row = candidate(bars, sessions, z=z)
    # Freeze observation sign for this execution arithmetic fixture only.
    for day in sessions[61:66]:
        if z < 0:
            bars["A", day] = replace(bars["A", day], close=bars["B", day].close * 0.8)
    trade, _ = simulate_trade(row, bars, sessions, 60, END)
    assert trade["exit_reason"] == "MAX_HOLD_5"
    assert trade["entry_session"] == str(sessions[61])
    assert trade["exit_execution_session"] == str(sessions[65])
    assert trade["holding_sessions"] == 5
    assert trade["long_weight"] == trade["short_weight"] == 0.5
    assert trade["gross_notional"] == 1 and trade["net_notional"] == 0
    for leg in ("a", "b"):
        long = trade[f"direction_{leg}"] == "LONG"
        assert trade[f"entry_fill_{leg}"] == trade[f"entry_open_{leg}"] * (
            1.0005 if long else 0.9995
        )
        assert trade[f"exit_fill_{leg}"] == trade[f"exit_reference_{leg}"] * (
            0.9995 if long else 1.0005
        )
    assert trade["gross_pair_return"] == 0.5 * (
        trade["gross_long_return"] + trade["gross_short_return"]
    )
    assert trade["net_pair_return"] == 0.5 * (trade["net_long_return"] + trade["net_short_return"])
    assert trade["modeled_cost_drag"] == trade["gross_pair_return"] - trade["net_pair_return"]
    assert 0 < trade["modeled_cost_drag"] < 0.002


@pytest.mark.parametrize("cross_on", [1, 4, 5])
def test_crossing_close_next_open_and_fifth_session_precedence(cross_on):
    sessions, _, _, bars = synthetic(crossing_after=cross_on)
    trade, _ = simulate_trade(candidate(bars, sessions), bars, sessions, 60, END)
    if cross_on < 5:
        assert trade["exit_reason"] == "MEAN_REVERSION"
        assert trade["exit_trigger_session"] == str(sessions[60 + cross_on])
        assert trade["exit_execution_session"] == str(sessions[61 + cross_on])
        assert trade["exit_reference_a"] == bars["A", sessions[61 + cross_on]].open
    else:
        assert trade["exit_reason"] == "MAX_HOLD_5"
        assert trade["exit_reference_a"] == bars["A", sessions[65]].close


def test_negative_z_crossing_executes_next_open():
    sessions, _, _, bars = synthetic()
    row = candidate(bars, sessions, z=-2)
    trade, _ = simulate_trade(row, bars, sessions, 60, END)
    assert trade["exit_reason"] == "MEAN_REVERSION"
    assert trade["exit_execution_session"] == str(sessions[62])


@pytest.mark.parametrize("leg", ["A", "B"])
@pytest.mark.parametrize("offset,status", [(1, "ENTRY_UNOBSERVABLE"), (2, "EXIT_UNOBSERVABLE")])
def test_required_both_legs_never_substitute(leg, offset, status):
    sessions, _, _, bars = synthetic(crossing_after=1)
    row = candidate(bars, sessions)
    del bars[leg, sessions[60 + offset]]
    trade, _ = simulate_trade(row, bars, sessions, 60, END)
    assert trade["status"] == status
    assert trade["net_pair_return"] is None
    if offset == 1:
        assert trade["entry_fill_a"] is trade["entry_fill_b"] is None
    else:
        assert trade["exit_fill_a"] is trade["exit_fill_b"] is None


def test_mfe_mae_use_only_active_closes_no_stop_target_or_post_exit_prices():
    sessions, _, _, bars = synthetic(crossing_after=1)
    row = candidate(bars, sessions)
    expected, _ = simulate_trade(row, bars, sessions, 60, END)
    for symbol in ("A", "B"):
        for day in sessions[61:]:
            bars[symbol, day] = replace(bars[symbol, day], high=1e12, low=0.001)
        for day in sessions[62:]:
            bars[symbol, day] = replace(bars[symbol, day], close=1e6)
    actual, _ = simulate_trade(row, bars, sessions, 60, END)
    assert actual == expected  # Exit-day close and intraday extremes are irrelevant.
    mark = sum(
        0.5
        * (bars[s, sessions[61]].close / expected[f"entry_fill_{leg}"] - 1)
        * (1 if expected[f"direction_{leg}"] == "LONG" else -1)
        for s, leg in (("A", "a"), ("B", "b"))
    )
    assert expected["pair_mfe"] == expected["pair_mae"] == mark


def test_repeated_active_signals_ignored_then_reentry(monkeypatch):
    sessions, days, _, bars = synthetic()
    candidates = [candidate(bars, sessions, i, z=2) for i in range(60, 60 + len(days))]
    for row in candidates:
        row["calibration_status"] = "ELIGIBLE"
    monkeypatch.setattr(engine, "observe", lambda *args: (0.1, 2.0))
    prepared = SimpleNamespace(
        sessions=sessions,
        signal_dates={str(d): d for d in days},
        bars=bars,
        end=END,
        manifest={"candidates": candidates},
    )
    evaluations, signals = simulate_prepared_pairs(prepared)
    assert [r["signal_session"] for r in signals] == [str(days[0]), str(days[5])]
    assert sum(r["status"] == "PAIR_ACTIVE_IGNORED" for r in evaluations) == len(days) - 2


def test_tail_support_no_signals_and_censoring(tmp_path, config):
    db, sessions, _, tail, _ = seed(tmp_path, end=START)
    prepared = prepare_pairs_data(db, config, START, START)
    evaluations, signals = simulate_prepared_pairs(prepared)
    assert len(evaluations) == len(signals) == 1
    assert signals[0]["exit_execution_session"] == str(tail[-1])
    assert signals[0]["holding_sessions"] == 5
    for day in tail:
        prepared.bars.pop(("A", day))
    evaluations, signals = simulate_prepared_pairs(prepared)
    assert signals[0]["status"] == "OUTCOME_CENSORED"
    assert signals[0]["exit_fill_a"] is None
    metrics, _ = diagnostics(prepared, evaluations, signals)
    assert metrics["censored_outcomes"] == 1 and metrics["executed_trades"] == 0
    assert metrics["net_expectancy"] is metrics["net_profit_factor"] is None
    assert max(r["signal_session"] for r in evaluations) == str(START)
    assert len(sessions) == 66


@pytest.mark.parametrize("offset", [1, 2, 3, 4, 5])
def test_missing_outcome_tail_at_every_holding_session_is_censored(offset):
    sessions, _, _, bars = synthetic(end=START)
    row = candidate(bars, sessions)
    del bars["A", sessions[60 + offset]]
    trade, _ = simulate_trade(row, bars, sessions, 60, START)
    assert trade["status"] == "OUTCOME_CENSORED"
    assert trade["net_pair_return"] is trade["exit_execution_session"] is None
    assert trade["entry_status"] == ("ENTRY_UNOBSERVABLE" if offset == 1 else "EXECUTED")


def test_exact_flat_price_cost_is_two_weighted_round_trips():
    row = {"direction_a": "LONG", "direction_b": "SHORT"}
    for leg, side in (("a", "LONG"), ("b", "SHORT")):
        row[f"entry_open_{leg}"] = row[f"exit_reference_{leg}"] = 100.0
        row[f"entry_fill_{leg}"] = engine.fill(100.0, side, entry=True)
        row[f"exit_fill_{leg}"] = engine.fill(100.0, side, entry=False)
    engine.trade_returns(row)
    assert row["gross_pair_return"] == 0.0
    expected = 0.5 * (0.9995 / 1.0005 - 1) + 0.5 * (1 - 1.0005 / 0.9995)
    assert row["net_pair_return"] == pytest.approx(expected, abs=1e-15)
    assert row["modeled_cost_drag"] == pytest.approx(0.00100000025, abs=1e-12)


def test_no_signal_evaluation_produces_no_trade(config):
    sessions, days, _, bars = synthetic(end=START)
    bars["A", START] = replace(bars["A", START], close=bars["B", START].close)
    _, candidates, _ = discover_pairs(identities(), bars, sessions, days, config)
    prepared = SimpleNamespace(
        sessions=sessions,
        signal_dates={str(d): d for d in days},
        bars=bars,
        end=START,
        manifest={"candidates": candidates},
    )
    evaluations, signals = simulate_prepared_pairs(prepared)
    assert len(evaluations) == 1 and evaluations[0]["status"] == "NO_SIGNAL"
    assert not signals


def test_spy_is_only_a_diagnostic_and_unavailable_is_explicit(tmp_path, config):
    db, *_ = seed(tmp_path)
    prepared = prepare_pairs_data(db, config, START, END)
    before_evaluations, before_signals = simulate_prepared_pairs(prepared)
    for day in prepared.sessions:
        prepared.bars.pop(("SPY", day), None)
    after_evaluations, after_signals = simulate_prepared_pairs(prepared)
    assert before_evaluations == after_evaluations
    for old, new in zip(before_signals, after_signals, strict=True):
        assert {k: v for k, v in old.items() if k != "spy_return"} == {
            k: v for k, v in new.items() if k != "spy_return"
        }
    metrics, tables = diagnostics(prepared, after_evaluations, after_signals)
    assert metrics["market_beta"]["availability"] == "INSUFFICIENT_OBSERVATIONS"
    assert not tables["market_beta"]


def test_zero_sql_loop_and_no_candidate_rediscovery(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    _, paths = run_pairs_stat_arb_v1(db, config, START, END, tmp_path, stem="pre", preflight=True)
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    prepared = prepare_pairs_data(
        db, config, START, END, candidate_manifest=paths["pair_candidates"]
    )
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    evaluations, signals = simulate_prepared_pairs(prepared)
    assert evaluations and signals
    assert prepared.metadata["simulation_sql_queries"] == 0


def test_preflight_no_signal_or_outcome_evaluation(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(engine, "observe", forbidden)
    monkeypatch.setattr(research, "simulate_prepared_pairs", forbidden)
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="clean", preflight=True
    )
    assert set(paths) == {"summary", "pair_candidates", "coverage", "daily_requirements"}
    payload = json.loads(paths["pair_candidates"].read_text())
    for key in (
        "net_pair_return",
        "exit_reason",
        "entry_z",
        "spread_t",
        "win_rate",
        "profit_factor",
    ):
        assert key not in json.dumps(payload["candidates"])
        assert key not in json.dumps(summary)
    assert summary["network_used"] is False


@pytest.mark.parametrize("command", ["preflight-pairs-stat-arb-v1", "validate-pairs-stat-arb-v1"])
def test_cli_read_only_no_provider_initialization(tmp_path, config, monkeypatch, command):
    db, *_ = seed(tmp_path)
    config.storage.database_path = db.path
    config.storage.reports_path = tmp_path / "reports"
    monkeypatch.setattr(cli, "load_settings", lambda *args: SimpleNamespace(strategy=config))
    monkeypatch.setattr(Database, "initialize", forbidden)
    args = [command, "--start", str(START), "--end", str(END), "--output-stem", "cli"]
    if command.startswith("validate"):
        args.append("--rediscover-candidates")
    assert cli.main(args) == 0


def test_report_metrics_analytic_break_even_and_diagnostics(tmp_path, config):
    db, *_ = seed(tmp_path)
    prepared = prepare_pairs_data(db, config, START, END)
    evaluations, signals = simulate_prepared_pairs(prepared)
    metrics, tables = diagnostics(prepared, evaluations, signals)
    trades = tables["trades"]
    assert metrics["executed_trades"] == len(trades) > 0
    assert len(tables["chronological_thirds"]) == 3
    assert len(tables["holding_attribution"]) == 5
    assert len(tables["exit_attribution"]) == 2
    assert sum(r["net_pnl_contribution"] for r in tables["symbol_concentration"]) == pytest.approx(
        sum(r["net_pair_return"] for r in trades)
    )
    assert sum(r["pair_trades"] for r in tables["symbol_concentration"]) == 2 * len(trades)
    assert sum(r["net_contribution"] for r in tables["leg_attribution"]) == pytest.approx(
        sum(r["net_pair_return"] for r in trades)
    )
    cost = metrics["break_even_adverse_bps_per_fill"] / 10000
    implied = mean(
        0.5
        * (
            (1 + r["gross_long_return"]) * (1 - cost) / (1 + cost)
            - (1 - r["gross_short_return"]) * (1 + cost) / (1 - cost)
        )
        for r in trades
    )
    assert implied == pytest.approx(0, abs=1e-14)
    assert not {"CAGR", "Sharpe", "max_drawdown", "equity"} & metrics.keys()
    _, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="validation", rediscover_candidates=True
    )
    assert {
        "summary",
        "pair_candidates",
        "signals",
        "trades",
        "monthly",
        "yearly",
        "chronological_thirds",
        "exit_attribution",
        "z_bucket_attribution",
        "correlation_bucket_attribution",
        "holding_attribution",
        "industry_attribution",
        "pair_concentration",
        "symbol_concentration",
        "market_beta",
        "leg_attribution",
    } <= paths.keys()


def test_relationship_known_line_ties_and_missing_diagnostics():
    result = relationship([(1, 3), (1, 3), (2, 5), (3, 7)])
    assert result["pearson"] == result["spearman"] == result["r_squared"] == 1
    assert result["ols_beta"] == 2 and result["ols_intercept"] == 1
    assert relationship([])["availability"] == "INSUFFICIENT_OBSERVATIONS"
    assert relationship([(1, 2), (1, 2)])["availability"] == "ZERO_VARIANCE"
    assert core_metrics([])["net_expectancy"] is None


@pytest.mark.parametrize(
    "z,bucket", [(2, "2.0_TO_2.5"), (2.5, "2.5_TO_3.0"), (3, "2.5_TO_3.0"), (3.001, "OVER_3.0")]
)
def test_fixed_z_buckets(z, bucket):
    assert z_bucket({"entry_z": -z}) == bucket


@pytest.mark.parametrize(
    "corr,bucket",
    [(0.7, "0.70_TO_0.80"), (0.8, "0.80_TO_0.90"), (0.9, "0.90_TO_1.00"), (1, "0.90_TO_1.00")],
)
def test_fixed_correlation_buckets(corr, bucket):
    assert correlation_bucket({"correlation": corr}) == bucket
