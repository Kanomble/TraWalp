"""Session adapter to canonical F ranking, engine fills/sizing and PositionManager."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

from trading_system.backtest.diagnostics import finalize_position
from trading_system.backtest.engine import (
    BacktestEngine,
    _ReentryTracker,
    _uses_legacy_position_defaults,
    _variant_score_value,
)
from trading_system.backtest.position_manager import PositionAction, PositionManager
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.champion import champion_ranking
from trading_system.models.backtest import BacktestPosition
from trading_system.shadow.contracts import digest, normalized
from trading_system.shadow.qualification import qualify_f_inputs
from trading_system.shadow.state import PENDING, POSITION
from trading_system.strategy.screener import Screener


def ranking_incompleteness(report, session, config) -> list[dict]:
    """Compatibility entry point for the causal forward completeness contract."""
    return qualify_f_inputs(report, session, config)["incomplete_inputs"]


class ChampionSession:
    """Only orchestration is new. Every economic rule is delegated to the engine."""

    def __init__(self, database, config, *, screen_source=None, qualified_inputs=None):
        self.database, self.config, self.screen_source = database, config, screen_source
        self.qualified_inputs = qualified_inputs
        self.engine = BacktestEngine(database, config)
        champion = FROZEN_CHAMPION_F
        self.engine.current_preset = champion.preset
        self.engine.position_management = position_management_preset(
            config.position_management,
            champion.preset,
            legacy_max_holding_days=config.backtest.max_holding_days,
        )
        self.engine.position_manager = PositionManager(
            self.engine.position_management,
            slippage_bps=config.backtest.slippage_bps,
            commission_bps=config.backtest.commission_bps,
        )
        self.engine._legacy_reason_compat = _uses_legacy_position_defaults(
            config.position_management, config.backtest.max_holding_days
        )

    def process(self, state, session, next_session, bars, emit):
        state = deepcopy(state)
        engine, manager = self.engine, self.engine.position_manager
        engine._position_sequence = state["position_sequence"]
        positions = {}
        if state["position"]:
            position = POSITION.validate_python(state["position"])
            positions[position.symbol] = position
        pending = PENDING.validate_python(state["pending"]["order"]) if state["pending"] else None
        cash = state["cash"]
        realized = state["realized_pnl"]
        last_exits = deepcopy(state["last_exits"])

        def transition(kind, **details):
            nonlocal state
            after = normalized(
                {
                    "cash": cash,
                    "realized_pnl": realized,
                    "position": next(iter(positions.values()), None),
                    "pending": state["pending"],
                    "position_sequence": engine._position_sequence,
                    "last_exits": last_exits,
                }
            )
            if "pending_after" in details:
                after["pending"] = normalized(details.pop("pending_after"))
            emit(
                kind, {"before_state_digest": digest(state), "after": after, **normalized(details)}
            )
            state = deepcopy(after)

        def execute(position, decision, bar):
            nonlocal cash, realized
            cash, trade, closed = engine._execute_decision(
                position, session, decision, cash, bar=bar, exit_score=position.current_score
            )
            if not closed:
                raise RuntimeError("Frozen F forbids partial exits")
            completed = finalize_position(position, [trade])
            last_exits[position.symbol] = normalized(completed)
            realized += trade.pnl
            del positions[position.symbol]
            transition(
                "EXIT_EXECUTED", trade=trade, completed_position=completed, reason=trade.exit_reason
            )

        # Match the configured Daily engine ordering, including entry-session hold=1.
        for position in positions.values():
            position.holding_days += 1
            transition("POSITION_MANAGED", phase="SESSION_OPEN")
        for position in list(positions.values()):
            decision = manager.evaluate_open(position, bars[position.symbol])
            if decision.action is not PositionAction.HOLD:
                execute(position, decision, bars[position.symbol])
        if pending:
            position, cash, reason = engine._open_position(
                pending, bars[pending.record.symbol], bars, positions, cash
            )
            if position is not None:
                positions[position.symbol] = position
                transition(
                    "ENTRY_EXECUTED",
                    pending_after=None,
                    reference=float(bars[position.symbol].open),
                )
            else:
                transition("ENTRY_REJECTED", pending_after=None, reason=reason)
        for position in list(positions.values()):
            bar = bars[position.symbol]
            position.last_price = float(bar.close)
            decision = manager.evaluate_intrabar(position, bar)
            if decision.action is not PositionAction.HOLD:
                execute(position, decision, bar)
            else:
                next_atr = engine._atr_as_of(position.symbol, session, 14)
                manager.update_after_bar(position, bar, next_atr=next_atr)
                transition("POSITION_MANAGED", phase="BAR_COMPLETED")

        # Signal T is evaluated at the close, after prior intent execution and risk exits.
        if self.qualified_inputs is not None:
            raw, report = self.qualified_inputs.raw_report, self.qualified_inputs.ranked_report
            qualification = self.qualified_inputs.qualification
            upstream_issues = [
                {"symbol": None, "reason": reason}
                for reason in self.qualified_inputs.details["blocking_reasons"]
                if reason != "RANKING_UNRESOLVED"
            ]
        else:
            raw = (
                self.screen_source.screen(session)
                if self.screen_source is not None
                else Screener(self.database, self.config).run(session, use_market_snapshots=False)
            )
            report = champion_ranking(raw, self.config)
            qualification = qualify_f_inputs(raw, session, self.config, ranked=report)
            upstream_issues = []
        issues = qualification["incomplete_inputs"] + upstream_issues
        candidates = [
            {
                "symbol": r.symbol,
                "f_score": r.scores.total,
                "quality_score": r.scores.quality.score,
                "valuation_score": r.scores.valuation.score,
                "rank": r.rank,
            }
            for r in report.records
            if r.eligible
        ]
        candidates.sort(key=lambda item: item["rank"])
        provenance = deepcopy(getattr(self.database, "provenance", {}))
        # The entire feature set is digested, including ineligible peer members.
        # generated_at is intentionally excluded: it is not a decision input.
        feature_rows = sorted((raw or report).records, key=lambda item: item.symbol)
        provenance["relevant_feature_set_digest"] = digest(feature_rows)
        provenance["position_management_ohlc_digest"] = digest(bars)
        emit(
            "SCREEN_EVALUATED",
            {
                "eligible_candidate_count": len(candidates),
                "candidates": candidates,
                "ranking_digest": digest(candidates),
                "data_complete": not issues,
                "incomplete_inputs": issues,
                "qualification": qualification,
                "input_readiness": self.qualified_inputs.as_dict()
                if self.qualified_inputs is not None
                else None,
                "input_provenance": provenance,
                "input_set_digest": digest(provenance),
                "exclusion_summary": dict(
                    sorted(
                        Counter(
                            reason
                            for r in report.records
                            if not r.eligible
                            for reason in r.exclusion_reasons
                        ).items()
                    )
                ),
            },
        )
        records = {r.symbol: r for r in report.records}
        for position in list(positions.values()):
            record = records.get(position.symbol)
            current_score = (
                _variant_score_value(record, FROZEN_CHAMPION_F.variant, self.config)
                if record
                else None
            )
            if current_score is not None:
                engine._record_score(position, session, record, current_score)
            decision = manager.evaluate_close(
                position, float(bars[position.symbol].close), current_score=current_score
            )
            if decision.action is PositionAction.SELL:
                execute(position, decision, bars[position.symbol])
            else:
                transition("POSITION_MANAGED", phase="SESSION_CLOSE")
        if issues:
            reason = "NO_TRADE_DATA_INCOMPLETE"
        elif positions or state["pending"]:
            reason = "CAPACITY_FULL"
        elif not candidates:
            reason = "NO_ELIGIBLE_CANDIDATES"
        else:
            trackers = {
                symbol: _ReentryTracker(BacktestPosition.model_validate(value))
                for symbol, value in last_exits.items()
            }
            orders = engine._entry_orders(
                report,
                FROZEN_CHAMPION_F.variant,
                positions,
                Counter(),
                execution_session=next_session,
                closed_dates={
                    symbol: tracker.previous_position.exit_date
                    for symbol, tracker in trackers.items()
                },
                reentry_trackers=trackers,
            )
            if orders:
                order = orders[0]
                transition(
                    "ENTRY_INTENT_CREATED",
                    pending_after={
                        "order": normalized(order),
                        "expected_execution_session": next_session.isoformat(),
                    },
                    symbol=order.record.symbol,
                    rank=order.daily_candidate_rank,
                    f_score=order.variant_score,
                    atr14=order.record.technical.atr14,
                    signal_session=session,
                    expected_execution_session=next_session,
                )
                return state
            reason = "ENTRY_REJECTED_BY_CHAMPION"
        emit("DECISION_NO_TRADE", {"reason": reason, "state_digest": digest(state)})
        return state
