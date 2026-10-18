"""Synthetic-only tests for the preregistered outcome-independent relationship study."""

import copy
import json
import socket
import sqlite3
from dataclasses import asdict
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from trading_system import cli
from trading_system.backtest import pairs_v2_feasibility as f
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.research_definitions import PAIRS_STAT_ARB_V1


def synthetic(seed=42, n=270, phi=0.85):
    rng = np.random.default_rng(seed)
    x = 4 + np.cumsum(rng.normal(0, 0.015, n))
    e = np.zeros(n)
    for i in range(1, n):
        e[i] = phi * e[i - 1] + rng.normal(0, 0.007)
    return 0.3 + 1.2 * x + e, x, e


def test_ols_and_stationary_residual():
    y, x, e = synthetic()
    result = f.evaluate_state(y, x, 252)
    assert result["fit_status"] == "FIT_OK"
    assert result["beta"] == pytest.approx(1.2, abs=0.07)
    assert result["alpha"] + result["beta"] * x[:252].mean() == pytest.approx(y[:252].mean())
    assert result["cointegration_p_value"] < 0.01
    assert result["adf_p_value"] < result["cointegration_p_value"]
    assert result["adf_test_stat"] == pytest.approx(result["cointegration_test_stat"], abs=1e-8)
    assert abs(result["residual_mean"]) < 1e-12
    assert result["residual_zero_crossings"] > 10
    assert result["residual_std"] > 0


def test_independent_random_walks_not_systematically_cointegrated():
    passes = 0
    for seed in range(40):
        rng = np.random.default_rng(seed)
        x, y = np.cumsum(rng.normal(size=(2, 252)), axis=1)
        result = f.relationship(y, x)
        assert result["fit_status"] == "FIT_OK"
        passes += result["cointegration_p_value"] <= 0.05
    # A finite synthetic false-positive-rate check, not a guarantee for each walk.
    assert passes <= 8


def test_lookahead_isolation_and_current_observation_separation():
    y, x, _ = synthetic()
    before = f.evaluate_state(y, x, 252)
    y[252:] += 10
    x[253:] -= 5
    after = f.evaluate_state(y, x, 252)
    observations = {"residual_current", "residual_z_t", "observation_status"}
    assert {k: v for k, v in before.items() if k not in observations} == {
        k: v for k, v in after.items() if k not in observations
    }
    assert before["residual_z_t"] != after["residual_z_t"]
    y[253:] += 100
    assert f.evaluate_state(y, x, 252) == after


def test_only_one_canonical_direction_and_contract(monkeypatch):
    y, x, _ = synthetic()
    actual_coint, adfuller = f.require_statistics()
    calls = []

    def spy(a, b, **kwargs):
        calls.append((a.copy(), b.copy(), kwargs))
        return actual_coint(a, b, **kwargs)

    monkeypatch.setattr(f, "require_statistics", lambda: (spy, adfuller))
    f.evaluate_state(y, x, 252)
    assert len(calls) == 3
    for a, b, kwargs in calls:
        assert kwargs == f.CONTRACT["cointegration"]
        assert len(a) in (252, 126)
        assert np.max(a) > np.max(b)  # Generated A~B, never the reverse regression.
    np.testing.assert_array_equal(calls[0][0], y[:252])
    np.testing.assert_array_equal(calls[0][1], x[:252])


def test_missing_history_and_constant_series():
    y, x, _ = synthetic()
    assert f.evaluate_state(y, x, 251)["fit_status"] == "INSUFFICIENT_RELATIONSHIP_HISTORY"
    result = f.relationship(np.ones(252), x[:252])
    assert result["fit_status"] == "NUMERICAL_FAILURE"
    assert result["fit_reason"] == "CONSTANT_OR_NEAR_CONSTANT_LOG_PRICE"


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_logs(bad):
    y, x, _ = synthetic()
    y[10] = bad
    assert f.relationship(y[:252], x[:252])["fit_reason"] == "NONFINITE_LOG_PRICE"


def test_perfect_relationship_is_not_silent_p_zero():
    _, x, _ = synthetic()
    result = f.relationship(0.3 + 1.2 * x[:252], x[:252])
    assert result["fit_status"] == "NUMERICAL_FAILURE"
    assert "cointegration_p_value" not in result


def test_half_life_exact_discrete_ar1_and_non_mean_reversion():
    _, _, e = synthetic(n=25000, phi=0.9)
    result = f.half_life(e)
    assert result["half_life"] == pytest.approx(np.log(0.5) / np.log(0.9), rel=0.15)
    assert f.half_life(np.exp(np.arange(252) * 0.01))["half_life_status"] == "HALF_LIFE_UNDEFINED"
    assert f.half_life(np.ones(252))["half_life_status"] == "HALF_LIFE_UNDEFINED"


def test_beta_stability_stable_and_broken():
    y, x, e = synthetic()
    stable = f.evaluate_state(y, x, 252)
    broken = y.copy()
    broken[126:] = 0.3 + 2.2 * x[126:] + e[126:]
    unstable = f.evaluate_state(broken, x, 252)
    assert stable["beta_relative_change"] < 0.1
    assert unstable["beta_abs_change"] > 0.8


def test_persistence_breaks_on_membership_gap_and_failure():
    assert f.run_lengths([1, 2, 3, 5, 6, 7, 8], [True, True, False, True, True, False, True]) == [
        2,
        2,
        1,
    ]
    assert f.run_lengths([1, 2], [False, False]) == []


def fixture_manifest(tmp_path, monkeypatch, pnl):
    m = {
        "signal_sessions": ["2024-01-02"],
        "strategy_definition": asdict(PAIRS_STAT_ARB_V1),
        "market_data_feed": "IEX",
        "market_data_adjustment": "all",
        "universe": [{"symbol": s, "sic2": "35"} for s in ("A", "B")],
        "selected": {"2024-01-02": {"35": [{"symbol": s} for s in ("A", "B")]}},
        "candidates": [
            {
                "symbol_a": "A",
                "symbol_b": "B",
                "signal_session": "2024-01-02",
                "sic2": "35",
                "correlation": 0.2,
                "v1_pnl": pnl,
            }
        ],
    }
    m["fingerprint"] = fingerprint(m)
    monkeypatch.setattr(f, "MANIFEST_FINGERPRINT", m["fingerprint"])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(m))
    return path


def test_no_pnl_dependency_and_no_v1_correlation_gate(tmp_path, monkeypatch):
    first = fixture_manifest(tmp_path, monkeypatch, 100000)
    outcomes = tmp_path / (f.V1_STEM + "_trades.csv")
    outcomes.write_text("gross_pair_return,net_pair_return\n100000,99999\n")
    a = f.load_frozen(first, date(2024, 1, 2), date(2024, 1, 2))[0]
    second = fixture_manifest(tmp_path, monkeypatch, -100000)
    outcomes.write_text("gross_pair_return,net_pair_return\n-100000,-100001\n")
    b = f.load_frozen(second, date(2024, 1, 2), date(2024, 1, 2))[0]
    assert a == b and len(a) == 1 and a[0]["correlation"] == 0.2
    assert "v1_pnl" not in a[0]


def test_no_network_and_cli_no_provider_or_database_construction(monkeypatch, tmp_path):
    with f.offline(), pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
        socket.create_connection(("example.invalid", 443))

    def forbidden(*args, **kwargs):
        raise AssertionError("provider/DB construction is forbidden")

    for name in ("AlpacaDataClient", "SecClient", "DataSynchronizer", "Database", "_synchronizer"):
        monkeypatch.setattr(cli, name, forbidden)
    captured = []

    def fake_run(*args, **kwargs):
        captured.append(kwargs)
        return dict(
            sample="DEVELOPMENT_SAMPLE",
            unique_pairs_tested=1,
            pair_sessions_tested=1,
            insufficient_history_count=0,
            numerical_failure_count=0,
            runtime_seconds=0,
            network_used=False,
            database_writes=False,
        )

    monkeypatch.setattr(f, "run", fake_run)
    assert (
        cli.main(
            [
                "research-pairs-v2-feasibility",
                "--start",
                "2024-01-02",
                "--end",
                "2026-08-12",
                "--output-stem",
                "fixture",
            ]
        )
        == 0
    )
    assert captured[0]["workers"] == 4
    assert (
        cli.main(
            [
                "research-pairs-v2-feasibility",
                "--start",
                "2024-01-02",
                "--end",
                "2026-08-13",
                "--output-stem",
                "forbidden",
            ]
        )
        == 2
    )
    assert len(captured) == 1


def test_read_only_batch_loading_and_price_reason_codes(tmp_path):
    path = tmp_path / "fixture.db"
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE bars(symbol,timeframe,timestamp,close)")
        c.executemany(
            "INSERT INTO bars VALUES(?,?,?,?)",
            [("A", "1d", str(d), v) for d, v in zip(days, [10, 0, -1, "inf", None], strict=True)],
        )
    identity = {"A": {"identity_status": "CURRENT_SECURITY_NO_IDENTITY_WARNING"}}
    before = path.read_bytes()
    logs, codes, counts = f.load_logs(path, ["A"], days, identity)
    assert codes["A"].tolist() == [0, 2, 2, 3, 3]
    assert counts["sql_query_count"] == 1 and counts["sql_statement_count"] == 2
    assert path.read_bytes() == before
    assert logs["A"][0] == pytest.approx(np.log(10))


def test_v1_definition_unchanged_and_artifact_hashing_only(tmp_path):
    before = copy.deepcopy(asdict(PAIRS_STAT_ARB_V1))
    original = tmp_path / "v1_trades.csv"
    original.write_text("pnl\n123\n")
    digest = f.sha256(original)
    y, x, _ = synthetic()
    f.evaluate_state(y, x, 252)
    assert asdict(PAIRS_STAT_ARB_V1) == before
    assert before["calibration_sessions"] == 60 and before["max_hold_sessions"] == 5
    assert before["min_correlation"] == 0.7 and before["entry_z"] == 2
    assert f.sha256(original) == digest


def test_relationship_stability_matrix_keeps_all_eight_patterns():
    categories = set()
    for full in (True, False):
        for first in (True, False):
            for second in (True, False):
                row = dict(
                    zip(
                        (
                            "cointegration_p_value",
                            "first_cointegration_p_value",
                            "second_cointegration_p_value",
                        ),
                        [0.01 if b else 0.8 for b in (full, first, second)],
                        strict=True,
                    )
                )
                categories.add(f.stability_category(row, 0.05))
    assert len(categories) == 8
    assert f.stability_category({"cointegration_p_value": 0.01}, 0.05) == "UNAVAILABLE"


def test_reused_adf_is_identical_to_established_library_test():
    from statsmodels.regression.linear_model import OLS

    _, adfuller = f.require_statistics()
    for seed in range(5):
        y, x, _ = synthetic(seed=seed)
        result = f.relationship(y[:252], x[:252], residual_adf=True)
        residual = OLS(y[:252], np.column_stack((x[:252], np.ones(252)))).fit().resid
        expected = adfuller(residual, **f.CONTRACT["residual_adf"])
        assert result["adf_test_stat"] == pytest.approx(expected[0], abs=1e-8)
        assert result["adf_p_value"] == pytest.approx(expected[1], abs=1e-10)


def test_identical_half_window_cache_preserves_every_state(monkeypatch):
    y, x, _ = synthetic(n=400)
    actual = f.relationship
    calls = []

    def spy(a, b, **kwargs):
        calls.append(len(a))
        return actual(a, b, **kwargs)

    monkeypatch.setattr(f, "relationship", spy)
    cache = {}
    f.evaluate_state(y, x, 252, half_cache=cache)
    cached = f.evaluate_state(y, x, 378, half_cache=cache)
    assert calls == [252, 126, 126, 252, 126]
    uncached = f.evaluate_state(y, x, 378)
    assert cached == uncached


def test_empty_valid_population_reports_availability():
    frame = pd.DataFrame(
        [
            {
                "pair": "A/B",
                "sic2": "35",
                "session": "2024-01-02",
                "session_index": 252,
                "year": "2024",
                "third": "THIRD_1",
                "fit_status": "INSUFFICIENT_RELATIONSHIP_HISTORY",
                "fit_reason": "MISSING_LOCAL_BAR",
            }
        ]
    )
    summary, tables = f.aggregate(frame)
    assert summary["pair_sessions_tested"] == 0
    assert summary["insufficient_history_count"] == 1
    assert summary["cointegration_p_le_05_share"] is None
    assert tables["half_life"][0]["valid_half_life_count"] == 0
