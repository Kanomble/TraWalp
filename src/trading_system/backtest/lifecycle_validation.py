"""Fixed, local-only lifecycle and entry-quality research runners and fresh exports."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from trading_system.backtest.capacity_validation import (
    _single_result_comparison,
    _validate_frozen_control_config,
)
from trading_system.backtest.engine import (
    BacktestEngine,
    _backtest_sessions,
    prepare_strategy_comparison,
)
from trading_system.backtest.entry_quality import (
    F_INTRADAY_ENTRY_RESEARCH_FAMILY,
    F_INTRADAY_ENTRY_VARIANTS,
    EntryQualityStatus,
    missing_session_timestamps,
    opening_weakness_decision,
)
from trading_system.backtest.f_candidates import (
    CandidateManifest,
    ManifestScreenSource,
    ReplaySpool,
    compact_record,
    data_fingerprint,
    load_manifest,
    manifest_metadata,
    replay_session,
)
from trading_system.backtest.lifecycle import (
    F_LIFECYCLE_RESEARCH_FAMILY,
    F_LIFECYCLE_VARIANTS,
    lifecycle_strategy_config,
)
from trading_system.backtest.lifecycle_diagnostics import LifecycleDiagnostics
from trading_system.backtest.peer_context import PEER_MEMBERSHIP_BASIS, TechnicalPeerContextProvider
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.progress import ProgressPhase, ResearchProgress, log_completion
from trading_system.backtest.qualification import qualify_historical_screen_start
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.backtest.universe_provenance import audit_universe_provenance
from trading_system.backtest.validation import (
    CANONICAL_COST_STRESS_CASES,
    _cost_row,
    _field_union,
    calendar_stability,
    chronological_subperiod_analysis,
    strategy_summary,
    symbol_and_leave_one_out,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    StrategyComparisonKind,
)
from trading_system.models.market_data import BarTimeframe

DIAGNOSTIC_FIELDS = {
    "holding_duration_analysis": [
        "position_id",
        "symbol",
        "holding_period",
        "positions_closed_before_10",
        "positions_closed_day_10",
        "positions_extended_after_10",
        "positions_reaching_15",
        "positions_reaching_20",
        "positions_reaching_30",
        "return_day_10",
        "MFE_after_day_10",
        "MAE_after_day_10",
        "final_return",
    ],
    "trend_health_events": [
        "position_id",
        "symbol",
        "session",
        "holding_day",
        "trend_health",
        "peer_state",
        "holding_extended",
        "profit_target_deferred",
    ],
    "dynamic_profit_events": [
        "symbol",
        "signal_date",
        "entry_date",
        "session",
        "holding_day",
        "profit_target_price",
        "profit_target_reached",
        "trend_health",
        "peer_state",
        "profit_target_deferred",
        "eventual_exit_date",
        "eventual_exit_reason",
        "return_at_original_target",
        "final_return",
        "additional_return_after_deferral",
        "MFE_after_original_target",
        "MAE_after_original_target",
    ],
    "peer_context": [
        "symbol",
        "signal_date",
        "session",
        "observation_phase",
        "peer_state",
        "peer_count_valid",
        "peer_above_sma20_ratio",
        "peer_positive_1d_ratio",
        "peer_positive_5d_ratio",
        "peer_median_1d_return",
        "peer_median_5d_return",
        "peer_best_1d_return",
        "peer_worst_1d_return",
        "stock_5d_return",
        "relative_strength_vs_peers_5d",
        "signal_peer_state",
        "entry_peer_state",
        "exit_peer_state",
    ],
    "peer_summary": ["peer_state", "potential_entries", "executed_positions", "expectancy"],
    "peer_spillover": [
        "symbol",
        "signal_date",
        "largest_peer_1d_return",
        "largest_peer_5d_return",
        "median_peer_1d_return",
        "median_peer_5d_return",
        "peer_dispersion",
        "largest_peer_move_previous_session",
        "candidate_return_next_session",
        "candidate_return_next_5_sessions",
    ],
    "correlation": [
        "symbol",
        "signal_date",
        "mean_correlation_to_open_positions",
        "max_correlation_to_open_positions",
        "correlation_pairs_valid",
    ],
    "entry_gap_analysis": [
        "symbol",
        "signal_date",
        "signal_close",
        "next_open",
        "gap_return",
        "ATR",
        "gap_in_ATR",
        "candidate_rank",
        "position_result",
        "MFE",
        "MAE",
        "holding_period",
        "exit_reason",
    ],
    "entry_gap_summary": ["gap_group", "potential_entries", "executed_positions", "expectancy"],
    "entry_quality_events": [
        "symbol",
        "signal_date",
        "entry_session",
        "status",
        "decision_timestamp",
        "actual_entry_timestamp",
        "last_15m_close",
        "session_vwap_to_date",
    ],
}
COMMON_TABLES = (
    "summary",
    "metrics",
    "positions",
    "execution_legs",
    "monthly",
    "yearly",
    "chronological_subperiods",
    "symbol_concentration",
    "cost_stress",
)


@dataclass(frozen=True)
class LifecycleResearchBundle:
    family: str
    results: dict[str, BacktestResult]
    tables: dict[str, list[dict]]
    summary: dict


def research_output_paths(directory: Path, stem: str, *, preflight=False, daily_preflight=False):
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("output-stem must be a plain file stem")
    names = (
        [
            "preflight.json",
            "intraday_candidates.json",
            "candidate_replay.jsonl",
            "missing_symbol_sessions.csv",
        ]
        if preflight
        else ["summary.json", *[f"{name}.csv" for name in (*COMMON_TABLES, *DIAGNOSTIC_FIELDS)]]
    )
    if daily_preflight:
        names = [
            "preflight.json",
            "missing_symbol_sessions.csv",
            "daily_requirements.csv",
            "candidate_summary.csv",
        ]
    paths = {name: directory / f"{stem}_{name}" for name in names}
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Research output already exists: {existing[0]}")
    return paths


def _qualify_daily_research(database, config, start, end):
    """Shared frozen-control, warmup and portfolio-session qualification; no screens/run."""
    if not database.path.is_file():
        raise FileNotFoundError(f"Research requires an existing local database: {database.path}")
    _validate_frozen_control_config(config, start, end)
    management = position_management_preset(
        config.position_management,
        FROZEN_CHAMPION_F.preset,
        legacy_max_holding_days=config.backtest.max_holding_days,
    )
    if (
        BarTimeframe(management.bar_timeframe) is not BarTimeframe.DAY_1
        or not management.max_hold.enabled
        or management.max_hold.mode != "hard"
        or management.max_hold.days != 10
    ):
        raise ValueError("Research requires configured Daily hard max hold=10 for the control")
    if any(
        (
            not management.stop_loss.enabled,
            not management.take_profit.enabled,
            management.trailing_stop.enabled,
            management.atr_trailing_stop.enabled,
            management.signal_decay.enabled,
            management.partial_take_profit.enabled,
            management.portfolio_rotation.enabled,
            management.profit_lock.enabled,
        )
    ):
        raise ValueError("Research requires the frozen configured management composition")
    qualification = qualify_historical_screen_start(
        database, config, start, end, allow_start_shift=False
    )
    official = trading_sessions_between(start, end)
    missing = sorted(set(official) - set(database.bar_sessions(start, end)))
    qualification["missing_portfolio_sessions"] = [session.isoformat() for session in missing]
    if missing:
        qualification["failure_reasons"].append(
            "Daily research requires all requested portfolio sessions locally"
        )
    qualification["qualified"] = qualification["ready"] = not qualification["failure_reasons"]
    qualification["failure_reason"] = "; ".join(qualification["failure_reasons"]) or None
    return qualification


def _prepare(database, config, start, end, progress=None):
    with (
        progress.phase("daily_qualification", "daily qualification") if progress else nullcontext()
    ) as phase:
        qualification = _qualify_daily_research(database, config, start, end)
    if progress:
        progress.record("daily_qualification", phase)
    if qualification["failure_reasons"]:
        raise ValueError(
            "Daily qualification failed: " + "; ".join(qualification["failure_reasons"])
        )
    with (
        progress.phase("screen_source_prepare", "preparing PIT screen source")
        if progress
        else nullcontext()
    ) as phase:
        preparation = prepare_strategy_comparison(
            database, config, start, end, comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F
        )
    if progress:
        progress.record("screen_source_prepare", phase)
    return preparation, qualification


def iter_f_candidates(screen_source, config, sessions, *, progress=None, replay_spool=None):
    """Canonical PIT F eligibility/rank, before any allocation or future coverage checks."""
    from trading_system.backtest.engine import evaluate_variant_entry

    discovered = 0
    recent = deque(maxlen=10)
    elapsed = 0.0
    # Bypass the comparison cache only for this streaming API.
    source = getattr(screen_source, "source", screen_source)
    for index, (signal, execution) in enumerate(zip(sessions[:-1], sessions[1:], strict=True), 1):
        started = time.monotonic()
        if hasattr(source, "f_candidates"):
            result = source.f_candidates(signal, config)
            records = result.records
            if replay_spool:
                replay_spool.append(result.replay)
        else:
            records, rejected = [], []
            for record in source.screen(signal).records:
                evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config)
                if evaluation.eligible:
                    records.append((evaluation.score, record))
                elif replay_spool:
                    rejected.append(compact_record(record.symbol, record))
            records.sort(key=lambda pair: (-pair[0], pair[1].symbol))
            if replay_spool:
                replay_spool.append(replay_session(signal, records, rejected))
        for rank, (_, record) in enumerate(records, 1):
            yield signal, execution, rank, record
        discovered += len(records)
        seconds = time.monotonic() - started
        elapsed += seconds
        recent.append(seconds)
        if progress:
            timing = {
                "avg_seconds_per_session": elapsed / index,
                "recent_seconds_per_session": sum(recent) / len(recent),
            }
            if len(recent) >= 3 and sum(recent) > 0:
                timing["estimated_remaining_seconds"] = (
                    (len(sessions) - 1 - index) * sum(recent) / len(recent)
                )
            progress.update(
                sessions_processed=index,
                candidate_sessions_discovered=discovered,
                session=signal.isoformat(),
                **timing,
            )


def build_f_intraday_entry_preflight(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    preparation=None,
    progress=None,
    candidate_manifest=None,
):
    """Discover every eligible F candidate, including capacity-blocked candidates; no backtest."""
    progress = progress or ResearchProgress()
    qualification = {}
    spool = None
    snapshot = None
    if candidate_manifest is None:
        with progress.phase(
            "snapshot_fingerprint", "fingerprinting local discovery inputs"
        ) as phase:
            snapshot = data_fingerprint(database)
        progress.record("snapshot_fingerprint", phase)
    if preparation is None:
        preparation, qualification = _prepare(database, config, start, end, progress)
    candidates, missing, checks = [], [], []
    if candidate_manifest is None:
        spool = ReplaySpool()
        with progress.phase(
            "candidate_discovery",
            "discovering F candidates",
            percentage=("sessions_processed", "sessions_total"),
            sessions_processed=0,
            sessions_total=max(0, len(preparation.sessions) - 1),
            candidate_sessions_discovered=0,
        ) as phase:
            for signal, execution, rank, record in iter_f_candidates(
                preparation.screen_source,
                config,
                preparation.sessions,
                progress=phase,
                replay_spool=spool,
            ):
                from trading_system.backtest.engine import evaluate_variant_entry

                candidates.append(
                    {
                        "symbol": record.symbol,
                        "signal_date": signal.isoformat(),
                        "signal_session": signal.isoformat(),
                        "execution_session": execution.isoformat(),
                        "candidate_rank": rank,
                        "evaluation_score": evaluate_variant_entry(
                            record, FROZEN_CHAMPION_F.variant, config
                        ).score,
                        "timeframe": "15m",
                        "candidate_paths": [F_INTRADAY_ENTRY_VARIANTS[1].label],
                        "requirement_type": "candidate_session",
                    }
                )
            phase.update(unique_symbols=len({row["symbol"] for row in candidates}))
        progress.record("candidate_discovery", phase)
        with progress.phase(
            "snapshot_verification", "verifying discovery inputs stayed unchanged"
        ) as phase:
            if data_fingerprint(database) != snapshot:
                spool.file.close()
                raise ValueError(
                    "Discovery inputs changed during preflight; rerun on a stable snapshot"
                )
        progress.record("snapshot_verification", phase)
    else:
        candidates = candidate_manifest["candidate_sessions"]
    symbols = {row["symbol"] for row in candidates}
    requirements_by_key = {
        (
            row["symbol"],
            date.fromisoformat(row["signal_date"]),
            date.fromisoformat(row["execution_session"]),
        ): row
        for row in candidates
    }
    execution_by_signal = {
        (symbol, signal): execution for symbol, signal, execution in requirements_by_key
    }
    intraday_rows = daily_rows = queries = 0
    batches = 0
    load_seconds = evaluation_seconds = 0.0
    with (
        progress.phase(
            "coverage_load",
            "loading native 15m candidate-session coverage",
            percentage=("requirement_batches_processed", "requirement_batches_total"),
            requirement_batches_processed=0,
            requirement_batches_total=(len(requirements_by_key) + 199) // 200,
            intraday_rows_loaded=0,
            daily_rows_loaded=0,
        ) as load_phase,
        progress.phase(
            "coverage_evaluation",
            "evaluating I1 entry-quality coverage",
            percentage=("candidate_sessions_checked", "candidate_sessions_total"),
            candidate_sessions_checked=0,
            candidate_sessions_total=len(candidates),
            qualified=0,
            unavailable=0,
        ) as phase,
    ):
        load_started = time.monotonic()
        for (
            native_by_key,
            previous_by_key,
            native_count,
            daily_count,
        ) in database.iter_entry_coverage_batches(requirements_by_key):
            load_seconds += time.monotonic() - load_started
            evaluation_started = time.monotonic()
            batches += 1
            queries += 2
            intraday_rows += native_count
            daily_rows += daily_count
            load_phase.update(
                requirement_batches_processed=batches,
                intraday_rows_loaded=intraday_rows,
                daily_rows_loaded=daily_rows,
                sqlite_query_count_coverage=queries,
            )
            for (symbol, signal), previous_close in previous_by_key.items():
                # Execution is fixed by the candidate requirement, never by future prices.
                execution = execution_by_signal[symbol, signal]
                key = symbol, signal, execution
                row = requirements_by_key[key]
                native = native_by_key[symbol, execution]
                decision = opening_weakness_decision(native, execution, previous_close)
                gaps = missing_session_timestamps(native, execution)
                unavailable = bool(gaps) or decision.status is EntryQualityStatus.UNAVAILABLE
                check = {
                    **row,
                    "status": "INTRADAY_UNAVAILABLE" if unavailable else "QUALIFIED",
                    "decision_status": decision.status.value,
                    "missing_timestamps": [t.isoformat() for t in gaps],
                    "reason": "incomplete_entry_session" if gaps else decision.reason,
                }
                checks.append(check)
                if unavailable:
                    missing.append(check)
                phase.update(
                    candidate_sessions_checked=len(checks),
                    qualified=len(checks) - len(missing),
                    unavailable=len(missing),
                )
            evaluation_seconds += time.monotonic() - evaluation_started
            load_started = time.monotonic()
        load_seconds += time.monotonic() - load_started
    order = {(row["symbol"], row["execution_session"]): i for i, row in enumerate(candidates)}
    checks.sort(key=lambda row: order[row["symbol"], row["execution_session"]])
    missing.sort(key=lambda row: order[row["symbol"], row["execution_session"]])
    progress.record("coverage_load", load_phase)
    progress.record("coverage_evaluation", phase)
    # Streaming phases overlap in wall time; report disjoint measured work durations.
    progress.performance.update(
        coverage_load_seconds=load_seconds, coverage_evaluation_seconds=evaluation_seconds
    )
    source_diagnostics = getattr(preparation.screen_source, "diagnostics", None)
    progress.performance.update(
        candidate_sessions=len(candidates),
        unique_candidate_symbols=len(symbols),
        intraday_rows_loaded=intraday_rows,
        daily_rows_loaded=daily_rows + getattr(source_diagnostics, "bars_rows_loaded", 0),
        coverage_daily_rows_loaded=daily_rows,
        coverage_batches=batches,
        sqlite_query_count_coverage=queries,
        full_screen_cache_size=len(getattr(preparation.screen_source, "cache", {})),
    )
    discovery_source = getattr(preparation.screen_source, "source", preparation.screen_source)
    if diagnostics := getattr(discovery_source, "discovery_diagnostics", None):
        progress.performance.update(diagnostics.as_dict())
    report = {
        "report_type": "local_f_intraday_entry_preflight",
        "local_only": True,
        "network_accessed": False,
        "backtest_executed": False,
        "research_family": F_INTRADAY_ENTRY_RESEARCH_FAMILY,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "period_classification": "DEVELOPMENT / RESEARCH",
        "daily_qualification": qualification,
        "intraday_qualified": not missing,
        "candidate_sessions": len(candidates),
        "missing_symbol_sessions": missing,
        "coverage": checks,
        "coverage_requirement": "complete native regular entry session; no exit-session intraday",
        "vwap_requirement": "native VWAP weighted by volume for first two completed bars",
    }
    requirements = {
        "report_type": "intraday_candidate_requirements",
        "discovery_complete": True,
        "candidate_discovery_status": "COMPLETE",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "research_family": F_INTRADAY_ENTRY_RESEARCH_FAMILY,
        "strategies": [item.label for item in F_INTRADAY_ENTRY_VARIANTS],
        "timeframes": ["15m"],
        "extended_hours": False,
        "warmup_bars": 0,
        "candidate_symbols": [
            {"symbol": symbol} for symbol in sorted({row["symbol"] for row in candidates})
        ],
        "candidate_sessions": candidates,
        "required_sessions": candidates,
        "potential_position_ranges": [],
    }
    if spool is not None:
        requirements.update(manifest_metadata(config, candidates, spool, snapshot))
        requirements = CandidateManifest(requirements, spool)
    elif candidate_manifest is not None:
        requirements = candidate_manifest
    report["performance"] = progress.snapshot()
    return report, requirements


def export_f_intraday_entry_preflight(report, requirements, directory: Path, *, stem: str):
    paths = research_output_paths(directory, stem, preflight=True)
    with _research_export(paths, report, "preflight.json", validation=False) as staged:
        _atomic_text(staged["preflight.json"], json.dumps(report, indent=2))
        public = {key: value for key, value in requirements.items() if not key.startswith("_")}
        spool = getattr(requirements, "replay_spool", None)
        if spool is not None:
            spool.file.seek(0)
            with staged["candidate_replay.jsonl"].open("wb") as output:
                shutil.copyfileobj(spool.file, output)
            public["replay_file"] = paths["candidate_replay.jsonl"].name
        else:
            _atomic_text(staged["candidate_replay.jsonl"], "")
        _atomic_text(
            staged["intraday_candidates.json"], json.dumps(public, indent=2, allow_nan=False)
        )
        rows = report["missing_symbol_sessions"]
        _atomic_csv(
            staged["missing_symbol_sessions.csv"],
            rows,
            _field_union(rows) or ["symbol", "execution_session", "status", "reason"],
        )
    return paths


@contextmanager
def _research_export(paths, summary, summary_name, *, validation):
    """Stage the entire bundle, publish the summary last, and roll back on Ctrl+C.

    Persisted timings stop after payload staging: writing the timing metadata itself
    and publishing files necessarily follow that snapshot. The export phase log also
    measures those final operations. No incomplete bundle survives a handled interrupt.
    """
    directory = paths[summary_name].parent
    directory.mkdir(parents=True, exist_ok=True)
    event = "intraday_validation_progress" if validation else "intraday_preflight_progress"
    published = []
    with (
        ProgressPhase(event, "export", message="exporting reports") as phase,
        TemporaryDirectory(prefix=".research-export-", dir=directory) as temporary,
    ):
        staged = {name: Path(temporary) / path.name for name, path in paths.items()}
        try:
            yield staged
            performance = dict(summary.get("performance", {}))
            build_seconds = performance.get("total_seconds", 0.0) - performance.get(
                "export_seconds", 0.0
            )
            performance["export_seconds"] = time.monotonic() - phase.started
            performance["total_seconds"] = build_seconds + performance["export_seconds"]
            performance["export_timing_scope"] = "payload_staging_before_metadata_and_publication"
            _atomic_text(
                staged[summary_name],
                json.dumps({**summary, "performance": performance}, indent=2, allow_nan=False),
            )
            for name in [key for key in paths if key != summary_name] + [summary_name]:
                # Register before replace so an interrupt immediately after it rolls back too.
                published.append(paths[name])
                os.replace(staged[name], paths[name])
        except BaseException:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    summary.setdefault("performance", {}).update(performance)
    # The terminal summary can include publication; persisted metadata cannot measure
    # its own final write. Keep that distinction explicit in both timing snapshots.
    completed_performance = {
        **performance,
        "export_seconds": phase.seconds,
        "total_seconds": build_seconds + phase.seconds,
        "export_timing_scope": "through_publication",
    }
    log_completion(
        "F intraday entry validation" if validation else "Intraday entry preflight",
        completed_performance,
        **(
            {
                "qualified": summary["intraday_qualified"],
                "missing_symbol_sessions": len(summary["missing_symbol_sessions"]),
            }
            if not validation
            else {}
        ),
    )


def run_f_lifecycle_v2(database: Database, config: StrategyConfig, start: date, end: date):
    return _run_research(database, config, start, end, intraday=False)


def run_f_intraday_entry(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    candidate_manifest: Path | None = None,
    rediscover_candidates=False,
):
    if candidate_manifest is None and not rediscover_candidates:
        raise ValueError(
            "Validation requires --candidate-manifest from preflight, "
            "or explicit --rediscover-candidates"
        )
    return _run_research(
        database, config, start, end, intraday=True, candidate_manifest=candidate_manifest
    )


def _run_research(database, config, start, end, *, intraday, candidate_manifest=None):
    progress = ResearchProgress(validation=True) if intraday else None
    with (
        progress.phase("qualification", "daily qualification") if progress else nullcontext()
    ) as phase:
        manifest = None
        if candidate_manifest is not None:
            qualification = _qualify_daily_research(database, config, start, end)
            if qualification["failure_reasons"]:
                raise ValueError(
                    "Daily qualification failed: " + "; ".join(qualification["failure_reasons"])
                )
            sessions = tuple(_backtest_sessions(database, start, end))
            manifest, source = load_manifest(
                candidate_manifest, database, config, start, end, sessions
            )
            preparation = SimpleNamespace(sessions=sessions, screen_source=source)
        else:
            preparation, qualification = _prepare(database, config, start, end)
    if progress:
        progress.record("qualification", phase)
    intraday_qualification = None
    if intraday:
        with progress.phase("coverage_verification", "intraday coverage verification") as phase:
            intraday_qualification, requirements = build_f_intraday_entry_preflight(
                database,
                config,
                start,
                end,
                preparation=preparation,
                progress=progress,
                candidate_manifest=manifest,
            )
        progress.record("coverage_verification", phase)
        if manifest is None:
            spool = requirements.replay_spool
            preparation = SimpleNamespace(
                sessions=preparation.sessions,
                screen_source=ManifestScreenSource(None, spool.offsets, stream=spool.file),
            )
        if not intraday_qualification["intraday_qualified"]:
            raise ValueError(
                "INTRADAY_UNAVAILABLE: run preflight-f-intraday-entry, then manually "
                "qualify/sync the reported native symbol-sessions before validation"
            )
    identities = F_INTRADAY_ENTRY_VARIANTS if intraday else F_LIFECYCLE_VARIANTS
    family = F_INTRADAY_ENTRY_RESEARCH_FAMILY if intraday else F_LIFECYCLE_RESEARCH_FAMILY
    with (
        progress.phase("diagnostics_prepare", "preparing diagnostic context")
        if progress
        else nullcontext()
    ) as phase:
        context = TechnicalPeerContextProvider(database, config, end)
    diagnostics_prepare_seconds = phase.seconds if progress else 0.0
    results, tables = {}, {name: [] for name in (*COMMON_TABLES, *DIAGNOSTIC_FIELDS)}
    pending_diagnostics = []
    cases = [("BASELINE", 5.0, 0.0)]
    if not intraday:
        cases.extend(CANONICAL_COST_STRESS_CASES)
    for case, slippage, commission in cases:
        cost_config = config.model_copy(
            update={
                "backtest": config.backtest.model_copy(
                    update={"slippage_bps": slippage, "commission_bps": commission}
                )
            }
        )
        for identity in identities:
            variant_config = (
                cost_config.model_copy(deep=True)
                if intraday
                else lifecycle_strategy_config(cost_config, identity)
            )
            observer = LifecycleDiagnostics(context, variant_config) if case == "BASELINE" else None
            engine = BacktestEngine(
                database,
                variant_config,
                screen_source=preparation.screen_source,
                lifecycle_preset=None if intraday else identity,
                lifecycle_context=context,
                opening_weakness_veto=intraday and identity.opening_weakness_veto,
                audit_observer=observer,
                entry_context_observer=observer.observe_entry_context if observer else None,
                execution_context_observer=observer.observe_execution_context if observer else None,
                require_complete_daily_position_bars=True,
            )
            strategy = identity.research_id.rsplit("-", 1)[-1]
            with (
                progress.phase(
                    "backtest",
                    f"running {strategy}",
                    strategy=strategy,
                    percentage=("sessions_processed", "sessions_total"),
                    sessions_processed=0,
                    sessions_total=len(preparation.sessions),
                    positions_closed=0,
                    active_positions=0,
                )
                if progress
                else nullcontext()
            ) as phase:
                engine.progress = phase
                result = engine.run(
                    start, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
                )
            if progress:
                progress.record(strategy.lower(), phase)
                progress.performance.update(
                    {
                        f"sessions_processed_{strategy}": len(result.equity_curve),
                        f"positions_{strategy}": len(result.positions),
                    }
                )
                pending_diagnostics.append((case, identity, result, observer, engine))
                continue
            _append_research_tables(
                results,
                tables,
                family,
                case,
                identity,
                result,
                observer,
                engine,
            )
    with (
        progress.phase("diagnostics", "building diagnostics") if progress else nullcontext()
    ) as phase:
        for item in pending_diagnostics:
            _append_research_tables(results, tables, family, *item)
        provenance = audit_universe_provenance(database, start, end)
    if progress:
        progress.performance["diagnostics_seconds"] = phase.seconds + diagnostics_prepare_seconds
    summary = {
        "research_family": family,
        "period_classification": "DEVELOPMENT / RESEARCH",
        "clean_oos": False,
        "automatic_winner_selection": False,
        "frozen_champion": FROZEN_CHAMPION_F.label,
        "frozen_champion_unchanged": True,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "local_only": True,
        "network_accessed": False,
        "max_positions": 1,
        "variants": [asdict(identity) for identity in identities],
        "cost_stress_method": "FULL_PORTFOLIO_RERUN",
        "portfolio_reruns": len(cases) * len(identities),
        "daily_qualification": qualification,
        "intraday_qualification": intraday_qualification,
        "universe_provenance": provenance,
        "peer_membership_basis": PEER_MEMBERSHIP_BASIS,
        "historical_peer_membership_verified": False,
        "peer_price_observations_pit": True,
        "capacity_combination_executed": False,
        "execution_semantics": {
            "daily": "open gaps; daily stop before target; configured time exit at close",
            "target_deferral_context": "previous completed daily session",
            "deterioration_exit": "decision at close; next native daily open, gap stops first",
            "conditional_hold": "decision once at day10 close, hard maximum day20 close",
            "intraday_entry": "two completed 15m bars; next native bar open; daily management",
        },
        "diagnostic_semantics": {
            "forward_labels": "reporting only; run-end censored; incomplete windows are null",
            "excursions": "touch/exit intrabar order unknown; full exit-day ranges excluded",
            "gap_quintiles": "descriptive within-run ATR quintiles, never filters",
            "peer_dispersion": "population standard deviation of valid peer 1d returns",
        },
        "results": tables["summary"],
        "configurations": {key: result.configuration for key, result in results.items()},
        "warnings": sorted({w for result in results.values() for w in result.warnings}),
    }
    if progress:
        summary["performance"] = progress.snapshot()
    return LifecycleResearchBundle(family, results, tables, summary)


def _append_research_tables(results, tables, family, case, identity, result, observer, engine):
    cost = _cost_row(case, identity.label, result)
    cost.update(
        research_id=identity.research_id,
        research_family=family,
        cost_stress_method="FULL_PORTFOLIO_RERUN",
    )
    tables["cost_stress"].append(cost)
    if case != "BASELINE":
        return
    results[identity.research_id] = result
    summary = strategy_summary(identity.label, result)
    tables["summary"].append(
        {**summary, "research_id": identity.research_id, "research_family": family}
    )
    tables["metrics"].append(tables["summary"][-1])
    comparison = _single_result_comparison(result)
    monthly, _ = calendar_stability(comparison, "month")
    yearly, _ = calendar_stability(comparison, "year")
    symbols, _, _ = symbol_and_leave_one_out(comparison)
    own_tables = {
        "positions": [p.model_dump(mode="json") for p in result.positions],
        "execution_legs": [p.model_dump(mode="json") for p in result.trades],
        "monthly": monthly,
        "yearly": yearly,
        "chronological_subperiods": chronological_subperiod_analysis(comparison),
        "symbol_concentration": symbols,
        **observer.tables(result, engine.position_manager),
        "entry_quality_events": engine.entry_quality_events,
    }
    for name, rows in own_tables.items():
        tables[name].extend(
            {
                **row,
                "strategy": identity.label,
                "research_id": identity.research_id,
                "research_family": family,
            }
            for row in rows
        )
    for field in (
        "positions_closed_before_10",
        "positions_closed_day_10",
        "positions_extended_after_10",
        "positions_reaching_15",
        "positions_reaching_20",
        "positions_reaching_30",
        "original_time_exit_positive_additional_MFE",
        "original_time_exit_negative_additional_MAE",
    ):
        tables["summary"][-1][field] = sum(
            int(row.get(field) or 0) for row in own_tables["holding_duration_analysis"]
        )


def export_f_lifecycle_research(bundle: LifecycleResearchBundle, directory: Path, *, stem: str):
    paths = research_output_paths(directory, stem)
    if bundle.family == F_INTRADAY_ENTRY_RESEARCH_FAMILY:
        with _research_export(paths, bundle.summary, "summary.json", validation=True) as staged:
            _write_research_tables(bundle, staged)
        return paths
    directory.mkdir(parents=True, exist_ok=True)
    _write_research_tables(bundle, paths)
    return paths


def _write_research_tables(bundle, paths):
    _atomic_text(paths["summary.json"], json.dumps(bundle.summary, indent=2, allow_nan=False))
    for name, rows in bundle.tables.items():
        fixed = DIAGNOSTIC_FIELDS.get(name, [])
        if name == "positions":
            fixed = list(BacktestPosition.model_fields)
        elif name == "execution_legs":
            fixed = list(BacktestTrade.model_fields)
        fields = list(
            dict.fromkeys(
                ["strategy", "research_id", "research_family", *fixed, *_field_union(rows)]
            )
        )
        _atomic_csv(paths[f"{name}.csv"], rows, fields)
