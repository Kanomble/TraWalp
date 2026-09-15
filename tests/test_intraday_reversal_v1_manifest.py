"""Offline reversal manifest, local CLI and remediation-contract regressions."""

import csv
import json
import sqlite3

import pytest
from test_intraday_reversal_v1 import SESSION, TF, absence, forbidden, native, seed_market
from test_intraday_reversal_v1 import config as config
from test_intraday_reversal_v1 import offline as offline

from trading_system import cli
from trading_system.backtest import intraday_reversal_v1_data as data
from trading_system.backtest import intraday_reversal_v1_research as research
from trading_system.backtest.intraday_reversal_v1_manifest import MISMATCH
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import candidate_requirements_from_report


def preflight(db, config, tmp_path):
    return research.run_intraday_reversal_v1(
        db, config, SESSION, SESSION, tmp_path, stem="pre", preflight=True
    )


def validate(db, config, tmp_path, path, *, stem="val"):
    return research.run_intraday_reversal_v1(
        db, config, SESSION, SESSION, tmp_path, stem=stem, candidate_manifest=path
    )


def test_preflight_freezes_candidates_without_outcomes_and_exports_sync_contract(
    tmp_path, config, monkeypatch
):
    db = seed_market(tmp_path, complete_native=False)
    monkeypatch.setattr(research, "NativeEntrySessions", forbidden)
    monkeypatch.setattr(research, "rank_first_hour", forbidden)
    monkeypatch.setattr(research, "execute_signal", forbidden)
    summary, paths = preflight(db, config, tmp_path)
    assert summary["metrics"] == {} and summary["stability"] == {}
    assert summary["ready_for_local_validation"] is False
    assert summary["coverage"]["status_counts"]["LOCAL_MISSING_FETCHABLE"] == 100
    assert set(paths) == {"summary", "coverage", "reversal_candidates", "intraday_requirements"}
    manifest = json.loads(paths["reversal_candidates"].read_text())
    assert manifest["manifest_version"] == 1 and manifest["candidate_count"] == 100
    assert manifest["research_family"] == "research-intraday-reversal-v1"
    assert manifest["fingerprint"] == fingerprint(manifest)
    assert manifest["strategy_definition"]["minimum_cross_section"] == 80
    assert manifest["strategy_definition"]["slippage_bps"] == 5
    assert manifest["universe_definition"]["name"] == "REVERSAL_LIQUID_TOP100_US_EQUITY"
    payload = json.loads(paths["intraday_requirements"].read_text())
    requirements = candidate_requirements_from_report(
        payload, start=SESSION, end=SESSION, timeframes=(TF,)
    )
    assert len(requirements) == 100
    assert requirements[0].symbol == "S000"
    assert data.reversal_remediation_warmup(payload, config, (TF,), False) == 0
    with pytest.raises(ValueError, match="Reversal remediation"):
        data.reversal_remediation_warmup(payload, config, (TF,), True)
    config.universe.market_data_feed = "sip"
    with pytest.raises(ValueError, match="Reversal remediation"):
        data.reversal_remediation_warmup(payload, config, (TF,), False)


def test_manifest_reuse_skips_rediscovery_after_native_fixture_remediation(
    tmp_path, config, monkeypatch
):
    db = seed_market(tmp_path, complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    frozen = paths["reversal_candidates"].read_bytes()
    db.upsert_bars([bar for i in range(100) for bar in native(f"S{i:03}", first_close=90 + i / 10)])
    reference, reference_paths = research.run_intraday_reversal_v1(
        db, config, SESSION, SESSION, tmp_path, stem="reference", rediscover_candidates=True
    )
    monkeypatch.setattr(data, "discover_reversal_universe", forbidden)
    summary, reports = validate(db, config, tmp_path, paths["reversal_candidates"])
    assert paths["reversal_candidates"].read_bytes() == frozen
    assert summary["metrics"] == reference["metrics"]
    assert summary["candidate_source"] == "MANIFEST"
    assert summary["performance"]["candidate_discovery_seconds"] == 0
    assert summary["performance"]["sqlite_query_count_reversal_simulation"] == 0
    assert summary["metrics"]["executed_trades"] == 10
    for name, path in reports.items():
        if name != "summary":
            assert path.read_bytes() == reference_paths[name].read_bytes()
    with reports["trades"].open(newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert len(trades) == 10 and {row["exit_reason"] for row in trades} == {"SESSION_CLOSE"}
    assert not any(
        word in json.dumps(summary).lower() for word in ("cagr", "sharpe", "equity_curve")
    )


@pytest.mark.parametrize("mutation", ["class", "tradable", "daily_selected", "daily_unselected"])
def test_source_drift_fails_closed_without_rediscovery(tmp_path, config, monkeypatch, mutation):
    db = seed_market(tmp_path, tuple(f"S{i:03}" for i in range(101)), complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    with db.connect() as connection:
        if mutation == "class":
            connection.execute("UPDATE assets SET asset_class='CRYPTO' WHERE symbol='S000'")
        elif mutation == "tradable":
            connection.execute("UPDATE assets SET tradable=0 WHERE symbol='S000'")
        else:
            symbol = "S000" if mutation == "daily_selected" else "S100"
            connection.execute(
                "UPDATE bars SET volume=volume+1 WHERE symbol=? AND timeframe='1d'", (symbol,)
            )
    monkeypatch.setattr(data, "discover_reversal_universe", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        validate(db, config, tmp_path, paths["reversal_candidates"])
    assert not list(tmp_path.glob("val_*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_price", 6),
        ("min_avg_dollar_volume_20d", 11_000_000),
        ("market_data_feed", "sip"),
        ("market_data_adjustment", "split"),
    ],
)
def test_config_drift_fails_closed(tmp_path, config, monkeypatch, field, value):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    setattr(config.universe, field, value)
    monkeypatch.setattr(data, "discover_reversal_universe", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        validate(db, config, tmp_path, paths["reversal_candidates"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_version", 0),
        ("research_family", "research-orb-v1"),
        ("research_id", "ORB-V1-15M-LONG"),
        ("requested_start", "2025-05-01"),
        ("requested_end", "2025-05-05"),
        ("extended_hours", True),
        ("source_fingerprint", "changed"),
        ("universe_definition", {}),
    ],
)
def test_manifest_contract_drift_even_with_rehashed_payload(
    tmp_path, config, monkeypatch, field, value
):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    path = paths["reversal_candidates"]
    payload = json.loads(path.read_text())
    payload[field] = value
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(data, "discover_reversal_universe", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        validate(db, config, tmp_path, path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("slippage_bps", 0),
        ("signal_percent", 20),
        ("minimum_cross_section", 79),
        ("first_hour_bars", 3),
        ("exits", ["STOP", "SESSION_CLOSE"]),
        ("status", "REJECTED"),
    ],
)
def test_strategy_contract_has_no_hidden_variants(tmp_path, config, field, value):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    path = paths["reversal_candidates"]
    payload = json.loads(path.read_text())
    payload["strategy_definition"][field] = value
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=MISMATCH):
        validate(db, config, tmp_path, path)


@pytest.mark.parametrize(
    "status", ["LOCAL_MISSING_FETCHABLE", "PROVIDER_CHECK_FAILED", "PROVIDER_CONFIRMED_ABSENT"]
)
def test_strict_coverage_blocks_unresolved_data_and_preserves_absence(tmp_path, config, status):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
    if status != "LOCAL_MISSING_FETCHABLE":
        absence(db, "S000", status=status)
    summary, paths = preflight(db, config, tmp_path)
    assert summary["coverage"]["status_counts"][status] == 1
    if status == "PROVIDER_CONFIRMED_ABSENT":
        result, _ = validate(db, config, tmp_path, paths["reversal_candidates"])
        assert result["metrics"]["executed_trades"] == 0
        assert result["metrics"]["unobservable_cross_section_sessions"] == 1
    else:
        with pytest.raises(ValueError, match="REVERSAL_DATA_UNAVAILABLE"):
            validate(db, config, tmp_path, paths["reversal_candidates"])


@pytest.mark.parametrize(
    "command", ["preflight-intraday-reversal-v1", "validate-intraday-reversal-v1"]
)
def test_cli_uses_only_read_only_database_and_no_provider(tmp_path, config, monkeypatch, command):
    db = seed_market(tmp_path)
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage.database_path = db.path
    settings.strategy.storage.reports_path = tmp_path
    _, paths = preflight(db, config, tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda *_: settings)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)
    original, connections = sqlite3.connect, []

    def checked(path, *args, **kwargs):
        assert str(path).endswith("?mode=ro") and kwargs.get("uri") is True
        connections.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked)
    arguments = [command, "--start", str(SESSION), "--end", str(SESSION), "--output-stem", "cli"]
    if command.startswith("validate"):
        arguments += ["--candidate-manifest", str(paths["reversal_candidates"])]
        monkeypatch.setattr(data, "discover_reversal_universe", forbidden)
    assert cli.main(arguments) == 0
    assert connections
    summary = json.loads((tmp_path / "cli_summary.json").read_text())
    assert summary["status"] == "ACTIVE"
    assert summary["automatic_champion_selection"] is False
    assert summary["universe_definition"]["execution_eligibility_germany"] == "NOT_EVALUATED"


def test_validation_requires_explicit_source_and_exposes_no_economic_options():
    base = [
        "validate-intraday-reversal-v1",
        "--start",
        str(SESSION),
        "--end",
        str(SESSION),
        "--output-stem",
        "synthetic",
    ]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(base)
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            base + ["--candidate-manifest", "a.json", "--rediscover-candidates"]
        )
    for option in ("--slippage-bps", "--stop", "--top-n", "--signal-percent"):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(base + ["--rediscover-candidates", option, "1"])
    assert cli._parser().parse_args(base + ["--rediscover-candidates"]).rediscover_candidates


def test_existing_reports_are_never_overwritten(tmp_path, config):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
    _, paths = preflight(db, config, tmp_path)
    before = {name: path.read_bytes() for name, path in paths.items()}
    with pytest.raises(FileExistsError):
        preflight(db, config, tmp_path)
    assert before == {name: path.read_bytes() for name, path in paths.items()}


def test_preflight_first_hour_coverage_is_independent_of_later_fetchable_gaps(tmp_path, config):
    db = seed_market(tmp_path, ("S000",))
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE timeframe='15m' AND timestamp=?",
            (native()[5].timestamp.isoformat(),),
        )
    summary, _ = preflight(db, config, tmp_path)
    assert summary["coverage"]["observable_first_hour_symbol_sessions"] == 1
    assert summary["coverage"]["status_counts"]["LOCAL_MISSING_FETCHABLE"] == 1
    assert summary["ready_for_local_validation"] is False


def test_mocked_remediation_dispatch_obeys_reversal_contract(tmp_path, config, monkeypatch):
    db = seed_market(tmp_path, ("S000",), complete_native=False)
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

    # Test only dispatch: no real remediation, synchronizer or provider can run.
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
                "mocked",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    assert len(calls[0][0][1]) == 1
    assert calls[0][1]["warmup_bars"] == 0 and calls[0][1]["extended_hours"] is False
