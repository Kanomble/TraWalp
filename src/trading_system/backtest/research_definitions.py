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


@dataclass(frozen=True, slots=True)
class OrbDefinition:
    research_family: str = "research-orb-v1"
    research_id: str = "ORB-V1-15M-LONG"
    status: str = "REJECTED"
    universe_name: str = "ORB_LIQUID_TOP100_US_EQUITY"
    top_n: int = 100
    daily_lookback_sessions: int = 20
    timeframe: str = "15m"
    opening_range_minutes: int = 15
    direction: str = "LONG"
    extended_hours: bool = False
    slippage_bps: float = 5.0
    commission_bps: float = 0.0
    signal: str = "FIRST_COMPLETED_CLOSE_STRICTLY_ABOVE_OPENING_RANGE_HIGH"
    entry: str = "NEXT_ACTUAL_NATIVE_BAR_OPEN"
    stop: str = "FROZEN_OPENING_RANGE_LOW"
    exits: tuple[str, ...] = ("STOP", "SESSION_CLOSE")
    profit_target: None = None
    maximum_attempts_per_symbol_session: int = 1
    overnight: bool = False
    portfolio_strategy_defined: bool = False
    automatic_champion_selection: bool = False


ORB_V1 = OrbDefinition()


@dataclass(frozen=True, slots=True)
class IntradayReversalDefinition:
    research_family: str = "research-intraday-reversal-v1"
    research_id: str = "INTRADAY-REVERSAL-V1-15M-LONG"
    status: str = "REJECTED"
    universe_name: str = "REVERSAL_LIQUID_TOP100_US_EQUITY"
    top_n: int = 100
    daily_lookback_sessions: int = 20
    timeframe: str = "15m"
    extended_hours: bool = False
    first_hour_bars: int = 4
    first_hour_definition: str = "09:30_OPEN_TO_10:15_BAR_CLOSE_AT_10:30_ET"
    first_hour_return: str = "FOURTH_NATIVE_CLOSE / FIRST_NATIVE_OPEN - 1"
    minimum_cross_section: int = 80
    signal_percent: int = 10
    signal_count_rule: str = "MAX_1_FLOOR_OBSERVABLE_COUNT_TIMES_0.10"
    ranking: str = "FIRST_HOUR_RETURN_ASC_SYMBOL_ASC"
    direction: str = "LONG"
    entry: str = "NEXT_ACTUAL_NATIVE_BAR_OPEN"
    exits: tuple[str, ...] = ("SESSION_CLOSE",)
    stop: None = None
    profit_target: None = None
    slippage_bps: float = 5.0
    commission_bps: float = 0.0
    maximum_attempts_per_symbol_session: int = 1
    overnight: bool = False
    portfolio_strategy_defined: bool = False
    automatic_champion_selection: bool = False


INTRADAY_REVERSAL_V1 = IntradayReversalDefinition()


@dataclass(frozen=True, slots=True)
class MarketIntradayMomentumDefinition:
    research_family: str = "research-market-intraday-momentum-v1"
    research_id: str = "MARKET-INTRADAY-MOMENTUM-V1-15M-SPY"
    status: str = "REJECTED"
    symbol: str = "SPY"
    asset_class: str = "US_EQUITY"
    tradable_required: bool = True
    calendar: str = "XNYS"
    timezone: str = "America/New_York"
    timeframe: str = "15m"
    extended_hours: bool = False
    first_window: str = "FIRST_TWO_EXPECTED_NATIVE_REGULAR_BARS"
    first_30m_return: str = "SECOND_NATIVE_CLOSE / FIRST_NATIVE_OPEN - 1"
    decision: str = "SECOND_NATIVE_BAR_COMPLETION"
    direction_rule: str = "POSITIVE_LONG_NEGATIVE_SHORT_EXACT_ZERO_NO_SIGNAL"
    closing_window: str = "FINAL_TWO_EXPECTED_NATIVE_REGULAR_BARS"
    entry: str = "OFFICIAL_PENULTIMATE_NATIVE_BAR_OPEN"
    exits: tuple[str, ...] = ("FINAL_REGULAR_SESSION_CLOSE",)
    long_slippage_bps: float = 5.0
    short_slippage_bps: float = 5.0
    commission_bps: float = 0.0
    borrow_fee_bps: float = 0.0
    stop: None = None
    profit_target: None = None
    trailing_stop: None = None
    partial_exit: None = None
    maximum_attempts_per_session: int = 1
    overnight: bool = False
    portfolio_strategy_defined: bool = False
    automatic_champion_selection: bool = False
    execution_eligibility_germany: str = "NOT_EVALUATED"
    short_execution_eligibility: str = "NOT_MODELED"


MARKET_INTRADAY_MOMENTUM_V1 = MarketIntradayMomentumDefinition()


@dataclass(frozen=True, slots=True)
class ChampionEdgeDecompositionDefinition:
    research_family: str = "research-f-champion-edge-decomposition-v1"
    research_id: str = "F-CHAMPION-EDGE-DECOMPOSITION-V1"
    status: str = "ACTIVE"
    kind: str = "DIAGNOSTIC_ONLY"
    subject: str = "F/configured/C1"
    forward_horizons: tuple[int, ...] = (1, 3, 5, 10, 20)
    holding_checkpoints: tuple[int, ...] = (1, 2, 3, 5, 7, 10)
    quantiles: int = 5
    automatic_champion_selection: bool = False
    strategy_modified: bool = False


F_CHAMPION_EDGE_DECOMPOSITION_V1 = ChampionEdgeDecompositionDefinition()
