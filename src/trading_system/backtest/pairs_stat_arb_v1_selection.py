"""Outcome-clean selection inputs and PIT-safe missing-history qualification."""

from collections import defaultdict
from decimal import Decimal

from trading_system.backtest import research_definitions as definitions

SELECTION_GAP_SEMANTICS = (
    "PRICE_GATE_FIRST;FIRST_LOCAL_OBSERVATION_ON_OR_BEFORE_T;"
    "PREFIX_NONBLOCKING;INTERNAL_PREVIOUS_CLOSE_OR_ADV_GAP_BLOCKS_SIC2_SESSION"
)


def selection_inputs(
    universe, bars, sessions, days, config, history_anchors=None, *, consume_session=None
):
    """Qualify inputs only, without ranking or forming replacement membership.

    An anchor after T is treated exactly like no observation. Prefix absence
    never requests a fetch. Once a bar has been observed, later missing/invalid
    inputs are internal gaps, even if that symbol is still in its initial warmup.
    A consumer processes each resolved session immediately without retaining all
    eligible current-universe symbol-sessions in memory.
    """
    definition = definitions.PAIRS_STAT_ARB_V1
    lookback = definition.adv_lookback_sessions
    first_observed = dict(history_anchors or {})
    for symbol, day in bars:
        first_observed[symbol] = min(first_observed.get(symbol, day), day)
    indexes = {day: i for i, day in enumerate(sessions)}
    inputs, prefix, gaps, unresolved = {}, {}, {}, []
    counts = {"eligible_symbol_sessions": 0, "unavailable_liquidity_symbol_sessions": 0}
    for day in days:
        i = indexes[day]
        groups, blocked = defaultdict(list), defaultdict(list)
        for identity in universe:
            symbol, sic2 = identity["symbol"], identity["sic2"]
            previous = bars.get((symbol, sessions[i - 1])) if i else None
            # This earlier frozen gate determines exclusion without requiring ADV.
            if previous is not None and previous.close < config.universe.min_price:
                continue
            window_days = sessions[max(0, i - lookback + 1) : i + 1]
            needed = set(window_days)
            if i:
                needed.add(sessions[i - 1])
            missing = sorted(s for s in needed if bars.get((symbol, s)) is None)
            if previous is None or len(window_days) != lookback or missing:
                counts["unavailable_liquidity_symbol_sessions"] += 1
                first = first_observed.get(symbol)
                first = first if first is not None and first <= day else None
                internal = [s for s in missing if first is not None and s >= first]
                if internal:
                    blocked[sic2].append(symbol)
                    for missing_day in internal:
                        gap = gaps.setdefault(
                            (symbol, missing_day),
                            {
                                "symbol": symbol,
                                "session": str(missing_day),
                                "sic2": sic2,
                                "first_observed_session_on_or_before_T": str(first),
                                "affected_signal_sessions": [],
                                "roles": "LIQUIDITY_SELECTION_INTERNAL_GAP",
                                "status": "LOCAL_MISSING_FETCHABLE",
                                "input_scope": "VALIDATION_BLOCKING_REQUIREMENT",
                                "validation_blocking": True,
                            },
                        )
                        gap["affected_signal_sessions"].append(str(day))
                else:
                    row = prefix.setdefault(
                        symbol,
                        {
                            "symbol": symbol,
                            "sic2": sic2,
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
                    row["session_end"] = str(day)
                    row["sessions"] += 1
                continue
            adv = (
                sum(
                    Decimal(str(bars[symbol, s].close)) * bars[symbol, s].volume
                    for s in window_days
                )
                / lookback
            )
            if adv >= Decimal(str(config.universe.min_avg_dollar_volume_20d)):
                counts["eligible_symbol_sessions"] += 1
                groups[sic2].append(
                    {"symbol": symbol, "adv20": float(adv), "previous_close": previous.close}
                )
        for sic2, symbols in sorted(blocked.items()):
            # Do not rank around an unresolved member, even if five others qualify.
            groups.pop(sic2, None)
            unresolved.append(
                {
                    "signal_session": str(day),
                    "sic2": sic2,
                    "symbols": sorted(symbols),
                    "status": "SELECTION_MEMBERSHIP_UNRESOLVED",
                    "validation_blocking": True,
                }
            )
        if consume_session is None:
            inputs[str(day)] = dict(groups)
        else:
            consume_session(day, groups)
    counts.update(
        selection_history_diagnostics=[prefix[s] for s in sorted(prefix)],
        internal_selection_gaps=[gaps[key] for key in sorted(gaps)],
        unresolved_selection_membership=unresolved,
        unresolved_sic2_sessions=len(unresolved),
        selection_membership_resolved=not unresolved,
    )
    return inputs, counts
