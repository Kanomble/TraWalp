"""Outcome-clean selection inputs and PIT-safe missing-history qualification."""

from collections import defaultdict
from decimal import Decimal

from trading_system.backtest import research_definitions as definitions
from trading_system.backtest.pairs_stat_arb_v1_identity import CURRENT, QUARANTINED, USABLE
from trading_system.data.security_identity import ACTIVE, OUTSIDE, UNKNOWN, identity_epoch

SELECTION_GAP_SEMANTICS = (
    "RUN_IDENTITY_QUALIFICATION_FIRST;QUARANTINE_NONMEMBER;VERIFIED_EPOCH_PRECEDENCE;"
    "OUTSIDE_EPOCH_NONMEMBER;ORDINARY_CURRENT_REFERENCE_ALLOWED;"
    "PREFIX_WARMUP_NONBLOCKING;INTERNAL_MISSING_AND_SYSTEM_UNKNOWN_BLOCK_SIC2_SESSION_V2"
)


def selection_inputs(
    universe, bars, sessions, days, config, history_anchors=None, *, consume_session=None
):
    """Qualify inputs only, without ranking or forming replacement membership.

    Unknown identity blocks before price/ADV gates. Verified outside-epoch
    sessions are nonmembers. Missing inputs inside a verified interval block;
    observations before the interval are never consumed, even during warmup.
    A consumer processes each resolved session immediately without retaining all
    eligible current-universe symbol-sessions in memory.
    """
    definition = definitions.PAIRS_STAT_ARB_V1
    lookback = definition.adv_lookback_sessions
    epochs = {row["symbol"]: identity_epoch(row) for row in universe}
    identities = {row["symbol"]: row for row in universe}

    def status(symbol, day):
        row, epoch = identities.get(symbol, {}), epochs.get(symbol)
        if row.get("identity_status") == QUARANTINED:
            return QUARANTINED
        if epoch is not None:
            return ACTIVE if epoch.contains(day) else OUTSIDE
        return CURRENT if row.get("identity_status") == CURRENT else UNKNOWN

    first_observed = {
        symbol: day
        for symbol, day in (history_anchors or {}).items()
        if symbol in identities and status(symbol, day) in USABLE
    }
    for (symbol, day), bar in bars.items():
        if bar is not None and status(symbol, day) in USABLE:
            first_observed[symbol] = min(first_observed.get(symbol, day), day)
    indexes = {day: i for i, day in enumerate(sessions)}
    inputs, prefix, gaps, unresolved, epoch_diagnostics = {}, {}, {}, [], {}
    counts = {"eligible_symbol_sessions": 0, "unavailable_liquidity_symbol_sessions": 0}
    for day in days:
        i = indexes[day]
        groups, blocked = defaultdict(list), defaultdict(list)
        for identity in universe:
            symbol, sic2 = identity["symbol"], identity["sic2"]
            epoch = epochs[symbol]
            state = status(symbol, day)
            if state not in USABLE:
                key = symbol, state
                row = epoch_diagnostics.setdefault(
                    key,
                    {
                        "symbol": symbol,
                        "sic2": sic2,
                        "session": str(day),
                        "session_end": str(day),
                        "sessions": 0,
                        "record_kind": "SYMBOL_SUMMARY",
                        "timeframe": definition.timeframe,
                        "roles": "HISTORICAL_SECURITY_IDENTITY",
                        "input_scope": "IDENTITY_EPOCH_REQUIREMENT"
                        if state == UNKNOWN
                        else "SELECTION_DIAGNOSTIC",
                        "status": state,
                        "validation_blocking": state == UNKNOWN,
                        "resolved_epoch": epoch.record() if epoch else None,
                        "epoch_source": epoch.source if epoch else None,
                        "epoch_verified": epoch is not None,
                    },
                )
                row["session_end"] = str(day)
                row["sessions"] += 1
                if state == UNKNOWN:
                    blocked[sic2].append(symbol)
                continue
            previous = (
                bars.get((symbol, sessions[i - 1]))
                if i and status(symbol, sessions[i - 1]) in USABLE
                else None
            )
            # This earlier frozen gate determines exclusion without requiring ADV.
            if previous is not None and previous.close < config.universe.min_price:
                continue
            window_days = sessions[max(0, i - lookback + 1) : i + 1]
            needed = set(window_days)
            if i:
                needed.add(sessions[i - 1])
            missing = sorted(
                s for s in needed if status(symbol, s) in USABLE and bars.get((symbol, s)) is None
            )
            outside_window = any(status(symbol, s) not in USABLE for s in needed)
            if previous is None or len(window_days) != lookback or missing or outside_window:
                counts["unavailable_liquidity_symbol_sessions"] += 1
                first = first_observed.get(symbol)
                first = first if first is not None and first <= day else None
                # Verified existence makes all in-epoch missing inputs blocking.
                # Ordinary current-reference rows retain prefix diagnostics; a
                # first bar is an observation anchor, never an identity boundary.
                internal = missing if epoch else [s for s in missing if first and s >= first]
                if internal:
                    blocked[sic2].append(symbol)
                    for missing_day in internal:
                        gap = gaps.setdefault(
                            (symbol, missing_day),
                            {
                                "symbol": symbol,
                                "session": str(missing_day),
                                "sic2": sic2,
                                "first_observed_session_on_or_before_T": (
                                    str(first) if first else None
                                ),
                                "affected_signal_sessions": [],
                                "roles": "LIQUIDITY_SELECTION_INTERNAL_GAP",
                                "status": "LOCAL_MISSING",
                                "identity_epoch_status": state,
                                "epoch_source": epoch.source if epoch else None,
                                "epoch_verified": epoch is not None,
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
        identity_epoch_diagnostics=[epoch_diagnostics[k] for k in sorted(epoch_diagnostics)],
        identity_epochs_complete=not any(k[1] == UNKNOWN for k in epoch_diagnostics),
        unknown_identity_symbol_sessions=sum(
            r["sessions"] for r in epoch_diagnostics.values() if r["status"] == UNKNOWN
        ),
        outside_identity_epoch_symbol_sessions=sum(
            r["sessions"] for r in epoch_diagnostics.values() if r["status"] == OUTSIDE
        ),
    )
    return inputs, counts
