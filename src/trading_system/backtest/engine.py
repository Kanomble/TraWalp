"""Point-in-time daily-bar backtester with next-session simulated execution."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import pandas as pd

from trading_system.backtest.diagnostics import (
    add_post_exit_diagnostics,
    aggregate_entry_scores,
    aggregate_post_exit,
    aggregate_profit_capture,
    aggregate_stop_losses,
    calculate_execution_metrics,
    calculate_position_metrics,
    finalize_position,
)
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.backtest.metrics import calculate_metrics, maximum_drawdown
from trading_system.backtest.position_manager import (
    ExitReason,
    PositionAction,
    PositionDecision,
    PositionManager,
    PositionState,
)
from trading_system.backtest.presets import position_management_preset
from trading_system.config import PositionManagementConfig, StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    intraday_session_bounds,
    is_regular_session_timestamp,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    BenchmarkResult,
    EntryTriggerInfo,
    EquityPoint,
    PositionManagementPreset,
    ScoreObservation,
    StrategyComparison,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.strategy.screener import Screener
from trading_system.technical.indicators import atr as calculate_atr

LOGGER = logging.getLogger(__name__)

SCORE_FILTER_EXCLUSIONS = {
    "quality_score_unavailable",
    "quality_score_below_minimum",
    "valuation_score_unavailable",
    "valuation_score_below_minimum",
    "total_score_unavailable",
    "total_score_below_minimum",
}
BACKTEST_WARNINGS = (
    "historical universe uses current tradable company membership; "
    "results may have survivorship bias",
    "unresolved ticker identity conflicts are conservatively excluded for every historical date",
    "daily bars are provider-adjusted; no additional split adjustment is applied",
)


class MissingIntradayDataError(ValueError):
    """Raised when a reproducible intraday backtest lacks local provider bars."""


class ScreenSource(Protocol):
    def screen(self, session: date) -> ScreenReport: ...


class HistoricalScreenSource:
    """Adapter that guarantees historical screens never consult current snapshots."""

    def __init__(self, database: Database, config: StrategyConfig) -> None:
        self.screener = Screener(database, config)

    def screen(self, session: date) -> ScreenReport:
        return self.screener.run(session, use_market_snapshots=False)


class CachedScreenSource:
    """Session-keyed cache shared by strategy variants without crossing date horizons."""

    def __init__(self, source: ScreenSource) -> None:
        self.source = source
        self.cache: dict[date, ScreenReport] = {}

    def screen(self, session: date) -> ScreenReport:
        if session not in self.cache:
            self.cache[session] = self.source.screen(session)
        return self.cache[session]

    @property
    def diagnostics(self):
        return getattr(self.source, "diagnostics", None)


@dataclass(frozen=True)
class _PendingEntry:
    record: ScreenRecord
    signal_date: date
    variant_score: float
    variant: StrategyVariant
    entry_triggers: EntryTriggerInfo
    previous_position: BacktestPosition | None = None
    fresh_trigger_since_previous_exit: bool | None = None


@dataclass
class _ReentryTracker:
    previous_position: BacktestPosition
    last_triggers: EntryTriggerInfo | None = None
    fresh_trigger: bool = False


class BacktestEngine:
    def __init__(
        self,
        database: Database,
        config: StrategyConfig,
        *,
        screen_source: ScreenSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.config = config
        self.screen_source = screen_source
        self.clock = clock
        self.position_management = config.position_management
        self.position_manager = PositionManager(
            self.position_management,
            slippage_bps=config.backtest.slippage_bps,
            commission_bps=config.backtest.commission_bps,
        )
        self._legacy_reason_compat = False

    def run(
        self,
        start: date,
        end: date,
        *,
        variant: StrategyVariant = StrategyVariant.FULL,
        preset: PositionManagementPreset = PositionManagementPreset.CONFIGURED,
    ) -> BacktestResult:
        if start > end:
            raise ValueError("Backtest start must not be after end")
        self.position_management = position_management_preset(
            self.config.position_management,
            preset,
            legacy_max_holding_days=self.config.backtest.max_holding_days,
        )
        position_timeframe = BarTimeframe(self.position_management.bar_timeframe)
        intraday_monitoring = position_timeframe.intraday
        self.position_manager = PositionManager(
            self.position_management,
            slippage_bps=self.config.backtest.slippage_bps,
            commission_bps=self.config.backtest.commission_bps,
        )
        self._legacy_reason_compat = (
            preset is PositionManagementPreset.CONFIGURED
            and _uses_legacy_position_defaults(
                self.config.position_management, self.config.backtest.max_holding_days
            )
        )
        self._position_sequence = 0
        official_sessions = set(trading_sessions_between(start, end))
        sessions = [
            session
            for session in self.database.bar_sessions(start, end)
            if session in official_sessions
        ]
        if len(sessions) < 2:
            first, last = self.database.bar_date_bounds()
            raise ValueError(
                "Backtest requires at least two local market sessions in the requested range; "
                f"local bar coverage is {first} through {last}"
            )

        cash = float(self.config.backtest.initial_capital)
        positions: dict[str, PositionState] = {}
        pending: list[_PendingEntry] = []
        trades: list[BacktestTrade] = []
        execution_legs: dict[str, list[BacktestTrade]] = {}
        completed_positions: list[BacktestPosition] = []
        reentry_trackers: dict[str, _ReentryTracker] = {}
        intraday_histories: dict[str, list[DailyBar]] = {}
        curve: list[EquityPoint] = []
        skipped: Counter[str] = Counter()
        closed_dates: dict[str, date] = {}
        screen_source = self.screen_source or HistoricalFeatureScreenSource(
            self.database, self.config, sessions[0], sessions[-2]
        )

        def execute_position_decision(
            symbol: str,
            session: date,
            decision: PositionDecision,
            bar: DailyBar,
            *,
            exit_score: float | None = None,
        ) -> bool:
            nonlocal cash
            position = positions[symbol]
            cash, trade, closed = self._execute_decision(
                position,
                session,
                decision,
                cash,
                bar=bar,
                exit_score=exit_score,
            )
            trades.append(trade)
            execution_legs.setdefault(position.position_id, []).append(trade)
            if closed:
                completed = finalize_position(
                    position, execution_legs[position.position_id]
                )
                completed_positions.append(completed)
                reentry_trackers[symbol] = _ReentryTracker(completed)
                closed_dates[symbol] = session
                del positions[symbol]
            return closed

        for index, session in enumerate(sessions):
            final_session = index == len(sessions) - 1
            active_symbols = set(positions) | {order.record.symbol for order in pending}
            bars = self.database.bars_on_session(active_symbols, session)
            intraday_by_symbol: dict[str, list[DailyBar]] = {}
            last_intraday_bars: dict[str, DailyBar] = {}
            regular_open, regular_close = regular_session_bounds(session)
            if intraday_monitoring and active_symbols:
                window_start, window_end = intraday_session_bounds(
                    session, extended_hours=self.config.intraday.extended_hours
                )
                intraday_bars = self.database.bars_between(
                    active_symbols,
                    window_start,
                    window_end,
                    timeframe=position_timeframe,
                )
                for intraday_bar in intraday_bars:
                    intraday_by_symbol.setdefault(intraday_bar.symbol, []).append(
                        intraday_bar
                    )
                    last_intraday_bars[intraday_bar.symbol] = intraday_bar
                missing = sorted(active_symbols - set(intraday_by_symbol))
                if missing:
                    raise self._missing_intraday_data(
                        missing, start, end, position_timeframe
                    )

                for symbol in sorted(active_symbols):
                    history = intraday_histories.get(symbol)
                    if history is None:
                        history = self.database.bars_available_as_of(
                            symbol,
                            window_start,
                            timeframe=position_timeframe,
                            limit=(
                                self.config.intraday.warmup_bars
                                if self.config.intraday.extended_hours
                                else self.config.intraday.warmup_bars * 4
                            ),
                        )
                        if not self.config.intraday.extended_hours:
                            history = [
                                item
                                for item in history
                                if is_regular_session_timestamp(item.timestamp)
                            ][-self.config.intraday.warmup_bars :]
                        intraday_histories[symbol] = history
                    elif history and history[-1].timestamp < window_start:
                        gap = self.database.bars_between(
                            [symbol],
                            history[-1].timestamp + timedelta(microseconds=1),
                            window_start,
                            timeframe=position_timeframe,
                        )
                        if not self.config.intraday.extended_hours:
                            gap = [
                                item
                                for item in gap
                                if is_regular_session_timestamp(item.timestamp)
                            ]
                        history.extend(gap)

            opening_bars = (
                {
                    symbol: symbol_bars[0]
                    for symbol, symbol_bars in intraday_by_symbol.items()
                }
                if intraday_monitoring
                else bars
            )
            session_peak_market_value = sum(
                position.quantity
                * (
                    float(opening_bars[position.symbol].open)
                    if position.symbol in opening_bars
                    else position.last_price
                )
                for position in positions.values()
            )
            session_start_equity = cash + session_peak_market_value

            for position in positions.values():
                position.holding_days += 1

            if intraday_monitoring:
                pending_by_timestamp: dict[datetime, list[_PendingEntry]] = {}
                for order in pending:
                    regular_bars = [
                        item
                        for item in intraday_by_symbol[order.record.symbol]
                        if regular_open <= item.timestamp < regular_close
                    ]
                    if not regular_bars:
                        raise self._missing_intraday_data(
                            [order.record.symbol], start, end, position_timeframe
                        )
                    pending_by_timestamp.setdefault(regular_bars[0].timestamp, []).append(order)

                bars_by_timestamp: dict[datetime, dict[str, DailyBar]] = {}
                for symbol_bars in intraday_by_symbol.values():
                    for intraday_bar in symbol_bars:
                        bars_by_timestamp.setdefault(intraday_bar.timestamp, {})[
                            intraday_bar.symbol
                        ] = intraday_bar
                awaiting_open = set(positions)
                for timestamp in sorted(bars_by_timestamp):
                    timestamp_bars = bars_by_timestamp[timestamp]

                    # Existing overnight positions first see the first enabled-session open.
                    for symbol in sorted(awaiting_open & set(timestamp_bars)):
                        decision = self.position_manager.evaluate_open(
                            positions[symbol], timestamp_bars[symbol]
                        )
                        if decision.action is not PositionAction.HOLD:
                            execute_position_decision(
                                symbol, session, decision, timestamp_bars[symbol]
                            )
                        awaiting_open.remove(symbol)

                    # Prior-close signals enter at each symbol's first regular-session bar.
                    for order in sorted(
                        pending_by_timestamp.get(timestamp, []),
                        key=lambda item: (-item.variant_score, item.record.symbol),
                    ):
                        symbol = order.record.symbol
                        if symbol in positions:
                            skipped["duplicate_position"] += 1
                            continue
                        history = intraday_histories[symbol]
                        if len(history) < self.config.intraday.warmup_bars:
                            raise self._missing_intraday_data(
                                [symbol],
                                start,
                                end,
                                position_timeframe,
                                warmup=True,
                            )
                        entry_atr = self._atr_from_bars(
                            history,
                            self.position_management.atr_trailing_stop.atr_period,
                        )
                        position, cash, reason = self._open_position(
                            order,
                            timestamp_bars[symbol],
                            {
                                key: value[0]
                                for key, value in intraday_by_symbol.items()
                            },
                            positions,
                            cash,
                            entry_atr=entry_atr,
                        )
                        if position is None:
                            skipped[reason or "entry_rejected"] += 1
                            continue
                        positions[position.symbol] = position
                        execution_legs[position.position_id] = []
                        reentry_trackers.pop(position.symbol, None)

                    session_peak_market_value = max(
                        session_peak_market_value,
                        sum(
                            position.quantity
                            * (
                                float(timestamp_bars[symbol].open)
                                if symbol in timestamp_bars
                                else position.last_price
                            )
                            for symbol, position in positions.items()
                        ),
                    )
                    # Intrabar ambiguity remains conservative inside each provider bar.
                    for symbol in sorted(timestamp_bars):
                        bar = timestamp_bars[symbol]
                        if symbol in positions:
                            position = positions[symbol]
                            while True:
                                decision = self.position_manager.evaluate_intrabar(position, bar)
                                if decision.action is PositionAction.HOLD:
                                    break
                                if execute_position_decision(symbol, session, decision, bar):
                                    break
                            if symbol in positions:
                                history = intraday_histories[symbol]
                                history.append(bar)
                                next_atr = self._atr_from_bars(
                                    history,
                                    self.position_management.atr_trailing_stop.atr_period,
                                )
                                self.position_manager.update_after_bar(
                                    position, bar, next_atr=next_atr
                                )
                        elif not intraday_histories[symbol] or (
                            intraday_histories[symbol][-1].timestamp < bar.timestamp
                        ):
                            intraday_histories[symbol].append(bar)
                    session_peak_market_value = max(
                        session_peak_market_value,
                        sum(item.quantity * item.last_price for item in positions.values()),
                    )
                pending = []
            else:
                # 1. Existing positions can gap through levels fixed before this session.
                for symbol in list(positions):
                    bar = bars.get(symbol)
                    if bar is None:
                        continue
                    decision = self.position_manager.evaluate_open(positions[symbol], bar)
                    if decision.action is not PositionAction.HOLD:
                        execute_position_decision(symbol, session, decision, bar)

                # 2. Signals from the prior close execute only now, at this session's open.
                for order in sorted(
                    pending, key=lambda item: (-item.variant_score, item.record.symbol)
                ):
                    if order.record.symbol in positions:
                        skipped["duplicate_position"] += 1
                        continue
                    bar = bars.get(order.record.symbol)
                    if bar is None:
                        skipped["missing_next_session_bar"] += 1
                        continue
                    position, cash, reason = self._open_position(
                        order, bar, bars, positions, cash
                    )
                    if position is None:
                        skipped[reason or "entry_rejected"] += 1
                        continue
                    positions[position.symbol] = position
                    execution_legs[position.position_id] = []
                    reentry_trackers.pop(position.symbol, None)
                    session_peak_market_value = max(
                        session_peak_market_value,
                        sum(item.quantity * item.last_price for item in positions.values()),
                    )
                pending = []

                # 3. Daily OHLC monitoring uses stop-first priority and pre-bar trail levels.
                for symbol in list(positions):
                    bar = bars.get(symbol)
                    if bar is None:
                        continue
                    position = positions[symbol]
                    position.last_price = float(bar.close)
                    while True:
                        decision = self.position_manager.evaluate_intrabar(position, bar)
                        if decision.action is PositionAction.HOLD:
                            break
                        if execute_position_decision(symbol, session, decision, bar):
                            break
                    if symbol in positions:
                        next_atr = self._atr_as_of(
                            symbol,
                            session,
                            self.position_management.atr_trailing_stop.atr_period,
                        )
                        self.position_manager.update_after_bar(
                            position, bar, next_atr=next_atr
                        )

            # 4. Close-based score, rotation and time rules use this completed session only.
            report = None if final_session else screen_source.screen(session)
            records = {record.symbol: record for record in report.records} if report else {}
            best_symbol, best_score = self._best_candidate(report, variant, positions)
            for symbol in list(positions):
                position = positions[symbol]
                bar = bars.get(symbol)
                if bar is None:
                    continue
                record = records.get(symbol)
                current_score = (
                    _variant_score_value(record, variant, self.config) if record else None
                )
                if report is not None and record is not None and current_score is not None:
                    self._record_score(position, report.as_of, record, current_score)
                decision = self.position_manager.evaluate_close(
                    position,
                    float(bar.close),
                    current_score=current_score,
                    best_candidate_symbol=best_symbol,
                    best_candidate_score=best_score,
                )
                if decision.action is PositionAction.SELL:
                    cash, trade, _ = self._execute_decision(
                        position, session, decision, cash, bar=bar, exit_score=current_score
                    )
                    trades.append(trade)
                    execution_legs.setdefault(position.position_id, []).append(trade)
                    completed = finalize_position(
                        position, execution_legs[position.position_id]
                    )
                    completed_positions.append(completed)
                    reentry_trackers[symbol] = _ReentryTracker(completed)
                    closed_dates[symbol] = session
                    del positions[symbol]

            # 5. The last session liquidates at its close; no new signal is queued.
            if final_session:
                for symbol in list(positions):
                    position = positions[symbol]
                    bar = (
                        last_intraday_bars.get(symbol)
                        if intraday_monitoring
                        else bars.get(symbol)
                    )
                    if bar is None:
                        history = self.database.bars_available_as_of(
                            symbol,
                            session,
                            timeframe=position_timeframe,
                            limit=1,
                        )
                        if not history:
                            skipped["missing_final_exit_bar"] += 1
                            continue
                        bar = history[-1]
                    decision = PositionDecision(
                        action=PositionAction.SELL,
                        reason=ExitReason.END_OF_BACKTEST,
                        reference_price=float(bar.close),
                    )
                    cash, trade, _ = self._execute_decision(
                        position,
                        bar.timestamp.date(),
                        decision,
                        cash,
                        bar=bar,
                    )
                    trades.append(trade)
                    execution_legs.setdefault(position.position_id, []).append(trade)
                    completed_positions.append(
                        finalize_position(position, execution_legs[position.position_id])
                    )
                    del positions[symbol]
            else:
                # 6. Exited symbols re-enter only by winning this normal PIT ranking.
                assert report is not None
                pending = self._entry_orders(
                    report,
                    variant,
                    positions,
                    skipped,
                    closed_dates=closed_dates,
                    reentry_trackers=reentry_trackers,
                )

            market_value = sum(
                position.quantity * position.last_price for position in positions.values()
            )
            unrealized_pnl = sum(
                (position.last_price - position.entry_price) * position.quantity
                - position.entry_commission
                for position in positions.values()
            )
            equity = cash + market_value
            session_exposure = (
                session_peak_market_value / session_start_equity
                if session_start_equity > 0
                else 0.0
            )
            end_of_day_exposure = market_value / equity if equity > 0 else 0.0
            curve.append(
                EquityPoint(
                    date=session,
                    cash=max(cash, 0.0),
                    market_value=market_value,
                    portfolio_equity=equity,
                    active_positions=len(positions),
                    exposure=session_exposure,
                    session_exposure=session_exposure,
                    end_of_day_exposure=end_of_day_exposure,
                    realized_pnl=sum(trade.pnl for trade in trades),
                    unrealized_pnl=unrealized_pnl,
                )
            )

        warnings = list(BACKTEST_WARNINGS)
        if intraday_monitoring:
            warnings.extend(
                [
                    f"position management timeframe: {position_timeframe.value}",
                    f"extended hours: {str(self.config.intraday.extended_hours).lower()}",
                    f"intrabar ambiguity remains within {position_timeframe.value} bars; "
                    "pre-bar stops are evaluated first and new trailing highs affect only "
                    "the next bar",
                ]
            )
        else:
            warnings.append(
                "daily OHLC cannot order intrabar events; pre-bar stops are evaluated first "
                "and new trailing highs affect only the next bar"
            )
        first_bound, last_bound = self.database.bar_date_bounds()
        if first_bound is not None and start < first_bound:
            warnings.append(f"requested start predates local bars and was clipped to {sessions[0]}")
        if last_bound is not None and end > last_bound:
            warnings.append(f"requested end exceeds local bars and was clipped to {sessions[-1]}")
        benchmark = self._benchmark(sessions[0], sessions[-1])
        if benchmark.warning:
            warnings.append(benchmark.warning)
        source_diagnostics = getattr(screen_source, "diagnostics", None)
        performance_diagnostics = (
            source_diagnostics.as_dict()
            if source_diagnostics is not None
            else {"sessions_screened": len(sessions) - 1}
        )
        annualized_reliable = len(sessions) >= 63
        if not annualized_reliable:
            warnings.append("annualized metrics are unstable for fewer than 63 trading sessions")
        diagnosed_positions = tuple(
            add_post_exit_diagnostics(position, self.database, sessions[-1])
            for position in completed_positions
        )
        return BacktestResult(
            requested_start=start,
            requested_end=end,
            actual_start=sessions[0],
            actual_end=sessions[-1],
            generated_at=self.clock().isoformat(),
            strategy_variant=variant,
            position_management_preset=preset,
            initial_capital=float(self.config.backtest.initial_capital),
            configuration=self._configuration_snapshot(variant, preset),
            metrics=calculate_metrics(curve, trades, float(self.config.backtest.initial_capital)),
            benchmark=benchmark,
            trades=tuple(trades),
            positions=diagnosed_positions,
            position_metrics=calculate_position_metrics(
                diagnosed_positions, self._position_sequence
            ),
            execution_metrics=calculate_execution_metrics(trades),
            profit_capture_by_exit_reason=aggregate_profit_capture(diagnosed_positions),
            stop_loss_diagnostics=aggregate_stop_losses(diagnosed_positions),
            post_exit_by_reason=aggregate_post_exit(diagnosed_positions),
            entry_score_diagnostics=aggregate_entry_scores(diagnosed_positions),
            equity_curve=tuple(curve),
            skipped_entries=dict(sorted(skipped.items())),
            data_diagnostics={
                "trading_sessions": len(sessions),
                "screened_sessions": len(sessions) - 1,
                "current_tradable_companies": len(self.database.list_tradable_companies()),
                "unresolved_identity_conflicts": len(
                    self.database.unresolved_sec_identity_conflict_symbols()
                ),
                "local_bar_start": first_bound.isoformat() if first_bound else None,
                "local_bar_end": last_bound.isoformat() if last_bound else None,
                "benchmark_available": benchmark.available,
            },
            performance_diagnostics=performance_diagnostics,
            annualized_metrics_reliable=annualized_reliable,
            warnings=tuple(warnings),
            exits_by_reason=dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        )

    def _entry_orders(
        self,
        report: ScreenReport,
        variant: StrategyVariant,
        positions: dict[str, PositionState],
        skipped: Counter[str],
        *,
        closed_dates: dict[str, date] | None = None,
        reentry_trackers: dict[str, _ReentryTracker] | None = None,
    ) -> list[_PendingEntry]:
        trackers = reentry_trackers or {}
        self._update_reentry_trackers(report, trackers)
        capacity = self.config.portfolio.max_positions - len(positions)
        if capacity <= 0:
            return []
        occupied = set(positions)
        candidates: list[_PendingEntry] = []
        for record in report.records:
            if record.symbol in occupied:
                continue
            if not self._reentry_allowed(record.symbol, report.as_of, closed_dates or {}):
                skipped["reentry_cooldown"] += 1
                continue
            score, reason = _variant_entry_score(record, variant, self.config)
            if score is None:
                skipped[reason or "entry_filter"] += 1
                continue
            candidates.append(
                _PendingEntry(
                    record=record,
                    signal_date=report.as_of,
                    variant_score=score,
                    variant=variant,
                    entry_triggers=_entry_triggers(record, self.config),
                    previous_position=(
                        trackers[record.symbol].previous_position
                        if record.symbol in trackers
                        else None
                    ),
                    fresh_trigger_since_previous_exit=(
                        trackers[record.symbol].fresh_trigger
                        if record.symbol in trackers
                        else None
                    ),
                )
            )
        candidates.sort(key=lambda item: (-item.variant_score, item.record.symbol))
        sector_counts = Counter(position.sector for position in positions.values())
        orders: list[_PendingEntry] = []
        for candidate in candidates:
            sector = (candidate.record.sic or "unknown")[:2]
            if sector_counts[sector] >= self.config.portfolio.max_sector_positions:
                skipped["max_sector_positions"] += 1
                continue
            orders.append(candidate)
            sector_counts[sector] += 1
            if len(orders) >= capacity:
                break
        return orders

    def _update_reentry_trackers(
        self,
        report: ScreenReport,
        trackers: dict[str, _ReentryTracker],
    ) -> dict[str, EntryTriggerInfo]:
        states: dict[str, EntryTriggerInfo] = {}
        for record in report.records:
            triggers = _entry_triggers(record, self.config)
            states[record.symbol] = triggers
            tracker = trackers.get(record.symbol)
            if tracker is None:
                continue
            if tracker.last_triggers is not None and _has_fresh_trigger(
                tracker.last_triggers, triggers
            ):
                tracker.fresh_trigger = True
            tracker.last_triggers = triggers
        return states

    def _record_score(
        self,
        position: PositionState,
        session: date,
        record: ScreenRecord,
        total_score: float,
    ) -> None:
        if position.score_history and position.score_history[-1].date == session:
            return
        position.score_history.append(
            ScoreObservation(
                date=session,
                total_score=total_score,
                quality_score=record.scores.quality.score,
                valuation_score=record.scores.valuation.score,
                opportunity_score=record.scores.opportunity.score,
                timing_score=record.scores.timing.score,
            )
        )

    def _best_candidate(
        self,
        report: ScreenReport | None,
        variant: StrategyVariant,
        positions: dict[str, PositionState],
    ) -> tuple[str | None, float | None]:
        if report is None:
            return None, None
        ranked: list[tuple[float, str]] = []
        for record in report.records:
            if record.symbol in positions:
                continue
            score, _ = _variant_entry_score(record, variant, self.config)
            if score is not None:
                ranked.append((score, record.symbol))
        if not ranked:
            return None, None
        ranked.sort(key=lambda item: (-item[0], item[1]))
        score, symbol = ranked[0]
        return symbol, score

    def _reentry_allowed(
        self, symbol: str, signal_date: date, closed_dates: dict[str, date]
    ) -> bool:
        previous_exit = closed_dates.get(symbol)
        if previous_exit is None:
            return True
        rule = self.position_management.reentry
        if not rule.enabled:
            return False
        return (signal_date - previous_exit).days >= rule.cooldown_days

    def _open_position(
        self,
        order: _PendingEntry,
        bar: DailyBar,
        session_bars: dict[str, DailyBar],
        positions: dict[str, PositionState],
        cash: float,
        *,
        entry_atr: float | None = None,
    ) -> tuple[PositionState | None, float, str | None]:
        if len(positions) >= self.config.portfolio.max_positions:
            return None, cash, "max_positions"
        record = order.record
        sector = (record.sic or "unknown")[:2]
        sector_count = sum(position.sector == sector for position in positions.values())
        if sector_count >= self.config.portfolio.max_sector_positions:
            return None, cash, "max_sector_positions"
        atr_period = self.position_management.atr_trailing_stop.atr_period
        atr = entry_atr
        daily_management = (
            BarTimeframe(self.position_management.bar_timeframe) is BarTimeframe.DAY_1
        )
        if atr is None and daily_management:
            atr = (
                record.technical.atr14
                if atr_period == 14
                else self._atr_as_of(record.symbol, order.signal_date, atr_period)
            )
        reference = float(bar.open)
        fill = _buy_fill(reference, self.config.backtest.slippage_bps)
        fixed_stop_percent = self.position_management.stop_loss.percent
        if self.position_management.stop_loss.enabled and fixed_stop_percent is not None:
            stop_distance = fill * fixed_stop_percent
        elif atr is not None and atr > 0:
            stop_distance = min(
                atr * self.config.risk.atr_stop_multiple,
                fill * self.config.risk.max_stop_loss_pct,
            )
        else:
            return None, cash, "invalid_atr"
        if stop_distance <= 0 or stop_distance >= fill:
            return None, cash, "invalid_stop_distance"
        open_equity = cash + sum(
            position.quantity
            * (
                float(session_bars[position.symbol].open)
                if position.symbol in session_bars
                else position.last_price
            )
            for position in positions.values()
        )
        risk_quantity = open_equity * self.config.risk.risk_per_trade / stop_distance
        cap_quantity = open_equity * self.config.portfolio.max_position_pct / fill
        commission_rate = self.config.backtest.commission_bps / 10_000
        cash_quantity = cash / (fill * (1 + commission_rate))
        quantity = min(risk_quantity, cap_quantity, cash_quantity)
        if quantity <= 0:
            return None, cash, "insufficient_cash"
        notional = fill * quantity
        commission = notional * commission_rate
        cost = notional + commission
        if cost > cash + 1e-8:
            return None, cash, "insufficient_cash"
        take_profit_percent = (
            self.position_management.take_profit.percent
            if self.position_management.take_profit.percent is not None
            else self.config.backtest.profit_target_pct
        )
        position = PositionState(
            symbol=record.symbol,
            position_id=self._next_position_id(order, bar.timestamp.date()),
            signal_date=order.signal_date,
            entry_date=bar.timestamp.date(),
            entry_reference_price=reference,
            entry_price=fill,
            quantity=quantity,
            initial_quantity=quantity,
            position_value=notional,
            stop_price=(
                fill - stop_distance if self.position_management.stop_loss.enabled else None
            ),
            target_price=(
                fill * (1 + take_profit_percent)
                if self.position_management.take_profit.enabled
                else None
            ),
            entry_commission=commission,
            initial_entry_commission=commission,
            entry_slippage=(fill - reference) * quantity,
            quality_score=float(record.scores.quality.score),  # validated by entry filter
            valuation_score=float(record.scores.valuation.score),
            opportunity_score=record.scores.opportunity.score,
            timing_score=record.scores.timing.score,
            entry_score=order.variant_score,
            sector=sector,
            variant=order.variant,
            last_price=fill,
            current_atr=atr,
            holding_days=1,
            current_score=order.variant_score,
            entry_triggers=order.entry_triggers,
            score_history=[
                ScoreObservation(
                    date=order.signal_date,
                    total_score=order.variant_score,
                    quality_score=record.scores.quality.score,
                    valuation_score=record.scores.valuation.score,
                    opportunity_score=record.scores.opportunity.score,
                    timing_score=record.scores.timing.score,
                )
            ],
            is_reentry=order.previous_position is not None,
            previous_exit_date=(
                order.previous_position.exit_date if order.previous_position else None
            ),
            previous_exit_reason=(
                order.previous_position.exit_reason if order.previous_position else None
            ),
            previous_position_return=(
                order.previous_position.position_return if order.previous_position else None
            ),
            previous_position_mfe=(
                order.previous_position.maximum_favorable_excursion
                if order.previous_position
                else None
            ),
            previous_position_mae=(
                order.previous_position.maximum_adverse_excursion
                if order.previous_position
                else None
            ),
            previous_entry_score=(
                order.previous_position.entry_score if order.previous_position else None
            ),
            fresh_trigger_since_previous_exit=order.fresh_trigger_since_previous_exit,
            entry_timestamp=bar.timestamp,
        )
        self.position_manager.activate_at_open(position, fill)
        return position, cash - cost, None

    def _next_position_id(self, order: _PendingEntry, entry_date: date) -> str:
        self._position_sequence += 1
        return (
            f"{order.variant.value}-{self._position_sequence:06d}-"
            f"{order.record.symbol}-{entry_date.isoformat()}"
        )

    def _execute_decision(
        self,
        position: PositionState,
        exit_date: date,
        decision: PositionDecision,
        cash: float,
        *,
        bar: DailyBar | None = None,
        exit_score: float | None = None,
    ) -> tuple[float, BacktestTrade, bool]:
        if decision.reason is None or decision.reference_price is None:
            raise ValueError("Sell decision requires reason and reference price")
        reference = decision.reference_price
        quantity = min(decision.quantity or position.quantity, position.quantity)
        if quantity <= 0:
            raise ValueError("Sell quantity must be positive")
        before_quantity = position.quantity
        fraction = quantity / before_quantity
        allocated_value = position.position_value * fraction
        allocated_entry_commission = position.entry_commission * fraction
        allocated_entry_slippage = position.entry_slippage * fraction

        if bar is not None and decision.reason in {
            ExitReason.STOP_LOSS,
            ExitReason.TRAILING_STOP,
            ExitReason.ATR_TRAILING_STOP,
        }:
            position.highest_price_since_entry = max(
                position.highest_price_since_entry, float(bar.open), reference
            )
            position.lowest_price_since_entry = min(
                position.lowest_price_since_entry, float(bar.open), reference
            )
        elif bar is not None and decision.reason in {
            ExitReason.TAKE_PROFIT,
            ExitReason.PARTIAL_TAKE_PROFIT,
        }:
            position.highest_price_since_entry = max(
                position.highest_price_since_entry, reference
            )
            position.lowest_price_since_entry = min(
                position.lowest_price_since_entry, float(bar.open), reference
            )

        fill = _sell_fill(reference, self.config.backtest.slippage_bps)
        proceeds = fill * quantity
        exit_commission = proceeds * self.config.backtest.commission_bps / 10_000
        cash += proceeds - exit_commission
        gross_pnl = proceeds - allocated_value
        pnl = gross_pnl - exit_commission - allocated_entry_commission
        slippage = allocated_entry_slippage + (reference - fill) * quantity
        transaction_cost = allocated_entry_commission + exit_commission
        reason = decision.reason.value
        if decision.reason is ExitReason.MAX_HOLD and self._legacy_reason_compat:
            reason = "time_exit"
        elif decision.reason is ExitReason.TAKE_PROFIT and self._legacy_reason_compat:
            reason = "profit_target"
        denominator = allocated_value + allocated_entry_commission
        closed = quantity >= before_quantity - 1e-12
        position.execution_legs_count += 1
        trade = BacktestTrade(
            symbol=position.symbol,
            signal_date=position.signal_date,
            entry_date=position.entry_date,
            entry_timestamp=position.entry_timestamp,
            entry_reference_price=position.entry_reference_price,
            entry_price=position.entry_price,
            exit_date=exit_date,
            exit_timestamp=bar.timestamp if bar is not None else None,
            exit_reference_price=reference,
            exit_price=fill,
            quantity=quantity,
            position_value=allocated_value,
            stop_price=position.stop_price,
            target_price=position.target_price,
            quality_score=position.quality_score,
            valuation_score=position.valuation_score,
            opportunity_score=position.opportunity_score,
            timing_score=position.timing_score,
            total_score=position.entry_score,
            exit_reason=reason,
            pnl=pnl,
            return_pct=pnl / denominator,
            slippage=slippage,
            transaction_cost=transaction_cost,
            holding_days=max(position.holding_days, 1),
            strategy_variant=position.variant,
            entry_score=position.entry_score,
            exit_score=exit_score if exit_score is not None else position.current_score,
            gross_pnl=gross_pnl,
            net_pnl=pnl,
            highest_price_during_trade=position.highest_price_since_entry,
            lowest_price_during_trade=position.lowest_price_since_entry,
            maximum_favorable_excursion=(
                position.highest_price_since_entry / position.entry_price - 1
            ),
            maximum_adverse_excursion=(
                position.lowest_price_since_entry / position.entry_price - 1
            ),
            fees=transaction_cost,
            slippage_cost=slippage,
            is_partial_exit=not closed,
            partial_level=decision.partial_level,
            position_id=position.position_id,
            execution_leg_id=(
                f"{position.position_id}-L{position.execution_legs_count:02d}"
            ),
        )
        position.realized_profit += pnl
        if decision.partial_level is not None:
            position.partial_exit_levels_triggered.add(decision.partial_level)
        if not closed:
            position.quantity -= quantity
            position.position_value -= allocated_value
            position.entry_commission -= allocated_entry_commission
            position.entry_slippage -= allocated_entry_slippage
        LOGGER.info(
            "POSITION EXIT symbol=%s reason=%s entry=%.4f exit=%.4f return=%.2f%% "
            "holding_days=%d quantity=%.6f partial=%s",
            position.symbol,
            reason,
            position.entry_price,
            fill,
            trade.return_pct * 100,
            trade.holding_days,
            quantity,
            not closed,
        )
        return cash, trade, closed

    def _atr_as_of(self, symbol: str, session: date, period: int) -> float | None:
        bars = self.database.bars_available_as_of(symbol, session)
        return self._atr_from_bars(bars, period)

    @staticmethod
    def _atr_from_bars(bars: list[DailyBar], period: int) -> float | None:
        if len(bars) < period:
            return None
        values = calculate_atr(
            pd.Series([float(bar.high) for bar in bars]),
            pd.Series([float(bar.low) for bar in bars]),
            pd.Series([float(bar.close) for bar in bars]),
            period,
        )
        latest = values.iloc[-1]
        return float(latest) if pd.notna(latest) and float(latest) > 0 else None

    @staticmethod
    def _missing_intraday_data(
        symbols: list[str],
        start: date,
        end: date,
        timeframe: BarTimeframe,
        *,
        warmup: bool = False,
    ) -> MissingIntradayDataError:
        joined = ",".join(sorted(set(symbols)))
        qualifier = " including configured warmup history" if warmup else ""
        return MissingIntradayDataError(
            f"Missing historical {timeframe.value} bars{qualifier} for: {joined}. Run: "
            "python -m trading_system.cli sync-intraday "
            f"--symbols {joined} --start {start.isoformat()} --end {end.isoformat()} "
            f"--timeframes {timeframe.value}"
        )

    def _benchmark(self, start: date, end: date) -> BenchmarkResult:
        bars = [
            bar
            for bar in self.database.bars_available_as_of("SPY", end)
            if bar.timestamp.date() >= start
        ]
        if len(bars) < 2 or bars[0].timestamp.date() > start or bars[-1].timestamp.date() < end:
            return BenchmarkResult(
                available=False,
                warning=(
                    "SPY benchmark unavailable for the complete actual period; synchronize local "
                    "adjusted SPY daily bars"
                ),
            )
        values = [float(bar.close) for bar in bars]
        elapsed_days = (bars[-1].timestamp.date() - bars[0].timestamp.date()).days
        total_return = values[-1] / values[0] - 1
        return BenchmarkResult(
            available=True,
            first_date=bars[0].timestamp.date(),
            last_date=bars[-1].timestamp.date(),
            total_return=total_return,
            cagr=(1 + total_return) ** (365.25 / elapsed_days) - 1 if elapsed_days else None,
            maximum_drawdown=maximum_drawdown(values),
        )

    def _configuration_snapshot(
        self, variant: StrategyVariant, preset: PositionManagementPreset
    ) -> dict:
        return {
            "variant": variant.value,
            "strategy": self.config.model_dump(mode="json"),
            "portfolio": self.config.portfolio.model_dump(mode="json"),
            "risk": self.config.risk.model_dump(mode="json"),
            "backtest": self.config.backtest.model_dump(mode="json"),
            "position_management_preset": preset.value,
            "position_management": self.position_management.model_dump(mode="json"),
            "score_weights": self.config.scores.total.model_dump(mode="json"),
            "market_data_adjustment": self.config.universe.market_data_adjustment,
            "execution": {
                "entry": "next available portfolio session open",
                "screening_timeframe": BarTimeframe.DAY_1.value,
                "position_management_timeframe": BarTimeframe(
                    self.position_management.bar_timeframe
                ).value,
                "extended_hours": self.config.intraday.extended_hours,
                "intrabar_ambiguity": "pre_bar_stops_first; new trails apply next bar",
                "close_rules": "signal_decay, portfolio_rotation, max_hold",
                "end_of_backtest": "last available close",
            },
            "variant_definition": {
                "A": "Quality + Value scoring with common technical recovery entry gate",
                "B": "Quality + Value + Opportunity scoring with common recovery gate",
                "C": "Quality + Value + Opportunity + Timing scoring with common recovery gate",
            }[variant.value],
            "common_recovery_gate": {
                "applies_to": [item.value for item in StrategyVariant],
                "price_above_sma20": True,
                "any_of": [
                    "rsi_recovery",
                    "momentum5_above_zero",
                    "relative_volume_above_threshold",
                ],
                "relative_volume_threshold": self.config.backtest.min_relative_volume,
            },
        }


def compare_strategies(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    comparison_kind: StrategyComparisonKind = StrategyComparisonKind.ALL,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StrategyComparison:
    """Compare score variants, position presets, or both on shared PIT screens."""

    shared = CachedScreenSource(HistoricalFeatureScreenSource(database, config, start, end))
    generated_at = clock().isoformat()
    runs = _comparison_runs(comparison_kind)
    results: list[BacktestResult] = []
    skipped: dict[str, str] = {}
    for variant, preset in runs:
        label = _comparison_label(variant, preset, comparison_kind)
        try:
            result = BacktestEngine(
                database,
                config,
                screen_source=shared,
                clock=lambda: datetime.fromisoformat(generated_at),
            ).run(start, end, variant=variant, preset=preset)
        except MissingIntradayDataError as exc:
            skipped[label] = str(exc)
            continue
        results.append(result)
    if not results:
        raise ValueError("Strategy comparison produced no executable strategies")
    first = results[0]
    return StrategyComparison(
        requested_start=start,
        requested_end=end,
        actual_start=first.actual_start,
        actual_end=first.actual_end,
        generated_at=generated_at,
        variants=tuple(results),
        shared_screen_sessions=len(shared.cache),
        warnings=first.warnings,
        comparison_kind=comparison_kind,
        skipped_strategies=skipped,
    )


def compare_position_management(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StrategyComparison:
    """Backward-compatible wrapper around the unified comparison function."""

    return compare_strategies(
        database,
        config,
        start,
        end,
        comparison_kind=StrategyComparisonKind.POSITION_MANAGEMENT,
        clock=clock,
    )


def _comparison_runs(
    comparison_kind: StrategyComparisonKind,
) -> tuple[tuple[StrategyVariant, PositionManagementPreset], ...]:
    score_runs = tuple(
        (variant, PositionManagementPreset.CONFIGURED) for variant in StrategyVariant
    )
    position_runs = tuple(
        (StrategyVariant.FULL, preset)
        for preset in PositionManagementPreset
        if preset is not PositionManagementPreset.CONFIGURED
    )
    if comparison_kind is StrategyComparisonKind.SCORE_VARIANTS:
        return score_runs
    if comparison_kind is StrategyComparisonKind.POSITION_MANAGEMENT:
        return position_runs
    return (*score_runs, *position_runs)


def _comparison_label(
    variant: StrategyVariant,
    preset: PositionManagementPreset,
    comparison_kind: StrategyComparisonKind,
) -> str:
    if comparison_kind is StrategyComparisonKind.SCORE_VARIANTS:
        return variant.value
    if comparison_kind is StrategyComparisonKind.POSITION_MANAGEMENT:
        return preset.value
    return f"{variant.value}/{preset.value}"


def _variant_entry_score(
    record: ScreenRecord, variant: StrategyVariant, config: StrategyConfig
) -> tuple[float | None, str | None]:
    blocking = set(record.exclusion_reasons) - SCORE_FILTER_EXCLUSIONS
    if blocking:
        return None, sorted(blocking)[0]
    quality = record.scores.quality.score
    valuation = record.scores.valuation.score
    opportunity = record.scores.opportunity.score
    timing = record.scores.timing.score
    rules = config.backtest
    if quality is None or quality < rules.min_quality_score:
        return None, "quality_threshold"
    if valuation is None or valuation < rules.min_valuation_score:
        return None, "valuation_threshold"
    components: list[tuple[float, float]] = [
        (quality, config.scores.total.quality),
        (valuation, config.scores.total.valuation),
    ]
    if variant in {StrategyVariant.QUALITY_VALUE_OPPORTUNITY, StrategyVariant.FULL}:
        if opportunity is None or opportunity < rules.min_opportunity_score:
            return None, "opportunity_threshold"
        components.append((opportunity, config.scores.total.opportunity))
    if variant is StrategyVariant.FULL:
        if timing is None or timing < rules.min_timing_score:
            return None, "timing_threshold"
        components.append((timing, config.scores.total.timing))
    denominator = sum(weight for _, weight in components)
    score = sum(value * weight for value, weight in components) / denominator
    if score < rules.min_total_score:
        return None, "total_threshold"
    technical = record.technical
    if technical.price is None or technical.sma20 is None or technical.price <= technical.sma20:
        return None, "price_not_above_sma20"
    recovered = (
        technical.rsi_recovery is True
        or (technical.momentum5 is not None and technical.momentum5 > 0)
        or (
            technical.relative_volume is not None
            and technical.relative_volume > rules.min_relative_volume
        )
    )
    if not recovered:
        return None, "recovery_signal_required"
    return score, None


def _variant_score_value(
    record: ScreenRecord, variant: StrategyVariant, config: StrategyConfig
) -> float | None:
    """Comparable 0..100 score without applying entry gates to an open position."""

    components: list[tuple[float, float]] = []
    values = (
        (record.scores.quality.score, config.scores.total.quality),
        (record.scores.valuation.score, config.scores.total.valuation),
    )
    if any(value is None for value, _ in values):
        return None
    components.extend((float(value), weight) for value, weight in values if value is not None)
    if variant in {StrategyVariant.QUALITY_VALUE_OPPORTUNITY, StrategyVariant.FULL}:
        opportunity = record.scores.opportunity.score
        if opportunity is None:
            return None
        components.append((opportunity, config.scores.total.opportunity))
    if variant is StrategyVariant.FULL:
        timing = record.scores.timing.score
        if timing is None:
            return None
        components.append((timing, config.scores.total.timing))
    denominator = sum(weight for _, weight in components)
    return sum(value * weight for value, weight in components) / denominator


def _entry_triggers(record: ScreenRecord, config: StrategyConfig) -> EntryTriggerInfo:
    technical = record.technical
    return EntryTriggerInfo(
        price_above_sma20=(
            technical.price is not None
            and technical.sma20 is not None
            and technical.price > technical.sma20
        ),
        rsi_recovery=technical.rsi_recovery is True,
        momentum5_above_zero=(
            technical.momentum5 is not None and technical.momentum5 > 0
        ),
        relative_volume_above_threshold=(
            technical.relative_volume is not None
            and technical.relative_volume > config.backtest.min_relative_volume
        ),
    )


def _has_fresh_trigger(previous: EntryTriggerInfo, current: EntryTriggerInfo) -> bool:
    return any(
        not getattr(previous, field) and getattr(current, field)
        for field in EntryTriggerInfo.model_fields
    )


def _uses_legacy_position_defaults(
    config: PositionManagementConfig, legacy_max_holding_days: int
) -> bool:
    return (
        BarTimeframe(config.bar_timeframe) is BarTimeframe.DAY_1
        and config.stop_loss.enabled
        and config.stop_loss.percent is None
        and config.take_profit.enabled
        and config.take_profit.percent is None
        and not config.trailing_stop.enabled
        and not config.atr_trailing_stop.enabled
        and not config.signal_decay.enabled
        and not config.partial_take_profit.enabled
        and config.max_hold.enabled
        and config.max_hold.mode == "hard"
        and config.max_hold.days in {None, legacy_max_holding_days}
        and not config.portfolio_rotation.enabled
        and config.reentry.enabled
        and config.reentry.cooldown_days == 0
    )


def _buy_fill(reference: float, slippage_bps: float) -> float:
    return reference * (1 + slippage_bps / 10_000)


def _sell_fill(reference: float, slippage_bps: float) -> float:
    return reference * (1 - slippage_bps / 10_000)
