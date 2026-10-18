"""Outcome-independent DEVELOPMENT_SAMPLE relationship diagnostics, never a strategy."""

import hashlib
import json
import math
import os
import socket
import sqlite3
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import numpy as np
import pandas as pd

from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.pairs_stat_arb_v1_identity import USABLE, session_status
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_definitions import PAIRS_STAT_ARB_V1
from trading_system.data.market_sessions import trading_sessions_between

DEVELOPMENT_START = date(2024, 1, 2)
DEVELOPMENT_END = date(2026, 8, 12)
V1_STEM = "pairs_stat_arb_v1_2024-01-02_2026-08-12_v1"
MANIFEST_NAME = "pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_pair_candidates.json"
MANIFEST_FINGERPRINT = "b3576dcfea70d2e6a854bca7edf500e574724a8dacd2609183c041a9a55295c4"
WINDOW = 252
EPS = 1e-12
LEVELS = {"10": 0.10, "05": 0.05, "01": 0.01}
CONTRACT = {
    "sample": "DEVELOPMENT_SAMPLE",
    "window_sessions": WINDOW,
    "fit_data": "t-252 THROUGH t-1; CURRENT_CLOSE_ONLY_FOR_RESIDUAL_Z",
    "direction": "CANONICAL_A_LT_B_ONLY;OLS_LOG_A_ON_INTERCEPT_AND_LOG_B",
    "cointegration": {"method": "aeg", "trend": "c", "maxlag": 12, "autolag": "aic"},
    "residual_adf": {"regression": "n", "maxlag": 12, "autolag": "AIC"},
    "exact_computation_reuse": (
        "COINT_ADF_STAT_WITH_STATSMODELS_N1_PVALUE;IDENTICAL_HALF_WINDOW_CACHE"
    ),
    "half_window_sessions": 126,
    "residual_std_ddof": 1,
    "half_life": "OLS_DELTA_EPS_ON_CONSTANT_AND_LAG_EPS;LOG_0.5_DIV_LOG_PHI;0<PHI<1",
    "near_zero_lambda_tolerance": 1e-8,
    "variance_epsilon": EPS,
    "beta_change_denominator_epsilon": EPS,
    "maximum_design_condition": 1e12,
    "correlation_gate": None,
    "strategy_filters": None,
    "multiple_testing_adjustment": None,
}


def require_statistics():
    try:
        from statsmodels.tsa.stattools import adfuller, coint
    except ImportError as exc:
        raise RuntimeError(
            "STATS_DEPENDENCY_UNAVAILABLE: install project extra [research]"
        ) from exc
    return coint, adfuller


def half_life(residual):
    e = np.asarray(residual, dtype=float)
    result = {"half_life": None, "half_life_status": "HALF_LIFE_UNDEFINED", "ar1_lambda": None}
    if len(e) < 3 or not np.isfinite(e).all() or np.var(e[:-1]) <= EPS:
        return {**result, "half_life_reason": "INVALID_RESIDUAL_REGRESSION"}
    design = np.column_stack((np.ones(len(e) - 1), e[:-1]))
    coef = np.linalg.lstsq(design, np.diff(e), rcond=None)[0]
    lam = float(coef[1])
    result["ar1_lambda"] = lam if math.isfinite(lam) else None
    phi = 1 + lam
    if not math.isfinite(phi) or phi <= 0 or phi >= 1 or abs(lam) < 1e-8:
        return {**result, "half_life_reason": "NON_MONOTONE_OR_NON_MEAN_REVERTING_AR1"}
    value = math.log(0.5) / math.log(phi)
    return {**result, "half_life": value, "half_life_status": "VALID", "half_life_reason": None}


def relationship(y, x, *, residual_adf=False):
    """One direction/contract only. Halves are stability diagnostics, never alternatives."""
    y, x = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    result = {"fit_status": "NUMERICAL_FAILURE", "fit_reason": None}
    if len(y) not in (126, WINDOW) or len(y) != len(x):
        return {**result, "fit_reason": "INSUFFICIENT_TEST_OBSERVATIONS"}
    if not np.isfinite(y).all() or not np.isfinite(x).all():
        return {**result, "fit_reason": "NONFINITE_LOG_PRICE"}
    if min(np.var(x), np.var(y)) <= EPS:
        return {**result, "fit_reason": "CONSTANT_OR_NEAR_CONSTANT_LOG_PRICE"}
    design = np.column_stack((np.ones(len(x)), x))
    if np.linalg.cond(design) > 1e12:
        return {**result, "fit_reason": "SINGULAR_OR_ILL_CONDITIONED_REGRESSION"}
    try:
        alpha, beta = np.linalg.lstsq(design, y, rcond=None)[0]
        residual = y - alpha - beta * x
        if not np.isfinite(residual).all() or np.var(residual) <= EPS:
            return {**result, "fit_reason": "DEGENERATE_RESIDUAL_VARIANCE"}
        nonzero = residual[residual != 0]
        result.update(
            alpha=float(alpha),
            beta=float(beta),
            beta_finite=bool(np.isfinite(beta)),
            beta_positive=bool(beta > 0),
            beta_magnitude=float(abs(beta)),
            residual_mean=float(np.mean(residual)),
            residual_std=float(np.std(residual, ddof=1)),
            residual_min=float(np.min(residual)),
            residual_max=float(np.max(residual)),
            residual_zero_crossings=int(np.sum(nonzero[:-1] * nonzero[1:] < 0)),
            residual_lag1_autocorrelation=float(np.corrcoef(residual[:-1], residual[1:])[0, 1]),
            **half_life(residual),
        )
        coint, _adfuller = require_statistics()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            stat, p, crit = coint(y, x, **CONTRACT["cointegration"])
            if not np.isfinite([stat, p, *crit]).all() or not 0 <= p <= 1:
                return {**result, "fit_reason": "INVALID_COINTEGRATION_OUTPUT_OR_NEAR_COLLINEAR"}
            if caught:
                return {
                    **result,
                    "fit_reason": "COINTEGRATION_LIBRARY_WARNING",
                    "test_warning": ";".join(type(w.message).__name__ for w in caught),
                }
            result.update(cointegration_test_stat=float(stat), cointegration_p_value=float(p))
            if residual_adf:
                # coint already runs this exact ADF (regression='n', AIC, maxlag=12).
                # Only its MacKinnon p-value mapping differs: ordinary ADF N=1,
                # versus the estimated two-series EG relationship N=2, trend='c'.
                from statsmodels.tsa.adfvalues import mackinnonp

                adf_p = float(mackinnonp(stat, regression="n", N=1))
                if not math.isfinite(adf_p) or not 0 <= adf_p <= 1:
                    return {**result, "fit_reason": "INVALID_ADF_OUTPUT"}
                result.update(
                    adf_test_stat=float(stat),
                    adf_p_value=adf_p,
                )
        return {**result, "fit_status": "FIT_OK"}
    except (ValueError, FloatingPointError, np.linalg.LinAlgError, ZeroDivisionError) as exc:
        return {**result, "fit_reason": f"STATISTICAL_FIT_FAILED:{type(exc).__name__}"}


def evaluate_state(log_a, log_b, index, *, half_cache=None):
    """Fit state uses [t-252,t). Only residual_current/z observe t afterwards."""
    if index < WINDOW:
        return {
            "fit_status": "INSUFFICIENT_RELATIONSHIP_HISTORY",
            "fit_reason": "LESS_THAN_252_SESSIONS",
        }
    y, x = log_a[index - WINDOW : index], log_b[index - WINDOW : index]
    full = relationship(y, x, residual_adf=True)
    if full["fit_status"] != "FIT_OK":
        return full
    for prefix, begin, end in (
        ("first", index - WINDOW, index - 126),
        ("second", index - 126, index),
    ):
        key = begin, end
        if half_cache is not None and key in half_cache:
            half = half_cache[key]
        else:
            half = relationship(log_a[begin:end], log_b[begin:end])
            if half_cache is not None:
                half_cache[key] = half
        full.update(
            {
                f"{prefix}_{key}": half.get(key)
                for key in (
                    "alpha",
                    "beta",
                    "cointegration_test_stat",
                    "cointegration_p_value",
                    "fit_status",
                    "fit_reason",
                )
            }
        )
    if full["first_fit_status"] == full["second_fit_status"] == "FIT_OK":
        full["beta_abs_change"] = abs(full["second_beta"] - full["first_beta"])
        full["beta_relative_change"] = full["beta_abs_change"] / max(abs(full["beta"]), EPS)
        full["alpha_abs_change"] = abs(full["second_alpha"] - full["first_alpha"])
    full.update(
        residual_current=None, residual_z_t=None, observation_status="CURRENT_CLOSE_UNAVAILABLE"
    )
    if index < len(log_a) and np.isfinite([log_a[index], log_b[index]]).all():
        current = float(log_a[index] - full["alpha"] - full["beta"] * log_b[index])
        full.update(
            residual_current=current,
            residual_z_t=(current - full["residual_mean"]) / full["residual_std"],
            observation_status="OBSERVED_T_RELATIVE_TO_PAST_CALIBRATION",
        )
    for label, level in LEVELS.items():
        for prefix in ("full", "first", "second"):
            p = full.get(
                "cointegration_p_value" if prefix == "full" else f"{prefix}_cointegration_p_value"
            )
            full[f"{prefix}_window_cointegrated_p_le_{label}"] = (
                p <= level if p is not None else None
            )
    return full


def process_pair(task):
    a, b, sic2, log_a, log_b, codes_a, codes_b, states = task
    results = []
    half_cache = {}
    for session, index, corr, third in states:
        row = {
            "pair": f"{a}/{b}",
            "symbol_a": a,
            "symbol_b": b,
            "sic2": sic2,
            "session": session,
            "session_index": index,
            "year": session[:4],
            "third": third,
            "v1_return_correlation": corr,
        }
        codes = np.concatenate((codes_a[index - WINDOW : index], codes_b[index - WINDOW : index]))
        reasons = set(codes.tolist()) - {0}
        if index < WINDOW or 1 in reasons or 5 in reasons:
            row.update(
                fit_status="INSUFFICIENT_RELATIONSHIP_HISTORY",
                fit_reason="MISSING_LOCAL_BAR_OR_FROZEN_IDENTITY_WINDOW",
            )
        elif reasons:
            labels = {2: "NON_POSITIVE_PRICE", 3: "NONFINITE_PRICE", 4: "DUPLICATE_SESSION_BAR"}
            row.update(
                fit_status="NUMERICAL_FAILURE",
                fit_reason=";".join(labels[r] for r in sorted(reasons)),
            )
        else:
            row.update(evaluate_state(log_a, log_b, index, half_cache=half_cache))
        results.append(row)
    return results


def _forbidden(*args, **kwargs):
    raise RuntimeError("LOCAL_FEASIBILITY_NETWORK_FORBIDDEN")


@contextmanager
def offline():
    with (
        patch.object(socket.socket, "connect", _forbidden),
        patch.object(socket.socket, "connect_ex", _forbidden),
        patch.object(socket, "create_connection", _forbidden),
    ):
        yield


def worker_init():
    # Spawned workers inherit one-thread BLAS settings and need no database handle.
    socket.socket.connect = _forbidden
    socket.socket.connect_ex = _forbidden
    socket.create_connection = _forbidden
    require_statistics()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_period(start, end):
    if not DEVELOPMENT_START <= start <= end <= DEVELOPMENT_END:
        raise ValueError(
            "DEVELOPMENT_SAMPLE_ONLY: evaluations must stay within 2024-01-02..2026-08-12"
        )


def load_frozen(manifest_path, start, end):
    """No trade, signal, return, attribution or PnL file is a relationship input."""
    validate_period(start, end)
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if m["fingerprint"] != MANIFEST_FINGERPRINT or fingerprint(m) != m["fingerprint"]:
        raise ValueError("FROZEN_V1_MANIFEST_FINGERPRINT_MISMATCH")
    if m["strategy_definition"] != asdict(PAIRS_STAT_ARB_V1):
        raise ValueError("FROZEN_V1_DEFINITION_MISMATCH")
    days = m["signal_sessions"]
    thirds = {d: f"THIRD_{min(i * 3 // len(days), 2) + 1}" for i, d in enumerate(days)}
    identities = {r["symbol"]: r for r in m["universe"]}
    candidates, seen = [], set()
    for c in m["candidates"]:
        if not str(start) <= c["signal_session"] <= str(end):
            continue
        a, b, d = c["symbol_a"], c["symbol_b"], c["signal_session"]
        key = d, a, b
        if (
            a >= b
            or key in seen
            or identities[a]["sic2"] != identities[b]["sic2"]
            or c["sic2"] != identities[a]["sic2"]
        ):
            raise ValueError("NONCANONICAL_OR_DUPLICATE_FROZEN_MEMBERSHIP")
        selected = {r["symbol"] for r in m["selected"][d][c["sic2"]]}
        if not {a, b} <= selected:
            raise ValueError("FROZEN_SELECTION_MISMATCH")
        seen.add(key)
        candidates.append(
            {
                "symbol_a": a,
                "symbol_b": b,
                "sic2": c["sic2"],
                "session": d,
                "correlation": c.get("correlation"),
                "third": thirds[d],
            }
        )
    return (
        candidates,
        identities,
        {
            k: m[k]
            for k in (
                "fingerprint",
                "market_data_feed",
                "market_data_adjustment",
                "strategy_definition",
            )
        },
    )


def load_logs(database, symbols, sessions, identities):
    logs = {s: np.full(len(sessions), np.nan) for s in symbols}
    codes = {s: np.ones(len(sessions), dtype=np.int8) for s in symbols}
    indexes = {str(d): i for i, d in enumerate(sessions)}
    queries = Counter()

    def trace(statement):
        queries["sql_statement_count"] += 1
        if statement.lstrip().upper().startswith("SELECT"):
            queries["sql_query_count"] += 1

    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.set_trace_callback(trace)
    try:
        connection.execute("PRAGMA query_only=ON")
        for offset in range(0, len(symbols), 300):
            batch = symbols[offset : offset + 300]
            placeholders = ",".join("?" for _ in batch)
            cursor = connection.execute(
                f"SELECT symbol,timestamp,close FROM bars WHERE symbol IN ({placeholders}) "
                "AND timeframe='1d' AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
                [*batch, str(sessions[0]), str(sessions[-1] + timedelta(days=1))],
            )
            seen = set()
            for symbol, timestamp, raw in cursor:
                i = indexes.get(timestamp[:10])
                if i is None:
                    continue
                key = symbol, i
                if key in seen:
                    codes[symbol][i], logs[symbol][i] = 4, np.nan
                    continue
                seen.add(key)
                if session_status(identities[symbol], sessions[i]) not in USABLE:
                    codes[symbol][i] = 5
                    continue
                try:
                    price = float(raw)
                    if not math.isfinite(price):
                        codes[symbol][i] = 3
                    elif price <= 0:
                        codes[symbol][i] = 2
                    else:
                        logs[symbol][i], codes[symbol][i] = math.log(price), 0
                except (ValueError, TypeError, OverflowError):
                    codes[symbol][i] = 3
    finally:
        connection.close()
    return logs, codes, dict(queries)


def metrics(frame):
    valid = frame[frame.fit_status == "FIT_OK"]
    n = len(valid)
    result = {
        "pair_sessions_attempted": len(frame),
        "pair_sessions_tested": n,
        "unique_pairs_tested": int(valid.pair.nunique()),
        "insufficient_history_count": int(
            (frame.fit_status == "INSUFFICIENT_RELATIONSHIP_HISTORY").sum()
        ),
        "numerical_failure_count": int((frame.fit_status == "NUMERICAL_FAILURE").sum()),
    }
    for label, level in LEVELS.items():
        passing = valid[valid.cointegration_p_value <= level]
        result.update(
            {
                f"cointegration_p_le_{label}_count": len(passing),
                f"cointegration_p_le_{label}_share": len(passing) / n if n else None,
                f"pairs_ever_p_le_{label}": int(passing.pair.nunique()),
            }
        )
    for column in ("cointegration_p_value", "beta", "half_life"):
        values = valid[column].dropna()
        result["median_" + column] = float(values.median()) if len(values) else None
    return result


def distribution(values):
    values = pd.Series(values, dtype=float).dropna()
    return {
        "count": len(values),
        "median": float(values.median()) if len(values) else None,
        "p25": float(values.quantile(0.25)) if len(values) else None,
        "p75": float(values.quantile(0.75)) if len(values) else None,
    }


def beta_bucket(beta):
    if beta <= 0:
        return "LE_0"
    if beta < 0.25:
        return "GT_0_LT_0.25"
    if beta < 0.5:
        return "0.25_TO_0.5"
    if beta < 1:
        return "0.5_TO_1"
    if beta < 2:
        return "1_TO_2"
    return "GE_2"


def life_bucket(value):
    if pd.isna(value):
        return "UNDEFINED"
    for upper, label in (
        (2, "LE_2"),
        (5, "2_TO_5"),
        (10, "5_TO_10"),
        (20, "10_TO_20"),
        (60, "20_TO_60"),
    ):
        if value <= upper:
            return label
    return "GT_60"


def stability_category(row, level):
    p = [
        row.get(key)
        for key in (
            "cointegration_p_value",
            "first_cointegration_p_value",
            "second_cointegration_p_value",
        )
    ]
    if any(pd.isna(x) for x in p):
        return "UNAVAILABLE"
    flags = tuple(x <= level for x in p)
    return {
        (True, True, True): "FULL_AND_BOTH_HALVES",
        (True, True, False): "FULL_AND_FIRST_ONLY",
        (True, False, True): "FULL_AND_SECOND_ONLY",
        (True, False, False): "FULL_ONLY",
        (False, True, True): "FIRST_AND_SECOND_BUT_NOT_FULL",
        (False, True, False): "FIRST_ONLY",
        (False, False, True): "SECOND_ONLY",
        (False, False, False): "NONE",
    }[flags]


def run_lengths(indexes, passes):
    lengths, current, previous = [], 0, None
    for index, passing in zip(indexes, passes, strict=True):
        if current and (not passing or index != previous + 1):
            lengths.append(current)
            current = 0
        if passing:
            current += 1
        previous = index
    if current:
        lengths.append(current)
    return lengths


def aggregate(frame):
    # Explicit columns keep empty/failed populations observable, never implicit successes.
    for key in (
        "cointegration_p_value",
        "first_cointegration_p_value",
        "second_cointegration_p_value",
        "beta",
        "half_life",
        "beta_abs_change",
        "beta_relative_change",
        "residual_z_t",
    ):
        if key not in frame:
            frame[key] = np.nan
    valid = frame[frame.fit_status == "FIT_OK"].copy()
    pairs = []
    for pair, members in frame.groupby("pair", sort=True):
        members = members.sort_values("session_index")
        good = members[members.fit_status == "FIT_OK"]
        passing = (members.fit_status == "FIT_OK") & (members.cointegration_p_value <= 0.05)
        runs = run_lengths(members.session_index.tolist(), passing.tolist())
        item = {
            "pair": pair,
            "sic2": members.sic2.iloc[0],
            **metrics(members),
            "share_p_le_05": float((good.cointegration_p_value <= 0.05).mean())
            if len(good)
            else None,
            "longest_consecutive_p_le_05_run": max(runs, default=0),
            "median_consecutive_p_le_05_run": float(np.median(runs)) if runs else 0,
            "cointegrated_run_count": len(runs),
        }
        pairs.append(item)
    pair_frame = pd.DataFrame(pairs)
    tested_pairs = (
        pair_frame[pair_frame.pair_sessions_tested > 0] if len(pair_frame) else pair_frame
    )
    persistence = (
        {
            f"pairs_p_le_05_at_least_{n}pct": int((tested_pairs.share_p_le_05 >= n / 100).sum())
            for n in (25, 50, 75)
        }
        if len(tested_pairs)
        else {f"pairs_p_le_05_at_least_{n}pct": 0 for n in (25, 50, 75)}
    )
    persistence.update(
        median_pair_persistence=float(tested_pairs.share_p_le_05.median())
        if len(tested_pairs)
        else None,
        median_longest_cointegrated_run=float(tested_pairs.longest_consecutive_p_le_05_run.median())
        if len(tested_pairs)
        else None,
    )
    beta_rows, life_rows, opportunities, matrices = [], [], [], []
    scopes = [("ALL_TESTED", valid)] + [
        (f"P_LE_{label}", valid[valid.cointegration_p_value <= level])
        for label, level in LEVELS.items()
    ]
    stable = valid[
        (valid.cointegration_p_value <= 0.05)
        & (valid.first_cointegration_p_value <= 0.05)
        & (valid.second_cointegration_p_value <= 0.05)
    ]
    scopes.append(("FULL_AND_BOTH_HALVES_P_LE_05", stable))
    for scope, members in scopes:
        changes = members.beta_relative_change.dropna()
        b = {
            "scope": scope,
            "pair_sessions": len(members),
            "unique_pairs": int(members.pair.nunique()),
            "median_beta": distribution(members.beta)["median"],
            "median_abs_beta_change": distribution(members.beta_abs_change)["median"],
            "median_relative_beta_change": distribution(changes)["median"],
            "stability_observable_denominator": len(changes),
        }
        for n in (10, 25, 50):
            b[f"relative_beta_change_le_{n}pct_share"] = (
                float((changes <= n / 100).mean()) if len(changes) else None
            )
        b["relative_beta_change_gt_50pct_share"] = (
            float((changes > 0.5).mean()) if len(changes) else None
        )
        b.update(
            {
                f"beta_{key}_count": value
                for key, value in Counter(map(beta_bucket, members.beta)).items()
            }
        )
        beta_rows.append(b)
        life = distribution(members.half_life)
        life_rows.append(
            {
                "scope": scope,
                "pair_sessions": len(members),
                "valid_half_life_count": life["count"],
                "undefined_half_life_count": int(members.half_life.isna().sum()),
                "median_half_life": life["median"],
                "p25_half_life": life["p25"],
                "p75_half_life": life["p75"],
                **{
                    f"bucket_{k}": v
                    for k, v in Counter(map(life_bucket, members.half_life)).items()
                },
            }
        )
        z = members.residual_z_t.dropna()
        o = {
            "scope": scope,
            "pair_sessions": len(members),
            "observable_current_z": len(z),
            "missing_current_z": len(members) - len(z),
            "unique_pairs": int(members.pair.nunique()),
        }
        for level, label in ((1.5, "1_5"), (2, "2_0"), (2.5, "2_5"), (3, "3_0")):
            mask = members.residual_z_t.abs() >= level
            o[f"states_abs_z_ge_{label}"] = int(mask.sum())
            o[f"share_abs_z_ge_{label}"] = float((z.abs() >= level).mean()) if len(z) else None
            o[f"unique_pairs_abs_z_ge_{label}"] = int(members[mask].pair.nunique())
        opportunities.append(o)
    for _label, level in LEVELS.items():
        counts = Counter(stability_category(row, level) for row in valid.to_dict("records"))
        for category, count in sorted(counts.items()):
            matrices.append(
                {
                    "p_level": level,
                    "category": category,
                    "count": count,
                    "share": count / len(valid) if len(valid) else None,
                }
            )
    tables = {
        "pair_summary": pairs,
        "cointegration_persistence": pairs,
        "beta_stability": beta_rows,
        "half_life": life_rows,
        "residual_z_opportunities": opportunities,
        "relationship_stability": matrices,
    }
    for name, column in (
        ("session_statistics", "session"),
        ("sic2_attribution", "sic2"),
        ("yearly", "year"),
        ("chronological_thirds", "third"),
    ):
        tables[name] = [
            {column: str(label), **metrics(group)}
            for label, group in frame.groupby(column, sort=True)
        ]
    summary = {
        **metrics(frame),
        **persistence,
        "half_life": life_rows,
        "beta_stability": beta_rows,
        "relationship_stability": matrices,
        "residual_z_opportunities": opportunities,
        "failure_reasons": dict(Counter(frame.loc[frame.fit_status != "FIT_OK", "fit_reason"])),
        "half_window_failure_count": int(
            (
                valid.first_cointegration_p_value.isna() | valid.second_cointegration_p_value.isna()
            ).sum()
        ),
    }
    return summary, tables


def clean_records(rows):
    # pandas uses NaN for unavailable fields; JSON/CSV preserve null/empty explicitly.
    return [
        {
            k: None if isinstance(v, (float, np.floating)) and not math.isfinite(v) else v
            for k, v in row.items()
        }
        for row in rows
    ]


def export_table(path, rows):
    rows = clean_records(rows)
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["status"]
    _atomic_csv(path, rows, fields)


def run(database, reports, start, end, *, stem, manifest_path=None, workers=1, progress=print):
    validate_period(start, end)
    if Path(stem).name != stem or stem.startswith(V1_STEM) or not stem:
        raise ValueError("OUTPUT_STEM_MUST_BE_A_NEW_V2_BASENAME")
    reports = Path(reports)
    if list(reports.glob(stem + "_*")):
        raise FileExistsError("V2_FEASIBILITY_OUTPUT_EXISTS: use a new output stem")
    started = perf_counter()
    require_statistics()
    manifest_path = Path(manifest_path or reports / MANIFEST_NAME)
    sources = sorted(set(reports.glob(V1_STEM + "_*")) | {manifest_path})
    hashes = {str(p): sha256(p) for p in sources if p.is_file()}
    db_stat = Path(database).stat()
    candidates, identities, metadata = load_frozen(manifest_path, start, end)
    if not candidates:
        raise ValueError("NO_FROZEN_PAIR_CANDIDATES_IN_DEVELOPMENT_RANGE")
    days = trading_sessions_between(start, end)
    prior = trading_sessions_between(days[0] - timedelta(days=800), days[0])[:-1][-WINDOW:]
    if len(prior) != WINDOW:
        raise ValueError("252_OFFICIAL_SESSION_WINDOW_UNAVAILABLE_STOP")
    sessions = prior + days
    indexes = {str(d): i for i, d in enumerate(sessions)}
    symbols = sorted({s for c in candidates for s in (c["symbol_a"], c["symbol_b"])})
    progress(f"Batch-loading local 252-session histories for {len(symbols)} frozen symbols")
    logs, codes, sql = load_logs(database, symbols, sessions, identities)
    source_digest = hashlib.sha256()
    for symbol in symbols:
        source_digest.update(symbol.encode())
        source_digest.update(logs[symbol].tobytes())
        source_digest.update(codes[symbol].tobytes())
    grouped = defaultdict(list)
    for c in candidates:
        grouped[c["symbol_a"], c["symbol_b"], c["sic2"]].append(
            (c["session"], indexes[c["session"]], c["correlation"], c["third"])
        )
    tasks = [
        (a, b, sic2, logs[a], logs[b], codes[a], codes[b], sorted(states))
        for (a, b, sic2), states in sorted(grouped.items())
    ]
    del candidates
    rows = []
    work_started = perf_counter()
    # Numerical-library threading is an execution resource control, never model tuning.
    with patch.dict(
        os.environ,
        {
            k: "1"
            for k in (
                "OPENBLAS_NUM_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
    ):
        if workers > 1:
            with ProcessPoolExecutor(max_workers=workers, initializer=worker_init) as pool:
                for i, result in enumerate(pool.map(process_pair, tasks, chunksize=1), 1):
                    rows.extend(result)
                    if i % 50 == 0:
                        progress(
                            f"Relationship pairs completed {i}/{len(tasks)}; states {len(rows)}"
                        )
        else:
            for i, task in enumerate(tasks, 1):
                rows.extend(process_pair(task))
                if i % 50 == 0:
                    progress(f"Relationship pairs completed {i}/{len(tasks)}; states {len(rows)}")
    fit_seconds = perf_counter() - work_started
    frame = pd.DataFrame(rows)
    del rows
    summary, tables = aggregate(frame)
    reports.mkdir(parents=True, exist_ok=True)
    paths = {name: reports / f"{stem}_{name}.csv" for name in tables}
    for name, members in tables.items():
        export_table(paths[name], members)
    detail = reports / f"{stem}_pair_session_details.csv.gz"
    frame.sort_values(["session", "symbol_a", "symbol_b"]).to_csv(
        detail, index=False, compression={"method": "gzip", "compresslevel": 5}
    )
    paths["pair_session_details"] = detail
    if hashes != {p: sha256(p) for p in hashes}:
        raise ValueError("FROZEN_V1_SOURCE_ARTIFACT_MUTATED")
    now = Path(database).stat()
    if (db_stat.st_size, db_stat.st_mtime_ns) != (now.st_size, now.st_mtime_ns):
        raise ValueError("LOCAL_DATABASE_CHANGED_DURING_FEASIBILITY")
    summary.update(
        research_id="PAIRS_V2_FEASIBILITY",
        sample="DEVELOPMENT_SAMPLE",
        requested_start=str(start),
        requested_end=str(end),
        reserved_forward_validation_start="2026-08-13",
        contract=CONTRACT,
        contract_sha256=hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest(),
        canonical_direction_only=True,
        return_correlation_gate_used=False,
        pnl_inputs_used=False,
        network_used=False,
        database_writes=False,
        trading_simulation=False,
        V1_unchanged=True,
        all_relationship_fits_use_only_data_through="t-1",
        frozen_manifest=metadata,
        source_artifact_sha256=hashes,
        local_log_input_sha256=source_digest.hexdigest(),
        local_history_start=str(sessions[0]),
        last_local_bar_read=str(sessions[-1]),
        source_artifacts_unchanged=True,
        database_size_and_mtime_unchanged=True,
        **sql,
        worker_processes=workers,
        relationship_runtime_seconds=fit_seconds,
        runtime_seconds=perf_counter() - started,
        versions={name: version(name) for name in ("statsmodels", "scipy", "numpy", "pandas")},
        artifacts={k: str(v) for k, v in paths.items()},
        limitations=[
            "DEVELOPMENT_SAMPLE_NOT_HOLDOUT",
            "OVERLAPPING_WINDOWS_AND_DEPENDENT_PAIRS",
            "RAW_UNADJUSTED_MULTIPLE_TEST_P_VALUES",
            "I1_LOG_PRICE_ASSUMPTION_NOT_ESTABLISHED_BY_EG_REJECTION_ALONE",
            "CURRENT_UNIVERSE_AND_SIC_NOT_SURVIVORSHIP_OR_PIT_CLEAN",
            "CONFIG_BOUND_DAILY_NO_PER_BAR_PROVENANCE",
            "CURRENT_REFERENCE_AND_QUALIFICATION_SELECTION_BIASES_RETAINED",
            "HALF_WINDOW_TESTS_HAVE_LOWER_POWER;NO_REJECTION_IS_NOT_PROOF_OF_NO_RELATIONSHIP",
        ],
    )
    _atomic_text(reports / f"{stem}_summary.json", json.dumps(summary, indent=2, allow_nan=False))
    return summary
