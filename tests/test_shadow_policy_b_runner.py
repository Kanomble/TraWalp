"""Atomic Policy B integration on Task-6A synthetic 300-session SIP fixtures only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from shadow_fixtures import assert_history, official_history
from test_champion_consolidation import bar, record
from test_champion_consolidation import offline as offline
from test_shadow_execution import case as case
from test_shadow_execution import config as config
from test_shadow_execution import events

from trading_system.backtest.data_integrity import DataIntegrityError
from trading_system.shadow import runner as runner_module
from trading_system.shadow.contracts import (
    FULL,
    MANAGEMENT_WITH_BLOCKED_SIGNALS,
    ShadowError,
)
from trading_system.shadow.journal import Journal
from trading_system.shadow.runner import ShadowRunner


def ranking_gap(case, index):
    """Remove exactly one foreign issuer SIP observation; critical AAA stays usable."""
    runner, db, sessions, source = case
    day = sessions[index]
    missing = official_history(day)[100]
    assert_history(db, ("AAA", "BBB"), day)
    with db.connect() as c:
        c.execute(
            "DELETE FROM forward_market_observations WHERE symbol='BBB' AND session=?",
            (missing.isoformat(),),
        )
    source.signals[day] = (record("AAA"), record("BBB"))
    assert_history(db, ("AAA",), day)
    assert_history(db, ("BBB",), day, missing=[("BBB", missing)])
    return missing


def admission(runner, session):
    started = next(
        e for e in events(runner.journal, "SESSION_STARTED") if e["session"] == session.isoformat()
    )
    with runner.journal.connection() as c:
        return runner.journal.load_input_manifest(c, started["payload"]["readiness_evidence_hash"])


def assert_limited(runner, day):
    evidence = admission(runner, day)
    assert evidence["contract"] == "shadow-session-admission/v1"
    assert evidence["purpose"] == MANAGEMENT_WITH_BLOCKED_SIGNALS
    ready = evidence["evidence"]["readiness"]
    assert ready["blocking_reasons"] == ["RANKING_UNRESOLVED"]
    assert ready["economic_shadow_ready"] is ready["ready"] is False
    assert ready["observation_shadow_ready"] is True
    assert evidence["evidence"]["qualification"]["incomplete_inputs"] == [
        {"symbol": "BBB", "reasons": ["MARKET_HISTORY_UNPROVEN"]}
    ]
    selected = [e for e in events(runner.journal) if e["session"] == day.isoformat()]
    assert sum(e["event_type"] == "SESSION_COMMITTED" for e in selected) == 1
    assert not any(e["event_type"] == "ENTRY_INTENT_CREATED" for e in selected)
    decision = next(e for e in selected if e["event_type"] == "DECISION_NO_TRADE")
    assert decision["payload"]["reason"] == "NO_TRADE_DATA_INCOMPLETE"
    assert runner.journal.verify().last_session == day
    return selected


@pytest.mark.parametrize(
    "scenario,opening,high,low,reason",
    [
        ("held", 100, 101, 99, None),
        ("gap_stop", 90, 120, 89, "stop_loss"),
        ("opening_target", 120, 121, 90, "profit_target"),
        ("stop_target_conflict", 100, 120, 90, "stop_loss"),
        ("target", 100, 120, 99, "profit_target"),
        ("session_10", 100, 101, 99, "time_exit"),
    ],
)
def test_held_management_with_foreign_ranking_gap(case, scenario, opening, high, low, reason):
    runner, db, sessions, _ = case
    index = 10 if scenario == "session_10" else 2
    for day in sessions[:index]:
        runner.run(day)
    before = runner.journal.verify().state
    position = before["position"]
    assert position is not None
    ranking_gap(case, index)
    db.upsert_bars([bar("AAA", sessions[index], opening=opening, high=high, low=low)])
    source_before = db.path.read_bytes()
    runner.run(sessions[index])
    selected = assert_limited(runner, sessions[index])
    assert db.path.read_bytes() == source_before
    kinds = [e["event_type"] for e in selected]
    assert kinds[0:3] == ["SESSION_STARTED", "DATA_PREFLIGHT_PASSED", "POSITION_MANAGED"]
    assert kinds[-2:] == ["DECISION_NO_TRADE", "SESSION_COMMITTED"]
    exits = [e for e in selected if e["event_type"] == "EXIT_EXECUTED"]
    after = runner.journal.verify().state
    if reason:
        assert len(exits) == 1
        trade = exits[0]["payload"]["trade"]
        assert trade["exit_reason"] == reason
        assert trade["position_id"] == position["position_id"]
        assert trade["quantity"] == position["quantity"]
        assert trade["holding_days"] == position["holding_days"] + 1
        reference = (
            opening
            if scenario in {"gap_stop", "opening_target"}
            else position["stop_price"]
            if reason == "stop_loss"
            else position["target_price"]
            if reason == "profit_target"
            else 100
        )
        assert trade["exit_price"] == reference * (1 - runner.config.backtest.slippage_bps / 10000)
        assert after["position"] is None
        assert (
            after["cash"]
            == before["cash"] + trade["exit_price"] * trade["quantity"] - trade["fees"]
        )
        assert after["realized_pnl"] == before["realized_pnl"] + trade["pnl"]
        assert (kinds.index("EXIT_EXECUTED") > kinds.index("SCREEN_EVALUATED")) == (
            reason == "time_exit"
        )
    else:
        assert not exits
        assert after["position"]["position_id"] == position["position_id"]
        assert after["position"]["holding_days"] == position["holding_days"] + 1
        assert after["position"]["last_price"] == 100
        assert after["cash"] == before["cash"]


def test_pending_is_executed_once_despite_foreign_ranking_gap(case):
    runner, _, sessions, _ = case
    runner.run(sessions[0])
    before = deepcopy(runner.journal.verify().state["pending"])
    ranking_gap(case, 1)
    runner.run(sessions[1])
    selected = assert_limited(runner, sessions[1])
    kinds = [e["event_type"] for e in selected]
    assert kinds.index("ENTRY_EXECUTED") < kinds.index("SCREEN_EVALUATED")
    state = runner.journal.verify().state
    assert state["pending"] is None
    assert state["position"]["signal_date"] == before["order"]["signal_date"]
    assert state["position"]["entry_date"] == before["expected_execution_session"]
    assert state["position"]["holding_days"] == 1
    assert state["position_sequence"] == 1
    original = events(runner.journal)
    assert runner.run(sessions[1])["status"] == "ALREADY_PROCESSED"
    assert events(runner.journal) == original


def test_flat_limited_commit_is_final_even_after_source_repair(case):
    runner, db, sessions, source = case
    missing = ranking_gap(case, 0)
    before = runner.journal.verify().state
    runner.run(sessions[0])
    assert_limited(runner, sessions[0])
    assert runner.journal.verify().state == before
    original = events(runner.journal)
    db.upsert_bars([bar("BBB", missing)])
    source.signals[sessions[0]] = (record("AAA"),)
    assert runner.run(sessions[0])["status"] == "ALREADY_PROCESSED"
    assert events(runner.journal) == original
    assert not events(runner.journal, "ENTRY_INTENT_CREATED")
    source.signals[sessions[1]] = (record("AAA"),)
    runner.run(sessions[1])
    assert admission(runner, sessions[1])["purpose"] == FULL
    assert [e["session"] for e in events(runner.journal, "ENTRY_INTENT_CREATED")] == [
        sessions[1].isoformat()
    ]


@pytest.mark.parametrize("held", [False, True])
def test_critical_missing_bar_blocks_even_with_ranking_gap_and_retries_same_session(case, held):
    runner, db, sessions, _ = case
    index = 2 if held else 1
    for day in sessions[:index]:
        runner.run(day)
    ranking_gap(case, index)
    before = runner.journal.verify()
    with db.connect() as c:
        c.execute(
            "DELETE FROM forward_market_observations WHERE symbol='AAA' AND session=?",
            (sessions[index].isoformat(),),
        )
    code = "HELD_POSITION_BAR_MISSING" if held else "PENDING_ENTRY_BAR_MISSING"
    with pytest.raises(DataIntegrityError, match=code):
        runner.run(sessions[index])
    failed = runner.journal.verify()
    assert failed.state == before.state and failed.last_session == before.last_session
    assert failed.event_count == before.event_count + 1
    assert events(runner.journal)[-1]["event_type"] == "DATA_PREFLIGHT_FAILED"
    db.upsert_bars([bar("AAA", sessions[index])])
    runner.run(sessions[index])
    assert_limited(runner, sessions[index])


@pytest.mark.parametrize(
    "problem", ["sec_stale", "spy_missing", "asset_failed", "sip_failed", "runtime_drift"]
)
def test_hard_upstream_or_runtime_failure_never_uses_ranking_exception(case, monkeypatch, problem):
    runner, db, sessions, _ = case
    runner.run(sessions[0])
    ranking_gap(case, 1)
    before = runner.journal.verify()
    original = events(runner.journal)
    if problem == "spy_missing":
        with db.connect() as c:
            c.execute(
                "DELETE FROM forward_market_observations WHERE symbol='SPY' AND session=?",
                (sessions[1].isoformat(),),
            )
    elif problem == "runtime_drift":
        current = runner_module.fingerprints(runner.config)
        monkeypatch.setattr(
            runner_module, "fingerprints", lambda _: {**current, "runtime": {"python": "changed"}}
        )
    else:
        key = {"sec_stale": "sec", "asset_failed": "asset_universe", "sip_failed": "forward_sip"}[
            problem
        ]
        state = db.sync_value("dataset", key)
        state.update(
            {"last_success_at": (runner.clock() - timedelta(days=5)).isoformat()}
            if problem == "sec_stale"
            else {"status": "failed", "errors": 1}
        )
        db.set_sync_value("dataset", key, state)
    expected = (
        "SHADOW_CONFIGURATION_DRIFT"
        if problem == "runtime_drift"
        else "SHADOW_SESSION_DATA_NOT_READY"
    )
    with pytest.raises(ShadowError, match=expected):
        runner.run(sessions[1])
    assert runner.journal.verify().state == before.state
    assert runner.journal.verify().last_session == before.last_session
    assert events(runner.journal) == original


@pytest.mark.parametrize(
    "crash_at",
    [
        "ENTRY_EXECUTED",
        "POSITION_MANAGED",
        "EXIT_EXECUTED",
        "DECISION_NO_TRADE",
        "SESSION_COMMITTED",
    ],
)
def test_crash_rolls_back_entire_limited_session_then_retry_executes_once(
    case, monkeypatch, crash_at
):
    runner, db, sessions, _ = case
    runner.run(sessions[0])
    ranking_gap(case, 1)
    if crash_at == "EXIT_EXECUTED":
        db.upsert_bars([bar("AAA", sessions[1], high=120, low=90)])
    before = runner.journal.verify()
    original_events = events(runner.journal)
    with runner.journal.connection() as c:
        original_manifests = [
            tuple(r) for r in c.execute("SELECT * FROM input_manifests ORDER BY content_hash")
        ]
    append = runner.journal.append
    attempted_position_ids = []

    def crash(c, kind, payload, **kwargs):
        result = append(c, kind, payload, **kwargs)
        if kind == "ENTRY_EXECUTED":
            attempted_position_ids.append(payload["after"]["position"]["position_id"])
        if kind == crash_at:
            raise SystemExit("synthetic crash before SQLite commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(runner.journal, "append", crash)
        with pytest.raises(SystemExit):
            runner.run(sessions[1])
    assert events(runner.journal) == original_events
    assert runner.journal.verify().state == before.state
    with runner.journal.connection() as c:
        assert original_manifests == [
            tuple(r) for r in c.execute("SELECT * FROM input_manifests ORDER BY content_hash")
        ]
    restarted = ShadowRunner(
        Journal(runner.journal.path),
        runner.market_path,
        runner.config,
        clock=runner.clock,
        screen_source=runner.screen_source,
    )
    restarted.run(sessions[1])
    assert_limited(restarted, sessions[1])
    executed = events(restarted.journal, "ENTRY_EXECUTED")
    assert len(executed) == 1
    assert executed[0]["payload"]["after"]["position"]["position_id"] == attempted_position_ids[0]
    committed = events(restarted.journal)
    assert restarted.run(sessions[1])["status"] == "ALREADY_PROCESSED"
    assert events(restarted.journal) == committed


def test_concurrent_limited_runners_fill_pending_only_once(case):
    runner, _, sessions, _ = case
    runner.run(sessions[0])
    ranking_gap(case, 1)

    def attempt(_):
        other = ShadowRunner(
            Journal(runner.journal.path),
            runner.market_path,
            runner.config,
            clock=runner.clock,
            screen_source=runner.screen_source,
        )
        return other.run(sessions[1])["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["ALREADY_PROCESSED", "SESSION_COMMITTED"]
    assert_limited(runner, sessions[1])
    assert len(events(runner.journal, "ENTRY_EXECUTED")) == 1
    assert runner.journal.verify().state["position_sequence"] == 1


def test_signal_emit_guard_rolls_back_a_faulty_adapter(case, monkeypatch):
    runner, _, sessions, _ = case
    ranking_gap(case, 0)
    before = events(runner.journal)

    def forbidden(adapter, state, session, next_session, bars, emit):
        emit("ENTRY_INTENT_CREATED", {})

    monkeypatch.setattr(runner_module.ChampionSession, "process", forbidden)
    with pytest.raises(ShadowError, match="SHADOW_BLOCKED_SIGNAL_INTENT"):
        runner.run(sessions[0])
    assert events(runner.journal) == before
