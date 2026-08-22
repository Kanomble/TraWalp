from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest.position_manager import (
    ExitReason,
    PositionAction,
    PositionManager,
    PositionState,
    ProfitLockState,
    economic_break_even_stop,
)
from trading_system.config import (
    AtrTrailingStopConfig,
    MaxHoldConfig,
    PartialTakeProfitConfig,
    PartialTakeProfitLevel,
    PortfolioRotationConfig,
    PositionManagementConfig,
    ProfitLockConfig,
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
        "position_id": "position-1",
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
        "initial_entry_commission": 0,
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


@pytest.mark.parametrize(
    ("bar", "expected_reference"),
    [
        (_bar(opening=106, high=107, low=90, close=95), 105),
        (_bar(opening=95, high=96, low=90, close=92), 95),
    ],
)
def test_highest_active_long_stop_wins_when_multiple_stops_are_breached(
    bar: DailyBar, expected_reference: float
) -> None:
    manager = PositionManager(
        _config(
            stop_loss=StopLossConfig(enabled=True, percent=0.03),
            trailing_stop=TrailingStopConfig(enabled=True),
            atr_trailing_stop=AtrTrailingStopConfig(enabled=True),
        )
    )
    position = _position(
        stop_price=97,
        trailing_stop_price=105,
        atr_trailing_stop_price=103,
    )

    decision = (
        manager.evaluate_open(position, bar)
        if float(bar.open) < 105
        else manager.evaluate_intrabar(position, bar)
    )

    assert decision.reason is ExitReason.TRAILING_STOP
    assert decision.reference_price == expected_reference


def test_lower_partial_profit_executes_before_higher_full_target() -> None:
    manager = PositionManager(
        _config(
            take_profit=TakeProfitConfig(enabled=True, percent=0.12),
            partial_take_profit=PartialTakeProfitConfig(
                enabled=True,
                levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
            ),
        )
    )
    position = _position(target_price=112)

    partial = manager.evaluate_intrabar(position, _bar(high=115, low=99))
    assert partial.reason is ExitReason.PARTIAL_TAKE_PROFIT
    assert partial.reference_price == pytest.approx(101.5)
    assert partial.quantity == 50

    position.partial_exit_levels_triggered.add(0)
    full = manager.evaluate_intrabar(position, _bar(high=115, low=99))
    assert full.reason is ExitReason.TAKE_PROFIT
    assert full.reference_price == 112

    gap_position = _position(target_price=112)
    gap_partial = manager.evaluate_open(
        gap_position, _bar(opening=115, high=116, low=114, close=115)
    )
    assert gap_partial.reason is ExitReason.PARTIAL_TAKE_PROFIT
    assert gap_partial.reference_price == 115
    gap_position.partial_exit_levels_triggered.add(0)
    gap_position.quantity = 50
    gap_full = manager.evaluate_open(
        gap_position, _bar(opening=115, high=116, low=114, close=115)
    )
    assert gap_full.reason is ExitReason.TAKE_PROFIT
    assert gap_full.reference_price == 115


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


def test_profit_lock_waits_for_completed_bar_and_activates_next_bar() -> None:
    manager = PositionManager(
        _config(
            stop_loss=StopLossConfig(enabled=True, percent=0.03),
            profit_lock=ProfitLockConfig(enabled=True),
        )
    )
    position = _position(stop_price=97, initial_risk_per_share_R=3, holding_days=1)
    activation_bar = _bar(high=103, low=99, close=102)

    assert manager.evaluate_intrabar(position, activation_bar).action is PositionAction.HOLD
    manager.update_after_bar(position, activation_bar)

    assert position.profit_lock_state is ProfitLockState.BREAK_EVEN_LOCK
    assert position.profit_lock_stop_price == pytest.approx(100)
    assert position.break_even_lock_timestamp == activation_bar.timestamp
    next_bar = manager.evaluate_intrabar(position, _bar(high=101, low=99, close=100))
    assert next_bar.reason is ExitReason.PROFIT_LOCK
    assert next_bar.reference_price == pytest.approx(100)


def test_profit_lock_no_activation_two_r_raise_only_and_gap() -> None:
    manager = PositionManager(
        _config(
            stop_loss=StopLossConfig(enabled=True, percent=0.03),
            atr_trailing_stop=AtrTrailingStopConfig(enabled=True),
            profit_lock=ProfitLockConfig(enabled=True),
        )
    )
    position = _position(
        stop_price=97,
        atr_trailing_stop_price=105,
        initial_risk_per_share_R=3,
    )

    manager.update_after_bar(position, _bar(high=102.9, low=99))
    assert position.profit_lock_stop_price is None
    manager.update_after_bar(position, _bar(high=106, low=99))
    assert position.profit_lock_state is ProfitLockState.ONE_R_LOCK
    assert position.profit_lock_stop_price == pytest.approx(103)
    assert position.one_r_lock_timestamp is not None

    higher_stop = manager.evaluate_intrabar(position, _bar(high=106, low=104))
    assert higher_stop.reason is ExitReason.ATR_TRAILING_STOP
    gap = manager.evaluate_open(position, _bar(opening=100, high=101, low=99, close=100))
    assert gap.reason is ExitReason.ATR_TRAILING_STOP
    assert gap.reference_price == 100
    assert position.gap_affected_trade is True


def test_economic_break_even_reference_offsets_fill_and_commissions() -> None:
    reference = economic_break_even_stop(
        100.05, slippage_rate=0.0005, commission_rate=0.0005
    )
    sell_fill = reference * (1 - 0.0005)
    net_exit = sell_fill * (1 - 0.0005)

    assert net_exit == pytest.approx(100.05 * (1 + 0.0005))


def test_profit_lock_survives_original_quantity_runner_partial() -> None:
    manager = PositionManager(
        _config(
            partial_take_profit=PartialTakeProfitConfig(
                enabled=True,
                levels=[
                    PartialTakeProfitLevel(
                        profit=0.12,
                        sell_fraction=0.33,
                        quantity_basis="original",
                    )
                ],
            ),
            profit_lock=ProfitLockConfig(enabled=True),
        )
    )
    position = _position(initial_risk_per_share_R=3)

    partial = manager.evaluate_intrabar(position, _bar(high=112.1, low=99))
    assert partial.quantity == pytest.approx(33)
    position.quantity -= float(partial.quantity)
    position.partial_exit_levels_triggered.add(0)
    manager.update_after_bar(position, _bar(high=106, low=99))

    assert position.quantity == pytest.approx(67)
    assert position.profit_lock_stop_price == pytest.approx(103)


def test_trail_guard_blocks_entry_bar_trail_but_not_catastrophe_or_partial() -> None:
    manager = PositionManager(
        _config(
            stop_loss=StopLossConfig(enabled=True, percent=0.03),
            atr_trailing_stop=AtrTrailingStopConfig(
                enabled=True,
                atr_period=14,
                atr_multiplier=1,
                activation_profit=0,
                minimum_completed_bars_before_activation=1,
            ),
            partial_take_profit=PartialTakeProfitConfig(
                enabled=True,
                levels=[PartialTakeProfitLevel(profit=0.015, sell_fraction=0.5)],
            ),
        )
    )
    position = _position(stop_price=97, current_atr=2, holding_days=1)
    manager.activate_at_open(position, 100)
    assert position.atr_trailing_stop_price is None

    entry_bar = _bar(high=101, low=97.5, close=100)
    assert manager.evaluate_intrabar(position, entry_bar).action is PositionAction.HOLD
    manager.update_after_bar(position, entry_bar, next_atr=2)
    assert position.completed_bars_before_trail_arm == 1
    assert position.atr_trailing_stop_price == pytest.approx(99)
    guarded_exit = manager.evaluate_intrabar(position, _bar(low=98.5))
    assert guarded_exit.reason is ExitReason.ATR_TRAILING_STOP

    catastrophe = _position(stop_price=97, current_atr=2, holding_days=1)
    assert manager.evaluate_intrabar(catastrophe, _bar(low=96)).reason is ExitReason.STOP_LOSS
    partial = _position(stop_price=97, current_atr=2, holding_days=1)
    decision = manager.evaluate_intrabar(partial, _bar(high=102, low=97.5))
    assert decision.reason is ExitReason.PARTIAL_TAKE_PROFIT
