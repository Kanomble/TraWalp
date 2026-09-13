"""Local preflight and three fixed portfolio runs for intraday risk containment."""

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from trading_system.backtest.capacity_validation import _single_result_comparison
from trading_system.backtest.engine import BacktestEngine, _backtest_sessions
from trading_system.backtest.f_candidates import ManifestScreenSource, ReplaySpool, data_fingerprint
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.backtest.intraday_risk import (
    F_INTRADAY_RISK_RESEARCH_FAMILY,
    F_INTRADAY_RISK_VARIANTS,
    RISK_EVENT_FIELDS,
    SUPPORT_NOTE,
    IntradayRiskOverlay,
)
from trading_system.backtest.lifecycle_validation import _qualify_daily_research, iter_f_candidates
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.backtest.validation import (
    _field_union,
    calendar_stability,
    chronological_subperiod_analysis,
    strategy_summary,
    symbol_and_leave_one_out,
)
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayQualificationStatus,
    IntradayRequirementStatus,
    _classify_missing,
    _expected_timestamps,
    _observation_key,
)
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.market_data import BarTimeframe


@dataclass
class IntradayRiskBundle:
    summary: dict
    requirements: dict
    results: dict
    tables: dict


def _position_requirements(baseline, start, end):
    entries = {(p.symbol, p.entry_date) for p in baseline.positions}
    by_signal = {
        (p.signal_date, p.symbol): tuple(
            (p.symbol, session) for session in trading_sessions_between(p.entry_date, p.exit_date)
        )
        for p in baseline.positions
    }
    requirements = tuple(
        CandidateIntradayRequirement(
            symbol,
            session,
            BarTimeframe.MINUTES_15,
            ("R1_COMMON_SUPPORT",),
            "candidate_session"
            if (symbol, session) in entries
            else "potential_open_position_session",
        )
        for symbol, session in sorted({key for keys in by_signal.values() for key in keys})
    )
    report = {
        "report_type": "intraday_risk_position_requirements",
        "research_family": F_INTRADAY_RISK_RESEARCH_FAMILY,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "discovery_complete": True,
        "strategies": [v.label for v in F_INTRADAY_RISK_VARIANTS],
        "timeframes": ["15m"],
        "extended_hours": False,
        "warmup_bars": 0,
        "support_semantics": SUPPORT_NOTE,
        "candidate_sessions": [
            {
                "symbol": p.symbol,
                "signal_session": p.signal_date.isoformat(),
                "execution_session": p.entry_date.isoformat(),
                "candidate_rank": p.daily_candidate_rank,
                "candidate_paths": ["R1_COMMON_SUPPORT"],
            }
            for p in baseline.positions
        ],
        "required_sessions": [
            {
                "symbol": r.symbol,
                "execution_session": r.session.isoformat(),
                "timeframe": "15m",
                "candidate_paths": list(r.candidate_paths),
                "requirement_type": r.requirement_type,
            }
            for r in requirements
        ],
        "potential_position_ranges": [],
    }
    return requirements, by_signal, report


def _coverage(database, config, requirements, native):
    observations = database.sync_values(PROVIDER_OBSERVATION_SOURCE)
    by_key = {(r.symbol, r.session): r for r in requirements}
    details, queries = [], 0
    # Reuse the exact bounded SQL join and run-owned bounded spool. Previous Daily
    # closes are returned by that shared loader but are not inputs to the risk rule.
    triples = [(r.symbol, r.session, r.session) for r in requirements]
    for bars_by_key, _, _, _ in database.iter_entry_coverage_batches(triples):
        queries += 2
        for key, bars in bars_by_key.items():
            requirement = by_key[key]
            expected = _expected_timestamps(
                requirement.session, requirement.timeframe, extended_hours=False
            )
            present = {b.timestamp for b in bars}
            missing = tuple(t for t in expected if t not in present)
            status, reason = _classify_missing(
                requirement,
                missing,
                observations.get(_observation_key(requirement)),
                feed=config.universe.market_data_feed,
                adjustment=config.universe.market_data_adjustment,
                extended_hours=False,
            )
            if not missing:
                by_timestamp = {b.timestamp: b for b in bars}
                native.add(
                    requirement.symbol,
                    requirement.session,
                    requirement.session,
                    [by_timestamp[t] for t in expected],
                    None,
                )
            details.append(
                {
                    "symbol": requirement.symbol,
                    "session": requirement.session.isoformat(),
                    "timeframe": "15m",
                    "requirement_type": requirement.requirement_type,
                    "classification": status.value,
                    "reason": reason,
                    "missing_timestamps": [t.isoformat() for t in missing],
                }
            )
    counts = Counter(row["classification"] for row in details)
    status = (
        IntradayQualificationStatus.NOT_READY_PROVIDER_VERIFICATION_FAILURE
        if counts[IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value]
        else IntradayQualificationStatus.NOT_READY_LOCAL_GAPS
        if counts[IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value]
        else IntradayQualificationStatus.READY_WITH_PROVIDER_ABSENCE
        if counts[IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value]
        else IntradayQualificationStatus.READY
    )
    return details, status, queries


def _tables(results, events):
    tables = {
        name: []
        for name in (
            "metrics",
            "positions",
            "execution_legs",
            "equity_curve",
            "monthly",
            "yearly",
            "chronological_subperiods",
            "symbol_concentration",
            "intraday_risk_events",
        )
    }
    tables["intraday_risk_events"] = events
    for strategy, result in results.items():
        comparison = _single_result_comparison(result)
        monthly, _ = calendar_stability(comparison, "month")
        yearly, _ = calendar_stability(comparison, "year")
        symbols, _, _ = symbol_and_leave_one_out(comparison)
        rows = {
            "metrics": [strategy_summary(strategy, result)],
            "positions": [p.model_dump(mode="json") for p in result.positions],
            "execution_legs": [p.model_dump(mode="json") for p in result.trades],
            "equity_curve": [p.model_dump(mode="json") for p in result.equity_curve],
            "monthly": monthly,
            "yearly": yearly,
            "chronological_subperiods": chronological_subperiod_analysis(comparison),
            "symbol_concentration": symbols,
        }
        for name, values in rows.items():
            tables[name].extend({**row, "strategy": strategy} for row in values)
    return tables


def _comparisons(results):
    comparisons = {}
    for name, treated, control in (
        ("intraday_risk_effect", "R1_COMMON_SUPPORT", "R0_COMMON_SUPPORT"),
        ("coverage_support_bias", "R0_COMMON_SUPPORT", "R0_FULL"),
    ):
        before, after = results[control].metrics.model_dump(), results[treated].metrics.model_dump()

        # Attribute only exactly shared signal, symbol and entry timestamp keys.
        def by_entry(result):
            return {(p.signal_date, p.symbol, p.entry_timestamp): p for p in result.positions}

        old, new = by_entry(results[control]), by_entry(results[treated])
        shared = old.keys() & new.keys()
        comparisons[name] = {
            "comparison": f"{treated} - {control}",
            "primary": name == "intraday_risk_effect",
            "metric_deltas": {
                key: after[key] - before[key]
                if after[key] is not None and before[key] is not None
                else None
                for key in before
            },
            "shared_entry_positions": len(shared),
            "shared_entry_net_pnl_delta": sum(
                new[key].net_pnl - old[key].net_pnl for key in sorted(shared)
            ),
        }
    return comparisons


def build_f_intraday_risk_preflight(database, config, start, end, *, preparation=None):
    return _run(database, config, start, end, validate=False, preparation=preparation)


def run_f_intraday_risk(database, config, start, end, *, preparation=None):
    return _run(database, config, start, end, validate=True, preparation=preparation)


def _run(database, config, start, end, *, validate, preparation):
    timings = {"sqlite_query_count_native_risk": 0}
    qualification = _qualify_daily_research(database, config, start, end)
    if qualification["failure_reasons"]:
        raise ValueError(
            "Daily qualification failed: " + "; ".join(qualification["failure_reasons"])
        )
    snapshot = data_fingerprint(database, config, start=start, end=end)
    sessions = (
        preparation.sessions
        if preparation is not None
        else tuple(_backtest_sessions(database, start, end))
    )
    source = (
        preparation.screen_source
        if preparation is not None
        else HistoricalFeatureScreenSource(database, config, start, end)
    )
    spool = ReplaySpool()
    try:
        with ProgressPhase("intraday_risk_progress", "candidate_discovery") as phase:
            for _ in iter_f_candidates(
                source, config, sessions, replay_spool=spool, progress=phase
            ):
                pass
        timings["candidate_discovery_seconds"] = phase.seconds
        replay = ManifestScreenSource(None, spool.offsets, stream=spool.file)
        del source, preparation
        results, events = {}, []
        with NativeEntrySessions() as native:
            with ProgressPhase("intraday_risk_progress", "r0_full") as phase:
                baseline = BacktestEngine(
                    database,
                    config,
                    screen_source=replay,
                    require_complete_daily_position_bars=True,
                    progress=phase,
                ).run(
                    start, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
                )
            timings["r0_full_seconds"] = phase.seconds
            results["R0_FULL"] = baseline
            requirements, by_signal, report = _position_requirements(baseline, start, end)
            with ProgressPhase("intraday_risk_progress", "coverage_verification") as phase:
                coverage, status, queries = _coverage(database, config, requirements, native)
            timings.update(
                coverage_verification_seconds=phase.seconds, coverage_sql_queries=queries
            )
            absent = {
                (row["symbol"], row["session"])
                for row in coverage
                if row["classification"]
                == IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value
            }
            unsupported = {
                key
                for key, required in by_signal.items()
                if any((symbol, session.isoformat()) in absent for symbol, session in required)
            }
            present = {
                (row["symbol"], row["session"])
                for row in coverage
                if row["classification"] == IntradayRequirementStatus.REQUIRED_PRESENT.value
            }
            supported = frozenset(
                key
                for key, required in by_signal.items()
                if all((symbol, session.isoformat()) in present for symbol, session in required)
            )
            report["data_snapshot_fingerprint"] = snapshot
            report["coverage"] = coverage
            summary = {
                "research_family": F_INTRADAY_RISK_RESEARCH_FAMILY,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "period_classification": "DEVELOPMENT / RESEARCH",
                "clean_oos": False,
                "local_only": True,
                "network_accessed": False,
                "frozen_champion": "F/configured/C1",
                "frozen_champion_unchanged": True,
                "automatic_winner_selection": False,
                "max_positions": 1,
                "support_semantics": SUPPORT_NOTE,
                "rule": (
                    "two consecutive completed native 15m closes <= entry - 0.5R; next native open"
                ),
                "qualification_status": status.value,
                "daily_qualification": qualification,
                "required_position_sessions": len(requirements),
                "provider_absent_position_sessions": len(absent),
                "unsupported_baseline_positions": len(unsupported),
                "common_support_positions": len(supported),
                "support_mask": [
                    {
                        "signal_date": p.signal_date.isoformat(),
                        "symbol": p.symbol,
                        "baseline_position_id": p.position_id,
                        "supported": (p.signal_date, p.symbol) in supported,
                    }
                    for p in baseline.positions
                ],
                "variants": [asdict(v) for v in F_INTRADAY_RISK_VARIANTS],
                "r1_backtest_executed": validate,
            }
            if validate:
                if status not in {
                    IntradayQualificationStatus.READY,
                    IntradayQualificationStatus.READY_WITH_PROVIDER_ABSENCE,
                }:
                    raise ValueError(f"Intraday risk coverage not ready: {status.value}")
                for variant in F_INTRADAY_RISK_VARIANTS[1:]:
                    overlay = IntradayRiskOverlay(
                        baseline.positions,
                        supported,
                        native,
                        strategy=variant.label,
                        enabled=variant.overlay,
                    )
                    with ProgressPhase("intraday_risk_progress", variant.label.lower()) as phase:
                        result = BacktestEngine(
                            database,
                            config,
                            screen_source=replay,
                            intraday_risk=overlay,
                            require_complete_daily_position_bars=True,
                            progress=phase,
                        ).run(
                            start,
                            end,
                            variant=FROZEN_CHAMPION_F.variant,
                            preset=FROZEN_CHAMPION_F.preset,
                        )
                    results[variant.label] = result
                    timings[f"{variant.label.lower()}_seconds"] = phase.seconds
                    events.extend(overlay.events)
            timings["native_risk_cache_peak"] = native.peak_cache_size
        with ProgressPhase("intraday_risk_progress", "diagnostics") as phase:
            tables = _tables(results, events)
            summary["metrics"] = tables["metrics"]
            summary["configurations"] = {
                label: result.configuration for label, result in results.items()
            }
            summary["warnings"] = sorted(
                {warning for result in results.values() for warning in result.warnings}
            )
            summary["universe_membership_basis"] = (
                "Current local tradable universe; historical survivorship bias is not resolved"
            )
            risk_events = [row for row in events if row["strategy"] == "R1_COMMON_SUPPORT"]
            counts = Counter(row["status"] for row in risk_events)
            summary.update(
                r1_breakdown_triggers=sum(
                    row["decision_timestamp"] is not None for row in risk_events
                ),
                r1_breakdown_executions=counts["EXECUTED"],
                r1_preempted_by_stop=counts["CANONICAL_STOP_PRECEDED"],
                r1_preempted_by_target=counts["CANONICAL_TARGET_PRECEDED"],
                portfolio_reruns=len(results),
            )
            if validate:
                summary["comparisons"] = _comparisons(results)
        timings["diagnostics_seconds"] = phase.seconds
        if data_fingerprint(database, config, start=start, end=end) != snapshot:
            raise ValueError(
                "Daily research inputs changed during intraday risk research; "
                "rerun on a stable snapshot"
            )
        summary["performance"] = timings
        return IntradayRiskBundle(summary, report, results, tables)
    finally:
        spool.file.close()


def risk_output_paths(directory: Path, stem: str, *, preflight=False):
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("output-stem must be a plain file stem")
    names = (
        ["preflight.json", "intraday_requirements.json"]
        if preflight
        else [
            "summary.json",
            "intraday_requirements.json",
            "metrics.csv",
            "positions.csv",
            "execution_legs.csv",
            "equity_curve.csv",
            "monthly.csv",
            "yearly.csv",
            "chronological_subperiods.csv",
            "symbol_concentration.csv",
            "intraday_risk_events.csv",
        ]
    )
    paths = {name: directory / f"{stem}_{name}" for name in names}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("Intraday risk output already exists; use a fresh output-stem")
    return paths


def export_f_intraday_risk(bundle, directory, *, stem, preflight=False):
    paths = risk_output_paths(directory, stem, preflight=preflight)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_text(
        paths["intraday_requirements.json"],
        json.dumps(bundle.requirements, indent=2, allow_nan=False),
    )
    for name, rows in bundle.tables.items():
        if not preflight:
            fields = list(RISK_EVENT_FIELDS) if name == "intraday_risk_events" else ["strategy"]
            _atomic_csv(
                paths[f"{name}.csv"], rows, list(dict.fromkeys([*fields, *_field_union(rows)]))
            )
    _atomic_text(
        paths["preflight.json" if preflight else "summary.json"],
        json.dumps(bundle.summary, indent=2, allow_nan=False),
    )
    return paths
