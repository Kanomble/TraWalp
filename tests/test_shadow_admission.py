"""Offline preflight/init parity; recording sink never creates a journal."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_champion_consolidation import bar
from test_champion_consolidation import offline as offline
from test_forward_sip_contract import db as db
from test_forward_sip_contract import seed
from test_shadow_execution import ready_states
from test_shadow_qualification import SESSION, candidate, report

from trading_system import cli
from trading_system.config import load_strategy_config
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.shadow import cli as shadow_cli
from trading_system.shadow.admission import evaluate_inception_admission
from trading_system.shadow.contracts import ShadowError, digest
from trading_system.shadow.modes import ECONOMIC, OBSERVATION_ONLY
from trading_system.shadow.runner import ShadowRunner


@pytest.fixture
def setup(db, tmp_path):
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    seed(db, [bar("SPY", SESSION)])
    config = load_strategy_config().model_copy(deep=True)
    config.storage.database_path = db.path
    row = candidate(count=1, reasons=("insufficient_market_history",))
    source = SimpleNamespace(screen=lambda day: report(row))
    calls = []
    journal = SimpleNamespace(
        path=tmp_path / "never-created.sqlite3", initialize=lambda **kw: calls.append(kw) or kw
    )
    return SimpleNamespace(
        db=db, config=config, now=now, source=source, journal=journal, calls=calls
    )


def admission(case, mode):
    return evaluate_inception_admission(
        case.db.path,
        case.config,
        journal_path=case.journal.path,
        now=case.now,
        requested_mode=mode,
        screen_source=case.source,
    )


def run_init(case, mode, clock=None):
    return ShadowRunner(
        case.journal,
        case.db.path,
        case.config,
        clock=clock or (lambda: case.now),
        screen_source=case.source,
    ).initialize(shadow_mode=mode)


@pytest.mark.parametrize("mode", [ECONOMIC, OBSERVATION_ONLY])
@pytest.mark.parametrize(
    "problem",
    [
        "ranking",
        "complete",
        "boundary",
        "undeclared",
        "invalid_boundary",
        "spy",
        "sec",
        "asset",
        "stale",
    ],
)
def test_preflight_init_parity(setup, mode, problem):
    case = setup
    if problem == "complete":
        case.source.screen = lambda day: report(candidate(count=0, reasons=("financial_excluded",)))
    elif problem in {"boundary", "undeclared", "invalid_boundary"}:
        state = case.db.sync_values("dataset")["forward_sip"]
        state["end"] = {"boundary": "2026-10-22", "undeclared": None, "invalid_boundary": "bad"}[
            problem
        ]
        case.db.set_sync_value("dataset", "forward_sip", state)
    elif problem == "spy":
        with case.db.connect() as c:
            c.execute("DELETE FROM forward_market_observations WHERE symbol='SPY'")
    elif problem in {"sec", "asset", "stale"}:
        key = "asset_universe" if problem == "asset" else "sec"
        state = case.db.sync_values("dataset")[key]
        if problem == "stale":
            state["last_success_at"] = (case.now - timedelta(hours=97)).isoformat()
        else:
            state.update(status="failed", errors=1)
        case.db.set_sync_value("dataset", key, state)
    expected = problem == "complete" or (problem == "ranking" and mode == OBSERVATION_ONLY)
    before = case.db.path.read_bytes()
    result = admission(case, mode)
    output = result.as_dict()
    assert output["requested_mode"] == mode
    assert output["selected_mode_ready"] is expected
    assert output["economic_shadow_ready"] is (problem == "complete")
    assert output["observation_shadow_ready"] is (problem in {"complete", "ranking"})
    assert output["ranking_complete"] is (problem == "complete")
    assert output["transition_boundary_current"] is (
        problem not in {"boundary", "undeclared", "invalid_boundary"}
    )
    assert output["analysis_session"] == SESSION.isoformat()
    assert len(output["input_readiness_digest"]) == 64
    assert output["point_in_time_only"]
    if expected:
        assert not output["blocking_reasons"]
        run_init(case, mode)
        stored = case.calls[0]["input_readiness"]
        assert stored["input_readiness_digest"] == output["input_readiness_digest"]
        assert stored["input_readiness_digest"] == digest(
            {k: v for k, v in stored.items() if k != "input_readiness_digest"}
        )
    else:
        with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY") as error:
            run_init(case, mode)
        assert error.value.details["blocking_reasons"] == output["blocking_reasons"]
        assert not case.calls
    assert case.db.path.read_bytes() == before
    assert not case.journal.path.exists()


@pytest.mark.parametrize("mode,exit_code", [(ECONOMIC, 1), (OBSERVATION_ONLY, 0)])
def test_cli_requested_mode_and_read_only(setup, monkeypatch, capsys, mode, exit_code):
    from trading_system.shadow import admission as module

    case = setup
    original = module.evaluate_inception_admission

    def evaluate(*args, **kwargs):
        return original(*args, **kwargs, now=case.now, screen_source=case.source)

    monkeypatch.setattr(module, "evaluate_inception_admission", evaluate)
    monkeypatch.setattr(shadow_cli, "load_strategy_config", lambda path: case.config)

    def forbidden(*args, **kwargs):
        pytest.fail("Preflight constructed a journal")

    monkeypatch.setattr(shadow_cli, "Journal", forbidden)
    argv = ["shadow-f-preflight", "--journal", str(case.journal.path)]
    if mode == OBSERVATION_ONLY:
        argv.append("--observation-only")
    args = cli._parser().parse_args(argv)
    # A closed rollback-journal fixture also permits checking filesystem identity.
    # WAL readers may create SQLite-managed -shm/-wal coordination files even
    # with mode=ro; the parity cases above separately check unchanged WAL DB bytes.
    with case.db.connect() as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    before = {p.name: p.read_bytes() for p in case.db.path.parent.iterdir() if p.is_file()}
    assert shadow_cli.command(args) == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output["requested_mode"] == mode
    assert output["selected_mode_ready"] is (exit_code == 0)
    assert output["blocking_reasons"] == ([] if exit_code == 0 else ["RANKING_UNRESOLVED"])
    assert {p.name: p.read_bytes() for p in case.db.path.parent.iterdir() if p.is_file()} == before


def test_just_completed_session_requires_new_boundary(setup):
    case = setup
    case.now = regular_session_bounds(SESSION)[1] + timedelta(microseconds=1)
    # States refreshed just after the close still need an explicit new end.
    ready_states(case.db, case.now)
    state = case.db.sync_values("dataset")["forward_sip"]
    state["end"] = "2026-10-22"
    case.db.set_sync_value("dataset", "forward_sip", state)
    assert admission(case, OBSERVATION_ONLY).details["blocking_reasons"] == [
        "SIP_TRANSITION_BOUNDARY_NOT_CURRENT"
    ]
    state["end"] = SESSION.isoformat()
    case.db.set_sync_value("dataset", "forward_sip", state)
    assert admission(case, OBSERVATION_ONLY).selected_mode_ready
    run_init(case, OBSERVATION_ONLY)


@pytest.mark.parametrize("change", ["session", "clock"])
def test_init_time_change_refuses_without_journal(setup, change):
    case = setup
    assert admission(case, OBSERVATION_ONLY).selected_mode_ready
    completed = (
        datetime(2026, 10, 26, 22, tzinfo=UTC)
        if change == "session"
        else case.now - timedelta(seconds=1)
    )
    times = iter([case.now, completed])
    code = (
        "SHADOW_INCEPTION_SESSION_CHANGED_RETRY"
        if change == "session"
        else "SHADOW_INCEPTION_CLOCK_REGRESSION"
    )
    with pytest.raises(ShadowError, match=code):
        run_init(case, OBSERVATION_ONLY, clock=lambda: next(times))
    assert not case.calls
    assert not case.journal.path.exists()


def test_config_guard_is_shared(setup):
    case = setup
    case.config.risk.risk_per_trade = 0.02
    for operation in (
        lambda: admission(case, OBSERVATION_ONLY),
        lambda: run_init(case, OBSERVATION_ONLY),
    ):
        with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
            operation()
    assert not case.calls


def test_preflight_is_not_a_later_init_guarantee(setup):
    case = setup
    assert admission(case, OBSERVATION_ONLY).selected_mode_ready
    case.db.set_sync_value("dataset", "sec", {"status": "failed", "errors": 1})
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY"):
        run_init(case, OBSERVATION_ONLY)
    assert not case.calls


@pytest.mark.parametrize("feed,adjustment", [("iex", "all"), ("sip", "raw")])
def test_no_feed_or_adjustment_fallback(setup, feed, adjustment):
    case = setup
    with case.db.connect() as connection:
        connection.execute("DELETE FROM forward_market_observations WHERE symbol='SPY'")
    seed(case.db, [bar("SPY", SESSION)], feed=feed, adjustment=adjustment)
    result = admission(case, OBSERVATION_ONLY)
    assert not result.selected_mode_ready
    assert "SPY_CURRENT_OBSERVATION_UNUSABLE" in result.details["blocking_reasons"]
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY"):
        run_init(case, OBSERVATION_ONLY)


@pytest.mark.parametrize("operation", ["run", "activate_economic"])
def test_existing_journal_runtime_drift_still_blocks_before_inputs(setup, monkeypatch, operation):
    from trading_system.shadow import contracts

    case = setup
    identity = contracts.fingerprints(case.config)
    runtime = contracts.runtime_manifest()
    monkeypatch.setattr(contracts, "runtime_manifest", lambda: {**runtime, "pandas": "changed"})
    case.journal.connection = lambda **kwargs: nullcontext(None)
    case.journal.verify = lambda connection: SimpleNamespace(inception=identity)
    case.source.screen = lambda day: pytest.fail("Read inputs before drift guard")
    runner = ShadowRunner(
        case.journal, case.db.path, case.config, clock=lambda: case.now, screen_source=case.source
    )
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        getattr(runner, operation)()
    assert not case.calls
