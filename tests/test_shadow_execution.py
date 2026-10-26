"""Forward Shadow acceptance on temporary SQLite and small deterministic fixtures only."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_champion_consolidation import bar, record
from test_champion_consolidation import offline as offline

from trading_system import cli
from trading_system.backtest.data_integrity import DataIntegrityError, DataIntegrityMode
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.champion import champion_ranking, run_champion_backtest
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
from trading_system.models.screening import ScreenReport
from trading_system.shadow import runner as runner_module
from trading_system.shadow.contracts import GENESIS_HASH, ShadowError, digest, fingerprints
from trading_system.shadow.inputs import LocalInputs
from trading_system.shadow.journal import Journal
from trading_system.shadow.runner import ShadowRunner


@pytest.fixture
def config():
    return StrategyConfig.model_validate(_read_yaml(Path("config/strategy.yaml")))


class Screens:
    def __init__(self, signals=None):
        self.signals = signals or {}

    def screen(self, session):
        rows = self.signals.get(session, (record(quality=50),))
        rows = tuple(
            row.model_copy(
                update={
                    "as_of": session,
                    "technical": row.technical.model_copy(update={"market_session": session}),
                }
            )
            for row in rows
        )
        return ScreenReport(
            as_of=session,
            generated_at="synthetic",
            analyzed_count=len(rows),
            eligible_count=0,
            records=rows,
        )


@pytest.fixture
def case(tmp_path, config):
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 1, 23))
    db.upsert_bars([bar(symbol, day) for symbol in ("AAA", "BBB", "SPY") for day in sessions])
    source = Screens({sessions[0]: (record("BBB"), record("AAA"))})
    journal = Journal(tmp_path / "shadow.sqlite3")
    runner = ShadowRunner(
        journal,
        db.path,
        config,
        screen_source=source,
        clock=lambda: datetime(2024, 1, 1, 12, tzinfo=UTC),
    )
    runner.initialize()
    runner.clock = lambda: datetime(2024, 2, 1, tzinfo=UTC)
    return runner, db, sessions, source


def events(journal, kind=None):
    with journal.connection() as connection:
        rows = connection.execute("SELECT * FROM events ORDER BY sequence_number").fetchall()
    return [
        {**dict(row), "payload": json.loads(row["payload"])}
        for row in rows
        if kind is None or row["event_type"] == kind
    ]


def test_inception_exactly_once_and_no_backfill(case):
    runner, _, _, _ = case
    verified = runner.journal.verify()
    metadata = verified.inception
    assert verified.event_count == 1 and verified.last_session is None
    assert metadata["initial_capital"] == 100.0
    assert metadata["strategy_identity"] == "FROZEN_CHAMPION_F"
    assert metadata["production_label"] == "F/configured/C1"
    assert metadata["latest_completed_official_session_at_inception"] == "2023-12-29"
    for key, value in fingerprints(runner.config).items():
        assert metadata[key] == value
    before = runner.journal.path.read_bytes()
    with pytest.raises(ShadowError, match="SHADOW_ALREADY_INITIALIZED"):
        runner.initialize(capital=200)
    assert runner.journal.path.read_bytes() == before


def test_custom_capital_and_no_market_database_needed(tmp_path, config):
    journal = Journal(tmp_path / "shadow.sqlite3")
    runner = ShadowRunner(
        journal, tmp_path / "absent.sqlite3", config, clock=lambda: datetime(2024, 1, 1, tzinfo=UTC)
    )
    runner.initialize(capital=123.45)
    assert journal.verify().state["cash"] == 123.45
    assert not runner.market_path.exists()


@pytest.mark.parametrize("capital", [0, -1, float("nan"), float("inf")])
def test_invalid_capital_does_not_create_file(tmp_path, config, capital):
    runner = ShadowRunner(Journal(tmp_path / "shadow.sqlite3"), tmp_path / "missing", config)
    with pytest.raises(ShadowError, match="SHADOW_INVALID_INITIAL_CAPITAL"):
        runner.initialize(capital=capital)
    assert not runner.journal.path.exists()


@pytest.mark.parametrize(
    "field",
    [
        "economic_config_fingerprint",
        "strategy_fingerprint",
        "production_label",
        "engine_fingerprint",
        "strict_contract_fingerprint",
        "data_integrity_contract_version",
        "journal_schema_version",
    ],
)
def test_drift_fails_before_read_or_append(case, monkeypatch, field):
    runner, _, _, _ = case
    before = events(runner.journal)
    current = fingerprints(runner.config)
    monkeypatch.setattr(runner_module, "fingerprints", lambda config: {**current, field: "CHANGED"})
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()
    assert events(runner.journal) == before


def test_real_economic_config_change_is_drift(case):
    runner, _, _, _ = case
    runner.config.backtest.min_quality_score += 1
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()


def test_modified_engine_file_changes_fingerprint(case, monkeypatch):
    runner, _, _, _ = case
    before = fingerprints(runner.config)
    original = Path.read_bytes

    def read(path):
        content = original(path)
        return content + b"\n# execution drift\n" if path.name == "position_manager.py" else content

    monkeypatch.setattr(Path, "read_bytes", read)
    assert fingerprints(runner.config)["engine_fingerprint"] != before["engine_fingerprint"]
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()


def test_default_next_official_session_and_duplicate_are_deterministic(case):
    runner, _, sessions, _ = case
    assert runner.run()["session"] == sessions[0]
    before = events(runner.journal)
    assert runner.run(sessions[0])["status"] == "ALREADY_PROCESSED"
    assert events(runner.journal) == before
    assert runner.run()["session"] == sessions[1]


def test_gap_rejected(case):
    runner, _, sessions, _ = case
    with pytest.raises(ShadowError, match="SHADOW_SESSION_GAP"):
        runner.run(sessions[1])
    assert runner.journal.verify().event_count == 1


@pytest.mark.parametrize("session", [date(2023, 12, 28), date(2023, 12, 29)])
def test_pre_inception_rejected(case, session):
    with pytest.raises(ShadowError, match="SHADOW_BEFORE_INCEPTION"):
        case[0].run(session)


@pytest.mark.parametrize("session", [date(2024, 1, 1), date(2024, 1, 6)])
def test_non_xnys_session_rejected(case, session):
    with pytest.raises(DataIntegrityError, match="SESSION_NOT_OFFICIAL"):
        case[0].run(session)


def test_current_incomplete_future_and_no_new_session(case):
    runner, _, sessions, _ = case
    runner.clock = lambda: regular_session_bounds(sessions[0])[1] - timedelta(microseconds=1)
    with pytest.raises(DataIntegrityError, match="SESSION_NOT_COMPLETE"):
        runner.run(sessions[0])
    assert runner.run()["status"] == "NO_COMPLETED_SESSION"
    with pytest.raises(DataIntegrityError, match="SESSION_NOT_COMPLETE"):
        runner.run(sessions[1])
    runner.clock = lambda: regular_session_bounds(sessions[0])[1]
    assert runner.run()["session"] == sessions[0]


@pytest.mark.parametrize("atr", [2, 0.1, 50])
def test_signal_pending_next_open_sizing_and_c1(case, atr):
    runner, db, sessions, source = case
    source.signals[sessions[0]] = (record("BBB", atr=atr), record("AAA", atr=atr))
    # Isolate sizing: the tight ATR fixture must survive its entry-session low.
    db.upsert_bars([bar("AAA", sessions[1], low=100)])
    runner.run()
    state = runner.journal.verify().state
    assert state["cash"] == 100 and state["position"] is None
    assert state["pending"]["order"]["record"]["symbol"] == "AAA"
    assert state["pending"]["expected_execution_session"] == sessions[1].isoformat()
    runner.run()
    state = runner.journal.verify().state
    position = state["position"]
    assert state["pending"] is None
    assert position["entry_price"] == 100 * 1.0005
    stop_distance = min(atr * 2, position["entry_price"] * 0.1)
    assert position["quantity"] == min(100 * 0.01 / stop_distance, 100 / position["entry_price"])
    assert position["stop_price"] == position["entry_price"] - stop_distance
    assert position["target_price"] == position["entry_price"] * 1.12
    assert position["holding_days"] == 1
    assert events(runner.journal, "DECISION_NO_TRADE")[-1]["payload"]["reason"] == "CAPACITY_FULL"
    assert len(events(runner.journal, "ENTRY_EXECUTED")) == 1
    candidates = events(runner.journal, "SCREEN_EVALUATED")[0]["payload"]["candidates"]
    canonical = champion_ranking(case[3].screen(sessions[0]), runner.config)
    assert [row["symbol"] for row in candidates] == [
        r.symbol for r in canonical.records if r.eligible
    ]
    assert candidates[0]["f_score"] == 90


@pytest.mark.parametrize(
    "missing_index,expected_code",
    [
        (1, "PENDING_ENTRY_BAR_MISSING"),
        (2, "HELD_POSITION_BAR_MISSING"),
        (10, "HELD_POSITION_BAR_MISSING"),
    ],
)
def test_strict_preflight_no_economic_commit_and_no_deferral(
    case, monkeypatch, missing_index, expected_code
):
    runner, db, sessions, _ = case
    for _ in range(missing_index):
        runner.run()
    before = runner.journal.verify()
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='AAA' AND substr(timestamp,1,10)=?",
            (sessions[missing_index].isoformat(),),
        )
    original = LocalInputs.bars_available_as_of

    def history(inputs, symbol, day, **kwargs):
        assert not (symbol == "AAA" and day == sessions[missing_index]), "stale fallback forbidden"
        return original(inputs, symbol, day, **kwargs)

    monkeypatch.setattr(LocalInputs, "bars_available_as_of", history)
    with pytest.raises(DataIntegrityError, match=expected_code):
        runner.run()
    after = runner.journal.verify()
    assert after.state == before.state and after.last_session == before.last_session
    assert after.event_count == before.event_count + 1
    assert events(runner.journal)[-1]["event_type"] == "DATA_PREFLIGHT_FAILED"
    if missing_index == 1:
        assert not events(runner.journal, "ENTRY_EXECUTED")
        assert after.state["pending"] == before.state["pending"]
    # Repair belongs to a separate explicit process; the runner itself never defers.
    db.upsert_bars([bar("AAA", sessions[missing_index])])
    monkeypatch.setattr(LocalInputs, "bars_available_as_of", original)
    assert runner.run()["session"] == sessions[missing_index]


def test_strict_api_is_mandatory(case, monkeypatch):
    runner = case[0]
    calls = []
    original = runner_module.validate_session_position_data

    def preflight(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "validate_session_position_data", preflight)
    runner.run()
    runner.run()
    assert calls[-1]["pending_entries"] == {"AAA": case[2][0]}
    assert calls[-1]["previous_processed_session"] == case[2][0]


@pytest.mark.parametrize(
    "scenario,opening,high,low,reason",
    [
        ("gap_stop", 90, 120, 89, "stop_loss"),
        ("opening_target", 120, 121, 90, "profit_target"),
        ("ambiguity_stop_first", 100, 120, 90, "stop_loss"),
        ("target", 100, 120, 99, "profit_target"),
        ("max_hold", 100, 101, 99, "time_exit"),
    ],
)
def test_focused_champion_equivalence(case, scenario, opening, high, low, reason):
    runner, db, sessions, source = case
    db.upsert_bars([bar("AAA", sessions[2], opening=opening, high=high, low=low)])
    baseline = run_champion_backtest(
        db,
        runner.config,
        sessions[0],
        sessions[12],
        screen_source=source,
        data_integrity_mode=DataIntegrityMode.STRICT,
    )
    before = db.path.read_bytes()
    for day in sessions[:13]:
        runner.run(day)
    exits = events(runner.journal, "EXIT_EXECUTED")
    assert len(exits) == len(baseline.trades) == 1
    actual, expected = exits[0]["payload"]["trade"], baseline.trades[0].model_dump(mode="json")
    for key in (
        "symbol",
        "entry_date",
        "entry_price",
        "quantity",
        "stop_price",
        "target_price",
        "exit_date",
        "exit_reason",
        "exit_price",
        "pnl",
        "holding_days",
    ):
        assert actual[key] == expected[key], (scenario, key)
    assert actual["exit_reason"] == reason
    state = runner.journal.verify().state
    assert state["position"] is None and state["pending"] is None
    assert state["cash"] == baseline.equity_curve[-1].cash
    assert runner.status()["equity"] == baseline.equity_curve[-1].portfolio_equity
    assert before == db.path.read_bytes()


def test_reentry_after_exit_matches_champion(case):
    runner, db, sessions, source = case
    source.signals[sessions[2]] = (record("AAA", quality=95), record("BBB", quality=80))
    db.upsert_bars([bar("AAA", sessions[2], opening=90, high=101, low=89)])
    baseline = run_champion_backtest(
        db,
        runner.config,
        sessions[0],
        sessions[-1],
        screen_source=source,
        data_integrity_mode=DataIntegrityMode.STRICT,
    )
    for session in sessions:
        runner.run(session)
    entries = events(runner.journal, "ENTRY_EXECUTED")
    assert len(entries) == 2 and entries[1]["payload"]["after"]["position"]["is_reentry"]
    assert entries[1]["payload"]["after"]["position"]["entry_date"] == sessions[3].isoformat()
    exits = events(runner.journal, "EXIT_EXECUTED")
    assert [
        (e["payload"]["trade"]["exit_reason"], e["payload"]["trade"]["pnl"]) for e in exits
    ] == [(t.exit_reason, t.pnl) for t in baseline.trades]
    assert runner.status()["cash"] == baseline.equity_curve[-1].cash


@pytest.mark.parametrize(
    "kind,expected",
    [("no_candidates", "NO_ELIGIBLE_CANDIDATES"), ("incomplete", "NO_TRADE_DATA_INCOMPLETE")],
)
def test_no_trade_reasons_are_explicit(case, kind, expected):
    runner, _, sessions, source = case
    source.signals = {sessions[0]: ()} if kind == "incomplete" else {}
    runner.run()
    assert events(runner.journal, "DECISION_NO_TRADE")[-1]["payload"]["reason"] == expected
    assert runner.journal.verify().last_session == sessions[0]


def test_partial_stale_cross_section_never_selects_fresh_candidate(case):
    runner, _, sessions, source = case
    source.signals[sessions[0]] = (
        record("AAA"),
        record("BBB").model_copy(update={"exclusion_reasons": ("stale_market_data",)}),
    )
    runner.run()
    assert not events(runner.journal, "ENTRY_INTENT_CREATED")
    assert (
        events(runner.journal, "DECISION_NO_TRADE")[0]["payload"]["reason"]
        == "NO_TRADE_DATA_INCOMPLETE"
    )


def test_source_revisions_do_not_rewrite_old_decisions(case):
    runner, db, sessions, source = case
    runner.run()
    original = events(runner.journal)
    db.upsert_bars([bar("AAA", sessions[0], close=100.5)])
    source.signals[sessions[0]] = (record("BBB", quality=99),)
    assert runner.run(sessions[0])["status"] == "ALREADY_PROCESSED"
    assert events(runner.journal) == original
    assert runner.journal.verify().state["pending"]["order"]["record"]["symbol"] == "AAA"


def test_crash_after_intent_rolls_back_and_restart_retries_once(case, monkeypatch):
    runner, _, sessions, _ = case
    original = runner.journal.append

    def crash(connection, kind, payload, **kwargs):
        value = original(connection, kind, payload, **kwargs)
        if kind == "ENTRY_INTENT_CREATED":
            raise SystemExit("simulated process abort before session commit")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(runner.journal, "append", crash)
        with pytest.raises(SystemExit):
            runner.run()
    assert runner.journal.verify().event_count == 1
    restarted = ShadowRunner(
        Journal(runner.journal.path),
        runner.market_path,
        runner.config,
        clock=runner.clock,
        screen_source=runner.screen_source,
    )
    restarted.run(sessions[0])
    restarted.run(sessions[1])
    restarted.run(sessions[1])
    assert len(events(restarted.journal, "ENTRY_INTENT_CREATED")) == 1
    assert len(events(restarted.journal, "ENTRY_EXECUTED")) == 1


def test_concurrent_runners_commit_one_session(case):
    runner, _, sessions, _ = case

    def attempt():
        other = ShadowRunner(
            Journal(runner.journal.path),
            runner.market_path,
            runner.config,
            clock=runner.clock,
            screen_source=runner.screen_source,
        )
        return other.run(sessions[0])["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(statuses) == ["ALREADY_PROCESSED", "SESSION_COMMITTED"]
    assert len(events(runner.journal, "SESSION_COMMITTED")) == 1


def test_source_snapshot_is_read_only_and_provenance_changes_with_inputs(case):
    runner, db, sessions, _ = case
    before = db.path.read_bytes()
    with LocalInputs(db.path).snapshot() as inputs:
        inputs.bars_available_as_of("AAA", sessions[0])
        initial = deepcopy(inputs.provenance)
        with inputs.connect() as connection, pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM bars")
    assert before == db.path.read_bytes()
    db.upsert_bars([bar("AAA", sessions[0], close=100.5)])
    with LocalInputs(db.path).snapshot() as inputs:
        inputs.bars_available_as_of("AAA", sessions[0])
        assert initial != inputs.provenance
    runner.run()
    screen = events(runner.journal, "SCREEN_EVALUATED")[0]["payload"]
    assert screen["input_set_digest"] == digest(screen["input_provenance"])


def test_market_journal_alias_refused(case):
    runner, db, _, _ = case
    with pytest.raises(ShadowError, match="SHADOW_DATABASE_MUST_BE_SEPARATE"):
        ShadowRunner(Journal(db.path), db.path, runner.config)


def test_default_production_screen_path_is_used_read_only(case, monkeypatch):
    from trading_system.strategy.screener import Screener

    runner, db, sessions, source = case
    runner.screen_source = None
    calls = []

    def screen(screener, session, **kwargs):
        calls.append((session, kwargs))
        assert isinstance(screener.database, LocalInputs)
        return source.screen(session)

    monkeypatch.setattr(Screener, "run", screen)
    runner.run()
    assert calls == [(sessions[0], {"use_market_snapshots": False})]
    assert runner.journal.verify().state["pending"]["order"]["record"]["symbol"] == "AAA"


def test_cli_all_commands_only_temporary_journal(case, tmp_path, capsys):
    runner, db, _, _ = case
    path = tmp_path / "cli-shadow.sqlite3"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        runner.config.model_copy(
            update={"storage": runner.config.storage.model_copy(update={"database_path": db.path})}
        ).model_dump_json(),
        encoding="utf-8",
    )
    prefix = ["--config", str(config_path)]
    assert (
        cli.main(prefix + ["shadow-f-init", "--journal", str(path), "--initial-capital", "150"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["initial_capital"] == 150
    assert cli.main(prefix + ["shadow-f-verify", "--journal", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"
    assert cli.main(prefix + ["shadow-f-status", "--journal", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["configuration_drift"] == "UNCHANGED"
    assert cli.main(prefix + ["shadow-f-run", "--journal", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "NO_COMPLETED_SESSION"
    assert (
        cli.main(prefix + ["shadow-f-run", "--journal", str(path), "--session", "2024-01-02"]) == 1
    )
    assert json.loads(capsys.readouterr().err)["status"] == "SHADOW_BEFORE_INCEPTION"
    assert cli.main(prefix + ["shadow-f-init", "--journal", str(path)]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "SHADOW_ALREADY_INITIALIZED"


def test_status_and_verify_never_create_missing_journal(tmp_path, config):
    journal = Journal(tmp_path / "missing.sqlite3")
    runner = ShadowRunner(journal, tmp_path / "market.sqlite3", config)
    with pytest.raises(ShadowError, match="SHADOW_NOT_INITIALIZED"):
        runner.status()
    assert not journal.path.exists()


def test_strategy_unchanged(case):
    runner, _, _, _ = case
    config_before = runner.config.model_dump()
    definition = deepcopy(FROZEN_CHAMPION_F)
    yaml_before = Path("config/strategy.yaml").read_bytes()
    runner.run()
    runner.run()
    assert runner.config.model_dump() == config_before
    assert definition == FROZEN_CHAMPION_F
    assert Path("config/strategy.yaml").read_bytes() == yaml_before
    assert events(runner.journal)[0]["previous_event_hash"] == GENESIS_HASH


def test_empty_local_production_screen_fails_closed_without_synchronizing(case):
    runner, db, _, _ = case
    before = db.path.read_bytes()
    runner.screen_source = None
    runner.run()
    assert runner.journal.verify().state["pending"] is None
    screen = events(runner.journal, "SCREEN_EVALUATED")[0]["payload"]
    assert screen["input_provenance"]["identity_inputs"]["tradable_companies"] == digest([])
    assert (
        events(runner.journal, "DECISION_NO_TRADE")[0]["payload"]["reason"]
        == "NO_TRADE_DATA_INCOMPLETE"
    )
    assert db.path.read_bytes() == before


def test_real_nonempty_screener_records_local_bar_fact_and_feature_provenance(tmp_path, config):
    from test_backtest_features import _database

    from trading_system.champion import screen_champion

    db, session = _database(tmp_path)
    expected = screen_champion(db, config, session)
    journal = Journal(tmp_path / "nonempty-shadow.sqlite3")
    runner = ShadowRunner(
        journal,
        db.path,
        config,
        clock=lambda: regular_session_bounds(session)[0] - timedelta(hours=1),
    )
    runner.initialize()
    runner.clock = lambda: regular_session_bounds(session)[1]
    before = db.path.read_bytes()
    runner.run()
    screen = events(journal, "SCREEN_EVALUATED")[0]["payload"]
    assert screen["eligible_candidate_count"] == expected.eligible_count
    assert screen["input_provenance"]["market_inputs"]
    assert screen["input_provenance"]["fundamental_inputs"]
    assert screen["input_provenance"]["relevant_feature_set_digest"]
    assert db.path.read_bytes() == before


def test_incomplete_signal_cross_section_still_manages_existing_position(case):
    runner, _, sessions, source = case
    runner.run()
    runner.run()
    before = runner.journal.verify().state
    source.signals[sessions[2]] = ()
    runner.run()
    after = runner.journal.verify().state
    assert after["cash"] == before["cash"]
    assert after["position"]["holding_days"] == before["position"]["holding_days"] + 1
    assert after["pending"] is None
    assert (
        events(runner.journal, "DECISION_NO_TRADE")[-1]["payload"]["reason"]
        == "NO_TRADE_DATA_INCOMPLETE"
    )


@pytest.mark.parametrize("role", ["PENDING_ENTRY", "HELD_POSITION"])
@pytest.mark.parametrize("suffix", ["STALE", "INVALID"])
def test_critical_stale_and_invalid_bars_use_cc01_errors(case, monkeypatch, role, suffix):
    runner, _, sessions, _ = case
    runner.run()
    if role == "HELD_POSITION":
        runner.run()
    original = LocalInputs.bars_on_session

    def bad_bar(inputs, symbols, session):
        found = original(inputs, symbols, session)
        found["AAA"] = bar("AAA", sessions[0] if suffix == "STALE" else session)
        if suffix == "INVALID":
            found["AAA"] = found["AAA"].model_copy(update={"volume": 0})
        return found

    monkeypatch.setattr(LocalInputs, "bars_on_session", bad_bar)
    before = runner.journal.verify().state
    with pytest.raises(DataIntegrityError, match=f"{role}_BAR_{suffix}"):
        runner.run()
    assert runner.journal.verify().state == before


def test_official_early_close_and_holiday_sequence(case):
    original, db, _, source = case
    journal = Journal(original.journal.path.with_name("early-close.sqlite3"))
    runner = ShadowRunner(
        journal,
        db.path,
        original.config,
        screen_source=source,
        clock=lambda: datetime(2024, 11, 27, 22, tzinfo=UTC),
    )
    runner.initialize()
    runner.clock = lambda: datetime(2024, 11, 29, 17, 59, tzinfo=UTC)
    assert runner.run()["status"] == "NO_COMPLETED_SESSION"
    runner.clock = lambda: datetime(2024, 11, 29, 18, tzinfo=UTC)
    assert runner.run()["session"] == date(2024, 11, 29)
    assert runner.status()["next_expected_session"] == date(2024, 12, 2)


def test_invalid_frozen_economics_and_champion_identity_refused(case, monkeypatch):
    from dataclasses import replace

    from trading_system.shadow import contracts

    runner = case[0]
    monkeypatch.setattr(contracts, "FROZEN_CHAMPION_F", replace(FROZEN_CHAMPION_F, label="OTHER"))
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()
    monkeypatch.setattr(contracts, "FROZEN_CHAMPION_F", FROZEN_CHAMPION_F)
    runner.config.risk.risk_per_trade = 0.02
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()


def test_cli_corrupt_journal_has_structured_integrity_error(case, capsys):
    runner = case[0]
    with sqlite3.connect(runner.journal.path) as connection:
        connection.execute("DROP TRIGGER events_no_delete")
    assert cli.main(["shadow-f-verify", "--journal", str(runner.journal.path)]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "SHADOW_JOURNAL_INTEGRITY_FAILURE"
