from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest.diagnostics import (
    add_post_exit_diagnostics,
    aggregate_post_exit,
    calculate_execution_metrics,
    calculate_position_metrics,
    finalize_position,
)
from trading_system.backtest.position_manager import PositionState
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestTrade,
    ScoreObservation,
    StopLossClassification,
    StrategyVariant,
)
from trading_system.models.market_data import DailyBar


def _state(**updates) -> PositionState:
    values = {
        "symbol": "AAA",
        "position_id": "C-000001-AAA-2024-01-02",
        "signal_date": date(2024, 1, 1),
        "entry_date": date(2024, 1, 2),
        "entry_reference_price": 100,
        "entry_price": 100,
        "quantity": 100,
        "initial_quantity": 100,
        "position_value": 10_000,
        "stop_price": 97,
        "target_price": None,
        "entry_commission": 0,
        "initial_entry_commission": 0,
        "entry_slippage": 0,
        "quality_score": 80,
        "valuation_score": 70,
        "opportunity_score": 60,
        "timing_score": 50,
        "entry_score": 75,
        "sector": "35",
        "variant": StrategyVariant.FULL,
        "last_price": 100,
        "holding_days": 4,
        "highest_price_since_entry": 110,
        "lowest_price_since_entry": 95,
        "score_history": [
            ScoreObservation(date=date(2024, 1, 1), total_score=75),
            ScoreObservation(date=date(2024, 1, 3), total_score=60),
            ScoreObservation(date=date(2024, 1, 4), total_score=90),
        ],
    }
    values.update(updates)
    return PositionState(**values)


def _leg(
    *,
    leg: int,
    quantity: float,
    position_value: float,
    exit_reference: float,
    pnl: float,
    reason: str,
    partial: bool,
    stop_price: float = 97,
) -> BacktestTrade:
    return BacktestTrade(
        symbol="AAA",
        signal_date=date(2024, 1, 1),
        entry_date=date(2024, 1, 2),
        entry_reference_price=100,
        entry_price=100,
        exit_date=date(2024, 1, 5),
        exit_reference_price=exit_reference,
        exit_price=exit_reference,
        quantity=quantity,
        position_value=position_value,
        stop_price=stop_price,
        target_price=None,
        quality_score=80,
        valuation_score=70,
        opportunity_score=60,
        timing_score=50,
        total_score=75,
        exit_reason=reason,
        pnl=pnl,
        return_pct=pnl / position_value,
        slippage=0,
        transaction_cost=0,
        holding_days=4,
        strategy_variant=StrategyVariant.FULL,
        gross_pnl=pnl,
        net_pnl=pnl,
        is_partial_exit=partial,
        position_id="C-000001-AAA-2024-01-02",
        execution_leg_id=f"C-000001-AAA-2024-01-02-L{leg:02d}",
    )


def _bar(session: date, *, high: str, low: str, close: str) -> DailyBar:
    return DailyBar(
        symbol="AAA",
        timestamp=datetime(session.year, session.month, session.day, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000,
    )


def test_partial_legs_aggregate_to_one_weighted_economic_position() -> None:
    legs = [
        _leg(
            leg=1,
            quantity=25,
            position_value=2_500,
            exit_reference=102,
            pnl=50,
            reason="partial_take_profit",
            partial=True,
        ),
        _leg(
            leg=2,
            quantity=75,
            position_value=7_500,
            exit_reference=97,
            pnl=-225,
            reason="stop_loss",
            partial=False,
        ),
    ]

    position = finalize_position(_state(quantity=75, position_value=7_500), legs)
    position_metrics = calculate_position_metrics([position], positions_opened=1)
    execution_metrics = calculate_execution_metrics(legs)

    assert position.execution_legs == 2
    assert position.position_return == pytest.approx(-0.0175)
    assert position_metrics.positions_opened == position_metrics.positions_closed == 1
    assert position_metrics.losing_positions == 1
    assert position_metrics.position_win_rate == 0
    assert execution_metrics.execution_legs == 2
    assert execution_metrics.execution_leg_win_rate == 0.5


def test_equal_partial_legs_have_exact_weighted_position_pnl() -> None:
    legs = [
        _leg(
            leg=1,
            quantity=50,
            position_value=5_000,
            exit_reference=102,
            pnl=100,
            reason="partial_take_profit",
            partial=True,
        ),
        _leg(
            leg=2,
            quantity=50,
            position_value=5_000,
            exit_reference=97,
            pnl=-150,
            reason="stop_loss",
            partial=False,
        ),
    ]

    position = finalize_position(_state(quantity=50, position_value=5_000), legs)

    assert position.gross_pnl == -50
    assert position.net_pnl == -50
    assert position.position_return == pytest.approx(-0.005)


def test_never_profitable_position_metric_includes_gap_stop() -> None:
    position = finalize_position(
        _state(highest_price_since_entry=100),
        [_leg(
            leg=1,
            quantity=100,
            position_value=10_000,
            exit_reference=95,
            pnl=-500,
            reason="stop_loss",
            partial=False,
        )],
    )

    metrics = calculate_position_metrics([position], positions_opened=1)

    assert position.stop_loss_classification is StopLossClassification.GAP_THROUGH_STOP
    assert metrics.never_profitable_stop_positions == 1
    assert metrics.gap_through_stop_positions == 1
    assert metrics.never_profitable_stop_rate == 1


def test_profit_capture_and_score_summary_are_position_level() -> None:
    leg = _leg(
        leg=1,
        quantity=100,
        position_value=10_000,
        exit_reference=106,
        pnl=600,
        reason="atr_trailing_stop",
        partial=False,
    )

    position = finalize_position(_state(), [leg])

    assert position.maximum_favorable_excursion == pytest.approx(0.10)
    assert position.position_return == pytest.approx(0.06)
    assert position.profit_capture_ratio == pytest.approx(0.60)
    assert position.profit_giveback == pytest.approx(0.04)
    assert position.minimum_score_during_trade == 60
    assert position.maximum_score_during_trade == 90
    assert position.exit_score == 90

    no_mfe = finalize_position(
        _state(highest_price_since_entry=100),
        [_leg(
            leg=1,
            quantity=100,
            position_value=10_000,
            exit_reference=97,
            pnl=-300,
            reason="stop_loss",
            partial=False,
        )],
    )
    assert no_mfe.profit_capture_ratio is None


@pytest.mark.parametrize(
    ("highest", "reference", "expected"),
    [
        (100, 97, StopLossClassification.NEVER_PROFITABLE),
        (102, 97, StopLossClassification.PROFITABLE_THEN_STOPPED),
        (102, 95, StopLossClassification.GAP_THROUGH_STOP),
    ],
)
def test_stop_loss_classification(highest, reference, expected) -> None:
    position = finalize_position(
        _state(highest_price_since_entry=highest),
        [_leg(
            leg=1,
            quantity=100,
            position_value=10_000,
            exit_reference=reference,
            pnl=(reference - 100) * 100,
            reason="stop_loss",
            partial=False,
        )],
    )

    assert position.stop_loss_classification is expected


def test_post_exit_windows_require_complete_trading_day_horizons(tmp_path) -> None:
    database = Database(tmp_path / "post-exit.sqlite3")
    database.initialize()
    sessions = [date(2024, 1, day) for day in (8, 9, 10, 11, 12)]
    database.upsert_bars(
        [
            _bar(sessions[0], high="110", low="95", close="102"),
            _bar(sessions[1], high="103", low="97", close="101"),
            _bar(sessions[2], high="108", low="96", close="105"),
            _bar(sessions[3], high="107", low="94", close="99"),
            _bar(sessions[4], high="106", low="93", close="98"),
        ]
    )
    position = finalize_position(
        _state(),
        [_leg(
            leg=1,
            quantity=100,
            position_value=10_000,
            exit_reference=100,
            pnl=0,
            reason="take_profit",
            partial=False,
        )],
    )

    diagnosed = add_post_exit_diagnostics(position, database, sessions[-1])

    assert diagnosed.post_exit_return_1d == pytest.approx(0.02)
    assert diagnosed.post_exit_return_3d == pytest.approx(0.05)
    assert diagnosed.post_exit_return_5d == pytest.approx(-0.02)
    assert diagnosed.post_exit_return_10d is None
    assert diagnosed.post_exit_mfe_1d == pytest.approx(0.10)
    assert diagnosed.post_exit_mae_1d == pytest.approx(-0.05)
    assert diagnosed.post_exit_mfe_5d == pytest.approx(0.10)
    assert diagnosed.post_exit_mae_5d == pytest.approx(-0.07)

    aggregate = aggregate_post_exit([diagnosed])[0]
    assert aggregate.observations_1d == 1
    assert aggregate.observations_3d == 1
    assert aggregate.observations_5d == 1
    assert aggregate.observations_10d == 0
