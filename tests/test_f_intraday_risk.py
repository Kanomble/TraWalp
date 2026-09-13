"""Bounded offline fixtures for one fixed post-entry risk hypothesis."""

import csv
import json
import socket
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
import test_f_lifecycle_v2 as fixtures

from trading_system.backtest import intraday_risk_validation as research
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.intraday_risk import IntradayRiskOverlay
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.cli import _parser
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayRequirementStatus,
    _expected_timestamps,
    _observation_key,
    _observation_payload,
    candidate_requirements_from_report,
)
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe

config = fixtures.config
local_market = fixtures.local_market


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Risk research must not access a network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def _native(session, *, changes=None, symbol="AAA"):
    output = []
    for index, bar in enumerate(fixtures.native_session(session, weak=False)):
        values = {"open": 100, "high": 101, "low": 99, "close": 100}
        values.update((changes or {}).get(index, {}))
        values["high"] = max(values["high"], values["open"], values["close"])
        values["low"] = min(values["low"], values["open"], values["close"])
        output.append(
            bar.model_copy(
                update={
                    **{key: Decimal(str(value)) for key, value in values.items()},
                    "symbol": symbol,
                    "vwap": None,  # VWAP is not part of this risk hypothesis.
                }
            )
        )
    return output


def _store(database, session, bars):
    daily = fixtures.bar(bars[0].symbol, session).model_copy(
        update={
            "open": bars[0].open,
            "high": max(b.high for b in bars),
            "low": min(b.low for b in bars),
            "close": bars[-1].close,
        }
    )
    database.upsert_bars([daily, *bars])


@pytest.fixture
def case(monkeypatch, local_market, config):
    database, sessions = local_market
    sessions = sessions[:6]
    for session in sessions[1:]:
        _store(database, session, _native(session))
    preparation = SimpleNamespace(sessions=sessions, screen_source=fixtures.Screens(sessions[0]))
    monkeypatch.setattr(
        research, "_qualify_daily_research", lambda *args: {"ready": True, "failure_reasons": []}
    )
    return database, config, sessions, preparation


def _control(case):
    database, config, sessions, preparation = case
    return BacktestEngine(
        database,
        config,
        screen_source=preparation.screen_source,
        require_complete_daily_position_bars=True,
    ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)


def _run(case, *, preflight=False):
    database, config, sessions, preparation = case
    runner = research.build_f_intraday_risk_preflight if preflight else research.run_f_intraday_risk
    return runner(database, config, sessions[0], sessions[-1], preparation=preparation)


def _levels(case):
    trade = _control(case).trades[0]
    return (
        trade.entry_price - 0.5 * (trade.entry_price - trade.stop_price),
        trade.stop_price,
        trade.target_price,
    )


def _r1_event(bundle):
    return next(
        row
        for row in bundle.tables["intraday_risk_events"]
        if row["strategy"] == "R1_COMMON_SUPPORT"
    )


def test_r0_full_and_inactive_overlay_are_exact_frozen_control(case, monkeypatch):
    reference = _control(case)
    database, _, _, _ = case
    original_between = database.bars_between
    original_available = database.bars_available_as_of

    def between(*args, **kwargs):
        assert kwargs.get("timeframe") is not BarTimeframe.MINUTES_15
        return original_between(*args, **kwargs)

    def available(*args, **kwargs):
        assert kwargs.get("timeframe") is not BarTimeframe.MINUTES_15
        return original_available(*args, **kwargs)

    monkeypatch.setattr(database, "bars_between", between)
    monkeypatch.setattr(database, "bars_available_as_of", available)
    bundle = _run(case)
    assert list(bundle.results) == ["R0_FULL", "R0_COMMON_SUPPORT", "R1_COMMON_SUPPORT"]
    for result in bundle.results.values():
        assert result.model_dump(exclude={"generated_at"}) == reference.model_dump(
            exclude={"generated_at"}
        )
    assert bundle.summary["r1_breakdown_executions"] == 0
    assert bundle.summary["performance"]["sqlite_query_count_native_risk"] == 0
    assert bundle.summary["performance"]["coverage_sql_queries"] == 2
    assert bundle.summary["performance"]["native_risk_cache_peak"] <= 8
    assert (
        bundle.summary["comparisons"]["intraday_risk_effect"]["metric_deltas"]["total_return"] == 0
    )


@pytest.mark.parametrize("pattern", ["one", "reset", "two", "equality"])
def test_fixed_two_close_rule_and_next_native_open(case, pattern):
    database, _, sessions, _ = case
    level, _, _ = _levels(case)
    below = level if pattern == "equality" else level - 0.1
    changes = {0: {"close": below}}
    if pattern in {"two", "equality"}:
        changes[1] = {"close": below}
        # Same execution bar's later stop touch cannot reach backwards past its open.
        changes[2] = {"open": level - 0.2, "low": 1, "high": 200}
    elif pattern == "reset":
        changes[1] = {"close": level + 0.1}
        changes[2] = {"close": below}
    native = _native(sessions[1], changes=changes)
    _store(database, sessions[1], native)
    bundle = _run(case)
    event = _r1_event(bundle)
    r0, r1 = bundle.results["R0_COMMON_SUPPORT"], bundle.results["R1_COMMON_SUPPORT"]
    if pattern in {"one", "reset"}:
        assert event["status"] == "NOT_TRIGGERED"
        assert r1.model_dump(exclude={"generated_at"}) == r0.model_dump(exclude={"generated_at"})
    else:
        trade = r1.trades[0]
        assert event["status"] == "EXECUTED"
        assert trade.entry_price == r0.trades[0].entry_price
        assert trade.entry_timestamp == r0.trades[0].entry_timestamp
        assert trade.quantity == r0.trades[0].quantity
        assert trade.stop_price == r0.trades[0].stop_price
        assert trade.exit_timestamp == native[2].timestamp
        assert trade.exit_reference_price == float(native[2].open)
        assert trade.highest_price_during_trade < 200
        assert trade.lowest_price_during_trade > 1
        assert (
            event["decision_timestamp"] == (native[1].timestamp + timedelta(minutes=15)).isoformat()
        )
        assert (
            event["first_breakdown_close_timestamp"]
            == (native[0].timestamp + timedelta(minutes=15)).isoformat()
        )
        assert event["second_breakdown_close_timestamp"] == event["decision_timestamp"]
        assert trade.exit_price == float(native[2].open) * (
            1 - case[1].backtest.slippage_bps / 10000
        )


@pytest.mark.parametrize(
    "canonical", ["stop", "target", "both", "execution_open_stop", "execution_open_target"]
)
def test_canonical_stop_target_precedence_preserves_control_economics(case, canonical):
    database, _, sessions, _ = case
    level, stop, target = _levels(case)
    changes = {0: {"close": level - 0.1}, 1: {"close": level - 0.1}}
    if canonical == "stop":
        changes[0]["low"] = stop
    elif canonical == "target":
        changes[0]["high"] = target
    elif canonical == "both":
        changes[0].update(low=stop, high=target)
    else:
        changes[2] = {"open": stop if canonical.endswith("stop") else target}
    _store(database, sessions[1], _native(sessions[1], changes=changes))
    bundle = _run(case)
    assert bundle.results["R1_COMMON_SUPPORT"].model_dump(
        exclude={"generated_at"}
    ) == bundle.results["R0_COMMON_SUPPORT"].model_dump(exclude={"generated_at"})
    assert _r1_event(bundle)["status"] == (
        "CANONICAL_TARGET_PRECEDED"
        if canonical in {"target", "execution_open_target"}
        else "CANONICAL_STOP_PRECEDED"
    )


def test_trigger_at_last_close_executes_next_session_open_unless_baseline_exits(case):
    database, _, sessions, _ = case
    level, _, _ = _levels(case)
    count = len(_native(sessions[1]))
    _store(
        database,
        sessions[1],
        _native(
            sessions[1],
            changes={
                count - 2: {"close": level - 0.1},
                count - 1: {"close": level - 0.1},
            },
        ),
    )
    bundle = _run(case)
    assert (
        bundle.results["R1_COMMON_SUPPORT"].trades[0].exit_timestamp
        == _native(sessions[2])[0].timestamp
    )
    # A final-session close liquidation wins over a decision with no next bar available.
    shortened = (
        *case[:2],
        sessions[:2],
        SimpleNamespace(sessions=sessions[:2], screen_source=case[3].screen_source),
    )
    bundle = _run(shortened)
    assert _r1_event(bundle)["status"] == "TRIGGERED"
    assert bundle.summary["r1_breakdown_executions"] == 0
    assert bundle.results["R1_COMMON_SUPPORT"].trades == bundle.results["R0_COMMON_SUPPORT"].trades


def _remove_and_observe(case, session, *, status=None):
    database, config, _, _ = case
    opening, closing = regular_session_bounds(session)
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='AAA' AND timeframe='15m' "
            "AND timestamp>=? AND timestamp<?",
            (opening.isoformat(), closing.isoformat()),
        )
    if status is not None:
        requirement = CandidateIntradayRequirement(
            "AAA",
            session,
            BarTimeframe.MINUTES_15,
            requirement_type="potential_open_position_session",
        )
        database.set_sync_value(
            PROVIDER_OBSERVATION_SOURCE,
            _observation_key(requirement),
            _observation_payload(
                "AAA",
                BarTimeframe.MINUTES_15,
                session,
                feed=config.universe.market_data_feed,
                adjustment=config.universe.market_data_adjustment,
                extended_hours=False,
                status=status,
                requirement_type=requirement.requirement_type,
                missing_timestamps=_expected_timestamps(
                    session, BarTimeframe.MINUTES_15, extended_hours=False
                ),
                error="fixture failure"
                if status is IntradayRequirementStatus.PROVIDER_CHECK_FAILED
                else None,
            ),
        )


def test_identical_common_support_consumes_rank_one_without_rank_two_substitution(case):
    database, _, sessions, preparation = case
    preparation.screen_source = fixtures.Screens(
        sessions[0], (fixtures.record("AAA"), fixtures.record("BBB"))
    )
    for session in sessions:
        _store(database, session, _native(session, symbol="BBB"))
    _remove_and_observe(
        case, sessions[3], status=IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT
    )
    bundle = _run(case)
    assert [p.symbol for p in bundle.results["R0_FULL"].positions] == ["AAA"]
    for label in ("R0_COMMON_SUPPORT", "R1_COMMON_SUPPORT"):
        assert bundle.results[label].trades == ()
        assert bundle.results[label].skipped_entries["intraday_risk_unsupported_signal"] == 1
    assert bundle.summary["unsupported_baseline_positions"] == 1
    assert bundle.summary["provider_absent_position_sessions"] == 1
    assert bundle.summary["common_support_positions"] == 0
    assert {row["status"] for row in bundle.tables["intraday_risk_events"]} == {
        "PROVIDER_ABSENT_SUPPORT"
    }


def test_preflight_exports_exact_sync_requirements_and_never_runs_r1(case, tmp_path, monkeypatch):
    _remove_and_observe(case, case[2][3])

    def forbidden(*args, **kwargs):
        pytest.fail("Preflight must not execute a risk overlay")

    monkeypatch.setattr(IntradayRiskOverlay, "session_exit", forbidden)
    bundle = _run(case, preflight=True)
    assert list(bundle.results) == ["R0_FULL"]
    assert bundle.summary["r1_backtest_executed"] is False
    assert bundle.summary["qualification_status"] == "NOT_READY_LOCAL_GAPS"
    assert bundle.summary["common_support_positions"] == 0
    paths = research.export_f_intraday_risk(bundle, tmp_path, stem="risk", preflight=True)
    payload = json.loads(paths["intraday_requirements.json"].read_text())
    requirements = candidate_requirements_from_report(
        payload, start=case[2][0], end=case[2][-1], timeframes=["15m"]
    )
    assert len(requirements) == bundle.summary["required_position_sessions"] == 5
    assert requirements[0].requirement_type == "candidate_session"
    assert all(r.requirement_type == "potential_open_position_session" for r in requirements[1:])
    assert "production" in bundle.summary["support_semantics"]
    for command in ("preflight-f-intraday-risk", "validate-f-intraday-risk"):
        args = _parser().parse_args(
            [command, "--start", "2024-01-02", "--end", "2024-01-09", "--output-stem", "risk"]
        )
        assert args.command == command


@pytest.mark.parametrize(
    "status,expected",
    [
        (None, "NOT_READY_LOCAL_GAPS"),
        (
            IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
            "NOT_READY_PROVIDER_VERIFICATION_FAILURE",
        ),
    ],
)
def test_unresolved_local_or_provider_failure_rejects_validation(case, status, expected):
    _remove_and_observe(case, case[2][3], status=status)
    with pytest.raises(ValueError, match=expected):
        _run(case)


def test_nonpositive_r_skips_overlay_and_export_has_required_schema(case, tmp_path):
    database, config, sessions, _ = case
    with NativeEntrySessions() as native:
        overlay = IntradayRiskOverlay([], (), native, strategy="R1_COMMON_SUPPORT", enabled=True)
        position = SimpleNamespace(
            position_id="invalid",
            symbol="AAA",
            signal_date=sessions[0],
            entry_date=sessions[1],
            entry_price=100,
            stop_price=100,
        )
        overlay.on_entry(position)
        assert overlay.session_exit(position, sessions[1]) is None
        assert overlay.events[0]["breakdown_level"] is None
    bundle = _run(case)
    paths = research.export_f_intraday_risk(bundle, tmp_path, stem="validation")
    with paths["intraday_risk_events.csv"].open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert set(research.RISK_EVENT_FIELDS) <= set(reader.fieldnames)
        assert list(reader)
    assert (
        bundle.summary["comparisons"]["intraday_risk_effect"]["comparison"]
        == "R1_COMMON_SUPPORT - R0_COMMON_SUPPORT"
    )
    assert (
        bundle.summary["comparisons"]["coverage_support_bias"]["comparison"]
        == "R0_COMMON_SUPPORT - R0_FULL"
    )


def test_earlier_native_canonical_touch_permanently_preempts_overlay(case):
    database, _, sessions, _ = case
    level, stop, _ = _levels(case)
    # Native/Daily disagreement must not allow a later R1 exit past this stop touch.
    database.upsert_bars(_native(sessions[1], changes={0: {"low": stop}}))
    _store(
        database,
        sessions[2],
        _native(
            sessions[2],
            changes={
                0: {"close": level - 0.1},
                1: {"close": level - 0.1},
            },
        ),
    )
    bundle = _run(case)
    assert bundle.results["R1_COMMON_SUPPORT"].model_dump(exclude={"generated_at"}) == (
        bundle.results["R0_COMMON_SUPPORT"].model_dump(exclude={"generated_at"})
    )
    assert _r1_event(bundle)["status"] == "CANONICAL_STOP_PRECEDED"


def test_signals_exposed_by_early_exit_stay_outside_frozen_baseline_support(case):
    database, _, sessions, preparation = case
    level, _, _ = _levels(case)

    class Screens:
        def screen(self, session):
            symbol = "AAA" if session == sessions[0] else "BBB"
            return fixtures.Screens(session, (fixtures.record(symbol),)).screen(session)

    preparation.screen_source = Screens()
    for session in sessions:
        _store(database, session, _native(session, symbol="BBB"))
    _store(
        database,
        sessions[1],
        _native(
            sessions[1],
            changes={
                0: {"close": level - 0.1},
                1: {"close": level - 0.1},
            },
        ),
    )
    bundle = _run(case)
    assert [p.symbol for p in bundle.results["R0_FULL"].positions] == ["AAA"]
    assert [p.symbol for p in bundle.results["R1_COMMON_SUPPORT"].positions] == ["AAA"]
    assert (
        bundle.results["R1_COMMON_SUPPORT"].skipped_entries["intraday_risk_unsupported_signal"] > 0
    )
    assert any(
        row["status"] == "OUTSIDE_BASELINE_SUPPORT" for row in bundle.tables["intraday_risk_events"]
    )
