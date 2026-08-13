"""Point-in-time daily-bar backtester with next-session simulated execution."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from trading_system.backtest.metrics import calculate_metrics, maximum_drawdown
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import (
    BacktestResult,
    BacktestTrade,
    BenchmarkResult,
    EquityPoint,
    StrategyComparison,
    StrategyVariant,
)
from trading_system.models.market_data import DailyBar
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.strategy.screener import Screener

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
    "daily OHLC cannot order an intraday stop and target; the stop is assumed first",
)


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


@dataclass(frozen=True)
class _PendingEntry:
    record: ScreenRecord
    signal_date: date
    variant_score: float
    variant: StrategyVariant


@dataclass
class _Position:
    symbol: str
    signal_date: date
    entry_date: date
    entry_reference_price: float
    entry_price: float
    quantity: float
    position_value: float
    stop_price: float
    target_price: float
    entry_commission: float
    entry_slippage: float
    quality_score: float
    valuation_score: float
    opportunity_score: float | None
    timing_score: float | None
    total_score: float
    sector: str
    variant: StrategyVariant
    last_price: float
    holding_days: int = 0


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
        self.screen_source = screen_source or HistoricalScreenSource(database, config)
        self.clock = clock

    def run(
        self,
        start: date,
        end: date,
        *,
        variant: StrategyVariant = StrategyVariant.FULL,
    ) -> BacktestResult:
        if start > end:
            raise ValueError("Backtest start must not be after end")
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
        positions: dict[str, _Position] = {}
        pending: list[_PendingEntry] = []
        trades: list[BacktestTrade] = []
        curve: list[EquityPoint] = []
        skipped: Counter[str] = Counter()

        for index, session in enumerate(sessions):
            final_session = index == len(sessions) - 1
            active_symbols = set(positions) | {order.record.symbol for order in pending}
            bars = self.database.bars_on_session(active_symbols, session)

            for position in positions.values():
                position.holding_days += 1

            # 1. Existing positions can gap through a stop/target at the session open.
            for symbol in list(positions):
                bar = bars.get(symbol)
                if bar is None:
                    continue
                position = positions[symbol]
                reference, reason = _open_exit(position, bar)
                if reference is not None:
                    cash, trade = self._close(position, session, reference, reason, cash)
                    trades.append(trade)
                    del positions[symbol]

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
                position, cash, reason = self._open_position(order, bar, bars, positions, cash)
                if position is None:
                    skipped[reason or "entry_rejected"] += 1
                    continue
                positions[position.symbol] = position
            pending = []

            # 3. Intraday stops/targets use daily OHLC; simultaneous hits are stop-first.
            for symbol in list(positions):
                bar = bars.get(symbol)
                if bar is None:
                    continue
                position = positions[symbol]
                position.last_price = float(bar.close)
                reference, reason = _intraday_exit(position, bar)
                if reference is not None:
                    cash, trade = self._close(position, session, reference, reason, cash)
                    trades.append(trade)
                    del positions[symbol]

            # 4. Time exits occur at this completed session's close.
            for symbol in list(positions):
                position = positions[symbol]
                bar = bars.get(symbol)
                if (
                    bar is not None
                    and position.holding_days >= self.config.backtest.max_holding_days
                ):
                    cash, trade = self._close(
                        position, session, float(bar.close), "time_exit", cash
                    )
                    trades.append(trade)
                    del positions[symbol]

            # 5. The last session liquidates at its close; no new signal is queued.
            if final_session:
                for symbol in list(positions):
                    position = positions[symbol]
                    bar = bars.get(symbol)
                    if bar is None:
                        history = self.database.bars_available_as_of(symbol, session, limit=1)
                        if not history:
                            skipped["missing_final_exit_bar"] += 1
                            continue
                        bar = history[-1]
                    cash, trade = self._close(
                        position,
                        bar.timestamp.date(),
                        float(bar.close),
                        "end_of_backtest",
                        cash,
                    )
                    trades.append(trade)
                    del positions[symbol]
            else:
                # 6. The point-in-time screen is calculated only after the close.
                report = self.screen_source.screen(session)
                pending = self._entry_orders(report, variant, positions, skipped)

            market_value = sum(
                position.quantity * position.last_price for position in positions.values()
            )
            unrealized_pnl = sum(
                (position.last_price - position.entry_price) * position.quantity
                - position.entry_commission
                for position in positions.values()
            )
            equity = cash + market_value
            curve.append(
                EquityPoint(
                    date=session,
                    cash=max(cash, 0.0),
                    market_value=market_value,
                    portfolio_equity=equity,
                    active_positions=len(positions),
                    exposure=market_value / equity if equity > 0 else 0.0,
                    realized_pnl=sum(trade.pnl for trade in trades),
                    unrealized_pnl=unrealized_pnl,
                )
            )

        warnings = list(BACKTEST_WARNINGS)
        first_bound, last_bound = self.database.bar_date_bounds()
        if first_bound is not None and start < first_bound:
            warnings.append(f"requested start predates local bars and was clipped to {sessions[0]}")
        if last_bound is not None and end > last_bound:
            warnings.append(f"requested end exceeds local bars and was clipped to {sessions[-1]}")
        benchmark = self._benchmark(sessions[0], sessions[-1])
        if benchmark.warning:
            warnings.append(benchmark.warning)
        return BacktestResult(
            requested_start=start,
            requested_end=end,
            actual_start=sessions[0],
            actual_end=sessions[-1],
            generated_at=self.clock().isoformat(),
            strategy_variant=variant,
            initial_capital=float(self.config.backtest.initial_capital),
            configuration=self._configuration_snapshot(variant),
            metrics=calculate_metrics(curve, trades, float(self.config.backtest.initial_capital)),
            benchmark=benchmark,
            trades=tuple(trades),
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
            warnings=tuple(warnings),
        )

    def _entry_orders(
        self,
        report: ScreenReport,
        variant: StrategyVariant,
        positions: dict[str, _Position],
        skipped: Counter[str],
    ) -> list[_PendingEntry]:
        capacity = self.config.portfolio.max_positions - len(positions)
        if capacity <= 0:
            return []
        occupied = set(positions)
        candidates: list[_PendingEntry] = []
        for record in report.records:
            if record.symbol in occupied:
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

    def _open_position(
        self,
        order: _PendingEntry,
        bar: DailyBar,
        session_bars: dict[str, DailyBar],
        positions: dict[str, _Position],
        cash: float,
    ) -> tuple[_Position | None, float, str | None]:
        if len(positions) >= self.config.portfolio.max_positions:
            return None, cash, "max_positions"
        record = order.record
        sector = (record.sic or "unknown")[:2]
        sector_count = sum(position.sector == sector for position in positions.values())
        if sector_count >= self.config.portfolio.max_sector_positions:
            return None, cash, "max_sector_positions"
        atr = record.technical.atr14
        if atr is None or atr <= 0:
            return None, cash, "invalid_atr"
        reference = float(bar.open)
        fill = _buy_fill(reference, self.config.backtest.slippage_bps)
        stop_distance = min(
            atr * self.config.risk.atr_stop_multiple,
            fill * self.config.risk.max_stop_loss_pct,
        )
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
        position = _Position(
            symbol=record.symbol,
            signal_date=order.signal_date,
            entry_date=bar.timestamp.date(),
            entry_reference_price=reference,
            entry_price=fill,
            quantity=quantity,
            position_value=notional,
            stop_price=fill - stop_distance,
            target_price=fill * (1 + self.config.backtest.profit_target_pct),
            entry_commission=commission,
            entry_slippage=(fill - reference) * quantity,
            quality_score=float(record.scores.quality.score),  # validated by entry filter
            valuation_score=float(record.scores.valuation.score),
            opportunity_score=record.scores.opportunity.score,
            timing_score=record.scores.timing.score,
            total_score=order.variant_score,
            sector=sector,
            variant=order.variant,
            last_price=fill,
            holding_days=1,
        )
        return position, cash - cost, None

    def _close(
        self,
        position: _Position,
        exit_date: date,
        reference: float,
        reason: str,
        cash: float,
    ) -> tuple[float, BacktestTrade]:
        fill = _sell_fill(reference, self.config.backtest.slippage_bps)
        proceeds = fill * position.quantity
        exit_commission = proceeds * self.config.backtest.commission_bps / 10_000
        cash += proceeds - exit_commission
        pnl = proceeds - exit_commission - position.position_value - position.entry_commission
        slippage = position.entry_slippage + (reference - fill) * position.quantity
        trade = BacktestTrade(
            symbol=position.symbol,
            signal_date=position.signal_date,
            entry_date=position.entry_date,
            entry_reference_price=position.entry_reference_price,
            entry_price=position.entry_price,
            exit_date=exit_date,
            exit_reference_price=reference,
            exit_price=fill,
            quantity=position.quantity,
            position_value=position.position_value,
            stop_price=position.stop_price,
            target_price=position.target_price,
            quality_score=position.quality_score,
            valuation_score=position.valuation_score,
            opportunity_score=position.opportunity_score,
            timing_score=position.timing_score,
            total_score=position.total_score,
            exit_reason=reason,
            pnl=pnl,
            return_pct=pnl / (position.position_value + position.entry_commission),
            slippage=slippage,
            transaction_cost=position.entry_commission + exit_commission,
            holding_days=max(position.holding_days, 1),
            strategy_variant=position.variant,
        )
        return cash, trade

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

    def _configuration_snapshot(self, variant: StrategyVariant) -> dict:
        return {
            "variant": variant.value,
            "strategy": self.config.model_dump(mode="json"),
            "portfolio": self.config.portfolio.model_dump(mode="json"),
            "risk": self.config.risk.model_dump(mode="json"),
            "backtest": self.config.backtest.model_dump(mode="json"),
            "score_weights": self.config.scores.total.model_dump(mode="json"),
            "market_data_adjustment": self.config.universe.market_data_adjustment,
            "execution": {
                "entry": "next available portfolio session open",
                "stop_target_ambiguity": "stop_first",
                "time_exit": "close",
                "end_of_backtest": "last available close",
            },
        }


def compare_strategies(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StrategyComparison:
    shared = CachedScreenSource(HistoricalScreenSource(database, config))
    generated_at = clock().isoformat()
    results = tuple(
        BacktestEngine(
            database,
            config,
            screen_source=shared,
            clock=lambda: datetime.fromisoformat(generated_at),
        ).run(start, end, variant=variant)
        for variant in StrategyVariant
    )
    first = results[0]
    return StrategyComparison(
        requested_start=start,
        requested_end=end,
        actual_start=first.actual_start,
        actual_end=first.actual_end,
        generated_at=generated_at,
        variants=results,
        shared_screen_sessions=len(shared.cache),
        warnings=first.warnings,
    )


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


def _buy_fill(reference: float, slippage_bps: float) -> float:
    return reference * (1 + slippage_bps / 10_000)


def _sell_fill(reference: float, slippage_bps: float) -> float:
    return reference * (1 - slippage_bps / 10_000)


def _open_exit(position: _Position, bar: DailyBar) -> tuple[float | None, str]:
    opening = float(bar.open)
    if opening <= position.stop_price:
        return opening, "stop_loss"
    if opening >= position.target_price:
        return opening, "profit_target"
    return None, ""


def _intraday_exit(position: _Position, bar: DailyBar) -> tuple[float | None, str]:
    stop_hit = float(bar.low) <= position.stop_price
    target_hit = float(bar.high) >= position.target_price
    if stop_hit:
        return position.stop_price, "stop_loss"
    if target_hit:
        return position.target_price, "profit_target"
    return None, ""
