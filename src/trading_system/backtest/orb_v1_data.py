"""Local, read-only PIT universe and native coverage qualification; no ORB outcomes."""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import groupby
from time import perf_counter

from trading_system.backtest.orb_v1 import ORB_V1, OrbCandidate, validate_native_session
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayRequirementStatus,
    _classify_missing,
    _expected_timestamps,
    _observation_key,
)
from trading_system.data.market_sessions import daily_warmup_start, trading_sessions_between
from trading_system.models.market_data import BarTimeframe


class OrbReadOnlyDatabase(Database):
    """Force inherited identity helpers onto read-only connections and count actual reads."""

    def __init__(self, path):
        super().__init__(path)
        self.sql_queries = 0

    @contextmanager
    def read_only(self):
        with super().read_only() as connection:

            def trace(statement):
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    self.sql_queries += 1

            connection.set_trace_callback(trace)
            yield connection

    connect = read_only


@dataclass
class OrbPreparation:
    requested_start: date
    requested_end: date
    sessions: list[date]
    candidates: list[OrbCandidate]
    daily_qualification: dict
    coverage: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)

    @property
    def ready(self):
        admitted = {"REQUIRED_PRESENT", "PROVIDER_CONFIRMED_ABSENT"}
        return self.daily_qualification["ready"] and all(
            row["status"] in admitted for row in self.coverage
        )


def discover_orb_universe(database, config, start, end) -> OrbPreparation:
    """20 consecutive official Daily sessions, ending at T-1. No current snapshots.

    Bounded symbol batches and rolling sums; only the top 100 per session survive
    each batch. Missing/duplicate/invalid Daily observations cannot supply a window.
    """
    started = perf_counter()
    sessions = trading_sessions_between(start, end)
    if not sessions:
        raise ValueError("ORB_NO_REQUESTED_TRADING_SESSIONS")
    # The installed calendar's sessions_window includes its anchor. Request the
    # anchor plus 20 prior sessions; leave historical callers of the helper alone.
    first = daily_warmup_start(sessions[0], ORB_V1.daily_lookback_sessions + 1)
    history = trading_sessions_between(first, sessions[-1])
    session_set = set(sessions)
    next_session = dict(zip(history, history[1:], strict=False))
    conflicts = database.unresolved_sec_identity_conflict_symbols()
    symbols = sorted({company.symbol for company in database.list_tradable_companies()} - conflicts)
    top = {session: [] for session in sessions}
    complete = dict.fromkeys(sessions, 0)
    survivors = dict.fromkeys(sessions, 0)
    invalid_daily_bars = 0
    minimum_price = Decimal(str(config.universe.min_price))
    minimum_volume = Decimal(str(config.universe.min_avg_dollar_volume_20d))
    for batch in database.iter_bar_value_batches(symbols, first, history[-2], batch_size=100):
        for symbol, rows in groupby(batch, key=lambda row: row[0]):
            daily = {}
            for _, timestamp, high, low, close, volume in rows:
                session = date.fromisoformat(timestamp[:10])
                close, high, low = Decimal(str(close)), Decimal(str(high)), Decimal(str(low))
                if session in daily or not (
                    close.is_finite()
                    and high.is_finite()
                    and low.is_finite()
                    and 0 < low <= close <= high
                    and volume >= 0
                ):
                    daily[session] = None
                    invalid_daily_bars += 1
                else:
                    daily[session] = (close, close * volume)
            window = deque()
            total = Decimal(0)
            missing = 0
            for previous in history[:-1]:
                observation = daily.get(previous)
                window.append(observation)
                total += observation[1] if observation else 0
                missing += observation is None
                if len(window) > ORB_V1.daily_lookback_sessions:
                    removed = window.popleft()
                    total -= removed[1] if removed else 0
                    missing -= removed is None
                execution = next_session[previous]
                if (
                    execution not in session_set
                    or len(window) < ORB_V1.daily_lookback_sessions
                    or missing
                ):
                    continue
                complete[execution] += 1
                close = observation[0]
                average = total / ORB_V1.daily_lookback_sessions
                if close >= minimum_price and average >= minimum_volume:
                    survivors[execution] += 1
                    top[execution].append((average, symbol, close, previous))
        for session in sessions:
            top[session] = sorted(top[session], key=lambda item: (-item[0], item[1]))[
                : ORB_V1.top_n
            ]
    candidates = [
        OrbCandidate(session, symbol, previous, rank, float(close), float(average))
        for session in sessions
        for rank, (average, symbol, close, previous) in enumerate(top[session], 1)
    ]
    daily_qualification = {
        "ready": bool(symbols) and all(complete.values()),
        "warmup_start": first.isoformat(),
        "required_completed_sessions": 20,
        "current_tradable_company_count_after_identity_exclusions": len(symbols),
        "identity_conflict_symbols_excluded": sorted(conflicts),
        "invalid_or_duplicate_daily_bars": invalid_daily_bars,
        "sessions": [
            {
                "session": session.isoformat(),
                "complete_daily_windows": complete[session],
                "unavailable_daily_windows": len(symbols) - complete[session],
                "liquid_survivors": survivors[session],
                "selected": len(top[session]),
            }
            for session in sessions
        ],
    }
    return OrbPreparation(
        start,
        end,
        sessions,
        candidates,
        daily_qualification,
        performance={"daily_universe_discovery_seconds": perf_counter() - started},
    )


def prepare_orb_data(database, config, start, end, *, native_sessions=None) -> OrbPreparation:
    """Qualify exact requirements once; optionally spool only fully observable sessions.

    Preflight omits the spool and never evaluates breakout prices. Validation uses
    the same qualification, writing native batches to an owned bounded-memory spool.
    """
    database = OrbReadOnlyDatabase(database.path)
    prepared = discover_orb_universe(database, config, start, end)
    before_queries = database.sql_queries
    started = perf_counter()
    observations = database.sync_values(PROVIDER_OBSERVATION_SOURCE)
    verification_seconds = perf_counter() - started
    preparation_seconds = 0.0
    loaded = 0
    candidates_by_key = {(row.symbol, row.session): row for row in prepared.candidates}
    batches = iter(database.iter_entry_coverage_batches(row.key for row in prepared.candidates))
    while True:
        started = perf_counter()
        batch = next(batches, None)
        preparation_seconds += perf_counter() - started
        if batch is None:
            break
        native, _, native_count, _ = batch
        loaded += native_count
        for key, bars in native.items():
            started = perf_counter()
            candidate = candidates_by_key[key]
            requirement = CandidateIntradayRequirement(
                candidate.symbol,
                candidate.session,
                BarTimeframe.MINUTES_15,
                (ORB_V1.research_id,),
            )
            expected = _expected_timestamps(
                candidate.session, BarTimeframe.MINUTES_15, extended_hours=False
            )
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
            try:
                validate_native_session(candidate.symbol, candidate.session, bars)
            except ValueError as exc:
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
            verification_seconds += perf_counter() - started
            if native_sessions is not None and status == IntradayRequirementStatus.REQUIRED_PRESENT:
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
        orb_simulation_seconds=0.0,
        diagnostics_seconds=0.0,
        sqlite_query_count_orb_simulation=0,
    )
    return prepared


def orb_requirements_report(prepared, config):
    candidates = [
        {
            "symbol": row.symbol,
            "signal_session": row.previous_session.isoformat(),
            "execution_session": row.session.isoformat(),
            "daily_universe_rank": row.daily_universe_rank,
            "previous_close": row.previous_close,
            "average_dollar_volume_20d": row.average_dollar_volume_20d,
            "candidate_paths": [ORB_V1.research_id],
            "requirement_type": "candidate_session",
        }
        for row in prepared.candidates
    ]
    return {
        "research_family": ORB_V1.research_family,
        "research_id": ORB_V1.research_id,
        "discovery_complete": prepared.daily_qualification["ready"],
        "requested_start": prepared.requested_start.isoformat(),
        "requested_end": prepared.requested_end.isoformat(),
        "strategies": [ORB_V1.research_id],
        "candidate_sessions": candidates,
        "required_sessions": candidates,
        "timeframes": ["15m"],
        "native_warmup_bars": 0,
        "extended_hours": False,
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "universe_definition": ORB_V1.universe_name,
    }


def orb_remediation_warmup(payload, config, timeframes, extended_hours):
    """Narrow manifest contract: legacy candidate reports keep their existing warmup."""
    if payload.get("research_family") != ORB_V1.research_family:
        return config.intraday.warmup_bars
    if not (
        payload.get("research_id") == ORB_V1.research_id
        and payload.get("timeframes") == ["15m"]
        and tuple(timeframes) == (BarTimeframe.MINUTES_15,)
        and payload.get("native_warmup_bars") == 0
        and payload.get("extended_hours") is False
        and not extended_hours
        and payload.get("market_data_feed") == config.universe.market_data_feed.upper()
        and payload.get("market_data_adjustment") == config.universe.market_data_adjustment
    ):
        raise ValueError(
            "ORB remediation requires matching feed/adjustment, native 15m, regular hours"
        )
    return 0
