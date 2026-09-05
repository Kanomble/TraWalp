"""Non-trading lifecycle observations and explicitly separated forward outcome labels."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import mean

from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.lifecycle import previous_session
from trading_system.backtest.peer_context import TechnicalPeerContextProvider
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import BacktestResult, StrategyVariant


def gap_observation(signal_close, next_open, atr) -> dict:
    return {
        "signal_close": signal_close,
        "next_open": next_open,
        "ATR": atr,
        "gap_return": next_open / signal_close - 1 if signal_close and next_open else None,
        "gap_in_ATR": (next_open - signal_close) / atr
        if signal_close and next_open and atr and atr > 0
        else None,
    }


class LifecycleDiagnostics:
    """Observer has no return channel to entry filtering or lifecycle decisions."""

    def __init__(self, context: TechnicalPeerContextProvider, config):
        self.context = context
        self.config = config
        self.candidates: dict[tuple[date, str], dict] = {}

    def observe_screen(self, report, variant, config):
        pass  # Complete candidate context is supplied by the entry-context hook below.

    def observe_entry_context(self, report, positions, execution_session):
        eligible = [
            (evaluation.score, record)
            for record in report.records
            if (
                evaluation := evaluate_variant_entry(
                    record, StrategyVariant.QUALITY_VALUE_MOMENTUM, self.config
                )
            ).eligible
        ]
        eligible.sort(key=lambda item: (-item[0], item[1].symbol))
        for rank, (_, record) in enumerate(eligible, 1):
            self.candidates[report.as_of, record.symbol] = {
                "symbol": record.symbol,
                "signal_date": report.as_of.isoformat(),
                "entry_session": execution_session.isoformat() if execution_session else None,
                "candidate_rank": rank,
                "signal_session_ATR": record.technical.atr14,
                "selection_outcome": "potential",
                "executed": False,
                "correlation_observation_basis": "signal_close_potential_entry",
                **self.context.correlations(record.symbol, positions, report.as_of),
            }

    def observe_portfolio_decision(self, signal_date, symbol, outcome, reason=None):
        row = self.candidates.get((signal_date, symbol))
        if row is not None:
            row.update(selection_outcome=outcome, selection_reason=reason)

    def observe_execution_context(self, signal_date, symbol, positions):
        row = self.candidates.get((signal_date, symbol))
        if row is not None:
            row.update(self.context.correlations(symbol, positions, signal_date))
            row["correlation_observation_basis"] = "execution_after_overnight_exits"

    def observe_execution(self, signal_date, execution_date, symbol, executed, reason=None):
        row = self.candidates.get((signal_date, symbol))
        if row is not None:
            row.update(executed=executed, execution_failure_reason=reason)

    def tables(self, result: BacktestResult, manager=None) -> dict[str, list[dict]]:
        context = self.context
        tables: dict[str, list[dict]] = {
            name: []
            for name in (
                "entry_gap_analysis",
                "peer_context",
                "peer_spillover",
                "correlation",
                "holding_duration_analysis",
                "trend_health_events",
                "dynamic_profit_events",
            )
        }
        positions = {(p.signal_date, p.symbol): p for p in result.positions}
        for (signal, symbol), candidate in self.candidates.items():
            position = positions.get((signal, symbol))
            signal_bar = context.complete_history(symbol, signal, 1)
            next_session = date.fromisoformat(candidate["entry_session"])
            entry_bar = context.complete_history(symbol, next_session, 1)
            gap = {
                **candidate,
                **gap_observation(
                    float(signal_bar[-1].close) if signal_bar else None,
                    float(entry_bar[-1].open) if entry_bar else None,
                    candidate["signal_session_ATR"],
                ),
                "position_id": position.position_id if position else None,
                "position_result": position.position_return if position else None,
                "MFE": position.maximum_favorable_excursion if position else None,
                "MAE": position.maximum_adverse_excursion if position else None,
                "holding_period": position.holding_days if position else None,
                "exit_reason": position.exit_reason if position else None,
            }
            tables["entry_gap_analysis"].append(gap)
            tables["correlation"].append({**candidate, "position_result": gap["position_result"]})
            peer = context.context(symbol, signal)
            prior_peer = context.context(symbol, previous_session(signal))
            future1 = context.forward_bars(symbol, signal, 1)
            future5 = context.forward_bars(symbol, signal, 5)
            close = float(signal_bar[-1].close) if signal_bar else None
            tables["peer_spillover"].append(
                {
                    **candidate,
                    **peer.row(),
                    "largest_peer_move_previous_session": prior_peer.peer_best_1d_return,
                    "candidate_return_next_session": float(future1[-1].close) / close - 1
                    if future1 and close
                    else None,
                    "candidate_return_next_5_sessions": float(future5[-1].close) / close - 1
                    if future5 and close
                    else None,
                    "forward_labels_only": True,
                    "forward_1d_complete": bool(future1),
                    "forward_5d_complete": bool(future5),
                }
            )
            # Entry context is known at entry (previous completed Daily close). Exit context
            # includes an explicitly labelled descriptive exit-close observation, not a signal.
            observations = [("signal", signal)]
            if position:
                observations.extend(
                    [
                        ("entry", previous_session(position.entry_date)),
                        ("exit", position.exit_date),
                        ("exit_pre_session", previous_session(position.exit_date)),
                    ]
                )
            for phase, observed in observations:
                tables["peer_context"].append(
                    {
                        **candidate,
                        **context.context(symbol, observed).row(),
                        "position_id": position.position_id if position else None,
                        "observation_phase": phase,
                        "position_result": gap["position_result"],
                        "signal_peer_state": peer.state.value,
                        "entry_peer_state": context.peer_state(
                            symbol, previous_session(position.entry_date)
                        ).value
                        if position
                        else None,
                        "exit_peer_state": context.peer_state(symbol, position.exit_date).value
                        if position
                        else None,
                    }
                )

        for position in result.positions:
            tables["holding_duration_analysis"].append(holding_row(context, position))
        if manager is not None:
            tables["trend_health_events"] = list(getattr(manager, "trend_events", ()))
            by_id = {p.position_id: p for p in result.positions}
            for event in getattr(manager, "profit_events", ()):
                tables["dynamic_profit_events"].append(
                    profit_event_row(context, result, event, by_id.get(event["position_id"]))
                )
        if not tables["trend_health_events"]:
            for p in result.positions:
                for day, session in enumerate(
                    trading_sessions_between(p.entry_date, p.exit_date), 1
                ):
                    tables["trend_health_events"].append(
                        {
                            "position_id": p.position_id,
                            "symbol": p.symbol,
                            "session": session.isoformat(),
                            "holding_day": day,
                            "trend_health": context.trend(p.symbol, session).value,
                            "peer_state": context.peer_state(p.symbol, session).value,
                            "diagnostic_only": True,
                        }
                    )
        tables["entry_gap_summary"] = gap_summary(tables["entry_gap_analysis"])
        tables["peer_summary"] = grouped_outcomes(
            [row for row in tables["peer_context"] if row["observation_phase"] == "signal"],
            "peer_state",
        )
        return tables


def excursions(bars, reference):
    if not bars or not reference:
        return None, None
    return (
        max(0.0, max(float(b.high) for b in bars) / reference - 1),
        min(0.0, min(float(b.low) for b in bars) / reference - 1),
    )


def holding_row(context, position):
    p = position
    sessions = trading_sessions_between(p.entry_date, context.end)
    day10 = sessions[9] if len(sessions) >= 10 else None
    at10 = context.complete_history(p.symbol, day10, 1) if day10 else []
    close10 = float(at10[-1].close) if at10 else None
    # Actual post-day-10 range excludes the exit day's unknown pre/post-fill ordering.
    after = [
        b
        for b in context.history(p.symbol, p.exit_date)
        if day10 and day10 < b.timestamp.date() < p.exit_date
    ]
    mfe, mae = excursions(after, close10)
    # The control's original day-10 exit gets a clearly separate 10-session forward label.
    hypothetical = context.forward_bars(p.symbol, day10, 10) if day10 else []
    counter_mfe, counter_mae = excursions(hypothetical, close10)
    original_time_exit = p.holding_days == 10 and p.exit_reason in {"max_hold", "time_exit"}
    return {
        "position_id": p.position_id,
        "symbol": p.symbol,
        "signal_date": p.signal_date.isoformat(),
        "entry_date": p.entry_date.isoformat(),
        "exit_date": p.exit_date.isoformat(),
        "exit_reason": p.exit_reason,
        "holding_period": p.holding_days,
        "positions_closed_before_10": int(p.holding_days < 10),
        "positions_closed_day_10": int(p.holding_days == 10),
        "positions_extended_after_10": int(p.holding_days > 10),
        **{f"positions_reaching_{n}": int(p.holding_days >= n) for n in (15, 20, 30)},
        "return_day_10": close10 / p.entry_price - 1 if close10 and p.holding_days >= 10 else None,
        "MFE_after_day_10": mfe,
        "MAE_after_day_10": mae,
        "final_return": p.position_return,
        "counterfactual_day11_20_MFE": counter_mfe,
        "counterfactual_day11_20_MAE": counter_mae,
        "counterfactual_window_complete": bool(hypothetical),
        "original_day10_time_exit": original_time_exit,
        "original_time_exit_positive_additional_MFE": (
            counter_mfe > 0 if original_time_exit and counter_mfe is not None else None
        ),
        "original_time_exit_negative_additional_MAE": (
            counter_mae < 0 if original_time_exit and counter_mae is not None else None
        ),
        "excursion_basis": "relative_to_day10_close; completed_pre_exit_sessions_only",
    }


def profit_event_row(context, result, event, position):
    row = dict(event)
    row.update(
        {
            k: None
            for k in (
                "eventual_exit_date",
                "eventual_exit_reason",
                "return_at_original_target",
                "final_return",
                "additional_return_after_deferral",
                "MFE_after_original_target",
                "MAE_after_original_target",
            )
        }
    )
    if position is None:
        return row
    p = position
    costs = result.configuration["backtest"]
    commission = costs["commission_bps"] / 10_000
    fill = event["original_executable_reference"] * (1 - costs["slippage_bps"] / 10_000)
    original = fill * (1 - commission) / (p.entry_price * (1 + commission)) - 1
    target_day = date.fromisoformat(event["session"])
    # Neither target-touch day nor exit day can order extremes around the intrabar fill.
    after = [
        b
        for b in context.history(p.symbol, p.exit_date)
        if target_day < b.timestamp.date() < p.exit_date
    ]
    mfe, mae = excursions(after, event["original_executable_reference"])
    row.update(
        {
            "eventual_exit_date": p.exit_date.isoformat(),
            "eventual_exit_reason": p.exit_reason,
            "return_at_original_target": original,
            "final_return": p.position_return,
            "additional_return_after_deferral": p.position_return - original,
            "MFE_after_original_target": mfe,
            "MAE_after_original_target": mae,
            "excursion_basis": "completed_sessions_strictly_after_target_and_before_exit",
            "excursion_sessions_observed": len(after),
        }
    )
    return row


def grouped_outcomes(rows, field):
    groups = defaultdict(list)
    for row in rows:
        groups[row[field]].append(row)
    output = []
    for key, group in sorted(groups.items()):
        realized = [r["position_result"] for r in group if r.get("position_result") is not None]
        gaps = [r["gap_in_ATR"] for r in group if r.get("gap_in_ATR") is not None]
        output.append(
            {
                field: key,
                "potential_entries": len(group),
                "executed_positions": len(realized),
                "expectancy": mean(realized) if realized else None,
                "win_rate": mean(value > 0 for value in realized) if realized else None,
                "min_gap_in_ATR": min(gaps) if gaps else None,
                "max_gap_in_ATR": max(gaps) if gaps else None,
            }
        )
    return output


def gap_summary(rows):
    labelled = []
    for row in rows:
        value = row["gap_return"]
        label = (
            "UNAVAILABLE"
            if value is None
            else ("NEGATIVE" if value < 0 else "POSITIVE" if value > 0 else "FLAT")
        )
        labelled.append({**row, "gap_group": label})
    output = grouped_outcomes(labelled, "gap_group")
    # Descriptive within-run quintiles, never thresholds exposed to a trading decision.
    valid = sorted((r for r in rows if r["gap_in_ATR"] is not None), key=lambda r: r["gap_in_ATR"])
    quantiles = [
        {**r, "gap_group": f"ATR_QUINTILE_{min(4, i * 5 // len(valid)) + 1}"}
        for i, r in enumerate(valid)
    ]
    output.extend(grouped_outcomes(quantiles, "gap_group"))
    return output
