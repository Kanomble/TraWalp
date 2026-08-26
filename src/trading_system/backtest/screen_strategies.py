"""Authoritative metadata and policy helpers for Daily screen variants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trading_system.config import StrategyConfig
from trading_system.models.backtest import ResearchLifecycle, StrategyVariant
from trading_system.models.screening import ScreenRecord
from trading_system.models.signals import TechnicalSnapshot


class ScreenEntryPolicy(StrEnum):
    COMMON_RECOVERY = "common_recovery"
    LOSS_AWARE_RECOVERY = "loss_aware_recovery"
    TREND_PULLBACK = "trend_pullback"
    QUALITY_VALUE_MOMENTUM = "quality_value_momentum"


@dataclass(frozen=True, slots=True)
class ScreenStrategyDefinition:
    variant: StrategyVariant
    name: str
    description: str
    score_components: tuple[str, ...]
    entry_policy: ScreenEntryPolicy
    lifecycle: ResearchLifecycle
    control: bool
    apply_total_threshold: bool = True


SCREEN_STRATEGY_DEFINITIONS: tuple[ScreenStrategyDefinition, ...] = (
    ScreenStrategyDefinition(
        StrategyVariant.QUALITY_VALUE,
        "Quality + Value",
        "Quality + Value scoring with the common technical recovery entry gate.",
        ("quality", "valuation"),
        ScreenEntryPolicy.COMMON_RECOVERY,
        ResearchLifecycle.LEGACY_COMPATIBILITY,
        True,
    ),
    ScreenStrategyDefinition(
        StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
        "Quality + Value + Opportunity",
        "Quality + Value + Opportunity scoring with the common recovery gate.",
        ("quality", "valuation", "opportunity"),
        ScreenEntryPolicy.COMMON_RECOVERY,
        ResearchLifecycle.LEGACY_COMPATIBILITY,
        True,
    ),
    ScreenStrategyDefinition(
        StrategyVariant.FULL,
        "Full Recovery",
        "Quality + Value + Opportunity + Timing scoring with the common recovery gate.",
        ("quality", "valuation", "opportunity", "timing"),
        ScreenEntryPolicy.COMMON_RECOVERY,
        ResearchLifecycle.LEGACY_COMPATIBILITY,
        True,
    ),
    ScreenStrategyDefinition(
        StrategyVariant.LOSS_AWARE_RECOVERY,
        "Loss-Aware Recovery",
        "The full C recovery model with an additional structural loss-path veto.",
        ("quality", "valuation", "opportunity", "timing"),
        ScreenEntryPolicy.LOSS_AWARE_RECOVERY,
        ResearchLifecycle.ACTIVE_RESEARCH,
        False,
    ),
    ScreenStrategyDefinition(
        StrategyVariant.TREND_PULLBACK,
        "Trend Pullback",
        "Quality + Value stocks in established uptrends after a moderate pullback.",
        ("quality", "valuation"),
        ScreenEntryPolicy.TREND_PULLBACK,
        ResearchLifecycle.ACTIVE_RESEARCH,
        False,
        apply_total_threshold=False,
    ),
    ScreenStrategyDefinition(
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        "Quality-Value Momentum",
        "Quality + Value stocks with persistent strength near their 52-week highs.",
        ("quality", "valuation"),
        ScreenEntryPolicy.QUALITY_VALUE_MOMENTUM,
        ResearchLifecycle.ACTIVE_RESEARCH,
        False,
        apply_total_threshold=False,
    ),
)

_DEFINITION_BY_VARIANT = {item.variant: item for item in SCREEN_STRATEGY_DEFINITIONS}


def screen_strategy_definition(variant: StrategyVariant) -> ScreenStrategyDefinition:
    return _DEFINITION_BY_VARIANT[variant]


def variant_score_value(
    record: ScreenRecord, variant: StrategyVariant, config: StrategyConfig
) -> float | None:
    """Return the definition's normalized score without applying entry gates."""

    definition = screen_strategy_definition(variant)
    components: list[tuple[float, float]] = []
    for name in definition.score_components:
        value = getattr(record.scores, name).score
        if value is None:
            return None
        components.append((float(value), float(getattr(config.scores.total, name))))
    denominator = sum(weight for _, weight in components)
    return sum(value * weight for value, weight in components) / denominator


@dataclass(frozen=True, slots=True)
class StrategyGateEvaluation:
    first_failure: str | None
    failure_detail: str | None
    blocking_reasons: tuple[str, ...]
    price_above_sma20: bool | None
    rsi_recovery: bool | None
    momentum5_above_zero: bool | None
    relative_volume_above_threshold: bool | None
    recovery_gate_pass: bool | None


def evaluate_strategy_gate(
    technical: TechnicalSnapshot,
    variant: StrategyVariant,
    config: StrategyConfig,
) -> StrategyGateEvaluation:
    """Evaluate one variant's technical policy using only the supplied PIT snapshot."""

    price_above_sma20 = _greater(technical.price, technical.sma20)
    momentum5_above_zero = None if technical.momentum5 is None else technical.momentum5 > 0
    relative_volume_above_threshold = (
        None
        if technical.relative_volume is None
        else technical.relative_volume > config.backtest.min_relative_volume
    )
    recovery_values = (
        technical.rsi_recovery,
        momentum5_above_zero,
        relative_volume_above_threshold,
    )
    recovery_gate_pass = (
        None
        if all(value is None for value in recovery_values)
        else any(value is True for value in recovery_values)
    )
    definition = screen_strategy_definition(variant)
    failures: list[tuple[str, str]] = []

    if definition.entry_policy in {
        ScreenEntryPolicy.COMMON_RECOVERY,
        ScreenEntryPolicy.LOSS_AWARE_RECOVERY,
    }:
        if price_above_sma20 is None:
            failures.append(("price_not_above_sma20", "sma20_or_price_unavailable"))
        elif not price_above_sma20:
            failures.append(("price_not_above_sma20", "price_not_above_sma20"))
        if recovery_gate_pass is not True:
            failures.append(
                (
                    "recovery_signal_required",
                    "recovery_inputs_unavailable"
                    if recovery_gate_pass is None
                    else "recovery_signal_required",
                )
            )

    if definition.entry_policy is ScreenEntryPolicy.LOSS_AWARE_RECOVERY:
        rules = config.screen_strategies.loss_aware_recovery
        if technical.recovery_from_63d_low is None:
            failures.append(("loss_aware_recovery_unavailable", "loss_aware_recovery_unavailable"))
        elif technical.recovery_from_63d_low < rules.min_recovery_from_63d_low:
            failures.append(
                (
                    "loss_aware_recovery_from_low_insufficient",
                    "loss_aware_recovery_from_low_insufficient",
                )
            )
        if technical.max_drawdown_126d is None:
            failures.append(
                (
                    "loss_aware_max_drawdown_unavailable",
                    "loss_aware_max_drawdown_unavailable",
                )
            )
        elif technical.max_drawdown_126d < rules.max_drawdown_126d_floor:
            failures.append(
                ("loss_aware_max_drawdown_exceeded", "loss_aware_max_drawdown_exceeded")
            )
        if technical.momentum126 is None:
            failures.append(
                ("loss_aware_momentum126_unavailable", "loss_aware_momentum126_unavailable")
            )
        if technical.sma200_distance is None:
            failures.append(
                (
                    "loss_aware_sma200_distance_unavailable",
                    "loss_aware_sma200_distance_unavailable",
                )
            )
        if (
            technical.momentum126 is not None
            and technical.sma200_distance is not None
            and technical.momentum126 < rules.structural_momentum126_threshold
            and technical.sma200_distance < rules.structural_sma200_distance_threshold
        ):
            failures.append(("loss_aware_structural_downtrend", "loss_aware_structural_downtrend"))

    if definition.entry_policy is ScreenEntryPolicy.TREND_PULLBACK:
        rules = config.screen_strategies.trend_pullback
        if technical.price is None:
            failures.append(
                ("trend_pullback_price_unavailable", "trend_pullback_price_unavailable")
            )
        if rules.require_price_above_sma200:
            if technical.sma200 is None:
                failures.append(
                    ("trend_pullback_sma200_unavailable", "trend_pullback_sma200_unavailable")
                )
            elif technical.price is not None and technical.price <= technical.sma200:
                failures.append(
                    (
                        "trend_pullback_price_not_above_sma200",
                        "trend_pullback_price_not_above_sma200",
                    )
                )
        if technical.momentum126 is None:
            failures.append(
                (
                    "trend_pullback_momentum126_unavailable",
                    "trend_pullback_momentum126_unavailable",
                )
            )
        elif technical.momentum126 <= rules.min_momentum126:
            failures.append(
                (
                    "trend_pullback_momentum126_not_positive",
                    "trend_pullback_momentum126_not_positive",
                )
            )
        if technical.drawdown_63d is None:
            failures.append(
                (
                    "trend_pullback_drawdown_63d_unavailable",
                    "trend_pullback_drawdown_63d_unavailable",
                )
            )
        elif technical.drawdown_63d > rules.max_drawdown_63d:
            failures.append(
                ("trend_pullback_pullback_too_shallow", "trend_pullback_pullback_too_shallow")
            )
        elif technical.drawdown_63d < rules.min_drawdown_63d:
            failures.append(
                ("trend_pullback_pullback_too_deep", "trend_pullback_pullback_too_deep")
            )
        if rules.require_price_above_sma20:
            if technical.sma20 is None:
                failures.append(
                    ("trend_pullback_sma20_unavailable", "trend_pullback_sma20_unavailable")
                )
            elif technical.price is not None and technical.price <= technical.sma20:
                failures.append(
                    (
                        "trend_pullback_price_not_above_sma20",
                        "trend_pullback_price_not_above_sma20",
                    )
                )
        if technical.momentum5 is None:
            failures.append(
                ("trend_pullback_momentum5_unavailable", "trend_pullback_momentum5_unavailable")
            )
        elif technical.momentum5 <= rules.min_momentum5:
            failures.append(
                (
                    "trend_pullback_momentum5_not_positive",
                    "trend_pullback_momentum5_not_positive",
                )
            )
        if rules.require_sma20_rising:
            if technical.sma20_rising is None:
                failures.append(
                    (
                        "trend_pullback_sma20_direction_unavailable",
                        "trend_pullback_sma20_direction_unavailable",
                    )
                )
            elif not technical.sma20_rising:
                failures.append(
                    ("trend_pullback_sma20_not_rising", "trend_pullback_sma20_not_rising")
                )

    if definition.entry_policy is ScreenEntryPolicy.QUALITY_VALUE_MOMENTUM:
        rules = config.screen_strategies.quality_value_momentum
        if technical.price is None:
            failures.append(("qv_momentum_price_unavailable", "qv_momentum_price_unavailable"))
        if technical.drawdown_52w is None:
            failures.append(
                (
                    "qv_momentum_drawdown_52w_unavailable",
                    "qv_momentum_drawdown_52w_unavailable",
                )
            )
        elif technical.drawdown_52w < rules.min_drawdown_52w:
            failures.append(("qv_momentum_not_near_52w_high", "qv_momentum_not_near_52w_high"))
        if technical.momentum126 is None:
            failures.append(
                (
                    "qv_momentum_momentum126_unavailable",
                    "qv_momentum_momentum126_unavailable",
                )
            )
        elif technical.momentum126 <= rules.min_momentum126:
            failures.append(
                (
                    "qv_momentum_momentum126_not_positive",
                    "qv_momentum_momentum126_not_positive",
                )
            )
        for period, required in (
            (50, rules.require_price_above_sma50),
            (200, rules.require_price_above_sma200),
        ):
            if not required:
                continue
            sma_value = getattr(technical, f"sma{period}")
            if sma_value is None:
                failures.append(
                    (
                        f"qv_momentum_sma{period}_unavailable",
                        f"qv_momentum_sma{period}_unavailable",
                    )
                )
            elif technical.price is not None and technical.price <= sma_value:
                failures.append(
                    (
                        f"qv_momentum_price_not_above_sma{period}",
                        f"qv_momentum_price_not_above_sma{period}",
                    )
                )
        if rules.require_sma20_rising:
            if technical.sma20_rising is None:
                failures.append(
                    (
                        "qv_momentum_sma20_direction_unavailable",
                        "qv_momentum_sma20_direction_unavailable",
                    )
                )
            elif not technical.sma20_rising:
                failures.append(("qv_momentum_sma20_not_rising", "qv_momentum_sma20_not_rising"))

    first = failures[0] if failures else (None, None)
    return StrategyGateEvaluation(
        first_failure=first[0],
        failure_detail=first[1],
        blocking_reasons=tuple(detail for _, detail in failures),
        price_above_sma20=price_above_sma20,
        rsi_recovery=technical.rsi_recovery,
        momentum5_above_zero=momentum5_above_zero,
        relative_volume_above_threshold=relative_volume_above_threshold,
        recovery_gate_pass=(
            recovery_gate_pass
            if definition.entry_policy
            in {
                ScreenEntryPolicy.COMMON_RECOVERY,
                ScreenEntryPolicy.LOSS_AWARE_RECOVERY,
            }
            else None
        ),
    )


def _greater(left: float | None, right: float | None) -> bool | None:
    return None if left is None or right is None else left > right
