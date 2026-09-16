"""Read-only local identity, Daily batches and outcome-clean pair discovery."""

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from trading_system.backtest import research_definitions as definitions
from trading_system.backtest.liquid_universe import ResearchReadOnlyDatabase, _canonical
from trading_system.backtest.pairs_stat_arb_v1 import PairBar, calibration
from trading_system.backtest.pairs_stat_arb_v1_manifest import (
    calendar_window,
    load_manifest,
    manifest_contract,
    seal_manifest,
    verify_membership,
)
from trading_system.data.qualification import provider_range_verified


@dataclass
class PreparedPairs:
    start: date
    end: date
    sessions: list
    signal_dates: dict
    bars: dict
    manifest: dict
    coverage: list
    requirements: list
    metadata: dict


def company_universe(database):
    assets = {a.symbol: a for a in database.list_tradable_assets()}
    result = []
    for company in database.list_tradable_companies():
        asset = assets.get(company.symbol)
        sic = (company.sic or "").strip()
        if (
            asset is None
            or company.symbol == definitions.PAIRS_STAT_ARB_V1.benchmark_symbol
            or not asset.tradable
            or asset.asset_class != definitions.PAIRS_STAT_ARB_V1.asset_class
            or not re.fullmatch(r"[0-9]{3,4}", sic)
            or int(sic) == 0
        ):
            continue
        sic = sic.zfill(4)
        result.append(
            {
                "symbol": company.symbol,
                "cik": company.cik,
                "sic": sic,
                "sic2": sic[:2],
                "tradable": True,
                "asset_class": definitions.PAIRS_STAT_ARB_V1.asset_class,
            }
        )
    return sorted(result, key=lambda row: row["symbol"])


def load_daily(database, symbols, start, end):
    """Batch-load compact bars; invalid/duplicate sessions are unusable, never filled."""
    bars = {}
    symbols = sorted(set(symbols))
    with database.read_only() as connection:
        for offset in range(0, len(symbols), 300):
            batch = symbols[offset : offset + 300]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT symbol,timestamp,open,high,low,close,volume FROM bars "
                f"WHERE symbol IN ({placeholders}) AND timeframe=? "
                "AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
                [
                    *batch,
                    definitions.PAIRS_STAT_ARB_V1.timeframe,
                    str(start),
                    str(end + timedelta(days=1)),
                ],
            )
            for symbol, timestamp, op, high, low, close, volume in rows:
                day = date.fromisoformat(timestamp[:10])
                key = symbol, day
                try:
                    op, high, low, close = map(float, (op, high, low, close))
                    valid = (
                        all(math.isfinite(v) for v in (op, high, low, close))
                        and 0 < low <= min(op, close) <= max(op, close) <= high
                        and isinstance(volume, int)
                        and volume >= 0
                    )
                except (TypeError, ValueError, OverflowError):
                    valid = False
                bars[key] = (
                    PairBar(op, high, low, close, volume) if valid and key not in bars else None
                )
    return bars


def source_digest(universe, bars, sessions):
    digest = hashlib.sha256(_canonical(universe))
    # Includes missing inputs and all potential members, not only selected winners.
    for identity in universe:
        symbol = identity["symbol"]
        for day in sessions:
            bar = bars.get((symbol, day))
            values = [bar.open, bar.high, bar.low, bar.close, bar.volume] if bar else None
            digest.update(_canonical([symbol, str(day), values]))
    return digest.hexdigest()


def discover_pairs(universe, bars, sessions, days, config):
    definition = definitions.PAIRS_STAT_ARB_V1
    lookback = definition.adv_lookback_sessions
    selected, candidates = {}, []
    indexes = {day: index for index, day in enumerate(sessions)}
    counts = {"eligible_symbol_sessions": 0, "unavailable_liquidity_symbol_sessions": 0}
    unavailable = {}
    for day in days:
        index = indexes[day]
        groups = defaultdict(list)
        for identity in universe:
            symbol = identity["symbol"]
            previous = bars.get((symbol, sessions[index - 1]))
            window = [bars.get((symbol, s)) for s in sessions[index - lookback + 1 : index + 1]]
            if previous is None or len(window) != lookback or any(bar is None for bar in window):
                counts["unavailable_liquidity_symbol_sessions"] += 1
                diagnostic = unavailable.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "session": str(day),
                        "session_end": str(day),
                        "sessions": 0,
                        "record_kind": "SYMBOL_SUMMARY",
                        "timeframe": definition.timeframe,
                        "input_scope": "SELECTION_DIAGNOSTIC",
                        "validation_blocking": False,
                        "roles": "LIQUIDITY_SELECTION",
                        "status": "INSUFFICIENT_SELECTION_HISTORY",
                    },
                )
                diagnostic["sessions"] += 1
                diagnostic["session_end"] = str(day)
                continue
            adv = sum(Decimal(str(bar.close)) * bar.volume for bar in window) / lookback
            if previous.close >= config.universe.min_price and adv >= Decimal(
                str(config.universe.min_avg_dollar_volume_20d)
            ):
                counts["eligible_symbol_sessions"] += 1
                groups[identity["sic2"]].append(
                    {"symbol": symbol, "adv20": float(adv), "previous_close": previous.close}
                )
        retained = {
            sic2: sorted(names, key=lambda row: (-row["adv20"], row["symbol"]))[: definition.top_n]
            for sic2, names in sorted(groups.items())
        }
        selected[str(day)] = retained
        for sic2, names in retained.items():
            by_symbol = {row["symbol"]: row for row in names}
            for a, b in combinations(sorted(by_symbol), 2):
                candidates.append(
                    {
                        "signal_session": str(day),
                        "symbol_a": a,
                        "symbol_b": b,
                        "sic2": sic2,
                        "adv20_a": by_symbol[a]["adv20"],
                        "adv20_b": by_symbol[b]["adv20"],
                        **calibration(bars, sessions, index, a, b),
                    }
                )
    counts["selection_history_diagnostics"] = [
        unavailable[symbol] for symbol in sorted(unavailable)
    ]
    return selected, candidates, counts


def daily_requirements(selected, sessions, candidates, bars):
    """Only frozen selection/pair inputs, never the source-fingerprint Cartesian product.

    Missing calibration prefixes with no earlier local observation are insufficient
    history, not inferred provider gaps. Missing bars after an observed history start
    in a required candidate window remain blocking. No post-T observation establishes
    a candidate's history: each selected symbol already has a complete ADV window at T.
    """
    definition = definitions.PAIRS_STAT_ARB_V1
    required = defaultdict(set)
    insufficient_calibration = set()
    indexes = {str(day): i for i, day in enumerate(sessions)}
    for signal, groups in selected.items():
        i = indexes[signal]
        for names in groups.values():
            for name in names:
                symbol = name["symbol"]
                for day in sessions[i - definition.adv_lookback_sessions + 1 : i + 1]:
                    required[symbol, day].add("SELECTED_ADV_WINDOW")
                required[symbol, sessions[i - 1]].add("SELECTED_PREVIOUS_CLOSE")
    first_observed = {}
    for (symbol, day), bar in bars.items():
        if bar is not None:
            first_observed[symbol] = min(first_observed.get(symbol, day), day)
    for candidate in candidates:
        i = indexes[candidate["signal_session"]]
        for symbol in (candidate["symbol_a"], candidate["symbol_b"]):
            for day in sessions[i - definition.calibration_sessions : i + 1]:
                if day < first_observed[symbol]:
                    insufficient_calibration.add((symbol, day))
                else:
                    required[symbol, day].add("PAIR_CALIBRATION_AND_OBSERVATION")
            if candidate["calibration_status"] == "ELIGIBLE":
                for day in sessions[i + 1 : i + definition.max_hold_sessions + 1]:
                    required[symbol, day].add("POSSIBLE_OUTCOME_ONLY")
    return required, insufficient_calibration


def compact_coverage(rows, sessions):
    """Compress adjacent official-session records; keep every actionable missing bar explicit."""
    indexes = {str(day): i for i, day in enumerate(sessions)}
    result = []
    for row in rows:
        comparable = {
            k: v for k, v in row.items() if k not in {"session", "session_end", "sessions"}
        }
        if result and row["status"] != "LOCAL_MISSING_FETCHABLE":
            last = result[-1]
            if (
                comparable
                == {
                    k: v for k, v in last.items() if k not in {"session", "session_end", "sessions"}
                }
                and indexes[row["session"]] == indexes[last["session_end"]] + 1
            ):
                last["session_end"] = row["session"]
                last["sessions"] += 1
                continue
        result.append({**row, "session_end": row["session"], "sessions": 1})
    return result


def prepare_pairs_data(database, config, start, end, *, candidate_manifest=None, preflight=False):
    contract = manifest_contract(config, start, end)
    payload = (
        load_manifest(candidate_manifest, contract) if candidate_manifest is not None else None
    )
    database = ResearchReadOnlyDatabase(database.path)
    sessions, days, tail = calendar_window(start, end)
    universe = company_universe(database)
    symbols = [row["symbol"] for row in universe]
    bars = load_daily(database, symbols, sessions[0], days[-1])
    digest = source_digest(universe, bars, [s for s in sessions if s <= days[-1]])
    if payload is None:
        selected, candidates, counts = discover_pairs(universe, bars, sessions, days, config)
        payload = seal_manifest(contract, universe, selected, candidates, counts, digest)
    else:
        verify_membership(payload, universe, digest, bars, sessions)
    required, insufficient_calibration = daily_requirements(
        payload["selected"], sessions, payload["candidates"], bars
    )
    tail_symbols = {symbol for symbol, day in required if day > end}
    # Discovery and source hashing above never receive post-T outcome data.
    bars.update(load_daily(database, tail_symbols, tail[0], tail[-1]))
    if not preflight:
        bars.update(
            load_daily(
                database, [definitions.PAIRS_STAT_ARB_V1.benchmark_symbol], days[0], tail[-1]
            )
        )
    coverage_state = database.sync_values("daily_history_coverage")
    conflicts = [
        symbol
        for symbol in symbols
        if isinstance(coverage_state.get(symbol), dict)
        and (
            coverage_state[symbol].get("adjustment")
            not in (None, config.universe.market_data_adjustment)
            or coverage_state[symbol].get("feed")
            not in (None, config.universe.market_data_feed.lower())
        )
    ]
    if conflicts:
        raise ValueError(f"PAIRS_STAT_ARB_DAILY_PROVENANCE_MISMATCH: {conflicts[:10]}")
    coverage = []
    for (symbol, day), roles in sorted(required.items()):
        present = bars.get((symbol, day)) is not None
        verified = provider_range_verified(
            coverage_state.get(symbol),
            day,
            day,
            feed=config.universe.market_data_feed.lower(),
            adjustment=config.universe.market_data_adjustment,
        )
        coverage.append(
            {
                "symbol": symbol,
                "session": str(day),
                "timeframe": definitions.PAIRS_STAT_ARB_V1.timeframe,
                "record_kind": "SESSION_RANGE",
                "input_scope": "VALIDATION_BLOCKING_REQUIREMENT",
                "validation_blocking": True,
                "roles": ";".join(sorted(roles)),
                "status": "REQUIRED_PRESENT" if present else "LOCAL_MISSING_FETCHABLE",
                "provider_range_verified": verified,
                "provider_session_absence": "NOT_ESTABLISHED",
            }
        )
    coverage = compact_coverage(coverage, sessions)
    requirements = list(coverage)
    coverage.extend(
        compact_coverage(
            [
                {
                    "symbol": symbol,
                    "session": str(day),
                    "record_kind": "SESSION_RANGE",
                    "timeframe": definitions.PAIRS_STAT_ARB_V1.timeframe,
                    "input_scope": "CALIBRATION_DIAGNOSTIC",
                    "validation_blocking": False,
                    "roles": "PAIR_CALIBRATION",
                    "status": "INSUFFICIENT_CALIBRATION_HISTORY",
                }
                for symbol, day in sorted(insufficient_calibration)
            ],
            sessions,
        )
    )
    coverage.extend(payload["discovery_counts"]["selection_history_diagnostics"])
    observed = [day for (symbol, day), bar in bars.items() if bar and (symbol, day) in required]
    return PreparedPairs(
        start,
        end,
        sessions,
        {str(day): day for day in days},
        bars,
        payload,
        coverage,
        requirements,
        {
            "preparation_sql_queries": database.sql_queries,
            "simulation_sql_queries": 0,
            "candidate_source": "MANIFEST" if candidate_manifest is not None else "DISCOVERED",
            "outcome_data_end": str(max(observed)) if observed else None,
            "blocking_required_symbol_sessions": len(required),
            "insufficient_calibration_symbol_sessions": len(insufficient_calibration),
            "source_fingerprint_inputs": {
                "input_scope": "SOURCE_FINGERPRINT_INPUT",
                "validation_blocking": False,
                "symbols": len(universe),
                "session_start": str(sessions[0]),
                "session_end": str(days[-1]),
                "sessions": len(sessions) - len(tail),
                "fingerprint": digest,
                "note": "All current-universe selection-period inputs, including missing markers; "
                "only separately qualified candidate requirements block validation.",
            },
            "daily_provenance": "CONFIG_BOUND_NO_PER_BAR_PROVENANCE",
            "symbols_without_coverage_provenance": [s for s in symbols if s not in coverage_state],
            "provider_confirmed_absent": None,
            "provider_check_failed": None,
            "provider_session_status_support": "UNAVAILABLE_IN_DAILY_RANGE_METADATA",
        },
    )
