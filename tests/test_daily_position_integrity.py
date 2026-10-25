"""CC-01: only synthetic screens/bars and temporary SQLite databases."""

import sqlite3
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_champion_consolidation import Screens, bar, record
from test_champion_consolidation import offline as offline

from trading_system.backtest import engine as engine_module
from trading_system.backtest.data_integrity import (
    DataIntegrityError,
    DataIntegrityMode,
    validate_session_position_data,
)
from trading_system.backtest.engine import BacktestEngine, _backtest_sessions
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.champion import run_champion_backtest
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
from trading_system.models.market_data import BarTimeframe

SIGNAL = date(2022, 3, 4)
ENTRY = date(2022, 3, 7)
MISSING = date(2022, 3, 8)
NEXT = date(2022, 3, 9)


@pytest.fixture
def config():
    return StrategyConfig.model_validate(_read_yaml(Path("config/strategy.yaml")))


@pytest.fixture
def missing_case(tmp_path):
    db = Database(tmp_path / "case.sqlite3")
    db.initialize()
    db.upsert_bars(
        [bar("ABBV", day) for day in (SIGNAL, ENTRY, NEXT)]
        + [bar("SPY", day) for day in (SIGNAL, ENTRY, MISSING, NEXT)]
    )
    return db


def run_case(db, config, end=NEXT, **kwargs):
    return BacktestEngine(
        db, config, screen_source=Screens(SIGNAL, (record("ABBV"),)), **kwargs
    ).run(SIGNAL, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset)


def test_reproduction_legacy_skips_held_management_and_marks_previous_close(missing_case, config):
    result = run_case(missing_case, config)
    before = next(p for p in result.equity_curve if p.date == ENTRY)
    missing = next(p for p in result.equity_curve if p.date == MISSING)
    assert missing.active_positions == 1
    assert (missing.market_value, missing.unrealized_pnl) == (
        before.market_value,
        before.unrealized_pnl,
    )
    assert not any(t.exit_date == MISSING for t in result.trades)
    assert result.positions[0].exit_date == NEXT
    assert result.positions[0].holding_days == 3


def test_reproduction_legacy_final_liquidation_uses_stale_bar(missing_case, config):
    result = run_case(missing_case, config, end=MISSING)
    assert result.actual_end == MISSING
    assert result.positions[0].exit_date == ENTRY
    assert result.positions[0].exit_reference_price == 100


def test_reproduction_legacy_loses_globally_absent_official_session(tmp_path):
    db = Database(tmp_path / "absent.sqlite3")
    db.initialize()
    db.upsert_bars([bar("SPY", day) for day in (ENTRY, NEXT)])
    assert _backtest_sessions(db, ENTRY, NEXT) == [ENTRY, NEXT]


def validate(session=MISSING, **updates):
    values = dict(
        bars={"ABBV": bar("ABBV", session)},
        held_positions={"ABBV": ENTRY},
        pending_entries={},
        previous_processed_session=ENTRY,
        now=datetime(2024, 1, 1, tzinfo=UTC),
    )
    values.update(updates)
    return validate_session_position_data(session, **values)


def test_valid_session_preflight_changes_nothing():
    inputs = dict(
        bars={"ABBV": bar("ABBV", MISSING)},
        held_positions={"ABBV": ENTRY},
        pending_entries={},
        previous_processed_session=ENTRY,
        now=datetime(2024, 1, 1, tzinfo=UTC),
    )
    before = deepcopy(inputs)
    validate_session_position_data(MISSING, **inputs)
    assert inputs == before


def test_missing_held_error_is_structured_and_inputs_unchanged():
    held = {"ABBV": ENTRY}
    with pytest.raises(DataIntegrityError) as failure:
        validate(bars={}, held_positions=held)
    assert failure.value.as_dict() == dict(
        status="DATA_INTEGRITY_FAILURE",
        code="HELD_POSITION_BAR_MISSING",
        symbol="ABBV",
        session="2022-03-08",
        role="HELD_POSITION",
        position_entry_session="2022-03-07",
        holding_session_number=2,
    )
    assert held == {"ABBV": ENTRY}
    assert not isinstance(failure.value, ValueError)


@pytest.mark.parametrize(
    "invalid,suffix",
    [
        (bar("ABBV", ENTRY), "STALE"),
        (bar("OTHER", MISSING), "INVALID"),
        (bar("ABBV", MISSING).model_copy(update={"timeframe": BarTimeframe.MINUTES_15}), "INVALID"),
        (bar("ABBV", MISSING).model_copy(update={"low": Decimal("200")}), "INVALID"),
        (bar("ABBV", MISSING).model_copy(update={"close": Decimal("NaN")}), "INVALID"),
        (bar("ABBV", MISSING).model_copy(update={"volume": 0}), "INVALID"),
    ],
)
def test_held_stale_invalid_ohlc_and_zero_volume_substitutes_fail(invalid, suffix):
    with pytest.raises(DataIntegrityError) as failure:
        validate(bars={"ABBV": invalid})
    assert failure.value.code == "HELD_POSITION_BAR_" + suffix


def test_failure_order_held_before_pending_then_alphabetical():
    with pytest.raises(DataIntegrityError) as failure:
        validate(
            bars={}, held_positions={"ZZZ": ENTRY, "ABBV": ENTRY}, pending_entries={"AAA": ENTRY}
        )
    assert (failure.value.role, failure.value.symbol) == ("HELD_POSITION", "ABBV")


def test_noncritical_symbol_absence_or_bad_bar_does_not_stop_preflight():
    validate(bars={"ABBV": bar("ABBV", MISSING), "UNSELECTED": object()})
    validate(bars={}, held_positions={})


@pytest.mark.parametrize("session", [date(2022, 3, 5), date(2022, 4, 15)])
def test_weekend_and_exchange_holiday_not_sessions(session):
    with pytest.raises(DataIntegrityError) as failure:
        validate(session=session, held_positions={}, previous_processed_session=None)
    assert failure.value.code == "SESSION_NOT_OFFICIAL"


def test_session_must_be_completed_at_official_close():
    close = regular_session_bounds(MISSING)[1]
    with pytest.raises(DataIntegrityError) as failure:
        validate(now=close - timedelta(microseconds=1))
    assert failure.value.code == "SESSION_NOT_COMPLETE"
    validate(now=close)
    with pytest.raises(DataIntegrityError) as failure:
        validate(now=close.replace(tzinfo=None))
    assert failure.value.code == "SESSION_CLOCK_INVALID"


@pytest.mark.parametrize("previous", [SIGNAL, MISSING, NEXT])
def test_sessions_cannot_be_skipped_repeated_or_reversed(previous):
    with pytest.raises(DataIntegrityError) as failure:
        validate(previous_processed_session=previous)
    assert failure.value.code == "OFFICIAL_SESSION_SEQUENCE_GAP"


def test_friday_to_monday_is_consecutive_official_sequence():
    validate(session=ENTRY, held_positions={}, previous_processed_session=SIGNAL)


def test_pending_entry_requires_exact_next_official_session_bar():
    validate(held_positions={}, pending_entries={"ABBV": ENTRY})
    with pytest.raises(DataIntegrityError) as failure:
        validate(bars={}, held_positions={}, pending_entries={"ABBV": ENTRY})
    assert failure.value.code == "PENDING_ENTRY_BAR_MISSING"
    assert failure.value.role == "PENDING_ENTRY"
    with pytest.raises(DataIntegrityError) as failure:
        validate(
            session=NEXT,
            held_positions={},
            pending_entries={"ABBV": ENTRY},
            previous_processed_session=MISSING,
        )
    assert failure.value.code == "PENDING_ENTRY_SESSION_MISMATCH"


@pytest.mark.parametrize("suffix", ["STALE", "INVALID"])
def test_pending_entry_stale_and_invalid_are_critical(suffix):
    current_bar = bar("ABBV", ENTRY) if suffix == "STALE" else bar("OTHER", MISSING)
    with pytest.raises(DataIntegrityError) as failure:
        validate(bars={"ABBV": current_bar}, held_positions={}, pending_entries={"ABBV": ENTRY})
    assert failure.value.code == "PENDING_ENTRY_BAR_" + suffix


def capture_states(monkeypatch):
    states = []
    original = BacktestEngine._open_position

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        if result[0] is not None:
            states.append(result[0])
        return result

    monkeypatch.setattr(BacktestEngine, "_open_position", capture)
    return states


def test_strict_abbv_stops_before_counter_mark_management_or_next_session(
    missing_case,
    config,
    monkeypatch,
):
    states = capture_states(monkeypatch)
    source = Screens(SIGNAL, (record("ABBV"),))
    screened, equity_dates = [], []
    original_screen = source.screen
    original_equity = engine_module.EquityPoint

    def screen(day):
        screened.append(day)
        return original_screen(day)

    def equity(**kwargs):
        equity_dates.append(kwargs["date"])
        return original_equity(**kwargs)

    monkeypatch.setattr(source, "screen", screen)
    monkeypatch.setattr(engine_module, "EquityPoint", equity)
    engine = BacktestEngine(
        missing_case, config, screen_source=source, data_integrity_mode=DataIntegrityMode.STRICT
    )
    managed_dates = []
    from trading_system.backtest.position_manager import PositionManager

    original_open = PositionManager.evaluate_open

    def management(manager, position, current_bar):
        managed_dates.append(current_bar.timestamp.date())
        return original_open(manager, position, current_bar)

    monkeypatch.setattr(PositionManager, "evaluate_open", management)
    with pytest.raises(DataIntegrityError) as failure:
        engine.run(SIGNAL, NEXT, variant=FROZEN_CHAMPION_F.variant)
    assert failure.value.code == "HELD_POSITION_BAR_MISSING"
    assert failure.value.session == MISSING
    assert len(states) == 1 and states[0].holding_days == 1
    assert states[0].last_price == 100
    assert MISSING not in managed_dates
    assert screened == equity_dates == [SIGNAL, ENTRY]


def test_strict_pending_missing_neither_enters_nor_defers(missing_case, config, monkeypatch):
    with missing_case.connect() as connection:
        connection.execute("DELETE FROM bars WHERE symbol='ABBV' AND timestamp LIKE '2022-03-07%'")
    states = capture_states(monkeypatch)
    with pytest.raises(DataIntegrityError) as failure:
        run_case(missing_case, config, data_integrity_mode=DataIntegrityMode.STRICT)
    assert (failure.value.code, failure.value.session) == ("PENDING_ENTRY_BAR_MISSING", ENTRY)
    assert not states


def test_strict_does_not_hide_globally_absent_session(missing_case, config, monkeypatch):
    with missing_case.connect() as connection:
        connection.execute("DELETE FROM bars WHERE timestamp LIKE '2022-03-08%'")

    def forbid(*args, **kwargs):
        raise AssertionError("Strict session calendar must not read local bar-session membership")

    monkeypatch.setattr(missing_case, "bar_sessions", forbid)
    assert _backtest_sessions(
        missing_case, ENTRY, NEXT, data_integrity_mode=DataIntegrityMode.STRICT
    ) == [ENTRY, MISSING, NEXT]
    with pytest.raises(DataIntegrityError) as failure:
        run_case(missing_case, config, data_integrity_mode=DataIntegrityMode.STRICT)
    assert failure.value.code == "HELD_POSITION_BAR_MISSING" and failure.value.session == MISSING


def test_strict_final_liquidation_never_queries_stale_bar(missing_case, config, monkeypatch):
    original = missing_case.bars_available_as_of

    def history(symbol, session, **kwargs):
        assert symbol != "ABBV" or session != MISSING, "Stale final lookup forbidden"
        return original(symbol, session, **kwargs)

    monkeypatch.setattr(missing_case, "bars_available_as_of", history)
    with pytest.raises(DataIntegrityError) as failure:
        run_case(missing_case, config, end=MISSING, data_integrity_mode=DataIntegrityMode.STRICT)
    assert failure.value.code == "HELD_POSITION_BAR_MISSING"


def test_strict_rejects_stale_bar_even_if_database_reader_returns_it(
    missing_case,
    config,
    monkeypatch,
):
    original = missing_case.bars_on_session

    def bars(symbols, session):
        found = original(symbols, session)
        if session == MISSING:
            found["ABBV"] = bar("ABBV", ENTRY)
        return found

    monkeypatch.setattr(missing_case, "bars_on_session", bars)
    with pytest.raises(DataIntegrityError) as failure:
        run_case(missing_case, config, data_integrity_mode=DataIntegrityMode.STRICT)
    assert failure.value.code == "HELD_POSITION_BAR_STALE"


def test_missing_day_ten_stops_with_day_nine_state(tmp_path, config, monkeypatch):
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 1, 23))
    db = Database(tmp_path / "day10.sqlite3")
    db.initialize()
    db.upsert_bars(
        [bar("SPY", s) for s in sessions]
        + [bar("ABBV", s) for i, s in enumerate(sessions) if i != 10]
    )
    states = capture_states(monkeypatch)
    with pytest.raises(DataIntegrityError) as failure:
        BacktestEngine(
            db,
            config,
            screen_source=Screens(sessions[0], (record("ABBV"),)),
            data_integrity_mode=DataIntegrityMode.STRICT,
        ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
    assert failure.value.session == sessions[10]
    assert failure.value.holding_session_number == 10
    assert states[0].holding_days == 9


def test_complete_ten_session_hold_has_identical_exit_in_strict_and_legacy(tmp_path, config):
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 1, 23))
    db = Database(tmp_path / "complete.sqlite3")
    db.initialize()
    db.upsert_bars([bar(symbol, s) for symbol in ("ABBV", "SPY") for s in sessions])
    results = [
        BacktestEngine(
            db,
            config,
            screen_source=Screens(sessions[0], (record("ABBV"),)),
            data_integrity_mode=mode,
        ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
        for mode in (DataIntegrityMode.LEGACY_RESEARCH, DataIntegrityMode.STRICT)
    ]
    assert results[0].positions == results[1].positions
    assert results[0].trades == results[1].trades
    assert results[0].equity_curve == results[1].equity_curve
    assert results[1].positions[0].holding_days == 10
    assert results[1].positions[0].exit_date == sessions[10]
    assert results[1].positions[0].exit_reason == "time_exit"


@pytest.mark.parametrize(
    "opening,high,low,reason",
    [
        (90, 120, 89, "stop_loss"),
        (120, 121, 90, "profit_target"),
        (100, 120, 90, "stop_loss"),
        (100, 120, 99, "profit_target"),
        (100, 101, 99, "end_of_backtest"),
    ],
)
def test_complete_data_strict_and_legacy_have_identical_economics(
    missing_case,
    config,
    opening,
    high,
    low,
    reason,
):
    missing_case.upsert_bars([bar("ABBV", MISSING, opening=opening, high=high, low=low)])
    legacy = run_case(missing_case, config)
    strict = run_case(missing_case, config, data_integrity_mode=DataIntegrityMode.STRICT)
    assert strict.positions == legacy.positions
    assert strict.trades == legacy.trades
    assert strict.equity_curve == legacy.equity_curve
    assert strict.positions[0].exit_reason == reason
    assert strict.positions[0].entry_date == ENTRY
    assert strict.configuration["data_integrity_mode"] == "STRICT"
    assert "data_integrity_mode" not in legacy.configuration


def test_champion_exposes_strict_and_preserves_frozen_economics(missing_case, config):
    frozen, strategy = asdict(FROZEN_CHAMPION_F), config.model_dump()
    yaml_before = Path("config/strategy.yaml").read_bytes()
    with pytest.raises(DataIntegrityError) as failure:
        run_champion_backtest(
            missing_case,
            config,
            SIGNAL,
            NEXT,
            screen_source=Screens(SIGNAL, (record("ABBV"),)),
            data_integrity_mode=DataIntegrityMode.STRICT,
        )
    assert failure.value.code == "HELD_POSITION_BAR_MISSING"
    assert asdict(FROZEN_CHAMPION_F) == frozen
    assert config.model_dump() == strategy
    assert Path("config/strategy.yaml").read_bytes() == yaml_before


def test_strict_error_precedes_legacy_optional_held_guard(missing_case, config):
    with pytest.raises(DataIntegrityError) as failure:
        run_case(
            missing_case,
            config,
            require_complete_daily_position_bars=True,
            data_integrity_mode=DataIntegrityMode.STRICT,
        )
    assert failure.value.code == "HELD_POSITION_BAR_MISSING"


def test_strict_rejects_unsupported_intraday_path_instead_of_legacy_fallback(missing_case, config):
    config = config.model_copy(
        update={
            "position_management": config.position_management.model_copy(
                update={"bar_timeframe": "15m"}
            )
        }
    )
    with pytest.raises(DataIntegrityError) as failure:
        run_case(missing_case, config, data_integrity_mode=DataIntegrityMode.STRICT)
    assert failure.value.code == "STRICT_DAILY_PATH_REQUIRED"


def test_real_abbv_entry_session_metadata_requires_ninth_official_observation():
    with pytest.raises(DataIntegrityError) as failure:
        validate(bars={}, held_positions={"ABBV": date(2022, 2, 24)})
    assert failure.value.holding_session_number == 9
    assert failure.value.position_entry_session == date(2022, 2, 24)


def test_read_only_replay_guard_blocks_all_clients_network_and_production_writes(tmp_path):
    from trading_system.backtest.f_data_gap_audit import AuditDatabase, digest, local_guard
    from trading_system.data.massive_identity_research import MassiveIdentityResearchClient

    db_path = tmp_path / "guard.sqlite3"
    db = Database(db_path)
    db.initialize()
    db.upsert_bars([bar("ABBV", ENTRY)])
    before = digest(db_path)
    with local_guard() as counters:
        readonly = AuditDatabase(db_path)
        assert set(readonly.bars_on_session(["ABBV"], ENTRY)) == {"ABBV"}
        with pytest.raises(RuntimeError):
            readonly.initialize()
        with pytest.raises(RuntimeError):
            MassiveIdentityResearchClient("synthetic-test-key")
        with (
            readonly.read_only() as connection,
            pytest.raises(sqlite3.DatabaseError, match="not authorized"),
        ):
            connection.execute("DELETE FROM bars")
        assert counters["network_calls"] == counters["provider_calls"] == 0
        assert counters["database_writes"] == 0
    assert digest(db_path) == before
