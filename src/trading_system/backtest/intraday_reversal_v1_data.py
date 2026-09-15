"""Local Reversal V1 candidate preparation; no ranking of intraday returns or outcomes."""

from time import perf_counter

from trading_system.backtest.intraday_reversal_v1_manifest import load_candidate_manifest
from trading_system.backtest.liquid_universe import (
    LiquidPreparation,
    ResearchReadOnlyDatabase,
    discover_liquid_universe,
)
from trading_system.backtest.native_candidate_data import qualify_native_candidates
from trading_system.backtest.research_definitions import INTRADAY_REVERSAL_V1 as REVERSAL_V1
from trading_system.models.market_data import BarTimeframe


def discover_reversal_universe(database, config, start, end):
    return discover_liquid_universe(
        database, config, start, end, REVERSAL_V1, error_prefix="REVERSAL"
    )


def prepare_reversal_data(
    database, config, start, end, *, native_sessions=None, candidate_manifest=None
):
    database = ResearchReadOnlyDatabase(database.path)
    if candidate_manifest is None:
        prepared = discover_reversal_universe(database, config, start, end)
    else:
        started = perf_counter()
        payload, candidates, sessions = load_candidate_manifest(
            candidate_manifest, database, config, start, end
        )
        prepared = LiquidPreparation(
            start,
            end,
            sessions,
            candidates,
            payload["daily_qualification"],
            performance={
                "daily_universe_discovery_seconds": 0.0,
                "candidate_discovery_seconds": 0.0,
                "candidate_manifest_load_seconds": perf_counter() - started,
            },
            source_fingerprint=payload["source_fingerprint"],
            candidate_source="MANIFEST",
            candidate_manifest_path=str(candidate_manifest),
            candidate_manifest_fingerprint=payload["fingerprint"],
        )
    # Partial first hours are retained independently of subsequent outcome availability.
    prepared = qualify_native_candidates(
        database,
        config,
        prepared,
        REVERSAL_V1,
        native_sessions=native_sessions,
        retain_partial=True,
        observation_bars=REVERSAL_V1.first_hour_bars,
        error_prefix="REVERSAL",
    )
    for row in prepared.coverage:
        row["first_hour_observable"] = row.pop("observation_window_observable")
    return prepared


def reversal_requirements_report(prepared, config):
    candidates = [
        {
            "symbol": row.symbol,
            "signal_session": row.previous_session.isoformat(),
            "execution_session": row.session.isoformat(),
            "daily_universe_rank": row.daily_universe_rank,
            "previous_close": row.previous_close,
            "average_dollar_volume_20d": row.average_dollar_volume_20d,
            "candidate_paths": [REVERSAL_V1.research_id],
            "requirement_type": "candidate_session",
        }
        for row in prepared.candidates
    ]
    return {
        "research_family": REVERSAL_V1.research_family,
        "research_id": REVERSAL_V1.research_id,
        "discovery_complete": prepared.daily_qualification["ready"],
        "requested_start": prepared.requested_start.isoformat(),
        "requested_end": prepared.requested_end.isoformat(),
        "strategies": [REVERSAL_V1.research_id],
        "candidate_sessions": candidates,
        "required_sessions": candidates,
        "timeframes": ["15m"],
        "native_warmup_bars": 0,
        "extended_hours": False,
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "universe_definition": REVERSAL_V1.universe_name,
    }


def reversal_remediation_warmup(payload, config, timeframes, extended_hours):
    if not (
        payload.get("research_family") == REVERSAL_V1.research_family
        and payload.get("research_id") == REVERSAL_V1.research_id
        and payload.get("timeframes") == ["15m"]
        and tuple(timeframes) == (BarTimeframe.MINUTES_15,)
        and payload.get("native_warmup_bars") == 0
        and payload.get("extended_hours") is False
        and not extended_hours
        and payload.get("market_data_feed") == config.universe.market_data_feed.upper()
        and payload.get("market_data_adjustment") == config.universe.market_data_adjustment
    ):
        raise ValueError(
            "Reversal remediation requires matching feed/adjustment, native 15m, regular hours"
        )
    return 0
