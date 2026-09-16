"""Definition-consumption sentinels are test doubles, not research parameter variants."""

import math
from dataclasses import asdict, replace
from statistics import mean, stdev
from types import SimpleNamespace

import pytest
from test_pairs_stat_arb_v1 import END, START, candidate, identities, seed, synthetic
from test_pairs_stat_arb_v1 import config as config
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types

from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import research_definitions as definitions
from trading_system.backtest.pairs_stat_arb_v1_data import (
    daily_requirements,
    discover_pairs,
    prepare_pairs_data,
)
from trading_system.backtest.pairs_stat_arb_v1_manifest import calendar_window, manifest_contract
from trading_system.backtest.pairs_stat_arb_v1_research import diagnostics


def test_frozen_primary_economics_remain_exact():
    d = definitions.PAIRS_STAT_ARB_V1
    assert (
        d.calibration_sessions,
        d.min_correlation,
        d.entry_z,
        d.long_weight,
        d.short_weight,
        d.slippage_bps,
        d.max_hold_sessions,
        d.top_n,
        d.adv_lookback_sessions,
        d.outcome_tail_sessions,
    ) == (60, 0.70, 2.0, 0.5, 0.5, 5.0, 5, 5, 20, 5)


def test_calibration_and_signal_read_the_definition(monkeypatch):
    sessions, _, _, bars = synthetic()
    sentinel = replace(
        definitions.PAIRS_STAT_ARB_V1, calibration_sessions=8, min_correlation=0.6, entry_z=1.25
    )
    monkeypatch.setattr(definitions, "PAIRS_STAT_ARB_V1", sentinel)

    def measured(a, b):
        assert len(a) == len(b) == 7
        return 0.6

    monkeypatch.setattr(engine, "correlation", measured)
    result = engine.calibration(bars, sessions, 60, "A", "B")
    spreads = [math.log(bars["A", day].close / bars["B", day].close) for day in sessions[52:60]]
    assert result["calibration_status"] == "ELIGIBLE"
    assert result["calibration_mean"] == mean(spreads)
    assert result["calibration_std"] == stdev(spreads)
    assert engine.signal_direction(1.25)[1:] == ("SHORT", "LONG")
    assert engine.signal_direction(-1.25)[1:] == ("LONG", "SHORT")
    assert engine.crossing(1.25, -0.1)


def test_discovery_manifest_and_calendar_share_definition(monkeypatch, config):
    sessions, days, _, _ = synthetic(end=START)
    bars = {
        (symbol, day): engine.PairBar(100, 100, 100, 100, 1_000_000)
        for symbol in ("A", "B", "C")
        for day in sessions
    }
    del bars["A", sessions[56]]  # Inside ordinary ADV20, outside the sentinel's last three.
    assert (
        "35"
        not in discover_pairs(identities(("A", "B", "C")), bars, sessions, days, config)[0][
            str(START)
        ]
    )
    sentinel = replace(
        definitions.PAIRS_STAT_ARB_V1,
        adv_lookback_sessions=3,
        top_n=2,
        calibration_sessions=8,
        outcome_tail_sessions=2,
    )
    monkeypatch.setattr(definitions, "PAIRS_STAT_ARB_V1", sentinel)
    selected, pairs, _ = discover_pairs(identities(("A", "B", "C")), bars, sessions, days, config)
    assert [r["symbol"] for r in selected[str(START)]["35"]] == ["A", "B"]
    assert len(pairs) == 1
    actual_sessions, actual_days, tail = calendar_window(START, START)
    assert len(actual_sessions) == 8 + len(actual_days) + 2
    assert len(tail) == 2
    assert manifest_contract(config, START, START)["strategy_definition"] == asdict(sentinel)


def test_weights_fills_holding_and_requirements_read_definition(monkeypatch, config):
    sessions, days, _, bars = synthetic()
    row = candidate(bars, sessions)
    sentinel = replace(
        definitions.PAIRS_STAT_ARB_V1,
        long_weight=0.65,
        short_weight=0.35,
        slippage_bps=12.0,
        max_hold_sessions=3,
    )
    monkeypatch.setattr(definitions, "PAIRS_STAT_ARB_V1", sentinel)
    trade, _ = engine.simulate_trade(row, bars, sessions, 60, END)
    assert trade["holding_sessions"] == 3
    assert trade["exit_execution_session"] == str(sessions[63])
    assert trade["long_weight"] == 0.65 and trade["short_weight"] == 0.35
    assert (
        trade["net_pair_return"]
        == 0.65 * trade["net_long_return"] + 0.35 * trade["net_short_return"]
    )
    for leg, long in (("a", False), ("b", True)):
        assert trade[f"entry_fill_{leg}"] == trade[f"entry_open_{leg}"] * (
            1.0012 if long else 0.9988
        )
        assert trade[f"exit_fill_{leg}"] == trade[f"exit_reference_{leg}"] * (
            0.9988 if long else 1.0012
        )
    path = [
        0.65 * (bars["B", day].close / trade["entry_fill_b"] - 1)
        + 0.35 * (1 - bars["A", day].close / trade["entry_fill_a"])
        for day in sessions[61:64]
    ]
    assert trade["pair_mfe"] == max(path) and trade["pair_mae"] == min(path)
    selected, pairs, _ = discover_pairs(identities(), bars, sessions, [days[0]], config)
    required, _ = daily_requirements(selected, sessions, pairs, bars)
    assert max(day for _, day in required) == sessions[63]
    prepared = SimpleNamespace(signal_dates={str(day): day for day in days})
    metrics, tables = diagnostics(prepared, [row], [trade])
    assert sum(r["net_contribution"] for r in tables["leg_attribution"]) == trade["net_pair_return"]
    cost = metrics["break_even_adverse_bps_per_fill"] / 10000
    assert (
        0.65 * ((1 + trade["gross_long_return"]) * (1 - cost) / (1 + cost) - 1)
        + 0.35 * (1 - (1 - trade["gross_short_return"]) * (1 + cost) / (1 - cost))
    ) == pytest.approx(0, abs=1e-14)


def test_prepared_manifest_cannot_claim_other_execution_economics(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    monkeypatch.setattr(
        definitions, "PAIRS_STAT_ARB_V1", replace(definitions.PAIRS_STAT_ARB_V1, slippage_bps=12)
    )
    with pytest.raises(ValueError, match="PAIRS_STAT_ARB_MANIFEST_MISMATCH"):
        engine.simulate_prepared_pairs(prepared)
