"""Read-only four-bar qualification; no signal or return calculations."""

from dataclasses import dataclass, field
from datetime import date
from itertools import chain, groupby
from time import perf_counter

from trading_system.backtest.liquid_universe import ResearchReadOnlyDatabase
from trading_system.backtest.market_intraday_momentum_v1 import MOMENTUM_V1, MomentumSession
from trading_system.backtest.market_intraday_momentum_v1_manifest import (
    discover_market_sessions,
    load_candidate_manifest,
)
from trading_system.backtest.native_session_grid import validate_native_session
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayRequirementStatus,
    _classify_missing,
    _observation_key,
)
from trading_system.models.market_data import BarTimeframe

REMEDIATION_NOTE = (
    "Only sessions with unresolved research-window gaps are exported for remediation. "
    "The existing generic remediation contract qualifies full sessions and requests the "
    "span from its earliest to latest missing bar. It may fetch unused middle bars or a "
    "whole affected session. Research validation still requires only the four frozen timestamps."
)


@dataclass
class MomentumPreparation:
    start: date
    end: date
    proxy: dict
    candidates: list[MomentumSession]
    coverage: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)
    candidate_source: str = "REDISCOVERED"
    candidate_manifest_path: str | None = None
    candidate_manifest_fingerprint: str | None = None

    @property
    def ready(self):
        return (
            bool(self.candidates)
            and len(self.coverage) == 2 * len(self.candidates)
            and all(
                row["status"] in {"REQUIRED_PRESENT", "PROVIDER_CONFIRMED_ABSENT"}
                for row in self.coverage
            )
        )


def prepare_momentum_data(
    database, config, start, end, *, native_sessions=None, candidate_manifest=None
):
    database = ResearchReadOnlyDatabase(database.path)
    started = perf_counter()
    if candidate_manifest is None:
        proxy, candidates = discover_market_sessions(database, start, end)
        prepared = MomentumPreparation(start, end, proxy, candidates)
    else:
        payload, proxy, candidates = load_candidate_manifest(
            candidate_manifest, database, config, start, end
        )
        prepared = MomentumPreparation(
            start,
            end,
            proxy,
            candidates,
            candidate_source="MANIFEST",
            candidate_manifest_path=str(candidate_manifest),
            candidate_manifest_fingerprint=payload["fingerprint"],
        )
    prepared.performance.update(
        candidate_discovery_seconds=perf_counter() - started if candidate_manifest is None else 0.0,
        candidate_manifest_load_seconds=perf_counter() - started if candidate_manifest else 0.0,
    )
    before = database.sql_queries
    started = perf_counter()
    observations = database.sync_values(PROVIDER_OBSERVATION_SOURCE)
    by_session = {row.session: row for row in candidates}
    requirements = (
        (MOMENTUM_V1.symbol, row.session, timestamp)
        for row in candidates
        for timestamp in row.required_timestamps
    )
    batches = database.iter_native_timestamp_batches(requirements)
    loaded = 0
    for (_, session), points in groupby(chain.from_iterable(batches), key=lambda item: item[:2]):
        candidate = by_session[session]
        bars = [bar for _, _, _, bar in points if bar is not None]
        loaded += len(bars)
        requirement = CandidateIntradayRequirement(
            MOMENTUM_V1.symbol, session, BarTimeframe.MINUTES_15, (MOMENTUM_V1.research_id,)
        )
        for window, expected in (
            ("MORNING_WINDOW", candidate.morning),
            ("CLOSING_WINDOW", candidate.final),
        ):
            present = [bar for bar in bars if bar.timestamp in expected]
            missing = tuple(
                timestamp
                for timestamp in expected
                if timestamp not in {bar.timestamp for bar in present}
            )
            status, reason = _classify_missing(
                requirement,
                missing,
                observations.get(_observation_key(requirement)),
                feed=config.universe.market_data_feed,
                adjustment=config.universe.market_data_adjustment,
                extended_hours=False,
            )
            try:
                validate_native_session(
                    MOMENTUM_V1.symbol, session, present, error_prefix="MARKET_INTRADAY_MOMENTUM"
                )
            except ValueError as exc:
                status, reason = IntradayRequirementStatus.PROVIDER_CHECK_FAILED, str(exc)
            prepared.coverage.append(
                {
                    "session": session.isoformat(),
                    "symbol": MOMENTUM_V1.symbol,
                    "window": window,
                    "timeframe": "15m",
                    "status": status.value,
                    "reason": reason,
                    "expected_bars": 2,
                    "native_bars_present": len(present),
                    "required_timestamps": [ts.isoformat() for ts in expected],
                    "missing_timestamps": [ts.isoformat() for ts in missing],
                }
            )
        if native_sessions is not None:
            # Retain partial windows for explicit unobservable terminal events.
            native_sessions.add(*candidate.key, bars, None)
    prepared.performance.update(
        native_qualification_seconds=perf_counter() - started,
        coverage_sql_queries=database.sql_queries - before,
        native_bars_loaded=loaded,
        simulation_seconds=0.0,
        sqlite_query_count_simulation=0,
        native_cache_peak=0,
    )
    return prepared


def momentum_requirements_report(prepared, config):
    unresolved = {
        row["session"]
        for row in prepared.coverage
        if row["status"] in {"LOCAL_MISSING_FETCHABLE", "PROVIDER_CHECK_FAILED"}
    }
    sessions = [
        {
            "symbol": MOMENTUM_V1.symbol,
            "execution_session": row.session.isoformat(),
            "candidate_paths": [MOMENTUM_V1.research_id],
            "requirement_type": "candidate_session",
            "research_required_timestamps": [ts.isoformat() for ts in row.required_timestamps],
        }
        for row in prepared.candidates
    ]
    return {
        "research_family": MOMENTUM_V1.research_family,
        "research_id": MOMENTUM_V1.research_id,
        "discovery_complete": True,
        "requested_start": prepared.start.isoformat(),
        "requested_end": prepared.end.isoformat(),
        "strategies": [MOMENTUM_V1.research_id],
        "candidate_sessions": sessions,
        "required_sessions": [row for row in sessions if row["execution_session"] in unresolved],
        "timeframes": ["15m"],
        "extended_hours": False,
        "native_warmup_bars": 0,
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "research_coverage_scope": "FIRST_TWO_AND_FINAL_TWO_NATIVE_BARS_ONLY",
        "remediation_scope": "EXISTING_FULL_SESSION_GRID_BOUNDED_MISSING_SPAN",
        "remediation_session_scope": "ONLY_UNRESOLVED_REQUIRED_WINDOW_SESSIONS",
        "candidate_manifest_fingerprint": prepared.candidate_manifest_fingerprint,
        "remediation_note": REMEDIATION_NOTE,
    }


def momentum_remediation_warmup(payload, config, timeframes, extended_hours):
    sessions = payload.get("required_sessions", [])
    if not (
        payload.get("research_family") == MOMENTUM_V1.research_family
        and payload.get("research_id") == MOMENTUM_V1.research_id
        and payload.get("timeframes") == ["15m"]
        and tuple(timeframes) == (BarTimeframe.MINUTES_15,)
        and payload.get("native_warmup_bars") == 0
        and payload.get("extended_hours") is False
        and not extended_hours
        and payload.get("market_data_feed") == config.universe.market_data_feed.upper()
        and payload.get("market_data_adjustment") == config.universe.market_data_adjustment
        and isinstance(sessions, list)
        and bool(sessions)
        and all(row.get("symbol") == MOMENTUM_V1.symbol for row in sessions)
        and all(
            row.get("symbol") == MOMENTUM_V1.symbol for row in payload.get("candidate_sessions", [])
        )
        and "potential_position_ranges" not in payload
    ):
        raise ValueError(
            "Market momentum remediation requires SPY, matching feed/adjustment, "
            "native 15m, regular hours"
        )
    return 0
