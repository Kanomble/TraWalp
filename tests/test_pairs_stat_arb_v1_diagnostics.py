"""Focused mathematical, clock, censoring and post-trade isolation regressions."""

import copy
import math
import socket
import sqlite3
from dataclasses import asdict
from datetime import date, timedelta
from statistics import correlation, mean, stdev

import pytest

from trading_system.backtest import pairs_stat_arb_v1 as v1
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_diagnostics as audit
from trading_system.backtest.pairs_stat_arb_v1_manifest import verify_membership


@pytest.fixture
def sample():
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(80)]
    bars = {}
    for i, day in enumerate(days):
        base = 100 * math.exp(0.02 * math.sin(i * 0.37))
        spread = 0.005 * math.sin(i * 0.73) if i < 60 else 0.03 - (i - 60) * 0.012
        for symbol, price in (("A", base * math.exp(spread)), ("B", base), ("SPY", base)):
            bars[symbol, day] = v1.PairBar(
                price * 1.001, price * 1.03, price * 0.97, price, 1_000_000
            )
    identities = {
        s: {"symbol": s, "identity_status": "CURRENT_SECURITY_NO_IDENTITY_WARNING"}
        for s in ("A", "B")
    }
    spread, z = v1.observe(bars, days, 60, "A", "B")
    status, da, db = v1.signal_direction(z)
    candidate = {
        "signal_session": str(days[60]),
        "symbol_a": "A",
        "symbol_b": "B",
        "sic2": "35",
        "adv20_a": 1e8,
        "adv20_b": 1e8,
        "spread_t": spread,
        "entry_z": z,
        "direction_a": da,
        "direction_b": db,
        "status": status,
        **v1.calibration(bars, days, 60, "A", "B"),
    }
    return days, bars, identities, candidate


@pytest.mark.parametrize(
    "field,sign",
    [
        ("spread_t", -1),
        ("calibration_mean", -1),
        ("calibration_std", 1),
        ("entry_z", -1),
        ("correlation", 1),
    ],
)
def test_spread_calibration_std_z_correlation_mirror(sample, field, sign):
    days, bars, identities, _ = sample
    a = audit.observation(bars, days, 60, "A", "B", identities)
    b = audit.observation(bars, days, 60, "B", "A", identities)
    assert a[field] == pytest.approx(sign * b[field], rel=audit.REL_TOL, abs=audit.ABS_TOL)
    if field == "calibration_mean":
        spreads = [math.log(bars["A", d].close / bars["B", d].close) for d in days[:60]]
        assert a[field] == pytest.approx(mean(spreads))
        assert a["calibration_std"] == pytest.approx(stdev(spreads))


@pytest.mark.parametrize("z", [2.0, -2.0, 8.0, -8.0])
def test_economic_legs_invariant(z):
    _, a, b = v1.signal_direction(z)
    original = {"symbol_a": "A", "symbol_b": "B", "entry_z": z, "direction_a": a, "direction_b": b}
    mirrored = audit.mirror(original)
    _, mirrored["direction_a"], mirrored["direction_b"] = v1.signal_direction(-z)
    assert audit.economic_legs(original) == audit.economic_legs(mirrored)


@pytest.mark.parametrize("max_hold", [False, True])
def test_exit_return_cost_mfe_mae_and_entry_invariant(sample, max_hold):
    days, bars, identities, candidate = sample
    if max_hold:
        for i in range(61, 66):
            bar = bars["B", days[i]]
            price = bar.close * math.exp(0.03)
            bars["A", days[i]] = v1.PairBar(price, price * 1.01, price * 0.99, price, 1_000_000)
    original, _ = v1.simulate_trade(
        candidate, bars, days, 60, days[-1], identity_by_symbol=identities
    )
    mirrored, _ = v1.simulate_trade(
        audit.mirror(candidate), bars, days, 60, days[-1], identity_by_symbol=identities
    )
    for field in (
        "exit_execution_session",
        "exit_trigger_session",
        "holding_sessions",
        "exit_reason",
        "entry_session",
    ):
        assert original[field] == mirrored[field]
    for field in (
        "gross_pair_return",
        "net_pair_return",
        "modeled_cost_drag",
        "pair_mfe",
        "pair_mae",
    ):
        assert original[field] == pytest.approx(mirrored[field], abs=audit.ABS_TOL)
    check = audit.Symmetry()
    audit.audit_trade(original, bars, days, identities, check)
    assert not check.violations
    assert original["exit_reason"] == ("MAX_HOLD_5" if max_hold else "MEAN_REVERSION")


def test_manifest_rejects_reverse_pair_as_independent_position(sample):
    days, bars, identities, candidate = sample
    names = [{"symbol": s, "sic2": "35", **identities[s]} for s in ("A", "B")]
    selected = {}
    for s in ("A", "B"):
        adv = sum(bars[s, d].close * bars[s, d].volume for d in days[41:61]) / 20
        selected[s] = {"symbol": s, "adv20": adv, "previous_close": bars[s, days[59]].close}
    ordered = sorted(selected.values(), key=lambda r: (-r["adv20"], r["symbol"]))
    membership = {
        k: candidate[k]
        for k in (
            "signal_session",
            "symbol_a",
            "symbol_b",
            "sic2",
            "calibration_mean",
            "calibration_std",
            "correlation",
            "calibration_status",
        )
    }
    membership.update(adv20_a=selected["A"]["adv20"], adv20_b=selected["B"]["adv20"])
    counts = {"selection_membership_resolved": True, "company_security_types_complete": True}
    payload = {
        "universe": names,
        "source_fingerprint": "fixture",
        "discovery_counts": counts,
        "selected": {str(days[60]): {"35": ordered}},
        "signal_sessions": [str(days[60])],
        "min_adv20": 0,
        "min_price": 0,
        "candidates": [membership],
    }
    verify_membership(payload, names, "fixture", bars, days, counts)
    assert audit.pair_key("A", "B") == audit.pair_key("B", "A")
    payload["candidates"].append(audit.mirror(membership))
    with pytest.raises(ValueError, match="pair membership/calibration"):
        verify_membership(payload, names, "fixture", bars, days, counts)


@pytest.mark.parametrize(
    "zs,cls,first",
    [
        ([-0.1, -0.2, -0.3, -0.4, -0.5], "ZERO_CROSSED", 1),
        ([2.0, 1.4, 0.8, 0.9, 0.7], "STRONG_CONVERGENCE", None),
        ([2.5, 2.3, 2.0, 1.8, 1.7], "PARTIAL_CONVERGENCE", None),
        ([3.0, 3.1, 3.2, 3.3, 3.4], "FURTHER_DIVERGENCE", None),
        ([2.5, 2.6, 2.4, 2.5, 2.6], "NO_CONVERGENCE", None),
    ],
)
def test_first_passage_and_diagnostic_only_classes(zs, cls, first):
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(len(zs))]
    result = audit.path_metrics(2.5, days, zs)
    assert result["path_class_5"] == cls
    assert result["first_zero_crossing_holding_session"] == first
    if cls == "STRONG_CONVERGENCE":
        assert result["first_holding_abs_z_le_1_0"] == 3
    if cls == "PARTIAL_CONVERGENCE":
        assert result["first_holding_abs_z_le_1_5"] is None


def test_complete_and_censored_10_15_horizons():
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(15)]
    complete = audit.path_metrics(2.5, days, [2 - 0.05 * i for i in range(15)])
    assert complete["availability_15"] == "OBSERVABLE"
    assert complete["abs_z_t_plus_10"] == pytest.approx(1.55)
    assert complete["session_of_min_abs_z_15"] == str(days[-1])
    missing = audit.path_metrics(2.5, days, [2.0] * 9 + [None] + [1.0] * 5)
    for h in (10, 15):
        assert missing[f"availability_{h}"] == "CENSORED_FORWARD_PATH"
        assert missing[f"z_t_plus_{h}"] is None
    summary = audit.summary_rows(
        [{"entry_abs_z": 2.5, **complete}, {"entry_abs_z": 2.5, **missing}]
    )
    assert summary[-1]["observable_denominator"] == 1
    assert summary[-1]["censored_count"] == 1
    tail = audit.path_metrics(2.5, days[:5], [2.0] * 5)
    assert tail["z_t_plus_10"] is None and tail["z_t_plus_15"] is None
    assert tail["availability_15"] == "CENSORED_FORWARD_PATH"


def test_entry_clock_and_fixed_reference(sample):
    days, bars, identities, candidate = sample
    trade, _ = v1.simulate_trade(candidate, bars, days, 60, days[-1])
    result = audit.forward_path(trade, bars, days, identities)
    assert result["horizon_session_1"] == trade["entry_session"]
    assert result["horizon_session_5"] == str(days[65])
    z = (
        math.log(bars["A", days[65]].close / bars["B", days[65]].close)
        - candidate["calibration_mean"]
    ) / candidate["calibration_std"]
    assert result["fixed_z_t_plus_5"] == pytest.approx(z)
    assert result["z_t_plus_5"] == pytest.approx(v1.observe(bars, days, 65, "A", "B")[1])


def test_forward_leakage_does_not_change_entry_or_call_selection(sample, monkeypatch):
    days, bars, identities, candidate = sample
    frozen = copy.deepcopy(candidate)
    before = v1.observe(bars, days, 60, "A", "B")
    trade, _ = v1.simulate_trade(candidate, bars, days, 60, days[-1])
    snapshot = copy.deepcopy(trade)

    def forbidden(*args, **kwargs):
        raise AssertionError("selection/execution called by post-trade audit")

    for target, name in (
        (data, "discover_pairs"),
        (data, "prepare_pairs_data"),
        (v1, "simulate_prepared_pairs"),
        (v1, "simulate_trade"),
        (v1, "signal_direction"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    audit.forward_path(trade, bars, days, identities)
    for day in days[61:]:
        bars["A", day] = v1.PairBar(500, 510, 490, 500, 1_000_000)
    audit.forward_path(trade, bars, days, identities)
    assert v1.observe(bars, days, 60, "A", "B") == before
    assert candidate == frozen and trade == snapshot
    assert asdict(v1.definitions.PAIRS_STAT_ARB_V1)["max_hold_sessions"] == 5


def test_offline_guard_and_read_only_database(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE probe (value TEXT)")
    with (
        audit.ReadOnlyBars(path).read_only() as c,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        c.execute("INSERT INTO probe VALUES ('mutated')")
    with audit.offline(), pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
        socket.create_connection(("example.invalid", 443))


def test_convergence_return_relationship_no_causal_claim():
    result = audit.relationship([(1, 0.01), (2, 0.02), (3, 0.03)])
    assert result["pearson"] == pytest.approx(1)
    assert result["spearman"] == pytest.approx(1)
    assert correlation([1, 2, 3], [0.01, 0.02, 0.03]) == pytest.approx(1)


def test_symmetry_violation_stops_before_forward_analysis(sample, tmp_path, monkeypatch):
    days, bars, identities, candidate = sample
    trade, _ = v1.simulate_trade(candidate, bars, days, 60, days[-1])
    original = {"research_id": "PAIRS-STAT-ARB-V1-DAILY", "metrics": {}}
    manifest = {
        "fingerprint": "fixture",
        "history_sessions": [str(d) for d in days],
        "universe": list(identities.values()),
    }
    # The IO boundary is stubbed, while the real symmetry/stop/report path runs.
    monkeypatch.setattr(
        audit, "frozen_inputs", lambda *_: (original, manifest, [candidate], [trade])
    )
    monkeypatch.setattr(audit, "load_daily", lambda *a, **kw: bars)
    monkeypatch.setattr(audit, "digest", lambda *_: "fixture_digest")

    def inject_violation(self, *args):
        self.boolean("z", False, {"reproducer": "injected mirror discrepancy"})

    def forbidden(*args, **kwargs):
        raise AssertionError("forward analysis must stop after symmetry failure")

    monkeypatch.setattr(audit.Symmetry, "check_observation", inject_violation)
    monkeypatch.setattr(audit, "forward_path", forbidden)
    report = audit.run(tmp_path, tmp_path / "manifest.json", tmp_path / "unused.db", "failure")
    assert report["symmetry"]["status"] == "SYMMETRY_FAIL"
    assert report["symmetry"]["z_violations"] == 1
    assert report["recommendation"] == "INVESTIGATE_IMPLEMENTATION_FIRST"
    assert report["stopped_before_path_analysis"]
    assert not list(tmp_path.glob("*mean_reversion_paths*"))
