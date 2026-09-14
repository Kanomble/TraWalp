"""Immutable research identities shared without loading experimental execution code."""

from dataclasses import dataclass
from enum import StrEnum

F_INTRADAY_ENTRY_RESEARCH_FAMILY = "research-f-intraday-entry-quality"


@dataclass(frozen=True, slots=True)
class EntryQualityPreset:
    research_id: str
    label: str
    opening_weakness_veto: bool = False
    common_support: bool = False


F_INTRADAY_ENTRY_VARIANTS = (
    EntryQualityPreset("F-INTRADAY-ENTRY-I0", "F-entry-control"),
    EntryQualityPreset("F-INTRADAY-ENTRY-I1", "F-entry-opening-weakness-veto", True),
)

# Validation runs; retain the discovery identities above for existing candidate manifests.
F_INTRADAY_COMMON_SUPPORT_VARIANTS = (
    EntryQualityPreset("F-INTRADAY-ENTRY-I0_FULL", "I0_FULL"),
    EntryQualityPreset(
        "F-INTRADAY-ENTRY-I0_COMMON_SUPPORT", "I0_COMMON_SUPPORT", common_support=True
    ),
    EntryQualityPreset("F-INTRADAY-ENTRY-I1_COMMON_SUPPORT", "I1_COMMON_SUPPORT", True, True),
)


@dataclass(frozen=True, slots=True)
class LifecyclePreset:
    research_id: str
    label: str
    max_hold_days: int | None = None
    conditional_extension: bool = False
    require_peers: bool = False
    defer_profit_target: bool = False


F_LIFECYCLE_RESEARCH_FAMILY = "research-f-lifecycle-v2"
F_LIFECYCLE_VARIANTS = (
    LifecyclePreset("F-LIFECYCLE-L0", "F-lifecycle-control"),
    LifecyclePreset("F-LIFECYCLE-L1", "F-lifecycle-hold15", 15),
    LifecyclePreset("F-LIFECYCLE-L2", "F-lifecycle-hold20", 20),
    LifecyclePreset("F-LIFECYCLE-L3", "F-lifecycle-hold30", 30),
    LifecyclePreset("F-LIFECYCLE-L4", "F-lifecycle-conditional-hold20", 20, True),
    LifecyclePreset("F-LIFECYCLE-L5", "F-lifecycle-hold20-peer-confirmed", 20, True, True),
    LifecyclePreset("F-LIFECYCLE-L6", "F-lifecycle-dynamic-profit-peer", 20, False, True, True),
)


@dataclass(frozen=True, slots=True)
class IntradayRiskVariant:
    research_id: str
    label: str
    common_support: bool = False
    overlay: bool = False


F_INTRADAY_RISK_VARIANTS = (
    IntradayRiskVariant("F-INTRADAY-RISK-R0_FULL", "R0_FULL"),
    IntradayRiskVariant("F-INTRADAY-RISK-R0_COMMON_SUPPORT", "R0_COMMON_SUPPORT", True),
    IntradayRiskVariant("F-INTRADAY-RISK-R1_COMMON_SUPPORT", "R1_COMMON_SUPPORT", True, True),
)


class RegimeCapacityRule(StrEnum):
    """The four frozen rules admitted by the registered research family."""

    CONTROL_C1 = "CONTROL_C1"
    CONTROL_C5 = "CONTROL_C5"
    REGIME_SMA200 = "REGIME_SMA200"
    REGIME_SMA200_MOM126 = "REGIME_SMA200_MOM126"


F_INTRADAY_RISK_RESEARCH_FAMILY = "research-f-intraday-risk-v1"
