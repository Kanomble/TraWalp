"""Opt-in F lifecycle hypotheses; no production presets or global configuration changes."""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from trading_system.backtest.position_manager import (
    ExitReason,
    PositionAction,
    PositionDecision,
    PositionManager,
    PositionState,
)
from trading_system.backtest.research_definitions import (
    F_LIFECYCLE_RESEARCH_FAMILY as F_LIFECYCLE_RESEARCH_FAMILY,
)
from trading_system.backtest.research_definitions import (
    F_LIFECYCLE_VARIANTS as F_LIFECYCLE_VARIANTS,
)
from trading_system.backtest.research_definitions import (
    LifecyclePreset as LifecyclePreset,
)
from trading_system.config import StrategyConfig
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.market_data import DailyBar


class TrendHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    WEAKENING = "WEAKENING"
    UNAVAILABLE = "UNAVAILABLE"


class PeerTrendState(StrEnum):
    CONFIRMED = "PEER_CONFIRMED"
    WEAK = "PEER_WEAK"
    UNAVAILABLE = "PEER_UNAVAILABLE"


class EntryCapacityProvider(Protocol):
    def __call__(self, signal_session: date) -> int: ...


class LifecycleContextProvider(Protocol):
    def trend(self, symbol: str, session: date) -> TrendHealthState: ...

    def peer_state(self, symbol: str, session: date) -> PeerTrendState: ...


def lifecycle_strategy_config(config: StrategyConfig, preset: LifecyclePreset) -> StrategyConfig:
    """Only fixed holding duration changes; capacity is an independent composition hook."""
    if preset not in F_LIFECYCLE_VARIANTS:
        raise ValueError("Unregistered lifecycle hypothesis")
    result = config.model_copy(deep=True)
    if preset.max_hold_days is not None and not preset.conditional_extension:
        result = result.model_copy(
            update={
                "position_management": result.position_management.model_copy(
                    update={
                        "max_hold": result.position_management.max_hold.model_copy(
                            update={"days": preset.max_hold_days}
                        )
                    }
                )
            }
        )
    return result


def previous_session(session: date) -> date:
    return trading_sessions_between(session - timedelta(days=14), session - timedelta(days=1))[-1]


class LifecyclePositionManager(PositionManager):
    """Adapt configured decisions, preserving stop-first and the configured target price.

    Target decisions use yesterday's completed observations. New deterioration exits are
    queued at the close and executed at the next real Daily open, after gap stops.
    """

    def __init__(self, config, *, preset, context, slippage_bps=0, commission_bps=0):
        super().__init__(config, slippage_bps=slippage_bps, commission_bps=commission_bps)
        self.preset: LifecyclePreset = preset
        self.context: LifecycleContextProvider = context
        self.session: date | None = None
        self.extended: set[str] = set()
        self.deferred: dict[str, date] = {}
        self.pending_exits: dict[str, ExitReason] = {}
        self.trend_events: list[dict] = []
        self.profit_events: list[dict] = []

    def start_session(self, session: date) -> None:
        self.session = session

    def _states(self, position, *, prior=False):
        assert self.session is not None
        observed = previous_session(self.session) if prior else self.session
        return (
            self.context.trend(position.symbol, observed),
            self.context.peer_state(position.symbol, observed),
            observed,
        )

    def evaluate_open(self, position: PositionState, bar: DailyBar) -> PositionDecision:
        decision = super().evaluate_open(position, bar)
        # Deferred targets are disabled in _profit_decision, but stop gaps stay executable.
        if decision.reason in {
            ExitReason.STOP_LOSS,
            ExitReason.TRAILING_STOP,
            ExitReason.ATR_TRAILING_STOP,
            ExitReason.PROFIT_LOCK,
        }:
            return decision
        reason = self.pending_exits.pop(position.position_id, None)
        if reason is not None:
            return PositionDecision(PositionAction.SELL, reason, float(bar.open))
        return decision

    def _profit_decision(self, position, observed_high, *, gap_reference=None):
        if not self.preset.defer_profit_target:
            return super()._profit_decision(position, observed_high, gap_reference=gap_reference)
        if position.position_id in self.deferred:
            return PositionDecision(PositionAction.HOLD)
        decision = super()._profit_decision(position, observed_high, gap_reference=gap_reference)
        if decision.reason is not ExitReason.TAKE_PROFIT:
            return decision
        trend, peers, observed = self._states(position, prior=True)
        defer = trend is TrendHealthState.HEALTHY and peers is PeerTrendState.CONFIRMED
        self.profit_events.append(
            {
                "position_id": position.position_id,
                "symbol": position.symbol,
                "signal_date": position.signal_date.isoformat(),
                "entry_date": position.entry_date.isoformat(),
                "session": self.session.isoformat(),
                "holding_day": position.holding_days,
                "context_session": observed.isoformat(),
                "profit_target_price": position.target_price,
                "profit_target_reached": True,
                "original_executable_reference": decision.reference_price,
                "trend_health": trend.value,
                "peer_state": peers.value,
                "profit_target_deferred": defer,
            }
        )
        if defer:
            self.deferred[position.position_id] = self.session
            return PositionDecision(PositionAction.HOLD)
        return decision

    def evaluate_close(self, position, current_price, **kwargs):
        decision = super().evaluate_close(position, current_price, **kwargs)
        trend, peers, observed = self._states(position)
        allowed = trend is TrendHealthState.HEALTHY and (
            not self.preset.require_peers or peers is PeerTrendState.CONFIRMED
        )
        if (
            self.preset.conditional_extension
            and decision.reason is ExitReason.MAX_HOLD
            and position.holding_days < self.preset.max_hold_days
            and (position.position_id in self.extended or allowed)
        ):
            self.extended.add(position.position_id)
            decision = PositionDecision(PositionAction.HOLD)
        if self.preset.defer_profit_target and position.position_id in self.deferred:
            if trend is not TrendHealthState.HEALTHY:
                self.pending_exits[position.position_id] = ExitReason.LIFECYCLE_TREND
            elif peers is not PeerTrendState.CONFIRMED:
                self.pending_exits[position.position_id] = ExitReason.LIFECYCLE_PEERS
        self.trend_events.append(
            {
                "position_id": position.position_id,
                "symbol": position.symbol,
                "session": observed.isoformat(),
                "holding_day": position.holding_days,
                "trend_health": trend.value,
                "peer_state": peers.value,
                "holding_extended": position.position_id in self.extended,
                "profit_target_deferred": position.position_id in self.deferred,
                "decision": decision.action.value,
                "exit_reason": decision.reason.value if decision.reason else None,
                "queued_exit_reason": self.pending_exits.get(position.position_id),
            }
        )
        return decision
