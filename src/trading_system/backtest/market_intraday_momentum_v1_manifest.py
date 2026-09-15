"""Dedicated immutable SPY/calendar manifest; no Daily or intraday price inputs."""

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from trading_system.backtest.liquid_universe import _canonical, fingerprint
from trading_system.backtest.market_intraday_momentum_v1 import (
    MOMENTUM_V1,
    MomentumSession,
    session_definition,
)
from trading_system.data.market_sessions import trading_sessions_between

MANIFEST_VERSION = 1
MISMATCH = "MARKET_INTRADAY_MOMENTUM_MANIFEST_MISMATCH"


def proxy_basis(database):
    assets = database.list_tradable_assets()  # one local membership load; no SEC/Daily lookup
    proxy = next((asset for asset in assets if asset.symbol == MOMENTUM_V1.symbol), None)
    if proxy is None or not proxy.tradable or proxy.asset_class != MOMENTUM_V1.asset_class:
        raise ValueError("SPY_MARKET_PROXY_UNAVAILABLE")
    return {"symbol": proxy.symbol, "asset_class": proxy.asset_class, "tradable": proxy.tradable}


def requested_sessions(start, end):
    if start > end:
        raise ValueError("Market momentum start must not be after end")
    sessions = trading_sessions_between(start, end)
    if not sessions:
        raise ValueError("MARKET_INTRADAY_MOMENTUM_NO_TRADING_SESSIONS")
    return sessions


def discover_market_sessions(database, start, end):
    proxy = proxy_basis(database)
    return proxy, [session_definition(day) for day in requested_sessions(start, end)]


def session_record(candidate):
    return {
        "session": candidate.session.isoformat(),
        "symbol": MOMENTUM_V1.symbol,
        "opening": candidate.opening.isoformat(),
        "closing": candidate.closing.isoformat(),
        "morning": [timestamp.isoformat() for timestamp in candidate.morning],
        "final": [timestamp.isoformat() for timestamp in candidate.final],
    }


def manifest_contract(config, start, end):
    return {
        "manifest_version": MANIFEST_VERSION,
        "research_family": MOMENTUM_V1.research_family,
        "research_id": MOMENTUM_V1.research_id,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "strategy_definition": json.loads(_canonical(asdict(MOMENTUM_V1))),
        "calendar": MOMENTUM_V1.calendar,
        "timezone": MOMENTUM_V1.timezone,
        "timeframe": "15m",
        "extended_hours": False,
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "native_timestamp_semantics": "BAR_START; EXACT_FIRST_TWO_AND_FINAL_TWO_REGULAR_BARS",
        "proxy_membership": "CURRENT_LOCAL_ALPACA_MEMBERSHIP",
        "daily_history_required": False,
    }


def candidate_manifest(prepared, config):
    payload = {
        **manifest_contract(config, prepared.start, prepared.end),
        "proxy": prepared.proxy,
        "candidate_count": len(prepared.candidates),
        "candidates": [session_record(candidate) for candidate in prepared.candidates],
    }
    payload["fingerprint"] = fingerprint(payload)
    return payload


def load_candidate_manifest(path, database, config, start, end):
    """Verify current membership and calendar semantics, never call candidate discovery.

    Native prices are requalified separately, so explicit remediation can add bars.
    Recomputed JSON checksums cannot authorize changed windows or strategy economics.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint(payload):
            raise ValueError("fingerprint")
        for key, value in manifest_contract(config, start, end).items():
            saved = payload.get(key)
            if key == "strategy_definition" and isinstance(saved, dict):
                # Preserve pre-decision reproduction after verifying the original checksum.
                if saved.get("status") not in {"ACTIVE", "REJECTED"}:
                    raise ValueError("research status")
                saved = {**saved, "status": value["status"]}
            if _canonical(saved) != _canonical(value):
                raise ValueError(key)
        proxy = proxy_basis(database)
        if _canonical(payload["proxy"]) != _canonical(proxy):
            raise ValueError("SPY asset membership/class")
        days = requested_sessions(start, end)
        rows = payload["candidates"]
        if (
            type(payload["candidate_count"]) is not int
            or payload["candidate_count"] != len(days)
            or len(rows) != len(days)
        ):
            raise ValueError("candidate count")
        candidates = []
        for day, row in zip(days, rows, strict=True):
            # Validate every saved bound against the current official calendar, not prices.
            if _canonical(row) != _canonical(session_record(session_definition(day))):
                raise ValueError("XNYS session/window drift")
            candidates.append(
                MomentumSession(
                    date.fromisoformat(row["session"]),
                    datetime.fromisoformat(row["opening"]),
                    datetime.fromisoformat(row["closing"]),
                    tuple(datetime.fromisoformat(ts) for ts in row["morning"]),
                    tuple(datetime.fromisoformat(ts) for ts in row["final"]),
                )
            )
        return payload, proxy, candidates
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ValueError(f"{MISMATCH}: {exc}") from exc
