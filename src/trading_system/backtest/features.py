"""Batched, run-local, point-in-time historical feature preparation."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.fundamentals.metrics import shares_outstanding_as_of
from trading_system.fundamentals.quality import accounting_state_as_of, attach_market_price
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact, FundamentalMetrics
from trading_system.models.screening import ScreenReport
from trading_system.models.signals import TechnicalSnapshot
from trading_system.strategy.screener import (
    Screener,
    _missing_metric_warnings,
    _PreparedCandidate,
)


@dataclass
class HistoricalPerformanceDiagnostics:
    feature_prepare_seconds: float = 0.0
    bars_load_seconds: float = 0.0
    facts_load_seconds: float = 0.0
    accounting_feature_seconds: float = 0.0
    technical_feature_seconds: float = 0.0
    peer_score_seconds: float = 0.0
    screen_seconds: float = 0.0
    universe_resolution_seconds: float = 0.0
    identity_resolution_seconds: float = 0.0
    universe_filter_seconds: float = 0.0
    report_construction_seconds: float = 0.0
    sqlite_query_count: int = 0
    bars_rows_loaded: int = 0
    facts_rows_loaded: int = 0
    companies_universe: int = 0
    companies_after_identity: int = 0
    companies_after_static: int = 0
    companies_after_market: int = 0
    companies_after_fundamental: int = 0
    feature_cache_hits: int = 0
    feature_cache_misses: int = 0
    sessions_screened: int = 0
    estimated_bar_memory_bytes: int = 0
    estimated_fact_memory_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        output = vars(self).copy()
        for key, value in output.items():
            if key.endswith("_seconds"):
                output[key] = round(float(value), 6)
        return output


@dataclass
class _MarketFeature:
    technical: TechnicalSnapshot
    price: Decimal
    average_price_20d: Decimal | None
    average_volume_20d: Decimal | None
    average_dollar_volume_20d: float | None
    history_count: int


@dataclass(frozen=True, slots=True)
class _HistoricalBar:
    symbol: str
    session: date
    high: float
    low: float
    close: Decimal
    volume: int


@dataclass
class _AccountingCache:
    facts: list[FundamentalFact]
    state_by_key: dict[tuple[date, int, tuple[Any, ...] | None], Any] = field(default_factory=dict)

    def metrics(
        self,
        session: date,
        price: Decimal,
        diagnostics: HistoricalPerformanceDiagnostics,
    ) -> FundamentalMetrics:
        eligible_count = _eligible_fact_count(self.facts, session)
        latest_filed = self.facts[eligible_count - 1].filed if eligible_count else date.min
        eligible = self.facts[:eligible_count]
        shares = shares_outstanding_as_of(eligible, session)
        shares_key = (
            (
                shares.period_end,
                shares.filed,
                shares.accession_number,
                shares.tag,
                shares.value,
            )
            if shares is not None
            else None
        )
        # Accounting values are stable between filing changes, except that an old
        # shares fact can age out. Including the selected share identity prevents
        # that expiry from reusing an unsafe earlier state.
        key = (latest_filed, eligible_count, shares_key)
        state = self.state_by_key.get(key)
        if state is None:
            started = time.perf_counter()
            state = accounting_state_as_of(eligible, session)
            diagnostics.accounting_feature_seconds += time.perf_counter() - started
            diagnostics.feature_cache_misses += 1
            self.state_by_key[key] = state
        else:
            diagnostics.feature_cache_hits += 1
        return attach_market_price(state, price)


class HistoricalFeatureScreenSource:
    """One-run historical screen source with causal technical and filing-state caches."""

    def __init__(
        self,
        database: Database,
        config: StrategyConfig,
        start: date,
        end: date,
    ) -> None:
        self.database = database
        self.config = config
        self.start = start
        self.end = end
        self.screener = Screener(database, config)
        self.diagnostics = HistoricalPerformanceDiagnostics()
        self._market: dict[str, dict[date, _MarketFeature]] = {}
        self._facts: dict[str, _AccountingCache] = {}
        self._shares: dict[str, list[FundamentalFact]] = {}
        self._cheap_exclusions: dict[str, dict[date, list[str]]] = {}
        self._companies: list[CompanyIdentity] = []
        self._conflicted: list[CompanyIdentity] = []
        self._static_exclusions: dict[str, str] = {}
        self._prepare()

    def _prepare(self) -> None:
        started = time.perf_counter()
        universe_started = time.perf_counter()
        companies = self.database.list_tradable_companies()
        self.diagnostics.universe_resolution_seconds = time.perf_counter() - universe_started
        identity_started = time.perf_counter()
        conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        self.diagnostics.identity_resolution_seconds = time.perf_counter() - identity_started
        self.diagnostics.sqlite_query_count += 3  # companies and two compact conflict-state sources
        self.diagnostics.companies_universe = len(companies)
        self._conflicted = [company for company in companies if company.symbol in conflicts]
        safe = [company for company in companies if company.symbol not in conflicts]
        self.diagnostics.companies_after_identity = len(safe)
        for company in safe:
            if self.config.universe.exclude_reits and is_reit(company.sic):
                self._static_exclusions[company.symbol] = "reit_excluded"
            elif self.config.universe.exclude_financials and is_financial_or_reit(company.sic):
                self._static_exclusions[company.symbol] = "financial_excluded"
        market_symbols = [
            company.symbol for company in safe if company.symbol not in self._static_exclusions
        ]
        reit_symbols = [
            company.symbol
            for company in safe
            if self._static_exclusions.get(company.symbol) == "reit_excluded"
        ]
        bar_symbols = [*market_symbols, *reit_symbols]
        self.diagnostics.companies_after_static = len(market_symbols)
        sessions = self.database.bar_sessions(self.start, self.end)
        earliest_bar, _ = self.database.bar_date_bounds()
        self.diagnostics.sqlite_query_count += 2
        required_start = max(
            earliest_bar or self.start,
            self.start - timedelta(days=max(550, self.config.universe.market_data_days * 2)),
        )
        bars_started = time.perf_counter()
        grouped_bars: dict[str, list[_HistoricalBar]] = defaultdict(list)
        for batch in self.database.iter_bar_value_batches(bar_symbols, required_start, self.end):
            self.diagnostics.sqlite_query_count += 1
            self.diagnostics.bars_rows_loaded += len(batch)
            for symbol, timestamp, high, low, close, volume in batch:
                bar = _HistoricalBar(
                    symbol=str(symbol),
                    session=date.fromisoformat(str(timestamp)[:10]),
                    high=float(high),
                    low=float(low),
                    close=Decimal(str(close)),
                    volume=int(volume),
                )
                grouped_bars[bar.symbol].append(bar)
        self.diagnostics.bars_load_seconds = time.perf_counter() - bars_started
        self.diagnostics.estimated_bar_memory_bytes = self.diagnostics.bars_rows_loaded * 96

        filter_started = time.perf_counter()
        cheap_market: dict[str, dict[date, _MarketFeature]] = {}
        technical_symbols: list[str] = []
        for symbol, bars in grouped_bars.items():
            symbol_market = _cheap_market_features(bars, sessions, self.config)
            cheap_market[symbol] = symbol_market
            symbol_exclusions = {
                session: _cheap_market_exclusions(feature, self.config, session)
                for session, feature in symbol_market.items()
            }
            self._cheap_exclusions[symbol] = symbol_exclusions
            if symbol not in self._static_exclusions and any(
                not reasons for reasons in symbol_exclusions.values()
            ):
                technical_symbols.append(symbol)
        self._market = cheap_market
        self.diagnostics.universe_filter_seconds += time.perf_counter() - filter_started
        technical_started = time.perf_counter()
        for symbol in technical_symbols:
            self._market[symbol] = _exact_market_features(
                grouped_bars[symbol], cheap_market[symbol], self.config
            )
        self.diagnostics.technical_feature_seconds = time.perf_counter() - technical_started
        market_survivors = [
            company.symbol
            for company in safe
            if company.symbol not in self._static_exclusions and company.symbol in technical_symbols
        ]
        self.diagnostics.companies_after_market = len(market_survivors)

        # Shares are the only accounting input needed for the market-cap gate.
        # Loading this compact fact slice avoids reconstructing millions of facts
        # for companies that cannot enter the PIT peer/scoring cross-section.
        facts_started = time.perf_counter()
        grouped_shares: dict[str, list[FundamentalFact]] = defaultdict(list)
        share_symbols = [
            *market_survivors,
            *[
                symbol
                for symbol in reit_symbols
                if any(not reasons for reasons in self._cheap_exclusions.get(symbol, {}).values())
            ],
        ]
        for batch in self.database.iter_fact_batches(
            share_symbols, self.end, metrics=("shares_outstanding",)
        ):
            self.diagnostics.sqlite_query_count += 1
            self.diagnostics.facts_rows_loaded += len(batch)
            for fact in batch:
                grouped_shares[fact.symbol].append(fact)
        self._shares = dict(grouped_shares)
        fundamental_survivors = [
            symbol
            for symbol in market_survivors
            if _has_any_market_cap_survivor(
                self._market[symbol], self._shares.get(symbol, []), self.config
            )
        ]
        grouped_facts: dict[str, list[FundamentalFact]] = defaultdict(list)
        # Four years covers the eight sequential quarters required for current
        # and prior-year TTM construction, including SEC cumulative periods and
        # a generous filing lag. The recent-share gate above guarantees that a
        # stale issuer cannot become eligible solely from this bounded stream.
        fundamental_start = self.start - timedelta(days=4 * 366)
        for batch in self.database.iter_fact_batches(
            fundamental_survivors,
            self.end,
            period_end_on_or_after=fundamental_start,
            retain_latest_periods=24,
        ):
            self.diagnostics.sqlite_query_count += 1
            self.diagnostics.facts_rows_loaded += len(batch)
            for fact in batch:
                grouped_facts[fact.symbol].append(fact)
        self.diagnostics.facts_load_seconds = time.perf_counter() - facts_started
        self.diagnostics.estimated_fact_memory_bytes = self.diagnostics.facts_rows_loaded * 320
        self._facts = {symbol: _AccountingCache(facts) for symbol, facts in grouped_facts.items()}
        self.diagnostics.companies_after_fundamental = len(fundamental_survivors)
        self._companies = safe
        self.diagnostics.feature_prepare_seconds = time.perf_counter() - started

    def screen(self, session: date) -> ScreenReport:
        started = time.perf_counter()
        prepared = [self._candidate(company, session) for company in self._companies]
        score_started = time.perf_counter()
        report = self.screener.report_from_prepared(
            prepared,
            self._conflicted,
            requested_as_of=session,
            market_session=session,
        )
        score_seconds = time.perf_counter() - score_started
        self.diagnostics.peer_score_seconds += score_seconds
        self.diagnostics.report_construction_seconds += score_seconds
        self.diagnostics.screen_seconds += time.perf_counter() - started
        self.diagnostics.sessions_screened += 1
        return report

    def _candidate(self, company: CompanyIdentity, session: date) -> _PreparedCandidate:
        static = self._static_exclusions.get(company.symbol)
        if static == "financial_excluded":
            return _empty_candidate(company, session, static)
        cheap_market = self._cheap_exclusions.get(company.symbol, {}).get(session)
        market = self._market.get(company.symbol, {}).get(session)
        if cheap_market is None:
            reasons = ["insufficient_market_history"]
            if static is not None:
                reasons.append(static)
            return _empty_candidate(company, session, *reasons)
        if static == "reit_excluded":
            reasons = list(cheap_market)
            if not reasons and (
                (shares := shares_outstanding_as_of(self._shares.get(company.symbol, []), session))
                is None
                or market is None
                or market.price * shares.value < Decimal(str(self.config.universe.min_market_cap))
            ):
                reasons.append("missing_or_small_market_cap")
            reasons.append(static)
            return _empty_candidate(company, session, *reasons)
        if cheap_market:
            return _PreparedCandidate(
                company=company,
                fundamentals=FundamentalMetrics(),
                technical=market.technical if market else TechnicalSnapshot(),
                average_dollar_volume_20d=market.average_dollar_volume_20d if market else None,
                base_exclusions=cheap_market,
                data_warnings=[],
                analysis_date=session,
                fundamentals_evaluated=False,
            )
        if market is None:
            return _empty_candidate(company, session, "insufficient_market_history")
        selected_shares = shares_outstanding_as_of(self._shares.get(company.symbol, []), session)
        if selected_shares is None or market.price * selected_shares.value < Decimal(
            str(self.config.universe.min_market_cap)
        ):
            return _PreparedCandidate(
                company=company,
                fundamentals=FundamentalMetrics(),
                technical=market.technical,
                average_dollar_volume_20d=market.average_dollar_volume_20d,
                base_exclusions=["missing_or_small_market_cap"],
                data_warnings=[],
                analysis_date=market.technical.market_session or session,
                fundamentals_evaluated=False,
            )
        cache = self._facts.get(company.symbol)
        if cache is None:
            return _PreparedCandidate(
                company=company,
                fundamentals=FundamentalMetrics(),
                technical=market.technical,
                average_dollar_volume_20d=market.average_dollar_volume_20d,
                base_exclusions=["no_point_in_time_fundamentals", "missing_or_small_market_cap"],
                data_warnings=[],
                analysis_date=session,
            )
        fundamentals = cache.metrics(session, market.price, self.diagnostics)
        exclusions = self.screener._universe_exclusions(  # noqa: SLF001
            _universe_snapshot(company, market, fundamentals), market.history_count
        )
        base_exclusions = list(exclusions)
        if _eligible_fact_count(cache.facts, session) == 0:
            base_exclusions.append("no_point_in_time_fundamentals")
        analysis_date = market.technical.market_session or session
        if analysis_date != session:
            base_exclusions.append("stale_market_data")
        return _PreparedCandidate(
            company=company,
            fundamentals=fundamentals,
            technical=market.technical,
            average_dollar_volume_20d=market.average_dollar_volume_20d,
            base_exclusions=base_exclusions,
            data_warnings=_missing_metric_warnings(fundamentals, market.technical),
            analysis_date=analysis_date,
        )


def _cheap_market_features(
    bars: list[_HistoricalBar], sessions: list[date], config: StrategyConfig
) -> dict[date, _MarketFeature]:
    """Prepare only rolling values needed to reject the large illiquid cross-section."""

    output: dict[date, _MarketFeature] = {}
    position = -1
    for session in sessions:
        while position + 1 < len(bars) and bars[position + 1].session <= session:
            position += 1
        if position < 0:
            continue
        bar = bars[position]
        recent = bars[max(0, position - 19) : position + 1]
        average_price = (
            _average_decimal([item.close for item in recent]) if len(recent) == 20 else None
        )
        average_volume = (
            _average_decimal([Decimal(item.volume) for item in recent])
            if len(recent) == 20
            else None
        )
        output[session] = _MarketFeature(
            technical=TechnicalSnapshot(
                market_session=bar.session,
                price=float(bar.close),
            ),
            price=bar.close,
            average_price_20d=average_price,
            average_volume_20d=average_volume,
            average_dollar_volume_20d=(
                float(average_price * average_volume)
                if average_price is not None and average_volume is not None
                else None
            ),
            history_count=min(position + 1, config.universe.market_data_days),
        )
    return output


def _exact_market_features(
    bars: list[_HistoricalBar],
    cheap: dict[date, _MarketFeature],
    config: StrategyConfig,
) -> dict[date, _MarketFeature]:
    """Calculate the canonical technical snapshot once for each requested PIT prefix."""

    output: dict[date, _MarketFeature] = {}
    position = -1
    for session, market in cheap.items():
        while position + 1 < len(bars) and bars[position + 1].session <= session:
            position += 1
        if position < 0:
            continue
        first = max(0, position + 1 - config.universe.market_data_days)
        prefix = bars[first : position + 1]
        technical = _fast_technical_snapshot(prefix, config).model_copy(
            update={"market_session": bars[position].session}
        )
        output[session] = _MarketFeature(
            technical=technical,
            price=market.price,
            average_price_20d=market.average_price_20d,
            average_volume_20d=market.average_volume_20d,
            average_dollar_volume_20d=market.average_dollar_volume_20d,
            history_count=market.history_count,
        )
    return output


def _fast_technical_snapshot(
    bars: list[_HistoricalBar] | list[Any], config: StrategyConfig
) -> TechnicalSnapshot:
    """Last-row equivalent of ``technical_snapshot`` without per-symbol DataFrames."""

    if not bars:
        return TechnicalSnapshot()
    close = np.fromiter((float(bar.close) for bar in bars), dtype=float)
    high = np.fromiter((float(bar.high) for bar in bars), dtype=float)
    low = np.fromiter((float(bar.low) for bar in bars), dtype=float)
    volume = np.fromiter((float(bar.volume) for bar in bars), dtype=float)
    rules = config.technical
    rsi_values = _wilder_rsi(close, 14)
    rsi_now = _last_finite(rsi_values)
    rsi_previous = _value_at(rsi_values, len(rsi_values) - 2)
    recent_rsi = rsi_values[-(rules.rsi_recovery_lookback + 1) : -1]
    recent_rsi = recent_rsi[np.isfinite(recent_rsi)]
    sma20 = _trailing_mean(close, 20)
    sma20_prior = _trailing_mean(close, 20, end=len(close) - rules.sma_slope_lookback)
    momentum20 = _momentum_at(close, 20, len(close) - 1)
    momentum20_prior = _momentum_at(close, 20, len(close) - 6)
    true_ranges = np.maximum.reduce(
        (
            high - low,
            np.abs(high - np.concatenate(([np.nan], close[:-1]))),
            np.abs(low - np.concatenate(([np.nan], close[:-1]))),
        )
    )
    true_ranges[0] = high[0] - low[0]
    atr_values = _wilder_average_array(true_ranges, 14)
    returns = close[1:] / close[:-1] - 1 if len(close) > 1 else np.array([])
    volatility = (
        float(np.std(returns[-20:], ddof=1) * math.sqrt(252)) if len(returns) >= 20 else None
    )
    relative_volume = (
        float(volume[-1] / np.mean(volume[-21:-1]))
        if len(volume) >= 21 and np.mean(volume[-21:-1]) != 0
        else None
    )
    return TechnicalSnapshot(
        price=float(close[-1]),
        sma20=sma20,
        sma50=_trailing_mean(close, 50),
        sma200=_trailing_mean(close, 200),
        sma20_rising=(sma20 > sma20_prior)
        if sma20 is not None and sma20_prior is not None
        else None,
        rsi14=rsi_now,
        rsi_recovery=(
            bool(
                np.any(recent_rsi < rules.rsi_oversold)
                and rules.rsi_recovery_min < rsi_now <= rules.rsi_recovery_max
                and rsi_now > rsi_previous
            )
            if rsi_now is not None and rsi_previous is not None
            else None
        ),
        momentum5=_momentum_at(close, 5, len(close) - 1),
        momentum20=momentum20,
        momentum20_improving=(momentum20 > momentum20_prior)
        if momentum20 is not None and momentum20_prior is not None
        else None,
        momentum63=_momentum_at(close, 63, len(close) - 1),
        momentum126=_momentum_at(close, 126, len(close) - 1),
        volatility=volatility,
        atr14=_last_finite(atr_values),
        relative_volume=relative_volume,
        drawdown_52w=(float(close[-1] / np.max(close[-252:]) - 1)) if len(close) >= 252 else None,
    )


def _wilder_average_array(values: np.ndarray, period: int) -> np.ndarray:
    output = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return output
    previous = float(np.mean(values[:period]))
    output[period - 1] = previous
    for position in range(period, len(values)):
        current = values[position]
        previous = (previous * (period - 1) + current) / period
        output[position] = previous
    return output


def _wilder_rsi(close: np.ndarray, period: int) -> np.ndarray:
    output = np.full(len(close), np.nan, dtype=float)
    if len(close) <= period:
        return output
    changes = np.diff(close)
    gains = np.maximum(changes, 0)
    losses = np.maximum(-changes, 0)
    average_gain = _wilder_average_array(gains, period)
    average_loss = _wilder_average_array(losses, period)
    for offset in range(period - 1, len(changes)):
        gain = average_gain[offset]
        loss = average_loss[offset]
        position = offset + 1
        if gain == 0 and loss == 0:
            output[position] = 50.0
        elif loss == 0:
            output[position] = 100.0
        else:
            output[position] = 100 - 100 / (1 + gain / loss)
    return output


def _trailing_mean(values: np.ndarray, period: int, *, end: int | None = None) -> float | None:
    end = len(values) if end is None else end
    if end < period:
        return None
    return float(np.mean(values[end - period : end]))


def _momentum_at(values: np.ndarray, period: int, position: int) -> float | None:
    earlier = position - period
    if position < 0 or earlier < 0:
        return None
    return float(values[position] / values[earlier] - 1)


def _last_finite(values: np.ndarray) -> float | None:
    return _value_at(values, len(values) - 1)


def _value_at(values: np.ndarray, position: int) -> float | None:
    if position < 0 or position >= len(values) or not math.isfinite(values[position]):
        return None
    return float(values[position])


def _cheap_market_exclusions(
    market: _MarketFeature, config: StrategyConfig, session: date
) -> list[str]:
    exclusions: list[str] = []
    if market.history_count < config.data_quality.min_market_history_days:
        exclusions.append("insufficient_market_history")
    if market.price < Decimal(str(config.universe.min_price)):
        exclusions.append("invalid_or_low_price")
    if (
        market.average_dollar_volume_20d is None
        or market.average_dollar_volume_20d < config.universe.min_avg_dollar_volume_20d
    ):
        exclusions.append("insufficient_liquidity")
    if market.technical.market_session != session:
        exclusions.append("stale_market_data")
    return exclusions


def _has_any_market_cap_survivor(
    features: dict[date, _MarketFeature],
    share_facts: list[FundamentalFact],
    config: StrategyConfig,
) -> bool:
    minimum = Decimal(str(config.universe.min_market_cap))
    return any(
        (shares := shares_outstanding_as_of(share_facts, session)) is not None
        and market.price * shares.value >= minimum
        for session, market in features.items()
        if not _cheap_market_exclusions(market, config, session)
    )


def _universe_snapshot(
    company: CompanyIdentity,
    market: _MarketFeature,
    fundamentals: FundamentalMetrics | None,
):
    from trading_system.data.universe import UniverseSnapshot

    shares = None
    if fundamentals and fundamentals.market_cap is not None and market.price > 0:
        shares = fundamentals.market_cap / market.price
    return UniverseSnapshot(
        symbol=company.symbol,
        latest_price=market.price,
        average_price_20d=market.average_price_20d,
        average_volume_20d=market.average_volume_20d,
        shares_outstanding=shares,
        sic=company.sic,
    )


def _empty_candidate(
    company: CompanyIdentity, session: date, *exclusions: str
) -> _PreparedCandidate:
    return _PreparedCandidate(
        company=company,
        fundamentals=FundamentalMetrics(),
        technical=TechnicalSnapshot(),
        average_dollar_volume_20d=None,
        base_exclusions=list(exclusions),
        data_warnings=[],
        analysis_date=session,
        fundamentals_evaluated=False,
    )


def _eligible_fact_count(facts: list[FundamentalFact], session: date) -> int:
    low, high = 0, len(facts)
    while low < high:
        middle = (low + high) // 2
        if facts[middle].filed <= session:
            low = middle + 1
        else:
            high = middle
    return low


def _average_decimal(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None
