"""Small ORB-only candidate contract and content fingerprint; no intraday prices."""

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

from trading_system.backtest.orb_v1 import OrbCandidate
from trading_system.backtest.research_definitions import ORB_V1
from trading_system.data.market_sessions import daily_warmup_start, trading_sessions_between

MANIFEST_VERSION = 2
MISMATCH = "ORB_CANDIDATE_MANIFEST_MISMATCH"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(payload):
    """Integrity checksum, not a signature; all fields except the checksum itself participate."""
    return hashlib.sha256(
        _canonical({k: v for k, v in payload.items() if k != "fingerprint"})
    ).hexdigest()


def orb_universe_definition(config):
    return {
        "name": ORB_V1.universe_name,
        "asset_universe": "CURRENT_ALPACA_TRADABLE_US_EQUITY",
        "security_scope": "ALPACA_TRADABLE_US_EQUITY",
        "individual_stocks_only": False,
        "etfs_etps_may_be_included": True,
        "direct_crypto_allowed": False,
        "unknown_asset_class_allowed": False,
        "execution_eligibility_germany": "NOT_EVALUATED",
        "membership": "CURRENT_UNIVERSE_ONLY",
        "survivorship": "NOT_SURVIVORSHIP_CLEAN",
        "top_n": ORB_V1.top_n,
        "previous_close_min": config.universe.min_price,
        "average_dollar_volume_20d_min": config.universe.min_avg_dollar_volume_20d,
        "ranking": "20 completed Daily sessions through T-1: mean(close * volume) DESC, symbol ASC",
        "sec_identity_required": False,
        "intraday_absence_replacement": False,
    }


def manifest_contract(config, start, end):
    return {
        "manifest_version": MANIFEST_VERSION,
        "research_family": ORB_V1.research_family,
        "research_id": ORB_V1.research_id,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "strategy_definition": json.loads(_canonical(asdict(ORB_V1))),
        "universe_definition": orb_universe_definition(config),
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "extended_hours": False,
    }


def selection_basis(database, config, start, end):
    sessions = trading_sessions_between(start, end)
    if not sessions:
        raise ValueError("ORB_NO_REQUESTED_TRADING_SESSIONS")
    first = daily_warmup_start(sessions[0], ORB_V1.daily_lookback_sessions + 1)
    history = trading_sessions_between(first, sessions[-1])
    # One current asset load per run, before any Daily ranking; no SEC or provider reads.
    assets = database.list_tradable_assets()
    classes = Counter(asset.asset_class for asset in assets)
    return {
        "universe_definition": orb_universe_definition(config),
        "sessions": [s.isoformat() for s in sessions],
        "history_sessions": [s.isoformat() for s in history],
        "tradable_assets": sorted(
            (asset.symbol, asset.asset_class, asset.tradable) for asset in assets
        ),
        "asset_scope_diagnostics": {
            "tradable_assets_total": len(assets),
            "us_equity_assets_considered": classes["US_EQUITY"],
            "crypto_assets_excluded": classes["CRYPTO"],
            "unknown_asset_class_excluded": classes["UNKNOWN"],
            "other_asset_classes_excluded": sum(
                count
                for key, count in classes.items()
                if key not in {"US_EQUITY", "CRYPTO", "UNKNOWN"}
            ),
            "tradable_assets_by_class": dict(sorted(classes.items())),
        },
        "symbols": sorted(
            asset.symbol for asset in assets if asset.tradable and asset.asset_class == "US_EQUITY"
        ),
    }


def selection_digest(basis):
    return hashlib.sha256(_canonical(basis) + b"\n")


def iter_selection_batches(database, basis, digest):
    """Hash the exact rows consumed by discovery, or stream them only to verify reuse.

    Include all eligible membership histories, even those below rank 100/thresholds:
    correcting any of them could change selection. Exclude T/end and intraday data.
    """
    history = basis["history_sessions"]
    for batch in database.iter_bar_value_batches(
        basis["symbols"],
        date.fromisoformat(history[0]),
        date.fromisoformat(history[-2]),
        batch_size=100,
    ):
        for row in batch:
            digest.update(_canonical(row) + b"\n")
        yield batch


def candidate_manifest(prepared, config):
    candidates = [
        {
            **asdict(row),
            "session": row.session.isoformat(),
            "previous_session": row.previous_session.isoformat(),
        }
        for row in prepared.candidates
    ]
    payload = {
        **manifest_contract(config, prepared.requested_start, prepared.requested_end),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "daily_qualification": prepared.daily_qualification,
        "source_fingerprint": prepared.source_fingerprint,
    }
    payload["fingerprint"] = fingerprint(payload)
    return payload


def load_candidate_manifest(path, database, config, start, end):
    """Fail closed. Rehash source inputs, never rediscover/rerank or fetch native data."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint(payload):
            raise ValueError("fingerprint")
        for key, expected in manifest_contract(config, start, end).items():
            actual = payload.get(key)
            # Research disposition is not an economic input. Retain pre-rejection evidence.
            if (
                key == "strategy_definition"
                and isinstance(actual, dict)
                and actual.get("status") in {"ACTIVE", "REJECTED"}
            ):
                actual = {**actual, "status": expected["status"]}
            if _canonical(actual) != _canonical(expected):
                raise ValueError(key)
        basis = selection_basis(database, config, start, end)
        digest = selection_digest(basis)
        for _ in iter_selection_batches(database, basis, digest):
            pass
        if payload.get("source_fingerprint") != digest.hexdigest():
            raise ValueError("local asset membership/class/Daily source fingerprint")
        sessions = [date.fromisoformat(s) for s in basis["sessions"]]
        session_set = set(sessions)
        history = [date.fromisoformat(s) for s in basis["history_sessions"]]
        previous = dict(zip(history[1:], history, strict=False))
        raw_candidates = payload["candidates"]
        if (
            not isinstance(raw_candidates, list)
            or type(payload["candidate_count"]) is not int
            or payload["candidate_count"] != len(raw_candidates)
        ):
            raise ValueError("candidate_count")
        candidates = []
        ranks = Counter()
        seen = set()
        symbols = set(basis["symbols"])
        for row in raw_candidates:
            candidate = OrbCandidate(
                **{
                    **row,
                    "session": date.fromisoformat(row["session"]),
                    "previous_session": date.fromisoformat(row["previous_session"]),
                }
            )
            ranks[candidate.session] += 1
            if (
                candidate.session not in session_set
                or candidate.previous_session != previous[candidate.session]
                or candidate.symbol not in symbols
                or (candidate.symbol, candidate.session) in seen
                or type(candidate.daily_universe_rank) is not int
                or candidate.daily_universe_rank != ranks[candidate.session]
                or not 1 <= candidate.daily_universe_rank <= ORB_V1.top_n
                or not math.isfinite(candidate.previous_close)
                or not math.isfinite(candidate.average_dollar_volume_20d)
                or candidate.previous_close < config.universe.min_price
                or candidate.average_dollar_volume_20d < config.universe.min_avg_dollar_volume_20d
            ):
                raise ValueError("candidate row")
            seen.add((candidate.symbol, candidate.session))
            candidates.append(candidate)
        if candidates != sorted(candidates, key=lambda row: (row.session, row.daily_universe_rank)):
            raise ValueError("candidate ordering")
        evidence = payload["daily_qualification"]
        if evidence["ready"] is not True or evidence["required_completed_sessions"] != 20:
            raise ValueError("Daily qualification")
        if any(
            evidence.get(key) != value for key, value in basis["asset_scope_diagnostics"].items()
        ):
            raise ValueError("asset scope diagnostics")
        if [row["session"] for row in evidence["sessions"]] != basis["sessions"] or any(
            row["selected"] != ranks[date.fromisoformat(row["session"])]
            for row in evidence["sessions"]
        ):
            raise ValueError("Daily qualification candidate counts")
        return payload, candidates, sessions
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"{MISMATCH}: {exc}") from exc
