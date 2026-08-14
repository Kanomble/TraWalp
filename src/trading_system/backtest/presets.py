"""Named position-management configurations without duplicated strategy code."""

from __future__ import annotations

from trading_system.config import (
    AtrTrailingStopConfig,
    MaxHoldConfig,
    PartialTakeProfitConfig,
    PartialTakeProfitLevel,
    PositionManagementConfig,
    SignalDecayConfig,
    StopLossConfig,
    TakeProfitConfig,
)
from trading_system.models.backtest import PositionManagementPreset
from trading_system.models.market_data import BarTimeframe


def position_management_preset(
    base: PositionManagementConfig,
    preset: PositionManagementPreset,
    *,
    legacy_max_holding_days: int,
) -> PositionManagementConfig:
    """Resolve a preset and legacy fallbacks into one validated central config model."""

    if preset is PositionManagementPreset.CONFIGURED:
        return _resolved_legacy_fallbacks(base, legacy_max_holding_days)
    if preset is PositionManagementPreset.LEGACY:
        return PositionManagementConfig(
            stop_loss=StopLossConfig(enabled=True, percent=None),
            take_profit=TakeProfitConfig(enabled=True, percent=None),
            max_hold=MaxHoldConfig(enabled=True, days=legacy_max_holding_days, mode="hard"),
        )

    dynamic = PositionManagementConfig(
        stop_loss=StopLossConfig(enabled=True, percent=0.03),
        take_profit=TakeProfitConfig(enabled=False),
        signal_decay=SignalDecayConfig(enabled=True, minimum_score_ratio=0.75),
        max_hold=MaxHoldConfig(enabled=False, days=legacy_max_holding_days, mode="disabled"),
    )
    if preset is PositionManagementPreset.DYNAMIC_HOLD:
        return dynamic
    if preset is PositionManagementPreset.TAKE_PROFIT:
        return dynamic.model_copy(
            update={"take_profit": TakeProfitConfig(enabled=True, percent=0.02)}
        )

    atr_dynamic = dynamic.model_copy(
        update={
            "atr_trailing_stop": AtrTrailingStopConfig(
                enabled=True, atr_period=14, atr_multiplier=1.0, activation_profit=0.0
            )
        }
    )
    if preset is PositionManagementPreset.ATR_TRAILING:
        return atr_dynamic
    partial = atr_dynamic.model_copy(
        update={
            "partial_take_profit": PartialTakeProfitConfig(
                enabled=True,
                levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
            )
        }
    )
    if preset is PositionManagementPreset.PARTIAL_PROFIT:
        return partial
    if preset is PositionManagementPreset.INTRADAY_DYNAMIC:
        configured = BarTimeframe(base.bar_timeframe)
        timeframe = configured if configured.intraday else BarTimeframe.MINUTES_15
        return partial.model_copy(update={"bar_timeframe": timeframe})

    fixed_baseline = PositionManagementConfig(
        stop_loss=StopLossConfig(enabled=True, percent=0.03),
        take_profit=TakeProfitConfig(enabled=False),
        signal_decay=SignalDecayConfig(enabled=False),
        max_hold=MaxHoldConfig(
            enabled=False, days=legacy_max_holding_days, mode="disabled"
        ),
    )
    if preset is PositionManagementPreset.BASELINE_FIXED_STOP:
        return fixed_baseline
    if preset is PositionManagementPreset.FIXED_STOP_MAX_HOLD:
        return fixed_baseline.model_copy(
            update={
                "max_hold": MaxHoldConfig(
                    enabled=True, days=legacy_max_holding_days, mode="hard"
                )
            }
        )
    if preset is PositionManagementPreset.FIXED_STOP_TAKE_PROFIT:
        return fixed_baseline.model_copy(
            update={"take_profit": TakeProfitConfig(enabled=True, percent=0.02)}
        )
    fixed_atr = fixed_baseline.model_copy(
        update={
            "atr_trailing_stop": AtrTrailingStopConfig(
                enabled=True, atr_period=14, atr_multiplier=1.0, activation_profit=0.0
            )
        }
    )
    if preset is PositionManagementPreset.FIXED_STOP_ATR_TRAILING:
        return fixed_atr
    if preset is PositionManagementPreset.FIXED_STOP_PARTIAL_ATR:
        return fixed_atr.model_copy(
            update={
                "partial_take_profit": PartialTakeProfitConfig(
                    enabled=True,
                    levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
                )
            }
        )
    raise ValueError(f"Unknown position-management preset: {preset}")


def _resolved_legacy_fallbacks(
    config: PositionManagementConfig, legacy_max_holding_days: int
) -> PositionManagementConfig:
    max_hold = config.max_hold
    if max_hold.days is None:
        max_hold = max_hold.model_copy(update={"days": legacy_max_holding_days})
    return config.model_copy(update={"max_hold": max_hold})
