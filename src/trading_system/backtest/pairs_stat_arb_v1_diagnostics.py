"""Post-trade, local-only V1 audit. Never discovers, selects or executes trades.

Run explicitly with ``python -m trading_system.backtest.pairs_stat_arb_v1_diagnostics``.
H horizons count completed sessions from next-open entry: H1 is entry-day close,
H5 is V1's fifth holding close. The persisted signal-close z is ``entry_z``.
"""

import argparse
import csv
import hashlib
import json
import math
import socket
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from statistics import covariance, mean, median, variance
from unittest.mock import patch

from trading_system.backtest import pairs_stat_arb_v1 as v1
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.pairs_stat_arb_v1_data import load_daily
from trading_system.backtest.pairs_stat_arb_v1_identity import USABLE, window_status
from trading_system.backtest.pairs_stat_arb_v1_research import relationship
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.data.market_sessions import trading_sessions_between

HORIZONS = (1, 2, 3, 5, 10, 15)
ABS_TOL = 1e-10
REL_TOL = 1e-9
STEM = "pairs_stat_arb_v1_2024-01-02_2026-08-12_v1"
NUMERIC = {
    "adv20_a",
    "adv20_b",
    "calibration_mean",
    "calibration_std",
    "correlation",
    "spread_t",
    "entry_z",
    *v1.TRADE_FIELDS,
    "long_weight",
    "short_weight",
    "gross_notional",
    "net_notional",
} - {"entry_session", "exit_trigger_session", "exit_execution_session", "exit_reason"}


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return [
            {k: (float(v) if k in NUMERIC and v else v or None) for k, v in row.items()}
            for row in csv.DictReader(stream)
        ]


def pair_key(a, b):
    """Diagnostic identity only; production enforces this through its manifest."""
    if a == b:
        raise ValueError("self pair")
    return tuple(sorted((a, b)))


def close(x, y):
    return (
        x is y if x is None or y is None else math.isclose(x, y, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    )


def mirror(row):
    result = dict(row)
    for key in row:
        if key.endswith("_a") and key[:-2] + "_b" in row:
            other = key[:-2] + "_b"
            result[key], result[other] = row[other], row[key]
    for key in ("calibration_mean", "spread_t", "entry_z"):
        if row.get(key) is not None:
            result[key] = -row[key]
    return result


def economic_legs(row):
    return {row[f"direction_{leg}"]: row[f"symbol_{leg}"] for leg in ("a", "b")}


def observation(bars, sessions, index, a, b, identities):
    stats = v1.calibration(bars, sessions, index, a, b, identity_by_symbol=identities)
    ba, bb = bars.get((a, sessions[index])), bars.get((b, sessions[index]))
    if ba is not None and bb is not None and stats.get("calibration_std"):
        spread = math.log(ba.close / bb.close)
        stats.update(
            spread_t=spread, entry_z=(spread - stats["calibration_mean"]) / stats["calibration_std"]
        )
    return stats


def exit_signature(entry_z, zs):
    """Mathematical check of the existing schedule, no new executed trade."""
    for holding, z in enumerate(zs[:4], 1):
        if z is None:
            raise ValueError("actual holding path unavailable")
        if v1.crossing(entry_z, z):
            return "MEAN_REVERSION", holding, holding + 1, "open"
    return "MAX_HOLD_5", 5, 5, "close"


def path_class(entry_z, zs):
    """Diagnostic-only: partial >=20% contraction, divergence >=20% expansion."""
    if not zs or any(z is None for z in zs):
        return "CENSORED_FORWARD_PATH"
    if any(v1.crossing(entry_z, z) for z in zs):
        return "ZERO_CROSSED"
    if min(map(abs, zs)) <= 1:
        return "STRONG_CONVERGENCE"
    if min(map(abs, zs)) <= 0.8 * abs(entry_z):
        return "PARTIAL_CONVERGENCE"
    if min(map(abs, zs)) >= abs(entry_z) and abs(zs[-1]) >= 1.2 * abs(entry_z):
        return "FURTHER_DIVERGENCE"
    return "NO_CONVERGENCE"


def path_metrics(entry_z, sessions, zs):
    result = {}
    for threshold, label in ((1.5, "1_5"), (1.0, "1_0"), (0.5, "0_5")):
        hit = next((i for i, z in enumerate(zs) if z is not None and abs(z) <= threshold), None)
        result[f"first_session_abs_z_le_{label}"] = str(sessions[hit]) if hit is not None else None
        result[f"first_holding_abs_z_le_{label}"] = hit + 1 if hit is not None else None
    hit = next((i for i, z in enumerate(zs) if z is not None and v1.crossing(entry_z, z)), None)
    result["first_zero_crossing_session"] = str(sessions[hit]) if hit is not None else None
    result["first_zero_crossing_holding_session"] = hit + 1 if hit is not None else None
    for h in HORIZONS:
        observed = len(zs) >= h and all(z is not None for z in zs[:h])
        z = zs[h - 1] if observed else None
        result.update(
            {
                f"z_t_plus_{h}": z,
                f"abs_z_t_plus_{h}": abs(z) if z is not None else None,
                f"convergence_ratio_{h}": abs(z / entry_z) if z is not None else None,
                f"availability_{h}": "OBSERVABLE" if observed else "CENSORED_FORWARD_PATH",
            }
        )
        if h in (5, 10, 15):
            m = min(range(h), key=lambda i: abs(zs[i])) if observed else None
            result[f"min_abs_z_{h}"] = abs(zs[m]) if m is not None else None
            result[f"session_of_min_abs_z_{h}"] = str(sessions[m]) if m is not None else None
            result[f"path_class_{h}"] = (
                path_class(entry_z, zs[:h]) if observed else "CENSORED_FORWARD_PATH"
            )
            result[f"converged_then_rediverged_{h}"] = (
                min(map(abs, zs[:h])) <= 0.8 * abs(entry_z) and abs(zs[h - 1]) >= abs(entry_z)
                if observed
                else None
            )
    return result


def forward_path(trade, bars, sessions, identities):
    """Read a frozen trade only. No selection, qualification or entry callable."""
    entry = date.fromisoformat(trade["entry_session"])
    start = sessions.index(entry)
    days = sessions[start : start + 15]
    zs, fixed, returns = [], [], {}
    a, b = trade["symbol_a"], trade["symbol_b"]
    for holding, day in enumerate(days, 1):
        state = window_status(
            identities, (a, b), [date.fromisoformat(trade["signal_session"]), day]
        )
        obs = v1.observe(bars, sessions, start + holding - 1, a, b, identity_by_symbol=identities)
        ba, bb = bars.get((a, day)), bars.get((b, day))
        usable = state in USABLE and ba is not None and bb is not None
        zs.append(obs[1] if usable and obs is not None else None)
        fixed.append(
            (math.log(ba.close / bb.close) - trade["calibration_mean"]) / trade["calibration_std"]
            if usable
            else None
        )
        if holding in HORIZONS:
            returns[holding] = (
                sum(
                    v1.leg_weight(trade[f"direction_{leg}"])
                    * (bar.close / trade[f"entry_open_{leg}"] - 1)
                    * (1 if trade[f"direction_{leg}"] == "LONG" else -1)
                    for leg, bar in (("a", ba), ("b", bb))
                )
                if usable
                else None
            )
    result = path_metrics(trade["entry_z"], days, zs)
    result.update({f"fixed_{k}": v for k, v in path_metrics(trade["entry_z"], days, fixed).items()})
    for h in HORIZONS:
        result[f"horizon_session_{h}"] = str(days[h - 1]) if len(days) >= h else None
        result[f"forward_gross_pair_return_{h}"] = (
            returns.get(h) if result[f"availability_{h}"] == "OBSERVABLE" else None
        )
    return result


class Symmetry:
    def __init__(self):
        self.counts = Counter()
        self.errors = defaultdict(float)
        self.rows = {}
        self.violations = []

    def check(self, kind, original, mirrored, context):
        error = abs(original - mirrored) if original is not None and mirrored is not None else 0
        self.errors[kind] = max(self.errors[kind], error)
        if not close(original, mirrored):
            self.counts[f"{kind}_violations"] += 1
            self.violations.append(
                {
                    "status": "SYMMETRY_VIOLATION",
                    "kind": kind,
                    "original": original,
                    "mirrored_expected": mirrored,
                    **context,
                }
            )
        return error

    def boolean(self, kind, valid, context):
        if not valid:
            self.counts[f"{kind}_violations"] += 1
            self.violations.append({"status": "SYMMETRY_VIOLATION", "kind": kind, **context})

    def check_observation(self, candidate, original, mirrored):
        a, b = candidate["symbol_a"], candidate["symbol_b"]
        key = pair_key(a, b)
        row = self.rows.setdefault(
            key,
            {
                "record_kind": "PAIR_OBSERVATIONS",
                "symbol_a": a,
                "symbol_b": b,
                "observations_checked": 0,
                "max_numeric_error": 0.0,
            },
        )
        row["observations_checked"] += 1
        context = {
            "signal_session": candidate["signal_session"],
            "symbol_a": a,
            "symbol_b": b,
            "original_state": json.dumps(original),
            "mirrored_state": json.dumps(mirrored),
        }
        for kind, field, sign in (
            ("spread", "spread_t", -1),
            ("mean", "calibration_mean", -1),
            ("std", "calibration_std", 1),
            ("z", "entry_z", -1),
            ("correlation", "correlation", 1),
        ):
            x, y = original.get(field), mirrored.get(field)
            error = self.check(kind, x, sign * y if y is not None else None, context)
            row["max_numeric_error"] = max(row["max_numeric_error"], error)
        for field in ("calibration_mean", "calibration_std", "correlation"):
            if not close(candidate.get(field), original.get(field)):
                raise ValueError(f"FROZEN_INPUT_MISMATCH: {context} {field}")
        for field in ("entry_z", "spread_t"):
            if candidate.get(field) is not None and not close(
                candidate[field], original.get(field)
            ):
                raise ValueError(f"FROZEN_SIGNAL_INPUT_MISMATCH: {context} {field}")


def audit_trade(trade, bars, sessions, identities, audit):
    """Evaluate representation identities using fixed original trades and fills."""
    mirrored = mirror(trade)
    _, mirrored["direction_a"], mirrored["direction_b"] = v1.signal_direction(mirrored["entry_z"])
    context = {
        "signal_session": trade["signal_session"],
        "symbol_a": trade["symbol_a"],
        "symbol_b": trade["symbol_b"],
        "original_trade": json.dumps(trade),
        "mirrored_trade": json.dumps(mirrored),
    }
    audit.boolean("leg", economic_legs(trade) == economic_legs(mirrored), context)
    start = sessions.index(date.fromisoformat(trade["entry_session"]))
    original_zs, mirrored_zs = [], []
    for index in range(start, start + 5):
        x = v1.observe(
            bars,
            sessions,
            index,
            trade["symbol_a"],
            trade["symbol_b"],
            identity_by_symbol=identities,
        )
        y = v1.observe(
            bars,
            sessions,
            index,
            mirrored["symbol_a"],
            mirrored["symbol_b"],
            identity_by_symbol=identities,
        )
        original_zs.append(x[1] if x else None)
        mirrored_zs.append(y[1] if y else None)
        audit.check("z", x[1] if x else None, -y[1] if y else None, context)
    sig, msig = (
        exit_signature(trade["entry_z"], original_zs),
        exit_signature(mirrored["entry_z"], mirrored_zs),
    )
    reason, trigger_h, exit_h, reference = sig
    audit.boolean(
        "exit",
        sig == msig
        and reason == trade["exit_reason"]
        and exit_h == trade["holding_sessions"]
        and str(sessions[start + exit_h - 1]) == trade["exit_execution_session"]
        and str(sessions[start + trigger_h - 1]) == trade["exit_trigger_session"],
        context,
    )
    for row in (trade, mirrored):
        calculated = dict(row)
        path = []
        for holding in range(1, trigger_h + 1):
            path.append(
                sum(
                    v1.leg_weight(row[f"direction_{leg}"])
                    * (
                        bars[row[f"symbol_{leg}"], sessions[start + holding - 1]].close
                        / row[f"entry_fill_{leg}"]
                        - 1
                    )
                    * (1 if row[f"direction_{leg}"] == "LONG" else -1)
                    for leg in ("a", "b")
                )
            )
        v1.trade_returns(calculated)
        calculated.update(pair_mfe=max(path), pair_mae=min(path))
        for field in (
            "gross_pair_return",
            "net_pair_return",
            "modeled_cost_drag",
            "pair_mfe",
            "pair_mae",
        ):
            audit.check("return", trade[field], calculated[field], {**context, "field": field})
        for leg in ("a", "b"):
            symbol = row[f"symbol_{leg}"]
            entry_open = bars[symbol, sessions[start]].open
            exit_ref = getattr(bars[symbol, sessions[start + exit_h - 1]], reference)
            for field, actual in (
                ("entry_open", entry_open),
                ("exit_reference", exit_ref),
                ("entry_fill", v1.fill(entry_open, row[f"direction_{leg}"], entry=True)),
                ("exit_fill", v1.fill(exit_ref, row[f"direction_{leg}"], entry=False)),
            ):
                audit.check(
                    "entry" if field.startswith("entry") else "return",
                    row[f"{field}_{leg}"],
                    actual,
                    {**context, "field": f"{field}_{leg}"},
                )
    return {
        "record_kind": "TRADE",
        "symbol_a": trade["symbol_a"],
        "symbol_b": trade["symbol_b"],
        "signal_session": trade["signal_session"],
        "entry_session": trade["entry_session"],
        "exit_session": trade["exit_execution_session"],
        "original_z": trade["entry_z"],
        "mirrored_z": mirrored["entry_z"],
        "long_symbol": economic_legs(trade)["LONG"],
        "short_symbol": economic_legs(trade)["SHORT"],
        "status": "CHECKED",
    }


def bucket_z(row):
    z = abs(row["entry_z"])
    return "2.0_TO_2.5" if z < 2.5 else "2.5_TO_3.0" if z < 3 else "GE_3.0"


def bucket_corr(row):
    c = row["entry_correlation"]
    return "0.70_TO_0.80" if c < 0.8 else "0.80_TO_0.90" if c < 0.9 else "0.90_TO_1.00"


def summary_rows(rows, group="ALL", prefix=""):
    result = []
    for h in HORIZONS:
        observed = [r for r in rows if r[f"{prefix}availability_{h}"] == "OBSERVABLE"]
        n = len(observed)
        item = {
            "group": group,
            "path_semantics": "FIXED_ENTRY_CALIBRATION_Z" if prefix else "V1_ROLLING_Z",
            "horizon": h,
            "trade_count": len(rows),
            "observable_denominator": n,
            "censored_count": len(rows) - n,
            "median_entry_abs_z": median([r["entry_abs_z"] for r in rows]) if rows else None,
        }
        for field in (f"abs_z_t_plus_{h}", f"convergence_ratio_{h}"):
            item[f"median_{field}"] = median([r[prefix + field] for r in observed]) if n else None
        for label in ("1_5", "1_0", "0_5", "zero"):
            field = (
                "first_zero_crossing_holding_session"
                if label == "zero"
                else f"first_holding_abs_z_le_{label}"
            )
            hits = [
                r[prefix + field]
                for r in observed
                if r[prefix + field] is not None and r[prefix + field] <= h
            ]
            item[f"abs_z_le_{label}_count" if label != "zero" else "zero_cross_count"] = len(hits)
            item[f"abs_z_le_{label}_fraction" if label != "zero" else "zero_cross_fraction"] = (
                len(hits) / n if n else None
            )
            item[f"median_holding_to_{label}_among_hits"] = median(hits) if hits else None
        item["spread_convergence_fraction"] = (
            sum(r[f"{prefix}convergence_ratio_{h}"] < 1 for r in observed) / n if n else None
        )
        if h in (5, 10, 15):
            item["median_min_abs_z"] = (
                median([r[f"{prefix}min_abs_z_{h}"] for r in observed]) if n else None
            )
            for cls in (
                "ZERO_CROSSED",
                "STRONG_CONVERGENCE",
                "PARTIAL_CONVERGENCE",
                "NO_CONVERGENCE",
                "FURTHER_DIVERGENCE",
            ):
                item[f"class_{cls}_count"] = sum(
                    r[f"{prefix}path_class_{h}"] == cls for r in observed
                )
            item["converged_then_rediverged_count"] = sum(
                r[f"{prefix}converged_then_rediverged_{h}"] for r in observed
            )
        result.append(item)
    return result


def grouped_summary(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return [
        item
        for label, members in sorted(groups.items())
        for prefix in ("", "fixed_")
        for item in summary_rows(members, label, prefix)
    ]


def beta_difference(trade, bars, sessions):
    i = sessions.index(date.fromisoformat(trade["signal_session"]))
    days = sessions[i - 60 : i]
    series = []
    for symbol in (economic_legs(trade)["LONG"], economic_legs(trade)["SHORT"], "SPY"):
        prices = [bars.get((symbol, d)) for d in days]
        if any(b is None for b in prices):
            return None
        series.append(
            [math.log(y.close / x.close) for x, y in zip(prices, prices[1:], strict=False)]
        )
    market_var = variance(series[2])
    return (
        (covariance(series[0], series[2]) - covariance(series[1], series[2])) / market_var
        if market_var
        else None
    )


def leg_market_tables(rows):
    table = []
    dimensions = {
        "year": lambda r: r["signal_session"][:4],
        "sic2": lambda r: r["sic2"],
        "z_bucket": bucket_z,
        "correlation_bucket": bucket_corr,
        "market_window": lambda r: (
            "UP_GE_1PCT"
            if r["spy_return"] >= 0.01
            else "DOWN_LE_MINUS_1PCT"
            if r["spy_return"] <= -0.01
            else "MIDDLE"
        ),
        "market_days": lambda r: r["market_day_class"],
        "beta_difference": lambda r: (
            "UNAVAILABLE"
            if r["pair_beta_difference"] is None
            else "ABS_GE_0.5"
            if abs(r["pair_beta_difference"]) >= 0.5
            else "ABS_LT_0.5"
        ),
    }
    for dimension, key in dimensions.items():
        groups = defaultdict(list)
        for row in rows:
            groups[key(row)].append(row)
        for label, members in sorted(groups.items()):
            item = {"dimension": dimension, "group": label, "trade_count": len(members)}
            for field in (
                "gross_long_return",
                "gross_short_return",
                "gross_pair_return",
                "net_pair_return",
            ):
                item[f"mean_{field}"] = mean(r[field] for r in members)
                item[f"sum_{field}"] = sum(r[field] for r in members)
            item["short_negative_fraction"] = mean(r["gross_short_return"] < 0 for r in members)
            table.append(item)
    relations = []
    for x in ("entry_abs_z", "entry_correlation", "spy_return", "pair_beta_difference"):
        for y in (
            "gross_long_return",
            "gross_short_return",
            "gross_pair_return",
            "net_pair_return",
        ):
            relations.append(
                {
                    "independent": x,
                    "dependent": y,
                    **relationship(
                        [(r[x], r[y]) for r in rows if r[x] is not None and r[y] is not None]
                    ),
                }
            )
    return table, relations


@contextmanager
def offline():
    def forbidden(*args, **kwargs):
        raise RuntimeError("LOCAL_DIAGNOSTIC_NETWORK_FORBIDDEN")

    with (
        patch.object(socket.socket, "connect", forbidden),
        patch.object(socket.socket, "connect_ex", forbidden),
        patch.object(socket, "create_connection", forbidden),
    ):
        yield


class ReadOnlyBars:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def read_only(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


def write_table(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    _atomic_csv(path, rows, fields or ["status"])


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def frozen_inputs(reports, manifest_path):
    summary = json.loads((reports / f"{STEM}_summary.json").read_text())
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest["fingerprint"] != summary["manifest_fingerprint"]
        or fingerprint(manifest) != manifest["fingerprint"]
    ):
        raise ValueError("FROZEN_MANIFEST_FINGERPRINT_MISMATCH")
    if manifest["strategy_definition"] != asdict(v1.definitions.PAIRS_STAT_ARB_V1):
        raise ValueError("FROZEN_DEFINITION_MISMATCH")
    candidates = read_csv(reports / f"{STEM}_pair_candidates.csv")
    saved = {(r["signal_session"], r["symbol_a"], r["symbol_b"]): r for r in candidates}
    if len(saved) != len(candidates) or len(saved) != len(manifest["candidates"]):
        raise ValueError("FROZEN_CANDIDATE_MEMBERSHIP_MISMATCH")
    for row in manifest["candidates"]:
        other = saved[row["signal_session"], row["symbol_a"], row["symbol_b"]]
        if row["symbol_a"] >= row["symbol_b"]:
            raise ValueError("NONCANONICAL_FROZEN_PAIR")
        for key, value in row.items():
            if isinstance(value, (int, float)):
                valid = close(value, other[key])
            else:
                valid = value == other[key]
            if not valid:
                raise ValueError(f"FROZEN_CANDIDATE_MISMATCH: {key}")
    trades = read_csv(reports / f"{STEM}_trades.csv")
    signals = read_csv(reports / f"{STEM}_signals.csv")
    if trades != signals or len(trades) != summary["metrics"]["executed_trades"]:
        raise ValueError("FROZEN_TRADE_POPULATION_MISMATCH")
    for trade in trades:
        candidate = saved[trade["signal_session"], trade["symbol_a"], trade["symbol_b"]]
        for key in ("entry_z", "spread_t", "calibration_mean", "calibration_std", "correlation"):
            if not close(trade[key], candidate[key]):
                raise ValueError("FROZEN_SIGNAL_MISMATCH")
    return summary, manifest, candidates, trades


def run(reports, manifest_path, database, output_stem):
    """Two phases: fail closed on symmetry before interpreting any forward data."""
    output = reports / output_stem
    if list(reports.glob(output_stem + "_*")):
        raise FileExistsError("Use a new output stem; existing diagnostic artifacts are preserved")
    original, manifest, candidates, trades = frozen_inputs(reports, manifest_path)
    sources = [
        manifest_path,
        *[
            reports / f"{STEM}_{suffix}"
            for suffix in (
                "summary.json",
                "trades.csv",
                "signals.csv",
                "pair_candidates.csv",
                "correlation_bucket_attribution.csv",
                "z_bucket_attribution.csv",
                "exit_attribution.csv",
                "holding_attribution.csv",
                "leg_attribution.csv",
                "yearly.csv",
                "monthly.csv",
                "chronological_thirds.csv",
            )
        ],
    ]
    hashes = {str(p): digest(p) for p in sources}
    sessions = [date.fromisoformat(s) for s in manifest["history_sessions"]]
    sessions += trading_sessions_between(
        sessions[-1] + timedelta(days=1), sessions[-1] + timedelta(days=40)
    )
    identities = {r["symbol"]: r for r in manifest["universe"]}
    symbols = {s for r in candidates for s in (r["symbol_a"], r["symbol_b"])} | {"SPY"}
    print(f"Loading local bars for {len(symbols)} frozen symbols", flush=True)
    bars = load_daily(
        ReadOnlyBars(database), symbols, sessions[0], sessions[-1], identity_by_symbol=identities
    )
    bars.update(load_daily(ReadOnlyBars(database), ["SPY"], sessions[0], sessions[-1]))
    indexes = {str(d): i for i, d in enumerate(sessions)}
    audit = Symmetry()
    for i, candidate in enumerate(candidates):
        a, b = candidate["symbol_a"], candidate["symbol_b"]
        index = indexes[candidate["signal_session"]]
        x = observation(bars, sessions, index, a, b, identities)
        y = observation(bars, sessions, index, b, a, identities)
        audit.check_observation(candidate, x, y)
        if i % 20000 == 0:
            print(f"Symmetry observations {i}/{len(candidates)}", flush=True)
    rows = list(audit.rows.values())
    previous_exit = {}
    for trade in trades:
        key = pair_key(trade["symbol_a"], trade["symbol_b"])
        audit.boolean(
            "pair_lock",
            key not in previous_exit or trade["signal_session"] >= previous_exit[key],
            trade,
        )
        previous_exit[key] = trade["exit_execution_session"]
        rows.append(audit_trade(trade, bars, sessions, identities, audit))
    symmetry = {
        "pairs_checked": len(audit.rows),
        "trades_checked": len(trades),
        "observations_checked": len(candidates),
        "pair_lock": "CANONICAL_MEMBERSHIP_BOUNDARY_AND_NO_OVERLAP",
        **{
            f"{k}_violations": audit.counts[f"{k}_violations"]
            for k in (
                "spread",
                "mean",
                "std",
                "z",
                "correlation",
                "leg",
                "entry",
                "exit",
                "return",
                "pair_lock",
            )
        },
        "max_numeric_error": max(audit.errors.values(), default=0),
        "max_numeric_error_by_field": dict(audit.errors),
        "absolute_tolerance": ABS_TOL,
        "relative_tolerance": REL_TOL,
        "status": "SYMMETRY_FAIL" if audit.violations else "SYMMETRY_PASS",
    }
    write_table(Path(str(output) + "_symmetry_audit.csv"), rows)
    report = {
        "research_id": original["research_id"],
        "audit_type": "POST_TRADE_DIAGNOSTIC_ONLY",
        "manifest_fingerprint": manifest["fingerprint"],
        "source_artifact_sha256": hashes,
        "baseline_metrics": original["metrics"],
        "symmetry": symmetry,
        "pair_order_meaning": "LEXICOGRAPHIC_NO_ECONOMIC_MEANING",
        "provider_calls": 0,
        "strategy_changes": False,
        "selection_or_execution_rerun": False,
        "database_access": "SQLITE_MODE_RO_QUERY_ONLY",
        "leakage_guard": "FORWARD_READS_ONLY_AFTER_FROZEN_TRADES_LOADED",
    }
    if audit.violations:
        write_table(Path(str(output) + "_symmetry_violations.csv"), audit.violations)
        report.update(
            recommendation="INVESTIGATE_IMPLEMENTATION_FIRST", stopped_before_path_analysis=True
        )
        _atomic_text(
            Path(str(output) + "_diagnostic_summary.json"),
            json.dumps(report, indent=2, allow_nan=False),
        )
        return report
    report["z_sign_conclusions"] = [
        "POSITIVE_VS_NEGATIVE_Z_ATTRIBUTION_IS_LABEL_DEPENDENT",
        "DO_NOT_USE_Z_SIGN_AS_STRATEGY_FILTER",
    ]
    print("SYMMETRY_PASS; starting post-trade forward diagnostics", flush=True)
    paths = []
    for trade in trades:
        row = {
            **trade,
            "pair": f"{trade['symbol_a']}/{trade['symbol_b']}",
            "exit_session": trade["exit_execution_session"],
            "entry_abs_z": abs(trade["entry_z"]),
            "entry_correlation": trade["correlation"],
            "actual_exit_reason": trade["exit_reason"],
            "actual_holding_sessions": trade["holding_sessions"],
            "return_role": "FORWARD_DIAGNOSTIC",
            **forward_path(trade, bars, sessions, identities),
        }
        row["pair_beta_difference"] = beta_difference(trade, bars, sessions)
        i = sessions.index(date.fromisoformat(trade["entry_session"]))
        daily = []
        for j in range(i, i + int(trade["holding_sessions"])):
            p, c = bars.get(("SPY", sessions[j - 1])), bars.get(("SPY", sessions[j]))
            if p is not None and c is not None:
                daily.append(c.close / p.close - 1)
        up, down = any(r >= 0.01 for r in daily), any(r <= -0.01 for r in daily)
        row["market_day_class"] = (
            "UNAVAILABLE"
            if len(daily) != trade["holding_sessions"]
            else "BOTH"
            if up and down
            else "STRONG_UP"
            if up
            else "STRONG_DOWN"
            if down
            else "NEITHER"
        )
        paths.append(row)
    summaries = summary_rows(paths) + summary_rows(paths, prefix="fixed_")
    thirds = read_csv(reports / f"{STEM}_chronological_thirds.csv")

    def third(row):
        return next(
            r["group"]
            for r in thirds
            if r["signal_start"] <= row["signal_session"] <= r["signal_end"]
        )

    tables = {
        "mean_reversion_paths": paths,
        "convergence_summary": summaries,
        "convergence_by_z_bucket": grouped_summary(paths, bucket_z),
        "convergence_by_correlation_bucket": grouped_summary(paths, bucket_corr),
        "convergence_by_year": grouped_summary(paths, lambda r: r["signal_session"][:4]),
        "convergence_by_month": grouped_summary(paths, lambda r: r["signal_session"][:7]),
        "convergence_by_third": grouped_summary(paths, third),
        "convergence_by_exit": grouped_summary(paths, lambda r: r["actual_exit_reason"]),
    }
    tables["leg_diagnostics"], tables["leg_market_relationships"] = leg_market_tables(paths)
    relationships, return_groups = [], []
    for prefix in ("", "fixed_"):
        for h in HORIZONS:
            valid = [
                r
                for r in paths
                if r[f"{prefix}availability_{h}"] == "OBSERVABLE"
                and r[f"forward_gross_pair_return_{h}"] is not None
            ]
            relationships.append(
                {
                    "horizon": h,
                    "path": "FIXED_ENTRY" if prefix else "V1_ROLLING",
                    "independent": "ABS_Z_ENTRY_MINUS_ABS_Z_H",
                    "dependent": "FORWARD_DIAGNOSTIC_GROSS_PAIR_RETURN",
                    **relationship(
                        [
                            (
                                r["entry_abs_z"] - r[f"{prefix}abs_z_t_plus_{h}"],
                                r[f"forward_gross_pair_return_{h}"],
                            )
                            for r in valid
                        ]
                    ),
                }
            )
            if h in (5, 10, 15):
                for cls in sorted({r[f"{prefix}path_class_{h}"] for r in valid}):
                    values = [
                        r[f"forward_gross_pair_return_{h}"]
                        for r in valid
                        if r[f"{prefix}path_class_{h}"] == cls
                    ]
                    return_groups.append(
                        {
                            "horizon": h,
                            "path": prefix or "V1_ROLLING",
                            "class": cls,
                            "trade_count": len(values),
                            "mean_gross_pair_return": mean(values),
                            "median_gross_pair_return": median(values),
                        }
                    )
    tables.update(
        convergence_return_relationships=relationships, convergence_return_buckets=return_groups
    )
    for name, members in tables.items():
        write_table(Path(str(output) + f"_{name}.csv"), members)
    if hashes != {str(p): digest(p) for p in sources}:
        raise ValueError("SOURCE_ARTIFACT_MUTATED_DURING_AUDIT")
    report.update(
        trade_count=len(paths),
        primary_z_semantics="ROLLING_PRECEDING_60_CLOSES_EXCLUDING_CURRENT_OBSERVATION;CORRELATION_NOT_REGATED_DURING_HOLDING",
        fixed_z_semantics="SIGNAL_ENTRY_CALIBRATION_MEAN_STD_FROZEN;REFERENCE_ONLY",
        horizon_semantics="H_COMPLETED_SESSIONS_FROM_ENTRY_OPEN;H1_ENTRY_CLOSE;H5_ACTUAL_FIFTH_HOLDING_CLOSE",
        first_passage_semantics="FIRST_OBSERVED_CLOSE_AMONG_H1_TO_H15;NO_INTRADAY_INFERENCE",
        censoring_semantics="COMPLETE_PREFIX_REQUIRED_FOR_EACH_HORIZON;NO_FILL;DENOMINATORS_SEPARATE_FOR_EACH_PATH",
        diagnostic_class_thresholds={
            "partial_min_ratio": 0.8,
            "divergence_terminal_ratio": 1.2,
            "strong_abs_z": 1.0,
        },
        market_day_threshold=0.01,
        beta_difference_threshold=0.5,
        beta_semantics="59_PRE_SIGNAL_CALIBRATION_LOG_RETURNS_VS_LOCAL_SPY;LONG_BETA_MINUS_SHORT_BETA",
        summary=summaries,
        source_artifacts_unchanged=True,
        limitations=original["limitations"]
        + [
            "DEPENDENT_OVERLAPPING_TRADES",
            "DESCRIPTIVE_SEEN_VALIDATION_SAMPLE",
            "CONFIG_BOUND_DAILY_NO_PER_ROW_PROVENANCE",
            "ROLLING_Z_CONTRACTION_CAN_REFLECT_MOVING_MEAN_OR_STD",
        ],
        artifacts={name: str(Path(str(output) + f"_{name}.csv")) for name in tables},
    )
    _atomic_text(
        Path(str(output) + "_diagnostic_summary.json"),
        json.dumps(report, indent=2, allow_nan=False),
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_pair_candidates.json"
        ),
    )
    parser.add_argument("--database", type=Path, default=Path("data/trading_system.sqlite3"))
    parser.add_argument("--output-stem", default=STEM + "_diagnostic")
    args = parser.parse_args()
    with offline():
        report = run(args.reports, args.manifest, args.database, args.output_stem)
    print(
        json.dumps(
            {"symmetry": report["symmetry"], "trade_count": report.get("trade_count")}, indent=2
        )
    )
    if report["symmetry"]["status"] != "SYMMETRY_PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
