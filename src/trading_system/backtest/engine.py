"""Point-in-time daily-bar backtester with next-session simulated execution."""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import pandas as pd

from trading_system.backtest.coverage import warmup_coverage_diagnostics
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
from trading_system.backtest.first_hour_pullback import (
    F4_STOP_DISTANCE_PCT,
    SwingHighDetector,
    plan_first_hour_pullback,
)
from trading_system.backtest.metrics import calculate_metrics, maximum_drawdown
from trading_system.backtest.position_manager import (
    ExitReason,
    PositionAction,
    PositionDecision,
    PositionManager,
    PositionState,
)
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import (
    RESEARCH_FAMILY_RUNS,
    STRATEGY_RESEARCH_REGISTRY,
    comparison_strategy_label,
    research_family_runs,
)
from trading_system.backtest.research_registry import (
    research_strategy_label as registered_research_strategy_label,
)
from trading_system.config import PositionManagementConfig, StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    intraday_session_bounds,
    intraday_warmup_start,
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
    IntradayPrefetch,
    IntradayPrefetchTimeframe,
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
CONFIRMED_ENTRY_PRESETS = {
    PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY,
    PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
}
HYBRID_ENTRY_PRESETS = {PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING}
NEGATIVE_COOLDOWN_PRESETS = CONFIRMED_ENTRY_PRESETS
GROSS_LOSS_COOLDOWN_PRESETS = {
    PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
}
OPENING_SURVIVOR_GATE_PRESETS = {
    PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
}
INTRADAY_ISOLATION_PRESETS = {
    PositionManagementPreset.INTRADAY_DYNAMIC,
    *GROSS_LOSS_COOLDOWN_PRESETS,
    *OPENING_SURVIVOR_GATE_PRESETS,
}
THESIS_RECOVERY_PRESETS = {
    PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
}
FIRST_HOUR_PULLBACK_PRESETS = {
    PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
}
RESEARCH_CANDIDATE_EVENT_PRESETS = {
    *INTRADAY_ISOLATION_PRESETS,
    *THESIS_RECOVERY_PRESETS,
    *FIRST_HOUR_PULLBACK_PRESETS,
}
# Backward-compatible public collection, derived from the central registry.
RESEARCH_PRESETS = {
    metadata.preset
    for metadata in STRATEGY_RESEARCH_REGISTRY
    if metadata.family in {"d1-d5-archive", "intraday-isolation", "intraday-next"}
    and not metadata.control
}


class MissingIntradayDataError(ValueError):
    """Raised when a reproducible intraday backtest lacks local provider bars."""


class ScreenSource(Protocol):
    def screen(self, session: date) -> ScreenReport: ...


class IntradaySynchronizer(Protocol):
    def sync_intraday(
        self,
        requested_symbols,
        timeframes,
        start: datetime,
        end: datetime,
        *,
        incremental: bool | None = None,
        extended_hours: bool | None = None,
    ) -> dict: ...


@dataclass(frozen=True, slots=True)
class EntryFilterEvaluation:
    """Structured result of the canonical backtest entry funnel.

    The audit consumes this object instead of reimplementing entry thresholds.
    ``failure_detail`` distinguishes unavailable input from a genuine threshold
    miss while ``first_failure`` retains the public skipped-entry reason.
    """

    score: float | None
    first_failure: str | None
    failure_detail: str | None
    blocking_reasons: tuple[str, ...]
    quality_score: float | None
    valuation_score: float | None
    opportunity_score: float | None
    timing_score: float | None
    weighted_score: float | None
    price_above_sma20: bool | None
    rsi_recovery: bool | None
    momentum5_above_zero: bool | None
    relative_volume_above_threshold: bool | None
    recovery_gate_pass: bool | None

    @property
    def eligible(self) -> bool:
        return self.score is not None and self.first_failure is None


class CandidateAuditObserver(Protocol):
    """Optional non-trading observer used by the historical candidate audit."""

    def observe_screen(
        self,
        report: ScreenReport,
        variant: StrategyVariant,
        config: StrategyConfig,
    ) -> None: ...

    def observe_portfolio_decision(
        self,
        signal_date: date,
        symbol: str,
        outcome: str,
        reason: str | None = None,
    ) -> None: ...

    def observe_execution(
        self,
        signal_date: date,
        execution_date: date,
        symbol: str,
        executed: bool,
        reason: str | None = None,
    ) -> None: ...


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


@dataclass(frozen=True, slots=True)
class IntradayPrefetchRequirement:
    """Candidate-bounded local data requirement for one position timeframe."""

    timeframe: BarTimeframe
    variants: tuple[StrategyVariant, ...]
    symbols: tuple[str, ...]
    first_execution_sessions: tuple[tuple[str, date], ...]
    comparison_sessions: tuple[date, ...]
    requested_start: datetime
    requested_end: datetime
    warmup_bars: int
    extended_hours: bool


@dataclass(frozen=True, slots=True)
class StrategyComparisonPreparation:
    """Frozen comparison plan whose PIT screen cache is reused by every run."""

    screen_source: CachedScreenSource
    requested_start: date
    requested_end: date
    comparison_kind: StrategyComparisonKind
    sessions: tuple[date, ...]
    runs: tuple[tuple[StrategyVariant, PositionManagementPreset], ...]
    intraday_requirements: tuple[IntradayPrefetchRequirement, ...]

    @property
    def intraday_candidate_symbols(self) -> int:
        return len(
            {
                symbol
                for requirement in self.intraday_requirements
                for symbol in requirement.symbols
            }
        )


@dataclass(frozen=True, slots=True)
class IntradayCoverageAssessment:
    """Local execution coverage before a provider synchronization."""

    requirement: IntradayPrefetchRequirement
    complete_symbols: tuple[str, ...]
    sync_symbols: tuple[str, ...]
    incomplete_reasons: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass
class _PendingEntry:
    record: ScreenRecord
    signal_date: date
    variant_score: float
    variant: StrategyVariant
    entry_triggers: EntryTriggerInfo
    previous_position: BacktestPosition | None = None
    fresh_trigger_since_previous_exit: bool | None = None
    daily_candidate_rank: int | None = None
    daily_candidate_count: int | None = None
    confirmation_bar_expected_timestamp: datetime | None = None
    confirmation_bar_timestamp: datetime | None = None
    confirmation_bar_present: bool = False
    confirmation_open: float | None = None
    confirmation_high: float | None = None
    confirmation_low: float | None = None
    confirmation_close: float | None = None
    confirmation_volume: int | None = None
    confirmation_vwap: float | None = None
    confirmation_passed: bool | None = None
    confirmation_failure_reason: str | None = None
    intended_entry_timestamp: datetime | None = None
    execution_bar_present: bool = False
    cooldown_applied: bool = False
    cooldown_blocked: bool = False
    cooldown_reason: str | None = None
    intraday_session_status: str | None = None
    candidate_event_index: int | None = None
    research_metadata: dict = field(default_factory=dict)
    f4_confirmation_bar: DailyBar | None = None


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
        audit_observer: CandidateAuditObserver | None = None,
        strict_coverage_sensitivity: bool = False,
        intraday_session_statuses: dict[tuple[str, date], str] | None = None,
        allow_missing_intraday_data: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.config = config
        self.screen_source = screen_source
        self.audit_observer = audit_observer
        self.strict_coverage_sensitivity = strict_coverage_sensitivity
        self.intraday_session_statuses = intraday_session_statuses or {}
        self.allow_missing_intraday_data = allow_missing_intraday_data
        self.clock = clock
        self.position_management = config.position_management
        self.position_manager = PositionManager(
            self.position_management,
            slippage_bps=config.backtest.slippage_bps,
            commission_bps=config.backtest.commission_bps,
        )
        self._legacy_reason_compat = False
        self.current_preset = PositionManagementPreset.CONFIGURED
        self._research_counters: Counter[str] = Counter()
        self._confirmation_events: list[dict] = []
        self._candidate_events: list[dict] = []

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
        self.current_preset = preset
        position_timeframe = BarTimeframe(self.position_management.bar_timeframe)
        intraday_monitoring = position_timeframe.intraday
        confirmed_entry = preset in CONFIRMED_ENTRY_PRESETS
        hybrid_entry = preset in HYBRID_ENTRY_PRESETS
        opening_survivor_gate = preset in OPENING_SURVIVOR_GATE_PRESETS
        first_hour_pullback = preset in FIRST_HOUR_PULLBACK_PRESETS
        native_intraday_loop = intraday_monitoring or confirmed_entry
        execution_timeframe = (
            BarTimeframe.MINUTES_15 if confirmed_entry else position_timeframe
        )
        self.position_manager = PositionManager(
            self.position_management,
            slippage_bps=self.config.backtest.slippage_bps,
            commission_bps=self.config.backtest.commission_bps,
        )
        self._legacy_reason_compat = (
            preset is PositionManagementPreset.LEGACY
            or (
                preset is PositionManagementPreset.CONFIGURED
                and _uses_legacy_position_defaults(
                    self.config.position_management, self.config.backtest.max_holding_days
                )
            )
        )
        self._position_sequence = 0
        self._research_counters: Counter[str] = Counter()
        self._confirmation_events: list[dict] = []
        self._candidate_events: list[dict] = []
        sessions = _backtest_sessions(self.database, start, end)

        cash = float(self.config.backtest.initial_capital)
        positions: dict[str, PositionState] = {}
        pending: list[_PendingEntry] = []
        trades: list[BacktestTrade] = []
        execution_legs: dict[str, list[BacktestTrade]] = {}
        completed_positions: list[BacktestPosition] = []
        reentry_trackers: dict[str, _ReentryTracker] = {}
        intraday_histories: dict[str, list[DailyBar]] = {}
        f4_exit_detectors: dict[str, SwingHighDetector] = {}
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
            if first_hour_pullback:
                position.actual_exit_timestamp = bar.timestamp
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
                self._complete_candidate_event(position, completed)
                f4_exit_detectors.pop(position.position_id, None)
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
            if native_intraday_loop and active_symbols:
                window_start, window_end = intraday_session_bounds(
                    session, extended_hours=self.config.intraday.extended_hours
                )
                intraday_bars = self.database.bars_between(
                    active_symbols,
                    window_start,
                    window_end,
                    timeframe=execution_timeframe,
                )
                strict_excluded_symbols = {
                    symbol
                    for symbol in active_symbols
                    if self.strict_coverage_sensitivity
                    and self.intraday_session_statuses.get((symbol, session)) != "COMPLETE"
                }
                if strict_excluded_symbols:
                    self._research_counters["strict_coverage_exclusions"] += len(
                        strict_excluded_symbols
                    )
                for intraday_bar in intraday_bars:
                    if intraday_bar.symbol in strict_excluded_symbols:
                        continue
                    if first_hour_pullback and not (
                        regular_open <= intraday_bar.timestamp < regular_close
                    ):
                        continue
                    intraday_by_symbol.setdefault(intraday_bar.symbol, []).append(
                        intraday_bar
                    )
                    last_intraday_bars[intraday_bar.symbol] = intraday_bar
                missing = sorted(active_symbols - set(intraday_by_symbol))
                if (
                    missing
                    and not confirmed_entry
                    and not first_hour_pullback
                    and not self.strict_coverage_sensitivity
                    and not self.allow_missing_intraday_data
                ):
                    raise self._missing_intraday_data(
                        missing, start, end, execution_timeframe
                    )

                for symbol in sorted(active_symbols):
                    history = intraday_histories.get(symbol)
                    if history is None:
                        history = self.database.bars_available_as_of(
                            symbol,
                            window_start,
                            timeframe=execution_timeframe,
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
                            timeframe=execution_timeframe,
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
                if native_intraday_loop
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

            if native_intraday_loop:
                if confirmed_entry:
                    pending_by_timestamp = self._confirmed_pending_entries(
                        pending,
                        intraday_by_symbol,
                        session,
                        skipped,
                    )
                elif first_hour_pullback:
                    pending_by_timestamp = self._first_hour_pullback_pending_entries(
                        pending,
                        intraday_by_symbol,
                        intraday_histories,
                        session,
                        skipped,
                    )
                else:
                    pending_by_timestamp: dict[datetime, list[_PendingEntry]] = {}
                    for order in pending:
                        order.intraday_session_status = self.intraday_session_statuses.get(
                            (order.record.symbol, session), "NATIVE_SESSION"
                        )
                        regular_bars = [
                            item
                            for item in intraday_by_symbol.get(order.record.symbol, ())
                            if regular_open <= item.timestamp < regular_close
                        ]
                        if not regular_bars:
                            self._update_candidate_event(
                                order,
                                entry_bar_expected_timestamp=regular_open.isoformat(),
                                entry_bar_present=False,
                                actual_entry_timestamp=None,
                                execution_failure_reason="missing_intraday_entry_session",
                            )
                            if self.strict_coverage_sensitivity:
                                order.intraday_session_status = (
                                    self.intraday_session_statuses.get(
                                        (order.record.symbol, session),
                                        "UNKNOWN",
                                    )
                                )
                                skipped["strict_incomplete_session"] += 1
                                continue
                            if self.allow_missing_intraday_data:
                                skipped["missing_intraday_entry_session"] += 1
                                self._observe_execution(
                                    order,
                                    session,
                                    executed=False,
                                    reason="missing_intraday_entry_session",
                                )
                                continue
                            raise self._missing_intraday_data(
                                [order.record.symbol], start, end, execution_timeframe
                            )
                        pending_by_timestamp.setdefault(regular_bars[0].timestamp, []).append(
                            order
                        )
                        order.intended_entry_timestamp = regular_bars[0].timestamp
                        order.execution_bar_present = True
                        canonical_entry_present = any(
                            item.timestamp == regular_open for item in regular_bars
                        )
                        self._update_candidate_event(
                            order,
                            entry_bar_expected_timestamp=regular_open.isoformat(),
                            entry_bar_present=canonical_entry_present,
                            actual_entry_timestamp=regular_bars[0].timestamp.isoformat(),
                            execution_failure_reason=(
                                None
                                if canonical_entry_present
                                else "missing_canonical_entry_bar"
                            ),
                        )

                bars_by_timestamp: dict[datetime, dict[str, DailyBar]] = {}
                for symbol_bars in intraday_by_symbol.values():
                    for intraday_bar in symbol_bars:
                        bars_by_timestamp.setdefault(intraday_bar.timestamp, {})[
                            intraday_bar.symbol
                        ] = intraday_bar
                awaiting_open = set(positions)
                if hybrid_entry:
                    # Overnight D5 positions retain the existing Daily swing semantics.
                    for symbol in list(positions):
                        daily_bar = bars.get(symbol)
                        if daily_bar is None:
                            continue
                        while symbol in positions:
                            decision = self.position_manager.evaluate_open(
                                positions[symbol], daily_bar
                            )
                            if decision.action is PositionAction.HOLD:
                                break
                            if execute_position_decision(
                                symbol, session, decision, daily_bar
                            ):
                                break
                        if symbol not in positions:
                            continue
                        position = positions[symbol]
                        while True:
                            decision = self.position_manager.evaluate_intrabar(
                                position, daily_bar
                            )
                            if decision.action is PositionAction.HOLD:
                                break
                            if execute_position_decision(
                                symbol, session, decision, daily_bar
                            ):
                                break
                        if symbol in positions:
                            next_atr = self._atr_as_of(
                                symbol,
                                session,
                                self.position_management.atr_trailing_stop.atr_period,
                            )
                            self.position_manager.update_after_bar(
                                position, daily_bar, next_atr=next_atr
                            )
                    awaiting_open.clear()
                for timestamp in sorted(bars_by_timestamp):
                    timestamp_bars = bars_by_timestamp[timestamp]

                    # Existing overnight positions first see the first enabled-session open.
                    for symbol in sorted(awaiting_open & set(timestamp_bars)):
                        while symbol in positions:
                            decision = self.position_manager.evaluate_open(
                                positions[symbol], timestamp_bars[symbol]
                            )
                            if decision.action is PositionAction.HOLD:
                                break
                            if execute_position_decision(
                                symbol, session, decision, timestamp_bars[symbol]
                            ):
                                break
                        awaiting_open.remove(symbol)

                    # Prior-close signals enter at each symbol's first regular-session bar.
                    for order in sorted(
                        pending_by_timestamp.get(timestamp, []),
                        key=lambda item: (-item.variant_score, item.record.symbol),
                    ):
                        symbol = order.record.symbol
                        if symbol in positions:
                            skipped["duplicate_position"] += 1
                            self._update_candidate_event(
                                order,
                                executed=False,
                                execution_failure_reason="duplicate_position",
                            )
                            self._observe_execution(
                                order, session, executed=False, reason="duplicate_position"
                            )
                            continue
                        entry_atr = None
                        if intraday_monitoring and not first_hour_pullback:
                            history = intraday_histories[symbol]
                            if len(history) < self.config.intraday.warmup_bars:
                                if self.allow_missing_intraday_data:
                                    skipped["insufficient_intraday_warmup"] += 1
                                    self._update_candidate_event(
                                        order,
                                        executed=False,
                                        warmup_sufficient=False,
                                        execution_failure_reason=(
                                            "insufficient_intraday_warmup"
                                        ),
                                    )
                                    self._observe_execution(
                                        order,
                                        session,
                                        executed=False,
                                        reason="insufficient_intraday_warmup",
                                    )
                                    continue
                                raise self._missing_intraday_data(
                                    [symbol],
                                    start,
                                    end,
                                    execution_timeframe,
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
                            warmup_history=(
                                intraday_histories[symbol]
                                if intraday_monitoring
                                else None
                            ),
                        )
                        if position is None:
                            skipped[reason or "entry_rejected"] += 1
                            self._observe_execution(
                                order,
                                session,
                                executed=False,
                                reason=reason or "entry_rejected",
                            )
                            self._update_candidate_event(
                                order,
                                executed=False,
                                execution_failure_reason=reason or "entry_rejected",
                            )
                            continue
                        positions[position.symbol] = position
                        execution_legs[position.position_id] = []
                        if first_hour_pullback:
                            if order.f4_confirmation_bar is None:
                                raise AssertionError(
                                    "F4 executable entry requires its completed confirmation bar"
                                )
                            f4_exit_detectors[position.position_id] = SwingHighDetector(
                                previous_bar=order.f4_confirmation_bar
                            )
                        reentry_trackers.pop(position.symbol, None)
                        self._observe_execution(order, session, executed=True)
                        self._update_candidate_event(
                            order,
                            executed=True,
                            warmup_sufficient=position.warmup_sufficient,
                            warmup_available_native_bars=(
                                position.warmup_available_native_bars
                            ),
                            actual_entry_timestamp=timestamp_bars[
                                symbol
                            ].timestamp.isoformat(),
                        )

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
                            if hybrid_entry and position.entry_date != session:
                                continue
                            if first_hour_pullback:
                                detector = f4_exit_detectors[position.position_id]
                                if detector.exit_due(bar.timestamp):
                                    if (
                                        detector.intended_exit_timestamp is not None
                                        and bar.timestamp
                                        > detector.intended_exit_timestamp
                                    ):
                                        detector.execution_bar_missing = True
                                        position.swing_high_execution_bar_missing = True
                                    opening_decision = self.position_manager.evaluate_open(
                                        position, bar
                                    )
                                    if opening_decision.action is not PositionAction.HOLD:
                                        execute_position_decision(
                                            symbol, session, opening_decision, bar
                                        )
                                        continue
                                    position.actual_exit_timestamp = bar.timestamp
                                    position.intended_exit_timestamp = (
                                        detector.intended_exit_timestamp
                                    )
                                    execute_position_decision(
                                        symbol,
                                        session,
                                        PositionDecision(
                                            action=PositionAction.SELL,
                                            reason=ExitReason.CONFIRMED_SWING_HIGH,
                                            reference_price=float(bar.open),
                                        ),
                                        bar,
                                    )
                                    continue
                            if opening_survivor_gate:
                                self._prepare_opening_survivor_gate(
                                    position,
                                    bar,
                                    session,
                                    bars_by_timestamp,
                                )
                                if self._opening_gate_exit_due(position, bar):
                                    position.opening_gate_exit_timestamp = bar.timestamp
                                    position.opening_gate_exit_reference_price = float(bar.open)
                                    decision = PositionDecision(
                                        action=PositionAction.SELL,
                                        reason=ExitReason.OPENING_BAR_FAIL,
                                        reference_price=float(bar.open),
                                    )
                                    execute_position_decision(symbol, session, decision, bar)
                                    continue
                            while True:
                                decision = self.position_manager.evaluate_intrabar(position, bar)
                                if decision.action is PositionAction.HOLD:
                                    break
                                if (
                                    opening_survivor_gate
                                    and self._is_position_opening_bar(position, bar)
                                    and decision.action is PositionAction.SELL
                                ):
                                    position.opening_gate_position_alive_at_evaluation = False
                                    position.baseline_first_bar_trail_exit_occurred = (
                                        decision.reason is ExitReason.ATR_TRAILING_STOP
                                    )
                                if execute_position_decision(symbol, session, decision, bar):
                                    break
                            if symbol in positions:
                                history = intraday_histories[symbol]
                                history.append(bar)
                                next_atr = (
                                    self._atr_from_bars(
                                        history,
                                        self.position_management.atr_trailing_stop.atr_period,
                                    )
                                    if intraday_monitoring
                                    else position.current_atr
                                )
                                self.position_manager.update_after_bar(
                                    position, bar, next_atr=next_atr
                                )
                                if first_hour_pullback:
                                    detector = f4_exit_detectors[position.position_id]
                                    detector.observe_completed_bar(bar)
                                    self._apply_f4_exit_diagnostics(position, detector)
                                if opening_survivor_gate:
                                    self._complete_opening_survivor_gate(position, bar)
                        elif not intraday_histories[symbol] or (
                            intraday_histories[symbol][-1].timestamp < bar.timestamp
                        ):
                            intraday_histories[symbol].append(bar)
                    session_peak_market_value = max(
                        session_peak_market_value,
                        sum(item.quantity * item.last_price for item in positions.values()),
                    )
                if first_hour_pullback:
                    for symbol in sorted(list(positions)):
                        position = positions[symbol]
                        if position.entry_date != session:
                            continue
                        bar = last_intraday_bars.get(symbol)
                        if bar is None:
                            skipped["missing_session_close_bar"] += 1
                            continue
                        detector = f4_exit_detectors[position.position_id]
                        if detector.exit_due(regular_close):
                            detector.execution_bar_missing = True
                            position.swing_high_execution_bar_missing = True
                        position.actual_exit_timestamp = bar.timestamp
                        execute_position_decision(
                            symbol,
                            session,
                            PositionDecision(
                                action=PositionAction.SELL,
                                reason=ExitReason.SESSION_CLOSE,
                                reference_price=float(bar.close),
                            ),
                            bar,
                        )
                pending = []
            else:
                # 1. Existing positions can gap through levels fixed before this session.
                for symbol in list(positions):
                    bar = bars.get(symbol)
                    if bar is None:
                        continue
                    while symbol in positions:
                        decision = self.position_manager.evaluate_open(positions[symbol], bar)
                        if decision.action is PositionAction.HOLD:
                            break
                        if execute_position_decision(symbol, session, decision, bar):
                            break

                # 2. Signals from the prior close execute only now, at this session's open.
                for order in sorted(
                    pending, key=lambda item: (-item.variant_score, item.record.symbol)
                ):
                    if order.record.symbol in positions:
                        skipped["duplicate_position"] += 1
                        self._observe_execution(
                            order, session, executed=False, reason="duplicate_position"
                        )
                        continue
                    bar = bars.get(order.record.symbol)
                    if bar is None:
                        skipped["missing_next_session_bar"] += 1
                        self._observe_execution(
                            order,
                            session,
                            executed=False,
                            reason="missing_next_session_bar",
                        )
                        continue
                    position, cash, reason = self._open_position(
                        order, bar, bars, positions, cash
                    )
                    if position is None:
                        skipped[reason or "entry_rejected"] += 1
                        self._observe_execution(
                            order,
                            session,
                            executed=False,
                            reason=reason or "entry_rejected",
                        )
                        continue
                    positions[position.symbol] = position
                    execution_legs[position.position_id] = []
                    reentry_trackers.pop(position.symbol, None)
                    self._observe_execution(order, session, executed=True)
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
            if report is not None and self.audit_observer is not None:
                self.audit_observer.observe_screen(report, variant, self.config)
            records = {record.symbol: record for record in report.records} if report else {}
            best_symbol, best_score = self._best_candidate(report, variant, positions)
            for symbol in list(positions):
                position = positions[symbol]
                bar = (
                    last_intraday_bars.get(symbol)
                    if intraday_monitoring
                    else bars.get(symbol)
                )
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
                    self._complete_candidate_event(position, completed)
                    f4_exit_detectors.pop(position.position_id, None)
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
                    completed = finalize_position(
                        position, execution_legs[position.position_id]
                    )
                    completed_positions.append(completed)
                    self._complete_candidate_event(position, completed)
                    f4_exit_detectors.pop(position.position_id, None)
                    del positions[symbol]
            else:
                # 6. Exited symbols re-enter only by winning this normal PIT ranking.
                assert report is not None
                pending = self._entry_orders(
                    report,
                    variant,
                    positions,
                    skipped,
                    execution_session=sessions[index + 1],
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
            strict_coverage_sensitivity=self.strict_coverage_sensitivity,
            research_diagnostics=self._research_diagnostics(
                trades, diagnosed_positions, skipped
            ),
        )

    def _entry_orders(
        self,
        report: ScreenReport,
        variant: StrategyVariant,
        positions: dict[str, PositionState],
        skipped: Counter[str],
        *,
        execution_session: date | None = None,
        closed_dates: dict[str, date] | None = None,
        reentry_trackers: dict[str, _ReentryTracker] | None = None,
    ) -> list[_PendingEntry]:
        trackers = reentry_trackers or {}
        self._update_reentry_trackers(report, trackers)
        capacity = self.config.portfolio.max_positions - len(positions)
        if capacity <= 0:
            if self.audit_observer is not None:
                for record in report.records:
                    evaluation = evaluate_variant_entry(record, variant, self.config)
                    if not evaluation.eligible:
                        continue
                    reason = (
                        "already_holding_symbol"
                        if record.symbol in positions
                        else "max_positions_reached"
                    )
                    self.audit_observer.observe_portfolio_decision(
                        report.as_of, record.symbol, "blocked", reason
                    )
            return []
        candidates: list[_PendingEntry] = []
        for record in report.records:
            evaluation = evaluate_variant_entry(record, variant, self.config)
            if evaluation.score is None or evaluation.first_failure is not None:
                skipped[evaluation.first_failure or "entry_filter"] += 1
                continue
            candidates.append(
                _PendingEntry(
                    record=record,
                    signal_date=report.as_of,
                    variant_score=evaluation.score,
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
        candidate_count = len(candidates)
        for rank, candidate in enumerate(candidates, start=1):
            candidate.daily_candidate_rank = rank
            candidate.daily_candidate_count = candidate_count
            if self.current_preset in RESEARCH_CANDIDATE_EVENT_PRESETS:
                candidate.candidate_event_index = len(self._candidate_events)
                self._candidate_events.append(
                    self._candidate_event(candidate, execution_session)
                )

        if self.current_preset is PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY:
            candidates = candidates[:1]

        occupied = set(positions)
        filtered: list[_PendingEntry] = []
        for candidate in candidates:
            symbol = candidate.record.symbol
            if symbol in occupied:
                self._update_candidate_event(candidate, selection_outcome="already_holding")
                if self.audit_observer is not None:
                    self.audit_observer.observe_portfolio_decision(
                        report.as_of, symbol, "blocked", "already_holding_symbol"
                    )
                continue
            if self.current_preset in THESIS_RECOVERY_PRESETS:
                previous = candidate.previous_position
                failed, recovered, blocked, score_delta = (
                    self._thesis_recovery_decision(previous, candidate.variant_score)
                )
                self._update_candidate_event(
                    candidate,
                    previous_trade_failed=failed,
                    thesis_recovered=recovered,
                    thesis_recovery_blocked=blocked,
                    score_delta=score_delta,
                )
                if failed:
                    self._research_counters["failed_trade_reentry_candidates"] += 1
                if blocked:
                    skipped["thesis_not_recovered"] += 1
                    self._research_counters["thesis_recovery_blocks"] += 1
                    self._update_candidate_event(
                        candidate, selection_outcome="thesis_recovery_blocked"
                    )
                    if self.audit_observer is not None:
                        self.audit_observer.observe_portfolio_decision(
                            report.as_of, symbol, "blocked", "thesis_not_recovered"
                        )
                    continue
                if failed and recovered:
                    self._research_counters["recovered_reentries"] += 1
            elif self.current_preset in GROSS_LOSS_COOLDOWN_PRESETS:
                previous = candidate.previous_position
                blocked = self._gross_loss_cooldown_blocked(previous, execution_session)
                candidate.cooldown_applied = blocked
                self._update_candidate_event(
                    candidate,
                    cooldown_active=blocked,
                    cooldown_blocked=blocked,
                    cooldown_reason=(
                        "negative_gross_return_next_xnys_session" if blocked else None
                    ),
                )
                if blocked:
                    candidate.cooldown_blocked = True
                    candidate.cooldown_reason = "negative_gross_return_next_xnys_session"
                    skipped["gross_loss_session_cooldown"] += 1
                    self._research_counters["cooldown_blocks"] += 1
                    if previous is not None and (
                        previous.entry_timestamp is not None
                        and previous.exit_timestamp == previous.entry_timestamp
                    ):
                        self._research_counters["blocks_after_entry_bar_losses"] += 1
                    else:
                        self._research_counters["blocks_after_later_losses"] += 1
                    self._update_candidate_event(candidate, selection_outcome="cooldown_blocked")
                    if self.audit_observer is not None:
                        self.audit_observer.observe_portfolio_decision(
                            report.as_of, symbol, "blocked", "gross_loss_session_cooldown"
                        )
                    continue
            elif self.current_preset in NEGATIVE_COOLDOWN_PRESETS:
                previous = candidate.previous_position
                candidate.cooldown_applied = bool(
                    previous is not None and previous.position_return < 0
                )
                if self._negative_cooldown_blocked(previous, execution_session):
                    candidate.cooldown_blocked = True
                    candidate.cooldown_reason = "negative_return_next_xnys_session"
                    skipped["reentry_cooldown"] += 1
                    if self.audit_observer is not None:
                        self.audit_observer.observe_portfolio_decision(
                            report.as_of, symbol, "blocked", "reentry_rule"
                        )
                    continue
            elif not self._reentry_allowed(symbol, report.as_of, closed_dates or {}):
                self._update_candidate_event(candidate, selection_outcome="reentry_blocked")
                skipped["reentry_cooldown"] += 1
                if self.audit_observer is not None:
                    self.audit_observer.observe_portfolio_decision(
                        report.as_of, symbol, "blocked", "reentry_rule"
                    )
                continue
            filtered.append(candidate)

        sector_counts = Counter(position.sector for position in positions.values())
        orders: list[_PendingEntry] = []
        for candidate in filtered:
            sector = (candidate.record.sic or "unknown")[:2]
            if sector_counts[sector] >= self.config.portfolio.max_sector_positions:
                self._update_candidate_event(candidate, selection_outcome="sector_limit")
                skipped["max_sector_positions"] += 1
                if self.audit_observer is not None:
                    self.audit_observer.observe_portfolio_decision(
                        report.as_of,
                        candidate.record.symbol,
                        "blocked",
                        "sector_limit",
                    )
                continue
            if (
                self.current_preset
                is not PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING
                and len(orders) >= capacity
            ):
                self._update_candidate_event(candidate, selection_outcome="max_positions_reached")
                if self.audit_observer is not None:
                    self.audit_observer.observe_portfolio_decision(
                        report.as_of,
                        candidate.record.symbol,
                        "blocked",
                        "max_positions_reached",
                    )
                continue
            orders.append(candidate)
            self._update_candidate_event(
                candidate,
                selection_outcome="order_created",
                entry_opportunity_required=True,
            )
            if (
                self.current_preset
                is not PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING
            ):
                sector_counts[sector] += 1
            if self.audit_observer is not None:
                self.audit_observer.observe_portfolio_decision(
                    report.as_of, candidate.record.symbol, "order_created"
                )
        return orders

    def _first_hour_pullback_pending_entries(
        self,
        pending: list[_PendingEntry],
        intraday_by_symbol: dict[str, list[DailyBar]],
        intraday_histories: dict[str, list[DailyBar]],
        session: date,
        skipped: Counter[str],
    ) -> dict[datetime, list[_PendingEntry]]:
        scheduled: dict[datetime, list[_PendingEntry]] = {}
        for order in pending:
            symbol = order.record.symbol
            self._research_counters["f4_c_candidates"] += 1
            order.intraday_session_status = self.intraday_session_statuses.get(
                (symbol, session), "NATIVE_SESSION"
            )
            plan = plan_first_hour_pullback(
                session,
                intraday_by_symbol.get(symbol, ()),
                intraday_histories.get(symbol, ()),
            )
            order.research_metadata.update(plan.diagnostics)
            order.f4_confirmation_bar = plan.confirmation_bar
            if plan.diagnostics.get("first_hour_complete") is True:
                self._research_counters["f4_complete_first_hours"] += 1
            if plan.diagnostics.get("opening_above_ema") is True:
                self._research_counters["f4_opening_ema_passes"] += 1
            self._research_counters["f4_pullback_candidates"] += int(
                plan.diagnostics.get("pullback_candidate_count", 0)
            )
            if plan.diagnostics.get("pullback_confirmed") is True:
                self._research_counters["f4_confirmed_pullbacks"] += 1
            self._update_candidate_event(
                order,
                **plan.diagnostics,
                intraday_session_status=order.intraday_session_status,
                entry_opportunity_required=plan.diagnostics.get(
                    "pullback_confirmed", False
                ),
                entry_bar_expected_timestamp=plan.diagnostics.get(
                    "intended_entry_timestamp"
                ),
                entry_bar_present=plan.executable,
            )
            if not plan.executable:
                reason = plan.failure_reason or "f4_entry_rejected"
                skipped[reason] += 1
                self._observe_execution(
                    order, session, executed=False, reason=reason
                )
                continue
            assert plan.entry_timestamp is not None
            order.intended_entry_timestamp = plan.entry_timestamp
            order.execution_bar_present = True
            scheduled.setdefault(plan.entry_timestamp, []).append(order)
        return scheduled

    def _confirmed_pending_entries(
        self,
        pending: list[_PendingEntry],
        intraday_by_symbol: dict[str, list[DailyBar]],
        session: date,
        skipped: Counter[str],
    ) -> dict[datetime, list[_PendingEntry]]:
        opening_timestamp, _ = regular_session_bounds(session)
        execution_timestamp = opening_timestamp + BarTimeframe.MINUTES_15.duration
        eligible: list[_PendingEntry] = []
        for order in pending:
            self._research_counters["confirmation_attempts"] += 1
            symbol = order.record.symbol
            status = self.intraday_session_statuses.get(
                (symbol, session), "NATIVE_SESSION"
            )
            order.intraday_session_status = status
            order.confirmation_bar_expected_timestamp = opening_timestamp
            order.intended_entry_timestamp = execution_timestamp
            if self.strict_coverage_sensitivity and status != "COMPLETE":
                order.confirmation_failure_reason = "strict_incomplete_session"
                skipped["strict_incomplete_session"] += 1
                self._research_counters["strict_coverage_exclusions"] += 1
                continue
            timestamp_bars = {
                bar.timestamp: bar for bar in intraday_by_symbol.get(symbol, ())
            }
            opening_bar = timestamp_bars.get(opening_timestamp)
            execution_bar = timestamp_bars.get(execution_timestamp)
            order.execution_bar_present = execution_bar is not None
            if opening_bar is None:
                order.confirmation_failure_reason = "missing_confirmation_bar"
                skipped["missing_confirmation_bar"] += 1
                self._research_counters["missing_confirmation_bar"] += 1
                continue
            order.confirmation_bar_present = True
            order.confirmation_bar_timestamp = opening_bar.timestamp
            order.confirmation_open = float(opening_bar.open)
            order.confirmation_high = float(opening_bar.high)
            order.confirmation_low = float(opening_bar.low)
            order.confirmation_close = float(opening_bar.close)
            order.confirmation_volume = opening_bar.volume
            order.confirmation_vwap = (
                float(opening_bar.vwap) if opening_bar.vwap is not None else None
            )
            order.confirmation_passed = (
                order.confirmation_close > order.confirmation_open
            )
            if not order.confirmation_passed:
                order.confirmation_failure_reason = "opening_bar_not_green"
                skipped["confirmation_rejected"] += 1
                self._research_counters["confirmation_rejections"] += 1
                continue
            self._research_counters["confirmation_passes"] += 1
            if execution_bar is None:
                order.confirmation_failure_reason = "missing_execution_bar"
                skipped["missing_execution_bar"] += 1
                self._research_counters["missing_execution_bar"] += 1
                continue
            eligible.append(order)

        eligible.sort(key=lambda item: (-item.variant_score, item.record.symbol))
        selected = eligible[:1]
        if selected:
            self._research_counters["confirmation_entries_selected"] += 1
        if len(eligible) > 1:
            self._research_counters["confirmed_not_selected"] += len(eligible) - 1
        selected_symbols = {order.record.symbol for order in selected}
        for order in pending:
            failure = order.confirmation_failure_reason
            if (
                order.confirmation_passed
                and order.execution_bar_present
                and order.record.symbol not in selected_symbols
            ):
                failure = "confirmed_not_selected"
            self._confirmation_events.append(
                {
                    "symbol": order.record.symbol,
                    "signal_date": order.signal_date.isoformat(),
                    "execution_session": session.isoformat(),
                    "daily_candidate_rank": order.daily_candidate_rank,
                    "daily_candidate_count": order.daily_candidate_count,
                    "daily_candidate_score": order.variant_score,
                    "daily_candidate_variant": order.variant.value,
                    "confirmation_bar_expected_timestamp": (
                        order.confirmation_bar_expected_timestamp.isoformat()
                        if order.confirmation_bar_expected_timestamp
                        else None
                    ),
                    "confirmation_bar_timestamp": (
                        order.confirmation_bar_timestamp.isoformat()
                        if order.confirmation_bar_timestamp
                        else None
                    ),
                    "confirmation_bar_present": order.confirmation_bar_present,
                    "confirmation_open": order.confirmation_open,
                    "confirmation_high": order.confirmation_high,
                    "confirmation_low": order.confirmation_low,
                    "confirmation_close": order.confirmation_close,
                    "confirmation_volume": order.confirmation_volume,
                    "confirmation_vwap": order.confirmation_vwap,
                    "confirmation_passed": order.confirmation_passed,
                    "confirmation_failure_reason": failure,
                    "intended_entry_timestamp": execution_timestamp.isoformat(),
                    "execution_bar_present": order.execution_bar_present,
                    "intraday_session_status": order.intraday_session_status,
                    "entry_selected": order.record.symbol in selected_symbols,
                    "strict_coverage_sensitivity": self.strict_coverage_sensitivity,
                }
            )
        return {execution_timestamp: selected} if selected else {}

    @staticmethod
    def _negative_cooldown_blocked(
        previous: BacktestPosition | None,
        execution_session: date | None,
    ) -> bool:
        if (
            previous is None
            or previous.position_return >= 0
            or execution_session is None
            or execution_session <= previous.exit_date
        ):
            return False
        sessions = trading_sessions_between(
            previous.exit_date + timedelta(days=1), execution_session
        )
        return bool(sessions and sessions[0] == execution_session)

    @staticmethod
    def _gross_loss_cooldown_blocked(
        previous: BacktestPosition | None,
        execution_session: date | None,
    ) -> bool:
        if previous is None or execution_session is None:
            return False
        gross_return = previous.gross_market_return
        if gross_return is None:
            gross_return = (
                previous.exit_reference_price / previous.entry_reference_price - 1
            )
        if gross_return >= 0 or execution_session <= previous.exit_date:
            return False
        next_session = BacktestEngine._next_xnys_session(previous.exit_date)
        return execution_session == next_session

    @staticmethod
    def _thesis_recovery_decision(
        previous: BacktestPosition | None,
        current_c_score: float,
    ) -> tuple[bool, bool | None, bool, float | None]:
        if previous is None:
            return False, None, False, None
        gross_return = previous.gross_market_return
        if gross_return is None:
            gross_return = previous.exit_reference_price / previous.entry_reference_price - 1
        if gross_return >= 0:
            return False, None, False, (
                current_c_score - previous.entry_score
                if previous.entry_score is not None
                else None
            )
        previous_score = previous.entry_score
        if previous_score is None:
            return True, False, True, None
        score_delta = current_c_score - previous_score
        recovered = current_c_score > previous_score
        return True, recovered, not recovered, score_delta

    @staticmethod
    def _next_xnys_session(session: date) -> date | None:
        sessions = trading_sessions_between(
            session + timedelta(days=1), session + timedelta(days=10)
        )
        return sessions[0] if sessions else None

    def _candidate_event(
        self,
        candidate: _PendingEntry,
        execution_session: date | None,
    ) -> dict:
        previous = candidate.previous_position
        previous_gross_return = None
        if previous is not None:
            previous_gross_return = previous.gross_market_return
            if previous_gross_return is None:
                previous_gross_return = (
                    previous.exit_reference_price / previous.entry_reference_price - 1
                )
        next_session = (
            self._next_xnys_session(previous.exit_date) if previous is not None else None
        )
        return {
            "symbol": candidate.record.symbol,
            "signal_session": candidate.signal_date.isoformat(),
            "execution_session": (
                execution_session.isoformat() if execution_session is not None else None
            ),
            "candidate_rank": candidate.daily_candidate_rank,
            "candidate_score": candidate.variant_score,
            "current_candidate_rank": candidate.daily_candidate_rank,
            "current_C_score": candidate.variant_score,
            "previous_same_symbol_position_id": (
                previous.position_id if previous is not None else None
            ),
            "previous_entry_C_score": (
                previous.entry_score if previous is not None else None
            ),
            "previous_exit_session": (
                previous.exit_date.isoformat() if previous is not None else None
            ),
            "previous_gross_market_return": previous_gross_return,
            "score_delta": (
                candidate.variant_score - previous.entry_score
                if previous is not None and previous.entry_score is not None
                else None
            ),
            "previous_trade_failed": (
                previous_gross_return < 0
                if previous_gross_return is not None
                else False
            ),
            "thesis_recovered": None,
            "thesis_recovery_blocked": False,
            "previous_same_symbol_exit_session": (
                previous.exit_date.isoformat() if previous is not None else None
            ),
            "previous_same_symbol_gross_return": (
                previous_gross_return
            ),
            "previous_same_symbol_net_return": (
                previous.position_return if previous is not None else None
            ),
            "next_xnys_session_after_previous_exit": (
                next_session.isoformat() if next_session is not None else None
            ),
            "cooldown_active": False,
            "cooldown_blocked": False,
            "cooldown_reason": None,
            "entry_opportunity_required": False,
            "entry_bar_expected_timestamp": None,
            "entry_bar_present": None,
            "actual_entry_timestamp": None,
            "executed": False,
            "warmup_sufficient": None,
            "warmup_available_native_bars": None,
            "execution_failure_reason": None,
            "selection_outcome": "eligible",
        }

    def _update_candidate_event(self, order: _PendingEntry, **updates) -> None:
        if order.candidate_event_index is None:
            return
        self._candidate_events[order.candidate_event_index].update(updates)

    @staticmethod
    def _metadata_datetime(metadata: dict, key: str) -> datetime | None:
        value = metadata.get(key)
        if value is None or isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    @staticmethod
    def _apply_f4_exit_diagnostics(
        position: PositionState,
        detector: SwingHighDetector,
    ) -> None:
        values = detector.diagnostics
        position.swing_high_candidate_timestamp = BacktestEngine._metadata_datetime(
            values, "swing_high_candidate_timestamp"
        )
        position.swing_high_candidate_high = values.get("swing_high_candidate_high")
        position.swing_high_confirmation_timestamp = (
            BacktestEngine._metadata_datetime(
                values, "swing_high_confirmation_timestamp"
            )
        )
        position.swing_high_confirmed = bool(
            values.get("swing_high_confirmed", False)
        )
        position.intended_exit_timestamp = BacktestEngine._metadata_datetime(
            values, "intended_exit_timestamp"
        )

    def _complete_candidate_event(
        self,
        state: PositionState,
        position: BacktestPosition,
    ) -> None:
        if state.candidate_event_index is None:
            return
        self._candidate_events[state.candidate_event_index].update(
            {
                "executed": True,
                "actual_entry_timestamp": (
                    position.entry_timestamp.isoformat()
                    if position.entry_timestamp is not None
                    else None
                ),
                "exit_reason": position.exit_reason,
                "gross_market_return": position.gross_market_return,
                "net_return": position.position_return,
                "MFE": position.maximum_favorable_excursion,
                "MAE": position.maximum_adverse_excursion,
                "intended_exit_timestamp": (
                    state.intended_exit_timestamp.isoformat()
                    if state.intended_exit_timestamp is not None
                    else None
                ),
                "actual_exit_timestamp": (
                    position.exit_timestamp.isoformat()
                    if position.exit_timestamp is not None
                    else None
                ),
                "swing_high_execution_bar_missing": (
                    state.swing_high_execution_bar_missing
                ),
            }
        )

    def _observe_execution(
        self,
        order: _PendingEntry,
        execution_date: date,
        *,
        executed: bool,
        reason: str | None = None,
    ) -> None:
        if self.audit_observer is not None:
            self.audit_observer.observe_execution(
                order.signal_date,
                execution_date,
                order.record.symbol,
                executed,
                reason,
            )

    @staticmethod
    def _is_position_opening_bar(position: PositionState, bar: DailyBar) -> bool:
        return (
            position.opening_gate_expected_timestamp is not None
            and bar.timestamp == position.opening_gate_expected_timestamp
        )

    def _prepare_opening_survivor_gate(
        self,
        position: PositionState,
        bar: DailyBar,
        session: date,
        bars_by_timestamp: dict[datetime, dict[str, DailyBar]],
    ) -> None:
        if position.entry_date != session or not self._is_position_opening_bar(position, bar):
            return
        if position.opening_gate_actual_timestamp is not None:
            return
        position.opening_gate_actual_timestamp = bar.timestamp
        position.opening_gate_open = float(bar.open)
        position.opening_gate_high = float(bar.high)
        position.opening_gate_low = float(bar.low)
        position.opening_gate_close = float(bar.close)
        position.opening_gate_volume = bar.volume
        position.opening_gate_vwap = float(bar.vwap) if bar.vwap is not None else None
        execution_timestamp = bar.timestamp + BarTimeframe.MINUTES_15.duration
        position.opening_gate_executable = (
            position.symbol in bars_by_timestamp.get(execution_timestamp, {})
        )

    def _complete_opening_survivor_gate(
        self,
        position: PositionState,
        bar: DailyBar,
    ) -> None:
        if not self._is_position_opening_bar(position, bar):
            return
        position.opening_gate_position_alive_at_evaluation = True
        position.opening_gate_evaluated = True
        position.opening_gate_evaluable = True
        position.opening_gate_green = float(bar.close) > float(bar.open)
        position.opening_gate_passed = position.opening_gate_green
        self._research_counters["opening_gate_evaluations"] += 1
        if position.opening_gate_green:
            self._research_counters["opening_gate_green_survivors"] += 1
            return
        self._research_counters["opening_gate_non_green_survivors"] += 1
        position.opening_gate_triggered = True
        if position.opening_gate_executable is False:
            position.opening_gate_failure_reason = "missing_execution_bar"
            self._research_counters["opening_gate_missing_execution_bars"] += 1

    @staticmethod
    def _opening_gate_exit_due(position: PositionState, bar: DailyBar) -> bool:
        expected = position.opening_gate_expected_timestamp
        return bool(
            expected is not None
            and position.opening_gate_triggered
            and position.opening_gate_executable is True
            and bar.timestamp == expected + BarTimeframe.MINUTES_15.duration
        )

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
        warmup_history: list[DailyBar] | None = None,
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
        f4_stop_price = None
        if self.current_preset in FIRST_HOUR_PULLBACK_PRESETS:
            f4_stop_price = reference * (1 - F4_STOP_DISTANCE_PCT)
            stop_distance = fill - f4_stop_price
        elif self.position_management.stop_loss.enabled and fixed_stop_percent is not None:
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
        warmup = (
            warmup_coverage_diagnostics(
                warmup_history,
                bar.timestamp,
                required_bars=self.config.intraday.warmup_bars,
            )
            if warmup_history is not None
            else {}
        )
        opening_gate_expected = None
        opening_gate_failure = None
        opening_gate_evaluable = None
        if self.current_preset in OPENING_SURVIVOR_GATE_PRESETS:
            opening_gate_expected, _ = regular_session_bounds(bar.timestamp.date())
            if bar.timestamp != opening_gate_expected:
                opening_gate_failure = "missing_opening_bar"
                opening_gate_evaluable = False
                self._research_counters["opening_gate_missing_opening_bars"] += 1
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
                f4_stop_price
                if f4_stop_price is not None
                else (
                    fill - stop_distance
                    if self.position_management.stop_loss.enabled
                    else None
                )
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
            initial_risk_per_share_R=(
                stop_distance if self.position_management.profit_lock.enabled else None
            ),
            trail_guard_enabled=(
                self.position_management.atr_trailing_stop.enabled
                and (
                    self.position_management.atr_trailing_stop
                    .minimum_completed_bars_before_activation
                    > 0
                )
            ),
            daily_candidate_rank=order.daily_candidate_rank,
            daily_candidate_count=order.daily_candidate_count,
            daily_candidate_score=order.variant_score,
            daily_candidate_variant=order.variant,
            confirmation_required=self.current_preset in CONFIRMED_ENTRY_PRESETS,
            confirmation_bar_expected_timestamp=(
                order.confirmation_bar_expected_timestamp
            ),
            confirmation_bar_timestamp=order.confirmation_bar_timestamp,
            confirmation_bar_present=order.confirmation_bar_present,
            confirmation_open=order.confirmation_open,
            confirmation_high=order.confirmation_high,
            confirmation_low=order.confirmation_low,
            confirmation_close=order.confirmation_close,
            confirmation_volume=order.confirmation_volume,
            confirmation_vwap=order.confirmation_vwap,
            confirmation_passed=order.confirmation_passed,
            confirmation_failure_reason=order.confirmation_failure_reason,
            intended_entry_timestamp=order.intended_entry_timestamp or bar.timestamp,
            actual_entry_timestamp=bar.timestamp,
            entry_delayed_from_open=self.current_preset
            in {*CONFIRMED_ENTRY_PRESETS, *FIRST_HOUR_PULLBACK_PRESETS},
            execution_bar_present=True,
            cooldown_applied=order.cooldown_applied,
            cooldown_blocked=order.cooldown_blocked,
            cooldown_reason=order.cooldown_reason,
            previous_position_net_return=(
                order.previous_position.position_return if order.previous_position else None
            ),
            intraday_session_status=order.intraday_session_status,
            opening_bar_complete=(
                order.confirmation_bar_present
                if self.current_preset in CONFIRMED_ENTRY_PRESETS
                else None
            ),
            execution_bar_complete=(
                order.execution_bar_present
                if self.current_preset in CONFIRMED_ENTRY_PRESETS
                else None
            ),
            warmup_required_bars=warmup.get("warmup_required_bars"),
            warmup_available_native_bars=warmup.get(
                "warmup_available_native_bars"
            ),
            warmup_sufficient=warmup.get("warmup_sufficient"),
            earliest_warmup_timestamp=warmup.get("earliest_warmup_timestamp"),
            latest_pre_entry_warmup_timestamp=warmup.get(
                "latest_pre_entry_warmup_timestamp"
            ),
            warmup_expected_timestamp_gap_count=warmup.get(
                "warmup_expected_timestamp_gap_count"
            ),
            opening_gate_expected_timestamp=opening_gate_expected,
            opening_gate_evaluable=opening_gate_evaluable,
            opening_gate_failure_reason=opening_gate_failure,
            candidate_event_index=order.candidate_event_index,
            opening_bar_timestamp=self._metadata_datetime(
                order.research_metadata, "opening_bar_timestamp"
            ),
            opening_ema20=order.research_metadata.get("opening_ema20"),
            opening_above_ema=order.research_metadata.get("opening_above_ema"),
            first_hour_complete=order.research_metadata.get("first_hour_complete"),
            first_hour_open=order.research_metadata.get("first_hour_open"),
            first_hour_high=order.research_metadata.get("first_hour_high"),
            first_hour_low=order.research_metadata.get("first_hour_low"),
            first_hour_close=order.research_metadata.get("first_hour_close"),
            ema20_at_1030=order.research_metadata.get("ema20_at_1030"),
            pullback_candidate_timestamp=self._metadata_datetime(
                order.research_metadata, "pullback_candidate_timestamp"
            ),
            pullback_candidate_low=order.research_metadata.get(
                "pullback_candidate_low"
            ),
            pullback_confirmation_timestamp=self._metadata_datetime(
                order.research_metadata, "pullback_confirmation_timestamp"
            ),
            pullback_confirmation_close=order.research_metadata.get(
                "pullback_confirmation_close"
            ),
            pullback_confirmed=bool(
                order.research_metadata.get("pullback_confirmed", False)
            ),
            initial_stop_price=f4_stop_price,
            stop_distance_pct=(
                F4_STOP_DISTANCE_PCT
                if self.current_preset in FIRST_HOUR_PULLBACK_PRESETS
                else None
            ),
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
        if bar is not None and reference == float(bar.open):
            if decision.reason is ExitReason.TAKE_PROFIT and position.target_price is not None:
                position.gap_affected_trade = float(bar.open) > position.target_price
            elif (
                decision.reason is ExitReason.PARTIAL_TAKE_PROFIT
                and decision.partial_level is not None
            ):
                level = self.position_management.partial_take_profit.levels[
                    decision.partial_level
                ]
                trigger = position.entry_price * (1 + level.profit)
                position.gap_affected_trade = float(bar.open) > trigger
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
            ExitReason.PROFIT_LOCK,
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
        elif bar is not None and decision.reason in {
            ExitReason.OPENING_BAR_FAIL,
            ExitReason.CONFIRMED_SWING_HIGH,
        }:
            position.highest_price_since_entry = max(
                position.highest_price_since_entry, float(bar.open), reference
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
            daily_candidate_rank=position.daily_candidate_rank,
            daily_candidate_count=position.daily_candidate_count,
            daily_candidate_score=position.daily_candidate_score,
            daily_candidate_variant=position.daily_candidate_variant,
            confirmation_required=position.confirmation_required,
            confirmation_bar_expected_timestamp=(
                position.confirmation_bar_expected_timestamp
            ),
            confirmation_bar_timestamp=position.confirmation_bar_timestamp,
            confirmation_bar_present=position.confirmation_bar_present,
            confirmation_open=position.confirmation_open,
            confirmation_high=position.confirmation_high,
            confirmation_low=position.confirmation_low,
            confirmation_close=position.confirmation_close,
            confirmation_volume=position.confirmation_volume,
            confirmation_vwap=position.confirmation_vwap,
            confirmation_passed=position.confirmation_passed,
            confirmation_failure_reason=position.confirmation_failure_reason,
            intended_entry_timestamp=position.intended_entry_timestamp,
            actual_entry_timestamp=position.actual_entry_timestamp,
            entry_delayed_from_open=position.entry_delayed_from_open,
            execution_bar_present=position.execution_bar_present,
            trail_guard_enabled=(
                self.position_management.atr_trailing_stop.enabled
                and (
                    self.position_management.atr_trailing_stop
                    .minimum_completed_bars_before_activation
                    > 0
                )
            ),
            completed_bars_before_trail_arm=(
                position.completed_bars_before_trail_arm
            ),
            trail_armed_timestamp=position.trail_armed_timestamp,
            trail_armed_reference_price=position.trail_armed_reference_price,
            atr_at_trail_activation=position.atr_at_trail_activation,
            mfe_at_trail_activation=position.mfe_at_trail_activation,
            initial_risk_per_share_R=position.initial_risk_per_share_R,
            maximum_mfe_in_R=(
                (position.highest_price_since_entry - position.entry_price)
                / position.initial_risk_per_share_R
                if position.initial_risk_per_share_R
                else None
            ),
            profit_lock_state=(
                position.profit_lock_state.value
                if self.position_management.profit_lock.enabled
                else None
            ),
            profit_lock_activation_timestamp=(
                position.profit_lock_activation_timestamp
            ),
            break_even_lock_timestamp=position.break_even_lock_timestamp,
            one_r_lock_timestamp=position.one_r_lock_timestamp,
            active_profit_lock_stop=position.profit_lock_stop_price,
            cooldown_applied=position.cooldown_applied,
            cooldown_blocked=position.cooldown_blocked,
            cooldown_reason=position.cooldown_reason,
            previous_position_net_return=position.previous_position_net_return,
            intraday_session_status=position.intraday_session_status,
            opening_bar_complete=position.opening_bar_complete,
            execution_bar_complete=position.execution_bar_complete,
            gap_affected_trade=position.gap_affected_trade,
            warmup_required_bars=position.warmup_required_bars,
            warmup_available_native_bars=position.warmup_available_native_bars,
            warmup_sufficient=position.warmup_sufficient,
            earliest_warmup_timestamp=position.earliest_warmup_timestamp,
            latest_pre_entry_warmup_timestamp=(
                position.latest_pre_entry_warmup_timestamp
            ),
            warmup_expected_timestamp_gap_count=(
                position.warmup_expected_timestamp_gap_count
            ),
            opening_gate_expected_timestamp=position.opening_gate_expected_timestamp,
            opening_gate_actual_timestamp=position.opening_gate_actual_timestamp,
            opening_gate_open=position.opening_gate_open,
            opening_gate_high=position.opening_gate_high,
            opening_gate_low=position.opening_gate_low,
            opening_gate_close=position.opening_gate_close,
            opening_gate_volume=position.opening_gate_volume,
            opening_gate_vwap=position.opening_gate_vwap,
            opening_gate_green=position.opening_gate_green,
            opening_gate_position_alive_at_evaluation=(
                position.opening_gate_position_alive_at_evaluation
            ),
            baseline_first_bar_trail_exit_occurred=(
                position.baseline_first_bar_trail_exit_occurred
            ),
            opening_gate_evaluated=position.opening_gate_evaluated,
            opening_gate_evaluable=position.opening_gate_evaluable,
            opening_gate_passed=position.opening_gate_passed,
            opening_gate_triggered=position.opening_gate_triggered,
            opening_gate_executable=position.opening_gate_executable,
            opening_gate_failure_reason=position.opening_gate_failure_reason,
            opening_gate_exit_timestamp=position.opening_gate_exit_timestamp,
            opening_gate_exit_reference_price=(
                position.opening_gate_exit_reference_price
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

    def _research_diagnostics(
        self,
        trades: list[BacktestTrade],
        positions: tuple[BacktestPosition, ...],
        skipped: Counter[str],
    ) -> dict:
        same_bar = [
            position
            for position in positions
            if position.entry_timestamp is not None
            and position.exit_timestamp == position.entry_timestamp
        ]
        survivors = [position for position in positions if position not in same_bar]
        partial_ids = {
            trade.position_id
            for trade in trades
            if trade.exit_reason == ExitReason.PARTIAL_TAKE_PROFIT.value
        }
        runners = [position for position in positions if position.position_id in partial_ids]
        reached_one_r = [
            position
            for position in positions
            if position.maximum_mfe_in_R is not None and position.maximum_mfe_in_R >= 1
        ]
        reached_two_r = [
            position
            for position in positions
            if position.maximum_mfe_in_R is not None and position.maximum_mfe_in_R >= 2
        ]
        locked = [
            position
            for position in positions
            if position.profit_lock_activation_timestamp is not None
        ]

        def average(values) -> float | None:
            selected = [float(value) for value in values if value is not None]
            return sum(selected) / len(selected) if selected else None

        attempts = self._research_counters["confirmation_attempts"]
        passes = self._research_counters["confirmation_passes"]
        cooldown_events = [
            event for event in self._candidate_events if event.get("cooldown_blocked") is True
        ]
        opening_gate_evaluated = [
            position for position in positions if position.opening_gate_evaluated
        ]
        thesis_blocked = [
            event
            for event in self._candidate_events
            if event.get("thesis_recovery_blocked") is True
        ]
        recovered = [
            event
            for event in self._candidate_events
            if event.get("previous_trade_failed") is True
            and event.get("thesis_recovered") is True
        ]
        f4_positions = [
            position
            for position in positions
            if position.stop_distance_pct == F4_STOP_DISTANCE_PCT
        ]
        return {
            **dict(sorted(self._research_counters.items())),
            "confirmation_events": self._confirmation_events,
            "candidate_events": self._candidate_events,
            "strict_coverage_sensitivity": self.strict_coverage_sensitivity,
            "same_entry_bar_final_exits": len(same_bar),
            "same_entry_bar_loss_rate": (
                sum(position.net_pnl < 0 for position in same_bar) / len(same_bar)
                if same_bar
                else None
            ),
            "same_entry_bar_losses": sum(position.net_pnl < 0 for position in same_bar),
            "first_bar_survivors": len(survivors),
            "survivor_win_rate": (
                sum(position.net_pnl > 0 for position in survivors) / len(survivors)
                if survivors
                else None
            ),
            "average_first_bar_survivor_return": average(
                position.position_return for position in survivors
            ),
            "trail_exits_in_entry_bar": sum(
                trade.exit_reason == ExitReason.ATR_TRAILING_STOP.value
                and trade.entry_timestamp is not None
                and trade.exit_timestamp == trade.entry_timestamp
                for trade in trades
            ),
            "confirmation_pass_rate": passes / attempts if attempts else None,
            "confirmation_rejection_count": self._research_counters[
                "confirmation_rejections"
            ],
            "missing_opening_bar_skips": skipped["missing_confirmation_bar"],
            "missing_execution_bar_skips": skipped["missing_execution_bar"],
            "cooldown_blocked_entries": skipped["reentry_cooldown"],
            "candidates_evaluated": len(self._candidate_events),
            "cooldown_blocks": len(cooldown_events),
            "unique_symbols_blocked": len(
                {event["symbol"] for event in cooldown_events}
            ),
            "subsequent_session_candidate_availability": sum(
                event.get("execution_session")
                == event.get("next_xnys_session_after_previous_exit")
                for event in self._candidate_events
                if event.get("next_xnys_session_after_previous_exit") is not None
            ),
            "opening_gate_evaluations": len(opening_gate_evaluated),
            "positions_stopped_before_opening_gate_evaluation": sum(
                position.opening_gate_position_alive_at_evaluation is False
                for position in positions
            ),
            "opening_gate_surviving_positions": len(opening_gate_evaluated),
            "opening_gate_green_survivors": sum(
                position.opening_gate_green is True for position in opening_gate_evaluated
            ),
            "opening_gate_non_green_survivors": sum(
                position.opening_gate_green is False for position in opening_gate_evaluated
            ),
            "opening_bar_fail_exits": sum(
                position.exit_reason == ExitReason.OPENING_BAR_FAIL.value
                for position in positions
            ),
            "opening_gate_missing_opening_bars": sum(
                position.opening_gate_failure_reason == "missing_opening_bar"
                for position in positions
            ),
            "opening_gate_missing_execution_bars": sum(
                position.opening_gate_failure_reason == "missing_execution_bar"
                for position in positions
            ),
            "baseline_first_bar_trail_exits": sum(
                position.baseline_first_bar_trail_exit_occurred
                for position in positions
            ),
            "partial_target_count": len(partial_ids),
            "runner_positions": len(runners),
            "runner_final_return": average(
                position.position_return for position in runners
            ),
            "runner_mfe": average(
                position.maximum_favorable_excursion for position in runners
            ),
            "runner_giveback": average(position.profit_giveback for position in runners),
            "reached_1r_mfe": len(reached_one_r),
            "reached_2r_mfe": len(reached_two_r),
            "break_even_lock_activations": sum(
                position.break_even_lock_timestamp is not None for position in positions
            ),
            "one_r_lock_activations": sum(
                position.one_r_lock_timestamp is not None for position in positions
            ),
            "losses_after_1r_mfe": sum(
                position.net_pnl < 0 for position in reached_one_r
            ),
            "losses_after_2r_mfe": sum(
                position.net_pnl < 0 for position in reached_two_r
            ),
            "average_giveback_after_profit_lock_activation": average(
                position.profit_giveback for position in locked
            ),
            "thesis_recovery_blocks": len(thesis_blocked),
            "recovered_reentries": len(recovered),
            "thesis_recovery_unique_symbols_blocked": len(
                {event["symbol"] for event in thesis_blocked}
            ),
            "f4_executed_trades": len(f4_positions),
            "f4_stop_exits": sum(
                position.exit_reason == ExitReason.STOP_LOSS.value
                for position in f4_positions
            ),
            "f4_confirmed_swing_high_exits": sum(
                position.exit_reason == ExitReason.CONFIRMED_SWING_HIGH.value
                for position in f4_positions
            ),
            "f4_session_close_exits": sum(
                position.exit_reason == ExitReason.SESSION_CLOSE.value
                for position in f4_positions
            ),
            "f4_average_entry_minutes_after_midnight_utc": average(
                position.entry_timestamp.hour * 60 + position.entry_timestamp.minute
                for position in f4_positions
                if position.entry_timestamp is not None
            ),
        }

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


def _backtest_sessions(database: Database, start: date, end: date) -> list[date]:
    official_sessions = set(trading_sessions_between(start, end))
    sessions = [
        session
        for session in database.bar_sessions(start, end)
        if session in official_sessions
    ]
    if len(sessions) < 2:
        first, last = database.bar_date_bounds()
        raise ValueError(
            "Backtest requires at least two local market sessions in the requested range; "
            f"local bar coverage is {first} through {last}"
        )
    return sessions


def prepare_strategy_comparison(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    comparison_kind: StrategyComparisonKind = StrategyComparisonKind.ALL,
) -> StrategyComparisonPreparation:
    """Build one comparison plan and populate its shared screens only when needed."""

    sessions = tuple(_backtest_sessions(database, start, end))
    runs = _comparison_runs(comparison_kind)
    shared = CachedScreenSource(HistoricalFeatureScreenSource(database, config, start, end))
    requirements = determine_intraday_comparison_requirements(
        config,
        runs,
        shared,
        sessions,
    )
    return StrategyComparisonPreparation(
        screen_source=shared,
        requested_start=start,
        requested_end=end,
        comparison_kind=comparison_kind,
        sessions=sessions,
        runs=runs,
        intraday_requirements=requirements,
    )


def determine_intraday_comparison_requirements(
    config: StrategyConfig,
    runs: tuple[tuple[StrategyVariant, PositionManagementPreset], ...],
    screen_source: ScreenSource,
    sessions: tuple[date, ...],
) -> tuple[IntradayPrefetchRequirement, ...]:
    """Resolve run timeframes and discover eligible PIT candidates without lookahead."""

    timeframes_by_variant: dict[StrategyVariant, set[BarTimeframe]] = {}
    for variant, preset in runs:
        resolved = position_management_preset(
            config.position_management,
            preset,
            legacy_max_holding_days=config.backtest.max_holding_days,
        )
        timeframe = _comparison_execution_timeframe(resolved, preset)
        if timeframe.intraday:
            timeframes_by_variant.setdefault(variant, set()).add(timeframe)
    if not timeframes_by_variant:
        return ()

    earliest_execution: dict[BarTimeframe, dict[str, date]] = {
        timeframe: {}
        for timeframes in timeframes_by_variant.values()
        for timeframe in timeframes
    }
    for index, signal_session in enumerate(sessions[:-1]):
        report = screen_source.screen(signal_session)
        execution_session = sessions[index + 1]
        for variant, timeframes in timeframes_by_variant.items():
            eligible_symbols = {
                record.symbol.upper()
                for record in report.records
                if evaluate_variant_entry(record, variant, config).eligible
            }
            for timeframe in timeframes:
                candidates = earliest_execution[timeframe]
                for symbol in eligible_symbols:
                    candidates.setdefault(symbol, execution_session)

    requirements: list[IntradayPrefetchRequirement] = []
    for timeframe in sorted(earliest_execution, key=lambda item: item.value):
        candidates = earliest_execution[timeframe]
        first_execution = min(candidates.values(), default=sessions[0])
        requirements.append(
            IntradayPrefetchRequirement(
                timeframe=timeframe,
                variants=tuple(
                    sorted(
                        (
                            variant
                            for variant, timeframes in timeframes_by_variant.items()
                            if timeframe in timeframes
                        ),
                        key=lambda item: item.value,
                    )
                ),
                symbols=tuple(sorted(candidates)),
                first_execution_sessions=tuple(sorted(candidates.items())),
                comparison_sessions=sessions,
                requested_start=intraday_warmup_start(
                    first_execution,
                    timeframe,
                    config.intraday.warmup_bars,
                    extended_hours=config.intraday.extended_hours,
                ),
                requested_end=intraday_session_bounds(
                    sessions[-1], extended_hours=config.intraday.extended_hours
                )[1],
                warmup_bars=config.intraday.warmup_bars,
                extended_hours=config.intraday.extended_hours,
            )
        )
    return tuple(requirements)


def _comparison_execution_timeframe(
    resolved: PositionManagementConfig,
    preset: PositionManagementPreset,
) -> BarTimeframe:
    if preset in CONFIRMED_ENTRY_PRESETS:
        return BarTimeframe.MINUTES_15
    return BarTimeframe(resolved.bar_timeframe)


def assess_comparison_intraday_coverage(
    database: Database,
    requirements: tuple[IntradayPrefetchRequirement, ...],
) -> tuple[IntradayCoverageAssessment, ...]:
    """Batch-read candidate bars and identify symbols that the engine cannot consume yet."""

    assessments: list[IntradayCoverageAssessment] = []
    for requirement in requirements:
        first_execution = dict(requirement.first_execution_sessions)
        if not requirement.symbols:
            assessments.append(
                IntradayCoverageAssessment(requirement, (), (), ())
            )
            continue
        coverage_sessions = trading_sessions_between(
            requirement.requested_start.date(), requirement.comparison_sessions[-1]
        )
        session_windows = [
            (
                session,
                *intraday_session_bounds(
                    session, extended_hours=requirement.extended_hours
                ),
            )
            for session in coverage_sessions
        ]
        window_starts = [opening for _, opening, _ in session_windows]
        bars_by_symbol: dict[str, list[tuple[datetime, date]]] = {
            symbol: [] for symbol in requirement.symbols
        }
        bars = database.bars_between(
            requirement.symbols,
            requirement.requested_start,
            requirement.requested_end,
            timeframe=requirement.timeframe,
        )
        for bar in bars:
            window_index = bisect_right(window_starts, bar.timestamp) - 1
            if window_index < 0:
                continue
            session, opening, closing = session_windows[window_index]
            if opening <= bar.timestamp < closing:
                bars_by_symbol[bar.symbol].append((bar.timestamp, session))

        incomplete: dict[str, tuple[str, ...]] = {}
        comparison_index = {
            session: index for index, session in enumerate(requirement.comparison_sessions)
        }
        for symbol in requirement.symbols:
            symbol_bars = bars_by_symbol[symbol]
            execution_session = first_execution[symbol]
            reasons: list[str] = []
            if not symbol_bars:
                reasons.append("no_data")
            warmup_count = sum(
                1 for _, bar_session in symbol_bars if bar_session < execution_session
            )
            if warmup_count < requirement.warmup_bars:
                reasons.append("warmup")
            present_sessions = {bar_session for _, bar_session in symbol_bars}
            required_sessions = requirement.comparison_sessions[
                comparison_index[execution_session] :
            ]
            missing_sessions = [
                session for session in required_sessions if session not in present_sessions
            ]
            if missing_sessions:
                reasons.append(
                    "sessions=" + ",".join(session.isoformat() for session in missing_sessions)
                )
            if reasons:
                incomplete[symbol] = tuple(reasons)
        sync_symbols = tuple(sorted(incomplete))
        assessments.append(
            IntradayCoverageAssessment(
                requirement=requirement,
                complete_symbols=tuple(
                    symbol for symbol in requirement.symbols if symbol not in incomplete
                ),
                sync_symbols=sync_symbols,
                incomplete_reasons=tuple(sorted(incomplete.items())),
            )
        )
    return tuple(assessments)


def comparison_intraday_prefetch_metadata(
    preparation: StrategyComparisonPreparation,
    *,
    enabled: bool,
) -> IntradayPrefetch:
    """Describe a required-but-not-run prefetch, including the CLI opt-out case."""

    return IntradayPrefetch(
        required=bool(preparation.intraday_requirements),
        enabled=enabled,
        candidate_symbols=preparation.intraday_candidate_symbols,
        timeframes={
            requirement.timeframe.value: IntradayPrefetchTimeframe(
                candidate_symbols=len(requirement.symbols),
                already_complete_symbols=0,
                sync_requested_symbols=0,
                warmup_bars=requirement.warmup_bars,
                extended_hours=requirement.extended_hours,
            )
            for requirement in preparation.intraday_requirements
        },
    )


def prefetch_comparison_intraday_data(
    database: Database,
    config: StrategyConfig,
    preparation: StrategyComparisonPreparation,
    assessments: tuple[IntradayCoverageAssessment, ...],
    synchronizer_factory: Callable[[], IntradaySynchronizer],
) -> IntradayPrefetch:
    """Synchronize only deficient candidate/timeframe pairs, then verify local coverage."""

    if tuple(item.requirement for item in assessments) != preparation.intraday_requirements:
        raise ValueError("Intraday coverage assessments do not match the comparison plan")
    synchronizer: IntradaySynchronizer | None = None
    factory_failure: str | None = None
    timeframes: dict[str, IntradayPrefetchTimeframe] = {}
    for assessment in assessments:
        requirement = assessment.requirement
        bars_added = 0
        provider_requests = 0
        failure_reasons: list[str] = []
        if assessment.sync_symbols:
            if synchronizer is None and factory_failure is None:
                try:
                    synchronizer = synchronizer_factory()
                except Exception as exc:
                    factory_failure = (
                        "intraday prefetch failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            if factory_failure is not None:
                failure_reasons.append(factory_failure)
            else:
                assert synchronizer is not None
                try:
                    sync_result = synchronizer.sync_intraday(
                        assessment.sync_symbols,
                        (requirement.timeframe,),
                        requirement.requested_start,
                        requirement.requested_end,
                        incremental=config.intraday.sync.incremental,
                        extended_hours=requirement.extended_hours,
                    )
                except Exception as exc:
                    failure_reasons.append(
                        "intraday prefetch failed for "
                        f"{requirement.timeframe.value}: {type(exc).__name__}: {exc}"
                    )
                else:
                    bars_added = int(sync_result.get("bars_inserted", 0))
                    provider_requests = int(sync_result.get("request_batches", 0))
                    provider_errors = int(sync_result.get("errors", 0))
                    if provider_errors:
                        failure_reasons.append(
                            "intraday prefetch failed for "
                            f"{requirement.timeframe.value}: {provider_errors} provider "
                            "request batch(es) failed"
                        )
                    post_sync = assess_comparison_intraday_coverage(
                        database, (requirement,)
                    )[0]
                    failure_reasons.extend(_coverage_failure_messages(post_sync))
        timeframes[requirement.timeframe.value] = IntradayPrefetchTimeframe(
            candidate_symbols=len(requirement.symbols),
            already_complete_symbols=len(assessment.complete_symbols),
            sync_requested_symbols=len(assessment.sync_symbols),
            warmup_bars=requirement.warmup_bars,
            extended_hours=requirement.extended_hours,
            bars_added=bars_added,
            provider_requests=provider_requests,
            failure_reasons=tuple(dict.fromkeys(failure_reasons)),
        )
    return IntradayPrefetch(
        required=bool(preparation.intraday_requirements),
        enabled=True,
        candidate_symbols=preparation.intraday_candidate_symbols,
        timeframes=timeframes,
    )


def _coverage_failure_messages(
    assessment: IntradayCoverageAssessment,
) -> tuple[str, ...]:
    timeframe = assessment.requirement.timeframe.value
    reasons = dict(assessment.incomplete_reasons)
    no_data = [symbol for symbol, items in reasons.items() if "no_data" in items]
    warmup = [
        symbol
        for symbol, items in reasons.items()
        if "warmup" in items and symbol not in no_data
    ]
    session_gaps = [
        symbol
        for symbol, items in reasons.items()
        if any(item.startswith("sessions=") for item in items)
        and symbol not in no_data
        and symbol not in warmup
    ]
    messages: list[str] = []
    if no_data:
        messages.append(
            f"provider returned no {timeframe} data for {_compact_symbols(no_data)}"
        )
    if warmup:
        messages.append(
            f"insufficient {timeframe} warmup for {_compact_symbols(warmup)}"
        )
    if session_gaps:
        messages.append(
            f"local {timeframe} data remains incomplete for "
            f"{_compact_symbols(session_gaps)}"
        )
    return tuple(messages)


def _compact_symbols(symbols: list[str], *, limit: int = 10) -> str:
    selected = sorted(symbols)
    rendered = ",".join(selected[:limit])
    if len(selected) > limit:
        rendered += f" (+{len(selected) - limit} more)"
    return rendered


def compare_strategies(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    comparison_kind: StrategyComparisonKind = StrategyComparisonKind.ALL,
    preparation: StrategyComparisonPreparation | None = None,
    intraday_prefetch: IntradayPrefetch | None = None,
    data_qualification: dict | None = None,
    strict_coverage_sensitivity: bool = False,
    intraday_session_statuses: dict[tuple[str, date], str] | None = None,
    allow_missing_intraday_data: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StrategyComparison:
    """Compare score variants, position presets, or both on shared PIT screens."""

    prepared = preparation or prepare_strategy_comparison(
        database, config, start, end, comparison_kind=comparison_kind
    )
    if (
        prepared.requested_start != start
        or prepared.requested_end != end
        or prepared.comparison_kind is not comparison_kind
    ):
        raise ValueError("Prepared strategy comparison does not match the requested run")
    shared = prepared.screen_source
    generated_at = clock().isoformat()
    runs = prepared.runs
    prefetch = intraday_prefetch or comparison_intraday_prefetch_metadata(
        prepared, enabled=False
    )
    results: list[BacktestResult] = []
    skipped: dict[str, str] = {}
    for variant, preset in runs:
        label = _comparison_label(variant, preset, comparison_kind)
        resolved = position_management_preset(
            config.position_management,
            preset,
            legacy_max_holding_days=config.backtest.max_holding_days,
        )
        timeframe = _comparison_execution_timeframe(resolved, preset)
        prefetch_timeframe = prefetch.timeframes.get(timeframe.value)
        if (
            timeframe.intraday
            and prefetch_timeframe is not None
            and prefetch_timeframe.failure_reasons
        ):
            skipped[label] = "; ".join(prefetch_timeframe.failure_reasons)
            continue
        try:
            result = BacktestEngine(
                database,
                config,
                screen_source=shared,
                strict_coverage_sensitivity=strict_coverage_sensitivity,
                intraday_session_statuses=intraday_session_statuses,
                allow_missing_intraday_data=allow_missing_intraday_data,
                clock=lambda: datetime.fromisoformat(generated_at),
            ).run(start, end, variant=variant, preset=preset)
        except MissingIntradayDataError as exc:
            skipped[label] = str(exc)
            continue
        results.append(result)
    if not results:
        raise ValueError("Strategy comparison produced no executable strategies")
    first = results[0]
    qualification_warnings: list[str] = []
    qualification = data_qualification or {}
    daily_qualification = qualification.get("daily", {})
    if daily_qualification.get("internal_missing_sessions", 0):
        qualification_warnings.append(
            "Daily qualification found internal missing sessions; see data qualification report"
        )
    for timeframe, report in qualification.get("intraday", {}).items():
        if report.get("missing_sessions", 0) or report.get("partial_sessions", 0):
            qualification_warnings.append(
                f"{timeframe} qualification contains missing/partial sessions; no Daily "
                "fallback or synthetic bars were used"
            )
    return StrategyComparison(
        requested_start=start,
        requested_end=end,
        actual_start=first.actual_start,
        actual_end=first.actual_end,
        generated_at=generated_at,
        variants=tuple(results),
        shared_screen_sessions=len(shared.cache),
        warnings=tuple(dict.fromkeys((*first.warnings, *qualification_warnings))),
        comparison_kind=comparison_kind,
        skipped_strategies=skipped,
        intraday_prefetch=prefetch,
        data_qualification=qualification,
        strict_coverage_sensitivity=strict_coverage_sensitivity,
        research_diagnostics={
            _comparison_label(
                result.strategy_variant,
                result.position_management_preset,
                comparison_kind,
            ): result.research_diagnostics
            for result in results
        },
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
    production_position_presets = (
        PositionManagementPreset.LEGACY,
        PositionManagementPreset.DYNAMIC_HOLD,
        PositionManagementPreset.TAKE_PROFIT,
        PositionManagementPreset.ATR_TRAILING,
        PositionManagementPreset.PARTIAL_PROFIT,
        PositionManagementPreset.INTRADAY_DYNAMIC,
        PositionManagementPreset.BASELINE_FIXED_STOP,
        PositionManagementPreset.FIXED_STOP_MAX_HOLD,
        PositionManagementPreset.FIXED_STOP_TAKE_PROFIT,
        PositionManagementPreset.FIXED_STOP_ATR_TRAILING,
        PositionManagementPreset.FIXED_STOP_PARTIAL_ATR,
    )
    position_runs = tuple(
        (StrategyVariant.FULL, preset) for preset in production_position_presets
    )
    if comparison_kind is StrategyComparisonKind.SCORE_VARIANTS:
        return score_runs
    if comparison_kind is StrategyComparisonKind.POSITION_MANAGEMENT:
        return position_runs
    if comparison_kind in RESEARCH_FAMILY_RUNS:
        return research_family_runs(comparison_kind)
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
    if comparison_kind in RESEARCH_FAMILY_RUNS:
        return comparison_strategy_label(comparison_kind, variant, preset)
    return f"{variant.value}/{preset.value}"


def research_strategy_label(
    variant: StrategyVariant, preset: PositionManagementPreset
) -> str:
    """Historical public label helper retained for validation/report compatibility."""

    if preset is PositionManagementPreset.CONFIGURED:
        return f"{variant.value}/configured"
    if preset is PositionManagementPreset.INTRADAY_DYNAMIC:
        return f"{variant.value}/intraday-dynamic"
    return registered_research_strategy_label(variant, preset)


def evaluate_variant_entry(
    record: ScreenRecord, variant: StrategyVariant, config: StrategyConfig
) -> EntryFilterEvaluation:
    """Evaluate the production entry funnel and expose its point-in-time evidence."""

    quality = record.scores.quality.score
    valuation = record.scores.valuation.score
    opportunity = record.scores.opportunity.score
    timing = record.scores.timing.score
    rules = config.backtest
    technical = record.technical
    price_above_sma20 = (
        None
        if technical.price is None or technical.sma20 is None
        else technical.price > technical.sma20
    )
    rsi_recovery = technical.rsi_recovery
    momentum_recovery = (
        None if technical.momentum5 is None else technical.momentum5 > 0
    )
    relative_volume_recovery = (
        None
        if technical.relative_volume is None
        else technical.relative_volume > rules.min_relative_volume
    )
    recovery_values = (rsi_recovery, momentum_recovery, relative_volume_recovery)
    recovery_pass = (
        None
        if all(value is None for value in recovery_values)
        else any(value is True for value in recovery_values)
    )
    blocking = tuple(
        reason for reason in record.exclusion_reasons if reason not in SCORE_FILTER_EXCLUSIONS
    )

    def result(
        first_failure: str | None,
        failure_detail: str | None = None,
        *,
        weighted_score: float | None = None,
    ) -> EntryFilterEvaluation:
        return EntryFilterEvaluation(
            score=weighted_score if first_failure is None else None,
            first_failure=first_failure,
            failure_detail=failure_detail,
            blocking_reasons=blocking,
            quality_score=quality,
            valuation_score=valuation,
            opportunity_score=opportunity,
            timing_score=timing,
            weighted_score=weighted_score,
            price_above_sma20=price_above_sma20,
            rsi_recovery=rsi_recovery,
            momentum5_above_zero=momentum_recovery,
            relative_volume_above_threshold=relative_volume_recovery,
            recovery_gate_pass=recovery_pass,
        )

    if blocking:
        return result(blocking[0], blocking[0])
    if quality is None:
        return result("quality_threshold", "quality_score_unavailable")
    if quality < rules.min_quality_score:
        return result("quality_threshold", "quality_threshold")
    if valuation is None:
        return result("valuation_threshold", "valuation_score_unavailable")
    if valuation < rules.min_valuation_score:
        return result("valuation_threshold", "valuation_threshold")
    components: list[tuple[float, float]] = [
        (quality, config.scores.total.quality),
        (valuation, config.scores.total.valuation),
    ]
    if variant in {StrategyVariant.QUALITY_VALUE_OPPORTUNITY, StrategyVariant.FULL}:
        if opportunity is None:
            return result("opportunity_threshold", "opportunity_score_unavailable")
        if opportunity < rules.min_opportunity_score:
            return result("opportunity_threshold", "opportunity_threshold")
        components.append((opportunity, config.scores.total.opportunity))
    if variant is StrategyVariant.FULL:
        if timing is None:
            return result("timing_threshold", "timing_score_unavailable")
        if timing < rules.min_timing_score:
            return result("timing_threshold", "timing_threshold")
        components.append((timing, config.scores.total.timing))
    denominator = sum(weight for _, weight in components)
    score = sum(value * weight for value, weight in components) / denominator
    if score < rules.min_total_score:
        return result("total_threshold", "total_threshold", weighted_score=score)
    if price_above_sma20 is None:
        return result(
            "price_not_above_sma20", "sma20_or_price_unavailable", weighted_score=score
        )
    if not price_above_sma20:
        return result("price_not_above_sma20", "price_not_above_sma20", weighted_score=score)
    if recovery_pass is not True:
        detail = (
            "recovery_inputs_unavailable"
            if recovery_pass is None
            else "recovery_signal_required"
        )
        return result("recovery_signal_required", detail, weighted_score=score)
    return result(None, weighted_score=score)


def _variant_entry_score(
    record: ScreenRecord, variant: StrategyVariant, config: StrategyConfig
) -> tuple[float | None, str | None]:
    evaluation = evaluate_variant_entry(record, variant, config)
    return evaluation.score, evaluation.first_failure


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
        and not config.profit_lock.enabled
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
