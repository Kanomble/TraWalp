"""Fixed, local-only lifecycle and entry-quality research runners and fresh exports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from trading_system.backtest.capacity_validation import (
    _single_result_comparison,
    _validate_frozen_control_config,
)
from trading_system.backtest.engine import BacktestEngine, prepare_strategy_comparison
from trading_system.backtest.entry_quality import (
    F_INTRADAY_ENTRY_RESEARCH_FAMILY,
    F_INTRADAY_ENTRY_VARIANTS,
    EntryQualityStatus,
    missing_session_timestamps,
    opening_weakness_decision,
)
from trading_system.backtest.lifecycle import (
    F_LIFECYCLE_RESEARCH_FAMILY,
    F_LIFECYCLE_VARIANTS,
    lifecycle_strategy_config,
)
from trading_system.backtest.lifecycle_diagnostics import LifecycleDiagnostics
from trading_system.backtest.peer_context import PEER_MEMBERSHIP_BASIS, TechnicalPeerContextProvider
from trading_system.backtest.presets import position_management_preset
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
from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
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


def research_output_paths(directory: Path, stem: str, *, preflight=False):
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("output-stem must be a plain file stem")
    names = (
        ["preflight.json", "intraday_candidates.json", "missing_symbol_sessions.csv"]
        if preflight
        else ["summary.json", *[f"{name}.csv" for name in (*COMMON_TABLES, *DIAGNOSTIC_FIELDS)]]
    )
    paths = {name: directory / f"{stem}_{name}" for name in names}
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"Research output already exists: {existing[0]}")
    return paths


def _prepare(database, config, start, end):
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
    if qualification["failure_reasons"]:
        raise ValueError(
            "Daily qualification failed: " + "; ".join(qualification["failure_reasons"])
        )
    official = trading_sessions_between(start, end)
    if set(official) - set(database.bar_sessions(start, end)):
        raise ValueError("Daily research requires all requested portfolio sessions locally")
    preparation = prepare_strategy_comparison(
        database, config, start, end, comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F
    )
    return preparation, qualification


def build_f_intraday_entry_preflight(
    database: Database, config: StrategyConfig, start: date, end: date, *, preparation=None
):
    """Discover every eligible F candidate, including capacity-blocked candidates; no backtest."""
    qualification = {}
    if preparation is None:
        preparation, qualification = _prepare(database, config, start, end)
    # Keep eligibility/ranking in the engine's canonical evaluator.
    from trading_system.backtest.engine import evaluate_variant_entry

    sessions = preparation.sessions
    candidates, missing, checks = [], [], []
    for signal, execution in zip(sessions[:-1], sessions[1:], strict=True):
        records = []
        for record in preparation.screen_source.screen(signal).records:
            evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config)
            if evaluation.eligible:
                records.append((evaluation.score, record))
        records.sort(key=lambda pair: (-pair[0], pair[1].symbol))
        for rank, (_, record) in enumerate(records, 1):
            opening, closing = regular_session_bounds(execution)
            native = database.bars_between(
                [record.symbol], opening, closing, timeframe=BarTimeframe.MINUTES_15
            )
            history = database.bars_available_as_of(record.symbol, signal, limit=1)
            previous_close = (
                float(history[-1].close)
                if history and (history[-1].timestamp.date() == signal)
                else None
            )
            decision = opening_weakness_decision(native, execution, previous_close)
            gaps = missing_session_timestamps(native, execution)
            unavailable = bool(gaps) or decision.status is EntryQualityStatus.UNAVAILABLE
            row = {
                "symbol": record.symbol,
                "signal_date": signal.isoformat(),
                "execution_session": execution.isoformat(),
                "candidate_rank": rank,
                "timeframe": "15m",
                "candidate_paths": [F_INTRADAY_ENTRY_VARIANTS[1].label],
                "requirement_type": "candidate_session",
            }
            candidates.append(row)
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
    return report, requirements


def export_f_intraday_entry_preflight(report, requirements, directory: Path, *, stem: str):
    paths = research_output_paths(directory, stem, preflight=True)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_text(paths["preflight.json"], json.dumps(report, indent=2))
    _atomic_text(paths["intraday_candidates.json"], json.dumps(requirements, indent=2))
    rows = report["missing_symbol_sessions"]
    _atomic_csv(
        paths["missing_symbol_sessions.csv"],
        rows,
        _field_union(rows) or ["symbol", "execution_session", "status", "reason"],
    )
    return paths


def run_f_lifecycle_v2(database: Database, config: StrategyConfig, start: date, end: date):
    return _run_research(database, config, start, end, intraday=False)


def run_f_intraday_entry(database: Database, config: StrategyConfig, start: date, end: date):
    return _run_research(database, config, start, end, intraday=True)


def _run_research(database, config, start, end, *, intraday):
    preparation, qualification = _prepare(database, config, start, end)
    intraday_qualification = None
    if intraday:
        intraday_qualification, _ = build_f_intraday_entry_preflight(
            database, config, start, end, preparation=preparation
        )
        if not intraday_qualification["intraday_qualified"]:
            raise ValueError(
                "INTRADAY_UNAVAILABLE: run preflight-f-intraday-entry, then manually "
                "qualify/sync the reported native symbol-sessions before validation"
            )
    identities = F_INTRADAY_ENTRY_VARIANTS if intraday else F_LIFECYCLE_VARIANTS
    family = F_INTRADAY_ENTRY_RESEARCH_FAMILY if intraday else F_LIFECYCLE_RESEARCH_FAMILY
    context = TechnicalPeerContextProvider(database, config, end)
    results, tables = {}, {name: [] for name in (*COMMON_TABLES, *DIAGNOSTIC_FIELDS)}
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
            result = engine.run(
                start, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
            )
            cost = _cost_row(case, identity.label, result)
            cost.update(
                research_id=identity.research_id,
                research_family=family,
                cost_stress_method="FULL_PORTFOLIO_RERUN",
            )
            tables["cost_stress"].append(cost)
            if case != "BASELINE":
                continue
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
        "universe_provenance": audit_universe_provenance(database, start, end),
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
    return LifecycleResearchBundle(family, results, tables, summary)


def export_f_lifecycle_research(bundle: LifecycleResearchBundle, directory: Path, *, stem: str):
    paths = research_output_paths(directory, stem)
    directory.mkdir(parents=True, exist_ok=True)
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
    return paths
