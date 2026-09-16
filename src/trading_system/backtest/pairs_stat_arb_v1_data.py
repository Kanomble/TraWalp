"""Read-only local identity, Daily batches and outcome-clean pair discovery."""

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

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
            or company.symbol == "SPY"
            or not asset.tradable
            or asset.asset_class != "US_EQUITY"
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
                "asset_class": "US_EQUITY",
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
                f"WHERE symbol IN ({placeholders}) AND timeframe='1d' "
                "AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
                [*batch, str(start), str(end + timedelta(days=1))],
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
    selected, candidates = {}, []
    indexes = {day: index for index, day in enumerate(sessions)}
    counts = {"eligible_symbol_sessions": 0, "unavailable_liquidity_symbol_sessions": 0}
    for day in days:
        index = indexes[day]
        groups = defaultdict(list)
        for identity in universe:
            symbol = identity["symbol"]
            previous = bars.get((symbol, sessions[index - 1]))
            window = [bars.get((symbol, s)) for s in sessions[index - 19 : index + 1]]
            if previous is None or len(window) != 20 or any(bar is None for bar in window):
                counts["unavailable_liquidity_symbol_sessions"] += 1
                continue
            adv = sum(Decimal(str(bar.close)) * bar.volume for bar in window) / 20
            if previous.close >= config.universe.min_price and adv >= Decimal(
                str(config.universe.min_avg_dollar_volume_20d)
            ):
                counts["eligible_symbol_sessions"] += 1
                groups[identity["sic2"]].append(
                    {"symbol": symbol, "adv20": float(adv), "previous_close": previous.close}
                )
        retained = {
            sic2: sorted(names, key=lambda row: (-row["adv20"], row["symbol"]))[:5]
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
    return selected, candidates, counts


def daily_requirements(universe, sessions, days, candidates):
    """Selection/calibration through signal end, plus candidate-specific execution windows."""
    required = defaultdict(set)
    end = days[-1]
    for company in universe:
        for day in sessions:
            if day <= end:
                required[company["symbol"], day].add("SELECTION_CALIBRATION")
    indexes = {str(day): i for i, day in enumerate(sessions)}
    for candidate in candidates:
        if candidate["calibration_status"] != "ELIGIBLE":
            continue
        i = indexes[candidate["signal_session"]]
        for symbol in (candidate["symbol_a"], candidate["symbol_b"]):
            for day in sessions[i + 1 : i + 6]:
                required[symbol, day].add("POSSIBLE_OUTCOME_ONLY")
    return required


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
    required = daily_requirements(universe, sessions, days, payload["candidates"])
    tail_symbols = {symbol for symbol, day in required if day > end}
    # Discovery and source hashing above never receive post-T outcome data.
    bars.update(load_daily(database, tail_symbols, tail[0], tail[-1]))
    if not preflight:
        bars.update(load_daily(database, ["SPY"], days[0], tail[-1]))
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
    coverage, requirements = [], []
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
                "timeframe": "1d",
                "roles": ";".join(sorted(roles)),
                "status": "REQUIRED_PRESENT" if present else "LOCAL_MISSING_FETCHABLE",
                "provider_range_verified": verified,
                "provider_session_absence": "NOT_ESTABLISHED",
            }
        )
        requirements.append({"symbol": symbol, "session": str(day), "roles": sorted(roles)})
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
            "daily_provenance": "CONFIG_BOUND_NO_PER_BAR_PROVENANCE",
            "symbols_without_coverage_provenance": [s for s in symbols if s not in coverage_state],
            "provider_confirmed_absent": None,
            "provider_check_failed": None,
            "provider_session_status_support": "UNAVAILABLE_IN_DAILY_RANGE_METADATA",
        },
    )
