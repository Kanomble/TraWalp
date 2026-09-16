"""Frozen Daily pair mathematics and simulation; no database, provider or F inputs."""

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import correlation, mean, stdev

from trading_system.backtest.research_definitions import PAIRS_STAT_ARB_V1

PAIRS_V1 = PAIRS_STAT_ARB_V1


@dataclass(frozen=True, slots=True)
class PairBar:
    open: float
    high: float
    low: float
    close: float
    volume: int


def calibration(bars, sessions, index, symbol_a, symbol_b):
    """Only the preceding 60 closes; the observation at index is never consumed."""
    if index < 60:
        return {"calibration_status": "INCOMPLETE_CALIBRATION"}
    a, b = [], []
    for session in sessions[index - 60 : index]:
        ba, bb = bars.get((symbol_a, session)), bars.get((symbol_b, session))
        if ba is None or bb is None:
            return {"calibration_status": "INCOMPLETE_CALIBRATION"}
        a.append(ba.close)
        b.append(bb.close)
    spreads = [math.log(x / y) for x, y in zip(a, b, strict=True)]
    deviation = stdev(spreads)
    result = {"calibration_mean": mean(spreads), "calibration_std": deviation}
    if deviation == 0:
        return {**result, "calibration_status": "ZERO_SPREAD_VARIANCE"}
    ra = [math.log(y / x) for x, y in zip(a, a[1:], strict=False)]
    rb = [math.log(y / x) for x, y in zip(b, b[1:], strict=False)]
    corr = correlation(ra, rb) if stdev(ra) > 0 and stdev(rb) > 0 else None
    return {
        **result,
        "correlation": corr,
        "calibration_status": (
            "ELIGIBLE" if corr is not None and corr >= 0.70 else "LOW_CORRELATION"
        ),
    }


def observe(bars, sessions, index, a, b):
    stats = calibration(bars, sessions, index, a, b)
    ba, bb = bars.get((a, sessions[index])), bars.get((b, sessions[index]))
    if ba is None or bb is None or not stats.get("calibration_std"):
        return None
    spread = math.log(ba.close / bb.close)
    return spread, (spread - stats["calibration_mean"]) / stats["calibration_std"]


def signal_direction(z):
    if z >= 2.0:
        return "SIGNAL_POSITIVE_Z", "SHORT", "LONG"
    if z <= -2.0:
        return "SIGNAL_NEGATIVE_Z", "LONG", "SHORT"
    return "NO_SIGNAL", None, None


def crossing(entry_z, current_z):
    return current_z <= 0 if entry_z >= 2 else current_z >= 0


def fill(reference, direction, *, entry):
    buy = (direction == "LONG") == entry
    return reference * (1.0005 if buy else 0.9995)


def trade_returns(row):
    long = "a" if row["direction_a"] == "LONG" else "b"
    short = "b" if long == "a" else "a"
    for kind, entry_key, exit_key in (
        ("gross", "entry_open", "exit_reference"),
        ("net", "entry_fill", "exit_fill"),
    ):
        lr = row[f"{exit_key}_{long}"] / row[f"{entry_key}_{long}"] - 1
        sr = 1 - row[f"{exit_key}_{short}"] / row[f"{entry_key}_{short}"]
        row.update({f"{kind}_long_return": lr, f"{kind}_short_return": sr})
        row[f"{kind}_pair_return"] = 0.5 * lr + 0.5 * sr
    row["modeled_cost_drag"] = row["gross_pair_return"] - row["net_pair_return"]


TRADE_FIELDS = [
    "entry_session",
    "entry_open_a",
    "entry_open_b",
    "entry_fill_a",
    "entry_fill_b",
    "exit_trigger_session",
    "exit_execution_session",
    "exit_reference_a",
    "exit_reference_b",
    "exit_fill_a",
    "exit_fill_b",
    "exit_reason",
    "holding_sessions",
    "gross_long_return",
    "gross_short_return",
    "gross_pair_return",
    "net_long_return",
    "net_short_return",
    "net_pair_return",
    "modeled_cost_drag",
    "pair_mfe",
    "pair_mae",
    "spy_return",
]


def simulate_trade(candidate, bars, sessions, index, signal_end):
    """Resolve one fixed pair, including data terminals, within its five-session horizon."""
    a, b = candidate["symbol_a"], candidate["symbol_b"]
    row = {**candidate, **dict.fromkeys(TRADE_FIELDS)}
    row.update(entry_status="ENTRY_UNOBSERVABLE", mean_crossing_observed=False)
    row.update(long_weight=0.5, short_weight=0.5, gross_notional=1.0, net_notional=0.0)
    horizon = min(index + 5, len(sessions) - 1)
    blocked_until = sessions[horizon]

    def unavailable(session, status):
        row["status"] = "OUTCOME_CENSORED" if session > signal_end else status
        row["exit_reason"] = row["status"]
        return row, blocked_until

    if index + 1 >= len(sessions):
        row.update(status="OUTCOME_CENSORED", exit_reason="OUTCOME_CENSORED")
        return row, blocked_until
    entry = sessions[index + 1]
    row["entry_session"] = entry.isoformat()
    ea, eb = bars.get((a, entry)), bars.get((b, entry))
    if ea is None or eb is None:
        result, _ = unavailable(entry, "ENTRY_UNOBSERVABLE")
        return result, sessions[index]  # No leg entered; a later signal is independent.
    row["entry_status"] = "EXECUTED"
    for leg, bar in (("a", ea), ("b", eb)):
        row[f"entry_open_{leg}"] = bar.open
        row[f"entry_fill_{leg}"] = fill(bar.open, row[f"direction_{leg}"], entry=True)
    scheduled = None
    path = []
    for holding, session in enumerate(sessions[index + 1 : index + 6], 1):
        ba, bb = bars.get((a, session)), bars.get((b, session))
        row["holding_sessions"] = holding
        if ba is None or bb is None:
            return unavailable(session, "EXIT_UNOBSERVABLE")
        if scheduled is not None:
            reason, reference = "MEAN_REVERSION", "open"
        else:
            pnl = sum(
                0.5
                * (bar.close / row[f"entry_fill_{leg}"] - 1)
                * (1 if row[f"direction_{leg}"] == "LONG" else -1)
                for leg, bar in (("a", ba), ("b", bb))
            )
            path.append(pnl)
            row.update(pair_mfe=max(path), pair_mae=min(path))
            observation = observe(bars, sessions, index + holding, a, b)
            if holding == 5:
                reason, reference = "MAX_HOLD_5", "close"
                row["exit_trigger_session"] = session.isoformat()
            elif observation is None:
                return unavailable(session, "EXIT_UNOBSERVABLE")
            else:
                if crossing(row["entry_z"], observation[1]):
                    scheduled = session
                    row["mean_crossing_observed"] = True
                    row["exit_trigger_session"] = session.isoformat()
                continue
        for leg, bar in (("a", ba), ("b", bb)):
            price = getattr(bar, reference)
            row[f"exit_reference_{leg}"] = price
            row[f"exit_fill_{leg}"] = fill(price, row[f"direction_{leg}"], entry=False)
        row.update(
            status="MEAN_REVERSION_EXIT" if reason == "MEAN_REVERSION" else reason,
            exit_reason=reason,
            exit_execution_session=session.isoformat(),
        )
        trade_returns(row)
        spy_entry, spy_exit = bars.get(("SPY", entry)), bars.get(("SPY", session))
        if spy_entry is not None and spy_exit is not None:
            row["spy_return"] = getattr(spy_exit, reference) / spy_entry.open - 1
        return row, session
    row.update(status="OUTCOME_CENSORED", exit_reason="OUTCOME_CENSORED")
    return row, blocked_until


def simulate_prepared_pairs(prepared):
    """No persistence handles: zero SQLite queries, all prices already in memory."""
    indexes = {day: index for index, day in enumerate(prepared.sessions)}
    evaluations, signals = [], []
    grouped = defaultdict(list)
    for candidate in prepared.manifest["candidates"]:
        grouped[candidate["symbol_a"], candidate["symbol_b"]].append(candidate)
    for (a, b), candidates in sorted(grouped.items()):
        blocked_until = None
        for candidate in candidates:
            day = candidate["signal_session"]
            session = prepared.signal_dates[day]
            row = {**candidate, "spread_t": None, "entry_z": None}
            if blocked_until is not None and session < blocked_until:
                row["status"] = "PAIR_ACTIVE_IGNORED"
            elif candidate["calibration_status"] != "ELIGIBLE":
                row["status"] = candidate["calibration_status"]
            else:
                observation = observe(prepared.bars, prepared.sessions, indexes[session], a, b)
                if observation is None:
                    row["status"] = "SIGNAL_UNOBSERVABLE"
                else:
                    row["spread_t"], row["entry_z"] = observation
                    status, da, db = signal_direction(row["entry_z"])
                    row.update(status=status, direction_a=da, direction_b=db)
                    if status != "NO_SIGNAL":
                        trade, blocked_until = simulate_trade(
                            row, prepared.bars, prepared.sessions, indexes[session], prepared.end
                        )
                        signals.append(trade)
            evaluations.append(row)

    def key(row):
        return row["signal_session"], row["symbol_a"], row["symbol_b"]

    return sorted(evaluations, key=key), sorted(signals, key=key)
