"""Local-only market momentum runs and session-level diagnostics; no portfolio model."""

import json
import math
from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import asdict
from itertools import groupby
from pathlib import Path
from statistics import mean, median, stdev
from time import perf_counter

from trading_system.backtest.market_intraday_momentum_v1 import (
    EVENT_FIELDS,
    MOMENTUM_V1,
    simulate_momentum_session,
)
from trading_system.backtest.market_intraday_momentum_v1_data import (
    momentum_requirements_report,
    prepare_momentum_data,
)
from trading_system.backtest.market_intraday_momentum_v1_manifest import (
    candidate_manifest as build_manifest,
)
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text

COVERAGE_FIELDS = (
    "session",
    "symbol",
    "window",
    "timeframe",
    "status",
    "reason",
    "expected_bars",
    "native_bars_present",
    "required_timestamps",
    "missing_timestamps",
)
MAGNITUDE_BUCKETS = (
    "0_TO_0.25_PERCENT",
    "0.25_TO_0.50_PERCENT",
    "0.50_TO_1.00_PERCENT",
    "OVER_1.00_PERCENT",
)


def simulate_prepared_momentum(prepared, native_sessions):
    """Only frozen records and owned native spool: no database/provider argument or calls."""
    if not prepared.ready:
        raise ValueError(
            "MARKET_INTRADAY_MOMENTUM_DATA_UNAVAILABLE: unresolved required-window coverage"
        )
    return [
        simulate_momentum_session(candidate, native_sessions.entry_session(*candidate.key)[0])
        for candidate in prepared.candidates
    ]


def return_statistics(values):
    n = len(values)
    average = mean(values) if values else None
    deviation = stdev(values) if n >= 2 else None
    error = deviation / math.sqrt(n) if deviation is not None else None
    return {
        "n": n,
        "mean": average,
        "standard_deviation": deviation,
        "standard_error": error,
        "ci95_lower": average - 1.96 * error if error is not None else None,
        "ci95_upper": average + 1.96 * error if error is not None else None,
    }


def core_metrics(events):
    trades = [row for row in events if row["net_return"] is not None]
    result = {
        "candidate_sessions": len(events),
        "morning_observable_sessions": sum(row["morning_observable"] for row in events),
        "signals": sum(row["signal"] for row in events),
        "long_signals": sum(row["direction"] == "LONG" for row in events),
        "short_signals": sum(row["direction"] == "SHORT" for row in events),
        "zero_return_no_signals": sum(
            row["status"] == "NO_SIGNAL_ZERO_FIRST_30M_RETURN" for row in events
        ),
        "executed_trades": len(trades),
        "unobservable_closing_window_signals": sum(
            row["status"] == "CLOSING_WINDOW_UNOBSERVABLE" for row in events
        ),
        "first_30m_unobservable_sessions": sum(not row["morning_observable"] for row in events),
    }
    for label in ("gross", "net"):
        values = [row[f"{label}_return"] for row in trades]
        wins, losses = (
            [value for value in values if value > 0],
            [value for value in values if value < 0],
        )
        result.update(
            {
                f"{label}_expectancy": mean(values) if values else None,
                f"median_{label}_return": median(values) if values else None,
                f"{label}_profit_factor": sum(wins) / -sum(losses) if losses else None,
                **{
                    f"{label}_return_{key}": value
                    for key, value in return_statistics(values).items()
                },
            }
        )
        if label == "net":
            result.update(
                win_rate=len(wins) / len(values) if values else None,
                average_winner=mean(wins) if wins else None,
                average_loser=mean(losses) if losses else None,
            )
    for field in (
        "first_30m_return",
        "final_30m_raw_return",
        "MFE",
        "MAE",
        "holding_minutes",
        "modeled_cost",
    ):
        values = [row[field] for row in events if row[field] is not None]
        result[f"mean_{field}"] = mean(values) if values else None
        result[f"median_{field}"] = median(values) if values else None
    result["modeled_cost_drag"] = result["mean_modeled_cost"]
    result["directional_hit_rate"] = (
        mean(row["directional_hit"] for row in trades) if trades else None
    )
    return result


def _average_ranks(values):
    output = [0.0] * len(values)
    ranked = sorted(enumerate(values), key=lambda item: item[1])
    offset = 0
    for _, group in groupby(ranked, key=lambda item: item[1]):
        items = list(group)
        rank = offset + (len(items) + 1) / 2
        for index, _ in items:
            output[index] = rank
        offset += len(items)
    return output


def _moments(x, y):
    mx, my = mean(x), mean(y)
    dx, dy = [value - mx for value in x], [value - my for value in y]
    return (
        sum(value * value for value in dx),
        sum(value * value for value in dy),
        sum(a * b for a, b in zip(dx, dy, strict=True)),
    )


def predictive_diagnostics(events):
    pairs = [
        (row["first_30m_return"], row["final_30m_raw_return"])
        for row in events
        if row["first_30m_return"] is not None and row["final_30m_raw_return"] is not None
    ]
    output = dict.fromkeys(
        (
            "pearson_correlation",
            "spearman_rank_correlation",
            "ols_slope",
            "ols_intercept",
            "r_squared",
        )
    )
    output["paired_sessions"] = len(pairs)
    if len(pairs) < 2:
        return output
    x, y = zip(*pairs, strict=True)
    xx, yy, xy = _moments(x, y)
    if xx > 0:
        output.update(ols_slope=xy / xx, ols_intercept=mean(y) - xy / xx * mean(x))
    if xx > 0 and yy > 0:
        correlation = max(-1.0, min(1.0, xy / math.sqrt(xx * yy)))
        rx, ry, rxy = _moments(_average_ranks(x), _average_ranks(y))
        output.update(
            pearson_correlation=correlation,
            r_squared=correlation**2,
            spearman_rank_correlation=max(-1.0, min(1.0, rxy / math.sqrt(rx * ry))),
        )
    return output


def magnitude_bucket(row):
    magnitude = abs(row["first_30m_return"])
    for threshold, label in zip((0.0025, 0.005, 0.01), MAGNITUDE_BUCKETS, strict=False):
        if magnitude <= threshold:
            return label
    return MAGNITUDE_BUCKETS[-1]


def build_diagnostics(prepared, events):
    days = [row.session for row in prepared.candidates]
    thirds = {
        str(day): f"THIRD_{min(index * 3 // len(days), 2) + 1}" for index, day in enumerate(days)
    }
    tables = {}
    for name, key, labels, rows in (
        ("monthly", lambda row: row["session"][:7], sorted({str(day)[:7] for day in days}), events),
        ("yearly", lambda row: row["session"][:4], sorted({str(day.year) for day in days}), events),
        (
            "chronological_subperiods",
            lambda row: thirds[row["session"]],
            [f"THIRD_{i}" for i in (1, 2, 3)],
            events,
        ),
        ("direction_attribution", lambda row: row["direction"], ("LONG", "SHORT"), events),
        (
            "magnitude_buckets",
            magnitude_bucket,
            MAGNITUDE_BUCKETS,
            [row for row in events if row["morning_observable"]],
        ),
    ):
        groups = defaultdict(list)
        for row in rows:
            groups[key(row)].append(row)
        tables[name] = [{"group": label, **core_metrics(groups[label])} for label in labels]
    for row in tables["chronological_subperiods"]:
        sessions = [day for day, group in thirds.items() if group == row["group"]]
        row.update(
            start=sessions[0] if sessions else None,
            end=sessions[-1] if sessions else None,
            trading_sessions=len(sessions),
        )
    return core_metrics(events), predictive_diagnostics(events), tables


def summary_definition(prepared, config, *, preflight):
    coverage = Counter(row["status"] for row in prepared.coverage)
    warnings = [
        "SPY-only research proxy; no replacement, Daily ranking, fundamentals "
        "or portfolio allocator.",
        "Current local asset membership is required; historical tradability is not reconstructed. "
        "There is no historical cross-sectional universe selection, "
        "but this is not a survivorship-clean claim.",
        "Alpaca tradable=true does not establish German/EU broker purchase eligibility. "
        "execution_eligibility_germany=NOT_EVALUATED; no UCITS substitution.",
        "Short borrow availability and execution eligibility are NOT_MODELED; borrow fee is zero. "
        "Separate execution validation is required before any real trading interpretation.",
        "Positive results do not automatically promote this family to champion, "
        "paper trading, or live trading.",
        "Confidence intervals use mean +/- 1.96 sample-standard-error, assuming independent "
        "executed sessions. Serial dependence and fat tails can weaken this approximation. "
        "Statistical significance does not establish economic significance.",
    ]
    if config.universe.market_data_feed.lower() == "iex":
        warnings.append("IEX is single-venue data, not consolidated US market coverage.")
    return {
        "research_family": MOMENTUM_V1.research_family,
        "research_id": MOMENTUM_V1.research_id,
        "status": MOMENTUM_V1.status,
        "mode": "PREFLIGHT" if preflight else "VALIDATION",
        "requested_start": prepared.start.isoformat(),
        "requested_end": prepared.end.isoformat(),
        "strategy_definition": asdict(MOMENTUM_V1),
        "proxy": prepared.proxy,
        "candidate_sessions": len(prepared.candidates),
        "candidate_source": prepared.candidate_source,
        "candidate_manifest_path": prepared.candidate_manifest_path,
        "candidate_manifest_fingerprint": prepared.candidate_manifest_fingerprint,
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "extended_hours": False,
        "execution_eligibility_germany": "NOT_EVALUATED",
        "short_execution_eligibility": "NOT_MODELED",
        "survivorship_clean": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
        "ready_for_local_validation": prepared.ready,
        "coverage": {
            "required_native_bars": len(prepared.candidates) * 4,
            "window_status_counts": dict(coverage),
            "by_window": {
                window: dict(
                    Counter(row["status"] for row in prepared.coverage if row["window"] == window)
                )
                for window in ("MORNING_WINDOW", "CLOSING_WINDOW")
            },
        },
        "metrics": {},
        "predictive_diagnostics": {},
        "stability": {},
        "performance": prepared.performance,
        "warnings": warnings,
        "metric_definitions": {
            "units": (
                "Returns and rates are fractions; equal independent entry notional per execution."
            ),
            "first_30m_summary": (
                "All morning-observable sessions, including zero returns and unexecuted signals."
            ),
            "final_30m_summary": (
                "All sessions with both morning and closing windows observable, "
                "including zero-morning no-signals."
            ),
            "predictive_diagnostics": (
                "Same paired sessions; Pearson, average-tie-rank Spearman and OLS with intercept; "
                "no sizing or filters."
            ),
            "execution_statistics": (
                "Executed sessions only; sample standard deviation (n-1), SE=SD/sqrt(n), "
                "normal 95% CI. Null for insufficient observations."
            ),
            "profit_factor": (
                "Sum positive returns / abs(sum negative returns); null without losses."
            ),
            "directional_hit": (
                "LONG final raw return > 0; SHORT final raw return < 0; zero is not a hit."
            ),
            "modeled_cost": (
                "Gross directional return minus net fill return; commission and borrow fee zero."
            ),
            "MFE_MAE": (
                "Direction-adjusted reference-price excursions over only the two holding bars, "
                "clamped around zero."
            ),
            "magnitude_buckets": (
                "Absolute first-30m return: [0,.25%], (.25%,.50%], (.50%,1%], >1%; "
                "diagnostics only."
            ),
            "chronological_thirds": (
                "Three contiguous groups of requested XNYS sessions, "
                "including unobservable and no-signal sessions."
            ),
        },
    }


def output_paths(directory, stem, *, preflight):
    if (
        not stem
        or Path(stem).name != stem
        or any(char in stem for char in "/\\:")
        or stem in {".", ".."}
    ):
        raise ValueError("Market momentum output stem must be a plain filename stem")
    names = (
        ("summary", "coverage", "momentum_candidates", "intraday_requirements")
        if preflight
        else (
            "summary",
            "coverage",
            "sessions",
            "trades",
            "monthly",
            "yearly",
            "chronological_subperiods",
            "direction_attribution",
            "magnitude_buckets",
        )
    )
    json_names = {"summary", "momentum_candidates", "intraday_requirements"}
    paths = {
        name: Path(directory) / f"{stem}_{name}.{'json' if name in json_names else 'csv'}"
        for name in names
    }
    existing = next((path for path in paths.values() if path.exists()), None)
    if existing:
        raise FileExistsError(f"Market momentum output already exists: {existing}")
    return paths


def run_market_intraday_momentum_v1(
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
            raise ValueError("Market momentum preflight always discovers sessions")
    elif (candidate_manifest is not None) == rediscover_candidates:
        raise ValueError("Market momentum validation requires a manifest or explicit rediscovery")
    paths = output_paths(directory, stem, preflight=preflight)
    with nullcontext() if preflight else NativeEntrySessions() as native:
        with ProgressPhase("market_intraday_momentum_v1_progress", "local_qualification"):
            prepared = prepare_momentum_data(
                database,
                config,
                start,
                end,
                native_sessions=native,
                candidate_manifest=candidate_manifest,
            )
        if preflight:
            manifest = build_manifest(prepared, config)
            prepared.candidate_manifest_path = str(paths["momentum_candidates"])
            prepared.candidate_manifest_fingerprint = manifest["fingerprint"]
        summary = summary_definition(prepared, config, preflight=preflight)
        tables = {"coverage": prepared.coverage}
        if not preflight:
            started = perf_counter()
            with ProgressPhase("market_intraday_momentum_v1_progress", "simulation"):
                events = simulate_prepared_momentum(prepared, native)
            prepared.performance.update(
                simulation_seconds=perf_counter() - started,
                sqlite_query_count_simulation=0,
                native_cache_peak=native.peak_cache_size,
            )
            started = perf_counter()
            metrics, predictive, diagnostics = build_diagnostics(prepared, events)
            summary.update(
                metrics=metrics, predictive_diagnostics=predictive, stability=diagnostics
            )
            tables.update(
                sessions=events,
                trades=[row for row in events if row["net_return"] is not None],
                **diagnostics,
            )
            prepared.performance["diagnostics_seconds"] = perf_counter() - started
        Path(directory).mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            fields = (
                EVENT_FIELDS
                if name in {"sessions", "trades"}
                else COVERAGE_FIELDS
                if name == "coverage"
                else tuple(rows[0])
                if rows
                else ("group", *core_metrics([]))
            )
            _atomic_csv(paths[name], rows, list(fields))
        if preflight:
            _atomic_text(
                paths["momentum_candidates"], json.dumps(manifest, indent=2, allow_nan=False)
            )
            _atomic_text(
                paths["intraday_requirements"],
                json.dumps(
                    momentum_requirements_report(prepared, config), indent=2, allow_nan=False
                ),
            )
        _atomic_text(paths["summary"], json.dumps(summary, indent=2, allow_nan=False))
    return summary, paths
