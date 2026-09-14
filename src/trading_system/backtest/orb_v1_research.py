"""Local ORB qualification, signal-level diagnostics and atomic research exports."""

import json
from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from zoneinfo import ZoneInfo

from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.orb_v1 import (
    EVENT_FIELDS,
    ORB_V1,
    OrbStatus,
    event_for,
    simulate_orb_session,
)
from trading_system.backtest.orb_v1_data import orb_requirements_report, prepare_orb_data
from trading_system.backtest.orb_v1_manifest import candidate_manifest as build_candidate_manifest
from trading_system.backtest.orb_v1_manifest import orb_universe_definition
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text

ENTRY_BUCKETS = ("09:45–10:30", "10:30–12:00", "12:00–14:00", "14:00–15:45")
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
)


def simulate_prepared_orb(prepared, native_sessions):
    """Only prepared objects and the owned spool: zero SQL/network access in this loop."""
    if not prepared.ready:
        raise ValueError(
            "ORB_DATA_UNAVAILABLE: qualify Daily warmup and all native requirements first"
        )
    coverage = {(row["symbol"], row["session"]): row for row in prepared.coverage}
    events = []
    for candidate in prepared.candidates:
        observation = coverage[candidate.symbol, candidate.session.isoformat()]
        if observation["status"] == "PROVIDER_CONFIRMED_ABSENT":
            event = event_for(candidate)
            event.update(status=OrbStatus.PROVIDER_CONFIRMED_ABSENT, reason=observation["reason"])
        else:
            bars, _ = native_sessions.entry_session(*candidate.key)
            event = simulate_orb_session(candidate, bars)
        events.append(event)
    return events


def trade_metrics(trades):
    values = [row["net_return"] for row in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    result = {
        "executed_trades": len(trades),
        "win_rate": len(wins) / len(values) if values else None,
        "loss_rate": len(losses) / len(values) if values else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "expectancy": mean(values) if values else None,
        "average_win": mean(wins) if wins else None,
        "average_loss": mean(losses) if losses else None,
        "best_trade": max(values) if values else None,
        "worst_trade": min(values) if values else None,
        "total_net_pnl_unit_notional": sum(values),
        "modeled_execution_cost": sum(row["modeled_execution_cost"] for row in trades),
    }
    for field, label in (
        ("gross_return", "gross_return"),
        ("net_return", "net_return"),
        ("return_R", "R"),
        ("MFE", "MFE"),
        ("MAE", "MAE"),
        ("holding_minutes", "holding_minutes"),
        ("modeled_execution_cost", "modeled_execution_cost"),
    ):
        sample = [row[field] for row in trades]
        result[f"mean_{label}"] = mean(sample) if sample else None
        result[f"median_{label}"] = median(sample) if sample else None
    result["average_holding_minutes"] = result["mean_holding_minutes"]
    return result


def entry_bucket(timestamp):
    local = datetime.fromisoformat(timestamp).astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    if 585 <= minutes < 630:
        return ENTRY_BUCKETS[0]
    if 630 <= minutes < 720:
        return ENTRY_BUCKETS[1]
    if 720 <= minutes < 840:
        return ENTRY_BUCKETS[2]
    if 840 <= minutes <= 945:
        return ENTRY_BUCKETS[3]
    raise ValueError("ORB entry outside frozen regular-session buckets")


def coverage_summary(rows):
    def summarize(group):
        counts = Counter(row["status"] for row in group)
        return {
            "eligible_symbol_sessions": len(group),
            "observable_intraday_symbol_sessions": counts["REQUIRED_PRESENT"],
            "provider_absent_symbol_sessions": counts["PROVIDER_CONFIRMED_ABSENT"],
            "local_missing_fetchable": counts["LOCAL_MISSING_FETCHABLE"],
            "provider_check_failed": counts["PROVIDER_CHECK_FAILED"],
            "coverage_ratio": counts["REQUIRED_PRESENT"] / len(group) if group else None,
        }

    result = summarize(rows)
    for field, key in (("symbol", "coverage_by_symbol"), ("session", "coverage_by_month")):
        groups = defaultdict(list)
        for row in rows:
            groups[row[field] if field == "symbol" else row[field][:7]].append(row)
        result[key] = {label: summarize(group) for label, group in sorted(groups.items())}
    return result


def build_diagnostics(prepared, events):
    trades = [row for row in events if row["net_return"] is not None]
    signal_count = sum(row["signal_status"] == OrbStatus.BREAKOUT_CONFIRMED for row in events)
    counts = Counter(row["status"] for row in events)
    observable = len(events) - counts[OrbStatus.PROVIDER_CONFIRMED_ABSENT]
    metrics = trade_metrics(trades)
    metrics.update(
        eligible_symbol_sessions=len(events),
        observable_intraday_symbol_sessions=observable,
        opening_ranges_available=observable,
        breakout_signals=signal_count,
        no_breakout_sessions=counts[OrbStatus.NO_BREAKOUT],
        no_next_bar_events=counts[OrbStatus.NO_ENTRY_NO_NEXT_BAR],
        invalid_risk_geometry=counts[OrbStatus.INVALID_RISK_GEOMETRY],
        stop_exits=counts[OrbStatus.EXECUTED_STOP],
        session_close_exits=counts[OrbStatus.EXECUTED_SESSION_CLOSE],
        signals_per_day=signal_count / len(prepared.sessions),
        trades_per_day=len(trades) / len(prepared.sessions),
        percentage_of_eligible_sessions_breaking_out=100 * signal_count / len(events)
        if events
        else None,
        percentage_of_observable_sessions_breaking_out=(
            100 * signal_count / observable if observable else None
        ),
        percentage_of_breakouts_stopped=(
            100 * counts[OrbStatus.EXECUTED_STOP] / signal_count if signal_count else None
        ),
    )
    thirds = {
        session.isoformat(): f"THIRD_{min(index * 3 // len(prepared.sessions), 2) + 1}"
        for index, session in enumerate(prepared.sessions)
    }
    tables = {}
    grouping = (
        (
            "monthly",
            lambda row: row["session"][:7],
            sorted({str(s)[:7] for s in prepared.sessions}),
        ),
        (
            "yearly",
            lambda row: row["session"][:4],
            sorted({str(s.year) for s in prepared.sessions}),
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
        ("entry_time_buckets", lambda row: entry_bucket(row["entry_timestamp"]), ENTRY_BUCKETS),
    )
    for table, key, labels in grouping:
        groups = defaultdict(list)
        for row in trades:
            groups[key(row)].append(row)
        tables[table] = [{"group": label, **trade_metrics(groups[label])} for label in labels]
    for row in tables["chronological_subperiods"]:
        period = [session for session, label in thirds.items() if label == row["group"]]
        row.update(
            start=period[0] if period else None,
            end=period[-1] if period else None,
            trading_sessions=len(period),
        )
    symbols = sorted(
        tables["symbol_concentration"],
        key=lambda row: (-row["total_net_pnl_unit_notional"], row["group"]),
    )
    total = metrics["total_net_pnl_unit_notional"]
    for rank, row in enumerate(symbols, 1):
        row.update(
            rank=rank,
            share_of_total_net_pnl=(
                row["total_net_pnl_unit_notional"] / total if total > 0 else None
            ),
        )
    tables["symbol_concentration"] = symbols
    concentration = {
        "unique_traded_symbols": len(symbols),
        **{
            f"top_{count}_symbol_share_of_total_net_pnl": (
                sum(row["total_net_pnl_unit_notional"] for row in symbols[:count]) / total
                if total > 0
                else None
            )
            for count in (1, 5, 10)
        },
    }
    by_day = []
    daily_events = defaultdict(list)
    for event in events:
        daily_events[event["session"]].append(event)
    for session in prepared.sessions:
        day = daily_events[session.isoformat()]
        by_day.append(
            {
                "session": session.isoformat(),
                "signals": sum(row["signal_status"] == OrbStatus.BREAKOUT_CONFIRMED for row in day),
                "trades": sum(row["net_return"] is not None for row in day),
            }
        )
    return trades, metrics, tables, concentration, by_day


def summary_definition(prepared, config, *, preflight):
    warnings = [
        "Historical universe uses current locally stored Alpaca tradable US_EQUITY instruments.",
        "ETFs/ETPs and other Alpaca US_EQUITY instruments may be included; "
        "direct crypto is excluded.",
        "Alpaca tradable=true is a data/research universe property and does not prove that an "
        "instrument can be purchased through a German/EU broker. Execution eligibility is not "
        "evaluated in this research family.",
        "Results are not survivorship-clean.",
        "ORB V1 is a signal-level research study.",
        "No realistic multi-position capital-allocation policy has yet been defined.",
        "Research only; not production-ready. No automatic champion promotion.",
        "The native bars table does not version feed provenance per row; the configured feed "
        "is reported, not independently certified for old local rows.",
        "Historical Daily adjustments are local provider-adjusted history, "
        "not a vintage corporate-action database.",
        "Research periods are not clean OOS unless explicitly established "
        "by a later research decision.",
    ]
    if config.universe.market_data_feed == "iex":
        warnings.append(
            "IEX is a single-venue feed; intraday price/volume observations "
            "may differ from consolidated SIP."
        )
    if any(row["unavailable_daily_windows"] for row in prepared.daily_qualification["sessions"]):
        warnings.append(
            "Symbols without 20 consecutive valid completed Daily bars cannot be qualified; "
            "see daily_qualification for missing-window counts."
        )
    if any(row["status"] == "PROVIDER_CONFIRMED_ABSENT" for row in prepared.coverage):
        warnings.append(
            "Provider-absent selected symbol-sessions remain unobservable and are never replaced; "
            "observed results may be selection-biased."
        )
    return {
        "research_family": ORB_V1.research_family,
        "research_id": ORB_V1.research_id,
        "status": ORB_V1.status,
        "requested_start": prepared.requested_start.isoformat(),
        "requested_end": prepared.requested_end.isoformat(),
        "preflight_only": preflight,
        "candidate_source": prepared.candidate_source,
        "candidate_manifest_path": prepared.candidate_manifest_path,
        "candidate_manifest_fingerprint": prepared.candidate_manifest_fingerprint,
        "candidate_count": len(prepared.candidates),
        "strategy_definition": asdict(ORB_V1),
        "universe_definition": orb_universe_definition(config),
        "opening_range_definition": "One native 09:30–09:45 America/New_York bar: high and low",
        "signal_definition": ORB_V1.signal,
        "entry_definition": ORB_V1.entry,
        "stop_definition": ORB_V1.stop,
        "exit_definition": (
            "Entry bar onward: open <= stop at actual open; else low <= stop at stop; "
            "else final regular native close. Stop precedes final close. "
            "XNYS early closes respected."
        ),
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "consolidated_market_data": config.universe.market_data_feed == "sip",
        "extended_hours": False,
        "survivorship_clean": False,
        "portfolio_strategy_defined": False,
        "automatic_champion_selection": False,
        "daily_qualification": prepared.daily_qualification,
        "ready_for_local_validation": prepared.ready,
        "coverage": coverage_summary(prepared.coverage),
        "metrics": {},
        "stability": {},
        "concentration": {},
        "performance": prepared.performance,
        "warnings": warnings,
        "metric_definitions": {
            "returns": (
                "Fractions: gross = exit_reference/entry_reference - 1; "
                "net = exit_fill/entry_fill - 1. 5 bps on each fill; zero commission."
            ),
            "return_R": "(exit_fill - entry_fill) / (entry_reference - opening_range_low)",
            "range_percentages": (
                "opening_range_width_pct = (high - low) / low; "
                "breakout_distance_pct = (breakout_close - high) / high; both are fractions"
            ),
            "expectancy": (
                "Arithmetic mean net return, equal unit entry notional per independent trade"
            ),
            "pnl_and_concentration": (
                "Sum net returns with one unit entry notional per trade; no capital allocation "
                "or compounding. Shares are null when total net PnL <= 0; signed shares can "
                "exceed 100% when other symbols lose."
            ),
            "modeled_execution_cost": (
                "Sum(gross_return - net_return); mean cost drag also reported"
            ),
            "MFE_MAE": (
                "Reference-price return excursions from entry, clamped around zero; stop-bar "
                "extrema excluded when their ordering relative to exit is unknown. "
                "Conservative observed excursions, not tick-exact extrema."
            ),
            "holding_minutes": (
                "Intrabar stop time estimated by stop-bar start (possibly zero minutes); "
                "exact open/close time otherwise. See exit_timestamp_semantics."
            ),
            "chronological_thirds": (
                "Three contiguous groups of requested XNYS sessions, "
                "including sessions with zero trades"
            ),
            "event_rows": (
                "One terminal row per eligible symbol-session; signal_status retains "
                "BREAKOUT_CONFIRMED for consumed attempts"
            ),
            "percentages": (
                "Explicit percentage_* metrics use 0–100; win/loss rates and coverage use fractions"
            ),
        },
    }


def orb_output_paths(directory, stem, *, preflight):
    if (
        not stem
        or Path(stem).name != stem
        or any(char in stem for char in "/\\:")
        or stem in {".", ".."}
    ):
        raise ValueError("ORB output stem must be a plain filename stem")
    names = (
        ("summary", "coverage", "intraday_requirements", "orb_candidates")
        if preflight
        else (
            "summary",
            "orb_events",
            "trades",
            "monthly",
            "yearly",
            "chronological_subperiods",
            "symbol_concentration",
            "entry_time_buckets",
            "coverage",
        )
    )
    json_reports = {"summary", "intraday_requirements", "orb_candidates"}
    paths = {
        name: Path(directory) / f"{stem}_{name}.{'json' if name in json_reports else 'csv'}"
        for name in names
    }
    existing = next((path for path in paths.values() if path.exists()), None)
    if existing:
        raise FileExistsError(f"ORB output already exists: {existing}")
    return paths


def run_orb_v1(
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
    paths = orb_output_paths(directory, stem, preflight=preflight)
    if preflight:
        if candidate_manifest is not None or rediscover_candidates:
            raise ValueError("ORB preflight always discovers candidates")
    elif (candidate_manifest is not None) == rediscover_candidates:
        raise ValueError(
            "ORB validation requires exactly one candidate manifest or explicit rediscovery"
        )
    with nullcontext() if preflight else NativeEntrySessions() as native:
        with ProgressPhase("orb_v1_progress", "local_data_qualification"):
            prepared = prepare_orb_data(
                database,
                config,
                start,
                end,
                native_sessions=native,
                candidate_manifest=candidate_manifest,
            )
        if preflight:
            manifest = build_candidate_manifest(prepared, config)
            prepared.candidate_manifest_path = str(paths["orb_candidates"])
            prepared.candidate_manifest_fingerprint = manifest["fingerprint"]
        summary = summary_definition(prepared, config, preflight=preflight)
        tables = {"coverage": prepared.coverage}
        if preflight:
            requirements = orb_requirements_report(prepared, config)
        else:
            started = perf_counter()
            with ProgressPhase("orb_v1_progress", "simulation"):
                events = simulate_prepared_orb(prepared, native)
            prepared.performance.update(
                orb_simulation_seconds=perf_counter() - started,
                sqlite_query_count_orb_simulation=0,
                native_cache_peak=native.peak_cache_size,
            )
            started = perf_counter()
            trades, metrics, diagnostics, concentration, by_day = build_diagnostics(
                prepared, events
            )
            tables.update(orb_events=events, trades=trades, **diagnostics)
            summary.update(
                metrics=metrics,
                stability={**diagnostics, "signals_and_trades_by_day": by_day},
                concentration=concentration,
            )
            prepared.performance["diagnostics_seconds"] = perf_counter() - started
        Path(directory).mkdir(parents=True, exist_ok=True)
        for name, rows in tables.items():
            fields = (
                EVENT_FIELDS
                if name in {"orb_events", "trades"}
                else COVERAGE_FIELDS
                if name == "coverage"
                else tuple(rows[0])
                if rows
                else ("group", *trade_metrics([]))
            )
            if name == "symbol_concentration" and not rows:
                fields += ("rank", "share_of_total_net_pnl")
            _atomic_csv(paths[name], rows, list(fields))
        if preflight:
            _atomic_text(paths["orb_candidates"], json.dumps(manifest, indent=2, allow_nan=False))
            _atomic_text(
                paths["intraday_requirements"], json.dumps(requirements, indent=2, allow_nan=False)
            )
        _atomic_text(paths["summary"], json.dumps(summary, indent=2, allow_nan=False))
    return summary, paths
