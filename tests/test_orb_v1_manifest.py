"""Local manifest integrity, native-only batching and immutable calendar cache regression."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date

import pytest
from test_orb_v1 import SESSION, TF, absence, bar, candidate, forbidden, native, seed_market
from test_orb_v1 import config as config
from test_orb_v1 import offline as offline

from trading_system import cli
from trading_system.backtest import native_session_grid, orb_v1, orb_v1_data, orb_v1_research
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.orb_v1_data import prepare_orb_data
from trading_system.backtest.orb_v1_manifest import fingerprint
from trading_system.backtest.orb_v1_research import run_orb_v1, simulate_prepared_orb
from trading_system.config import load_settings
from trading_system.data.database import Database


def preflight(database, config, tmp_path, *, end=SESSION, stem="pre"):
    return run_orb_v1(database, config, SESSION, end, tmp_path, stem=stem, preflight=True)


def test_pre_rejection_manifest_remains_reproducible(tmp_path, config, monkeypatch):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    path = paths["orb_candidates"]
    payload = json.loads(path.read_text())
    payload["strategy_definition"]["status"] = "ACTIVE"
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    summary, _ = validate(database, config, tmp_path, path)
    assert summary["status"] == "REJECTED"
    assert path.read_bytes() == before


def validate(database, config, tmp_path, path, *, start=SESSION, end=SESSION, stem="validation"):
    return run_orb_v1(database, config, start, end, tmp_path, stem=stem, candidate_manifest=path)


def test_preflight_exports_exact_top100_and_daily_evidence_without_native_spool(
    tmp_path, config, monkeypatch
):
    symbols = tuple(f"S{i:03}" for i in range(101))
    database = seed_market(tmp_path, symbols, complete_native=False)
    monkeypatch.setattr(orb_v1_research, "NativeEntrySessions", forbidden)
    monkeypatch.setattr(orb_v1_research, "simulate_orb_session", forbidden)
    summary, paths = preflight(database, config, tmp_path)
    assert set(paths) == {"summary", "coverage", "intraday_requirements", "orb_candidates"}
    manifest = json.loads(paths["orb_candidates"].read_text())
    assert paths["orb_candidates"].name == "pre_orb_candidates.json"
    assert manifest["candidate_count"] == 100
    assert manifest["manifest_version"] == 2
    assert manifest["universe_definition"]["security_scope"] == "ALPACA_TRADABLE_US_EQUITY"
    assert [row["symbol"] for row in manifest["candidates"]] == list(symbols[:100])
    assert [row["daily_universe_rank"] for row in manifest["candidates"]] == list(range(1, 101))
    assert set(manifest["candidates"][0]) == {
        "session",
        "symbol",
        "previous_session",
        "daily_universe_rank",
        "previous_close",
        "average_dollar_volume_20d",
    }
    assert manifest["candidates"][0]["previous_session"] == "2025-05-01"
    assert manifest["daily_qualification"] == summary["daily_qualification"]
    assert manifest["fingerprint"] == fingerprint(manifest)
    assert summary["candidate_count"] == 100
    assert summary["candidate_manifest_fingerprint"] == manifest["fingerprint"]
    assert "native_bars" not in json.dumps(manifest)


def test_manifest_reuses_candidates_after_native_data_added_without_daily_discovery(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path, ("AAA", "BBB"), complete_native=False)
    _, paths = preflight(database, config, tmp_path)
    # Local fixture insertion models completed remediation; no synchronizer/provider runs.
    database.upsert_bars(native("AAA"))
    absence(database, "BBB")
    reference, reference_paths = run_orb_v1(
        database,
        config,
        SESSION,
        SESSION,
        tmp_path,
        stem="rediscovered",
        rediscover_candidates=True,
    )
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    summary, output = validate(database, config, tmp_path, paths["orb_candidates"])
    for name in (
        "orb_events",
        "trades",
        "coverage",
        "monthly",
        "yearly",
        "chronological_subperiods",
        "symbol_concentration",
        "entry_time_buckets",
    ):
        assert output[name].read_bytes() == reference_paths[name].read_bytes()
    assert summary["metrics"] == reference["metrics"]
    assert summary["daily_qualification"] == reference["daily_qualification"]
    assert summary["universe_definition"] == reference["universe_definition"]
    assert summary["candidate_source"] == "MANIFEST"
    assert summary["candidate_manifest_path"] == str(paths["orb_candidates"])
    assert (
        summary["candidate_manifest_fingerprint"]
        == json.loads(paths["orb_candidates"].read_text())["fingerprint"]
    )
    assert summary["performance"]["candidate_discovery_seconds"] == 0
    assert summary["performance"]["daily_universe_discovery_seconds"] == 0
    # No wall-clock speed expectation; this is an actually measured stage.
    assert summary["performance"]["candidate_manifest_load_seconds"] > 0
    assert reference["candidate_source"] == "REDISCOVERED"
    assert reference["performance"]["candidate_manifest_load_seconds"] == 0
    assert summary["metrics"]["percentage_of_eligible_sessions_breaking_out"] == 50
    assert summary["metrics"]["percentage_of_observable_sessions_breaking_out"] == 100
    assert summary["survivorship_clean"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("research_family", "research-orb-v2"),
        ("research_id", "ORB-V2"),
        ("requested_start", "2025-05-01"),
        ("requested_end", "2025-05-05"),
        ("strategy_definition", {}),
        ("universe_definition", {}),
        ("market_data_feed", "SIP"),
        ("market_data_adjustment", "split"),
        ("extended_hours", True),
        ("manifest_version", 999),
        ("manifest_version", 1),
        ("source_fingerprint", "stale"),
        ("candidate_count", 100),
    ],
)
def test_incompatible_manifest_rejects_even_with_recomputed_integrity_hash(
    tmp_path, config, monkeypatch, field, value
):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    path = paths["orb_candidates"]
    payload = json.loads(path.read_text())
    payload[field] = value
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_price", 6),
        ("min_avg_dollar_volume_20d", 15_000_000),
        ("market_data_feed", "sip"),
        ("market_data_adjustment", "split"),
    ],
)
def test_changed_configuration_rejects_without_rediscovery(
    tmp_path, config, monkeypatch, field, value
):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    setattr(config.universe, field, value)
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, paths["orb_candidates"])


@pytest.mark.parametrize("start,end", [(date(2025, 5, 1), SESSION), (SESSION, date(2025, 5, 5))])
def test_requested_window_must_match_exactly(tmp_path, config, monkeypatch, start, end):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, paths["orb_candidates"], start=start, end=end)


@pytest.mark.parametrize(
    "tamper", ["price", "symbol", "rank", "fingerprint", "evidence", "invalid_json"]
)
def test_candidate_and_fingerprint_tampering_rejects(tmp_path, config, monkeypatch, tamper):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    path = paths["orb_candidates"]
    payload = json.loads(path.read_text())
    if tamper == "price":
        payload["candidates"][0]["previous_close"] = 999
    elif tamper == "symbol":
        payload["candidates"][0]["symbol"] = "CHANGED"
    elif tamper == "rank":
        payload["candidates"][0]["daily_universe_rank"] = 2
    elif tamper == "evidence":
        payload["daily_qualification"]["ready"] = False
    else:
        payload["fingerprint"] = "bad"
    path.write_text("{" if tamper == "invalid_json" else json.dumps(payload))
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, path)


@pytest.mark.parametrize(
    "mutation", ["daily_selected", "daily_unselected", "membership", "asset_class", "unknown_class"]
)
def test_local_selection_input_changes_invalidate_manifest(tmp_path, config, monkeypatch, mutation):
    database = seed_market(tmp_path, ("AAA", "BBB"))
    # BBB does not pass ADV; its history still matters to possible Top-100 selection.
    with database.connect() as connection:
        connection.execute("UPDATE bars SET volume=1 WHERE timeframe='1d' AND symbol='BBB'")
    _, paths = preflight(database, config, tmp_path)
    if mutation.startswith("daily"):
        symbol = "AAA" if mutation == "daily_selected" else "BBB"
        with database.connect() as connection:
            connection.execute(
                "UPDATE bars SET volume=volume+1 WHERE timeframe='1d' AND symbol=?", (symbol,)
            )
    elif mutation == "membership":
        with database.connect() as connection:
            connection.execute("UPDATE assets SET tradable=0 WHERE symbol='BBB'")
    else:
        with database.connect() as connection:
            connection.execute(
                "UPDATE assets SET asset_class=? WHERE symbol='BBB'",
                ("CRYPTO" if mutation == "asset_class" else "UNKNOWN",),
            )
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, paths["orb_candidates"])


def test_same_session_daily_and_irrelevant_config_do_not_invalidate_manifest(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE bars SET volume=volume+1 WHERE timeframe='1d' AND timestamp>=?", (str(SESSION),)
        )
    config.portfolio.max_positions = 9  # ORB is a signal study and never uses this parameter.
    config.intraday.extended_hours = True  # ORB has its own frozen regular-hours contract.
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    summary, _ = validate(database, config, tmp_path, paths["orb_candidates"])
    assert summary["candidate_source"] == "MANIFEST"


def test_sec_changes_do_not_invalidate_asset_manifest_or_trigger_rediscovery(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM companies")
    database.set_sync_value("sec_reference", "ticker_to_cik", {"AAA": "0000000999"})
    database.set_sync_value("sec_identity_conflicts", "AAA", {"status": "unresolved"})
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    summary, _ = validate(database, config, tmp_path, paths["orb_candidates"])
    assert summary["candidate_source"] == "MANIFEST"
    assert summary["candidate_count"] == 1


@pytest.mark.parametrize(
    "before,after", [("CRYPTO", "US_EQUITY"), ("UNKNOWN", "US_EQUITY"), ("CRYPTO", "UNKNOWN")]
)
def test_excluded_asset_class_drift_invalidates_manifest(
    tmp_path, config, monkeypatch, before, after
):
    database = seed_market(tmp_path, ("AAA", "BBB"))
    with database.connect() as connection:
        connection.execute("UPDATE assets SET asset_class=? WHERE symbol='BBB'", (before,))
    _, paths = preflight(database, config, tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE assets SET asset_class=? WHERE symbol='BBB'", (after,))
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    with pytest.raises(ValueError, match="ORB_CANDIDATE_MANIFEST_MISMATCH"):
        validate(database, config, tmp_path, paths["orb_candidates"])


@pytest.mark.parametrize(
    "arguments", [[], ["--candidate-manifest", "x", "--rediscover-candidates"]]
)
def test_cli_requires_exactly_one_source(arguments):
    with pytest.raises(SystemExit) as exc:
        cli._parser().parse_args(
            [
                "validate-orb-v1",
                "--start",
                str(SESSION),
                "--end",
                str(SESSION),
                "--output-stem",
                "test",
                *arguments,
            ]
        )
    assert exc.value.code == 2


def test_manifest_cli_dispatch_is_read_only_and_does_not_rediscover(tmp_path, config, monkeypatch):
    database = seed_market(tmp_path)
    _, paths = preflight(database, config, tmp_path)
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path = database.path
    settings.strategy.storage.reports_path = tmp_path
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    assert (
        cli.main(
            [
                "validate-orb-v1",
                "--start",
                str(SESSION),
                "--end",
                str(SESSION),
                "--candidate-manifest",
                str(paths["orb_candidates"]),
                "--output-stem",
                "cli",
            ]
        )
        == 0
    )


def test_native_only_loader_batches_are_indexed_and_do_not_read_daily(tmp_path):
    database = seed_market(tmp_path)
    requirements = [
        (f"S{i:03}", session) for i in range(101) for session in (SESSION, date(2025, 5, 5))
    ]
    requirements.append(("AAA", SESSION))
    statements = []
    plans = []

    class TracedDatabase(Database):
        @contextmanager
        def read_only(self):
            with super().read_only() as connection:

                def trace(sql):
                    if sql.lstrip().upper().startswith(("WITH", "SELECT")):
                        statements.append(sql)

                connection.set_trace_callback(trace)
                yield connection

    batches = list(TracedDatabase(database.path).iter_native_session_batches(requirements))
    assert len(batches) == len(statements) == 2
    assert [len(batch[0]) for batch in batches] == [200, 3]
    assert sum(batch[1] for batch in batches) == 26
    assert all("timeframe='15m'" in sql and "timeframe='1d'" not in sql for sql in statements)
    with database.read_only() as connection:
        for sql in statements:
            plans.extend(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql))
    # SQLite may choose the equivalent UNIQUE auto-index over the explicitly named index.
    assert any(
        "SEARCH bars USING INDEX" in plan
        and "symbol=? AND timeframe=? AND timestamp>? AND timestamp<?" in plan
        for plan in plans
    ), plans
    assert not any("SCAN bars" in plan for plan in plans)
    # The existing entry loader still supplies Daily previous closes for other families.
    _, previous, _, daily_count = next(database.iter_entry_coverage_batches([candidate().key]))
    assert previous["AAA", candidate().previous_session] == 100
    assert daily_count == 1


def test_multi_session_manifest_spool_and_simulation_boundaries(tmp_path, config, monkeypatch):
    database = seed_market(tmp_path, ("AAA", "BBB"))
    end = date(2025, 5, 5)
    database.upsert_bars(native("AAA", session=end) + native("BBB", session=end))
    _, paths = preflight(database, config, tmp_path, end=end)
    monkeypatch.setattr(orb_v1_data, "discover_orb_universe", forbidden)
    monkeypatch.setattr(Database, "iter_entry_coverage_batches", forbidden)
    with NativeEntrySessions(cache_limit=1) as spool:
        prepared = prepare_orb_data(
            database,
            config,
            SESSION,
            end,
            candidate_manifest=paths["orb_candidates"],
            native_sessions=spool,
        )
        assert prepared.performance["coverage_sql_queries"] == 2
        assert len(prepared.candidates) == 4
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        events = simulate_prepared_orb(prepared, spool)
        assert len(events) == 4
        assert spool.peak_cache_size == len(spool.cache) == 1


def test_expected_grid_cache_reused_across_coverage_validation_and_simulation(
    tmp_path, config, monkeypatch
):
    database = seed_market(tmp_path, ("AAA", "BBB"))
    original = native_session_grid._expected_timestamps
    calls = []

    def counted(session, timeframe, *, extended_hours):
        calls.append((session, timeframe, extended_hours))
        return original(session, timeframe, extended_hours=extended_hours)

    orb_v1.expected_native_timestamps.cache_clear()
    monkeypatch.setattr(native_session_grid, "_expected_timestamps", counted)
    with NativeEntrySessions() as spool:
        prepared = prepare_orb_data(database, config, SESSION, SESSION, native_sessions=spool)
        simulate_prepared_orb(prepared, spool)
    assert calls == [(SESSION, TF, False)]
    assert isinstance(orb_v1.expected_native_timestamps(SESSION, TF, False), tuple)
    # Cache identity includes timeframe and hours; it does not weaken native validation.
    orb_v1.expected_native_timestamps(SESSION, TF, True)
    assert calls[-1] == (SESSION, TF, True)
    orb_v1.expected_native_timestamps(SESSION, orb_v1.BarTimeframe.MINUTES_5, False)
    assert calls[-1] == (SESSION, orb_v1.BarTimeframe.MINUTES_5, False)
    for bad in ([bar(0), bar(0)], [bar(-1)], [bar(0).model_copy(update={"low": 200})]):
        with pytest.raises(ValueError):
            orb_v1.validate_native_session("AAA", SESSION, bad)


def test_observable_breakout_rate_is_null_when_all_selected_sessions_absent(tmp_path, config):
    database = seed_market(tmp_path, complete_native=False)
    absence(database)
    summary, _ = run_orb_v1(
        database, config, SESSION, SESSION, tmp_path, stem="absent", rediscover_candidates=True
    )
    assert summary["metrics"]["percentage_of_eligible_sessions_breaking_out"] == 0
    assert summary["metrics"]["percentage_of_observable_sessions_breaking_out"] is None
