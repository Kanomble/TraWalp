"""Vectorized daily-price indicators used by Milestone 2."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sma(values: pd.Series, period: int) -> pd.Series:
    return values.astype(float).rolling(period, min_periods=period).mean()


def ema(values: pd.Series, period: int) -> pd.Series:
    return values.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    source = values.astype(float)
    result = pd.Series(np.nan, index=source.index, dtype=float)
    if len(source) < period:
        return result
    seed = source.iloc[:period].mean()
    result.iloc[period - 1] = seed
    previous = seed
    for position in range(period, len(source)):
        current = source.iloc[position]
        if math.isnan(current) or math.isnan(previous):
            previous = float("nan")
        else:
            previous = (previous * (period - 1) + current) / period
        result.iloc[position] = previous
    return result


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. Flat windows are neutral (50), not oversold."""

    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # The first delta is unknown; seed using the first `period` real changes.
    average_gain = _wilder_average(gain.iloc[1:].reset_index(drop=True), period)
    average_loss = _wilder_average(loss.iloc[1:].reset_index(drop=True), period)
    result = pd.Series(np.nan, index=close.index, dtype=float)
    for offset in range(period - 1, len(average_gain)):
        output_position = offset + 1
        avg_gain = average_gain.iloc[offset]
        avg_loss = average_loss.iloc[offset]
        if math.isnan(avg_gain) or math.isnan(avg_loss):
            continue
        if avg_gain == 0 and avg_loss == 0:
            result.iloc[output_position] = 50.0
        elif avg_loss == 0:
            result.iloc[output_position] = 100.0
        else:
            result.iloc[output_position] = 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def momentum(close: pd.Series, period: int) -> pd.Series:
    return close.astype(float).pct_change(periods=period, fill_method=None)


def annualized_volatility(
    close: pd.Series, window: int = 20, annualization: int = 252
) -> pd.Series:
    returns = close.astype(float).pct_change(fill_method=None)
    return returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(annualization)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.astype(float).shift(1)
    ranges = pd.concat(
        [
            high.astype(float) - low.astype(float),
            (high.astype(float) - previous_close).abs(),
            (low.astype(float) - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return _wilder_average(true_range(high, low, close), period)


def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    previous_average = volume.astype(float).shift(1).rolling(period, min_periods=period).mean()
    return volume.astype(float) / previous_average.replace(0, np.nan)


def drawdown_from_high(close: pd.Series, period: int = 252) -> pd.Series:
    rolling_high = close.astype(float).rolling(period, min_periods=period).max()
    return close.astype(float) / rolling_high - 1


def indicator_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """Add all strategy indicators to an OHLCV frame without mutating it."""

    required = {"high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
    output = bars.copy()
    output["sma20"] = sma(output["close"], 20)
    output["sma50"] = sma(output["close"], 50)
    output["sma200"] = sma(output["close"], 200)
    output["ema20"] = ema(output["close"], 20)
    output["ema50"] = ema(output["close"], 50)
    output["rsi14"] = rsi(output["close"], 14)
    for period in (5, 20, 63, 126):
        output[f"momentum{period}"] = momentum(output["close"], period)
    output["volatility20"] = annualized_volatility(output["close"])
    output["atr14"] = atr(output["high"], output["low"], output["close"], 14)
    output["relative_volume20"] = relative_volume(output["volume"], 20)
    output["drawdown_52w"] = drawdown_from_high(output["close"], 252)
    return output
