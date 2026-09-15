"""Offline SPY manifest, required-window coverage, local CLI and isolation contracts."""

import csv
import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_market_intraday_momentum_v1 import (
    SESSION,
    TF,
    absence,
    bar,
    forbidden,
    native,
    seed_market,
)
from test_market_intraday_momentum_v1 import config as config
from test_market_intraday_momentum_v1 import offline as offline

from trading_system import cli
from trading_system.backtest import market_intraday_momentum_v1_data as data
from trading_system.backtest import market_intraday_momentum_v1_manifest as manifest
from trading_system.backtest import market_intraday_momentum_v1_research as research
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import candidate_requirements_from_report
from trading_system.models.market_data import BarTimeframe


def preflight(db, config, tmp_path, *, end=SESSION):
    return research.run_market_intraday_momentum_v1(
        db, config, SESSION, end, tmp_path, stem="pre", preflight=True
    )


def validate(db, config, tmp_path, path, *, end=SESSION, stem="val"):
    return research.run_market_intraday_momentum_v1(
        db, config, SESSION, end, tmp_path, stem=stem, candidate_manifest=path
    )


def test_preflight_no_signals_or_outcomes_and_sync_report_is_explicit(
    tmp_path, config, monkeypatch
):
    db = seed_market(tmp_path, complete=False)
    monkeypatch.setattr(research, "NativeEntrySessions", forbidden)
    monkeypatch.setattr(research, "simulate_momentum_session", forbidden)
    monkeypatch.setattr(research, "build_diagnostics", forbidden)
    summary, paths = preflight(db, config, tmp_path)
    assert summary["metrics"] == summary["predictive_diagnostics"] == summary["stability"] == {}
    assert summary["coverage"]["required_native_bars"] == 4
    assert summary["coverage"]["window_status_counts"] == {"LOCAL_MISSING_FETCHABLE": 2}
    assert summary["ready_for_local_validation"] is False
    payload = json.loads(paths["momentum_candidates"].read_text())
    assert payload["manifest_version"] == 1
    assert payload["candidate_count"] == 1
    assert payload["fingerprint"] == fingerprint(payload)
    candidate = payload["candidates"][0]
    assert len(candidate["morning"]) == len(candidate["final"]) == 2
    assert candidate["symbol"] == "SPY"
    assert payload["daily_history_required"] is False
    assert not {"direction", "net_return", "first_30m_open", "final_30m_raw_return"}.intersection(
        candidate
    )
    requirements = json.loads(paths["intraday_requirements"].read_text())
    parsed = candidate_requirements_from_report(
        requirements, start=SESSION, end=SESSION, timeframes=(TF,)
    )
    assert len(parsed) == 1 and parsed[0].symbol == "SPY"
    assert requirements["research_coverage_scope"] == "FIRST_TWO_AND_FINAL_TWO_NATIVE_BARS_ONLY"
    assert requirements["remediation_scope"] == "EXISTING_FULL_SESSION_GRID_BOUNDED_MISSING_SPAN"
    assert len(requirements["candidate_sessions"][0]["research_required_timestamps"]) == 4
    assert data.momentum_remediation_warmup(requirements, config, (TF,), False) == 0
    with pytest.raises(ValueError, match="Market momentum remediation"):
        data.momentum_remediation_warmup(requirements, config, (TF,), True)


def test_manifest_reuse_skips_discovery_after_local_fixture_addition(tmp_path, config, monkeypatch):
    end = date(2025, 5, 5)
    db = seed_market(tmp_path, complete=False)
    _, paths = preflight(db, config, tmp_path, end=end)
    path = paths["momentum_candidates"]
    before = path.read_bytes()
    db.upsert_bars(native() + native(end, first_close=98, final_close=196))
    reference, reference_paths = research.run_market_intraday_momentum_v1(
        db, config, SESSION, end, tmp_path, stem="ref", rediscover_candidates=True
    )
    monkeypatch.setattr(data, "discover_market_sessions", forbidden)
    monkeypatch.setattr(manifest, "discover_market_sessions", forbidden)
    result, reports = validate(db, config, tmp_path, path, end=end)
    assert result["candidate_source"] == "MANIFEST" and path.read_bytes() == before
    assert result["performance"]["candidate_discovery_seconds"] == 0
    assert result["performance"]["sqlite_query_count_simulation"] == 0
    assert result["metrics"] == reference["metrics"]
    assert result["metrics"]["executed_trades"] == 2
    assert result["metrics"]["long_signals"] == result["metrics"]["short_signals"] == 1
    for name, output in reports.items():
        if name != "summary":
            assert output.read_bytes() == reference_paths[name].read_bytes()
    with reports["sessions"].open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len({row["session"] for row in rows}) == 2
    assert {row["direction"] for row in rows} == {"LONG", "SHORT"}


@pytest.mark.parametrize(
    "index,status",
    [
        (0, "FIRST_30M_UNOBSERVABLE"),
        (1, "FIRST_30M_UNOBSERVABLE"),
        (2, "CLOSING_WINDOW_UNOBSERVABLE"),
        (3, "CLOSING_WINDOW_UNOBSERVABLE"),
    ],
)
def test_provider_absence_affects_only_its_required_window(tmp_path, config, index, status):
    db = seed_market(tmp_path)
    missing = native()[index].timestamp
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE timestamp=? AND timeframe='15m'", (missing.isoformat(),)
        )
    absence(db, (missing,))
    summary, paths = preflight(db, config, tmp_path)
    assert summary["ready_for_local_validation"] is True
    assert summary["coverage"]["window_status_counts"] == {
        "REQUIRED_PRESENT": 1,
        "PROVIDER_CONFIRMED_ABSENT": 1,
    }
    result, reports = validate(db, config, tmp_path, paths["momentum_candidates"])
    with reports["sessions"].open(newline="") as handle:
        event = next(csv.DictReader(handle))
    assert event["status"] == status and event["net_return"] == ""
    assert result["metrics"]["signals"] == int(index >= 2)
    if index >= 2:
        assert event["direction"] == "LONG"


@pytest.mark.parametrize("observed_status", [None, "PROVIDER_CHECK_FAILED"])
def test_unresolved_required_gaps_block_local_validation(tmp_path, config, observed_status):
    db = seed_market(tmp_path, complete=False)
    if observed_status:
        absence(db, tuple(row.timestamp for row in native()), status=observed_status)
    summary, paths = preflight(db, config, tmp_path)
    assert summary["ready_for_local_validation"] is False
    with pytest.raises(ValueError, match="MARKET_INTRADAY_MOMENTUM_DATA_UNAVAILABLE"):
        validate(db, config, tmp_path, paths["momentum_candidates"])
    assert not list(tmp_path.glob("val_*"))


def test_missing_or_invalid_unused_midday_bars_never_block(tmp_path, config):
    db = seed_market(tmp_path)
    # Only the required four bars existed. Even an invalid irrelevant row must not be loaded.
    db.upsert_bars([bar(10)])
    with db.connect() as connection:
        connection.execute(
            "UPDATE bars SET low=10000 WHERE timestamp=?", (bar(10).timestamp.isoformat(),)
        )
    summary, paths = preflight(db, config, tmp_path)
    assert summary["ready_for_local_validation"] is True
    assert summary["performance"]["native_bars_loaded"] == 4
    result, _ = validate(db, config, tmp_path, paths["momentum_candidates"])
    assert result["metrics"]["executed_trades"] == 1


@pytest.mark.parametrize(
    "field,value", [("asset_class", "CRYPTO"), ("asset_class", "UNKNOWN"), ("tradable", 0)]
)
def test_spy_metadata_drift_fails_closed(tmp_path, config, monkeypatch, field, value):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    with db.connect() as connection:
        connection.execute(f"UPDATE assets SET {field}=? WHERE symbol='SPY'", (value,))
    monkeypatch.setattr(data, "discover_market_sessions", forbidden)
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, paths["momentum_candidates"])


@pytest.mark.parametrize(
    "field,value", [("market_data_feed", "sip"), ("market_data_adjustment", "split")]
)
def test_feed_adjustment_drift_fails_closed(tmp_path, config, field, value):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    setattr(config.universe, field, value)
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, paths["momentum_candidates"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_window", "FIRST_HOUR"),
        ("closing_window", "LAST_HOUR"),
        ("direction_rule", "ALWAYS_LONG"),
        ("long_slippage_bps", 0),
        ("short_slippage_bps", 0),
        ("commission_bps", 1),
        ("borrow_fee_bps", 1),
        ("stop", "ATR"),
        ("symbol", "QQQ"),
    ],
)
def test_economic_semantics_cannot_be_bypassed_by_recomputed_checksum(
    tmp_path, config, monkeypatch, field, value
):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    path = paths["momentum_candidates"]
    payload = json.loads(path.read_text())
    payload["strategy_definition"][field] = value
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(data, "discover_market_sessions", forbidden)
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_version", 2),
        ("research_family", "research-orb-v1"),
        ("requested_start", "2025-05-01"),
        ("requested_end", "2025-05-05"),
        ("extended_hours", True),
        ("calendar", "OTHER"),
        ("timeframe", "5m"),
    ],
)
def test_manifest_contract_drift_fails_closed_even_after_rehash(tmp_path, config, field, value):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    path = paths["momentum_candidates"]
    payload = json.loads(path.read_text())
    payload[field] = value
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, path)


def test_calendar_source_change_fails_closed_without_discovery(tmp_path, config, monkeypatch):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    original = manifest.session_definition
    monkeypatch.setattr(
        manifest,
        "session_definition",
        lambda day: replace(original(day), closing=original(day).closing - timedelta(hours=1)),
    )
    monkeypatch.setattr(data, "discover_market_sessions", forbidden)
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, paths["momentum_candidates"])


@pytest.mark.parametrize("window", ["morning", "final"])
def test_saved_timestamp_tampering_fails_after_rehash(tmp_path, config, window):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    path = paths["momentum_candidates"]
    payload = json.loads(path.read_text())
    payload["candidates"][0][window][0] = bar(10).timestamp.isoformat()
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=manifest.MISMATCH):
        validate(db, config, tmp_path, path)


@pytest.mark.parametrize(
    "command", ["preflight-market-intraday-momentum-v1", "validate-market-intraday-momentum-v1"]
)
def test_cli_is_read_only_and_cannot_sync_or_request_provider(
    tmp_path, config, monkeypatch, command
):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path, settings.strategy.storage.reports_path = (
        db.path,
        tmp_path,
    )
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)
    original, connections = sqlite3.connect, []

    def read_only(path, *args, **kwargs):
        assert str(path).endswith("?mode=ro") and kwargs.get("uri") is True
        connections.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", read_only)
    arguments = [command, "--start", str(SESSION), "--end", str(SESSION), "--output-stem", "cli"]
    if command.startswith("validate"):
        arguments += ["--candidate-manifest", str(paths["momentum_candidates"])]
        monkeypatch.setattr(data, "discover_market_sessions", forbidden)
    assert cli.main(arguments) == 0 and connections
    result = json.loads((tmp_path / "cli_summary.json").read_text())
    assert result["status"] == "ACTIVE" and result["market_data_feed"] == "IEX"
    assert result["automatic_champion_selection"] is False
    assert result["execution_eligibility_germany"] == "NOT_EVALUATED"


def test_cli_source_selection_and_no_economic_overrides():
    arguments = [
        "validate-market-intraday-momentum-v1",
        "--start",
        str(SESSION),
        "--end",
        str(SESSION),
        "--output-stem",
        "synthetic",
    ]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(arguments)
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            arguments + ["--candidate-manifest", "a.json", "--rediscover-candidates"]
        )
    for option in ("--symbol", "--slippage-bps", "--direction", "--stop", "--timeframe"):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(arguments + ["--rediscover-candidates", option, "1"])
    assert cli._parser().parse_args(arguments + ["--rediscover-candidates"]).rediscover_candidates


def test_existing_outputs_cannot_be_rewritten(tmp_path, config):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    with pytest.raises(FileExistsError):
        preflight(db, config, tmp_path)
    assert before == {name: path.read_bytes() for name, path in paths.items()}


def test_sync_scope_excludes_sessions_with_only_unused_midday_gaps(tmp_path, config):
    end = date(2025, 5, 5)
    db = seed_market(tmp_path)  # first day's four bars present; second day absent
    _, paths = preflight(db, config, tmp_path, end=end)
    payload = json.loads(paths["intraday_requirements"].read_text())
    assert len(payload["candidate_sessions"]) == 2
    requirements = candidate_requirements_from_report(
        payload, start=SESSION, end=end, timeframes=(TF,)
    )
    assert len(requirements) == 1 and requirements[0].session == end
    db.upsert_bars(native(end))
    prepared = data.prepare_momentum_data(db, config, SESSION, end)
    payload = data.momentum_requirements_report(prepared, config)
    assert payload["required_sessions"] == []


def test_mocked_remediation_dispatch_is_spy_only_without_network(tmp_path, config, monkeypatch):
    db = seed_market(tmp_path, complete=False)
    _, paths = preflight(db, config, tmp_path)
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path = db.path
    settings.strategy.storage.reports_path = tmp_path
    settings.strategy.intraday.extended_hours = True
    settings.strategy.intraday.warmup_bars = 50
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    calls = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            **dict.fromkeys(
                (
                    "candidate_symbol_sessions_required",
                    "required_present_count",
                    "required_local_missing_count",
                    "provider_confirmed_absent_count",
                    "provider_check_failed_count",
                    "fetch_attempted_count",
                    "fetch_success_count",
                    "fetch_failed_count",
                ),
                0,
            ),
            "qualification_status": "READY",
        }

    monkeypatch.setattr(cli, "remediate_candidate_intraday_coverage", stub)
    monkeypatch.setattr(cli, "export_intraday_remediation_report", lambda *args, **kwargs: {})
    assert (
        cli.main(
            [
                "sync-intraday",
                "--start",
                str(SESSION),
                "--end",
                str(SESSION),
                "--timeframes",
                "15m",
                "--candidates-report",
                str(paths["intraday_requirements"]),
                "--candidate-gaps-only",
                "--output-stem",
                "mock_only",
            ]
        )
        == 0
    )
    assert len(calls) == 1 and calls[0][0][1][0].symbol == "SPY"
    assert calls[0][1]["warmup_bars"] == 0 and calls[0][1]["extended_hours"] is False
    payload = json.loads(paths["intraday_requirements"].read_text())
    payload["required_sessions"][0]["symbol"] = "QQQ"
    with pytest.raises(ValueError, match="Market momentum remediation"):
        data.momentum_remediation_warmup(payload, config, (TF,), False)


def test_non_native_timeframe_cannot_substitute_for_missing_native_bar(tmp_path, config):
    db = seed_market(tmp_path)
    first = native()[0]
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE timestamp=? AND timeframe='15m'", (first.timestamp.isoformat(),)
        )
    db.upsert_bars([first.model_copy(update={"timeframe": BarTimeframe.MINUTES_5})])
    summary, _ = preflight(db, config, tmp_path)
    assert summary["performance"]["native_bars_loaded"] == 3
    assert summary["coverage"]["by_window"]["MORNING_WINDOW"] == {"LOCAL_MISSING_FETCHABLE": 1}
    assert summary["ready_for_local_validation"] is False


def test_daily_ranking_settings_have_no_effect_on_spy_manifest(tmp_path, config):
    db = seed_market(tmp_path)
    _, paths = preflight(db, config, tmp_path)
    config.universe.min_price = 10000
    config.universe.min_avg_dollar_volume_20d = 1_000_000_000_000
    result, _ = validate(db, config, tmp_path, paths["momentum_candidates"])
    assert result["metrics"]["executed_trades"] == 1
