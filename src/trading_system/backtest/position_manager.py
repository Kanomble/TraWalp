"""Deterministic, composable decisions for already-open long positions."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from trading_system.config import PositionManagementConfig
from trading_system.models.backtest import EntryTriggerInfo, ScoreObservation, StrategyVariant
from trading_system.models.market_data import DailyBar

LOGGER = logging.getLogger(__name__)


class PositionAction(StrEnum):
    HOLD = "hold"
    SELL = "sell"
    PARTIAL_SELL = "partial_sell"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    ATR_TRAILING_STOP = "atr_trailing_stop"
    PARTIAL_TAKE_PROFIT = "partial_take_profit"
    SIGNAL_DECAY = "signal_decay"
    MAX_HOLD = "max_hold"
    TIME_DECAY = "time_decay"
    PORTFOLIO_ROTATION = "portfolio_rotation"
    END_OF_BACKTEST = "end_of_backtest"


@dataclass
class PositionState:
    """Mutable state belonging to one simulated position, never to a symbol globally."""

    symbol: str
    position_id: str
    signal_date: date
    entry_date: date
    entry_reference_price: float
    entry_price: float
    quantity: float
    initial_quantity: float
    position_value: float
    stop_price: float | None
    target_price: float | None
    entry_commission: float
    initial_entry_commission: float
    entry_slippage: float
    quality_score: float
    valuation_score: float
    opportunity_score: float | None
    timing_score: float | None
    entry_score: float
    sector: str
    variant: StrategyVariant
    last_price: float
    current_atr: float | None = None
    holding_days: int = 0
    current_score: float | None = None
    highest_price_since_entry: float = 0.0
    lowest_price_since_entry: float = math.inf
    highest_price_since_trailing_activation: float | None = None
    highest_price_since_atr_activation: float | None = None
    trailing_stop_price: float | None = None
    atr_trailing_stop_price: float | None = None
    partial_exit_levels_triggered: set[int] = field(default_factory=set)
    realized_profit: float = 0.0
    entry_triggers: EntryTriggerInfo = field(default_factory=EntryTriggerInfo)
    score_history: list[ScoreObservation] = field(default_factory=list)
    is_reentry: bool = False
    previous_exit_date: date | None = None
    previous_exit_reason: str | None = None
    previous_position_return: float | None = None
    previous_position_mfe: float | None = None
    previous_position_mae: float | None = None
    previous_entry_score: float | None = None
    fresh_trigger_since_previous_exit: bool | None = None
    execution_legs_count: int = 0
    entry_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.highest_price_since_entry <= 0:
            self.highest_price_since_entry = self.entry_price
        if not math.isfinite(self.lowest_price_since_entry):
            self.lowest_price_since_entry = self.entry_price


@dataclass(frozen=True)
class PositionDecision:
    action: PositionAction
    reason: ExitReason | None = None
    reference_price: float | None = None
    quantity: float | None = None
    partial_level: int | None = None
    details: dict[str, float | int | str | bool | None] = field(default_factory=dict)


class PositionManager:
    """Evaluate risk exits first, then score/portfolio/time exits at the close.

    Stop levels used inside a Daily OHLC bar were fixed before that bar began. High-water
    marks observed in the bar can only raise a trailing stop for the following bar.
    """

    def __init__(
        self,
        config: PositionManagementConfig,
        *,
        slippage_bps: float = 0,
        commission_bps: float = 0,
    ) -> None:
        self.config = config
        self.round_trip_cost_rate = 2 * (slippage_bps + commission_bps) / 10_000

    def activate_at_open(self, position: PositionState, opening: float) -> None:
        """Activation at the known opening price is safe before evaluating the remaining bar."""

        trailing = self.config.trailing_stop
        if trailing.enabled and opening >= position.entry_price * (1 + trailing.activation_profit):
            position.highest_price_since_trailing_activation = max(
                position.highest_price_since_trailing_activation or opening, opening
            )
            candidate = opening * (1 - trailing.trailing_distance)
            position.trailing_stop_price = _raise_only(position.trailing_stop_price, candidate)

        atr_rule = self.config.atr_trailing_stop
        if (
            atr_rule.enabled
            and opening >= position.entry_price * (1 + atr_rule.activation_profit)
            and _positive_finite(position.current_atr)
        ):
            position.highest_price_since_atr_activation = max(
                position.highest_price_since_atr_activation or opening, opening
            )
            candidate = opening - float(position.current_atr) * atr_rule.atr_multiplier
            if candidate > 0:
                position.atr_trailing_stop_price = _raise_only(
                    position.atr_trailing_stop_price, candidate
                )

    def evaluate_open(self, position: PositionState, bar: DailyBar) -> PositionDecision:
        opening = float(bar.open)
        self.activate_at_open(position, opening)
        for stop, reason in self._stops(position):
            if opening <= stop:
                return self._decision(position, PositionAction.SELL, reason, opening)
        return self._profit_decision(position, opening, gap_reference=opening)

    def evaluate_intrabar(self, position: PositionState, bar: DailyBar) -> PositionDecision:
        low = float(bar.low)
        high = float(bar.high)
        for stop, reason in self._stops(position):
            if low <= stop:
                return self._decision(position, PositionAction.SELL, reason, stop)
        return self._profit_decision(position, high)

    def update_after_bar(
        self, position: PositionState, bar: DailyBar, *, next_atr: float | None = None
    ) -> None:
        """Consume a completed bar and prepare trail levels for the next bar only."""

        high = float(bar.high)
        low = float(bar.low)
        position.highest_price_since_entry = max(position.highest_price_since_entry, high)
        position.lowest_price_since_entry = min(position.lowest_price_since_entry, low)
        position.last_price = float(bar.close)

        trailing = self.config.trailing_stop
        if trailing.enabled and high >= position.entry_price * (1 + trailing.activation_profit):
            position.highest_price_since_trailing_activation = max(
                position.highest_price_since_trailing_activation or high, high
            )
            candidate = (
                position.highest_price_since_trailing_activation
                * (1 - trailing.trailing_distance)
            )
            position.trailing_stop_price = _raise_only(position.trailing_stop_price, candidate)

        if _positive_finite(next_atr):
            position.current_atr = float(next_atr)
        atr_rule = self.config.atr_trailing_stop
        if (
            atr_rule.enabled
            and high >= position.entry_price * (1 + atr_rule.activation_profit)
            and _positive_finite(position.current_atr)
        ):
            position.highest_price_since_atr_activation = max(
                position.highest_price_since_atr_activation or high, high
            )
            candidate = (
                position.highest_price_since_atr_activation
                - float(position.current_atr) * atr_rule.atr_multiplier
            )
            if candidate > 0:
                position.atr_trailing_stop_price = _raise_only(
                    position.atr_trailing_stop_price, candidate
                )

    def evaluate_close(
        self,
        position: PositionState,
        current_price: float,
        *,
        current_score: float | None,
        best_candidate_symbol: str | None = None,
        best_candidate_score: float | None = None,
    ) -> PositionDecision:
        position.current_score = current_score if _finite(current_score) else None
        ratio = _score_ratio(position.entry_score, position.current_score)

        decay = self.config.signal_decay
        if decay.enabled and ratio is not None and ratio < decay.minimum_score_ratio:
            return self._decision(
                position,
                PositionAction.SELL,
                ExitReason.SIGNAL_DECAY,
                current_price,
                score_ratio=ratio,
            )

        rotation = self.config.portfolio_rotation
        improvement = _score_improvement(position.current_score, best_candidate_score)
        if (
            rotation.enabled
            and position.holding_days >= rotation.minimum_holding_days
            and best_candidate_symbol is not None
            and best_candidate_symbol != position.symbol
            and improvement is not None
            and improvement >= rotation.minimum_score_improvement
            and improvement > self.round_trip_cost_rate
        ):
            return self._decision(
                position,
                PositionAction.SELL,
                ExitReason.PORTFOLIO_ROTATION,
                current_price,
                score_improvement=improvement,
                best_candidate=best_candidate_symbol,
                best_candidate_score=best_candidate_score,
            )

        max_hold = self.config.max_hold
        mode = max_hold.mode if max_hold.enabled else "disabled"
        max_hold_reached = (
            max_hold.days is not None and position.holding_days >= max_hold.days
        )
        if mode != "disabled" and max_hold_reached:
            if mode == "hard":
                return self._decision(
                    position, PositionAction.SELL, ExitReason.MAX_HOLD, current_price
                )
            if ratio is not None and ratio < max_hold.review_minimum_score_ratio:
                return self._decision(
                    position,
                    PositionAction.SELL,
                    ExitReason.MAX_HOLD,
                    current_price,
                    score_ratio=ratio,
                )

        return self._decision(position, PositionAction.HOLD, score_ratio=ratio)

    def _stops(self, position: PositionState) -> tuple[tuple[float, ExitReason], ...]:
        candidates: list[tuple[float, ExitReason]] = []
        if self.config.stop_loss.enabled and position.stop_price is not None:
            candidates.append((position.stop_price, ExitReason.STOP_LOSS))
        if self.config.trailing_stop.enabled and position.trailing_stop_price is not None:
            candidates.append((position.trailing_stop_price, ExitReason.TRAILING_STOP))
        if (
            self.config.atr_trailing_stop.enabled
            and position.atr_trailing_stop_price is not None
        ):
            candidates.append(
                (position.atr_trailing_stop_price, ExitReason.ATR_TRAILING_STOP)
            )
        return tuple(sorted(candidates, key=lambda item: item[0], reverse=True))

    def _profit_decision(
        self,
        position: PositionState,
        observed_high: float,
        *,
        gap_reference: float | None = None,
    ) -> PositionDecision:
        partial = self._partial_decision(position, observed_high, gap_reference)
        partial_trigger = (
            position.entry_price
            * (1 + self.config.partial_take_profit.levels[partial.partial_level].profit)
            if partial is not None and partial.partial_level is not None
            else None
        )
        target_reached = (
            position.target_price is not None and observed_high >= position.target_price
        )
        if target_reached and (
            partial_trigger is None or position.target_price <= partial_trigger
        ):
            return self._decision(
                position,
                PositionAction.SELL,
                ExitReason.TAKE_PROFIT,
                gap_reference if gap_reference is not None else position.target_price,
            )
        if partial is not None:
            return partial
        return self._decision(position, PositionAction.HOLD)

    def _partial_decision(
        self, position: PositionState, observed_high: float, gap_reference: float | None = None
    ) -> PositionDecision | None:
        partial = self.config.partial_take_profit
        if not partial.enabled:
            return None
        for index, level in enumerate(partial.levels):
            if index in position.partial_exit_levels_triggered:
                continue
            trigger = position.entry_price * (1 + level.profit)
            if observed_high >= trigger:
                reference = gap_reference if gap_reference is not None else trigger
                quantity = position.quantity * level.sell_fraction
                action = (
                    PositionAction.SELL
                    if math.isclose(quantity, position.quantity)
                    else PositionAction.PARTIAL_SELL
                )
                return self._decision(
                    position,
                    action,
                    ExitReason.PARTIAL_TAKE_PROFIT,
                    reference,
                    quantity=quantity,
                    partial_level=index,
                )
        return None

    def _decision(
        self,
        position: PositionState,
        action: PositionAction,
        reason: ExitReason | None = None,
        reference_price: float | None = None,
        *,
        quantity: float | None = None,
        partial_level: int | None = None,
        **extra: float | int | str | bool | None,
    ) -> PositionDecision:
        details: dict[str, float | int | str | bool | None] = {
            "holding_days": position.holding_days,
            "entry_price": position.entry_price,
            "current_price": position.last_price,
            "return_pct": position.last_price / position.entry_price - 1,
            "entry_score": position.entry_score,
            "current_score": position.current_score,
            "atr": position.current_atr,
            "high_water_mark": position.highest_price_since_entry,
            "trailing_stop": position.trailing_stop_price,
            "atr_trailing_stop": position.atr_trailing_stop_price,
            **extra,
        }
        LOGGER.debug(
            "POSITION symbol=%s action=%s reason=%s details=%s",
            position.symbol,
            action.value,
            reason.value if reason else None,
            details,
        )
        return PositionDecision(
            action=action,
            reason=reason,
            reference_price=reference_price,
            quantity=quantity,
            partial_level=partial_level,
            details=details,
        )


def _raise_only(previous: float | None, candidate: float) -> float:
    return candidate if previous is None else max(previous, candidate)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _positive_finite(value: float | None) -> bool:
    return _finite(value) and float(value) > 0


def _score_ratio(entry_score: float | None, current_score: float | None) -> float | None:
    # TraWalp scores share a bounded 0..100 scale. Zero/missing/non-finite values are not
    # ratio-compatible and intentionally cannot cause a decay exit.
    if not _positive_finite(entry_score) or not _finite(current_score) or float(current_score) < 0:
        return None
    return float(current_score) / float(entry_score)


def _score_improvement(current_score: float | None, candidate_score: float | None) -> float | None:
    if not _finite(current_score) or not _finite(candidate_score):
        return None
    if float(current_score) < 0 or float(candidate_score) < 0:
        return None
    if float(current_score) == 0:
        return math.inf if float(candidate_score) > 0 else 0.0
    return (float(candidate_score) - float(current_score)) / float(current_score)
