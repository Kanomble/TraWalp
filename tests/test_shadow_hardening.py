"""Offline pre-inception input boundaries; all writes target temporary SQLite."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_champion_consolidation import bar, record
from test_champion_consolidation import offline as offline
from test_daily_history import DailyAlpaca, _bar
from test_shadow_execution import Screens, events, ready_states
from test_shadow_execution import case as case
from test_shadow_execution import config as config

from trading_system import cli
from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.champion import champion_ranking
from trading_system.config import PROJECT_ROOT, load_strategy_config, strategy_config_location
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.market_sessions import latest_completed_trading_session
from trading_system.data.observations import current_session_observation
from trading_system.data.sync import DataSynchronizer, _daily_range_verified
from trading_system.shadow import contracts
from trading_system.shadow.contracts import ShadowError, fingerprints
from trading_system.shadow.inputs import LocalInputs
from trading_system.shadow.journal import Journal
from trading_system.shadow.qualification import qualify_f_inputs
from trading_system.shadow.readiness import evaluate_shadow_input_readiness
from trading_system.shadow.runner import ShadowRunner
from trading_system.shadow.state import validate_state


@pytest.mark.parametrize("cwd", ["root", "parent", "arbitrary"])
def test_shared_paths_are_cwd_independent(tmp_path, monkeypatch, cwd):
    monkeypatch.chdir(
        {"root": PROJECT_ROOT, "parent": PROJECT_ROOT.parent, "arbitrary": tmp_path}[cwd]
    )
    config = load_strategy_config()
    assert strategy_config_location()[0] == PROJECT_ROOT / "config/strategy.yaml"
    assert config.storage.database_path == PROJECT_ROOT / "data/trading_system.sqlite3"
    assert (
        config.storage.database_path.with_name("shadow_f.sqlite3")
        == PROJECT_ROOT / "data/shadow_f.sqlite3"
    )
    assert config.storage.reports_path == PROJECT_ROOT / "reports"


def asset_row(symbol="AAA", exchange="NYSE"):
    return dict(
        symbol=symbol,
        name=symbol,
        exchange=exchange,
        status="active",
        tradable=True,
        fractionable=True,
        shortable=False,
        **{"class": "us_equity"},
    )


@pytest.mark.parametrize("exchange", ["NYSE", "NASDAQ", "TXSE", "XYZNEW"])
def test_raw_asset_exchange_preserved(exchange):
    client = AlpacaDataClient(
        "fixture",
        "fixture",
        historical_client=SimpleNamespace(),
        trading_client=SimpleNamespace(
            get_all_assets=lambda request: [asset_row(exchange=exchange)]
        ),
    )
    assert client.list_tradable_us_equities()[0].exchange == exchange
    assert client.last_asset_diagnostics["raw_assets_received"] == 1
    assert client.last_asset_diagnostics["unknown_exchange_values"] == (
        [exchange] if exchange in {"TXSE", "XYZNEW"} else []
    )


def test_real_sdk_transport_uses_raw_assets(monkeypatch):
    """Exercise installed SDK raw_data support, without a single HTTP call."""
    client = AlpacaDataClient("fixture", "fixture", historical_client=SimpleNamespace())
    assert client.trading._use_raw_data is True
    monkeypatch.setattr(client.trading, "get", lambda *args, **kwargs: [asset_row(exchange="TXSE")])
    assert client.list_tradable_us_equities()[0].exchange == "TXSE"


def test_malformed_asset_isolated_and_safety_retention_kept(tmp_path):
    rows = [asset_row("AAA"), asset_row("BBB", "TXSE"), {"symbol": "BROKEN"}]
    client = AlpacaDataClient(
        "fixture",
        "fixture",
        historical_client=SimpleNamespace(),
        trading_client=SimpleNamespace(get_all_assets=lambda request: rows),
    )
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    result = DataSynchronizer(db, client, None).sync_assets()
    assert result["assets_skipped_invalid"] == 1
    assert result["assets_parsed"] == 2
    assert result["tradable_assets_final"] == 2
    rows[:] = [asset_row("AAA"), {}, {}, {}]
    with pytest.raises(ValueError, match="PARSE_RETENTION"):
        client.list_tradable_us_equities()
    assert len(db.list_tradable_assets()) == 2
    rows[:] = [asset_row("AAA")]
    # Existing reconciliation safety is still active independently of parser retention.
    db.reconcile_assets(
        [
            *db.list_tradable_assets(),
            *[
                db.list_tradable_assets()[0].model_copy(update={"symbol": f"X{i}"})
                for i in range(10)
            ],
        ]
    )
    with pytest.raises(ValueError, match="snapshot|SNAPSHOT"):
        DataSynchronizer(db, client, None).sync_assets()


@pytest.mark.parametrize("problem", ["absent", "invalid", "exception", "spy"])
def test_daily_request_success_is_not_coverage(tmp_path, problem):
    db = Database(tmp_path / "daily.sqlite3")
    db.initialize()
    session = date(2024, 1, 2)
    provider = DailyAlpaca([_bar("AAA", session)])
    if problem == "invalid":
        provider.available.append(_bar("BBB", session).model_copy(update={"high": 1}))
    if problem == "exception":

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic provider failure")

        provider.daily_bars = fail
    sync = DataSynchronizer(db, provider, None)
    sync.sync_daily_history(["AAA", "BBB"], session, session, include_benchmark=problem == "spy")
    a = db.sync_value("daily_history_coverage", "AAA")
    b = db.sync_value("daily_history_coverage", "SPY" if problem == "spy" else "BBB")
    if problem == "exception":
        assert a is b is None
        return
    assert a["status"] == "VERIFIED_COMPLETE"
    assert b["request_status"] == "REQUEST_SUCCESS"
    assert b["status"] != "VERIFIED_COMPLETE"
    provider.available = [_bar(symbol, session) for symbol in ("AAA", "BBB", "SPY")]
    sync.sync_daily_history(["AAA", "BBB"], session, session, include_benchmark=True)
    assert all(
        db.sync_value("daily_history_coverage", symbol)["status"] == "VERIFIED_COMPLETE"
        for symbol in ("AAA", "BBB", "SPY")
    )


def test_old_request_only_coverage_is_not_a_verified_proof():
    start, end = datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC)
    assert not _daily_range_verified(
        dict(start=start.isoformat(), end_exclusive=end.isoformat(), feed="iex", adjustment="all"),
        start,
        end,
        "iex",
        "all",
    )


def qualified_row(**changes):
    row = record().model_copy(update={"current_market_observation": "OBSERVATION_PRESENT"})
    technical, scores = {}, {}
    for key, value in changes.items():
        if key in {"quality", "valuation"}:
            scores[key] = getattr(row.scores, key).model_copy(update={"score": value})
        elif key in type(row.technical).model_fields:
            technical[key] = value
        else:
            row = row.model_copy(update={key: value})
    return row.model_copy(
        update={
            "technical": row.technical.model_copy(update=technical),
            "scores": row.scores.model_copy(update=scores),
        }
    )


@pytest.mark.parametrize(
    "changes,incomplete",
    [
        ({"valuation": None, "price": 1, "exclusion_reasons": ("invalid_or_low_price",)}, False),
        ({"sma200": None, "quality": 30}, False),
        ({"quality": None}, True),
        ({"valuation": None}, True),
        ({"momentum126": None}, True),
        ({"atr14": None}, True),
        ({"current_market_observation": "OBSERVATION_ZERO_VOLUME"}, True),
        (
            {
                "current_market_observation": "OBSERVATION_ZERO_VOLUME",
                "exclusion_reasons": ("financial_excluded",),
            },
            False,
        ),
    ],
)
def test_causal_completeness(config, changes, incomplete):
    row = qualified_row(**changes)
    source = Screens({row.as_of: (row,)})
    result = qualify_f_inputs(source.screen(row.as_of), row.as_of, config)
    assert bool(result["unresolved_rank_relevant_count"]) is incomplete


def test_complete_input_f_ranking_and_entry_unchanged(config):
    session = date(2024, 1, 2)
    rows = (
        qualified_row(symbol="BBB"),
        qualified_row(symbol="AAA"),
        qualified_row(symbol="LOW", quality=20),
    )
    raw = Screens({session: rows}).screen(session)
    before = champion_ranking(raw, config)
    after = qualify_f_inputs(raw, session, config, ranked=before)
    assert after["unresolved_rank_relevant_count"] == 0
    assert [r.symbol for r in before.records if r.eligible] == ["AAA", "BBB"]
    assert before == champion_ranking(raw, config)
    assert [r.symbol for r in before.records if r.eligible] == sorted(
        [
            r.symbol
            for r in raw.records
            if evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config).eligible
        ]
    )


def test_irrelevant_gaps_allow_rank_one_but_missing_selected_atr_does_not(case):
    runner, _, sessions, source = case
    source.signals[sessions[0]] = (
        qualified_row(),
        qualified_row(symbol="BBB", quality=30, valuation=None, sma200=None),
    )
    runner.run()
    assert runner.journal.verify().state["pending"]["order"]["record"]["symbol"] == "AAA"


def test_selected_atr_unknown_does_not_promote_rank_two(case):
    runner, _, sessions, source = case
    source.signals[sessions[0]] = (
        qualified_row(atr14=None),
        qualified_row(symbol="BBB", quality=80),
    )
    runner.run()
    assert runner.journal.verify().state["pending"] is None
    assert (
        events(runner.journal, "DECISION_NO_TRADE")[-1]["payload"]["reason"]
        == "NO_TRADE_DATA_INCOMPLETE"
    )


@pytest.mark.parametrize(
    "state,status",
    [
        ("asset_universe", "failed"),
        ("daily_history", "running"),
        ("sec", "partial"),
        ("sec", "missing"),
    ],
)
def test_init_refuses_unready_dataset_without_file(case, tmp_path, state, status):
    old, db, _, source = case
    now = datetime(2024, 2, 1, tzinfo=UTC)
    db.upsert_bars([bar("SPY", latest_completed_trading_session(now))])
    db.set_sync_value("dataset", state, {"status": status, "last_success_at": now.isoformat()})
    runner = ShadowRunner(
        Journal(tmp_path / "refused.sqlite3"),
        db.path,
        old.config,
        clock=lambda: now,
        screen_source=source,
    )
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY"):
        runner.initialize()
    assert not runner.journal.path.exists()


def test_init_success_screens_once_and_persists_readiness(case, tmp_path):
    old, db, _, source = case
    now = old.clock()
    db.upsert_bars([bar("SPY", latest_completed_trading_session(now))])
    calls = []
    original = source.screen
    source.screen = lambda session: (calls.append(session), original(session))[1]
    runner = ShadowRunner(
        Journal(tmp_path / "ready.sqlite3"),
        db.path,
        old.config,
        clock=old.clock,
        screen_source=source,
    )
    runner.initialize(capital=123.45)
    assert calls == [latest_completed_trading_session(now)]
    inception = runner.journal.verify().inception
    assert inception["initial_capital"] == 123.45
    assert inception["input_readiness"]["unresolved_rank_relevant_count"] == 0
    assert inception["input_readiness"]["input_readiness_digest"]
    assert inception["data_pipeline_fingerprint"]
    assert all(
        inception["runtime"][name]
        for name in (
            "python",
            "alpaca-py",
            "requests",
            "pandas",
            "numpy",
            "pydantic",
            "PyYAML",
            "exchange-calendars",
        )
    )


def test_ingestion_code_and_provider_runtime_drift(case, monkeypatch):
    runner = case[0]
    before = fingerprints(runner.config)
    read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (
            read(path) + (b"\n# ingestion drift" if path.name == "alpaca_client.py" else b"")
        ),
    )
    after = fingerprints(runner.config)
    assert after["data_pipeline_fingerprint"] != before["data_pipeline_fingerprint"]
    assert after["engine_fingerprint"] == before["engine_fingerprint"]
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()
    monkeypatch.undo()
    manifest = contracts.runtime_manifest()
    monkeypatch.setattr(contracts, "runtime_manifest", lambda: {**manifest, "alpaca-py": "changed"})
    with pytest.raises(ShadowError, match="SHADOW_CONFIGURATION_DRIFT"):
        runner.run()


def test_current_observation_zero_volume_keeps_storage_but_is_unusable(tmp_path):
    db = Database(tmp_path / "zero.sqlite3")
    db.initialize()
    session = date(2024, 1, 2)
    zero = bar("AAA", session).model_copy(update={"volume": 0})
    db.upsert_bars([zero])
    assert db.bar_count() == 1
    assert current_session_observation(zero, session) == "OBSERVATION_ZERO_VOLUME"
    assert (
        current_session_observation(zero, session + timedelta(days=1))
        == "OBSERVATION_WRONG_SESSION"
    )


def test_cli_init_cannot_bypass_readiness_and_paths(tmp_path, case, capsys):
    runner, db, _, _ = case
    db.set_sync_value("dataset", "asset_universe", {"status": "failed"})
    config_path = tmp_path / "strategy.json"
    config_path.write_text(
        runner.config.model_copy(
            update={"storage": runner.config.storage.model_copy(update={"database_path": db.path})}
        ).model_dump_json(),
        encoding="utf-8",
    )
    journal = tmp_path / "never-created.sqlite3"
    assert cli.main(["--config", str(config_path), "shadow-f-init", "--journal", str(journal)]) == 1
    assert "SHADOW_INCEPTION_DATA_NOT_READY" in capsys.readouterr().err
    assert not journal.exists()


def test_shadow_local_configuration_constructs_no_provider(case, monkeypatch, tmp_path, capsys):
    from trading_system.data.sec_client import SecClient

    def forbidden(*args, **kwargs):
        raise AssertionError("Shadow must not construct providers or synchronizers")

    monkeypatch.setattr(AlpacaDataClient, "__init__", forbidden)
    monkeypatch.setattr(SecClient, "__init__", forbidden)
    monkeypatch.setattr(DataSynchronizer, "__init__", forbidden)
    journal = tmp_path / "uninitialized.sqlite3"
    assert cli.main(["shadow-f-verify", "--journal", str(journal)]) == 1
    assert "SHADOW_NOT_INITIALIZED" in capsys.readouterr().err
    assert not journal.exists()


def test_stale_quality_cannot_prove_current_rejection(config):
    session = date(2024, 1, 2)
    row = qualified_row(
        quality=20, as_of=date(2023, 12, 29), exclusion_reasons=("stale_market_data",)
    )
    raw = Screens({session: (row,)}).screen(session)
    # Screens intentionally normalizes synthetic dates; restore the stale source evidence.
    raw = raw.model_copy(update={"records": (row,)})
    assert qualify_f_inputs(raw, session, config)["unresolved_rank_relevant_count"] == 1


def test_current_rejection_does_not_require_all_downstream_fields(config):
    row = qualified_row(
        quality=20,
        current_market_observation="OBSERVATION_ZERO_VOLUME",
        valuation=None,
        sma200=None,
    )
    raw = Screens({row.as_of: (row,)}).screen(row.as_of)
    assert qualify_f_inputs(raw, row.as_of, config)["unresolved_rank_relevant_count"] == 0


def test_pure_config_does_not_load_environment(monkeypatch):
    import trading_system.config as config_module

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure strategy paths cannot load .env")

    monkeypatch.setattr(config_module, "load_dotenv", forbidden)
    assert load_strategy_config().storage.database_path.is_absolute()


def test_newest_daily_workflow_and_explained_sec_partial(case):
    runner, db, sessions, source = case
    now = runner.clock()
    db.set_sync_value(
        "dataset",
        "sec",
        {
            "status": "partial",
            "identity_conflicts": 3,
            "errors": 0,
            "last_success_at": None,
            "last_completed_at": now.isoformat(),
        },
    )
    # A newer normal Daily update takes precedence over an obsolete range job.
    db.set_sync_value(
        "dataset",
        "daily_history",
        {"status": "failed", "last_started_at": "2023-01-01T00:00:00+00:00"},
    )
    db.set_sync_value(
        "dataset",
        "historical_bars",
        {
            "status": "success",
            "last_started_at": now.isoformat(),
            "last_success_at": now.isoformat(),
            "feed": "iex",
            "adjustment": "all",
        },
    )
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs, runner.config, now=now, session=sessions[0], screen_source=source
        )
    assert result.ready
    assert "SEC_IDENTITY_CONFLICTS_DETERMINISTICALLY_EXCLUDED" in result.details["warnings"]
    assert result.details["daily_state"]["status"] == "success"


def test_sync_completion_clears_previous_error_and_exposes_technical_completion(tmp_path):
    db = Database(tmp_path / "states.sqlite3")
    db.initialize()
    db.set_sync_value("dataset", "sec", {"status": "failed", "error": "old failure"})
    sync = DataSynchronizer(db, None, None)
    sync._run_stage("sec", lambda: {"identity_conflicts": 1, "errors": 0})
    state = db.sync_value("dataset", "sec")
    assert state["status"] == "partial"
    assert state["last_completed_at"]
    assert "error" not in state


def test_provider_omission_cannot_certify_preexisting_bars_under_new_feed(tmp_path):
    db = Database(tmp_path / "feed.sqlite3")
    db.initialize()
    session = date(2024, 1, 2)
    db.upsert_bars([_bar("AAA", session), _bar("BBB", session)])
    provider = DailyAlpaca([_bar("AAA", session)])
    provider.feed = "sip"
    sync = DataSynchronizer(db, provider, None)
    sync.sync_daily_history(["AAA", "BBB"], session, session, include_benchmark=False)
    assert db.sync_value("daily_history_coverage", "AAA")["status"] == "VERIFIED_COMPLETE"
    omitted = db.sync_value("daily_history_coverage", "BBB")
    assert omitted["status"] == "OBSERVATION_ABSENT"
    assert omitted["request_missing_observations"] == 1
    provider.available.append(_bar("BBB", session))
    sync.sync_daily_history(["AAA", "BBB"], session, session, include_benchmark=False)
    assert db.sync_value("daily_history_coverage", "BBB")["status"] == "VERIFIED_COMPLETE"


def test_local_reverification_is_not_labeled_a_new_request(tmp_path):
    db = Database(tmp_path / "local.sqlite3")
    db.initialize()
    session = date(2024, 1, 2)
    provider = DailyAlpaca([_bar("AAA", session)])
    sync = DataSynchronizer(db, provider, None, daily_overlap_bars=0)
    sync.sync_daily_history(["AAA"], session, session, include_benchmark=False)
    provider.calls.clear()
    sync.sync_daily_history(["AAA"], session, session, include_benchmark=False)
    assert not provider.calls
    assert (
        db.sync_value("daily_history_coverage", "AAA")["request_status"]
        == "NOT_REQUESTED_LOCAL_VERIFICATION"
    )


def test_v1_pending_record_remains_verifiable_without_new_observation_field(case):
    from copy import deepcopy

    runner = case[0]
    runner.run()
    old_state = deepcopy(runner.journal.verify().state)
    old_state["pending"]["order"]["record"].pop("current_market_observation")
    before = deepcopy(old_state)
    validate_state(old_state, 100.0)
    assert old_state == before


def test_init_crossing_official_close_refuses_without_second_screen(case, tmp_path):
    old, db, _, source = case
    start = datetime(2024, 2, 1, 20, 59, tzinfo=UTC)
    closed = datetime(2024, 2, 1, 21, tzinfo=UTC)
    db.upsert_bars([bar("SPY", date(2024, 1, 31))])
    ready_states(db, start)
    times = iter([start, closed])
    runner = ShadowRunner(
        Journal(tmp_path / "close-race.sqlite3"),
        db.path,
        old.config,
        clock=lambda: next(times),
        screen_source=source,
    )
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_SESSION_CHANGED_RETRY"):
        runner.initialize()
    assert not runner.journal.path.exists()
