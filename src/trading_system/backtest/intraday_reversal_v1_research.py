"""Local-only Reversal V1 orchestration and signal-level research reports."""

import json
from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import asdict
from itertools import groupby
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from trading_system.backtest.intraday_reversal_v1 import (
    EVENT_FIELDS,
    REVERSAL_V1,
    SESSION_FIELDS,
    execute_signal,
    rank_first_hour,
)
from trading_system.backtest.intraday_reversal_v1_data import (
    prepare_reversal_data,
    reversal_requirements_report,
)
from trading_system.backtest.intraday_reversal_v1_manifest import (
    candidate_manifest as build_manifest,
)
from trading_system.backtest.intraday_reversal_v1_manifest import (
    reversal_universe_definition,
)
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text

COVERAGE_FIELDS = (
    "session",
    "symbol",
    "daily_universe_rank",
    "timeframe",
    "status",
    "reason",
    "expected_bars",
    "native_bars_present",
    "missing_timestamps",
    "first_hour_observable",
)
RANK_BUCKETS = ("WORST_2_PERCENT", "2_TO_5_PERCENT", "5_TO_10_PERCENT")


def simulate_prepared_reversal(prepared, native_sessions):
    """Zero SQL: one frozen session's cross-section in RAM, backed by a bounded spool."""
    if not prepared.ready:
        raise ValueError("REVERSAL_DATA_UNAVAILABLE: qualify Daily and native requirements first")
    coverage = {(row["symbol"], row["session"]): row for row in prepared.coverage}
    grouped = {
        day: list(rows) for day, rows in groupby(prepared.candidates, key=lambda row: row.session)
    }
    events, sessions = [], []
    for session in prepared.sessions:
        candidates = grouped.get(session, [])
        native = {row.symbol: native_sessions.entry_session(*row.key)[0] for row in candidates}
        rows, diagnostic = rank_first_hour(candidates, native)
        if diagnostic is None:
            diagnostic = dict.fromkeys(SESSION_FIELDS)
            diagnostic.update(
                session=session.isoformat(),
                eligible_symbol_sessions=0,
                observable_first_hour_symbol_sessions=0,
                cross_section_size=0,
                status="SESSION_UNOBSERVABLE_CROSS_SECTION",
                signals=0,
                executed_trades=0,
            )
        for row in rows:
            observation = coverage[row["symbol"], row["session"]]
            event = execute_signal(
                row,
                native[row["symbol"]],
                outcome_observable=observation["status"] == "REQUIRED_PRESENT",
            )
            events.append(event)
            diagnostic["executed_trades"] += event["net_return"] is not None
        sessions.append(diagnostic)
    return events, sessions


def trade_metrics(trades):
    result = {"executed_trades": len(trades)}
    for label in ("gross", "net"):
        values = [row[f"{label}_return"] for row in trades]
        wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
        result.update(
            {
                f"{label}_expectancy": mean(values) if values else None,
                f"median_{label}_return": median(values) if values else None,
                f"{label}_profit_factor": sum(wins) / -sum(losses) if losses else None,
            }
        )
        if label == "net":
            result.update(
                win_rate=len(wins) / len(values) if values else None,
                average_winner=mean(wins) if wins else None,
                average_loser=mean(losses) if losses else None,
                total_net_return_unit_notional=sum(values),
            )
    for field in (
        "first_hour_return",
        "cross_section_percentile",
        "MFE",
        "MAE",
        "holding_minutes",
        "modeled_cost",
    ):
        values = [row[field] for row in trades]
        result[f"mean_{field}"] = mean(values) if values else None
        result[f"median_{field}"] = median(values) if values else None
    result["modeled_cost_drag"] = result["mean_modeled_cost"]
    return result


def rank_bucket(row):
    percentile = row["cross_section_percentile"]
    return (
        RANK_BUCKETS[0]
        if percentile <= 0.02
        else RANK_BUCKETS[1]
        if percentile <= 0.05
        else RANK_BUCKETS[2]
    )


def build_diagnostics(prepared, events, sessions):
    trades = [row for row in events if row["net_return"] is not None]
    metrics = trade_metrics(trades)
    metrics.update(
        eligible_symbol_sessions=len(events),
        observable_first_hour_symbol_sessions=sum(
            row["first_hour_return"] is not None for row in events
        ),
        cross_section_qualified_sessions=sum(
            row["status"] == "CROSS_SECTION_QUALIFIED" for row in sessions
        ),
        unobservable_cross_section_sessions=sum(
            row["status"] == "SESSION_UNOBSERVABLE_CROSS_SECTION" for row in sessions
        ),
        signals=sum(row["signal"] for row in events),
        terminal_status_counts=dict(Counter(row["status"] for row in events)),
    )
    thirds = {
        day.isoformat(): f"THIRD_{min(index * 3 // len(prepared.sessions), 2) + 1}"
        for index, day in enumerate(prepared.sessions)
    }
    tables = {}
    for name, key, labels in (
        (
            "monthly",
            lambda row: row["session"][:7],
            sorted({str(day)[:7] for day in prepared.sessions}),
        ),
        (
            "yearly",
            lambda row: row["session"][:4],
            sorted({str(day.year) for day in prepared.sessions}),
        ),
        (
            "chronological_subperiods",
            lambda row: thirds[row["session"]],
            [f"THIRD_{i}" for i in (1, 2, 3)],
        ),
        (
            "symbol_concentration",
            lambda row: row["symbol"],
            sorted({row["symbol"] for row in trades}),
        ),
        ("signal_rank_buckets", rank_bucket, RANK_BUCKETS),
    ):
        groups = defaultdict(list)
        for row in trades:
            groups[key(row)].append(row)
        tables[name] = [{"group": label, **trade_metrics(groups[label])} for label in labels]
    for row in tables["chronological_subperiods"]:
        days = [day for day, group in thirds.items() if group == row["group"]]
        row.update(
            start=days[0] if days else None,
            end=days[-1] if days else None,
            trading_sessions=len(days),
        )
    symbols = sorted(
        tables["symbol_concentration"],
        key=lambda row: (-row["total_net_return_unit_notional"], row["group"]),
    )
    total = metrics["total_net_return_unit_notional"]
    for rank, row in enumerate(symbols, 1):
        row.update(
            rank=rank,
            trade_share=row["executed_trades"] / len(trades),
            share_of_total_net_return=row["total_net_return_unit_notional"] / total
            if total > 0
            else None,
        )
    tables["symbol_concentration"] = symbols
    return trades, metrics, tables


def summary_definition(prepared, config, *, preflight):
    counts = Counter(row["status"] for row in prepared.coverage)
    first_present = sum(row["first_hour_observable"] for row in prepared.coverage)
    warnings = [
        "CURRENT_UNIVERSE_ONLY; NOT_SURVIVORSHIP_CLEAN. Current local Alpaca US_EQUITY membership.",
        "ETFs/ETPs may be included. Direct CRYPTO and UNKNOWN are excluded; "
        "SEC identity is not required.",
        "Alpaca tradable=true is a data/research property and does not prove German/EU "
        "broker purchase eligibility. Execution eligibility is NOT_EVALUATED.",
        "Signal-level unit-notional research; no portfolio allocator, compounding "
        "or automatic champion promotion.",
        "Diagnostics do not filter signals. Positive results do not automatically "
        "promote this family to champion or live trading.",
    ]
    if config.universe.market_data_feed.lower() == "iex":
        warnings.append(
            "IEX is a single-venue feed, not consolidated market coverage; "
            "results depend on this observation scope."
        )
    return {
        "research_family": REVERSAL_V1.research_family,
        "research_id": REVERSAL_V1.research_id,
        "status": REVERSAL_V1.status,
        "mode": "PREFLIGHT" if preflight else "VALIDATION",
        "requested_start": prepared.requested_start.isoformat(),
        "requested_end": prepared.requested_end.isoformat(),
        "candidate_count": len(prepared.candidates),
        "candidate_source": prepared.candidate_source,
        "candidate_manifest_path": prepared.candidate_manifest_path,
        "candidate_manifest_fingerprint": prepared.candidate_manifest_fingerprint,
        "strategy_definition": asdict(REVERSAL_V1),
        "universe_definition": reversal_universe_definition(config),
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "extended_hours": False,
        "survivorship_clean": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
        "daily_qualification": prepared.daily_qualification,
        "ready_for_local_validation": prepared.ready,
        "coverage": {
            "eligible_symbol_sessions": len(prepared.candidates),
            "observable_first_hour_symbol_sessions": first_present,
            "status_counts": {
                status: counts[status]
                for status in (
                    "REQUIRED_PRESENT",
                    "LOCAL_MISSING_FETCHABLE",
                    "PROVIDER_CONFIRMED_ABSENT",
                    "PROVIDER_CHECK_FAILED",
                )
            },
        },
        "metrics": {},
        "stability": {},
        "performance": prepared.performance,
        "warnings": warnings,
        "metric_definitions": {
            "first_hour_return": (
                "close of 10:15-10:30 bar / open of 09:30-09:45 bar - 1; "
                "decision at fourth completion"
            ),
            "cross_section_percentile": (
                "1-based first-hour return rank / observable cross-section size; fractions"
            ),
            "returns": (
                "gross = exit_reference/entry_reference - 1; net = exit_fill/entry_fill - 1; "
                "unit entry notional, no compounding"
            ),
            "costs": (
                "entry_fill = entry_reference * 1.0005; exit_fill = exit_reference * 0.9995; "
                "commission zero; modeled_cost = gross - net"
            ),
            "profit_factor": (
                "sum positive returns / abs(sum negative returns); null without losses"
            ),
            "MFE_MAE": (
                "Reference-price excursions over all holding bars including entry and exit bars, "
                "clamped around zero; fractions"
            ),
            "holding_minutes": (
                "Final actual native bar completion minus next actual native entry open; "
                "XNYS early closes respected"
            ),
            "chronological_thirds": (
                "Three contiguous groups of requested XNYS sessions, including zero-trade sessions"
            ),
            "symbol_concentration": (
                "Sum independent unit-notional net returns; signed contribution shares null "
                "when aggregate <= 0; trade shares also reported"
            ),
            "market_regime": (
                "Session first-hour mean, median and population standard deviation; "
                "explanatory only"
            ),
            "partial_absence": (
                "First-hour-observable candidates remain ranked despite later absence. "
                "Selected incomplete outcomes are unexecuted; no replacement"
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
        raise ValueError("Reversal output stem must be a plain filename stem")
    names = (
        ("summary", "coverage", "intraday_requirements", "reversal_candidates")
        if preflight
        else (
            "summary",
            "coverage",
            "reversal_events",
            "trades",
            "sessions",
            "monthly",
            "yearly",
            "chronological_subperiods",
            "symbol_concentration",
            "signal_rank_buckets",
        )
    )
    json_names = {"summary", "intraday_requirements", "reversal_candidates"}
    paths = {
        name: Path(directory) / f"{stem}_{name}.{'json' if name in json_names else 'csv'}"
        for name in names
    }
    existing = next((path for path in paths.values() if path.exists()), None)
    if existing:
        raise FileExistsError(f"Reversal output already exists: {existing}")
    return paths


def run_intraday_reversal_v1(
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
            raise ValueError("Reversal preflight always discovers candidates")
    elif (candidate_manifest is not None) == rediscover_candidates:
        raise ValueError(
            "Reversal validation requires exactly one candidate manifest or explicit rediscovery"
        )
    paths = output_paths(directory, stem, preflight=preflight)
    with nullcontext() if preflight else NativeEntrySessions() as native:
        with ProgressPhase("intraday_reversal_v1_progress", "local_data_qualification"):
            prepared = prepare_reversal_data(
                database,
                config,
                start,
                end,
                native_sessions=native,
                candidate_manifest=candidate_manifest,
            )
        if preflight:
            manifest = build_manifest(prepared, config)
            prepared.candidate_manifest_path = str(paths["reversal_candidates"])
            prepared.candidate_manifest_fingerprint = manifest["fingerprint"]
        summary = summary_definition(prepared, config, preflight=preflight)
        tables = {"coverage": prepared.coverage}
        if not preflight:
            started = perf_counter()
            with ProgressPhase("intraday_reversal_v1_progress", "simulation"):
                events, sessions = simulate_prepared_reversal(prepared, native)
            prepared.performance.update(
                reversal_simulation_seconds=perf_counter() - started,
                sqlite_query_count_reversal_simulation=0,
                native_cache_peak=native.peak_cache_size,
            )
            started = perf_counter()
            trades, metrics, diagnostics = build_diagnostics(prepared, events, sessions)
            tables.update(reversal_events=events, trades=trades, sessions=sessions, **diagnostics)
            summary.update(metrics=metrics, stability=diagnostics)
            prepared.performance["diagnostics_seconds"] = perf_counter() - started
        Path(directory).mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            fields = (
                EVENT_FIELDS
                if name in {"reversal_events", "trades"}
                else COVERAGE_FIELDS
                if name == "coverage"
                else SESSION_FIELDS
                if name == "sessions"
                else tuple(rows[0])
                if rows
                else ("group", *trade_metrics([]))
            )
            if name == "symbol_concentration" and not rows:
                fields += ("rank", "trade_share", "share_of_total_net_return")
            _atomic_csv(paths[name], rows, list(fields))
        if preflight:
            _atomic_text(
                paths["reversal_candidates"], json.dumps(manifest, indent=2, allow_nan=False)
            )
            _atomic_text(
                paths["intraday_requirements"],
                json.dumps(
                    reversal_requirements_report(prepared, config), indent=2, allow_nan=False
                ),
            )
        _atomic_text(paths["summary"], json.dumps(summary, indent=2, allow_nan=False))
    return summary, paths
