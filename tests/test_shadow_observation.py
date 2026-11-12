"""Offline mode safety; journals and source writes are temporary fixtures only."""

import json
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from shadow_fixtures import assert_census, incomplete_candidate
from test_champion_consolidation import offline as offline
from test_champion_consolidation import record
from test_shadow_execution import case as case
from test_shadow_execution import config as config
from test_shadow_execution import events, ready_states

from trading_system.shadow.contracts import ShadowError
from trading_system.shadow.journal import Journal
from trading_system.shadow.modes import ECONOMIC, OBSERVATION_ONLY
from trading_system.shadow.runner import ShadowRunner
from trading_system.shadow.state import TRANSITIONS, initial_state


def observation(case, tmp_path, *, incomplete=True):
    old, db, sessions, source = case
    before = datetime(2023, 12, 29, 22, tzinfo=UTC)
    ready_states(db, before)
    if incomplete:
        missing = incomplete_candidate()
        source.signals[before.date()] = (record("AAA"), missing)
        source.signals[sessions[0]] = (record("AAA"), missing)
    runner = ShadowRunner(
        Journal(tmp_path / "observation.sqlite3"),
        db.path,
        old.config,
        screen_source=source,
        clock=lambda: datetime(2024, 1, 1, 12, tzinfo=UTC),
    )
    runner.initialize(shadow_mode=OBSERVATION_ONLY)
    runner.clock = lambda: datetime(2024, 1, 2, 22, tzinfo=UTC)
    ready_states(db, runner.clock())
    return runner, db, sessions, source


def test_observation_init_and_blocked_session(case, tmp_path, monkeypatch):
    runner, _, sessions, _ = observation(case, tmp_path)
    from trading_system.shadow import runner as module

    monkeypatch.setattr(
        module, "ChampionSession", lambda *a, **kw: pytest.fail("economic adapter invoked")
    )
    before = runner.journal.verify()
    assert before.inception["shadow_mode"] == OBSERVATION_ONLY
    assert before.inception["input_readiness"]["observation_shadow_ready"]
    assert not before.inception["input_readiness"]["economic_shadow_ready"]
    assert before.event_count == 1 and before.state == initial_state(100)
    result = runner.run(sessions[0])
    assert result["status"] == "OBSERVED_NO_ECONOMIC_DECISION"
    verified = runner.journal.verify()
    assert verified.state == before.state
    assert verified.ranking_blocked_sessions == 1
    assert verified.economic_sessions == 0
    assert len(events(runner.journal, "OBSERVATION_SESSION")) == 1
    assert len(events(runner.journal, "RANKING_BLOCKED")) == 1
    assert not any(
        e["event_type"] in TRANSITIONS | {"SCREEN_EVALUATED", "DECISION_NO_TRADE"}
        for e in events(runner.journal)
    )
    payload = events(runner.journal, "OBSERVATION_SESSION")[0]["payload"]
    assert payload["unresolved_count"] == 1
    assert_census(payload, source=2, rejected=0, evaluated=1, unresolved=1)
    assert payload["economic_action_allowed"] is False
    assert payload["ranking_published"] is False
    assert runner.run(sessions[0])["status"] == "ALREADY_PROCESSED"
    status = runner.status()
    assert status["journal_mode"] == OBSERVATION_ONLY
    assert status["last_observed_session"] == sessions[0]
    assert status["last_economic_session"] is None
    assert status["current_position_state"] == "FLAT"


def test_complete_observation_does_not_automatically_trade_and_explicit_activation(case, tmp_path):
    runner, db, sessions, source = observation(case, tmp_path, incomplete=False)
    runner.run(sessions[0])
    assert runner.journal.verify().shadow_mode == OBSERVATION_ONLY
    assert runner.journal.verify().state == initial_state(100)
    assert not events(runner.journal, "RANKING_BLOCKED")
    assert_census(
        events(runner.journal, "OBSERVATION_SESSION")[0]["payload"],
        source=2,
        rejected=0,
        evaluated=2,
        unresolved=0,
    )
    inception_bytes = events(runner.journal)[0]
    result = runner.activate_economic()
    assert result["economic_inception_session"] == sessions[1]
    assert events(runner.journal)[0] == inception_bytes
    assert len(events(runner.journal, "MODE_TRANSITION")) == 1
    assert runner.journal.verify().shadow_mode == ECONOMIC
    assert runner.journal.verify().state == initial_state(100)
    source.signals[sessions[1]] = (record("AAA"),)
    runner.clock = lambda: datetime(2024, 1, 3, 22, tzinfo=UTC)
    ready_states(db, runner.clock())
    runner.run(sessions[1])
    intent = events(runner.journal, "ENTRY_INTENT_CREATED")
    assert len(intent) == 1 and intent[0]["session"] == sessions[1].isoformat()
    assert runner.journal.verify().economic_sessions == 1
    with pytest.raises(ShadowError, match="SHADOW_ACTIVATION_NOT_READY"):
        runner.activate_economic()


def test_activation_refuses_incomplete_or_non_current_observation(case, tmp_path):
    runner, _, sessions, _ = observation(case, tmp_path)
    with pytest.raises(ShadowError, match="SHADOW_ACTIVATION_NOT_READY"):
        runner.activate_economic()
    runner.run(sessions[0])
    with pytest.raises(ShadowError, match="SHADOW_ACTIVATION_NOT_READY"):
        runner.activate_economic()
    assert not events(runner.journal, "MODE_TRANSITION")


def test_observation_crash_rollback_restart_and_concurrency(case, tmp_path, monkeypatch):
    runner, db, sessions, source = observation(case, tmp_path)
    original = runner.journal.append

    def crash(connection, kind, *args, **kwargs):
        if kind == "SESSION_COMMITTED":
            raise RuntimeError("fixture crash")
        return original(connection, kind, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(runner.journal, "append", crash)
        with pytest.raises(RuntimeError, match="fixture crash"):
            runner.run(sessions[0])
    assert runner.journal.verify().event_count == 1
    restarted = ShadowRunner(
        Journal(runner.journal.path),
        db.path,
        runner.config,
        clock=runner.clock,
        screen_source=source,
    )
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: restarted.run(sessions[0])["status"], range(2)))
    assert sorted(results) == ["ALREADY_PROCESSED", "OBSERVED_NO_ECONOMIC_DECISION"]
    assert len(events(runner.journal, "OBSERVATION_SESSION")) == 1


def test_observation_rejects_economic_events_and_mode_rewrites(case, tmp_path):
    runner, _, sessions, _ = observation(case, tmp_path)
    runner.run(sessions[0])
    import sqlite3

    with runner.journal.connection(write=True) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE events SET payload='{}' WHERE sequence_number=1")
    with pytest.raises(ShadowError), runner.journal.connection(write=True) as c:
        runner.journal.append(
            c, "MODE_TRANSITION", {"from": OBSERVATION_ONLY, "to": ECONOMIC}, now=runner.clock()
        )
        runner.journal.verify(c)
    assert runner.journal.verify().shadow_mode == OBSERVATION_ONLY


def test_economic_incomplete_refuses_without_economic_events(case):
    runner, _, sessions, source = case
    source.signals[sessions[0]] = (record("AAA"), record("BBB", quality=None))
    before = runner.journal.path.read_bytes()
    with pytest.raises(ShadowError, match="SHADOW_SESSION_DATA_NOT_READY"):
        runner.run(sessions[0])
    assert runner.journal.path.read_bytes() == before
    assert runner.journal.verify().last_session is None


def test_complete_economic_events_identical_to_pre_mode_runner(case, tmp_path, monkeypatch):
    runner, db, sessions, source = case
    code = subprocess.check_output(
        [
            "git",
            "-c",
            "safe.directory=G:/GitHub/TraWalp",
            "show",
            "HEAD:src/trading_system/shadow/runner.py",
        ],
        text=True,
    )
    legacy = types.ModuleType("fixture_pre_mode_runner")
    exec(compile(code, "fixture_pre_mode_runner", "exec"), legacy.__dict__)
    journal_code = subprocess.check_output(
        [
            "git",
            "-c",
            "safe.directory=G:/GitHub/TraWalp",
            "show",
            "HEAD:src/trading_system/shadow/journal.py",
        ],
        text=True,
    )
    legacy_journal = types.ModuleType("fixture_pre_mode_journal")
    monkeypatch.setitem(sys.modules, legacy_journal.__name__, legacy_journal)
    exec(compile(journal_code, legacy_journal.__name__, "exec"), legacy_journal.__dict__)
    ready_states(db, datetime(2023, 12, 29, 22, tzinfo=UTC))
    old = legacy.ShadowRunner(
        legacy_journal.Journal(tmp_path / "legacy.sqlite3"),
        db.path,
        runner.config,
        screen_source=source,
        clock=lambda: datetime(2024, 1, 1, 12, tzinfo=UTC),
    )
    old.initialize()
    old.clock = runner.clock
    ready_states(db, runner.clock())
    for session in sessions[:3]:
        old.run(session)
        runner.run(session)
    kinds = TRANSITIONS | {"SCREEN_EVALUATED", "DECISION_NO_TRADE"}

    def extract(j):
        return [(e["event_type"], e["payload"]) for e in events(j) if e["event_type"] in kinds]

    assert extract(old.journal) == extract(runner.journal)
    assert old.journal.verify().state == runner.journal.verify().state


def test_observation_infrastructure_failure_not_committed(case, tmp_path):
    runner, db, sessions, source = observation(case, tmp_path)
    db.set_sync_value("dataset", "asset_universe", {"status": "failed"})
    with pytest.raises(ShadowError, match="SHADOW_SESSION_DATA_NOT_READY"):
        runner.run(sessions[0])
    assert runner.journal.verify().last_session is None
    assert not events(runner.journal, "OBSERVATION_SESSION")


def test_complete_but_noncurrent_observation_cannot_activate(case, tmp_path):
    runner, _, sessions, _ = observation(case, tmp_path, incomplete=False)
    runner.run(sessions[0])
    runner.clock = lambda: datetime(2024, 1, 3, 22, tzinfo=UTC)
    with pytest.raises(ShadowError, match="SHADOW_ACTIVATION_NOT_READY"):
        runner.activate_economic()
    assert not events(runner.journal, "MODE_TRANSITION")


def test_verifier_rejects_injected_economic_screen(case, tmp_path, monkeypatch):
    runner, _, sessions, _ = observation(case, tmp_path)
    append = runner.journal.append

    def inject(connection, kind, payload, **kwargs):
        if kind == "OBSERVATION_SESSION":
            kind, payload = "SCREEN_EVALUATED", {}
        return append(connection, kind, payload, **kwargs)

    monkeypatch.setattr(runner.journal, "append", inject)
    with pytest.raises(ShadowError, match="SHADOW_JOURNAL_INTEGRITY_FAILURE"):
        runner.run(sessions[0])
    assert runner.journal.verify().event_count == 1


def test_cli_explicit_mode_and_status(case, tmp_path, monkeypatch, capsys):
    from trading_system import cli
    from trading_system.shadow import cli as shadow_cli

    runner, _, sessions, _ = observation(case, tmp_path)
    runner.run(sessions[0])
    assert cli._parser().parse_args(["shadow-f-init"]).observation_only is False
    assert (
        cli._parser().parse_args(["shadow-f-init", "--observation-only"]).observation_only is True
    )
    args = cli._parser().parse_args(["shadow-f-status", "--journal", str(runner.journal.path)])
    monkeypatch.setattr(shadow_cli, "load_strategy_config", lambda path: runner.config)
    assert shadow_cli.command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["journal_mode"] == OBSERVATION_ONLY
    assert payload["economic_sessions"] == 0
    assert payload["last_observed_session"] == sessions[0].isoformat()
    assert payload["current_position_state"] == "FLAT"
    verify_args = cli._parser().parse_args(
        ["shadow-f-verify", "--journal", str(runner.journal.path)]
    )
    assert shadow_cli.command(verify_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"


def test_mode_transition_revalidates_readiness_and_drift(case, tmp_path):
    runner, db, sessions, _ = observation(case, tmp_path, incomplete=False)
    runner.run(sessions[0])
    db.set_sync_value("dataset", "sec", {"status": "failed"})
    with pytest.raises(ShadowError, match="SHADOW_ACTIVATION_NOT_READY"):
        runner.activate_economic()
    ready_states(db, runner.clock())
    runner.config.risk.risk_per_trade = 0.02
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.activate_economic()
    assert not events(runner.journal, "MODE_TRANSITION")


def test_observation_init_keeps_current_transition_guard(case, tmp_path):
    original, db, _, source = case
    ready_states(db, datetime(2024, 1, 2, 22, tzinfo=UTC))
    journal = Journal(tmp_path / "wrong-boundary.sqlite3")
    runner = ShadowRunner(
        journal,
        db.path,
        original.config,
        screen_source=source,
        clock=lambda: datetime(2024, 1, 3, 22, tzinfo=UTC),
    )
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY") as error:
        runner.initialize(shadow_mode=OBSERVATION_ONLY)
    assert error.value.details["blocking_reasons"] == ["SIP_TRANSITION_BOUNDARY_NOT_CURRENT"]
    assert not journal.path.exists()
