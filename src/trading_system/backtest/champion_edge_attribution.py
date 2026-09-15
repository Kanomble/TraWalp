"""Descriptive post-hoc tables over fixed evidence; never returns strategy settings."""

from collections import defaultdict
from itertools import groupby
from math import sqrt
from statistics import mean, median, stdev

import numpy as np
import pandas as pd

from trading_system.backtest.research_definitions import F_CHAMPION_EDGE_DECOMPOSITION_V1 as EDGE_V1

HORIZONS = EDGE_V1.forward_horizons
MIN_QUINTILE_OBSERVATIONS = 25  # fixed minimum five observations per intended quintile
RANK_GROUPS = ("RANK_1", "RANK_2_3", "RANK_4_5", "RANK_6_10", "RANK_11_PLUS")
TARGET_REASONS = {"profit_target", "take_profit"}
STOP_REASONS = {"stop", "stop_loss"}
CLOSE_REASONS = {"time_exit", "max_hold", "end_of_backtest"}


def average(values):
    return mean(values) if values else None


def distribution(values):
    values = [value for value in values if value is not None]
    return {
        "observations": len(values),
        "mean": average(values),
        "median": median(values) if values else None,
        "positive_return_rate": average([value > 0 for value in values]),
    }


def candidate_metrics(rows):
    return {
        "candidate_count": len(rows),
        **{
            f"{horizon}d_{key}": value
            for horizon in HORIZONS
            for key, value in distribution(
                [row.get(f"forward_return_{horizon}d") for row in rows]
            ).items()
        },
    }


def trade_metrics(rows):
    returns = [row["return"] for row in rows]
    pnl = [row["net_pnl"] for row in rows]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
    holding = [row["holding_sessions"] for row in rows]
    return {
        "executed_trade_count": len(rows),
        "mean_return": average(returns),
        "median_return": median(returns) if returns else None,
        "mean_net_pnl": average(pnl),
        "median_net_pnl": median(pnl) if pnl else None,
        "win_rate": average([p > 0 for p in pnl]),
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "total_net_pnl": sum(pnl),
        "positive_pnl": sum(wins),
        "negative_pnl": sum(losses),
        "mean_holding_sessions": average(holding),
        "median_holding_sessions": median(holding) if holding else None,
        "target_rate": average([row["exit_reason"] in TARGET_REASONS for row in rows]),
        "stop_rate": average([row["exit_reason"] in STOP_REASONS for row in rows]),
    }


def rank_group(rank):
    return next(
        (label for limit, label in zip((1, 3, 5, 10), RANK_GROUPS, strict=False) if rank <= limit),
        RANK_GROUPS[-1],
    )


def quantile_definition(rows, field):
    values = [row[field] for row in rows if row.get(field) is not None]
    if len(values) < MIN_QUINTILE_OBSERVATIONS:
        return (
            None,
            f"INSUFFICIENT_QUINTILE_OBSERVATIONS: {len(values)} < {MIN_QUINTILE_OBSERVATIONS}",
        )
    edges = np.quantile(values, (0.2, 0.4, 0.6, 0.8)).tolist()
    if len(set(edges)) != 4 or len(set(values)) < 5:
        return None, "QUINTILES_UNAVAILABLE_TIED_BOUNDARIES"
    return edges, None


def quintile(value, edges):
    if value is None:
        return "UNAVAILABLE"
    return f"Q{sum(value > edge for edge in edges) + 1}"


def paired_attribution(candidates, trades, dimension, classify, *, edges=None):
    output = []
    for population, rows, metrics in (
        ("F_ELIGIBLE_CANDIDATES", candidates, candidate_metrics),
        ("EXECUTED_F_TRADES", trades, trade_metrics),
    ):
        groups = defaultdict(list)
        for row in rows:
            groups[classify(row)].append(row)
        for group, members in sorted(groups.items()):
            output.append(
                {
                    "population": population,
                    "dimension": dimension,
                    "group": group,
                    "interpretation": "POST_HOC_DIAGNOSTICS",
                    "quintile_boundaries": edges,
                    **metrics(members),
                }
            )
    return output


def quantile_attribution(candidates, trades, field, unavailable):
    edges, reason = quantile_definition(candidates, field)
    if reason:
        unavailable[field] = reason
        return [
            {
                "population": "F_ELIGIBLE_CANDIDATES",
                "dimension": field,
                "status": "UNAVAILABLE",
                "reason": reason,
                "candidate_count": sum(row.get(field) is not None for row in candidates),
                "interpretation": "POST_HOC_DIAGNOSTICS",
            }
        ]
    return paired_attribution(
        candidates, trades, field, lambda row: quintile(row.get(field), edges), edges=edges
    )


def drawdown_bucket(value):
    if value is None:
        return "UNAVAILABLE"
    if value > 0 or value < -0.1:
        return "OUTSIDE_PREDECLARED_RANGE"
    for lower, label in (
        (-0.025, "0_TO_MINUS_2.5_PERCENT"),
        (-0.05, "MINUS_2.5_TO_5_PERCENT"),
        (-0.075, "MINUS_5_TO_7.5_PERCENT"),
        (-0.1, "MINUS_7.5_TO_10_PERCENT"),
    ):
        if value >= lower:
            return label


def momentum_bucket(value):
    if value is None:
        return "UNAVAILABLE"
    if value <= 0:
        return "NON_POSITIVE"
    for upper, label in (
        (0.1, "0_TO_10_PERCENT"),
        (0.2, "10_TO_20_PERCENT"),
        (0.4, "20_TO_40_PERCENT"),
    ):
        if value <= upper:
            return label
    return "OVER_40_PERCENT"


def cap_bucket(value):
    if value is None:
        return "UNAVAILABLE_PIT"
    if value < 1e9:
        return "BELOW_1B"
    for upper, label in ((5e9, "1B_TO_5B"), (20e9, "5B_TO_20B"), (100e9, "20B_TO_100B")):
        if value <= upper:
            return label
    return "OVER_100B"


def exit_attribution(trades):
    groups = defaultdict(list)
    for row in trades:
        groups[row["exit_reason"]].append(row)
    positive = sum(max(0, row["net_pnl"]) for row in trades)
    negative = sum(min(0, row["net_pnl"]) for row in trades)
    output = []
    for reason, members in sorted(groups.items()):
        metrics = trade_metrics(members)
        output.append(
            {
                "population": "EXECUTED_F_TRADES",
                "exit_reason": reason,
                **metrics,
                "share_total_positive_pnl": metrics["positive_pnl"] / positive
                if positive
                else None,
                "share_total_negative_pnl": metrics["negative_pnl"] / negative
                if negative
                else None,
            }
        )
    return output


def holding_bucket(day):
    for last, label in (
        (1, "DAY_1"),
        (2, "DAY_2"),
        (3, "DAY_3"),
        (5, "DAY_4_5"),
        (7, "DAY_6_7"),
        (10, "DAY_8_10"),
    ):
        if day <= last:
            return label
    return "AFTER_DAY_10"  # missing-bar legacy behavior can extend elapsed calendar sessions


def holding_attribution(path):
    groups = defaultdict(list)
    for row in path:
        if row["unrealized_return"] is not None:
            groups[holding_bucket(row["holding_session"])].append(row)
    output = []
    for label, rows in groups.items():
        returns = [row["unrealized_return"] for row in rows]
        output.append(
            {
                "population": "ACTUAL_OPEN_POSITION_SESSION_OBSERVATIONS",
                "holding_day_group": label,
                "number_still_open": len({row["position_id"] for row in rows}),
                "open_position_session_observations": len(rows),
                "mean_unrealized_return": mean(returns),
                "median_unrealized_return": median(returns),
                "mean_mfe": average([row["MFE"] for row in rows if row["MFE"] is not None]),
                "mean_mae": average([row["MAE"] for row in rows if row["MAE"] is not None]),
                "fraction_eventually_profitable": average(
                    [row["eventual_net_pnl"] > 0 for row in rows]
                ),
                "fraction_eventually_stop": average(
                    [row["eventual_exit_reason"] in STOP_REASONS for row in rows]
                ),
                "fraction_eventually_target": average(
                    [row["eventual_exit_reason"] in TARGET_REASONS for row in rows]
                ),
                "interpretation": "POST_HOC_DIAGNOSTICS; session-weighted within multi-day buckets",
            }
        )
    return output


def spy_regimes(panel, official_sessions):
    """Strict completed-session inputs, no stale-close substitution across missing bars."""
    output = {}
    spy = panel.by_day.get("SPY", {})
    for index, day in enumerate(official_sessions):
        row = {"session": str(day)}

        def window(count, index=index):
            if index + 1 < count:
                return None
            bars = [spy.get(d) for d in official_sessions[index + 1 - count : index + 1]]
            return bars if all(bar is not None for bar in bars) else None

        sma, momentum, volatility, highs = window(200), window(127), window(21), window(252)
        row["spy_above_sma200"] = (
            float(sma[-1].close) > mean(float(b.close) for b in sma) if sma else None
        )
        row["spy_momentum126"] = (
            float(momentum[-1].close / momentum[0].close - 1) if momentum else None
        )
        row["spy_volatility20"] = (
            stdev(
                [
                    float(b.close / a.close - 1)
                    for a, b in zip(volatility, volatility[1:], strict=False)
                ]
            )
            * sqrt(252)
            if volatility
            else None
        )
        row["spy_drawdown_52w"] = (
            float(highs[-1].close / max(b.high for b in highs) - 1) if highs else None
        )
        output[day] = row
    return output


def regime_group(row, dimension, volatility_edges):
    value = row.get(dimension)
    if value is None:
        return "UNAVAILABLE"
    if dimension == "spy_above_sma200":
        return "ABOVE_SMA200" if value else "AT_OR_BELOW_SMA200"
    if dimension == "spy_momentum126":
        return "POSITIVE" if value > 0 else "NON_POSITIVE"
    if dimension == "spy_volatility20":
        return quintile(value, volatility_edges) if volatility_edges else "QUINTILES_UNAVAILABLE"
    for bound, label in (
        (-0.05, "DRAWDOWN_0_5_PERCENT"),
        (-0.1, "DRAWDOWN_5_10_PERCENT"),
        (-0.2, "DRAWDOWN_10_20_PERCENT"),
    ):
        if value >= bound:
            return label
    return "DRAWDOWN_OVER_20_PERCENT"


def market_regime_attribution(trades, idle, candidate_regimes, unavailable):
    edges, reason = quantile_definition(candidate_regimes, "spy_volatility20")
    if reason:
        unavailable["spy_volatility20"] = reason
    output = []
    for field in ("spy_above_sma200", "spy_momentum126", "spy_volatility20", "spy_drawdown_52w"):
        trade_groups, session_groups = defaultdict(list), defaultdict(list)
        for row in trades:
            trade_groups[regime_group(row, field, edges)].append(row)
        for row in idle:
            session_groups[regime_group(row, field, edges)].append(row)
        for group in sorted(set(trade_groups) | set(session_groups)):
            sessions = session_groups[group]
            output.append(
                {
                    "population": "EXECUTED_TRADES_AND_SEPARATE_EXECUTABLE_SESSIONS",
                    "dimension": field,
                    "group": group,
                    "quintile_boundaries": edges if field == "spy_volatility20" else None,
                    **trade_metrics(trade_groups[group]),
                    "sessions_invested": sum(row["invested"] for row in sessions),
                    "sessions_cash": sum(not row["invested"] for row in sessions),
                    "interpretation": (
                        "POST_HOC_DIAGNOSTICS; trade regime at signal close; "
                        "session exposure regime at prior official close"
                    ),
                }
            )
    return output


def linear_relationship(pairs):
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    result = dict.fromkeys(("pearson", "spearman", "ols_beta", "ols_intercept", "r_squared"))
    result["paired_observations"] = len(pairs)
    if len(pairs) < 3:
        return result
    x, y = np.asarray(pairs, dtype=float).T
    dx, dy = x - x.mean(), y - y.mean()
    xx, yy, xy = float(dx @ dx), float(dy @ dy), float(dx @ dy)
    if xx > 0:
        result.update(ols_beta=xy / xx, ols_intercept=float(y.mean() - xy / xx * x.mean()))
    if xx > 0 and yy > 0:
        pearson = max(-1.0, min(1.0, xy / sqrt(xx * yy)))
        result.update(
            pearson=pearson,
            spearman=float(pd.Series(x).rank().corr(pd.Series(y).rank())),
            r_squared=pearson**2,
        )
    return result


def concentration(trades, field):
    groups = defaultdict(list)
    for row in trades:
        groups[row.get(field) or "UNKNOWN"].append(row)
    total = sum(row["net_pnl"] for row in trades)
    absolute = sum(abs(row["net_pnl"]) for row in trades)
    output = []
    for label, members in sorted(groups.items()):
        metrics = trade_metrics(members)
        output.append(
            {
                "population": "EXECUTED_F_TRADES",
                field: label,
                **metrics,
                "trade_count_share": len(members) / len(trades),
                "net_pnl_share": metrics["total_net_pnl"] / total if total else None,
                "absolute_pnl_share": sum(abs(row["net_pnl"]) for row in members) / absolute
                if absolute
                else None,
                "average_f_rank": average([row["f_rank"] for row in members]),
                "classification_basis": "CURRENT_LOCAL_SIC_2_DIGIT_NOT_PIT"
                if field == "sector"
                else "SYMBOL",
            }
        )
    return output


def concentration_summary(symbols):
    output = {"unique_traded_symbols": len(symbols)}
    pnl_rank = sorted(symbols, key=lambda row: (-row["total_net_pnl"], row["symbol"]))
    count_rank = sorted(symbols, key=lambda row: (-row["executed_trade_count"], row["symbol"]))
    total = sum(row["total_net_pnl"] for row in symbols)
    for n in (1, 5, 10):
        pnl = sum(row["total_net_pnl"] for row in pnl_rank[:n])
        output[f"top_{n}_symbol_net_pnl"] = pnl
        output[f"top_{n}_symbol_pnl_share"] = pnl / total if total else None
        output[f"top_{n}_symbol_trade_count_share"] = sum(
            row["trade_count_share"] for row in count_rank[:n]
        )
    return output


def idle_summary(rows):
    streaks = [
        len(list(group))
        for invested, group in groupby(rows, key=lambda row: row["invested"])
        if not invested
    ]
    invested = sum(row["invested"] for row in rows)
    return {
        "executable_sessions": len(rows),
        "sessions_invested": invested,
        "sessions_cash": len(rows) - invested,
        "invested_session_share": invested / len(rows) if rows else None,
        "cash_session_share": 1 - invested / len(rows) if rows else None,
        "longest_cash_streak": max(streaks, default=0),
        "median_cash_streak": median(streaks) if streaks else 0,
    }
