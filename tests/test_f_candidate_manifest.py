"""Manifest compatibility, native requirement batching, and I0/I1 replay equivalence."""

import json
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest
import test_f_lifecycle_v2 as fixtures
from test_f_lifecycle_v2 import native_session, record, research_preparation

from trading_system.backtest import lifecycle_validation as validation
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.f_candidates import data_fingerprint, fingerprint, load_manifest
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe

config = fixtures.config
local_market = fixtures.local_market


def export_fixture(monkeypatch, local_market, config, tmp_path):
    _, database, sessions, preparation = research_preparation(monkeypatch, local_market)
    report, requirements = validation.build_f_intraday_entry_preflight(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
    )
    paths = validation.export_f_intraday_entry_preflight(
        report, requirements, tmp_path, stem="manifest"
    )
    requirements.close()
    return database, sessions, paths["intraday_candidates.json"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("research_family", "wrong"),
        ("requested_start", "2020-01-01"),
        ("requested_end", "2030-01-01"),
        ("strategy_variant", "C"),
        ("config_fingerprint", "bad"),
        ("runtime_fingerprint", "bad"),
        ("data_snapshot_fingerprint", "bad"),
        ("candidate_discovery_version", 999),
        ("candidate_count", 999),
        ("discovery_complete", False),
        ("candidate_fingerprint", "bad"),
        ("replay_sha256", "bad"),
        ("timeframes", ["5m"]),
        ("extended_hours", True),
        ("warmup_bars", 1),
        ("replay_file", "../outside.jsonl"),
    ],
)
def test_manifest_rejects_incompatible_or_corrupt_metadata(
    monkeypatch,
    local_market,
    config,
    tmp_path,
    field,
    value,
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_manifest(path, database, config, sessions[0], sessions[-1], sessions)


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_rank", 2),
        ("evaluation_score", 1.0),
        ("execution_session", "2024-01-04"),
        ("symbol", "WRONG"),
        ("timeframe", "5m"),
    ],
)
def test_manifest_checks_actual_replay_evaluation_not_just_metadata_hash(
    monkeypatch,
    local_market,
    config,
    tmp_path,
    field,
    value,
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    payload = json.loads(path.read_text())
    payload["candidate_sessions"][0][field] = value
    payload["required_sessions"] = payload["candidate_sessions"]
    payload["candidate_fingerprint"] = fingerprint(payload["candidate_sessions"])
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_manifest(path, database, config, sessions[0], sessions[-1], sessions)


def test_intraday_sync_allowed_but_daily_correction_invalidates(
    monkeypatch,
    local_market,
    config,
    tmp_path,
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    snapshot = data_fingerprint(database)
    database.upsert_bars(native_session(sessions[1], weak=False))
    assert data_fingerprint(database) == snapshot
    _, source = load_manifest(path, database, config, sessions[0], sessions[-1], sessions)
    for session in sessions[:-1]:
        source.screen(session)
        assert len(source.cache) == 1
    with database.connect() as connection:
        connection.execute("UPDATE bars SET close='101' WHERE symbol='AAA' AND timeframe='1d'")
    with pytest.raises(ValueError, match="data_snapshot_fingerprint"):
        load_manifest(path, database, config, sessions[0], sessions[-1], sessions)


def test_validation_reuses_manifest_without_preparing_or_discovering_again(
    monkeypatch,
    local_market,
    config,
    tmp_path,
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    database.upsert_bars(native_session(sessions[1], weak=False))
    reference = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], rediscover_candidates=True
    )

    def forbidden(*args, **kwargs):
        pytest.fail("validation repeated candidate discovery")

    monkeypatch.setattr(validation, "_prepare", forbidden)
    monkeypatch.setattr(validation, "iter_f_candidates", forbidden)
    monkeypatch.setattr(
        validation, "_qualify_daily_research", lambda *args: {"failure_reasons": []}
    )
    result = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], candidate_manifest=path
    )
    assert result.tables == reference.tables
    for key in result.results:
        assert result.results[key].model_dump(exclude={"generated_at"}) == reference.results[
            key
        ].model_dump(exclude={"generated_at"})
    assert "candidate_discovery_seconds" not in result.summary["performance"]
    assert (
        result.summary["intraday_qualification"]["performance"]["sqlite_query_count_coverage"] == 2
    )


def test_replay_preserves_open_position_scores_after_f_becomes_ineligible(
    monkeypatch,
    local_market,
    config,
    tmp_path,
):
    _, database, sessions, preparation = research_preparation(monkeypatch, local_market)

    class ChangingScreens:
        def screen(self, session):
            candidate = record().model_copy(update={"as_of": session})
            if session != sessions[0]:
                candidate = candidate.model_copy(
                    update={
                        "technical": candidate.technical.model_copy(update={"sma20_rising": False}),
                    }
                )
            return SimpleNamespace(as_of=session, records=(candidate,))

    preparation.screen_source = ChangingScreens()
    report, requirements = validation.build_f_intraday_entry_preflight(
        database, config, sessions[0], sessions[-1], preparation=preparation
    )
    paths = validation.export_f_intraday_entry_preflight(
        report, requirements, tmp_path, stem="scores"
    )
    _, source = load_manifest(
        paths["intraday_candidates.json"], database, config, sessions[0], sessions[-1], sessions
    )
    expected = BacktestEngine(database, config, screen_source=preparation.screen_source).run(
        sessions[0],
        sessions[-1],
        variant=fixtures.FROZEN_CHAMPION_F.variant,
    )
    actual = BacktestEngine(database, config, screen_source=source).run(
        sessions[0],
        sessions[-1],
        variant=fixtures.FROZEN_CHAMPION_F.variant,
    )
    assert expected.positions and len(expected.positions[0].score_history) > 1
    assert actual.model_dump(exclude={"generated_at"}) == expected.model_dump(
        exclude={"generated_at"}
    )
    requirements.close()


def test_coverage_batches_exact_requirements_query_count_and_previous_close(
    monkeypatch,
    local_market,
):
    database, sessions = local_market
    database.upsert_bars(native_session(sessions[1], weak=False))
    opening, closing = regular_session_bounds(sessions[1])
    expected_native = database.bars_between(
        ["AAA"], opening, closing, timeframe=BarTimeframe.MINUTES_15
    )
    expected_close = float(database.bars_available_as_of("AAA", sessions[0], limit=1)[-1].close)
    requirements = [(f"X{i:04}", sessions[0], sessions[1]) for i in range(400)]
    requirements.append(("AAA", sessions[0], sessions[1]))
    # A missing Daily session must not use its earlier close.
    requirements.append(("AAA", date(2023, 12, 31), date(2024, 1, 2)))
    queries = []
    original = database.read_only

    @contextmanager
    def traced():
        with original() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(database, "read_only", traced)

    def forbidden(*args, **kwargs):
        pytest.fail("per-candidate SQL")

    monkeypatch.setattr(database, "bars_between", forbidden)
    monkeypatch.setattr(database, "bars_available_as_of", forbidden)
    native, previous = {}, {}
    for bars, closes, _, _ in database.iter_entry_coverage_batches(requirements):
        native.update(bars)
        previous.update(closes)
        assert len(bars) <= 200
    assert native["AAA", sessions[1]] == expected_native
    assert previous["AAA", sessions[0]] == expected_close
    assert previous["AAA", date(2023, 12, 31)] is None
    assert sum(query.startswith("WITH requirements") for query in queries) == 6
    assert all("FROM bars WHERE" not in query for query in queries)
    with original() as connection:
        plans = [
            connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            for query in queries
            if query.startswith("WITH requirements")
        ]
    assert all(any("SEARCH bars USING INDEX" in row[3] for row in plan) for plan in plans)


def test_validation_missing_manifest_fails_before_discovery(local_market, config):
    database, sessions = local_market
    with pytest.raises(ValueError, match="candidate-manifest"):
        validation.run_f_intraday_entry(database, config, sessions[0], sessions[-1])
