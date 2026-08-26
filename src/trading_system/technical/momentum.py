"""Recovery-state extraction from technical indicator histories."""

from __future__ import annotations

import math

import pandas as pd

from trading_system.config import TechnicalConfig
from trading_system.models.signals import TechnicalSnapshot
from trading_system.technical.indicators import indicator_frame


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def technical_snapshot(
    bars: pd.DataFrame, rules: TechnicalConfig | None = None
) -> TechnicalSnapshot:
    rules = rules or TechnicalConfig()
    indicators = indicator_frame(bars)
    if indicators.empty:
        return TechnicalSnapshot()
    latest = indicators.iloc[-1]
    rsi_now = _finite(latest["rsi14"])
    rsi_previous = _finite(indicators["rsi14"].iloc[-2]) if len(indicators) >= 2 else None
    recent_rsi = indicators["rsi14"].iloc[-(rules.rsi_recovery_lookback + 1) : -1].dropna()
    rsi_recovery = (
        bool(
            (recent_rsi < rules.rsi_oversold).any()
            and rules.rsi_recovery_min < rsi_now <= rules.rsi_recovery_max
            and rsi_now > rsi_previous
        )
        if rsi_now is not None and rsi_previous is not None
        else None
    )
    sma20_now = _finite(latest["sma20"])
    slope_offset = rules.sma_slope_lookback + 1
    sma20_prior = (
        _finite(indicators["sma20"].iloc[-slope_offset])
        if len(indicators) >= slope_offset
        else None
    )
    momentum20_now = _finite(latest["momentum20"])
    momentum20_prior = _finite(indicators["momentum20"].iloc[-6]) if len(indicators) >= 6 else None
    return TechnicalSnapshot(
        price=_finite(latest["close"]),
        sma20=sma20_now,
        sma50=_finite(latest["sma50"]),
        sma200=_finite(latest["sma200"]),
        sma20_rising=(sma20_now > sma20_prior)
        if sma20_now is not None and sma20_prior is not None
        else None,
        rsi14=rsi_now,
        rsi_recovery=rsi_recovery,
        momentum5=_finite(latest["momentum5"]),
        momentum20=momentum20_now,
        momentum20_improving=(momentum20_now > momentum20_prior)
        if momentum20_now is not None and momentum20_prior is not None
        else None,
        momentum63=_finite(latest["momentum63"]),
        momentum126=_finite(latest["momentum126"]),
        volatility=_finite(latest["volatility20"]),
        atr14=_finite(latest["atr14"]),
        relative_volume=_finite(latest["relative_volume20"]),
        drawdown_52w=_finite(latest["drawdown_52w"]),
        drawdown_63d=_finite(latest["drawdown_63d"]),
        recovery_from_63d_low=_finite(latest["recovery_from_63d_low"]),
        max_drawdown_126d=_finite(latest["max_drawdown_126d"]),
        sma200_distance=_finite(latest["sma200_distance"]),
    )
