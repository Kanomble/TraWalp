"""Offline forward feed isolation, immutable evidence and CC01 regression."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_champion_consolidation import bar
from test_shadow_execution import Screens, ready_states

from trading_system.backtest.data_integrity import (
    DataIntegrityError,
    validate_session_position_data,
)
from trading_system.config import load_strategy_config
from trading_system.data.database import Database
from trading_system.data.forward_market import CONTRACT, persist, sync_forward
from trading_system.data.observations import current_session_observation
from trading_system.shadow.contracts import ShadowError, digest, fingerprints
from trading_system.shadow.inputs import LocalInputs
from trading_system.shadow.journal import Journal
from trading_system.shadow.runner import ShadowRunner

SESSION = date(2024, 1, 2)


def seed(db, bars, feed="sip", adjustment="all"):
    return persist(
        db,
        bars,
        provider="alpaca",
        feed=feed,
        adjustment=adjustment,
        sync_reference="offline-fixture",
        retrieved_at=datetime(2024, 1, 3, tzinfo=UTC),
    )


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    return db


def test_migration_feed_identity_no_relabelling(db):
    legacy = bar("AAA", SESSION)
    db.upsert_bars([legacy])
    seed(db, [legacy], "iex")
    seed(db, [legacy], "sip")
    seed(db, [legacy], "sip")
    assert db.bars_on_session(["AAA"], SESSION)["AAA"] == legacy
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM forward_market_observations").fetchone()[0] == 2
    with LocalInputs(db.path).snapshot() as inputs:
        assert inputs.bars_on_session(["AAA"], SESSION)["AAA"] == legacy
        evidence = inputs.provenance["market_inputs"][f"execution|{SESSION}|AAA"][0]
        assert evidence["feed"] == "sip"
        assert evidence["session"] == str(SESSION)
        assert evidence["sync_reference"] == "offline-fixture"
        assert evidence["close"] == str(legacy.close)
        assert evidence["source_timestamp"] == legacy.timestamp.isoformat()
        inputs.bars_on_session(["SPY"], SESSION)
        assert inputs.provenance["market_inputs"][f"execution|{SESSION}|AAA"][0] == evidence


def test_no_iex_fallback_or_session_mix(db):
    db.upsert_bars([bar("AAA", SESSION), bar("BBB", SESSION)])
    seed(db, [bar("AAA", SESSION)])
    seed(db, [bar("BBB", SESSION)], "iex")
    seed(db, [bar("BBB", SESSION)], "sip", "raw")
    with LocalInputs(db.path).snapshot() as inputs:
        assert set(inputs.bars_on_session(["AAA", "BBB"], SESSION)) == {"AAA"}
        assert inputs.bars_available_as_of("BBB", SESSION) == []
        with pytest.raises(sqlite3.OperationalError):
            inputs.upsert_bars([bar("AAA", SESSION)])


def test_corrupt_retrieval_provenance_is_not_usable(db):
    seed(db, [bar("AAA", SESSION)])
    with db.connect() as c:
        c.execute("UPDATE forward_market_observations SET retrieved_at='unknown'")
    with LocalInputs(db.path).snapshot() as inputs:
        assert inputs.bars_on_session(["AAA"], SESSION) == {}


@pytest.mark.parametrize("problem", ["missing", "stale", "zero"])
@pytest.mark.parametrize("role", ["held", "pending"])
def test_cc01_sip_critical_fail_closed(db, problem, role):
    db.upsert_bars([bar("AAA", SESSION)])
    if problem != "missing":
        value = bar("AAA", SESSION if problem == "zero" else SESSION - timedelta(days=1))
        seed(db, [value.model_copy(update={"volume": 0}) if problem == "zero" else value])
    with LocalInputs(db.path).snapshot() as inputs:
        bars = inputs.bars_on_session(["AAA"], SESSION)
        assert current_session_observation(bars.get("AAA"), SESSION) != "OBSERVATION_PRESENT"
        with pytest.raises(DataIntegrityError):
            validate_session_position_data(
                SESSION,
                bars=bars,
                held_positions={"AAA": date(2023, 12, 29)} if role == "held" else {},
                pending_entries={"AAA": date(2023, 12, 29)} if role == "pending" else {},
                previous_processed_session=date(2023, 12, 29),
                now=datetime(2024, 1, 3, tzinfo=UTC),
            )


def test_sync_requires_sip_and_explicit_window(db):
    with pytest.raises(ValueError, match="SIP_ALL_REQUIRED"):
        sync_forward(
            db,
            SimpleNamespace(
                feed=SimpleNamespace(value="iex"), adjustment=SimpleNamespace(value="all")
            ),
            SESSION,
            SESSION,
            ["AAA"],
        )
    calls = []
    client = SimpleNamespace(
        feed=SimpleNamespace(value="sip"),
        adjustment=SimpleNamespace(value="all"),
        daily_bars=lambda *args: calls.append(args) or [bar("AAA", SESSION)],
    )
    assert sync_forward(db, client, SESSION, SESSION, ["AAA"]) == 1
    assert calls[0][0] == ["AAA", "SPY"]
    assert calls[0][1].date() == SESSION
    assert calls[0][2].date() == SESSION + timedelta(days=1)
    assert db.bars_on_session(["AAA"], SESSION) == {}


def test_fingerprints_bind_contract_and_pipeline(monkeypatch):
    config = load_strategy_config()
    before = fingerprints(config)
    assert "data/forward_market.py" in before["data_pipeline_files"]
    assert "shadow/inputs.py" in before["data_pipeline_files"]
    assert "cli.py" in before["data_pipeline_files"]
    assert "champion.py" in before["data_pipeline_files"]
    assert "strategy/screener.py" in before["data_pipeline_files"]
    monkeypatch.setitem(CONTRACT, "adjustment", "raw")
    assert (
        fingerprints(config)["market_data_contract_fingerprint"]
        != before["market_data_contract_fingerprint"]
    )


def test_runner_boundary_manifest_and_journal_provenance(db, tmp_path):
    inception = date(2023, 12, 29)
    seed(db, [bar("SPY", inception), bar("SPY", SESSION)])
    ready_states(db, datetime(2023, 12, 29, 22, tzinfo=UTC))
    db.set_sync_value(
        "dataset",
        "forward_sip",
        {"status": "success", "last_success_at": "2023-12-29T22:00:00+00:00", "end": "2023-12-29"},
    )
    journal = Journal(tmp_path / "shadow.sqlite3")
    runner = ShadowRunner(
        journal,
        db.path,
        load_strategy_config(),
        screen_source=Screens(),
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )
    initialized = runner.initialize()
    assert initialized["forward_sip_transition_boundary"] == str(inception)
    ready_states(db, datetime(2024, 1, 2, 22, tzinfo=UTC))
    db.set_sync_value(
        "dataset",
        "forward_sip",
        {"status": "success", "last_success_at": "2024-01-02T22:00:00+00:00", "end": "2024-01-02"},
    )
    runner.clock = lambda: datetime(2024, 1, 3, tzinfo=UTC)
    runner.run(SESSION)
    assert journal.verify().last_session == SESSION
    with journal.connection() as c:
        payload = json.loads(
            c.execute("SELECT payload FROM events WHERE event_type='SCREEN_EVALUATED'").fetchone()[
                0
            ]
        )
        assert payload["primary_feed"] == "sip"
        assert payload["adjustment"] == "all"
        assert payload["market_data_contract_id"] == CONTRACT["market_data_contract_id"]
        assert "input_provenance" not in payload
        content = json.loads(
            c.execute(
                "SELECT content FROM input_manifests WHERE content_hash=?",
                (payload["input_manifest_hash"],),
            ).fetchone()[0]
        )
        assert digest(content) == payload["input_manifest_hash"]
    with journal.connection(write=True) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM input_manifests")


def test_inception_no_sip_refuses_without_journal(db, tmp_path):
    db.upsert_bars([bar("SPY", SESSION)])
    journal = Journal(tmp_path / "absent.sqlite3")
    runner = ShadowRunner(
        journal,
        db.path,
        load_strategy_config(),
        screen_source=Screens(),
        clock=lambda: datetime(2024, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(ShadowError, match="SHADOW_INCEPTION_DATA_NOT_READY"):
        runner.initialize()
    assert not journal.path.exists()


def test_champion_screen_uses_forward_adapter_and_pins_effective_session(db, tmp_path, monkeypatch):
    from trading_system.champion import screen_champion
    from trading_system.strategy.screener import Screener

    config = load_strategy_config()
    config.storage.reports_path = tmp_path / "reports"

    def screen(self, *args, **kwargs):
        assert isinstance(self.database, LocalInputs)
        assert kwargs["use_market_snapshots"] is False
        return Screens().screen(SESSION).model_copy(update={"effective_market_session": SESSION})

    monkeypatch.setattr(Screener, "run", screen)
    report = screen_champion(db, config, SESSION + timedelta(days=1))
    content = json.loads(Path(report.market_input_manifest_path).read_text())
    assert content["session"] == str(SESSION)
    assert content["feed"] == "sip"
    assert digest(content) == report.market_input_manifest_hash


def test_large_manifest_compression_preserves_content_identity():
    content = {"economic_observations": "source-value-" * 100_000}
    with sqlite3.connect(":memory:") as connection:
        content_hash = Journal.store_input_manifest(connection, content)
        stored = connection.execute("SELECT content FROM input_manifests").fetchone()[0]
        assert len(stored) < 100_000
        assert Journal.load_input_manifest(connection, content_hash) == content
        assert content_hash == digest(content)


def test_missing_declared_transition_blocks_before_screen(db, tmp_path):
    seed(db, [bar("SPY", SESSION)])
    db.set_sync_value("dataset", "forward_sip", {"status": "success"})
    runner = ShadowRunner(
        Journal(tmp_path / "absent.sqlite3"),
        db.path,
        load_strategy_config(),
        screen_source=Screens(),
        clock=lambda: datetime(2024, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(ShadowError) as error:
        runner.initialize()
    assert error.value.details["blocking_reasons"] == ["SIP_TRANSITION_BOUNDARY_NOT_DECLARED"]
    assert not runner.journal.path.exists()


def test_unresolved_forward_inputs_block_candidate_ranking(monkeypatch):
    from test_shadow_qualification import SESSION as qualified_session
    from test_shadow_qualification import candidate

    from trading_system.models.screening import ScreenReport
    from trading_system.shadow import qualification

    raw = ScreenReport(
        as_of=qualified_session,
        generated_at="fixture",
        analyzed_count=1,
        eligible_count=0,
        records=(candidate(observation="OBSERVATION_ZERO_VOLUME"),),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Candidate ranking before input qualification")

    monkeypatch.setattr(qualification, "champion_ranking", forbidden)
    ranked, result = qualification.qualified_forward_screen(
        raw, qualified_session, load_strategy_config()
    )
    assert result["unresolved_rank_relevant_count"] == 1
    assert ranked.eligible_count == 0
    assert all(not row.eligible and row.rank is None for row in ranked.records)


def test_fresh_identity_only_sec_partial_does_not_mask_ranking_block(db):
    from test_shadow_qualification import SESSION as session
    from test_shadow_qualification import candidate, report

    from trading_system.shadow.readiness import evaluate_shadow_input_readiness

    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    db.set_sync_value(
        "dataset",
        "sec",
        {
            "status": "partial",
            "errors": 0,
            "identity_conflicts": 1,
            "last_success_at": None,
            "last_completed_at": now.isoformat(),
        },
    )
    seed(db, [bar("SPY", session)])
    source = SimpleNamespace(
        screen=lambda day: report(candidate(observation="OBSERVATION_ZERO_VOLUME"))
    )
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs, load_strategy_config(), now=now, session=session, screen_source=source
        )
    assert not result.ready
    assert result.details["blocking_reasons"] == ["RANKING_UNRESOLVED"]
