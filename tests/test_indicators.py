import numpy as np
import pandas as pd
import pytest

from trading_system.config import TechnicalConfig
from trading_system.technical import momentum as momentum_module
from trading_system.technical.indicators import (
    atr,
    drawdown_from_high,
    ema,
    indicator_frame,
    momentum,
    recovery_from_low,
    relative_volume,
    rolling_max_drawdown,
    rsi,
    sma,
)


def test_sma_and_ema() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert sma(values, 3).iloc[-1] == 4
    expected = values.ewm(span=3, adjust=False, min_periods=3).mean()
    pd.testing.assert_series_equal(ema(values, 3), expected)


def test_rsi_wilder_edge_cases() -> None:
    rising = pd.Series(np.arange(1.0, 30.0))
    falling = pd.Series(np.arange(30.0, 1.0, -1))
    flat = pd.Series([10.0] * 30)
    assert rsi(rising).iloc[-1] == 100
    assert rsi(falling).iloc[-1] == 0
    assert rsi(flat).iloc[-1] == 50


def test_momentum_atr_drawdown_and_relative_volume() -> None:
    close = pd.Series([10.0, 11.0, 12.0, 13.0])
    assert momentum(close, 2).iloc[-1] == pytest.approx(13 / 11 - 1)
    high = pd.Series([11.0] * 20)
    low = pd.Series([9.0] * 20)
    stable_close = pd.Series([10.0] * 20)
    assert atr(high, low, stable_close, 14).iloc[-1] == 2

    prices = pd.Series([100.0] * 251 + [80.0])
    assert drawdown_from_high(prices).iloc[-1] == pytest.approx(-0.2)
    volumes = pd.Series([100.0] * 20 + [200.0])
    assert relative_volume(volumes).iloc[-1] == 2


def test_relative_volume_excludes_current_day_from_average() -> None:
    volumes = pd.Series([100.0] * 19 + [1000.0, 200.0])
    assert relative_volume(volumes).iloc[-1] == pytest.approx(200 / 145)


def test_indicator_frame_contains_complete_milestone_two_set() -> None:
    close = pd.Series(np.linspace(100, 130, 300))
    bars = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    output = indicator_frame(bars)
    expected = {
        "sma20",
        "sma50",
        "sma200",
        "ema20",
        "ema50",
        "rsi14",
        "momentum5",
        "momentum20",
        "momentum63",
        "momentum126",
        "volatility20",
        "atr14",
        "relative_volume20",
        "drawdown_52w",
        "drawdown_63d",
        "recovery_from_63d_low",
        "max_drawdown_126d",
        "sma200_distance",
    }
    assert expected <= set(output.columns)
    assert output.iloc[-1][list(expected)].notna().all()


def test_recovery_snapshot_requires_prior_oversold_and_current_rise(monkeypatch) -> None:
    size = 20
    frame = pd.DataFrame(
        {
            "close": [100.0] * size,
            "sma20": [98.0] * (size - 5) + [99.0] * 5,
            "sma50": [97.0] * size,
            "sma200": [95.0] * size,
            "rsi14": [40.0] * (size - 5) + [29.0, 31.0, 34.0, 38.0, 42.0],
            "momentum5": [0.02] * size,
            "momentum20": [-0.2] * (size - 5) + [-0.1] * 5,
            "momentum63": [-0.1] * size,
            "momentum126": [-0.05] * size,
            "volatility20": [0.3] * size,
            "atr14": [2.0] * size,
            "relative_volume20": [1.4] * size,
            "drawdown_52w": [-0.25] * size,
            "drawdown_63d": [-0.10] * size,
            "recovery_from_63d_low": [0.10] * size,
            "max_drawdown_126d": [-0.30] * size,
            "sma200_distance": [100 / 95 - 1] * size,
        }
    )
    monkeypatch.setattr(momentum_module, "indicator_frame", lambda _bars: frame)
    snapshot = momentum_module.technical_snapshot(pd.DataFrame({"unused": [1]}), TechnicalConfig())
    assert snapshot.rsi_recovery is True
    assert snapshot.sma20_rising is True
    assert snapshot.momentum20_improving is True


def test_new_rolling_price_features_have_exact_window_semantics() -> None:
    insufficient_63 = pd.Series([100.0] * 62)
    assert pd.isna(drawdown_from_high(insufficient_63, 63).iloc[-1])
    assert pd.isna(recovery_from_low(insufficient_63, 63).iloc[-1])

    new_high = pd.Series(np.linspace(80.0, 100.0, 63))
    assert drawdown_from_high(new_high, 63).iloc[-1] == pytest.approx(0)
    pullback = pd.Series([100.0] * 62 + [90.0])
    assert drawdown_from_high(pullback, 63).iloc[-1] == pytest.approx(-0.10)

    at_low = pd.Series([100.0] * 62 + [80.0])
    assert recovery_from_low(at_low, 63).iloc[-1] == pytest.approx(0)
    recovered = pd.Series([80.0] + [100.0] * 61 + [88.0])
    assert recovery_from_low(recovered, 63).iloc[-1] == pytest.approx(0.10)


def test_max_drawdown_126d_is_worst_peak_to_trough_not_current_drawdown() -> None:
    path = pd.Series([100.0] * 123 + [100.0, 70.0, 90.0])
    assert rolling_max_drawdown(path, 126).iloc[-1] == pytest.approx(-0.30)
    assert pd.isna(rolling_max_drawdown(path.iloc[:-1], 126).iloc[-1])
    rising = pd.Series(np.linspace(1.0, 126.0, 126))
    falling = pd.Series(np.linspace(126.0, 1.0, 126))
    assert rolling_max_drawdown(rising, 126).iloc[-1] == pytest.approx(0)
    assert rolling_max_drawdown(falling, 126).iloc[-1] == pytest.approx(1 / 126 - 1)


@pytest.mark.parametrize(
    ("prices", "relation"),
    [
        ([100.0] * 199 + [120.0], "above"),
        ([100.0] * 199 + [80.0], "below"),
        ([100.0] * 200, "equal"),
    ],
)
def test_sma200_distance(prices: list[float], relation: str) -> None:
    close = pd.Series(prices)
    frame = indicator_frame(
        pd.DataFrame({"high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    )
    expected = prices[-1] / (sum(prices[-200:]) / 200) - 1
    assert frame["sma200_distance"].iloc[-1] == pytest.approx(expected)
    assert {"above": expected > 0, "below": expected < 0, "equal": expected == 0}[relation]

    unavailable = indicator_frame(
        pd.DataFrame(
            {
                "high": close.iloc[:199] + 1,
                "low": close.iloc[:199] - 1,
                "close": close.iloc[:199],
                "volume": 1000,
            }
        )
    )
    assert pd.isna(unavailable["sma200_distance"].iloc[-1])
