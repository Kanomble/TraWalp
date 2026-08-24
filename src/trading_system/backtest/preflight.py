"""Read-only preparation diagnostics for long local strategy comparisons."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from trading_system.backtest.coverage import expected_native_15m_timestamps
from trading_system.backtest.engine import (
    StrategyComparisonPreparation,
    assess_comparison_intraday_coverage,
    prepare_strategy_comparison,
)
from trading_system.backtest.first_hour_pullback import plan_first_hour_pullback
from trading_system.backtest.qualification import (
    BENCHMARK_SYMBOL,
    IDENTITY_CONFLICT_SAMPLE_LIMIT,
    qualify_historical_screen_start,
)
from trading_system.backtest.report import _atomic_text
from trading_system.backtest.research_registry import (
    comparison_strategy_label,
    research_family_runs,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    regular_session_bounds,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.qualification import (
    provider_range_verified,
    qualify_daily_history,
)
from trading_system.models.backtest import StrategyComparisonKind
from trading_system.models.market_data import BarTimeframe, DailyBar


def build_compare_preflight(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    comparison_kind: StrategyComparisonKind,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Qualify and discover candidates without calling a backtest or the network."""

    if start > end:
        raise ValueError("Preflight start must not be after end")
    runs = research_family_runs(comparison_kind)
    labels = [
        comparison_strategy_label(comparison_kind, variant, preset)
        for variant, preset in runs
    ]
    company_symbols = {company.symbol for company in database.list_tradable_companies()}
    identity_conflicts = database.unresolved_sec_identity_conflict_symbols()
    excluded_conflicts = sorted((company_symbols - {BENCHMARK_SYMBOL}) & identity_conflicts)
    symbols_before_exclusions = company_symbols | {BENCHMARK_SYMBOL}
    symbols = sorted((company_symbols - identity_conflicts) | {BENCHMARK_SYMBOL})
    required_warmup = required_daily_warmup_sessions(config)
    daily = qualify_daily_history(
        database,
        symbols,
        start,
        end,
        warmup_sessions=required_warmup,
    )
    start_qualification = qualify_historical_screen_start(
        database,
        config,
        start,
        end,
        allow_start_shift=False,
    )
    official = trading_sessions_between(start, end)
    required_last = official[-1] if official else end
    local_requested_sessions = set(database.bar_sessions(start, end))
    requested_period_end_present = required_last in local_requested_sessions
    global_failure_reasons = list(start_qualification["failure_reasons"])
    if not requested_period_end_present:
        global_failure_reasons.append(
            f"required final XNYS session {required_last} is not represented by any local Daily bar"
        )
    daily_ready = not global_failure_reasons
    daily_global_readiness = {
        "required_warmup_sessions": required_warmup,
        "required_warmup_start": daily.qualification_start.isoformat(),
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_warmup_complete": start_qualification["benchmark_warmup_complete"],
        "benchmark_missing_warmup_sessions": start_qualification[
            "benchmark_missing_warmup_sessions"
        ],
        "requested_period_end_session": required_last.isoformat(),
        "requested_period_end_present": requested_period_end_present,
        "screenable_symbol_count_at_start": start_qualification[
            "screenable_symbol_count_at_start"
        ],
        "ready": daily_ready,
        "failure_reasons": global_failure_reasons,
    }
    preparation: StrategyComparisonPreparation | None = None
    discovery_error: str | None = None
    if daily_ready:
        try:
            preparation = prepare_strategy_comparison(
                database,
                config,
                start,
                end,
                comparison_kind=comparison_kind,
            )
        except ValueError as exc:
            discovery_error = str(exc)

    intraday = (
        _intraday_preflight(database, config, preparation)
        if preparation is not None
        else _empty_intraday_preflight(config)
    )
    discovery_status = "COMPLETE" if preparation is not None else "NOT_COMPLETE"
    ready = bool(preparation is not None and daily_ready and intraday["intraday_ready"])
    coverage_state = database.sync_values("daily_history_coverage")
    required_daily_command, required_daily_reason = _daily_sync_recommendation(
        daily_qualification_start=daily.qualification_start,
        end=end,
        daily_global_readiness=daily_global_readiness,
        discovery_error=discovery_error,
        coverage_state=coverage_state,
        qualification_symbols=symbols,
        provider_feed=config.universe.market_data_feed,
        provider_adjustment=config.universe.market_data_adjustment,
    )
    candidate_report = _candidate_report(
        start,
        end,
        comparison_kind,
        preparation,
        intraday,
        discovery_error,
    )
    candidate_discovery = {
        "status": discovery_status,
        "candidate_symbols": intraday["candidate_symbols"],
        "candidate_sessions": intraday["candidate_sessions"],
        "discovery_error": discovery_error,
    }
    daily_symbol_diagnostics = {
        "symbols_considered": len(symbols_before_exclusions),
        "identity_conflicts_excluded": len(excluded_conflicts),
        "identity_conflict_symbols": excluded_conflicts[:IDENTITY_CONFLICT_SAMPLE_LIMIT],
        "qualification_symbol_count_after_exclusions": len(symbols),
        "symbols_with_complete_initial_warmup": start_qualification[
            "symbols_with_complete_initial_warmup"
        ],
        "symbols_rejected_initially_for_insufficient_history": start_qualification[
            "symbols_rejected_initially_for_insufficient_history"
        ],
        "symbols_with_internal_gaps": daily.symbols_with_internal_gaps,
        "internal_missing_sessions": daily.internal_missing_sessions,
        "symbols_with_edge_or_lifecycle_gaps": daily.symbols_with_edge_or_lifecycle_gaps,
        "edge_or_lifecycle_missing_sessions": daily.edge_or_lifecycle_missing_sessions,
        "provider_range_verified_symbols": daily.provider_range_verified_symbols,
        "structurally_complete_symbols": daily.structurally_complete_symbols,
        "coverage_metadata_mismatches": daily.coverage_metadata_mismatches,
    }
    report: dict[str, Any] = {
        "report_type": "local_compare_preflight",
        "local_only": True,
        "network_accessed": False,
        "backtest_executed": False,
        "requested_period": {"start": start.isoformat(), "end": end.isoformat()},
        "required_daily_history_warmup_start": daily.qualification_start.isoformat(),
        "resolved_research_family": comparison_kind.value,
        "strategies": labels,
        "daily_pit_candidate_discovery_status": discovery_status,
        "daily_candidate_discovery_error": discovery_error,
        "candidate_discovery": candidate_discovery,
        "daily_qualification": daily.model_dump(mode="json"),
        "daily_global_readiness": daily_global_readiness,
        "daily_symbol_diagnostics": daily_symbol_diagnostics,
        "daily_coverage_semantics": {
            "provider_range_verified": (
                "the provider interval was checked successfully, including valid empty responses"
            ),
            "structural_session_complete": (
                "a native Daily bar is stored for every expected XNYS session"
            ),
            "coverage_metadata_mismatches_are_diagnostic": True,
        },
        "daily_ready": daily_ready,
        "intraday": intraday,
        "dataset_ready_for_local_compare": ready,
        "candidate_trade_path_gaps": {
            "status": "NOT_KNOWABLE_WITHOUT_EXECUTED_POSITIONS",
            "note": "Preflight does not simulate entries or exits.",
        },
        "recommended_manual_sync_daily_history_command": required_daily_command,
        "recommended_manual_sync_daily_history_reason": required_daily_reason,
        "recommended_manual_sync_intraday_command": None,
        "methodology": {
            "period_label": "historical_extension",
            "development_reference_start": "2025-05-01",
            "development_notice": (
                "2025-05-01 onward has informed hypothesis construction and is development "
                "research, not automatically out-of-sample evidence."
            ),
            "synthetic_bars": False,
            "per_symbol_insufficient_history_policy": (
                "production PIT screening rejects the symbol for that screen"
            ),
            "automatic_strategy_promotion": False,
        },
    }
    return report, candidate_report


def export_compare_preflight(
    report: dict[str, Any],
    candidate_report: dict[str, Any],
    output_directory: Path,
    *,
    stem: str,
) -> dict[str, Path]:
    """Write a non-overwriting preflight and sync-compatible candidate report."""

    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "preflight": output_directory / f"{stem}.json",
        "intraday_candidates": output_directory / f"{stem}_intraday_candidates.json",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Compare preflight export already exists: {existing[0]}")
    intraday_command = None
    if (
        candidate_report["discovery_complete"]
        and not report["intraday"]["intraday_ready"]
        and candidate_report["candidate_symbols"]
    ):
        intraday_command = (
            ".\\.venv\\Scripts\\python.exe -m trading_system.cli sync-intraday "
            f"--start {report['requested_period']['start']} "
            f"--end {report['requested_period']['end']} --timeframes 15m "
            f"--candidates-report {paths['intraday_candidates']}"
        )
    report = {**report, "recommended_manual_sync_intraday_command": intraday_command}
    _atomic_text(paths["preflight"], json.dumps(report, indent=2))
    _atomic_text(paths["intraday_candidates"], json.dumps(candidate_report, indent=2))
    return paths


def _daily_sync_recommendation(
    *,
    daily_qualification_start: date,
    end: date,
    daily_global_readiness: dict[str, Any],
    discovery_error: str | None,
    coverage_state: dict[str, Any],
    qualification_symbols: list[str],
    provider_feed: str,
    provider_adjustment: str,
) -> tuple[str | None, str]:
    """Recommend a Daily fetch only when it can address the blocking condition."""

    command = (
        ".\\.venv\\Scripts\\python.exe -m trading_system.cli sync-daily-history "
        f"--start {daily_qualification_start} --end {end}"
    )
    if not daily_global_readiness["benchmark_warmup_complete"]:
        return command, "SPY is missing one or more required benchmark warmup sessions."
    if not daily_global_readiness["requested_period_end_present"]:
        return command, "The required final research session is not represented locally."
    if daily_global_readiness["screenable_symbol_count_at_start"] == 0:
        unverified = [
            symbol
            for symbol in qualification_symbols
            if not provider_range_verified(
                coverage_state.get(symbol),
                daily_qualification_start,
                end,
                feed=provider_feed,
                adjustment=provider_adjustment,
            )
        ]
        if unverified:
            return (
                command,
                "No eligible symbol has the required initial history and the provider range has "
                f"not been verified for {len(unverified)} qualification symbols.",
            )
        return (
            None,
            "No useful Daily re-sync is known: required provider ranges are already verified; "
            "lifecycle/provider-native absence is not repairable by requesting the same range.",
        )
    if discovery_error is not None and _indicates_missing_daily_history(discovery_error):
        return (
            command,
            f"Candidate preparation reported unavailable Daily history: {discovery_error}",
        )
    if discovery_error is not None:
        return (
            None,
            "Candidate preparation failed, but its error does not identify a Daily range that a "
            f"re-sync can repair: {discovery_error}",
        )
    return (
        None,
        "Daily global readiness is satisfied; lifecycle, internal-gap, and coverage-metadata "
        "diagnostics do not by themselves require a re-sync.",
    )


def _indicates_missing_daily_history(error: str) -> bool:
    normalized = error.casefold()
    return any(
        phrase in normalized
        for phrase in (
            "daily history",
            "market history",
            "local market sessions",
            "local bar coverage",
        )
    )


def _intraday_preflight(
    database: Database,
    config: StrategyConfig,
    preparation: StrategyComparisonPreparation,
) -> dict[str, Any]:
    assessments = assess_comparison_intraday_coverage(
        database, preparation.intraday_requirements
    )
    details: list[dict[str, Any]] = []
    candidate_symbols: set[str] = set()
    candidate_sessions: set[tuple[str, date]] = set()
    missing_entries = 0
    missing_first_hours = 0
    missing_f5_execution = 0
    insufficient_warmup = 0
    for assessment in assessments:
        requirement = assessment.requirement
        candidate_symbols.update(requirement.symbols)
        candidate_sessions.update(requirement.candidate_execution_sessions)
        bars = database.bars_between(
            requirement.symbols,
            requirement.requested_start,
            requirement.requested_end,
            timeframe=requirement.timeframe,
        )
        by_symbol: dict[str, list[DailyBar]] = defaultdict(list)
        for bar in bars:
            by_symbol[bar.symbol].append(bar)
        for symbol, session in requirement.candidate_execution_sessions:
            opening, closing = regular_session_bounds(session)
            symbol_bars = sorted(by_symbol[symbol], key=lambda item: item.timestamp)
            session_bars = [
                bar for bar in symbol_bars if opening <= bar.timestamp < closing
            ]
            prior = [bar for bar in symbol_bars if bar.timestamp < opening]
            entry_present = bool(session_bars)
            warmup_count = len(prior)
            warmup_sufficient = warmup_count >= requirement.warmup_bars
            expected_first_hour = set(expected_native_15m_timestamps(session)[:4])
            present = {bar.timestamp for bar in session_bars}
            missing_first_hour = sorted(expected_first_hour - present)
            plan = plan_first_hour_pullback(session, session_bars, prior)
            execution_missing = plan.failure_reason == "missing_pullback_execution_bar"
            missing_entries += int(not entry_present)
            missing_first_hours += int(bool(missing_first_hour))
            missing_f5_execution += int(execution_missing)
            insufficient_warmup += int(not warmup_sufficient)
            details.append(
                {
                    "symbol": symbol,
                    "execution_session": session.isoformat(),
                    "entry_bar_present": entry_present,
                    "first_native_entry_timestamp": (
                        session_bars[0].timestamp.isoformat() if session_bars else None
                    ),
                    "missing_required_first_hour_timestamps": [
                        timestamp.isoformat() for timestamp in missing_first_hour
                    ],
                    "f5_entry_plan_status": plan.failure_reason or "EXECUTABLE",
                    "f5_intended_entry_timestamp": (
                        plan.entry_timestamp.isoformat()
                        if plan.entry_timestamp is not None
                        else None
                    ),
                    "warmup_required_native_bars": requirement.warmup_bars,
                    "warmup_available_native_bars": warmup_count,
                    "warmup_sufficient": warmup_sufficient,
                }
            )
    intraday_ready = not any(
        (missing_entries, missing_first_hours, missing_f5_execution, insufficient_warmup)
    )
    return {
        "required_timeframes": sorted(
            {item.requirement.timeframe.value for item in assessments}
        ),
        "regular_session_only": not config.intraday.extended_hours,
        "extended_hours": config.intraday.extended_hours,
        "native_warmup_bars": config.intraday.warmup_bars,
        "candidate_symbols": sorted(candidate_symbols),
        "candidate_symbol_count": len(candidate_symbols),
        "candidate_sessions": len(candidate_sessions),
        "missing_candidate_entry_opportunities": missing_entries,
        "missing_required_first_hour_f5_sessions": missing_first_hours,
        "missing_f5_pullback_execution_bars": missing_f5_execution,
        "insufficient_native_warmup_sessions": insufficient_warmup,
        "structural_coverage": [
            {
                "timeframe": item.requirement.timeframe.value,
                "candidate_symbols": len(item.requirement.symbols),
                "structurally_complete_symbols": len(item.complete_symbols),
                "structurally_incomplete_symbols": len(item.sync_symbols),
                "incomplete_reasons": dict(item.incomplete_reasons),
            }
            for item in assessments
        ],
        "candidate_session_details": details,
        "intraday_ready": intraday_ready,
        "synthetic_bars_created": False,
    }


def _empty_intraday_preflight(config: StrategyConfig) -> dict[str, Any]:
    return {
        "required_timeframes": [BarTimeframe.MINUTES_15.value],
        "regular_session_only": not config.intraday.extended_hours,
        "extended_hours": config.intraday.extended_hours,
        "native_warmup_bars": config.intraday.warmup_bars,
        "candidate_symbols": [],
        "candidate_symbol_count": 0,
        "candidate_sessions": 0,
        "missing_candidate_entry_opportunities": None,
        "missing_required_first_hour_f5_sessions": None,
        "missing_f5_pullback_execution_bars": None,
        "insufficient_native_warmup_sessions": None,
        "structural_coverage": [],
        "candidate_session_details": [],
        "intraday_ready": False,
        "synthetic_bars_created": False,
    }


def _candidate_report(
    start: date,
    end: date,
    comparison_kind: StrategyComparisonKind,
    preparation: StrategyComparisonPreparation | None,
    intraday: dict[str, Any],
    discovery_error: str | None,
) -> dict[str, Any]:
    sessions = (
        sorted(
            {
                (symbol, session)
                for requirement in preparation.intraday_requirements
                for symbol, session in requirement.candidate_execution_sessions
            }
        )
        if preparation is not None
        else []
    )
    return {
        "report_type": "intraday_candidate_requirements",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "research_family": comparison_kind.value,
        "discovery_complete": preparation is not None,
        "candidate_discovery_status": (
            "COMPLETE" if preparation is not None else "NOT_COMPLETE"
        ),
        "discovery_error": discovery_error,
        "timeframes": intraday["required_timeframes"],
        "extended_hours": intraday["extended_hours"],
        "warmup_bars": intraday["native_warmup_bars"],
        "candidate_symbols": [
            {"symbol": symbol} for symbol in intraday["candidate_symbols"]
        ],
        "candidate_sessions": [
            {"symbol": symbol, "execution_session": session.isoformat()}
            for symbol, session in sessions
        ],
    }
