"""Pair-level evidence and offline command orchestration; no portfolio or promotion."""

import json
import math
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path
from statistics import mean, median

from trading_system.backtest.pairs_stat_arb_v1 import PAIRS_V1, simulate_prepared_pairs
from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
from trading_system.backtest.report import _atomic_csv, _atomic_text

WARNINGS = [
    "CURRENT_UNIVERSE_ONLY; NOT_SURVIVORSHIP_CLEAN; CURRENT_LOCAL_SIC_NOT_PIT. "
    "SIC classification is current/local and not guaranteed PIT-safe.",
    "Short availability, borrow availability, hard-to-borrow fees, locate fees, broker "
    "execution eligibility and German/EU retail execution eligibility are NOT_MODELED. "
    "Commission=0; borrow fee=0. Separate execution-feasibility research is required.",
    "No automatic promotion to champion, paper/live trading or a combined F+Pairs portfolio.",
    "Overlapping pair trades are dependent. All returns use unit gross notional; "
    "no portfolio allocator or portfolio performance statistics are defined.",
    "Daily feed/adjustment are config-bound; local bars have no per-row provenance. "
    "Provider range verification does not establish individual missing-session absence.",
]


def core_metrics(trades):
    result = {"executed_trades": len(trades)}
    for kind in ("gross", "net"):
        values = [row[f"{kind}_pair_return"] for row in trades]
        wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
        result.update(
            {
                f"{kind}_expectancy": mean(values) if values else None,
                f"median_{kind}_return": median(values) if values else None,
                f"{kind}_profit_factor": sum(wins) / -sum(losses) if losses else None,
            }
        )
        if kind == "net":
            result.update(
                win_rate=len(wins) / len(values) if values else None,
                loss_rate=len(losses) / len(values) if values else None,
                average_winner=mean(wins) if wins else None,
                average_loser=mean(losses) if losses else None,
            )
    for field in ("holding_sessions", "pair_mfe", "pair_mae", "modeled_cost_drag"):
        values = [row[field] for row in trades if row.get(field) is not None]
        result[f"mean_{field}"] = mean(values) if values else None
        result[f"median_{field}"] = median(values) if values else None
    return result


def average_ranks(values):
    result = [0.0] * len(values)
    offset = 0
    for _, group in groupby(sorted(enumerate(values), key=lambda item: item[1]), lambda x: x[1]):
        members = list(group)
        for index, _ in members:
            result[index] = offset + (len(members) + 1) / 2
        offset += len(members)
    return result


def relationship(pairs):
    result = dict.fromkeys(("pearson", "spearman", "ols_beta", "ols_intercept", "r_squared"))
    result["observations"] = len(pairs)
    if len(pairs) < 2:
        return {**result, "availability": "INSUFFICIENT_OBSERVATIONS"}
    x, y = zip(*pairs, strict=True)

    def moments(a, b):
        ma, mb = mean(a), mean(b)
        da, db = [v - ma for v in a], [v - mb for v in b]
        return (
            sum(v * v for v in da),
            sum(v * v for v in db),
            sum(u * v for u, v in zip(da, db, strict=True)),
        )

    xx, yy, xy = moments(x, y)
    if xx:
        result.update(ols_beta=xy / xx, ols_intercept=mean(y) - xy / xx * mean(x))
    if xx and yy:
        pearson = max(-1.0, min(1.0, xy / math.sqrt(xx * yy)))
        rx, ry, rxy = moments(average_ranks(x), average_ranks(y))
        result.update(pearson=pearson, r_squared=pearson**2, spearman=rxy / math.sqrt(rx * ry))
    return {**result, "availability": "AVAILABLE" if xx and yy else "ZERO_VARIANCE"}


def z_bucket(row):
    z = abs(row["entry_z"])
    return "2.0_TO_2.5" if z < 2.5 else "2.5_TO_3.0" if z <= 3.0 else "OVER_3.0"


def correlation_bucket(row):
    corr = row["correlation"]
    return "0.70_TO_0.80" if corr < 0.80 else "0.80_TO_0.90" if corr < 0.90 else "0.90_TO_1.00"


def distribution(values):
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": median(values) if values else None,
        "mean": mean(values) if values else None,
        "max": max(values) if values else None,
    }


def diagnostics(prepared, evaluations, signals):
    trades = [row for row in signals if row["net_pair_return"] is not None]
    summary = core_metrics(trades)
    summary.update(
        candidate_pair_sessions=len(evaluations),
        eligible_correlated_pair_sessions=sum(
            row["calibration_status"] == "ELIGIBLE" for row in evaluations
        ),
        signals=len(signals),
        positive_z_signals=sum(row["entry_z"] >= 2 for row in signals),
        negative_z_signals=sum(row["entry_z"] <= -2 for row in signals),
        entered_trades=sum(row["entry_status"] == "EXECUTED" for row in signals),
        unobservable_entries=sum(row["entry_status"] == "ENTRY_UNOBSERVABLE" for row in signals),
        unobservable_exits=sum(row["status"] == "EXIT_UNOBSERVABLE" for row in signals),
        censored_outcomes=sum(row["status"] == "OUTCOME_CENSORED" for row in signals),
    )
    tables = {"pair_candidates": evaluations, "signals": signals, "trades": trades}
    days = list(prepared.signal_dates)
    thirds = {day: f"THIRD_{min(i * 3 // len(days), 2) + 1}" for i, day in enumerate(days)}
    labels = {
        "monthly": sorted({day[:7] for day in days}),
        "yearly": sorted({day[:4] for day in days}),
        "chronological_thirds": ["THIRD_1", "THIRD_2", "THIRD_3"],
        "exit_attribution": ["MEAN_REVERSION", "MAX_HOLD_5"],
        "z_bucket_attribution": ["2.0_TO_2.5", "2.5_TO_3.0", "OVER_3.0"],
        "correlation_bucket_attribution": ["0.70_TO_0.80", "0.80_TO_0.90", "0.90_TO_1.00"],
        "holding_attribution": [str(i) for i in range(1, 6)],
    }
    for name, key in (
        ("monthly", lambda row: row["signal_session"][:7]),
        ("yearly", lambda row: row["signal_session"][:4]),
        ("chronological_thirds", lambda row: thirds[row["signal_session"]]),
        ("exit_attribution", lambda row: row["exit_reason"]),
        ("z_bucket_attribution", z_bucket),
        ("correlation_bucket_attribution", correlation_bucket),
        ("holding_attribution", lambda row: str(row["holding_sessions"])),
    ):
        groups = defaultdict(list)
        for row in trades:
            groups[key(row)].append(row)
        tables[name] = (
            [
                {
                    "group": label,
                    "availability": "AVAILABLE" if groups[label] else "NO_TRADES",
                    **core_metrics(groups[label]),
                }
                for label in labels[name]
            ]
            if trades
            else []
        )
        if name == "chronological_thirds":
            for row in tables[name]:
                dates = [day for day in days if thirds[day] == row["group"]]
                row.update(
                    signal_start=dates[0] if dates else None,
                    signal_end=dates[-1] if dates else None,
                )
    tables["industry_attribution"] = []
    for sic2 in sorted({row["sic2"] for row in evaluations}):
        candidates = [r for r in evaluations if r["sic2"] == sic2]
        tables["industry_attribution"].append(
            {
                "sic2": sic2,
                "classification": "CURRENT_LOCAL_SIC_NOT_PIT",
                "candidate_pairs": len({(r["symbol_a"], r["symbol_b"]) for r in candidates}),
                "candidate_pair_sessions": len(candidates),
                "signals": sum(r["sic2"] == sic2 for r in signals),
                **core_metrics([r for r in trades if r["sic2"] == sic2]),
            }
        )
    pair_signals = Counter((r["symbol_a"], r["symbol_b"]) for r in signals)
    pair_trades = defaultdict(list)
    for row in trades:
        pair_trades[row["symbol_a"], row["symbol_b"]].append(row)
    total_net = sum(row["net_pair_return"] for row in trades)
    pnl = {key: sum(row["net_pair_return"] for row in rows) for key, rows in pair_trades.items()}
    abs_net = sum(abs(value) for value in pnl.values())
    tables["pair_concentration"] = [
        {
            "symbol_a": a,
            "symbol_b": b,
            "signals": count,
            "share_of_signals": count / len(signals),
            "executed_trades": len(pair_trades[a, b]),
            "net_pnl": pnl.get((a, b), 0),
            "share_of_net_pnl": pnl.get((a, b), 0) / total_net if total_net else None,
            "share_of_absolute_pair_net_pnl": abs(pnl.get((a, b), 0)) / abs_net
            if abs_net
            else None,
        }
        for (a, b), count in sorted(pair_signals.items())
    ]
    concentration = {"unique_traded_pairs": len(pnl)}
    for n in (1, 5, 10):
        concentration[f"top_{n}_pair_share_of_signals"] = (
            sum(sorted(pair_signals.values(), reverse=True)[:n]) / len(signals) if signals else None
        )
        concentration[f"top_{n}_pair_share_of_net_pnl"] = (
            sum(sorted(pnl.values(), reverse=True)[:n]) / total_net if total_net else None
        )
        concentration[f"top_{n}_pair_share_of_absolute_net_pnl"] = (
            sum(sorted(map(abs, pnl.values()), reverse=True)[:n]) / abs_net if abs_net else None
        )
    symbols = defaultdict(
        lambda: {"pair_trades": 0, "gross_pnl_contribution": 0.0, "net_pnl_contribution": 0.0}
    )
    for row in trades:
        for leg in ("a", "b"):
            item = symbols[row[f"symbol_{leg}"]]
            item["pair_trades"] += 1
            side = row[f"direction_{leg}"].lower()
            for kind in ("gross", "net"):
                item[f"{kind}_pnl_contribution"] += 0.5 * row[f"{kind}_{side}_return"]
    tables["symbol_concentration"] = [{"symbol": s, **r} for s, r in sorted(symbols.items())]
    concentration["unique_traded_symbols"] = len(symbols)
    summary["concentration"] = concentration
    market = relationship(
        [(r["spy_return"], r["net_pair_return"]) for r in trades if r["spy_return"] is not None]
    )
    summary["market_beta"] = market
    market["missing_spy_windows"] = len(trades) - market["observations"]
    tables["market_beta"] = (
        [{"dependent": "NET_PAIR_RETURN", "independent": "SPY_SAME_WINDOW", **market}]
        if market["observations"]
        else []
    )
    summary["signal_efficacy"] = {
        k: v
        for k, v in relationship(
            [(abs(r["entry_z"]), r["net_pair_return"]) for r in trades]
        ).items()
        if k in {"pearson", "spearman", "observations", "availability"}
    }
    tables["leg_attribution"] = (
        [
            {
                "side": side.upper(),
                "executed_trades": len(trades),
                "gross_contribution": sum(0.5 * r[f"gross_{side}_return"] for r in trades),
                "net_contribution": sum(0.5 * r[f"net_{side}_return"] for r in trades),
            }
            for side in ("long", "short")
        ]
        if trades
        else []
    )
    summary["mean_reversion_success"] = {
        "fraction_reaching_zero_before_max_hold": (
            mean(r["mean_crossing_observed"] for r in trades) if trades else None
        ),
        "fraction_exiting_max_hold_5": (
            mean(r["exit_reason"] == "MAX_HOLD_5" for r in trades) if trades else None
        ),
        "groups": {
            reason: {
                "entry_z": distribution(
                    [r["entry_z"] for r in trades if r["exit_reason"] == reason]
                ),
                "entry_abs_z": distribution(
                    [abs(r["entry_z"]) for r in trades if r["exit_reason"] == reason]
                ),
                "holding_sessions": distribution(
                    [r["holding_sessions"] for r in trades if r["exit_reason"] == reason]
                ),
            }
            for reason in ("MEAN_REVERSION", "MAX_HOLD_5")
        },
    }
    if trades:
        long_ratio = mean(1 + r["gross_long_return"] for r in trades)
        short_ratio = mean(1 - r["gross_short_return"] for r in trades)
        root_l, root_s = math.sqrt(long_ratio), math.sqrt(short_ratio)
        summary["break_even_adverse_bps_per_fill"] = 10000 * (root_l - root_s) / (root_l + root_s)
    else:
        summary["break_even_adverse_bps_per_fill"] = None
    return summary, tables


def summary_metadata(prepared, *, preflight):
    manifest = prepared.manifest
    candidates = manifest["candidates"]
    counts = Counter(row["calibration_status"] for row in candidates)
    missing = sum(row["status"] != "REQUIRED_PRESENT" for row in prepared.coverage)
    selected = manifest["selected"]
    return {
        "research_family": PAIRS_V1.research_family,
        "research_id": PAIRS_V1.research_id,
        "status": PAIRS_V1.status,
        "stage": "PREFLIGHT" if preflight else "VALIDATION",
        "requested_start": str(prepared.start),
        "requested_end": str(prepared.end),
        "actual_signal_start": manifest["signal_start"],
        "actual_signal_end": manifest["signal_end"],
        "signal_end": manifest["signal_end"],
        "manifest_fingerprint": manifest["fingerprint"],
        "sessions": len(selected),
        **manifest["discovery_counts"],
        "sic2_groups": len({sic2 for groups in selected.values() for sic2 in groups}),
        "unique_symbols": len(
            {
                r["symbol"]
                for groups in selected.values()
                for names in groups.values()
                for r in names
            }
        ),
        "unique_pairs": len({(r["symbol_a"], r["symbol_b"]) for r in candidates}),
        "pair_session_candidates": len(candidates),
        "complete_calibration_pairs": len(candidates) - counts["INCOMPLETE_CALIBRATION"],
        "incomplete_calibration_pairs": counts["INCOMPLETE_CALIBRATION"],
        "eligible_correlated_pair_sessions": counts["ELIGIBLE"],
        "calibration_status_counts": dict(counts),
        "local_missing_data": missing,
        "ready_for_local_validation": bool(candidates) and not missing,
        "limitations": [
            "CURRENT_UNIVERSE_ONLY",
            "NOT_SURVIVORSHIP_CLEAN",
            "CURRENT_LOCAL_SIC_NOT_PIT",
        ],
        "market_data_feed": manifest["market_data_feed"],
        "market_data_adjustment": manifest["market_data_adjustment"],
        "network_used": False,
        "portfolio_strategy_defined": False,
        "dollar_neutral": True,
        "beta_neutral": False,
        "commission_bps": 0,
        "borrow_fee_bps": 0,
        "slippage_bps_per_fill": 5,
        "short_availability": "NOT_MODELED",
        "german_eu_execution_eligibility": "NOT_MODELED",
        "signal_z_deferred_to_validation": True,
        "warnings": WARNINGS,
        **prepared.metadata,
    }


def run_pairs_stat_arb_v1(
    database,
    config,
    start,
    end,
    directory,
    *,
    stem,
    preflight=False,
    candidate_manifest=None,
    rediscover_candidates=False,
):
    if preflight:
        if candidate_manifest is not None or rediscover_candidates:
            raise ValueError("Pairs preflight discovers its own immutable membership")
    elif (candidate_manifest is not None) == rediscover_candidates:
        raise ValueError("Pairs validation requires a manifest or explicit --rediscover-candidates")
    if not stem or Path(stem).name != stem or any(c in stem for c in "/\\:") or stem in {".", ".."}:
        raise ValueError("Pairs output stem must be a plain filename stem")
    directory = Path(directory)
    if any(directory.glob(f"{stem}_*")):
        raise FileExistsError(f"Pairs immutable report stem already exists: {stem}")
    prepared = prepare_pairs_data(
        database, config, start, end, candidate_manifest=candidate_manifest, preflight=preflight
    )
    summary = summary_metadata(prepared, preflight=preflight)
    tables = {"coverage": prepared.coverage}
    documents = {}
    if preflight:
        documents.update(
            pair_candidates=prepared.manifest,
            daily_requirements={
                "research_family": PAIRS_V1.research_family,
                "research_id": PAIRS_V1.research_id,
                "manifest_fingerprint": prepared.manifest["fingerprint"],
                "market_data_feed": prepared.manifest["market_data_feed"],
                "market_data_adjustment": prepared.manifest["market_data_adjustment"],
                "timeframe": "1d",
                "outcome_tail_sessions": prepared.manifest["outcome_tail_sessions"],
                "required_sessions": prepared.requirements,
            },
        )
    else:
        evaluations, signals = simulate_prepared_pairs(prepared)
        metrics, diagnostic_tables = diagnostics(prepared, evaluations, signals)
        summary["metrics"] = metrics
        tables.update(diagnostic_tables)
        if rediscover_candidates:
            documents["rediscovered_manifest"] = prepared.manifest
    summary["unavailable_diagnostics"] = [name for name, rows in tables.items() if not rows]
    paths = {}
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        if not rows:
            continue  # No fabricated zero-observation table: explicit summary unavailability.
        path = directory / f"{stem}_{name}.csv"
        fields = list(dict.fromkeys(key for row in rows for key in row))
        _atomic_csv(path, rows, fields)
        paths[name] = path
    documents["summary"] = summary
    for name, document in documents.items():
        path = directory / f"{stem}_{name}.json"
        _atomic_text(path, json.dumps(document, indent=2, allow_nan=False))
        paths[name] = path
    return summary, paths
