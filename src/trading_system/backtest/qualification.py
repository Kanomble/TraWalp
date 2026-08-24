"""Shared lifecycle-aware qualification for historical PIT screen starts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.universe import is_financial_or_reit, is_reit

BENCHMARK_SYMBOL = "SPY"
IDENTITY_CONFLICT_SAMPLE_LIMIT = 10


def qualify_historical_screen_start(
    database: Database,
    config: StrategyConfig,
    requested_start: date,
    requested_end: date,
    *,
    allow_start_shift: bool,
) -> dict[str, Any]:
    """Find a causal screen start with exact benchmark and member Daily history.

    The benchmark is a global requirement.  Current-universe members are checked
    independently, matching the production screen's insufficient-history policy:
    one complete eligible member is enough to attempt PIT candidate discovery.
    """

    if requested_start > requested_end:
        raise ValueError("requested historical screen start must not follow end")
    required = required_daily_warmup_sessions(config)
    conflicts = database.unresolved_sec_identity_conflict_symbols()
    all_companies = database.list_tradable_companies()
    conflicted_symbols = sorted({company.symbol for company in all_companies} & conflicts)
    companies = [
        company
        for company in all_companies
        if company.symbol not in conflicts
        and not (config.universe.exclude_reits and is_reit(company.sic))
        and not (
            config.universe.exclude_financials
            and is_financial_or_reit(company.sic)
        )
    ]
    symbols = tuple(sorted({company.symbol for company in companies}))
    candidate_sessions = tuple(trading_sessions_between(requested_start, requested_end))
    if not candidate_sessions:
        raise ValueError("requested historical interval contains no XNYS sessions")
    first_screen = candidate_sessions[0]
    earliest_warmup = daily_warmup_start(first_screen, required)
    bars_by_symbol: dict[str, set[date]] = defaultdict(set)
    requested_symbols = (*symbols, BENCHMARK_SYMBOL)
    with database.read_only() as connection:
        for offset in range(0, len(requested_symbols), 400):
            batch = requested_symbols[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT symbol,timestamp FROM bars
                WHERE symbol IN ({placeholders}) AND timeframe='1d'
                AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                [
                    *batch,
                    earliest_warmup.isoformat(),
                    (requested_end + timedelta(days=1)).isoformat(),
                ],
            ).fetchall()
            for symbol, timestamp in rows:
                bars_by_symbol[str(symbol)].add(date.fromisoformat(str(timestamp)[:10]))

    sessions_to_check = candidate_sessions if allow_start_shift else candidate_sessions[:1]
    selected_session: date | None = None
    selected_history: tuple[date, ...] = ()
    selected_complete_symbols: list[str] = []
    initial_history: tuple[date, ...] = ()
    initial_complete_symbols: list[str] = []
    initial_benchmark_complete = False
    initial_benchmark_missing: list[date] = []
    for index, session in enumerate(sessions_to_check):
        required_history = tuple(
            trading_sessions_between(daily_warmup_start(session, required), session)
        )
        if len(required_history) != required:
            continue
        expected = set(required_history)
        benchmark_missing = sorted(expected - bars_by_symbol.get(BENCHMARK_SYMBOL, set()))
        complete = [
            symbol
            for symbol in symbols
            if expected.issubset(bars_by_symbol.get(symbol, set()))
        ]
        if index == 0:
            initial_history = required_history
            initial_complete_symbols = complete
            initial_benchmark_complete = not benchmark_missing
            initial_benchmark_missing = benchmark_missing
        if not benchmark_missing and complete:
            selected_session = session
            selected_history = required_history
            selected_complete_symbols = complete
            break

    # The first candidate always has a calendar-defined warmup.  Keep a defensive
    # fallback so a calendar/provider incompatibility produces an actionable report.
    if not initial_history:
        initial_history = tuple(trading_sessions_between(earliest_warmup, first_screen))
        initial_expected = set(initial_history)
        initial_benchmark_missing = sorted(
            initial_expected - bars_by_symbol.get(BENCHMARK_SYMBOL, set())
        )
        initial_benchmark_complete = not initial_benchmark_missing
        initial_complete_symbols = [
            symbol
            for symbol in symbols
            if initial_expected.issubset(bars_by_symbol.get(symbol, set()))
        ]

    diagnostic_history = selected_history or initial_history
    internal_gaps, edge_gaps, symbols_with_internal_gaps = _warmup_gap_diagnostics(
        symbols,
        bars_by_symbol,
        diagnostic_history,
    )
    failure_reasons: list[str] = []
    if selected_session is None:
        if not initial_benchmark_complete:
            failure_reasons.append(
                f"{BENCHMARK_SYMBOL} benchmark warmup is missing "
                f"{len(initial_benchmark_missing)} of {len(initial_history)} required XNYS "
                f"Daily sessions for the first screen on {first_screen}"
            )
        if not initial_complete_symbols:
            failure_reasons.append(
                "no eligible non-conflicted universe member has the complete required Daily "
                f"history for the first screen on {first_screen}"
            )
        if allow_start_shift:
            failure_reasons = [
                "no session has a complete SPY warmup and any complete eligible symbol"
            ]

    complete_symbols = selected_complete_symbols or initial_complete_symbols
    diagnostic_start = diagnostic_history[0] if diagnostic_history else earliest_warmup
    benchmark_complete = selected_session is not None or initial_benchmark_complete
    benchmark_missing_count = 0 if selected_session is not None else len(initial_benchmark_missing)
    report: dict[str, Any] = {
        "qualified": selected_session is not None,
        "ready": selected_session is not None,
        "failure_reason": "; ".join(failure_reasons) if failure_reasons else None,
        "failure_reasons": failure_reasons,
        "qualification_rule": (
            "exact SPY completed-screen history plus at least one eligible non-conflicted "
            "current-universe symbol with all required sessions; insufficient-history symbols "
            "retain the production PIT rejection"
        ),
        "requested_validation_start": requested_start.isoformat(),
        "requested_validation_end": requested_end.isoformat(),
        "requested_first_screen_session": first_screen.isoformat(),
        "required_prior_sessions": required,
        "required_warmup_sessions": required,
        "required_daily_warmup_start": diagnostic_start.isoformat(),
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_warmup_complete": benchmark_complete,
        "benchmark_missing_warmup_sessions": benchmark_missing_count,
        "benchmark_warmup_complete_at_requested_start": initial_benchmark_complete,
        "benchmark_missing_warmup_sessions_at_requested_start": len(
            initial_benchmark_missing
        ),
        "representative_eligible_symbols": len(symbols),
        "screenable_symbol_count_at_start": len(initial_complete_symbols),
        "symbols_with_complete_initial_warmup": len(initial_complete_symbols),
        "symbols_rejected_initially_for_insufficient_history": (
            len(symbols) - len(initial_complete_symbols)
        ),
        "symbols_with_complete_required_history": len(complete_symbols),
        "percentage_with_complete_required_history": (
            len(complete_symbols) / len(symbols) if symbols else 0.0
        ),
        "symbols_rejected_insufficient_market_history": len(symbols) - len(complete_symbols),
        "internal_daily_gaps": internal_gaps,
        "symbols_with_internal_daily_gaps": symbols_with_internal_gaps,
        "edge_or_lifecycle_daily_gaps": edge_gaps,
        "identity_conflicts_excluded": len(conflicted_symbols),
        "identity_conflict_symbols": conflicted_symbols[:IDENTITY_CONFLICT_SAMPLE_LIMIT],
        "qualification_symbol_count_after_exclusions": len(symbols),
        "actual_first_qualified_screen_session": (
            selected_session.isoformat() if selected_session is not None else None
        ),
    }
    return report


def _warmup_gap_diagnostics(
    symbols: tuple[str, ...],
    bars_by_symbol: dict[str, set[date]],
    required_history: tuple[date, ...],
) -> tuple[int, int, int]:
    expected = set(required_history)
    internal_gaps = 0
    edge_gaps = 0
    symbols_with_internal_gaps = 0
    for symbol in symbols:
        present = bars_by_symbol.get(symbol, set()) & expected
        missing = expected - present
        if not missing:
            continue
        internal = 0
        if present:
            first_present = min(present)
            last_present = max(present)
            internal = sum(first_present < item < last_present for item in missing)
        internal_gaps += internal
        edge_gaps += len(missing) - internal
        symbols_with_internal_gaps += int(internal > 0)
    return internal_gaps, edge_gaps, symbols_with_internal_gaps
