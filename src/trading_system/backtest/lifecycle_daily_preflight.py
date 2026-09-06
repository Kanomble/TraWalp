"""Local Daily execution coverage for every PIT-eligible F lifecycle candidate."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from trading_system.backtest.engine import prepare_strategy_comparison
from trading_system.backtest.lifecycle import F_LIFECYCLE_RESEARCH_FAMILY, F_LIFECYCLE_VARIANTS
from trading_system.backtest.lifecycle_validation import (
    _qualify_daily_research,
    iter_f_candidates,
    research_output_paths,
)
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import StrategyComparisonKind

# L0 inherits the frozen control's validated ten-session hard maximum.
HOLDING_LIMITS = {
    variant.research_id.rsplit("-", 1)[-1]: variant.max_hold_days or 10
    for variant in F_LIFECYCLE_VARIANTS
}
MAX_REQUIRED_HOLDING_SESSIONS = max(HOLDING_LIMITS.values())
COVERAGE_SYMBOL_BATCH_SIZE = 400
CANDIDATE_FIELDS = ["symbol", "signal_session", "entry_session", "candidate_rank"]
REQUIREMENT_FIELDS = [
    *CANDIDATE_FIELDS,
    "required_session",
    "holding_day_if_entered",
    "required_by_variants",
    "status",
]
MISSING_FIELDS = [
    "symbol",
    "required_session",
    "first_signal_session_requiring_it",
    "first_entry_session_requiring_it",
    "max_holding_day_requiring_it",
    "required_by_variants",
    "status",
    "candidate_requirement_count",
]
SUMMARY_FIELDS = [
    *CANDIDATE_FIELDS,
    "last_required_session",
    "required_sessions",
    "present_sessions",
    "missing_sessions",
    "status",
]


class DailyCoverageStatus(StrEnum):
    REQUIRED_PRESENT = "REQUIRED_PRESENT"
    LOCAL_DAILY_MISSING = "LOCAL_DAILY_MISSING"
    NOT_REQUIRED = "NOT_REQUIRED"


class _ReadOnlyResearchDatabase(Database):
    """Enforce read-only SQLite even for legacy shared readers using connect()."""

    connect = Database.read_only


@dataclass(frozen=True)
class LifecycleDailyPreflight:
    report: dict
    candidates: list[dict]
    missing: list[dict]
    sessions: tuple[str, ...]

    def daily_requirements(self) -> Iterator[dict]:
        """Expand the compact candidate/coverage snapshot only while exporting."""
        missing = {(row["symbol"], row["required_session"]) for row in self.missing}
        indices = {session: index for index, session in enumerate(self.sessions)}
        for candidate in self.candidates:
            for day, session in _candidate_horizon(candidate, self.sessions, indices):
                yield {
                    **{key: candidate[key] for key in CANDIDATE_FIELDS},
                    "required_session": session,
                    "holding_day_if_entered": day,
                    "required_by_variants": _required_variants(day),
                    "status": (
                        DailyCoverageStatus.LOCAL_DAILY_MISSING
                        if (candidate["symbol"], session) in missing
                        else DailyCoverageStatus.REQUIRED_PRESENT
                    ),
                }


def _required_variants(day: int) -> str:
    return ",".join(variant for variant, maximum in HOLDING_LIMITS.items() if day <= maximum)


def _candidate_horizon(candidate, sessions, indices):
    entry_index = indices[candidate["entry_session"]]
    # Entry = day 1; the official session vector is already censored at research end.
    return enumerate(sessions[entry_index : entry_index + MAX_REQUIRED_HOLDING_SESSIONS], 1)


def build_f_lifecycle_daily_preflight(
    database: Database, config: StrategyConfig, start: date, end: date
) -> LifecycleDailyPreflight:
    database = _ReadOnlyResearchDatabase(database.path)
    qualification = _qualify_daily_research(database, config, start, end)
    official = tuple(trading_sessions_between(start, end))
    sessions = tuple(session.isoformat() for session in official)
    daily_qualified = qualification["qualified"]
    candidates = []
    if daily_qualified and len(official) > 1:
        preparation = prepare_strategy_comparison(
            database, config, start, end, comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F
        )
        # Finish PIT selection before inspecting future coverage. Nothing from coverage
        # can enter the screen, evaluator, ranking, capacity or trading decisions.
        candidates = [
            {
                "symbol": record.symbol,
                "signal_session": signal.isoformat(),
                "entry_session": entry.isoformat(),
                "candidate_rank": rank,
            }
            for signal, entry, rank, record in iter_f_candidates(
                preparation.screen_source, config, official
            )
        ]
        del preparation  # Release the feature/screen cache before coverage and CSV expansion.

    by_symbol = defaultdict(list)
    for candidate in candidates:
        by_symbol[candidate["symbol"]].append(candidate)
    symbols = sorted(by_symbol)
    indices = {session: index for index, session in enumerate(sessions)}
    required_count, present_count, query_count = 0, 0, 0
    missing_rows = []
    # Only keys are loaded, never OHLCV objects. Memory for presence is bounded by
    # one symbol batch; requirements are expanded per candidate and streamed to CSV.
    if symbols:
        with database.read_only() as connection:
            for offset in range(0, len(symbols), COVERAGE_SYMBOL_BATCH_SIZE):
                batch = symbols[offset : offset + COVERAGE_SYMBOL_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                present = defaultdict(set)
                cursor = connection.execute(
                    f"""SELECT symbol,timestamp FROM bars WHERE symbol IN ({placeholders})
                    AND timeframe='1d' AND timestamp>=? AND timestamp<?
                    ORDER BY symbol,timestamp""",
                    [*batch, sessions[1], (end + timedelta(days=1)).isoformat()],
                )
                query_count += 1
                for symbol, timestamp in cursor:
                    present[str(symbol)].add(str(timestamp)[:10])
                for symbol in batch:
                    required, missing = set(), {}
                    for candidate in by_symbol[symbol]:
                        count, gaps, last = 0, 0, None
                        for day, session in _candidate_horizon(candidate, sessions, indices):
                            count += 1
                            last = session
                            required.add(session)
                            if session in present[symbol]:
                                continue
                            gaps += 1
                            row = missing.setdefault(
                                session,
                                {
                                    "symbol": symbol,
                                    "required_session": session,
                                    "first_signal_session_requiring_it": candidate[
                                        "signal_session"
                                    ],
                                    "first_entry_session_requiring_it": candidate["entry_session"],
                                    "max_holding_day_requiring_it": day,
                                    "required_by_variants": set(),
                                    "status": DailyCoverageStatus.LOCAL_DAILY_MISSING,
                                    "candidate_requirement_count": 0,
                                },
                            )
                            row["max_holding_day_requiring_it"] = max(
                                row["max_holding_day_requiring_it"], day
                            )
                            row["required_by_variants"].update(_required_variants(day).split(","))
                            row["candidate_requirement_count"] += 1
                        candidate.update(
                            last_required_session=last,
                            required_sessions=count,
                            present_sessions=count - gaps,
                            missing_sessions=gaps,
                            status=(
                                DailyCoverageStatus.LOCAL_DAILY_MISSING
                                if gaps
                                else DailyCoverageStatus.REQUIRED_PRESENT
                            ),
                        )
                    required_count += len(required)
                    present_count += len(required & present[symbol])
                    for session in sorted(missing):
                        row = missing[session]
                        row["required_by_variants"] = ",".join(sorted(row["required_by_variants"]))
                        missing_rows.append(row)

    report = {
        "report_type": "local_f_lifecycle_daily_preflight",
        "research_family": F_LIFECYCLE_RESEARCH_FAMILY,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "local_only": True,
        "network_accessed": False,
        "backtest_executed": False,
        "period_classification": "DEVELOPMENT / RESEARCH",
        "clean_oos": False,
        "strategy_identity": FROZEN_CHAMPION_F.label,
        "daily_qualification": qualification,
        "daily_qualified": daily_qualified,
        "lifecycle_daily_qualified": daily_qualified and not missing_rows,
        "candidate_discovery_complete": daily_qualified,
        "candidate_discovery_status": "COMPLETE" if daily_qualified else "DAILY_UNQUALIFIED",
        "candidate_count": len(candidates),
        "candidate_symbol_count": len(symbols),
        "candidate_requirement_count": sum(row["required_sessions"] for row in candidates),
        "required_symbol_sessions": required_count,
        "present_symbol_sessions": present_count,
        "missing_symbol_sessions": len(missing_rows),
        "missing_by_symbol": dict(sorted(Counter(row["symbol"] for row in missing_rows).items())),
        "missing_by_session": dict(
            sorted(Counter(row["required_session"] for row in missing_rows).items())
        ),
        "missing_by_variant": {
            variant: sum(variant in row["required_by_variants"].split(",") for row in missing_rows)
            for variant in HOLDING_LIMITS
        },
        "max_required_holding_sessions": MAX_REQUIRED_HOLDING_SESSIONS,
        "variant_max_holding_sessions": HOLDING_LIMITS,
        "coverage_method": (
            "all PIT-eligible frozen F candidates before allocation; entry=T+1 XNYS session; "
            "entry=holding day 1; up to 30 official sessions inclusive, capped at research end; "
            "indexed local Daily key presence only; no exit-path simulation or provider inference"
        ),
        "coverage_sql_queries": query_count,
    }
    return LifecycleDailyPreflight(report, candidates, missing_rows, sessions)


def export_f_lifecycle_daily_preflight(
    bundle: LifecycleDailyPreflight, directory: Path, *, stem: str
):
    paths = research_output_paths(directory, stem, daily_preflight=True)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_csv(paths["daily_requirements.csv"], bundle.daily_requirements(), REQUIREMENT_FIELDS)
    _atomic_csv(paths["missing_symbol_sessions.csv"], bundle.missing, MISSING_FIELDS)
    _atomic_csv(paths["candidate_summary.csv"], bundle.candidates, SUMMARY_FIELDS)
    _atomic_text(paths["preflight.json"], json.dumps(bundle.report, indent=2))
    return paths
