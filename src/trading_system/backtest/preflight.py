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
from trading_system.data.qualification import qualify_daily_history
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
    symbols = sorted(
        {company.symbol for company in database.list_tradable_companies()} | {"SPY"}
    )
    daily = qualify_daily_history(
        database,
        symbols,
        start,
        end,
        warmup_sessions=required_daily_warmup_sessions(config),
    )
    first_daily, last_daily = database.bar_date_bounds()
    official = trading_sessions_between(start, end)
    required_last = official[-1] if official else end
    daily_ready = bool(
        daily.bars_present
        and first_daily is not None
        and first_daily <= daily.qualification_start
        and last_daily is not None
        and last_daily >= required_last
        and daily.internal_missing_sessions == 0
    )
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
            daily_ready = False

    intraday = (
        _intraday_preflight(database, config, preparation)
        if preparation is not None
        else _empty_intraday_preflight(config)
    )
    ready = bool(daily_ready and intraday["intraday_ready"])
    required_daily_command = None
    if not daily_ready:
        required_daily_command = (
            ".\\.venv\\Scripts\\python.exe -m trading_system.cli sync-daily-history "
            f"--start {daily.qualification_start} --end {end}"
        )
    candidate_report = _candidate_report(
        start,
        end,
        comparison_kind,
        preparation,
        intraday,
    )
    report: dict[str, Any] = {
        "report_type": "local_compare_preflight",
        "local_only": True,
        "network_accessed": False,
        "backtest_executed": False,
        "requested_period": {"start": start.isoformat(), "end": end.isoformat()},
        "required_daily_history_warmup_start": daily.qualification_start.isoformat(),
        "resolved_research_family": comparison_kind.value,
        "strategies": labels,
        "daily_pit_candidate_discovery_status": (
            "COMPLETE" if preparation is not None else "NOT_COMPLETE"
        ),
        "daily_candidate_discovery_error": discovery_error,
        "daily_qualification": daily.model_dump(mode="json"),
        "daily_ready": daily_ready,
        "intraday": intraday,
        "dataset_ready_for_local_compare": ready,
        "candidate_trade_path_gaps": {
            "status": "NOT_KNOWABLE_WITHOUT_EXECUTED_POSITIONS",
            "note": "Preflight does not simulate entries or exits.",
        },
        "recommended_manual_sync_daily_history_command": required_daily_command,
        "recommended_manual_sync_intraday_command": None,
        "methodology": {
            "period_label": "historical_extension",
            "development_reference_start": "2025-05-01",
            "development_notice": (
                "2025-05-01 onward has informed hypothesis construction and is development "
                "research, not automatically out-of-sample evidence."
            ),
            "synthetic_bars": False,
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
    if not report["intraday"]["intraday_ready"] and candidate_report["candidate_symbols"]:
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
