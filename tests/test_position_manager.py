from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest.position_manager import (
    ExitReason,
    PositionAction,
    PositionManager,
    PositionState,
)
from trading_system.config import (
    AtrTrailingStopConfig,
    MaxHoldConfig,
    PartialTakeProfitConfig,
    PartialTakeProfitLevel,
    PortfolioRotationConfig,
    PositionManagementConfig,
    SignalDecayConfig,
    StopLossConfig,
    TakeProfitConfig,
    TrailingStopConfig,
)
from trading_system.models.backtest import StrategyVariant
from trading_system.models.market_data import DailyBar


def _bar(*, opening=100, high=101, low=99, close=100) -> DailyBar:
    return DailyBar(
        symbol="AAA",
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=Decimal(str(opening)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=1_000,
    )


def _position(**updates) -> PositionState:
    values = {
        "symbol": "AAA",
        "signal_date": date(2024, 1, 1),
        "entry_date": date(2024, 1, 2),
        "entry_reference_price": 100,
        "entry_price": 100,
        "quantity": 100,
        "initial_quantity": 100,
        "position_value": 10_000,
        "stop_price": None,
        "target_price": None,
        "entry_commission": 0,
        "entry_slippage": 0,
        "quality_score": 80,
        "valuation_score": 80,
        "opportunity_score": 80,
        "timing_score": 80,
        "entry_score": 80,
        "sector": "35",
        "variant": StrategyVariant.FULL,
        "last_price": 100,
        "current_atr": 2,
        "holding_days": 10,
    }
    values.update(updates)
    return PositionState(**values)


def _config(**updates) -> PositionManagementConfig:
    base = PositionManagementConfig(
        stop_loss=StopLossConfig(enabled=False),
        take_profit=TakeProfitConfig(enabled=False),
        max_hold=MaxHoldConfig(enabled=False, days=10, mode="disabled"),
    )
    return base.model_copy(update=updates)


@pytest.mark.parametrize(
    ("max_hold", "score", "expected"),
    [
        (MaxHoldConfig(enabled=True, days=10, mode="hard"), 80, PositionAction.SELL),
        (MaxHoldConfig(enabled=True, days=10, mode="review"), 76, PositionAction.HOLD),
        (MaxHoldConfig(enabled=True, days=10, mode="review"), 50, PositionAction.SELL),
        (MaxHoldConfig(enabled=False, days=10, mode="hard"), 0, PositionAction.HOLD),
        (MaxHoldConfig(enabled=True, days=10, mode="disabled"), 0, PositionAction.HOLD),
    ],
)
def test_max_hold_modes(max_hold, score, expected) -> None:
    decision = PositionManager(_config(max_hold=max_hold)).evaluate_close(
        _position(), 100, current_score=score
    )

    assert decision.action is expected
    if expected is PositionAction.SELL:
        assert decision.reason is ExitReason.MAX_HOLD


def test_fixed_stop_and_take_profit_use_stop_first() -> None:
    config = _config(
        stop_loss=StopLossConfig(enabled=True, percent=0.03),
        take_profit=TakeProfitConfig(enabled=True, percent=0.02),
    )
    manager = PositionManager(config)
    position = _position(stop_price=97, target_price=102)

    stopped = manager.evaluate_intrabar(position, _bar(high=102, low=97))
    profited = manager.evaluate_intrabar(position, _bar(high=102, low=98))
    assert stopped.reason is ExitReason.STOP_LOSS
    assert profited.reason is ExitReason.TAKE_PROFIT


def test_profit_trailing_stop_raises_only_and_never_uses_same_bar_low() -> None:
    manager = PositionManager(
        _config(
            trailing_stop=TrailingStopConfig(
                enabled=True, activation_profit=0.01, trailing_distance=0.01
            )
        )
    )
    position = _position(holding_days=1)
    activation_bar = _bar(high=105, low=99, close=104)

    assert manager.evaluate_intrabar(position, activation_bar).action is PositionAction.HOLD
    manager.update_after_bar(position, activation_bar)
    assert position.trailing_stop_price == pytest.approx(103.95)

    manager.update_after_bar(position, _bar(high=104, low=103, close=103.5))
    assert position.trailing_stop_price == pytest.approx(103.95)
    decision = manager.evaluate_intrabar(position, _bar(high=104, low=103))
    assert decision.reason is ExitReason.TRAILING_STOP


def test_atr_trailing_stop_uses_supplied_completed_bar_atr_and_never_falls() -> None:
    manager = PositionManager(
        _config(
            atr_trailing_stop=AtrTrailingStopConfig(
                enabled=True, atr_period=14, atr_multiplier=1, activation_profit=0
            )
        )
    )
    position = _position(current_atr=2)

    manager.update_after_bar(position, _bar(high=105), next_atr=2)
    assert position.atr_trailing_stop_price == pytest.approx(103)
    manager.update_after_bar(position, _bar(high=104), next_atr=4)
    assert position.atr_trailing_stop_price == pytest.approx(103)


def test_partial_level_triggers_once() -> None:
    manager = PositionManager(
        _config(
            partial_take_profit=PartialTakeProfitConfig(
                enabled=True,
                levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
            )
        )
    )
    position = _position()

    first = manager.evaluate_intrabar(position, _bar(high=101.5))
    assert first.action is PositionAction.PARTIAL_SELL
    assert first.quantity == 50
    position.partial_exit_levels_triggered.add(0)
    assert manager.evaluate_intrabar(position, _bar(high=110)).action is PositionAction.HOLD


def test_signal_decay_handles_missing_zero_and_real_decay() -> None:
    manager = PositionManager(
        _config(signal_decay=SignalDecayConfig(enabled=True, minimum_score_ratio=0.75))
    )

    assert manager.evaluate_close(_position(), 100, current_score=70).reason is None
    assert (
        manager.evaluate_close(_position(), 100, current_score=50).reason
        is ExitReason.SIGNAL_DECAY
    )
    assert manager.evaluate_close(_position(entry_score=0), 100, current_score=0).reason is None
    assert manager.evaluate_close(_position(), 100, current_score=None).reason is None


def test_portfolio_rotation_requires_better_external_candidate_and_covers_costs() -> None:
    rule = PortfolioRotationConfig(
        enabled=True, minimum_score_improvement=0.15, minimum_holding_days=1
    )
    manager = PositionManager(_config(portfolio_rotation=rule), slippage_bps=5, commission_bps=5)

    decision = manager.evaluate_close(
        _position(),
        100,
        current_score=70,
        best_candidate_symbol="BBB",
        best_candidate_score=90,
    )
    assert decision.reason is ExitReason.PORTFOLIO_ROTATION

    too_small = manager.evaluate_close(
        _position(),
        100,
        current_score=70,
        best_candidate_symbol="BBB",
        best_candidate_score=70.05,
    )
    assert too_small.action is PositionAction.HOLD
