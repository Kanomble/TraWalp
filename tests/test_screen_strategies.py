from datetime import date

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.research_registry import research_metadata
from trading_system.backtest.screen_strategies import (
    SCREEN_STRATEGY_DEFINITIONS,
    ScreenEntryPolicy,
    screen_strategy_definition,
)
from trading_system.cli import _parser
from trading_system.config import load_settings
from trading_system.models.backtest import (
    PositionManagementPreset,
    ResearchLifecycle,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def _score(name: str, value: float) -> ScoreBreakdown:
    return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)


def _record(
    *,
    quality: float = 90,
    valuation: float = 80,
    opportunity: float = 70,
    timing: float = 60,
    technical_updates: dict | None = None,
    exclusions: tuple[str, ...] = (),
) -> ScreenRecord:
    technical = TechnicalSnapshot(
        market_session=date(2024, 1, 5),
        price=110,
        sma20=105,
        sma50=102,
        sma200=100,
        sma20_rising=True,
        rsi_recovery=True,
        momentum5=0.02,
        momentum126=0.15,
        relative_volume=1.3,
        drawdown_52w=-0.05,
        drawdown_63d=-0.10,
        recovery_from_63d_low=0.10,
        max_drawdown_126d=-0.20,
        sma200_distance=0.10,
    ).model_copy(update=technical_updates or {})
    return ScreenRecord(
        symbol="AAA",
        name="AAA",
        as_of=date(2024, 1, 5),
        eligible=not exclusions,
        exclusion_reasons=exclusions,
        fundamentals=FundamentalMetrics(operating_cash_flow_positive=True),
        technical=technical,
        scores=StockScores(
            quality=_score("quality", quality),
            valuation=_score("valuation", valuation),
            opportunity=_score("opportunity", opportunity),
            timing=_score("timing", timing),
            total=80,
        ),
    )


def _config():
    load_settings.cache_clear()
    return load_settings().strategy


def test_screen_strategy_registry_is_the_authoritative_a_through_f_definition() -> None:
    assert tuple(item.variant for item in SCREEN_STRATEGY_DEFINITIONS) == tuple(StrategyVariant)
    assert [item.name for item in SCREEN_STRATEGY_DEFINITIONS] == [
        "Quality + Value",
        "Quality + Value + Opportunity",
        "Full Recovery",
        "Loss-Aware Recovery",
        "Trend Pullback",
        "Quality-Value Momentum",
    ]
    for variant in (
        StrategyVariant.LOSS_AWARE_RECOVERY,
        StrategyVariant.TREND_PULLBACK,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
    ):
        definition = screen_strategy_definition(variant)
        assert definition.lifecycle is ResearchLifecycle.ACTIVE_RESEARCH
        assert definition.control is False
        metadata = research_metadata(variant, PositionManagementPreset.CONFIGURED)
        assert metadata.lifecycle is ResearchLifecycle.ACTIVE_RESEARCH
        assert metadata.control is False


def test_frozen_screen_strategy_configuration_defaults() -> None:
    config = _config().screen_strategies
    assert config.loss_aware_recovery.model_dump() == {
        "min_recovery_from_63d_low": 0.05,
        "max_drawdown_126d_floor": -0.40,
        "structural_momentum126_threshold": -0.25,
        "structural_sma200_distance_threshold": -0.15,
    }
    assert config.trend_pullback.min_drawdown_63d == -0.20
    assert config.trend_pullback.max_drawdown_63d == -0.05
    assert config.quality_value_momentum.min_drawdown_52w == -0.10


def test_a_b_c_entry_results_and_scores_remain_frozen_controls() -> None:
    config = _config()
    record = _record()
    expected_scores = {
        StrategyVariant.QUALITY_VALUE: (90 * 0.4 + 80 * 0.3) / 0.7,
        StrategyVariant.QUALITY_VALUE_OPPORTUNITY: (90 * 0.4 + 80 * 0.3 + 70 * 0.2) / 0.9,
        StrategyVariant.FULL: 80,
    }
    for variant, expected in expected_scores.items():
        evaluation = evaluate_variant_entry(record, variant, config)
        assert evaluation.eligible is True
        assert evaluation.score == pytest.approx(expected)
        assert evaluation.first_failure is None
        assert evaluation.blocking_reasons == ()

    no_recovery = _record(
        technical_updates={
            "rsi_recovery": False,
            "momentum5": -0.01,
            "relative_volume": 1.0,
        }
    )
    assert [
        evaluate_variant_entry(no_recovery, variant, config).first_failure
        for variant in expected_scores
    ] == ["recovery_signal_required"] * 3

    records = [_record(quality=80), _record(quality=95)]
    for variant in expected_scores:
        ranked = sorted(
            records,
            key=lambda item: evaluate_variant_entry(item, variant, config).score or -1,
            reverse=True,
        )
        assert [item.scores.quality.score for item in ranked] == [95, 80]


def test_d_is_c_plus_loss_aware_gate_and_uses_the_identical_c_score() -> None:
    config = _config()
    record = _record()
    c = evaluate_variant_entry(record, StrategyVariant.FULL, config)
    d = evaluate_variant_entry(record, StrategyVariant.LOSS_AWARE_RECOVERY, config)
    assert c.eligible and d.eligible
    assert d.score == c.score
    assert screen_strategy_definition(StrategyVariant.LOSS_AWARE_RECOVERY).entry_policy is (
        ScreenEntryPolicy.LOSS_AWARE_RECOVERY
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"recovery_from_63d_low": 0.049},
            "loss_aware_recovery_from_low_insufficient",
        ),
        ({"max_drawdown_126d": -0.401}, "loss_aware_max_drawdown_exceeded"),
        (
            {"momentum126": -0.251, "sma200_distance": -0.151},
            "loss_aware_structural_downtrend",
        ),
        ({"recovery_from_63d_low": None}, "loss_aware_recovery_unavailable"),
        ({"max_drawdown_126d": None}, "loss_aware_max_drawdown_unavailable"),
        ({"momentum126": None}, "loss_aware_momentum126_unavailable"),
        ({"sma200_distance": None}, "loss_aware_sma200_distance_unavailable"),
    ],
)
def test_d_rejects_each_frozen_structural_failure(updates: dict, reason: str) -> None:
    config = _config()
    record = _record(technical_updates=updates)
    assert evaluate_variant_entry(record, StrategyVariant.FULL, config).eligible
    evaluation = evaluate_variant_entry(record, StrategyVariant.LOSS_AWARE_RECOVERY, config)
    assert not evaluation.eligible
    assert evaluation.first_failure == reason


@pytest.mark.parametrize(
    "updates",
    [
        {"momentum126": -0.30, "sma200_distance": -0.10},
        {"momentum126": -0.20, "sma200_distance": -0.20},
    ],
)
def test_d_structural_downtrend_requires_both_negative_conditions(updates: dict) -> None:
    evaluation = evaluate_variant_entry(
        _record(technical_updates=updates),
        StrategyVariant.LOSS_AWARE_RECOVERY,
        _config(),
    )
    assert evaluation.eligible


def test_e_valid_trend_pullback_passes_and_uses_normalized_quality_value_score() -> None:
    config = _config()
    evaluation = evaluate_variant_entry(_record(), StrategyVariant.TREND_PULLBACK, config)
    assert evaluation.eligible
    assert evaluation.score == pytest.approx((90 * 0.4 + 80 * 0.3) / 0.7)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"price": 99}, "trend_pullback_price_not_above_sma200"),
        ({"momentum126": 0}, "trend_pullback_momentum126_not_positive"),
        ({"drawdown_63d": -0.049}, "trend_pullback_pullback_too_shallow"),
        ({"drawdown_63d": -0.201}, "trend_pullback_pullback_too_deep"),
        ({"sma20": 111}, "trend_pullback_price_not_above_sma20"),
        ({"momentum5": 0}, "trend_pullback_momentum5_not_positive"),
        ({"sma20_rising": False}, "trend_pullback_sma20_not_rising"),
        ({"sma200": None}, "trend_pullback_sma200_unavailable"),
        ({"momentum126": None}, "trend_pullback_momentum126_unavailable"),
        ({"drawdown_63d": None}, "trend_pullback_drawdown_63d_unavailable"),
        ({"sma20": None}, "trend_pullback_sma20_unavailable"),
        ({"momentum5": None}, "trend_pullback_momentum5_unavailable"),
        ({"sma20_rising": None}, "trend_pullback_sma20_direction_unavailable"),
    ],
)
def test_e_frozen_gate_failures_are_explicit(updates: dict, reason: str) -> None:
    evaluation = evaluate_variant_entry(
        _record(technical_updates=updates), StrategyVariant.TREND_PULLBACK, _config()
    )
    assert not evaluation.eligible
    assert evaluation.first_failure == reason


def test_f_valid_strength_passes_qv_score_without_c_recovery_gate() -> None:
    config = _config()
    record = _record(
        technical_updates={
            "rsi_recovery": False,
            "momentum5": -0.02,
            "relative_volume": 0.5,
        }
    )
    assert not evaluate_variant_entry(record, StrategyVariant.FULL, config).eligible
    evaluation = evaluate_variant_entry(record, StrategyVariant.QUALITY_VALUE_MOMENTUM, config)
    assert evaluation.eligible
    assert evaluation.score == pytest.approx((90 * 0.4 + 80 * 0.3) / 0.7)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"drawdown_52w": -0.101}, "qv_momentum_not_near_52w_high"),
        ({"momentum126": 0}, "qv_momentum_momentum126_not_positive"),
        ({"sma50": 111}, "qv_momentum_price_not_above_sma50"),
        ({"sma200": 111}, "qv_momentum_price_not_above_sma200"),
        ({"sma20_rising": False}, "qv_momentum_sma20_not_rising"),
        ({"drawdown_52w": None}, "qv_momentum_drawdown_52w_unavailable"),
        ({"momentum126": None}, "qv_momentum_momentum126_unavailable"),
        ({"sma50": None}, "qv_momentum_sma50_unavailable"),
        ({"sma200": None}, "qv_momentum_sma200_unavailable"),
        ({"sma20_rising": None}, "qv_momentum_sma20_direction_unavailable"),
    ],
)
def test_f_frozen_gate_failures_are_explicit(updates: dict, reason: str) -> None:
    evaluation = evaluate_variant_entry(
        _record(technical_updates=updates),
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        _config(),
    )
    assert not evaluation.eligible
    assert evaluation.first_failure == reason


def test_score_variant_comparison_is_exact_a_through_f_on_configured_management() -> None:
    assert engine_module._comparison_runs(StrategyComparisonKind.SCORE_VARIANTS) == tuple(
        (variant, PositionManagementPreset.CONFIGURED) for variant in StrategyVariant
    )
    hybrid = engine_module._comparison_runs(StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID)
    assert {variant for variant, _ in hybrid} == {StrategyVariant.FULL}
    assert len(hybrid) == 3


@pytest.mark.parametrize("command", ["backtest", "audit-candidates"])
@pytest.mark.parametrize("variant", list("ABCDEF"))
def test_cli_research_variant_choices_accept_a_through_f(
    command: str, variant: str
) -> None:
    args = _parser().parse_args(
        [command, "--start", "2024-01-02", "--end", "2024-01-05", "--variant", variant]
    )
    assert args.variant == variant


def test_production_screen_default_has_no_implicit_research_variant() -> None:
    args = _parser().parse_args(["screen"])
    assert not hasattr(args, "variant")


def test_cross_strategy_diagnostics_are_bounded_and_explain_c_rejected_by_d() -> None:
    session = date(2024, 1, 5)
    c_only = _record(
        technical_updates={
            "max_drawdown_126d": -0.47,
            "momentum126": -0.30,
            "sma200_distance": -0.20,
            "drawdown_52w": -0.20,
        }
    ).model_copy(update={"symbol": "CONLY", "name": "CONLY"})
    e_only = _record(timing=50, technical_updates={"drawdown_52w": -0.20}).model_copy(
        update={"symbol": "EONLY", "name": "EONLY"}
    )
    f_only = _record(timing=50, technical_updates={"drawdown_63d": -0.02}).model_copy(
        update={"symbol": "FONLY", "name": "FONLY"}
    )
    multiple = _record().model_copy(update={"symbol": "MULTI", "name": "MULTI"})
    report = ScreenReport(
        as_of=session,
        generated_at="2024-01-05T22:00:00+00:00",
        analyzed_count=4,
        eligible_count=4,
        records=(c_only, e_only, f_only, multiple),
    )

    diagnostics = engine_module._screen_selection_diagnostics(
        {session: report}, _config(), sample_limit=2
    )

    assert diagnostics["selection_patterns"]["C"] == 1
    assert diagnostics["selection_patterns"]["E"] == 1
    assert diagnostics["selection_patterns"]["F"] == 1
    assert diagnostics["selection_patterns"]["C+D+E+F"] == 1
    assert diagnostics["selection_groups"] == {
        "C_only": 1,
        "D_only": 0,
        "E_only": 1,
        "F_only": 1,
        "multiple": 1,
    }
    assert (
        diagnostics["c_candidates_rejected_by_d_sample"][0]["d_rejection_reason"]
        == "loss_aware_max_drawdown_exceeded"
    )
    assert diagnostics["sample_limit_per_group"] == 2
