"""Pure post-trade diagnostics; forward bars never feed back into simulation state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import median

from trading_system.backtest.position_manager import PositionState
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestTrade,
    EntryScoreDiagnostics,
    ExecutionMetrics,
    ExitReasonDiagnostics,
    PositionMetrics,
    PostExitReasonDiagnostics,
    StopLossClassification,
    StopLossDiagnostics,
)

MEANINGFUL_PROFIT_TOLERANCE = 0.001
POST_EXIT_HORIZONS = (1, 3, 5, 10)


def finalize_position(
    state: PositionState, legs: Sequence[BacktestTrade]
) -> BacktestPosition:
    """Aggregate all exit fills for one entry using actual quantity and economic P&L."""

    if not legs:
        raise ValueError("A completed position requires at least one execution leg")
    final_leg = legs[-1]
    gross_pnl = sum(leg.gross_pnl if leg.gross_pnl is not None else leg.pnl for leg in legs)
    net_pnl = sum(leg.net_pnl if leg.net_pnl is not None else leg.pnl for leg in legs)
    entry_cost = state.entry_price * state.initial_quantity + state.initial_entry_commission
    position_return = net_pnl / entry_cost
    reference_entry_value = state.entry_reference_price * state.initial_quantity
    gross_market_return = (
        sum(leg.exit_reference_price * leg.quantity for leg in legs) / reference_entry_value - 1
    )
    mfe = state.highest_price_since_entry / state.entry_price - 1
    mae = state.lowest_price_since_entry / state.entry_price - 1
    capture = position_return / mfe if mfe > MEANINGFUL_PROFIT_TOLERANCE else None
    giveback = mfe - position_return
    scores = [observation.total_score for observation in state.score_history]
    entry_score = state.entry_score
    exit_score = scores[-1] if scores else entry_score
    minimum_score = min(scores) if scores else entry_score
    maximum_score = max(scores) if scores else entry_score
    score_change = exit_score - entry_score
    previous_score_change = (
        entry_score - state.previous_entry_score
        if state.previous_entry_score is not None
        else None
    )
    days_since_previous_exit = (
        (state.entry_date - state.previous_exit_date).days
        if state.previous_exit_date is not None
        else None
    )
    return BacktestPosition(
        position_id=state.position_id,
        symbol=state.symbol,
        signal_date=state.signal_date,
        entry_date=state.entry_date,
        exit_date=final_leg.exit_date,
        entry_timestamp=state.entry_timestamp,
        exit_timestamp=final_leg.exit_timestamp,
        entry_reference_price=state.entry_reference_price,
        entry_price=state.entry_price,
        exit_reference_price=final_leg.exit_reference_price,
        exit_price=final_leg.exit_price,
        initial_quantity=state.initial_quantity,
        execution_legs=len(legs),
        holding_days=max(state.holding_days, 1),
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        position_return=position_return,
        gross_market_return=gross_market_return,
        transaction_cost=sum(leg.transaction_cost for leg in legs),
        slippage=sum(leg.slippage for leg in legs),
        exit_reason=final_leg.exit_reason,
        maximum_favorable_excursion=mfe,
        maximum_adverse_excursion=mae,
        profit_capture_ratio=capture,
        profit_giveback=giveback,
        stop_loss_classification=_stop_classification(final_leg, mfe, position_return),
        entry_score=entry_score,
        exit_score=exit_score,
        minimum_score_during_trade=minimum_score,
        maximum_score_during_trade=maximum_score,
        minimum_score_ratio=minimum_score / entry_score if entry_score > 0 else None,
        maximum_score_ratio=maximum_score / entry_score if entry_score > 0 else None,
        score_change_absolute=score_change,
        score_change_percent=score_change / entry_score if entry_score > 0 else None,
        quality_score=state.quality_score,
        valuation_score=state.valuation_score,
        opportunity_score=state.opportunity_score,
        timing_score=state.timing_score,
        score_history=tuple(state.score_history),
        is_reentry=state.is_reentry,
        previous_exit_date=state.previous_exit_date,
        days_since_previous_exit=days_since_previous_exit,
        previous_exit_reason=state.previous_exit_reason,
        previous_position_return=state.previous_position_return,
        previous_position_mfe=state.previous_position_mfe,
        previous_position_mae=state.previous_position_mae,
        previous_entry_score=state.previous_entry_score,
        current_entry_score=entry_score,
        score_change_since_previous_entry=previous_score_change,
        entry_triggers=state.entry_triggers,
        fresh_trigger_since_previous_exit=state.fresh_trigger_since_previous_exit,
        daily_candidate_rank=state.daily_candidate_rank,
        daily_candidate_count=state.daily_candidate_count,
        daily_candidate_score=state.daily_candidate_score,
        daily_candidate_variant=state.daily_candidate_variant,
        confirmation_required=state.confirmation_required,
        confirmation_bar_expected_timestamp=state.confirmation_bar_expected_timestamp,
        confirmation_bar_timestamp=state.confirmation_bar_timestamp,
        confirmation_bar_present=state.confirmation_bar_present,
        confirmation_open=state.confirmation_open,
        confirmation_high=state.confirmation_high,
        confirmation_low=state.confirmation_low,
        confirmation_close=state.confirmation_close,
        confirmation_volume=state.confirmation_volume,
        confirmation_vwap=state.confirmation_vwap,
        confirmation_passed=state.confirmation_passed,
        confirmation_failure_reason=state.confirmation_failure_reason,
        intended_entry_timestamp=state.intended_entry_timestamp,
        actual_entry_timestamp=state.actual_entry_timestamp,
        entry_delayed_from_open=state.entry_delayed_from_open,
        execution_bar_present=state.execution_bar_present,
        trail_guard_enabled=state.trail_guard_enabled,
        completed_bars_before_trail_arm=state.completed_bars_before_trail_arm,
        trail_armed_timestamp=state.trail_armed_timestamp,
        trail_armed_reference_price=state.trail_armed_reference_price,
        atr_at_trail_activation=state.atr_at_trail_activation,
        mfe_at_trail_activation=state.mfe_at_trail_activation,
        initial_risk_per_share_R=state.initial_risk_per_share_R,
        maximum_mfe_in_R=(
            (state.highest_price_since_entry - state.entry_price)
            / state.initial_risk_per_share_R
            if state.initial_risk_per_share_R
            else None
        ),
        profit_lock_state=(
            state.profit_lock_state.value if state.initial_risk_per_share_R else None
        ),
        profit_lock_activation_timestamp=state.profit_lock_activation_timestamp,
        break_even_lock_timestamp=state.break_even_lock_timestamp,
        one_r_lock_timestamp=state.one_r_lock_timestamp,
        active_profit_lock_stop=state.profit_lock_stop_price,
        cooldown_applied=state.cooldown_applied,
        cooldown_blocked=state.cooldown_blocked,
        cooldown_reason=state.cooldown_reason,
        previous_position_net_return=state.previous_position_net_return,
        intraday_session_status=state.intraday_session_status,
        opening_bar_complete=state.opening_bar_complete,
        execution_bar_complete=state.execution_bar_complete,
        gap_affected_trade=state.gap_affected_trade,
        warmup_required_bars=state.warmup_required_bars,
        warmup_available_native_bars=state.warmup_available_native_bars,
        warmup_sufficient=state.warmup_sufficient,
        earliest_warmup_timestamp=state.earliest_warmup_timestamp,
        latest_pre_entry_warmup_timestamp=state.latest_pre_entry_warmup_timestamp,
        warmup_expected_timestamp_gap_count=(
            state.warmup_expected_timestamp_gap_count
        ),
        opening_gate_expected_timestamp=state.opening_gate_expected_timestamp,
        opening_gate_actual_timestamp=state.opening_gate_actual_timestamp,
        opening_gate_open=state.opening_gate_open,
        opening_gate_high=state.opening_gate_high,
        opening_gate_low=state.opening_gate_low,
        opening_gate_close=state.opening_gate_close,
        opening_gate_volume=state.opening_gate_volume,
        opening_gate_vwap=state.opening_gate_vwap,
        opening_gate_green=state.opening_gate_green,
        opening_gate_position_alive_at_evaluation=(
            state.opening_gate_position_alive_at_evaluation
        ),
        baseline_first_bar_trail_exit_occurred=(
            state.baseline_first_bar_trail_exit_occurred
        ),
        opening_gate_evaluated=state.opening_gate_evaluated,
        opening_gate_evaluable=state.opening_gate_evaluable,
        opening_gate_passed=state.opening_gate_passed,
        opening_gate_triggered=state.opening_gate_triggered,
        opening_gate_executable=state.opening_gate_executable,
        opening_gate_failure_reason=state.opening_gate_failure_reason,
        opening_gate_exit_timestamp=state.opening_gate_exit_timestamp,
        opening_gate_exit_reference_price=state.opening_gate_exit_reference_price,
        opening_bar_timestamp=state.opening_bar_timestamp,
        opening_ema20=state.opening_ema20,
        opening_above_ema=state.opening_above_ema,
        first_hour_complete=state.first_hour_complete,
        first_hour_open=state.first_hour_open,
        first_hour_high=state.first_hour_high,
        first_hour_low=state.first_hour_low,
        first_hour_close=state.first_hour_close,
        ema20_at_1030=state.ema20_at_1030,
        pullback_candidate_timestamp=state.pullback_candidate_timestamp,
        pullback_candidate_low=state.pullback_candidate_low,
        pullback_confirmation_timestamp=state.pullback_confirmation_timestamp,
        pullback_confirmation_close=state.pullback_confirmation_close,
        pullback_confirmed=state.pullback_confirmed,
        initial_stop_price=state.initial_stop_price,
        stop_distance_pct=state.stop_distance_pct,
        swing_high_candidate_timestamp=state.swing_high_candidate_timestamp,
        swing_high_candidate_high=state.swing_high_candidate_high,
        swing_high_confirmation_timestamp=state.swing_high_confirmation_timestamp,
        swing_high_confirmed=state.swing_high_confirmed,
        intended_exit_timestamp=state.intended_exit_timestamp,
        actual_exit_timestamp=state.actual_exit_timestamp,
        swing_high_execution_bar_missing=state.swing_high_execution_bar_missing,
    )


def add_post_exit_diagnostics(
    position: BacktestPosition, database: Database, analysis_end
) -> BacktestPosition:
    """Attach forward diagnostics bounded by the backtest horizon."""

    forward = [
        bar
        for bar in database.bars_available_as_of(position.symbol, analysis_end)
        if bar.timestamp.date() > position.exit_date
    ]
    updates: dict[str, float | None] = {}
    reference = position.exit_reference_price
    for horizon in POST_EXIT_HORIZONS:
        if len(forward) < horizon:
            returns = mfe = mae = None
        else:
            window = forward[:horizon]
            returns = float(window[-1].close) / reference - 1
            mfe = max(float(bar.high) for bar in window) / reference - 1
            mae = min(float(bar.low) for bar in window) / reference - 1
        updates[f"post_exit_return_{horizon}d"] = returns
        updates[f"post_exit_mfe_{horizon}d"] = mfe
        updates[f"post_exit_mae_{horizon}d"] = mae
    return position.model_copy(update=updates)


def calculate_execution_metrics(trades: Sequence[BacktestTrade]) -> ExecutionMetrics:
    wins = sum(trade.pnl > 0 for trade in trades)
    losses = sum(trade.pnl < 0 for trade in trades)
    breakeven = len(trades) - wins - losses
    return ExecutionMetrics(
        execution_legs=len(trades),
        winning_execution_legs=wins,
        losing_execution_legs=losses,
        breakeven_execution_legs=breakeven,
        execution_leg_win_rate=wins / len(trades) if trades else None,
        execution_leg_loss_rate=losses / len(trades) if trades else None,
    )


def calculate_position_metrics(
    positions: Sequence[BacktestPosition], positions_opened: int
) -> PositionMetrics:
    wins = [position for position in positions if position.net_pnl > 0]
    losses = [position for position in positions if position.net_pnl < 0]
    captures = [
        position.profit_capture_ratio
        for position in positions
        if position.profit_capture_ratio is not None
    ]
    stops = [position for position in positions if position.exit_reason == "stop_loss"]
    never = sum(
        position.maximum_favorable_excursion <= MEANINGFUL_PROFIT_TOLERANCE
        for position in stops
    )
    profitable_stops = sum(
        position.stop_loss_classification
        is StopLossClassification.PROFITABLE_THEN_STOPPED
        for position in stops
    )
    gap_stops = sum(
        position.stop_loss_classification is StopLossClassification.GAP_THROUGH_STOP
        for position in stops
    )
    reentries = [position for position in positions if position.is_reentry]
    post_returns = _values(positions, "post_exit_return_5d")
    post_mfes = _values(positions, "post_exit_mfe_5d")
    gross_profit = sum(position.net_pnl for position in wins)
    gross_loss = abs(sum(position.net_pnl for position in losses))
    return PositionMetrics(
        positions_opened=positions_opened,
        positions_closed=len(positions),
        winning_positions=len(wins),
        losing_positions=len(losses),
        breakeven_positions=len(positions) - len(wins) - len(losses),
        position_win_rate=len(wins) / len(positions) if positions else None,
        position_loss_rate=len(losses) / len(positions) if positions else None,
        average_position_return=_average(position.position_return for position in positions),
        average_position_win=_average(position.position_return for position in wins),
        average_position_loss=_average(position.position_return for position in losses),
        best_position=max((position.position_return for position in positions), default=None),
        worst_position=min((position.position_return for position in positions), default=None),
        average_position_holding_period=_average(
            position.holding_days for position in positions
        ),
        median_position_holding_period=(
            float(median(position.holding_days for position in positions))
            if positions
            else None
        ),
        gross_position_profit=gross_profit,
        gross_position_loss=gross_loss,
        position_profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        average_mfe=_average(position.maximum_favorable_excursion for position in positions),
        average_mae=_average(position.maximum_adverse_excursion for position in positions),
        average_profit_capture=_average(captures),
        average_profit_giveback=_average(position.profit_giveback for position in positions),
        never_profitable_stop_rate=never / len(stops) if stops else None,
        profitable_then_stopped_rate=profitable_stops / len(stops) if stops else None,
        never_profitable_stop_positions=never,
        profitable_then_stopped_positions=profitable_stops,
        gap_through_stop_positions=gap_stops,
        average_post_exit_return_5d=_average(post_returns),
        average_post_exit_mfe_5d=_average(post_mfes),
        reentry_positions=len(reentries),
        reentries_without_fresh_trigger=sum(
            position.fresh_trigger_since_previous_exit is False for position in reentries
        ),
    )


def aggregate_profit_capture(
    positions: Sequence[BacktestPosition],
) -> tuple[ExitReasonDiagnostics, ...]:
    groups = _group_by_reason(positions)
    return tuple(
        ExitReasonDiagnostics(
            exit_reason=reason,
            positions=len(items),
            average_mfe=_average(item.maximum_favorable_excursion for item in items),
            average_return=_average(item.position_return for item in items),
            average_capture=_average(
                item.profit_capture_ratio
                for item in items
                if item.profit_capture_ratio is not None
            ),
            average_giveback=_average(item.profit_giveback for item in items),
        )
        for reason, items in sorted(groups.items())
    )


def aggregate_stop_losses(
    positions: Sequence[BacktestPosition],
) -> tuple[StopLossDiagnostics, ...]:
    groups: dict[StopLossClassification, list[BacktestPosition]] = defaultdict(list)
    for position in positions:
        if position.stop_loss_classification is not None:
            groups[position.stop_loss_classification].append(position)
    return tuple(
        StopLossDiagnostics(
            classification=classification,
            positions=len(items),
            average_mfe=_average(item.maximum_favorable_excursion for item in items),
            average_mae=_average(item.maximum_adverse_excursion for item in items),
            average_holding_period=_average(item.holding_days for item in items),
            average_loss=_average(item.position_return for item in items),
        )
        for classification, items in sorted(groups.items(), key=lambda item: item[0].value)
    )


def aggregate_post_exit(
    positions: Sequence[BacktestPosition],
) -> tuple[PostExitReasonDiagnostics, ...]:
    groups = _group_by_reason(positions)
    output = []
    for reason, items in sorted(groups.items()):
        returns_1d = _values(items, "post_exit_return_1d")
        returns_3d = _values(items, "post_exit_return_3d")
        returns_5d = _values(items, "post_exit_return_5d")
        returns_10d = _values(items, "post_exit_return_10d")
        output.append(
            PostExitReasonDiagnostics(
                exit_reason=reason,
                positions=len(items),
                observations_1d=len(returns_1d),
                observations_3d=len(returns_3d),
                observations_5d=len(returns_5d),
                observations_10d=len(returns_10d),
                average_return_1d=_average(returns_1d),
                average_return_3d=_average(returns_3d),
                average_return_5d=_average(returns_5d),
                average_return_10d=_average(returns_10d),
                median_return_5d=float(median(returns_5d)) if returns_5d else None,
                positive_forward_rate_5d=(
                    sum(value > 0 for value in returns_5d) / len(returns_5d)
                    if returns_5d
                    else None
                ),
                negative_forward_rate_5d=(
                    sum(value < 0 for value in returns_5d) / len(returns_5d)
                    if returns_5d
                    else None
                ),
                gained_over_3pct_rate_5d=(
                    sum(value > 0.03 for value in returns_5d) / len(returns_5d)
                    if returns_5d
                    else None
                ),
                average_mfe_5d=_average(_values(items, "post_exit_mfe_5d")),
                average_mae_5d=_average(_values(items, "post_exit_mae_5d")),
            )
        )
    return tuple(output)


def aggregate_entry_scores(
    positions: Sequence[BacktestPosition],
) -> tuple[EntryScoreDiagnostics, ...]:
    groups = {
        "winning_positions": [item for item in positions if item.net_pnl > 0],
        "losing_positions": [item for item in positions if item.net_pnl < 0],
        "never_profitable_stop_losses": [
            item
            for item in positions
            if item.exit_reason == "stop_loss"
            and item.maximum_favorable_excursion <= MEANINGFUL_PROFIT_TOLERANCE
        ],
    }
    return tuple(
        EntryScoreDiagnostics(
            group=name,
            positions=len(items),
            average_total_score=_average(_values(items, "entry_score")),
            average_quality_score=_average(_values(items, "quality_score")),
            average_valuation_score=_average(_values(items, "valuation_score")),
            average_opportunity_score=_average(_values(items, "opportunity_score")),
            average_timing_score=_average(_values(items, "timing_score")),
        )
        for name, items in groups.items()
    )


def _stop_classification(
    final_leg: BacktestTrade, mfe: float, position_return: float
) -> StopLossClassification | None:
    if final_leg.exit_reason != "stop_loss":
        return None
    if (
        final_leg.stop_price is not None
        and final_leg.exit_reference_price < final_leg.stop_price * (1 - 1e-9)
    ):
        return StopLossClassification.GAP_THROUGH_STOP
    if mfe <= MEANINGFUL_PROFIT_TOLERANCE:
        return StopLossClassification.NEVER_PROFITABLE
    if position_return < 0:
        return StopLossClassification.PROFITABLE_THEN_STOPPED
    return StopLossClassification.NORMAL_STOP


def _group_by_reason(
    positions: Sequence[BacktestPosition],
) -> dict[str, list[BacktestPosition]]:
    groups: dict[str, list[BacktestPosition]] = defaultdict(list)
    for position in positions:
        groups[position.exit_reason].append(position)
    return groups


def _values(items: Iterable, field: str) -> list[float]:
    return [float(value) for item in items if (value := getattr(item, field)) is not None]


def _average(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else None
