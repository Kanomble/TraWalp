"""Dedicated Reversal V1 compatibility contract over generic local source evidence."""

import json
from dataclasses import asdict

from trading_system.backtest import liquid_universe as shared
from trading_system.backtest.research_definitions import INTRADAY_REVERSAL_V1 as REVERSAL_V1

MANIFEST_VERSION = 1
MISMATCH = "REVERSAL_CANDIDATE_MANIFEST_MISMATCH"


def reversal_universe_definition(config):
    return shared.liquid_universe_definition(config, REVERSAL_V1)


def manifest_contract(config, start, end):
    return {
        "manifest_version": MANIFEST_VERSION,
        "research_family": REVERSAL_V1.research_family,
        "research_id": REVERSAL_V1.research_id,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "strategy_definition": json.loads(shared._canonical(asdict(REVERSAL_V1))),
        "universe_definition": reversal_universe_definition(config),
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "extended_hours": False,
    }


def candidate_manifest(prepared, config):
    return shared.candidate_manifest(
        prepared, manifest_contract(config, prepared.requested_start, prepared.requested_end)
    )


def load_candidate_manifest(path, database, config, start, end):
    return shared.load_candidate_manifest(
        path,
        database,
        config,
        start,
        end,
        REVERSAL_V1,
        manifest_contract(config, start, end),
        mismatch=MISMATCH,
    )
