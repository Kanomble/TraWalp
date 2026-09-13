"""Isolated, fixed 0.5R containment after the unchanged configured Daily entry."""

from dataclasses import dataclass
from datetime import datetime

from trading_system.backtest.position_manager import ExitReason, PositionAction, PositionDecision
from trading_system.models.market_data import BarTimeframe

F_INTRADAY_RISK_RESEARCH_FAMILY = "research-f-intraday-risk-v1"
SUPPORT_NOTE = (
    "Future native coverage membership defines a paired research sample only, never a "
    "production trading signal. Only supported R0_FULL entry signals belong to the sample. "
    "Selected signals outside that fixed sample are consumed without lower-rank substitution."
)


@dataclass(frozen=True, slots=True)
class IntradayRiskVariant:
    research_id: str
    label: str
    common_support: bool = False
    overlay: bool = False


F_INTRADAY_RISK_VARIANTS = (
    IntradayRiskVariant("F-INTRADAY-RISK-R0_FULL", "R0_FULL"),
    IntradayRiskVariant("F-INTRADAY-RISK-R0_COMMON_SUPPORT", "R0_COMMON_SUPPORT", True),
    IntradayRiskVariant("F-INTRADAY-RISK-R1_COMMON_SUPPORT", "R1_COMMON_SUPPORT", True, True),
)
RISK_EVENT_FIELDS = (
    "strategy",
    "position_id",
    "symbol",
    "signal_date",
    "entry_date",
    "entry_price",
    "initial_stop_price",
    "initial_R",
    "breakdown_level",
    "first_breakdown_close_timestamp",
    "second_breakdown_close_timestamp",
    "decision_timestamp",
    "execution_timestamp",
    "execution_reference_price",
    "status",
    "reason",
)


@dataclass
class _RiskState:
    row: dict
    first_close: datetime | None = None
    decision_at: datetime | None = None


class IntradayRiskOverlay:
    """Research-only sample gate and native exit observer; no SQLite or provider access.

    Canonical touches suppress the overlay, then the engine's original Daily path
    supplies the canonical fill. This preserves R0 economics, including Daily ambiguity,
    whenever the overlay cannot execute first. The scanner never mutates stop/target.
    """

    def __init__(self, baseline, supported, native, *, strategy, enabled):
        self.baseline = {(p.signal_date, p.symbol): p for p in baseline}
        self.supported = frozenset(supported)
        self.native = native
        self.strategy = strategy
        self.enabled = enabled
        self.events = []
        self.states = {}

    def allow_entry(self, order, session):
        key = order.signal_date, order.record.symbol
        if key in self.supported:
            return True
        baseline = self.baseline.get(key)
        self.events.append(
            {
                **dict.fromkeys(RISK_EVENT_FIELDS),
                "strategy": self.strategy,
                "position_id": baseline.position_id if baseline else None,
                "symbol": order.record.symbol,
                "signal_date": order.signal_date.isoformat(),
                "entry_date": session.isoformat(),
                "status": "PROVIDER_ABSENT_SUPPORT" if baseline else "OUTSIDE_BASELINE_SUPPORT",
                "reason": "selected_signal_consumed_without_substitution",
            }
        )
        return False

    def on_entry(self, position):
        # stop_price is the actual configured stop just installed by _open_position.
        stop = position.stop_price
        initial_r = position.entry_price - stop if stop is not None else None
        row = {
            **dict.fromkeys(RISK_EVENT_FIELDS),
            "strategy": self.strategy,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "signal_date": position.signal_date.isoformat(),
            "entry_date": position.entry_date.isoformat(),
            "entry_price": position.entry_price,
            "initial_stop_price": stop,
            "initial_R": initial_r,
            "breakdown_level": position.entry_price - 0.5 * initial_r
            if initial_r is not None and initial_r > 0
            else None,
            "status": "NOT_TRIGGERED",
            "reason": "monitoring" if self.enabled else "control_without_overlay",
        }
        self.events.append(row)
        self.states[position.position_id] = _RiskState(row)

    @staticmethod
    def _preempt(state, reason):
        state.row.update(
            status="CANONICAL_STOP_PRECEDED"
            if reason is ExitReason.STOP_LOSS
            else "CANONICAL_TARGET_PRECEDED",
            reason="canonical_exit_precedes_next_native_execution; retain_configured_daily_fill",
        )

    def session_exit(self, position, session):
        if not self.enabled:
            return None
        state = self.states[position.position_id]
        if state.row["status"].startswith("CANONICAL_"):
            # Never claim a later overlay exit after a native canonical touch, even
            # if Daily and intraday aggregates disagree about that touch.
            return None
        level = state.row["breakdown_level"]
        if level is None:
            state.row["reason"] = "initial_R_not_positive"
            return None
        # The spool contains only previously qualified native position-sessions.
        bars, _ = self.native.entry_session(position.symbol, session, session)
        high, low = position.highest_price_since_entry, position.lowest_price_since_entry
        for bar in bars:
            opening = float(bar.open)
            stop, target = position.stop_price, position.target_price
            # Existing levels win at an execution open, including equality and gaps.
            if stop is not None and opening <= stop:
                self._preempt(state, ExitReason.STOP_LOSS)
                return None
            if target is not None and opening >= target:
                self._preempt(state, ExitReason.TAKE_PROFIT)
                return None
            if state.decision_at is not None and bar.timestamp >= state.decision_at:
                position.highest_price_since_entry = max(high, opening)
                position.lowest_price_since_entry = min(low, opening)
                # Only completed pre-exit bars and this actual open affect excursions.
                return PositionDecision(
                    PositionAction.SELL, ExitReason.INTRADAY_RISK_BREAKDOWN, opening
                ), bar
            if stop is not None and float(bar.low) <= stop:
                self._preempt(state, ExitReason.STOP_LOSS)
                return None
            if target is not None and float(bar.high) >= target:
                self._preempt(state, ExitReason.TAKE_PROFIT)
                return None
            high, low = max(high, float(bar.high)), min(low, float(bar.low))
            closed_at = bar.timestamp + BarTimeframe.MINUTES_15.duration
            if float(bar.close) > level:
                state.first_close = None
                state.row["first_breakdown_close_timestamp"] = None
            elif state.first_close is None:
                state.first_close = closed_at
                state.row["first_breakdown_close_timestamp"] = closed_at.isoformat()
            else:
                state.decision_at = closed_at
                state.row.update(
                    second_breakdown_close_timestamp=closed_at.isoformat(),
                    decision_timestamp=closed_at.isoformat(),
                    status="TRIGGERED",
                    reason="awaiting_next_actual_native_open",
                )
        return None

    def on_exit(self, position, decision, bar):
        state = self.states[position.position_id]
        if decision.reason is ExitReason.INTRADAY_RISK_BREAKDOWN:
            state.row.update(
                status="EXECUTED",
                reason="two_consecutive_completed_closes_at_or_below_0.5R",
                execution_timestamp=bar.timestamp.isoformat(),
                execution_reference_price=decision.reference_price,
            )
        elif self.enabled and decision.reason in {ExitReason.STOP_LOSS, ExitReason.TAKE_PROFIT}:
            if not state.row["status"].startswith("CANONICAL_"):
                self._preempt(state, decision.reason)
        elif state.row["status"] in {"NOT_TRIGGERED", "TRIGGERED"}:
            state.row["reason"] = f"canonical_{decision.reason.value}_exit"
