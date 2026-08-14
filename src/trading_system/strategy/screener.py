"""Point-in-time daily screening, hard filters and explainable reports."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    effective_trading_session,
    full_history_request_window,
)
from trading_system.data.universe import (
    UniverseSnapshot,
    is_financial_or_reit,
    is_reit,
    passes_universe_filters,
)
from trading_system.fundamentals.peers import assign_peer_groups, peer_diagnostics
from trading_system.fundamentals.quality import analyze_fundamentals
from trading_system.models.fundamentals import CompanyIdentity, FundamentalMetrics
from trading_system.models.market_data import DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import MarketDebug, PeerDebug, ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot
from trading_system.strategy.scoring import (
    combine_scores,
    score_opportunity,
    score_quality,
    score_timing,
    score_valuation,
)
from trading_system.technical.momentum import technical_snapshot

LOGGER = logging.getLogger(__name__)
PEER_VALUE_METRICS = (
    "revenue_growth",
    "eps_growth",
    "operating_cash_flow_growth",
    "operating_margin",
    "roic",
    "debt_to_ebitda",
    "fcf_yield",
)
INDUSTRY_METRICS = (
    "pe",
    "ev_to_ebitda",
    "ev_to_ebit",
    "operating_margin",
    "roic",
    "revenue_growth",
)


@dataclass
class _PreparedCandidate:
    company: CompanyIdentity
    fundamentals: FundamentalMetrics
    technical: TechnicalSnapshot
    average_dollar_volume_20d: float | None
    base_exclusions: list[str]
    data_warnings: list[str]
    analysis_date: date
    fundamentals_evaluated: bool = True


class Screener:
    """Calculate an explainable cross-sectional screen from local point-in-time data."""

    def __init__(self, database: Database, config: StrategyConfig) -> None:
        self.database = database
        self.config = config

    def run(
        self,
        as_of: date,
        *,
        now: datetime | None = None,
        use_market_snapshots: bool = True,
    ) -> ScreenReport:
        market_session = effective_trading_session(as_of, now)
        companies = self.database.list_tradable_companies()
        identity_conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        safe_companies = [
            company for company in companies if company.symbol not in identity_conflicts
        ]
        conflicted_companies = [
            company for company in companies if company.symbol in identity_conflicts
        ]
        with self.database.read_only() as connection:
            prepared = [
                self._prepare(
                    company,
                    market_session,
                    use_market_snapshots=use_market_snapshots,
                    connection=connection,
                )
                for company in safe_companies
            ]
        return self.report_from_prepared(
            prepared,
            conflicted_companies,
            requested_as_of=as_of,
            market_session=market_session,
        )

    def report_from_prepared(
        self,
        prepared: list[_PreparedCandidate],
        conflicted_companies: list[CompanyIdentity],
        *,
        requested_as_of: date,
        market_session: date,
    ) -> ScreenReport:
        """Score a prepared PIT cross-section using the canonical peer/ranking logic."""

        peer_table = self._peer_table(prepared)
        records = [self._score(candidate, peer_table) for candidate in prepared]
        records.extend(
            self._identity_conflict_record(company, market_session)
            for company in conflicted_companies
        )

        eligible = sorted(
            (record for record in records if record.eligible),
            key=lambda record: (
                -(record.scores.total or 0),
                -(record.scores.quality.score or 0),
                -(record.scores.valuation.score or 0),
                record.symbol,
            ),
        )
        ranks = {record.symbol: index for index, record in enumerate(eligible, start=1)}
        ranked = [
            record.model_copy(update={"rank": ranks.get(record.symbol)}) for record in records
        ]
        ranked.sort(
            key=lambda record: (
                record.rank is None,
                record.rank or 0,
                -(record.scores.total or -1),
                record.symbol,
            )
        )
        return ScreenReport(
            as_of=market_session,
            requested_as_of=requested_as_of,
            effective_market_session=market_session,
            generated_at=datetime.now(UTC).isoformat(),
            analyzed_count=len(ranked),
            eligible_count=len(eligible),
            identity_conflicts_excluded=len(conflicted_companies),
            identity_conflict_sample=tuple(
                sorted(company.symbol for company in conflicted_companies)[:10]
            ),
            records=tuple(ranked),
        )

    def debug_peers(
        self, symbol: str, as_of: date, *, now: datetime | None = None
    ) -> PeerDebug | None:
        market_session = effective_trading_session(as_of, now)
        identity_conflicts = self.database.unresolved_sec_identity_conflict_symbols()
        prepared = [
            self._prepare(company, market_session)
            for company in self.database.list_tradable_companies()
            if company.symbol not in identity_conflicts
        ]
        candidate = next((item for item in prepared if item.company.symbol == symbol.upper()), None)
        if candidate is None:
            return None
        frame = self._peer_table(prepared)
        return peer_diagnostics(
            frame,
            candidate.company.symbol,
            candidate.company.sic,
            self.config.peers.min_peer_count,
        )

    def _identity_conflict_record(
        self, company: CompanyIdentity, market_session: date
    ) -> ScreenRecord:
        return ScreenRecord(
            symbol=company.symbol,
            name=company.name,
            as_of=market_session,
            sic=company.sic,
            eligible=False,
            exclusion_reasons=("identity_conflict",),
            data_warnings=("unresolved_current_issuer_identity",),
            fundamentals=FundamentalMetrics(),
            technical=TechnicalSnapshot(),
            scores=StockScores(
                quality=_unavailable_score("quality"),
                valuation=_unavailable_score("valuation"),
                opportunity=_unavailable_score("opportunity"),
                timing=_unavailable_score("timing"),
            ),
        )

    def debug_market(self, symbol: str, as_of: date, *, now: datetime | None = None) -> MarketDebug:
        market_session = effective_trading_session(as_of, now)
        request_start, request_end = full_history_request_window(
            market_session, self.config.universe.market_data_days
        )
        bars = self.database.bars_available_as_of(
            symbol.upper(), market_session, limit=self.config.universe.market_data_days
        )
        technical = technical_snapshot(_bar_frame(bars), self.config.technical).model_copy(
            update={"market_session": bars[-1].timestamp.date() if bars else None}
        )
        closes = [float(bar.close) for bar in bars[-252:]]
        prior_volumes = [bar.volume for bar in bars[-21:-1]]
        return MarketDebug(
            symbol=symbol.upper(),
            requested_as_of=as_of,
            effective_market_session=market_session,
            actual_latest_bar_session=bars[-1].timestamp.date() if bars else None,
            requested_alpaca_start=request_start,
            requested_alpaca_end_exclusive=request_end,
            feed=self.config.universe.market_data_feed,
            adjustment=self.config.universe.market_data_adjustment,
            bar_count=len(bars),
            last_bars=tuple(bars[-10:]),
            latest_completed_close=technical.price,
            sma20=technical.sma20,
            sma50=technical.sma50,
            sma200=technical.sma200,
            rsi14=technical.rsi14,
            momentum5=technical.momentum5,
            momentum20=technical.momentum20,
            momentum63=technical.momentum63,
            high_52w=max(closes) if len(closes) == 252 else None,
            drawdown_52w=technical.drawdown_52w,
            atr14=technical.atr14,
            average_volume20=(sum(prior_volumes) / 20 if len(prior_volumes) == 20 else None),
            relative_volume=technical.relative_volume,
        )

    def _prepare(
        self,
        company: CompanyIdentity,
        market_session: date,
        *,
        use_market_snapshots: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> _PreparedCandidate:
        try:
            bars = self.database.bars_available_as_of(
                company.symbol,
                market_session,
                limit=self.config.universe.market_data_days,
                connection=connection,
            )
            analysis_date = bars[-1].timestamp.date() if bars else market_session
            facts = self.database.facts_available_as_of(
                company.symbol, analysis_date, connection=connection
            )
            price = bars[-1].close if bars else None
            market_snapshot = (
                self.database.latest_market_snapshot(company.symbol)
                if use_market_snapshots
                else None
            )
            snapshot_price_selected = False
            if (
                market_snapshot is not None
                and market_snapshot.latest_trade_price is not None
                and market_snapshot.latest_trade_timestamp is not None
                and market_snapshot.latest_trade_timestamp.date() == analysis_date
            ):
                # A snapshot may contain an intraday trade from a later session.
                # Only a trade from the completed analysis session is point-in-time safe.
                price = market_snapshot.latest_trade_price
                snapshot_price_selected = True
            fundamentals = analyze_fundamentals(facts, analysis_date, price)
            technical = technical_snapshot(_bar_frame(bars), self.config.technical)
            technical_updates: dict[str, Any] = {"market_session": analysis_date if bars else None}
            if snapshot_price_selected and price is not None:
                closes = [bar.close for bar in bars[-252:]]
                technical_updates["price"] = float(price)
                technical_updates["drawdown_52w"] = (
                    float(price / max(closes) - Decimal(1)) if len(closes) == 252 else None
                )
            technical = technical.model_copy(update=technical_updates)
            average_dollar_volume = _average_dollar_volume(bars)
            snapshot = UniverseSnapshot(
                symbol=company.symbol,
                latest_price=price,
                average_price_20d=_average_decimal([bar.close for bar in bars[-20:]]),
                average_volume_20d=_average_decimal([Decimal(bar.volume) for bar in bars[-20:]]),
                shares_outstanding=(
                    fundamentals.market_cap / price
                    if fundamentals.market_cap is not None and price is not None and price > 0
                    else None
                ),
                sic=company.sic,
            )
            exclusions = self._universe_exclusions(snapshot, len(bars))
            if bars and analysis_date != market_session:
                exclusions.append("stale_market_data")
            warnings = _missing_metric_warnings(fundamentals, technical)
            if not facts:
                exclusions.append("no_point_in_time_fundamentals")
            return _PreparedCandidate(
                company=company,
                fundamentals=fundamentals,
                technical=technical,
                average_dollar_volume_20d=average_dollar_volume,
                base_exclusions=exclusions,
                data_warnings=warnings,
                analysis_date=analysis_date,
            )
        except Exception as exc:
            LOGGER.exception("Candidate analysis failed symbol=%s", company.symbol)
            return _PreparedCandidate(
                company=company,
                fundamentals=FundamentalMetrics(),
                technical=TechnicalSnapshot(),
                average_dollar_volume_20d=None,
                base_exclusions=["analysis_error"],
                data_warnings=[f"analysis_error:{type(exc).__name__}"],
                analysis_date=market_session,
            )

    def _universe_exclusions(self, snapshot: UniverseSnapshot, history_count: int) -> list[str]:
        exclusions: list[str] = []
        universe = self.config.universe
        if history_count < self.config.data_quality.min_market_history_days:
            exclusions.append("insufficient_market_history")
        if snapshot.latest_price is None or snapshot.latest_price < Decimal(
            str(universe.min_price)
        ):
            exclusions.append("invalid_or_low_price")
        if snapshot.estimated_market_cap is None or snapshot.estimated_market_cap < Decimal(
            str(universe.min_market_cap)
        ):
            exclusions.append("missing_or_small_market_cap")
        if (
            snapshot.average_dollar_volume_20d is None
            or snapshot.average_dollar_volume_20d < Decimal(str(universe.min_avg_dollar_volume_20d))
        ):
            exclusions.append("insufficient_liquidity")
        if universe.exclude_reits and is_reit(snapshot.sic):
            exclusions.append("reit_excluded")
        elif universe.exclude_financials and is_financial_or_reit(snapshot.sic):
            exclusions.append("financial_excluded")
        if not passes_universe_filters(snapshot, universe) and not exclusions:
            exclusions.append("universe_filter_failed")
        return exclusions

    def _peer_table(self, prepared: list[_PreparedCandidate]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for candidate in prepared:
            if candidate.base_exclusions:
                continue
            row = {
                "symbol": candidate.company.symbol,
                "sic": candidate.company.sic,
                **candidate.fundamentals.model_dump(mode="python"),
            }
            rows.append(row)
        columns = list(dict.fromkeys(("symbol", "sic", *INDUSTRY_METRICS, *PEER_VALUE_METRICS)))
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=[*columns, "peer_group"])
        output = assign_peer_groups(frame, self.config.peers.min_peer_count)
        for sic in output["sic_normalized"].dropna().unique():
            diagnostic = peer_diagnostics(output, "*", str(sic), self.config.peers.min_peer_count)
            logged_group = diagnostic.selected_group or f"sic2:{str(sic)[:2]} (insufficient)"
            logged_count = (
                diagnostic.selected_peer_count
                if diagnostic.selected_group
                else diagnostic.two_digit_peer_count
            )
            LOGGER.debug(
                "Peer group sic=%s group=%s companies=%d valid_pe=%d "
                "valid_ev_ebitda=%d median_pe=%s median_ev_ebitda=%s",
                sic,
                logged_group,
                logged_count,
                diagnostic.valid_pe_count,
                diagnostic.valid_ev_ebitda_count,
                diagnostic.median_pe,
                diagnostic.median_ev_ebitda,
            )
        return output

    def _score(self, candidate: _PreparedCandidate, peer_table: pd.DataFrame) -> ScreenRecord:
        peer_row = peer_table.loc[peer_table["symbol"] == candidate.company.symbol]
        peer_group = None if peer_row.empty else _optional_string(peer_row.iloc[0]["peer_group"])
        group = (
            peer_table.loc[peer_table["peer_group"] == peer_group]
            if peer_group is not None
            else peer_table.iloc[0:0]
        )
        peer_values: dict[str, list[float | None]] = {}
        for metric in PEER_VALUE_METRICS:
            values = _optional_float_list(group[metric]) if metric in group.columns else []
            valid_count = sum(value is not None for value in values)
            peer_values[metric] = values if valid_count >= self.config.peers.min_peer_count else []
        industry_medians = {
            metric: _optional_float(peer_row.iloc[0].get(f"industry_median_{metric}"))
            if not peer_row.empty
            else None
            for metric in INDUSTRY_METRICS
        }
        scores = self._scores(candidate, peer_values, industry_medians)
        exclusions = list(candidate.base_exclusions)
        if candidate.fundamentals_evaluated:
            exclusions.extend(self._hard_filter_exclusions(candidate.fundamentals, scores))
        if peer_group is None:
            candidate.data_warnings.append("insufficient_peer_group")
        eligible = not exclusions
        if scores.total is not None:
            LOGGER.debug(
                "Candidate %s score=%.1f eligible=%s",
                candidate.company.symbol,
                scores.total,
                eligible,
            )
        return ScreenRecord(
            symbol=candidate.company.symbol,
            name=candidate.company.name,
            as_of=candidate.analysis_date,
            sic=candidate.company.sic,
            peer_group=peer_group,
            eligible=eligible,
            exclusion_reasons=tuple(dict.fromkeys(exclusions)),
            data_warnings=tuple(dict.fromkeys(candidate.data_warnings)),
            average_dollar_volume_20d=candidate.average_dollar_volume_20d,
            industry_medians=industry_medians,
            fundamentals=candidate.fundamentals,
            technical=candidate.technical,
            scores=scores,
        )

    def _scores(
        self,
        candidate: _PreparedCandidate,
        peer_values: dict[str, list[float | None]],
        industry_medians: dict[str, float | None],
    ) -> StockScores:
        data_quality = self.config.data_quality
        score_config = self.config.scores
        quality = score_quality(
            candidate.fundamentals,
            peer_values,
            score_config,
            min_available=data_quality.min_available_quality_metrics,
        )
        valuation = score_valuation(
            candidate.fundamentals,
            industry_medians,
            peer_values,
            score_config,
            min_available=data_quality.min_available_valuation_metrics,
        )
        opportunity = score_opportunity(candidate.technical, score_config)
        timing = score_timing(candidate.technical, score_config, self.config.technical)
        return combine_scores(quality, valuation, opportunity, timing, score_config)

    def _hard_filter_exclusions(
        self, fundamentals: FundamentalMetrics, scores: StockScores
    ) -> list[str]:
        filters = self.config.filters
        exclusions: list[str] = []
        if scores.quality.score is None:
            exclusions.append("quality_score_unavailable")
        elif scores.quality.score < filters.min_quality_score:
            exclusions.append("quality_score_below_minimum")
        if scores.valuation.score is None:
            exclusions.append("valuation_score_unavailable")
        elif scores.valuation.score < filters.min_valuation_score:
            exclusions.append("valuation_score_below_minimum")
        if scores.total is None:
            exclusions.append("total_score_unavailable")
        elif scores.total < filters.min_total_score:
            exclusions.append("total_score_below_minimum")
        if filters.require_positive_ocf and fundamentals.operating_cash_flow_positive is not True:
            exclusions.append("positive_operating_cash_flow_required")
        return exclusions


def _bar_frame(bars: list[DailyBar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.DataFrame(
        {
            "open": [float(bar.open) for bar in bars],
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [bar.volume for bar in bars],
        },
        index=pd.DatetimeIndex([bar.timestamp for bar in bars]),
    )


def _unavailable_score(name: str) -> ScoreBreakdown:
    return ScoreBreakdown(
        name=name,
        factors=(),
        available_factor_count=0,
        reason_score_unavailable="identity_conflict",
    )


def _average_decimal(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _average_dollar_volume(bars: list[DailyBar]) -> float | None:
    recent = bars[-20:]
    if len(recent) < 20:
        return None
    average_price = _average_decimal([bar.close for bar in recent])
    average_volume = _average_decimal([Decimal(bar.volume) for bar in recent])
    return float(average_price * average_volume)  # type: ignore[operator]


def _optional_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _optional_float_list(values: pd.Series) -> list[float | None]:
    return [_optional_float(value) for value in values]


def _optional_string(value: object) -> str | None:
    return None if value is None or pd.isna(value) else str(value)


def _missing_metric_warnings(
    fundamentals: FundamentalMetrics, technical: TechnicalSnapshot
) -> list[str]:
    warnings: list[str] = []
    for name, value in fundamentals.model_dump().items():
        if value is None:
            warnings.append(f"missing_fundamental:{name}")
    for name, value in technical.model_dump().items():
        if value is None:
            warnings.append(f"missing_technical:{name}")
    return warnings
