"""Versioned outcome-clean pair membership and explicit semantic drift checks."""

import json
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from trading_system.backtest import research_definitions as definitions
from trading_system.backtest.liquid_universe import _canonical, fingerprint
from trading_system.backtest.pairs_stat_arb_v1 import calibration
from trading_system.backtest.pairs_stat_arb_v1_security import (
    COMPANY_ONLY_REQUIREMENT,
    INSTRUMENT_TYPE_SEMANTICS,
)
from trading_system.backtest.pairs_stat_arb_v1_selection import SELECTION_GAP_SEMANTICS
from trading_system.data.market_sessions import trading_sessions_between

MISMATCH = "PAIRS_STAT_ARB_MANIFEST_MISMATCH"
HYPOTHESIS = (
    "Within highly liquid companies from the same SIC2 industry group,\n"
    "pairs with strong recent Daily return correlation may temporarily\n"
    "diverge in relative price.\n\n"
    "An extreme positive relative-price deviation is expected to mean-revert:\n"
    "short the relatively expensive member and long the relatively cheap member.\n\n"
    "An extreme negative deviation is treated symmetrically."
)


def calendar_window(start, end):
    definition = definitions.PAIRS_STAT_ARB_V1
    days = trading_sessions_between(start, end)
    if not days:
        raise ValueError("PAIRS_STAT_ARB_NO_SIGNAL_SESSIONS")
    lookback = max(definition.calibration_sessions, definition.adv_lookback_sessions)
    prior = trading_sessions_between(days[0] - timedelta(days=lookback * 3), days[0])[:-1][
        -lookback:
    ]
    tail_length = definition.outcome_tail_sessions
    tail = trading_sessions_between(end + timedelta(days=1), end + timedelta(days=tail_length * 4))[
        :tail_length
    ]
    if len(prior) != lookback or len(tail) != tail_length:
        raise ValueError("PAIRS_STAT_ARB_CALENDAR_UNAVAILABLE")
    return prior + days + tail, days, tail


def manifest_contract(config, start, end):
    definition = definitions.PAIRS_STAT_ARB_V1
    sessions, days, tail = calendar_window(start, end)
    if config.universe.market_data_adjustment == "raw":
        raise ValueError("PAIRS_STAT_ARB_REQUIRES_ADJUSTED_DAILY")
    return {
        "manifest_type": "pairs_stat_arb_v1_manifest",
        "manifest_version": 3,
        "coverage_semantics": (
            "SELECTED_ADV_PAIR_CALIBRATION_ELIGIBLE_OUTCOMES;PREFIX_HISTORY_NONBLOCKING;"
            "INTERNAL_SELECTION_GAPS_BLOCKING"
        ),
        "selection_gap_semantics": SELECTION_GAP_SEMANTICS,
        "selection_membership_fail_closed": True,
        "instrument_type_semantics": INSTRUMENT_TYPE_SEMANTICS,
        "company_only_requirement": COMPANY_ONLY_REQUIREMENT,
        "source_input_scope": "SOURCE_FINGERPRINT_INPUT_NOT_AUTOMATICALLY_VALIDATION_BLOCKING",
        "hypothesis": HYPOTHESIS,
        "research_family": definition.research_family,
        "research_id": definition.research_id,
        "status_at_creation": definition.status,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "signal_start": days[0].isoformat(),
        "signal_end": days[-1].isoformat(),
        "signal_sessions": [str(day) for day in days],
        "history_sessions": [str(day) for day in sessions],
        "outcome_tail_sessions": [str(day) for day in tail],
        "strategy_definition": asdict(definition),
        "min_price_source": "config.universe.min_price",
        "min_price": config.universe.min_price,
        "min_adv20_source": "config.universe.min_avg_dollar_volume_20d",
        "min_adv20": config.universe.min_avg_dollar_volume_20d,
        "universe_semantics": "CURRENT_UNIVERSE_ONLY;NOT_SURVIVORSHIP_CLEAN",
        "sic_semantics": "CURRENT_LOCAL_SIC_NOT_PIT",
        "market_data_feed": config.universe.market_data_feed.upper(),
        "market_data_adjustment": config.universe.market_data_adjustment,
        "signal_z_deferred": True,
    }


def seal_manifest(contract, universe, selected, candidates, counts, source_fingerprint):
    payload = {
        **contract,
        "universe": universe,
        "selected": selected,
        "candidates": candidates,
        "discovery_counts": counts,
        "source_fingerprint": source_fingerprint,
    }
    payload["fingerprint"] = fingerprint(payload)
    return payload


def load_manifest(path, contract):
    """Check economics independently of the checksum before touching local prices."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint(payload):
            raise ValueError("fingerprint")
        for key, expected in contract.items():
            if _canonical(payload.get(key)) != _canonical(expected):
                raise ValueError(key)
        return payload
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"{MISMATCH}: {exc}") from exc


def verify_membership(payload, universe, digest, bars, sessions, discovery_counts):
    """Verify saved membership and calibration; never discover/rank replacement pairs."""
    try:
        definition = definitions.PAIRS_STAT_ARB_V1
        lookback = definition.adv_lookback_sessions
        if payload["universe"] != universe or payload["source_fingerprint"] != digest:
            raise ValueError("current identity/SIC or selection-period Daily inputs changed")
        if _canonical(payload["discovery_counts"]) != _canonical(discovery_counts):
            raise ValueError("selection input qualification or security type evidence changed")
        if not discovery_counts["selection_membership_resolved"]:
            raise ValueError("SELECTION_MEMBERSHIP_UNRESOLVED")
        if not discovery_counts["company_security_types_complete"]:
            raise ValueError("AUTHORITATIVE_COMPANY_SECURITY_TYPE_UNAVAILABLE")
        identities = {row["symbol"]: row for row in universe}
        indexes = {str(day): index for index, day in enumerate(sessions)}
        expected = []
        selected = payload["selected"]
        if list(selected) != payload["signal_sessions"]:
            raise ValueError("selected session membership")
        for day, groups in selected.items():
            for sic2, names in sorted(groups.items()):
                if not 1 <= len(names) <= definition.top_n:
                    raise ValueError("top-five membership")
                if names != sorted(names, key=lambda row: (-row["adv20"], row["symbol"])):
                    raise ValueError("liquidity ordering")
                if len({row["symbol"] for row in names}) != len(names):
                    raise ValueError("duplicate symbol")
                for row in names:
                    if (
                        identities[row["symbol"]]["sic2"] != sic2
                        or row["adv20"] < payload["min_adv20"]
                        or row["previous_close"] < payload["min_price"]
                    ):
                        raise ValueError("company/SIC/liquidity semantics")
                    index = indexes[day]
                    symbol = row["symbol"]
                    prior = bars.get((symbol, sessions[index - 1]))
                    window = [
                        bars.get((symbol, s)) for s in sessions[index - lookback + 1 : index + 1]
                    ]
                    if prior is None or any(bar is None for bar in window):
                        raise ValueError("selected Daily window")
                    adv = float(
                        sum(Decimal(str(bar.close)) * bar.volume for bar in window) / lookback
                    )
                    if row["adv20"] != adv or row["previous_close"] != prior.close:
                        raise ValueError("saved liquidity observations")
                by_symbol = {row["symbol"]: row for row in names}
                for a, b in combinations(sorted(by_symbol), 2):
                    stats = calibration(bars, sessions, indexes[day], a, b)
                    expected.append(
                        {
                            "signal_session": day,
                            "symbol_a": a,
                            "symbol_b": b,
                            "sic2": sic2,
                            "adv20_a": by_symbol[a]["adv20"],
                            "adv20_b": by_symbol[b]["adv20"],
                            **stats,
                        }
                    )
        if _canonical(expected) != _canonical(payload["candidates"]):
            raise ValueError("pair membership/calibration")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"{MISMATCH}: {exc}") from exc
