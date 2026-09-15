"""Batched native candidate coverage and optional bounded spool; no signal evaluation."""

from time import perf_counter

from trading_system.backtest.native_session_grid import (
    expected_native_timestamps,
    validate_native_session,
)
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayRequirementStatus,
    _classify_missing,
    _observation_key,
)
from trading_system.models.market_data import BarTimeframe


def qualify_native_candidates(
    database,
    config,
    prepared,
    definition,
    *,
    native_sessions=None,
    retain_partial=False,
    observation_bars=0,
    error_prefix="RESEARCH",
):
    before_queries = database.sql_queries
    started = perf_counter()
    observations = database.sync_values(PROVIDER_OBSERVATION_SOURCE)
    verification_seconds = perf_counter() - started
    preparation_seconds = 0.0
    loaded = 0
    candidates_by_key = {(row.symbol, row.session): row for row in prepared.candidates}
    batches = iter(
        database.iter_native_session_batches(
            (row.symbol, row.session) for row in prepared.candidates
        )
    )
    while True:
        started = perf_counter()
        batch = next(batches, None)
        preparation_seconds += perf_counter() - started
        if batch is None:
            break
        native, native_count = batch
        loaded += native_count
        for key, bars in native.items():
            started = perf_counter()
            candidate = candidates_by_key[key]
            requirement = CandidateIntradayRequirement(
                candidate.symbol,
                candidate.session,
                BarTimeframe.MINUTES_15,
                (definition.research_id,),
            )
            expected = expected_native_timestamps(candidate.session, BarTimeframe.MINUTES_15, False)
            present = {bar.timestamp for bar in bars}
            missing = tuple(timestamp for timestamp in expected if timestamp not in present)
            status, reason = _classify_missing(
                requirement,
                missing,
                observations.get(_observation_key(requirement)),
                feed=config.universe.market_data_feed,
                adjustment=config.universe.market_data_adjustment,
                extended_hours=False,
            )
            native_valid = True
            try:
                validate_native_session(
                    candidate.symbol, candidate.session, bars, error_prefix=error_prefix
                )
            except ValueError as exc:
                native_valid = False
                # Invalid OHLC or off-grid native rows cannot be excused by absence evidence.
                status = IntradayRequirementStatus.PROVIDER_CHECK_FAILED
                reason = str(exc)
            prepared.coverage.append(
                {
                    "session": candidate.session.isoformat(),
                    "symbol": candidate.symbol,
                    "daily_universe_rank": candidate.daily_universe_rank,
                    "timeframe": "15m",
                    "status": status.value,
                    "reason": reason,
                    "expected_bars": len(expected),
                    "native_bars_present": len(bars),
                    "missing_timestamps": [timestamp.isoformat() for timestamp in missing],
                }
            )
            if observation_bars:
                prepared.coverage[-1]["observation_window_observable"] = (
                    native_valid
                    and len(expected) >= observation_bars
                    and not set(expected[:observation_bars]).intersection(missing)
                )
            verification_seconds += perf_counter() - started
            if native_sessions is not None and (
                status == IntradayRequirementStatus.REQUIRED_PRESENT
                or (
                    retain_partial and status == IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT
                )
            ):
                started = perf_counter()
                native_sessions.add(*candidate.key, bars, candidate.previous_close)
                preparation_seconds += perf_counter() - started
    prepared.coverage.sort(key=lambda row: (row["session"], row["daily_universe_rank"]))
    prepared.performance.update(
        coverage_verification_seconds=verification_seconds,
        native_intraday_prepare_seconds=preparation_seconds,
        coverage_sql_queries=database.sql_queries - before_queries,
        native_bars_loaded=loaded,
        native_cache_peak=0,
        diagnostics_seconds=0.0,
    )
    prepared.performance[f"{error_prefix.lower()}_simulation_seconds"] = 0.0
    prepared.performance[f"sqlite_query_count_{error_prefix.lower()}_simulation"] = 0
    return prepared
