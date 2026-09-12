"""Simulated-clock progress and bounded local research/export regression checks."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_f_lifecycle_v2 as fixtures
from test_f_lifecycle_v2 import (
    Screens,
    native_session,
    record,
    research_preparation,
    run,
)

from trading_system import cli
from trading_system.backtest import lifecycle_validation as validation
from trading_system.backtest import progress
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.progress import ProgressPhase

config = fixtures.config
local_market = fixtures.local_market


@pytest.fixture
def monotonic(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock.now)
    return clock


def test_boundaries_interval_counters_and_percentage(monotonic, caplog):
    caplog.set_level(logging.INFO, logger=progress.__name__)
    with ProgressPhase(
        "fixture_progress", "discovery", percentage=("done", "total"), done=0, total=10,
    ) as phase:
        assert "status=starting" in caplog.messages[0]
        assert "progress_pct=0.0" in caplog.messages[0]
        monotonic.now += 1799
        phase.update(done=4)
        assert len(caplog.messages) == 1
        monotonic.now += 1
        phase.heartbeat()
        assert len(caplog.messages) == 2
        assert "status=running" in caplog.messages[-1]
        assert "elapsed_seconds=1800.000" in caplog.messages[-1]
        assert "done=4 total=10 progress_pct=40.0" in caplog.messages[-1]
        monotonic.now += 1799
        phase.update(done=12)
        assert len(caplog.messages) == 2
        monotonic.now += 2
        phase.heartbeat()
        assert "progress_pct=100.0" in caplog.messages[-1]
    assert "status=completed" in caplog.messages[-1]
    assert phase.seconds == 3601
    assert not phase._worker.is_alive()


@pytest.mark.parametrize("total", [None, 0])
def test_unknown_or_empty_denominator_has_no_percentage(monotonic, caplog, total):
    caplog.set_level(logging.INFO, logger=progress.__name__)
    counters = {} if total is None else {"total": total}
    with ProgressPhase(
        "fixture_progress", "preparation", percentage=("done", "total"), done=0, **counters,
    ) as phase:
        monotonic.now += 1800
        phase.heartbeat()
    assert len(caplog.messages) == 3
    assert "progress_pct" not in caplog.text


def test_short_phase_and_interruption_stop_worker(monotonic, caplog):
    caplog.set_level(logging.INFO, logger=progress.__name__)
    with ProgressPhase("fixture", "short"):
        monotonic.now += 1
    assert ["starting" in caplog.messages[0], "completed" in caplog.messages[1]] == [True, True]
    caplog.clear()
    with pytest.raises(KeyboardInterrupt), ProgressPhase("fixture", "interrupted") as phase:
        raise KeyboardInterrupt
    assert len(caplog.messages) == 1
    assert "completed" not in caplog.text
    assert not phase._worker.is_alive()


def test_watchdog_heartbeat_without_counter_updates(monotonic, monkeypatch, caplog):
    """Simulate the worker's timer wake while the main thread is in one blocking operation."""
    caplog.set_level(logging.INFO, logger=progress.__name__)
    with ProgressPhase("fixture", "blocking") as phase:
        # Stop the real wait; exercise the same watcher deterministically, without sleeping.
        phase._stop.set()
        phase._worker.join()
        waits = iter((False, True))
        monkeypatch.setattr(phase._stop, "is_set", lambda: False)

        def wait(interval):
            assert interval == 1800
            monotonic.now += interval
            return next(waits)

        monkeypatch.setattr(phase._stop, "wait", wait)
        phase._watch()
        assert "status=running" in caplog.messages[-1]
        assert "elapsed_seconds=1800.000" in caplog.messages[-1]


def test_preflight_counts_empty_sessions_and_keeps_coverage_results(
    monkeypatch, local_market, config, monotonic, caplog,
):
    _, db, sessions, preparation = research_preparation(monkeypatch, local_market)
    db.upsert_bars(native_session(sessions[1], weak=True))
    preparation.screen_source = Screens(sessions[0], (record("AAA"), record("BBB")))
    original_screen = preparation.screen_source.screen

    def screen(session):
        monotonic.now += 1800
        return original_screen(session)

    monkeypatch.setattr(preparation.screen_source, "screen", screen)
    caplog.set_level(logging.INFO, logger=progress.__name__)
    report, requirements = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1], preparation=preparation,
    )
    perf = report["performance"]
    assert perf["candidate_sessions"] == perf["unique_candidate_symbols"] == 2
    assert perf["coverage_batches"] == 2
    assert perf["sqlite_query_count_coverage"] == 4
    assert perf["daily_rows_loaded"] == 1
    assert perf["intraday_rows_loaded"] == len(native_session(sessions[1], weak=True))
    assert perf["candidate_discovery_seconds"] == 1800 * (len(sessions) - 1)
    assert [row["candidate_rank"] for row in requirements["candidate_sessions"]] == [1, 2]
    assert [row["decision_status"] for row in report["coverage"]] == [
        "OPENING_WEAKNESS_VETO", "INTRADAY_UNAVAILABLE",
    ]
    assert report["coverage"][0]["status"] == "QUALIFIED"
    discovery = [m for m in caplog.messages if "phase=candidate_discovery" in m]
    assert f"sessions_processed={len(sessions) - 1}" in discovery[-1]
    assert "candidate_sessions_discovered=2" in discovery[-1]
    assert "progress_pct=100.0" in discovery[-1]
    completion = [m for m in caplog.messages if "phase=coverage_evaluation status=completed" in m]
    assert (
        "candidate_sessions_checked=2 candidate_sessions_total=2 qualified=1 unavailable=1"
    ) in completion[0]
    # Disable emission and rerun the same research; only performance metadata may differ.
    monkeypatch.setattr(ProgressPhase, "_emit", lambda *args: None)
    quiet_report, quiet_requirements = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1], preparation=preparation,
    )
    assert requirements == quiet_requirements
    report.pop("performance")
    quiet_report.pop("performance")
    assert report == quiet_report


@pytest.mark.parametrize("veto", [False, True])
def test_engine_heartbeat_counts_and_result_equivalence(
    monkeypatch, local_market, config, monotonic, caplog, veto,
):
    db, sessions = local_market
    db.upsert_bars(native_session(sessions[1], weak=False))
    baseline = run(db, sessions, config, opening_weakness_veto=veto)
    original = db.bars_on_session

    def bars_on_session(*args):
        monotonic.now += 1800
        return original(*args)

    monkeypatch.setattr(db, "bars_on_session", bars_on_session)
    caplog.set_level(logging.INFO, logger=progress.__name__)
    with ProgressPhase(
        "intraday_validation_progress", "backtest", strategy="I1" if veto else "I0",
        percentage=("sessions_processed", "sessions_total"),
    ) as phase:
        observed = run(db, sessions, config, opening_weakness_veto=veto, progress=phase)
    assert observed == baseline
    assert phase.counters["sessions_processed"] == phase.counters["sessions_total"] == len(sessions)
    assert phase.counters["positions_closed"] == len(observed.positions)
    assert phase.counters["active_positions"] == 0
    assert phase.counters["session"] == sessions[-1].isoformat()
    assert "progress_pct=100.0" in caplog.messages[-1]


def test_validation_stages_counters_and_persisted_timings(
    monkeypatch, local_market, config, tmp_path, monotonic, caplog,
):
    _, db, sessions, _ = research_preparation(monkeypatch, local_market)
    db.upsert_bars(native_session(sessions[1], weak=True))
    original = BacktestEngine.run

    def timed_run(*args, **kwargs):
        monotonic.now += 3
        return original(*args, **kwargs)

    monkeypatch.setattr(BacktestEngine, "run", timed_run)
    caplog.set_level(logging.INFO, logger=progress.__name__)
    bundle = validation.run_f_intraday_entry(db, config, sessions[0], sessions[-1])
    perf = bundle.summary["performance"]
    assert perf["i0_seconds"] == perf["i1_seconds"] == 3
    assert perf["sessions_processed_I0"] == perf["sessions_processed_I1"] == len(sessions)
    assert perf["positions_I0"] == 1
    assert perf["positions_I1"] == 0
    paths = validation.export_f_lifecycle_research(bundle, tmp_path, stem="timed")
    persisted = json.loads(paths["summary.json"].read_text())["performance"]
    assert persisted == perf
    assert set((
        "qualification_seconds", "coverage_verification_seconds", "i0_seconds", "i1_seconds",
        "diagnostics_seconds", "export_seconds", "total_seconds",
    )) <= persisted.keys()
    assert persisted["total_seconds"] == 6
    starts = [m.split("phase=")[1].split()[0] for m in caplog.messages if "status=starting" in m]
    assert starts == [
        "qualification", "coverage_verification", "candidate_discovery", "coverage_load",
        "coverage_evaluation", "diagnostics_prepare", "backtest", "backtest", "diagnostics",
        "export",
    ]
    assert "F intraday entry validation: completed" in caplog.text
    assert "F intraday entry validation timings:" in caplog.text
    monkeypatch.setattr(ProgressPhase, "_emit", lambda *args: None)
    quiet = validation.run_f_intraday_entry(db, config, sessions[0], sessions[-1])
    assert bundle.tables == quiet.tables
    for strategy, result in bundle.results.items():
        assert result.model_dump(exclude={"generated_at"}) == quiet.results[strategy].model_dump(
            exclude={"generated_at"}
        )
    quiet_paths = validation.export_f_lifecycle_research(quiet, tmp_path, stem="quiet")
    for name, path in paths.items():
        if name.endswith(".csv"):
            assert path.read_bytes() == quiet_paths[name].read_bytes()


def test_preflight_measures_preparation_and_export(
    monkeypatch, local_market, config, tmp_path, monotonic, caplog,
):
    db, sessions = local_market
    source = Screens(sessions[0])
    source.diagnostics = SimpleNamespace(bars_rows_loaded=123)
    preparation = SimpleNamespace(sessions=sessions, screen_source=source)

    def timed(function, seconds):
        def call(*args, **kwargs):
            monotonic.now += seconds
            return function(*args, **kwargs)
        return call

    monkeypatch.setattr(
        validation, "_qualify_daily_research",
        timed(lambda *args: {"ready": True, "failure_reasons": []}, 2),
    )
    monkeypatch.setattr(
        validation, "prepare_strategy_comparison", timed(lambda *args, **kwargs: preparation, 3),
    )
    monkeypatch.setattr(db, "bars_between", timed(db.bars_between, 5))
    monkeypatch.setattr(db, "bars_available_as_of", timed(db.bars_available_as_of, 7))
    monkeypatch.setattr(
        validation, "opening_weakness_decision", timed(validation.opening_weakness_decision, 11),
    )
    caplog.set_level(logging.INFO, logger=progress.__name__)
    report, requirements = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1],
    )
    perf = report["performance"]
    assert perf["daily_qualification_seconds"] == 2
    assert perf["screen_source_prepare_seconds"] == 3
    assert perf["candidate_discovery_seconds"] == 0
    assert perf["coverage_load_seconds"] == 12
    assert perf["coverage_evaluation_seconds"] == 11
    assert perf["total_seconds"] == 28
    assert perf["daily_rows_loaded"] == 124
    monkeypatch.setattr(validation, "_atomic_text", timed(validation._atomic_text, 13))
    monkeypatch.setattr(validation, "_atomic_csv", timed(validation._atomic_csv, 17))
    paths = validation.export_f_intraday_entry_preflight(
        report, requirements, tmp_path, stem="timings",
    )
    persisted = json.loads(paths["preflight.json"].read_text())["performance"]
    assert persisted == perf
    assert persisted["export_seconds"] == 43
    assert persisted["total_seconds"] == 71
    assert "Intraday entry preflight timings:" in caplog.text
    timings = next(m for m in caplog.messages if "Intraday entry preflight timings:" in m)
    assert "export_seconds=56.000000" in timings
    assert "total_seconds=84.000000" in timings
    assert "export_timing_scope=through_publication" in timings


@pytest.mark.parametrize("validation_run", [False, True])
@pytest.mark.parametrize("interrupt_at", ["staging", "publication"])
def test_interrupted_exports_leave_no_artifacts(
    monkeypatch, local_market, config, tmp_path, validation_run, interrupt_at,
):
    _, db, sessions, _ = research_preparation(monkeypatch, local_market)
    db.upsert_bars(native_session(sessions[1], weak=False))
    report, requirements = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1],
    )
    bundle = validation.run_f_intraday_entry(db, config, sessions[0], sessions[-1])
    directory = tmp_path / "exports"
    directory.mkdir()
    sentinel = directory / "previous_summary.json"
    sentinel.write_text("previous complete run")
    if interrupt_at == "staging":
        def interrupted(*args):
            raise KeyboardInterrupt
        monkeypatch.setattr(validation, "_atomic_csv", interrupted)
    else:
        original = validation.os.replace

        def replace(source, target):
            original(source, target)
            if Path(target).parent == directory:
                raise KeyboardInterrupt

        monkeypatch.setattr(validation.os, "replace", replace)
    with pytest.raises(KeyboardInterrupt):
        if validation_run:
            validation.export_f_lifecycle_research(bundle, directory, stem="interrupted")
        else:
            validation.export_f_intraday_entry_preflight(
                report, requirements, directory, stem="interrupted",
            )
    assert list(directory.iterdir()) == [sentinel]
    assert sentinel.read_text() == "previous complete run"


@pytest.mark.parametrize("command", ["preflight-f-intraday-entry", "validate-f-intraday-entry"])
def test_cli_interrupt_terminates_without_export(monkeypatch, config, tmp_path, command):
    monkeypatch.setattr(cli, "load_settings", lambda *args: SimpleNamespace(strategy=config))
    config.storage.reports_path = tmp_path / "reports"

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "build_f_intraday_entry_preflight", interrupted)
    monkeypatch.setattr(cli, "run_f_intraday_entry", interrupted)
    assert cli.main([
        command, "--start", "2024-01-02", "--end", "2024-02-23", "--output-stem", "interrupt",
    ]) == 130
    assert not config.storage.reports_path.exists()


@pytest.mark.parametrize("stage", ["discovery", "coverage_load", "coverage_evaluation", "backtest"])
def test_cli_interrupt_during_research_leaves_no_reports(
    monkeypatch, local_market, config, tmp_path, caplog, stage,
):
    _, db, sessions, preparation = research_preparation(monkeypatch, local_market)
    db.upsert_bars(native_session(sessions[1], weak=False))
    config.storage.database_path = db.path
    config.storage.reports_path = tmp_path / "reports"
    monkeypatch.setattr(cli, "load_settings", lambda *args: SimpleNamespace(strategy=config))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    if stage == "discovery":
        monkeypatch.setattr(preparation.screen_source, "screen", interrupted)
    elif stage == "coverage_load":
        monkeypatch.setattr(validation.Database, "bars_between", interrupted)
    elif stage == "coverage_evaluation":
        monkeypatch.setattr(validation, "opening_weakness_decision", interrupted)
    else:
        monkeypatch.setattr(BacktestEngine, "run", interrupted)
    command = "validate-f-intraday-entry" if stage == "backtest" else "preflight-f-intraday-entry"
    caplog.set_level(logging.INFO)
    assert cli.main([
        command, "--start", sessions[0].isoformat(), "--end", sessions[-1].isoformat(),
        "--output-stem", "interrupt",
    ]) == 130
    assert not config.storage.reports_path.exists()
    assert "interrupted by user" in caplog.text
    assert "phase=export" not in caplog.text
