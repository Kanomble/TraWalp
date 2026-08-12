import pytest

from trading_system.config import load_settings
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.signals import TechnicalSnapshot
from trading_system.strategy.scoring import (
    combine_scores,
    percentile_score,
    piecewise_score,
    score_opportunity,
    score_quality,
    score_timing,
    score_valuation,
    winsorize,
)


def configs():
    load_settings.cache_clear()
    strategy = load_settings().strategy
    return strategy.scores, strategy.technical, strategy.data_quality


def test_winsorization_and_percentiles_limit_outliers() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 1000.0]
    clipped = winsorize(values, 0.0, 0.8)
    assert clipped.max() < 1000
    assert percentile_score(4, values, higher_is_better=True) is not None
    assert percentile_score(1, values, higher_is_better=False) > 50


def test_opportunity_drawdown_is_nonlinear() -> None:
    score_config, _, _ = configs()
    scores = {
        drawdown: score_opportunity(
            TechnicalSnapshot(
                drawdown_52w=drawdown,
                momentum20=-0.1,
                momentum63=-0.1,
                volatility=0.3,
            ),
            score_config,
        )
        .factors[0]
        .score
        for drawdown in (0.0, -0.25, -0.8)
    }
    assert scores[-0.25] > scores[0.0]
    assert scores[-0.25] > scores[-0.8]


def test_missing_quality_factors_are_reweighted_but_minimum_is_enforced() -> None:
    score_config, _, quality = configs()
    metrics = FundamentalMetrics(
        revenue_growth=0.2,
        eps_growth=0.15,
        operating_cash_flow_positive=True,
        operating_margin=0.2,
    )
    peers = {
        "revenue_growth": [0.0, 0.1, 0.2],
        "eps_growth": [0.0, 0.1, 0.2],
        "operating_margin": [0.05, 0.1, 0.2],
    }
    breakdown = score_quality(
        metrics,
        peers,
        score_config,
        min_available=quality.min_available_quality_metrics,
    )
    assert breakdown.score is not None
    effective = sum(factor.effective_weight for factor in breakdown.factors)
    assert abs(effective - 1) < 1e-9

    insufficient = score_quality(
        metrics.model_copy(update={"operating_margin": None}), peers, score_config
    )
    assert insufficient.score is None


def test_absolute_growth_scores_survive_missing_peer_group() -> None:
    score_config, _, _ = configs()
    metrics = FundamentalMetrics(
        revenue_growth=0.1779,
        eps_growth=0.316,
        operating_cash_flow_growth=0.3435,
        operating_cash_flow_positive=True,
        operating_margin=0.4678,
        roic=0.3059,
        debt_to_ebitda=None,
    )
    quality = score_quality(metrics, {}, score_config, min_available=4)
    factors = {factor.name: factor for factor in quality.factors}
    assert factors["revenue_growth"].score is not None
    assert factors["eps_growth"].score is not None
    assert quality.score is not None
    assert sum(factor.effective_weight for factor in quality.factors) == pytest.approx(1)


def test_available_weights_are_visible_even_when_minimum_blocks_score() -> None:
    score_config, _, _ = configs()
    metrics = FundamentalMetrics(
        operating_cash_flow_positive=True,
        operating_margin=0.2,
        roic=0.2,
    )
    unavailable = score_quality(metrics, {}, score_config, min_available=4)
    factors = {factor.name: factor for factor in unavailable.factors}
    assert unavailable.score is None
    assert unavailable.reason_score_unavailable == "requires at least 4 available factors; found 3"
    assert factors["operating_cash_flow"].normalized_available_weight == pytest.approx(0.2)
    assert factors["operating_margin"].normalized_available_weight == pytest.approx(0.3)
    assert factors["roic"].normalized_available_weight == pytest.approx(0.5)
    assert all(factor.effective_weight == 0 for factor in unavailable.factors)

    available = score_quality(metrics, {}, score_config, min_available=3)
    assert available.score is not None
    assert factors["operating_cash_flow"].configured_weight == 0.1
    assert sum(factor.effective_weight for factor in available.factors) == pytest.approx(1)


def test_quality_valuation_timing_and_total_are_explainable() -> None:
    score_config, technical, data_quality = configs()
    metrics = FundamentalMetrics(
        revenue_growth=0.2,
        eps_growth=0.25,
        operating_cash_flow_growth=0.1,
        operating_cash_flow_positive=True,
        operating_margin=0.22,
        roic=0.18,
        debt_to_ebitda=1.5,
        pe=18,
        ev_to_ebitda=10,
        fcf_yield=0.07,
    )
    peers = {
        "revenue_growth": [0.02, 0.08, 0.12, 0.2],
        "eps_growth": [0.01, 0.1, 0.2, 0.25],
        "operating_cash_flow_growth": [0.0, 0.05, 0.1],
        "operating_margin": [0.08, 0.12, 0.18, 0.22],
        "roic": [0.05, 0.1, 0.15, 0.18],
        "debt_to_ebitda": [1, 1.5, 2, 3],
        "fcf_yield": [0.01, 0.03, 0.05, 0.07],
    }
    quality = score_quality(
        metrics,
        peers,
        score_config,
        min_available=data_quality.min_available_quality_metrics,
    )
    valuation = score_valuation(
        metrics,
        {"pe": 24, "ev_to_ebitda": 14},
        peers,
        score_config,
        min_available=data_quality.min_available_valuation_metrics,
    )
    snapshot = TechnicalSnapshot(
        price=105,
        sma20=100,
        sma50=102,
        sma20_rising=True,
        rsi14=42,
        rsi_recovery=True,
        momentum5=0.03,
        momentum20=-0.08,
        momentum20_improving=True,
        momentum63=-0.12,
        volatility=0.3,
        relative_volume=1.4,
        drawdown_52w=-0.25,
    )
    opportunity = score_opportunity(snapshot, score_config)
    timing = score_timing(snapshot, score_config, technical)
    scores = combine_scores(quality, valuation, opportunity, timing, score_config)
    assert all(section.score is not None for section in (quality, valuation, opportunity, timing))
    assert scores.total is not None and 0 <= scores.total <= 100
    expected_total = (
        0.4 * quality.score + 0.3 * valuation.score + 0.2 * opportunity.score + 0.1 * timing.score
    )
    assert scores.total == expected_total
    assert quality.factors[0].explanation


def test_falling_oversold_rsi_is_not_a_buy_signal() -> None:
    score_config, technical, _ = configs()
    timing = score_timing(TechnicalSnapshot(rsi14=25, rsi_recovery=False), score_config, technical)
    rsi_factor = next(factor for factor in timing.factors if factor.name == "rsi_recovery")
    assert rsi_factor.score == 0


def test_piecewise_clips_beyond_curve_bounds() -> None:
    assert piecewise_score(-10, [(0, 20), (1, 80)]) == 20
    assert piecewise_score(10, [(0, 20), (1, 80)]) == 80


def test_total_is_unavailable_when_a_main_score_is_unavailable() -> None:
    score_config, technical, _ = configs()
    empty_quality = score_quality(FundamentalMetrics(), {}, score_config)
    snapshot = TechnicalSnapshot(
        drawdown_52w=-0.25,
        momentum20=-0.1,
        momentum63=-0.1,
        volatility=0.3,
        rsi14=40,
        rsi_recovery=False,
    )
    opportunity = score_opportunity(snapshot, score_config)
    timing = score_timing(snapshot, score_config, technical)
    empty_valuation = score_valuation(FundamentalMetrics(), {}, {}, score_config)
    total = combine_scores(empty_quality, empty_valuation, opportunity, timing, score_config)
    assert total.total is None
