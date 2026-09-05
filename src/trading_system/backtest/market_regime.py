"""Point-in-time SPY market-regime decisions for capacity research."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import pandas as pd

from trading_system.models.market_data import DailyBar
from trading_system.technical.indicators import momentum, sma

SPY_SMA_PERIOD = 200
SPY_MOMENTUM_PERIOD = 126
RISK_OFF_CAPACITY = 1
RISK_ON_CAPACITY = 5


class MarketRegimeState(StrEnum):
    """Observable state at the close of one market session."""

    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    UNAVAILABLE = "UNAVAILABLE"
    STATIC_CONTROL = "STATIC_CONTROL"


class RegimeCapacityRule(StrEnum):
    """The four frozen rules admitted by the registered research family."""

    CONTROL_C1 = "CONTROL_C1"
    CONTROL_C5 = "CONTROL_C5"
    REGIME_SMA200 = "REGIME_SMA200"
    REGIME_SMA200_MOM126 = "REGIME_SMA200_MOM126"


@dataclass(frozen=True, slots=True)
class RegimeCapacityDecision:
    """One reproducible capacity decision made from data available by ``session``."""

    session: date
    regime: MarketRegimeState
    target_capacity: int
    spy_close: float | None
    spy_sma200: float | None
    spy_momentum126: float | None
    reason: str


class MarketRegimeCapacitySchedule:
    """Immutable trailing-indicator lookup with no provider or network dependency.

    Indicator values are indexed by the date of each completed local SPY Daily bar.
    Looking up session ``T`` never searches beyond ``T`` and requires an SPY close
    stamped on ``T``; missing bars or warmup therefore fail closed to capacity one.
    """

    def __init__(self, bars: Iterable[DailyBar], rule: RegimeCapacityRule) -> None:
        self.rule = rule
        ordered = sorted(
            (bar for bar in bars if bar.symbol.upper() == "SPY"),
            key=lambda bar: bar.timestamp,
        )
        closes = pd.Series(
            [float(bar.close) for bar in ordered],
            index=[bar.timestamp.date() for bar in ordered],
            dtype=float,
        )
        if closes.index.has_duplicates:
            closes = closes.groupby(level=0).last()
        self._close_by_session = closes.to_dict()
        self._sma_by_session = sma(closes, SPY_SMA_PERIOD).to_dict()
        self._momentum_by_session = momentum(closes, SPY_MOMENTUM_PERIOD).to_dict()

    @classmethod
    def static(cls, rule: RegimeCapacityRule) -> MarketRegimeCapacitySchedule:
        if rule not in {RegimeCapacityRule.CONTROL_C1, RegimeCapacityRule.CONTROL_C5}:
            raise ValueError("static regime schedule requires CONTROL_C1 or CONTROL_C5")
        return cls((), rule)

    def __call__(self, session: date) -> int:
        return self.decision(session).target_capacity

    def decision(self, session: date) -> RegimeCapacityDecision:
        if self.rule is RegimeCapacityRule.CONTROL_C1:
            return _static_decision(session, RISK_OFF_CAPACITY, self.rule)
        if self.rule is RegimeCapacityRule.CONTROL_C5:
            return _static_decision(session, RISK_ON_CAPACITY, self.rule)

        close = _finite(self._close_by_session.get(session))
        average = _finite(self._sma_by_session.get(session))
        momentum126 = _finite(self._momentum_by_session.get(session))
        if close is None:
            return _unavailable_decision(
                session,
                close,
                average,
                momentum126,
                "SPY Daily close unavailable for the signal session",
            )
        if average is None:
            return _unavailable_decision(
                session,
                close,
                average,
                momentum126,
                "SPY SMA200 unavailable: fewer than 200 completed Daily bars",
            )
        if self.rule is RegimeCapacityRule.REGIME_SMA200_MOM126 and momentum126 is None:
            return _unavailable_decision(
                session,
                close,
                average,
                momentum126,
                "SPY momentum126 unavailable: fewer than 127 completed Daily bars",
            )

        above_sma = close > average
        positive_momentum = momentum126 is not None and momentum126 > 0
        risk_on = above_sma and (self.rule is RegimeCapacityRule.REGIME_SMA200 or positive_momentum)
        if risk_on:
            rule_text = (
                "SPY close > SMA200"
                if self.rule is RegimeCapacityRule.REGIME_SMA200
                else "SPY close > SMA200 and momentum126 > 0"
            )
            return RegimeCapacityDecision(
                session=session,
                regime=MarketRegimeState.RISK_ON,
                target_capacity=RISK_ON_CAPACITY,
                spy_close=close,
                spy_sma200=average,
                spy_momentum126=momentum126,
                reason=f"RISK_ON: {rule_text}",
            )

        failed = ["SPY close <= SMA200"] if not above_sma else []
        if self.rule is RegimeCapacityRule.REGIME_SMA200_MOM126 and not positive_momentum:
            failed.append("SPY momentum126 <= 0")
        return RegimeCapacityDecision(
            session=session,
            regime=MarketRegimeState.RISK_OFF,
            target_capacity=RISK_OFF_CAPACITY,
            spy_close=close,
            spy_sma200=average,
            spy_momentum126=momentum126,
            reason=f"RISK_OFF: {'; '.join(failed)}",
        )


def _static_decision(
    session: date,
    target_capacity: int,
    rule: RegimeCapacityRule,
) -> RegimeCapacityDecision:
    return RegimeCapacityDecision(
        session=session,
        regime=MarketRegimeState.STATIC_CONTROL,
        target_capacity=target_capacity,
        spy_close=None,
        spy_sma200=None,
        spy_momentum126=None,
        reason=f"STATIC_CONTROL: fixed target_capacity={target_capacity} ({rule.value})",
    )


def _unavailable_decision(
    session: date,
    close: float | None,
    average: float | None,
    momentum126: float | None,
    reason: str,
) -> RegimeCapacityDecision:
    return RegimeCapacityDecision(
        session=session,
        regime=MarketRegimeState.UNAVAILABLE,
        target_capacity=RISK_OFF_CAPACITY,
        spy_close=close,
        spy_sma200=average,
        spy_momentum126=momentum126,
        reason=f"UNAVAILABLE: {reason}; conservative target_capacity=1",
    )


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
