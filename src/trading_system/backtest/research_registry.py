"""Central lifecycle and family registry for reproducible strategy research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_system.backtest.screen_strategies import SCREEN_STRATEGY_DEFINITIONS
from trading_system.models.backtest import (
    PositionManagementPreset,
    ResearchLifecycle,
    StrategyComparisonKind,
    StrategyVariant,
)


@dataclass(frozen=True, slots=True)
class StrategyResearchMetadata:
    preset: PositionManagementPreset
    variant: StrategyVariant
    research_id: str
    display_name: str
    lifecycle: ResearchLifecycle
    family: str
    control: bool
    description: str
    expensive_comparison_default: bool


@dataclass(frozen=True, slots=True)
class FrozenResearchChampion:
    """Research identity and temporal boundary; never strategy configuration."""

    validation_target: str
    forward_validation_target: str
    label: str
    variant: StrategyVariant
    preset: PositionManagementPreset
    development_cutoff: date


@dataclass(frozen=True, slots=True)
class FCapacityResearchVariant:
    """Research-only portfolio-capacity identity for the frozen F composition."""

    research_id: str
    label: str
    max_positions: int
    variant: StrategyVariant
    preset: PositionManagementPreset
    frozen_champion_control: bool


FROZEN_CHAMPION_F = FrozenResearchChampion(
    validation_target="champion-f",
    forward_validation_target="champion-f-forward",
    label="F/configured",
    variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
    preset=PositionManagementPreset.CONFIGURED,
    development_cutoff=date(2026, 8, 12),
)

F_CAPACITY_RESEARCH_FAMILY = "research-f-capacity"
F_CAPACITY_RESEARCH_VARIANTS: tuple[FCapacityResearchVariant, ...] = tuple(
    FCapacityResearchVariant(
        research_id=f"F-CAPACITY-{max_positions}",
        label=f"F-capacity-{max_positions}",
        max_positions=max_positions,
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
        frozen_champion_control=max_positions == 1,
    )
    for max_positions in (1, 2, 3, 5)
)


def _metadata(
    preset: PositionManagementPreset,
    research_id: str,
    display_name: str,
    lifecycle: ResearchLifecycle,
    family: str,
    description: str,
    *,
    variant: StrategyVariant = StrategyVariant.FULL,
    control: bool = False,
    expensive: bool = False,
) -> StrategyResearchMetadata:
    return StrategyResearchMetadata(
        preset=preset,
        variant=variant,
        research_id=research_id,
        display_name=display_name,
        lifecycle=lifecycle,
        family=family,
        control=control,
        description=description,
        expensive_comparison_default=expensive,
    )


STRATEGY_RESEARCH_REGISTRY: tuple[StrategyResearchMetadata, ...] = (
    _metadata(
        PositionManagementPreset.INTRADAY_DYNAMIC,
        "F0-C",
        "F0/C-intraday-dynamic",
        ResearchLifecycle.CHAMPION_CONTROL,
        "intraday-control",
        "Frozen C intraday-dynamic champion/control.",
        control=True,
        expensive=True,
    ),
    _metadata(
        PositionManagementPreset.INTRADAY_DYNAMIC,
        "F-INTRADAY-F",
        "F-intraday/F-intraday-dynamic",
        ResearchLifecycle.ACTIVE_RESEARCH,
        "intraday-hybrid",
        "PIT F candidate selection with the unchanged F0 intraday-dynamic management preset.",
        variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
        expensive=True,
    ),
    _metadata(
        PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN,
        "F1-C",
        "F1/C-intraday-loss-cooldown",
        ResearchLifecycle.ARCHIVED_RESEARCH,
        "intraday-isolation",
        "Gross-loss next-session cooldown isolation.",
    ),
    _metadata(
        PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        "F2-C",
        "F2/C-intraday-opening-survivor-gate",
        ResearchLifecycle.ARCHIVED_RESEARCH,
        "intraday-isolation",
        "Opening-bar survivor gate isolation.",
    ),
    _metadata(
        PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY,
        "F3-C",
        "F3/C-intraday-thesis-recovery",
        ResearchLifecycle.ACTIVE_RESEARCH,
        "intraday-next",
        "Same-symbol loss re-entry requires a strictly higher PIT C score.",
        expensive=True,
    ),
    _metadata(
        PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
        "F4-C",
        "F4/C-intraday-first-hour-pullback",
        ResearchLifecycle.ARCHIVED_RESEARCH,
        "intraday-next",
        "EMA20-qualified first-hour pullback with confirmed swing-high exit.",
        expensive=True,
    ),
    _metadata(
        PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        "F5-C",
        "F5/C-intraday-first-hour-pullback-f0-management",
        ResearchLifecycle.ACTIVE_RESEARCH,
        "intraday-hybrid",
        "F4 first-hour pullback entry with frozen F0 intraday management.",
        expensive=True,
    ),
    _metadata(
        PositionManagementPreset.F_FIRST_HOUR_PULLBACK_CONFIGURED,
        "F-ENTRY-FIRST-HOUR-CONFIGURED",
        "F-entry/F-first-hour-pullback-configured",
        ResearchLifecycle.ACTIVE_RESEARCH,
        "f-entry",
        (
            "PIT F candidate selection with the shared first-hour pullback entry and "
            "configured Daily position management."
        ),
        variant=StrategyVariant.QUALITY_VALUE_MOMENTUM,
        expensive=True,
    ),
    *(
        _metadata(
            preset,
            f"{preset.value.split('-', 1)[0]}-C",
            display_name,
            ResearchLifecycle.ARCHIVED_RESEARCH,
            "d1-d5-archive",
            "Archived D-family research retained for exact historical reproduction.",
        )
        for preset, display_name in (
            (PositionManagementPreset.D1_SWING_PROFIT_LOCK, "D1/C-swing-profit-lock"),
            (PositionManagementPreset.D2_SWING_RUNNER, "D2/C-swing-runner"),
            (PositionManagementPreset.D3_INTRADAY_TRAIL_GUARD, "D3/C-intraday-trail-guard"),
            (
                PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY,
                "D4/C-intraday-confirmed-entry",
            ),
            (
                PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
                "D5/C-hybrid-confirmed-swing",
            ),
        )
    ),
    _metadata(
        PositionManagementPreset.D1_SWING_PROFIT_LOCK,
        "D1-B",
        "D1/B-swing-profit-lock",
        ResearchLifecycle.ARCHIVED_RESEARCH,
        "d1-d5-archive",
        "Archived B-selection D1 robustness mirror.",
        variant=StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
    ),
    _metadata(
        PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
        "D5-B",
        "D5/B-hybrid-confirmed-swing",
        ResearchLifecycle.ARCHIVED_RESEARCH,
        "d1-d5-archive",
        "Archived B-selection D5 robustness mirror.",
        variant=StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
    ),
    *(
        _metadata(
            preset,
            f"HIST-{preset.value}",
            f"C/{preset.value}",
            lifecycle,
            "historical-position-management",
            "Historical position-management preset retained for compatibility.",
        )
        for preset, lifecycle in (
            (PositionManagementPreset.LEGACY, ResearchLifecycle.LEGACY_COMPATIBILITY),
            (PositionManagementPreset.DYNAMIC_HOLD, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.TAKE_PROFIT, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.ATR_TRAILING, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.PARTIAL_PROFIT, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.BASELINE_FIXED_STOP, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.FIXED_STOP_MAX_HOLD, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.FIXED_STOP_TAKE_PROFIT, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.FIXED_STOP_ATR_TRAILING, ResearchLifecycle.ARCHIVED_RESEARCH),
            (PositionManagementPreset.FIXED_STOP_PARTIAL_ATR, ResearchLifecycle.ARCHIVED_RESEARCH),
        )
    ),
    *(
        _metadata(
            PositionManagementPreset.CONFIGURED,
            f"{'CONTROL' if definition.control else 'SCREEN'}-{variant.value}",
            f"{variant.value}/configured",
            definition.lifecycle,
            "configured-controls" if definition.control else "screen-strategy-research",
            definition.description,
            variant=variant,
            control=definition.control,
        )
        for definition in SCREEN_STRATEGY_DEFINITIONS
        for variant in (definition.variant,)
    ),
)


RESEARCH_FAMILY_RUNS: dict[
    StrategyComparisonKind,
    tuple[tuple[StrategyVariant, PositionManagementPreset], ...],
] = {
    StrategyComparisonKind.RESEARCH_D1_D5: (
        (StrategyVariant.QUALITY_VALUE, PositionManagementPreset.CONFIGURED),
        (StrategyVariant.QUALITY_VALUE_OPPORTUNITY, PositionManagementPreset.CONFIGURED),
        (StrategyVariant.FULL, PositionManagementPreset.CONFIGURED),
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.D1_SWING_PROFIT_LOCK),
        (StrategyVariant.FULL, PositionManagementPreset.D2_SWING_RUNNER),
        (StrategyVariant.FULL, PositionManagementPreset.D3_INTRADAY_TRAIL_GUARD),
        (StrategyVariant.FULL, PositionManagementPreset.D4_INTRADAY_CONFIRMED_ENTRY),
        (StrategyVariant.FULL, PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING),
        (
            StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
            PositionManagementPreset.D1_SWING_PROFIT_LOCK,
        ),
        (
            StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
            PositionManagementPreset.D5_HYBRID_CONFIRMED_SWING,
        ),
    ),
    StrategyComparisonKind.EXTENDED_VALIDATION: (
        (StrategyVariant.QUALITY_VALUE_OPPORTUNITY, PositionManagementPreset.CONFIGURED),
        (StrategyVariant.FULL, PositionManagementPreset.CONFIGURED),
        (StrategyVariant.FULL, PositionManagementPreset.D1_SWING_PROFIT_LOCK),
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.D2_SWING_RUNNER),
        (
            StrategyVariant.QUALITY_VALUE_OPPORTUNITY,
            PositionManagementPreset.D1_SWING_PROFIT_LOCK,
        ),
    ),
    StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION: (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F1_INTRADAY_LOSS_COOLDOWN),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F2_INTRADAY_OPENING_SURVIVOR_GATE,
        ),
    ),
    StrategyComparisonKind.RESEARCH_INTRADAY_NEXT: (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F4_INTRADAY_FIRST_HOUR_PULLBACK,
        ),
    ),
    StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID: (
        (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC),
        (StrategyVariant.FULL, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        ),
        (
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.INTRADAY_DYNAMIC,
        ),
    ),
    StrategyComparisonKind.RESEARCH_F_ENTRY: (
        (
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.CONFIGURED,
        ),
        (
            StrategyVariant.QUALITY_VALUE_MOMENTUM,
            PositionManagementPreset.F_FIRST_HOUR_PULLBACK_CONFIGURED,
        ),
    ),
    StrategyComparisonKind.RESEARCH_CHAMPION_F: (
        (
            FROZEN_CHAMPION_F.variant,
            FROZEN_CHAMPION_F.preset,
        ),
    ),
}


def research_family_runs(
    kind: StrategyComparisonKind,
) -> tuple[tuple[StrategyVariant, PositionManagementPreset], ...]:
    try:
        return RESEARCH_FAMILY_RUNS[kind]
    except KeyError as exc:
        raise ValueError(f"Not a registered research comparison family: {kind}") from exc


def f_capacity_research_variants() -> tuple[FCapacityResearchVariant, ...]:
    """Return the pre-registered capacity levels without cloning F strategy logic."""

    return F_CAPACITY_RESEARCH_VARIANTS


def research_metadata(
    variant: StrategyVariant,
    preset: PositionManagementPreset,
) -> StrategyResearchMetadata:
    for metadata in STRATEGY_RESEARCH_REGISTRY:
        if metadata.variant is variant and metadata.preset is preset:
            return metadata
    raise ValueError(f"Unregistered research strategy: {variant.value}/{preset.value}")


def research_strategy_label(
    variant: StrategyVariant,
    preset: PositionManagementPreset,
) -> str:
    return research_metadata(variant, preset).display_name


def comparison_strategy_label(
    kind: StrategyComparisonKind,
    variant: StrategyVariant,
    preset: PositionManagementPreset,
) -> str:
    """Use historical labels where report compatibility requires them."""

    if kind in {
        StrategyComparisonKind.RESEARCH_D1_D5,
        StrategyComparisonKind.EXTENDED_VALIDATION,
    }:
        if preset is PositionManagementPreset.CONFIGURED:
            return f"{variant.value}/configured"
        if preset is PositionManagementPreset.INTRADAY_DYNAMIC:
            return f"{variant.value}/intraday-dynamic"
    return research_strategy_label(variant, preset)


def lifecycle_for_preset(
    preset: PositionManagementPreset,
    variant: StrategyVariant = StrategyVariant.FULL,
) -> ResearchLifecycle:
    """Return lifecycle metadata for the exact selection/management composition."""

    return research_metadata(variant, preset).lifecycle


def validate_research_registry() -> None:
    research_ids = [metadata.research_id for metadata in STRATEGY_RESEARCH_REGISTRY]
    if len(research_ids) != len(set(research_ids)):
        raise ValueError("Duplicate research IDs in strategy registry")
    for runs in RESEARCH_FAMILY_RUNS.values():
        for variant, preset in runs:
            research_metadata(variant, preset)
    capacities = [item.max_positions for item in F_CAPACITY_RESEARCH_VARIANTS]
    if capacities != [1, 2, 3, 5]:
        raise ValueError("F capacity research family must contain exactly 1, 2, 3, and 5")
    if any(
        item.variant is not FROZEN_CHAMPION_F.variant or item.preset is not FROZEN_CHAMPION_F.preset
        for item in F_CAPACITY_RESEARCH_VARIANTS
    ):
        raise ValueError("F capacity research family must preserve F/configured identity")


validate_research_registry()
