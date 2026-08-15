"""Explainable 0–100 scoring with robust peers and missing-weight redistribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from trading_system.config import ScoreConfig, TechnicalConfig
from trading_system.fundamentals.peers import relative_multiple
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.scores import FactorScore, ScoreBreakdown, StockScores
from trading_system.models.signals import TechnicalSnapshot


def clip_score(value: float) -> float:
    return min(100.0, max(0.0, float(value)))


def piecewise_score(value: float | None, curve: Sequence[tuple[float, float]]) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    x_values = [point[0] for point in curve]
    y_values = [point[1] for point in curve]
    return clip_score(float(np.interp(value, x_values, y_values)))


def winsorize(
    values: Sequence[float | None], lower_quantile: float = 0.05, upper_quantile: float = 0.95
) -> pd.Series:
    series = pd.Series(values, dtype=float)
    valid = series.dropna()
    if valid.empty:
        return series
    lower, upper = valid.quantile([lower_quantile, upper_quantile])
    return series.clip(lower=lower, upper=upper)


def percentile_score(
    value: float | None,
    peers: Sequence[float | None],
    *,
    higher_is_better: bool = True,
    lower_quantile: float = 0.05,
    upper_quantile: float = 0.95,
) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    clean = winsorize(peers, lower_quantile, upper_quantile).dropna()
    if clean.empty:
        return None
    lower, upper = clean.min(), clean.max()
    clipped = min(float(upper), max(float(lower), value))
    less = int((clean < clipped).sum())
    equal = int((clean == clipped).sum())
    percentile = 100 * (less + 0.5 * equal) / len(clean)
    return clip_score(percentile if higher_is_better else 100 - percentile)


def _breakdown(
    name: str,
    factors: list[tuple[str, float | bool | None, float | None, float, str]],
    *,
    min_available: int = 1,
) -> ScoreBreakdown:
    available = [factor for factor in factors if factor[2] is not None]
    denominator = sum(factor[3] for factor in available)
    can_score = len(available) >= min_available and denominator > 0
    unavailable_reason = None
    if len(available) < min_available:
        unavailable_reason = (
            f"requires at least {min_available} available factors; found {len(available)}"
        )
    elif denominator <= 0:
        unavailable_reason = "available factor weights sum to zero"
    output: list[FactorScore] = []
    weighted_score = 0.0
    for factor_name, raw, score, configured_weight, explanation in factors:
        normalized_available_weight = (
            configured_weight / denominator if denominator > 0 and score is not None else 0.0
        )
        effective_weight = normalized_available_weight if can_score else 0.0
        if score is not None:
            weighted_score += score * effective_weight
        output.append(
            FactorScore(
                name=factor_name,
                raw_value=raw,
                score=score,
                configured_weight=configured_weight,
                normalized_available_weight=normalized_available_weight,
                effective_weight=effective_weight,
                explanation=explanation,
            )
        )
    return ScoreBreakdown(
        name=name,
        score=clip_score(weighted_score) if can_score else None,
        factors=tuple(output),
        available_factor_count=len(available),
        minimum_required_factor_count=min_available,
        reason_score_unavailable=unavailable_reason,
    )


def _peer_score(
    value: float | None,
    peer_values: Mapping[str, Sequence[float | None]],
    metric: str,
    config: ScoreConfig,
    *,
    higher_is_better: bool = True,
) -> float | None:
    return percentile_score(
        value,
        peer_values.get(metric, ()),
        higher_is_better=higher_is_better,
        lower_quantile=config.winsor_lower_quantile,
        upper_quantile=config.winsor_upper_quantile,
    )


def _blended_absolute_peer_score(
    value: float | None,
    peers: Mapping[str, Sequence[float | None]],
    metric: str,
    curve: Sequence[tuple[float, float]],
    config: ScoreConfig,
) -> float | None:
    absolute = piecewise_score(value, curve)
    peer = _peer_score(value, peers, metric, config)
    available = [score for score in (absolute, peer) if score is not None]
    return sum(available) / len(available) if available else None


def score_quality(
    metrics: FundamentalMetrics,
    peer_values: Mapping[str, Sequence[float | None]],
    config: ScoreConfig,
    *,
    min_available: int = 4,
) -> ScoreBreakdown:
    weights = config.quality
    revenue_score = _blended_absolute_peer_score(
        metrics.revenue_growth,
        peer_values,
        "revenue_growth",
        config.revenue_growth_curve,
        config,
    )
    eps_score = _blended_absolute_peer_score(
        metrics.eps_growth,
        peer_values,
        "eps_growth",
        config.eps_growth_curve,
        config,
    )
    if metrics.operating_cash_flow_positive is False:
        ocf_score = 0.0
    elif metrics.operating_cash_flow_positive is True:
        ocf_peer_score = _peer_score(
            metrics.operating_cash_flow_growth, peer_values, "operating_cash_flow_growth", config
        )
        ocf_absolute_score = piecewise_score(
            metrics.operating_cash_flow_growth, config.operating_cash_flow_growth_curve
        )
        available_ocf_scores = [
            score for score in (ocf_absolute_score, ocf_peer_score) if score is not None
        ]
        ocf_score = (
            sum(available_ocf_scores) / len(available_ocf_scores) if available_ocf_scores else 50.0
        )
    else:
        ocf_score = None
    margin_score = _blended_absolute_peer_score(
        metrics.operating_margin,
        peer_values,
        "operating_margin",
        config.operating_margin_curve,
        config,
    )
    roic_score = _blended_absolute_peer_score(
        metrics.roic, peer_values, "roic", config.roic_curve, config
    )
    debt_absolute = piecewise_score(metrics.debt_to_ebitda, config.debt_to_ebitda_curve)
    debt_peer = _peer_score(
        metrics.debt_to_ebitda, peer_values, "debt_to_ebitda", config, higher_is_better=False
    )
    debt_available = [score for score in (debt_absolute, debt_peer) if score is not None]
    debt_score = sum(debt_available) / len(debt_available) if debt_available else None
    return _breakdown(
        "quality",
        [
            (
                "revenue_growth",
                metrics.revenue_growth,
                revenue_score,
                weights.revenue_growth,
                "Absolute growth curve; blended with peer percentile when available",
            ),
            (
                "eps_growth",
                metrics.eps_growth,
                eps_score,
                weights.eps_growth,
                "Absolute growth curve; blended with peers; non-positive bases are unavailable",
            ),
            (
                "operating_cash_flow",
                metrics.operating_cash_flow_growth,
                ocf_score,
                weights.operating_cash_flow,
                "Positive OCF required; absolute growth blended with peers when available",
            ),
            (
                "operating_margin",
                metrics.operating_margin,
                margin_score,
                weights.operating_margin,
                "Equal blend of absolute curve and peer percentile",
            ),
            (
                "roic",
                metrics.roic,
                roic_score,
                weights.roic,
                "Equal blend of absolute curve and peer percentile",
            ),
            (
                "debt_to_ebitda",
                metrics.debt_to_ebitda,
                debt_score,
                weights.debt_to_ebitda,
                "Absolute leverage curve plus inverse peer percentile",
            ),
        ],
        min_available=min_available,
    )


def score_valuation(
    metrics: FundamentalMetrics,
    industry_medians: Mapping[str, float | None],
    peer_values: Mapping[str, Sequence[float | None]],
    config: ScoreConfig,
    *,
    min_available: int = 2,
) -> ScoreBreakdown:
    weights = config.valuation
    relative_pe = relative_multiple(metrics.pe, industry_medians.get("pe"))
    relative_ev = relative_multiple(metrics.ev_to_ebitda, industry_medians.get("ev_to_ebitda"))
    multiple_basis = "EV/EBITDA"
    if relative_ev is None:
        relative_ev = relative_multiple(metrics.ev_to_ebit, industry_medians.get("ev_to_ebit"))
        multiple_basis = "EV/EBIT fallback"
    fcf_absolute = piecewise_score(metrics.fcf_yield, config.fcf_yield_curve)
    fcf_peer = _peer_score(metrics.fcf_yield, peer_values, "fcf_yield", config)
    available = [score for score in (fcf_absolute, fcf_peer) if score is not None]
    fcf_score = sum(available) / len(available) if available else None
    return _breakdown(
        "valuation",
        [
            (
                "relative_pe",
                relative_pe,
                piecewise_score(relative_pe, config.relative_multiple_curve),
                weights.relative_pe,
                "Company P/E divided by positive industry median P/E",
            ),
            (
                "relative_ev_ebitda",
                relative_ev,
                piecewise_score(relative_ev, config.relative_multiple_curve),
                weights.relative_ev_ebitda,
                f"Company {multiple_basis} divided by its positive industry median",
            ),
            (
                "fcf_yield",
                metrics.fcf_yield,
                fcf_score,
                weights.fcf_yield,
                "Absolute yield curve plus peer percentile",
            ),
        ],
        min_available=min_available,
    )


def score_opportunity(snapshot: TechnicalSnapshot, config: ScoreConfig) -> ScoreBreakdown:
    weights = config.opportunity
    medium_values = [
        value for value in (snapshot.momentum20, snapshot.momentum63) if value is not None
    ]
    medium_weakness = sum(medium_values) / len(medium_values) if medium_values else None
    return _breakdown(
        "opportunity",
        [
            (
                "drawdown_52w",
                snapshot.drawdown_52w,
                piecewise_score(snapshot.drawdown_52w, config.drawdown_curve),
                weights.drawdown_52w,
                "Nonlinear curve rewards roughly 20–35% drawdowns and penalizes collapse",
            ),
            (
                "medium_term_weakness",
                medium_weakness,
                piecewise_score(medium_weakness, config.medium_term_weakness_curve),
                weights.medium_term_weakness,
                "Mean 20d/63d return mapped to a configurable dislocation curve",
            ),
            (
                "volatility",
                snapshot.volatility,
                piecewise_score(snapshot.volatility, config.volatility_curve),
                weights.volatility,
                "Moderate volatility is useful; extreme volatility is penalized",
            ),
        ],
    )


def score_timing(
    snapshot: TechnicalSnapshot, config: ScoreConfig, rules: TechnicalConfig
) -> ScoreBreakdown:
    weights = config.timing
    if snapshot.rsi_recovery is True:
        rsi_score = 100.0
    elif snapshot.rsi_recovery is False and snapshot.rsi14 is not None:
        rsi_score = (
            35.0 if rules.rsi_recovery_min <= snapshot.rsi14 <= rules.rsi_recovery_max else 0.0
        )
    else:
        rsi_score = None
    ma_checks = [
        snapshot.price > snapshot.sma20
        if snapshot.price is not None and snapshot.sma20 is not None
        else None,
        snapshot.sma20_rising,
        snapshot.price > snapshot.sma50
        if snapshot.price is not None and snapshot.sma50 is not None
        else None,
    ]
    ma_weights = (0.5, 0.3, 0.2)
    ma_available = [
        (check, weight)
        for check, weight in zip(ma_checks, ma_weights, strict=True)
        if check is not None
    ]
    ma_score = (
        100
        * sum(weight for check, weight in ma_available if check)
        / sum(weight for _, weight in ma_available)
        if ma_available
        else None
    )
    momentum_checks = [
        snapshot.momentum5 > 0 if snapshot.momentum5 is not None else None,
        snapshot.momentum20_improving,
    ]
    momentum_available = [check for check in momentum_checks if check is not None]
    momentum_score = (
        100 * sum(bool(check) for check in momentum_available) / len(momentum_available)
        if momentum_available
        else None
    )
    return _breakdown(
        "timing",
        [
            (
                "rsi_recovery",
                snapshot.rsi_recovery,
                rsi_score,
                weights.rsi_recovery,
                "Oversold occurred recently and RSI recovered/rises; oversold alone scores zero",
            ),
            (
                "moving_average_recovery",
                snapshot.price,
                ma_score,
                weights.moving_average_recovery,
                "Price>SMA20, rising SMA20, and Price>SMA50 checks",
            ),
            (
                "momentum",
                snapshot.momentum5,
                momentum_score,
                weights.momentum,
                "Positive 5d momentum and improving 20d momentum",
            ),
            (
                "relative_volume",
                snapshot.relative_volume,
                piecewise_score(snapshot.relative_volume, config.relative_volume_curve),
                weights.relative_volume,
                "Current volume versus previous 20-session mean",
            ),
        ],
    )


def combine_scores(
    quality: ScoreBreakdown,
    valuation: ScoreBreakdown,
    opportunity: ScoreBreakdown,
    timing: ScoreBreakdown,
    config: ScoreConfig,
) -> StockScores:
    weights = config.total
    components = [
        (quality.score, weights.quality),
        (valuation.score, weights.valuation),
        (opportunity.score, weights.opportunity),
        (timing.score, weights.timing),
    ]
    available = [(score, weight) for score, weight in components if score is not None]
    total = (
        sum(score * weight for score, weight in available)
        if len(available) == len(components)
        else None
    )
    return StockScores(
        quality=quality,
        valuation=valuation,
        opportunity=opportunity,
        timing=timing,
        total=clip_score(total) if total is not None else None,
    )
