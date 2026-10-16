"""Actual local reference integration using only synthetic metadata and prices."""

import json
from pathlib import Path

import pytest
import yaml
from test_massive_reference import client_factory as client_factory
from test_massive_reference import clock as clock
from test_massive_reference import offline as offline
from test_massive_reference import response, ticker
from test_pairs_stat_arb_v1 import END, START, forbidden, seed

from trading_system import cli
from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_security as security
from trading_system.backtest.pairs_stat_arb_v1_data import company_universe, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import MISMATCH
from trading_system.backtest.pairs_stat_arb_v1_research import run_pairs_stat_arb_v1
from trading_system.config import StrategyConfig
from trading_system.data.security_reference import sync_security_reference


@pytest.fixture
def config():
    # No dotenv/environment read in provider integration tests.
    path = Path(__file__).resolve().parents[1] / "config" / "strategy.yaml"
    return StrategyConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def populate(db, client_factory, records=None):
    records = records if records is not None else [ticker("A", cik="1"), ticker("B", cik="2")]
    client, calls = client_factory([response(), response(results=records)])
    sync_security_reference(db, client)
    return calls


def test_asset_sec_metadata_without_reference_stays_blocking_before_price_reads(
    tmp_path, config, monkeypatch
):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(data, "load_daily", forbidden)
    monkeypatch.setattr(data, "load_history_anchors", forbidden)
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    monkeypatch.setattr(engine, "observe", forbidden)
    assert company_universe(db) == []
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="absent", preflight=True
    )
    assert summary["company_security_type_status"] == security.UNAVAILABLE
    assert not summary["ready_for_local_validation"]
    assert not summary["selection_membership_resolved"]
    assert summary["pair_session_candidates"] == 0 and summary["network_used"] is False
    assert summary["metadata_sync_requirement"].startswith("EXPLICIT_SYNC_SECURITY_REFERENCE")
    requirements = json.loads(paths["daily_requirements"].read_text())
    assert requirements["required_sessions"] == []
    assert {r["status"] for r in requirements["company_security_type_requirements"]} == {
        "SECURITY_TYPE_REFERENCE_MISSING"
    }
    for manifest in (None, paths["pair_candidates"]):
        with pytest.raises(ValueError, match=security.UNAVAILABLE):
            prepare_pairs_data(db, config, START, END, candidate_manifest=manifest)


@pytest.mark.parametrize(
    "code",
    [
        "ETF",
        "ETS",
        "ETP",
        "ETV",
        "SP",
        "ETN",
        "FUND",
        "PFD",
        "ADRC",
        "WARRANT",
        "UNIT",
        "RIGHT",
        "BOND",
        "OTHER",
    ],
)
def test_known_non_common_types_are_excluded_without_blocking(tmp_path, client_factory, code):
    db, *_ = seed(tmp_path)
    populate(db, client_factory, [ticker("A", code), ticker("B", cik="2")])
    eligible, diagnostics = company_universe(db, with_diagnostics=True)
    assert [r["symbol"] for r in eligible] == ["B"]
    assert diagnostics[0]["status"] == "EXCLUDED_SECURITY_TYPE"
    assert diagnostics[0]["validation_blocking"] is False


@pytest.mark.parametrize(
    "cik,expected", [("0000000001", "CIK_MATCH"), ("1", "CIK_MATCH"), (None, "CIK_MISSING_MASSIVE")]
)
def test_common_stock_with_sec_identity_and_sic_accepts_matching_or_absent_massive_cik(
    tmp_path, client_factory, cik, expected
):
    db, *_ = seed(tmp_path)
    populate(db, client_factory, [ticker("A", cik=cik), ticker("B", cik="2")])
    eligible, diagnostics = company_universe(db, with_diagnostics=True)
    assert len(eligible) == 2 and diagnostics == []
    assert eligible[0]["cik"] == "0000000001"
    assert eligible[0]["cik_cross_check"] == expected


@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing", "SECURITY_TYPE_REFERENCE_MISSING"),
        ("unknown", "SECURITY_TYPE_UNKNOWN"),
    ],
)
def test_unresolved_static_identity_blocks_all_discovery_before_adv_ranking(
    tmp_path, config, client_factory, monkeypatch, change, expected
):
    db, *_ = seed(tmp_path)
    rows = [
        ticker(
            "A",
            "FUTURE_CODE" if change == "unknown" else "CS",
            "1",
        ),
        ticker("B", cik="2"),
    ]
    if change == "missing":
        rows = rows[1:]
    populate(db, client_factory, rows)
    _, diagnostics = company_universe(db, with_diagnostics=True)
    assert diagnostics[0]["status"] == expected and diagnostics[0]["validation_blocking"]
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    monkeypatch.setattr(data, "load_daily", forbidden)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    assert prepared.manifest["selected"] == {} and prepared.manifest["candidates"] == []
    assert not prepared.manifest["discovery_counts"]["selection_membership_resolved"]


def test_synthetic_etf_with_sec_sic_high_adv_and_complete_history_never_enters_pairs(
    tmp_path, config, client_factory
):
    db, *_ = seed(tmp_path)
    populate(db, client_factory, [ticker("A", "ETF", "1"), ticker("B", "CS", "2")])
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    assert prepared.manifest["discovery_counts"]["company_security_types_complete"]
    assert prepared.manifest["universe"][0]["symbol"] == "B"
    assert all(
        [r["symbol"] for names in groups.values() for r in names] == ["B"]
        for groups in prepared.manifest["selected"].values()
    )
    assert prepared.manifest["candidates"] == []
    populate(db, client_factory)
    common = prepare_pairs_data(db, config, START, END, preflight=True)
    assert common.manifest["candidates"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("security_type", "ETF"),
        ("provider_security_type", "FUTURE_CODE"),
        ("cik", "0000000999"),
        ("primary_exchange", "XNAS"),
        ("composite_figi", "CHANGED"),
        ("share_class_figi", "CHANGED"),
        ("source", "CHANGED"),
        ("source_version", "CHANGED"),
        ("provider_updated_at", "2026-10-02T00:00:00Z"),
        ("active", 0),
        ("market", "crypto"),
        ("locale", "global"),
    ],
)
def test_manifest_v5_freezes_relevant_reference_fields_locally(
    tmp_path, config, client_factory, monkeypatch, field, value
):
    db, *_ = seed(tmp_path)
    populate(db, client_factory)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(prepared.manifest), encoding="utf-8")
    with db.connect() as connection:
        connection.execute(f"UPDATE security_reference SET {field}=? WHERE symbol='A'", (value,))
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    monkeypatch.setattr(engine, "observe", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path)


def test_catalog_fingerprint_drift_rejects_frozen_manifest(tmp_path, config, client_factory):
    db, *_ = seed(tmp_path)
    populate(db, client_factory)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(prepared.manifest))
    with db.connect() as connection:
        row = connection.execute(
            "SELECT snapshot_id,metadata FROM security_reference_snapshots"
        ).fetchone()
        metadata = json.loads(row["metadata"])
        metadata["type_catalog"][0]["description"] = "CHANGED"
        connection.execute(
            "UPDATE security_reference_snapshots SET metadata=? WHERE snapshot_id=?",
            (json.dumps(metadata), row["snapshot_id"]),
        )
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path)


def test_identical_resync_keeps_stable_membership_digest(tmp_path, config, client_factory):
    db, *_ = seed(tmp_path)
    populate(db, client_factory)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(prepared.manifest))
    first_snapshot = db.security_reference_snapshot()[1]["snapshot_id"]
    populate(db, client_factory)
    assert db.security_reference_snapshot()[1]["snapshot_id"] != first_snapshot
    reused = prepare_pairs_data(db, config, START, END, candidate_manifest=path)
    assert reused.manifest == prepared.manifest


@pytest.mark.parametrize(
    "code,status,blocking",
    [
        ("ETF", "EXCLUDED_SECURITY_TYPE", False),
        ("CS", "EXCLUDED_IDENTITY_CONFLICT", False),
    ],
)
def test_cik_conflict_quarantines_common_stock_only(
    tmp_path, client_factory, code, status, blocking
):
    db, *_ = seed(tmp_path)
    populate(db, client_factory, [ticker("A", code, "999"), ticker("B", cik="2")])
    universe, diagnostics = company_universe(db, with_diagnostics=True)
    assert [row["symbol"] for row in universe] == ["B"]
    assert diagnostics[0]["cik_cross_check"] == "CIK_MISMATCH"
    assert diagnostics[0]["status"] == status
    assert diagnostics[0]["validation_blocking"] is blocking


def test_manifest_rejects_previous_cik_gate_policy_without_changing_economics(tmp_path, config):
    from trading_system.backtest.liquid_universe import fingerprint
    from trading_system.backtest.pairs_stat_arb_v1_manifest import load_manifest, manifest_contract

    contract = manifest_contract(config, START, END)
    previous = {
        **contract,
        "cik_conflict_policy": "SEC_CANONICAL;BOTH_PRESENT_MUST_MATCH;MASSIVE_MISSING_ACCEPTED",
    }
    previous["fingerprint"] = fingerprint(previous)
    path = tmp_path / "previous-policy.json"
    path.write_text(json.dumps(previous))
    assert contract["manifest_version"] == previous["manifest_version"] == 9
    assert contract["strategy_definition"] == previous["strategy_definition"]
    with pytest.raises(ValueError, match=f"{MISMATCH}: cik_conflict_policy"):
        load_manifest(path, contract)


def test_cli_preflight_and_manifest_validation_construct_no_provider(
    tmp_path, config, client_factory, monkeypatch, capsys
):
    from types import SimpleNamespace

    from trading_system.backtest import pairs_stat_arb_v1_research
    from trading_system.data import massive_reference
    from trading_system.data.database import Database

    db, *_ = seed(tmp_path)
    populate(db, client_factory)
    config.storage.database_path = db.path
    config.storage.reports_path = tmp_path
    monkeypatch.setattr(cli, "load_settings", lambda *a: SimpleNamespace(strategy=config))
    monkeypatch.setattr(cli, "_synchronizer", forbidden)
    monkeypatch.setattr(cli, "AlpacaDataClient", forbidden)
    monkeypatch.setattr(cli, "SecClient", forbidden)
    monkeypatch.setattr(massive_reference, "MassiveReferenceClient", forbidden)
    monkeypatch.setattr(Database, "initialize", forbidden)
    assert (
        cli.main(
            [
                "preflight-pairs-stat-arb-v1",
                "--start",
                str(START),
                "--end",
                str(END),
                "--output-stem",
                "local_cli",
            ]
        )
        == 0
    )
    capsys.readouterr()
    # Validate frozen local inputs, with a no-op simulator: no outcomes evaluated.
    monkeypatch.setattr(pairs_stat_arb_v1_research, "simulate_prepared_pairs", lambda p: ([], []))
    assert (
        cli.main(
            [
                "validate-pairs-stat-arb-v1",
                "--start",
                str(START),
                "--end",
                str(END),
                "--output-stem",
                "local_cli_check",
                "--candidate-manifest",
                str(tmp_path / "local_cli_pair_candidates.json"),
            ]
        )
        == 0
    )


def test_manifest_without_reference_change_can_be_checked_without_discovery_or_network(
    tmp_path, config, client_factory, monkeypatch
):
    db, *_ = seed(tmp_path)
    calls = populate(db, client_factory)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(prepared.manifest))
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    monkeypatch.setattr(engine, "observe", forbidden)
    reused = prepare_pairs_data(db, config, START, END, candidate_manifest=path)
    assert reused.manifest == prepared.manifest and len(calls) == 2
    assert reused.manifest["manifest_version"] == 9


def test_reference_cli_sync_missing_key_and_audit_dispatch_are_isolated(
    tmp_path, config, monkeypatch, capsys
):
    from types import SimpleNamespace

    from trading_system.data.database import Database

    config.storage.database_path = tmp_path / "cli.sqlite3"
    database = Database(config.storage.database_path)
    database.initialize()
    monkeypatch.setattr(cli, "load_settings", lambda *a: SimpleNamespace(strategy=config))
    monkeypatch.setattr(cli, "_synchronizer", forbidden)
    monkeypatch.setattr(cli, "AlpacaDataClient", forbidden)
    monkeypatch.setattr(cli, "SecClient", forbidden)
    assert cli.main(["sync-security-reference"]) == 1
    assert "MASSIVE_API_KEY_MISSING" in capsys.readouterr().err
    monkeypatch.setattr(Database, "initialize", forbidden)
    assert cli.main(["audit-security-reference"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["massive_requests_total"] == 0
