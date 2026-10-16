"""Synthetic local metadata boundaries and fingerprints; no user DB/provider access."""

import json
from types import SimpleNamespace

import pytest
from test_massive_reference import client_factory as client_factory
from test_massive_reference import clock as clock
from test_massive_reference import offline as offline
from test_massive_reference import response, ticker
from test_pairs_stat_arb_v1 import END, START, forbidden, seed
from test_pairs_stat_arb_v1_security import config as config
from test_pairs_stat_arb_v1_security import populate

from trading_system import cli
from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_security as security
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.pairs_stat_arb_v1_data import company_universe, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import (
    MISMATCH,
    load_manifest,
    manifest_contract,
)
from trading_system.backtest.pairs_stat_arb_v1_research import summary_metadata
from trading_system.data.database import Database
from trading_system.data.security_reference import (
    MAPPING_VERSION,
    PAIRS_COMPANY_UNIVERSE_SCOPE,
    membership_fingerprint,
    pairs_base_company_universe,
    reference_audit,
    sync_security_reference,
)
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset


def asset(symbol):
    return TradableAsset(
        symbol=symbol, name=symbol, tradable=True, fractionable=False, asset_class="US_EQUITY"
    )


@pytest.fixture
def scope_db(tmp_path):
    db = Database(tmp_path / "scope.sqlite3")
    db.initialize()
    db.upsert_assets([asset(s) for s in "ABCD"])
    db.upsert_company(CompanyIdentity(symbol="A", name="A", cik="0000000001", sic="3571"))
    db.upsert_company(CompanyIdentity(symbol="C", name="C", cik="0000000003", sic="INVALID"))
    db.upsert_company(CompanyIdentity(symbol="D", name="D", cik="0000000000", sic="3571"))
    return db


def test_sec_company_gate_precedes_reference_qualification(scope_db, monkeypatch):
    original = security.local_security_types
    qualified_symbols = []

    def local_types(database, symbols, **kwargs):
        qualified_symbols.extend(symbols)
        return original(database, symbols, **kwargs)

    monkeypatch.setattr(security, "local_security_types", local_types)
    assert [r["symbol"] for r in pairs_base_company_universe(scope_db)] == ["A"]
    universe, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert universe == [] and qualified_symbols == ["A"]
    assert [(r["symbol"], r["status"], r["validation_blocking"]) for r in diagnostics] == [
        ("A", "SECURITY_TYPE_REFERENCE_MISSING", True)
    ]
    assert not {"B", "C", "D"} & {r["symbol"] for r in diagnostics}


@pytest.mark.parametrize(
    "code,type_name",
    [
        ("ETF", "ETF"),
        ("ETS", "ETF"),
        ("ETV", "ETP"),
        ("RIGHT", "RIGHT"),
        ("SP", "STRUCTURED_PRODUCT"),
    ],
)
def test_known_non_common_in_base_scope_is_nonblocking(scope_db, client_factory, code, type_name):
    populate(scope_db, client_factory, [ticker("A", code, "999")])
    universe, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert universe == [] and len(diagnostics) == 1
    assert diagnostics[0]["security_type"] == type_name
    assert diagnostics[0]["status"] == "EXCLUDED_SECURITY_TYPE"
    assert diagnostics[0]["validation_blocking"] is False
    assert diagnostics[0]["cik_cross_check"] == "CIK_MISMATCH"


@pytest.mark.parametrize("provider_cik", ["1", None])
def test_common_stock_with_valid_local_identity_enters_subsequent_selection(
    scope_db, client_factory, provider_cik
):
    populate(scope_db, client_factory, [ticker("A", "CS", provider_cik)])
    universe, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert [r["symbol"] for r in universe] == ["A"]
    assert universe[0]["sic2"] == "35" and diagnostics == []


@pytest.mark.parametrize("invalid_cik", ["0000000000", "", "NOT_A_CIK", "1.0", "12345678901"])
def test_invalid_local_cik_is_outside_scope_without_changing_general_model(scope_db, invalid_cik):
    with scope_db.connect() as connection:
        connection.execute("UPDATE companies SET cik=? WHERE symbol='D'", (invalid_cik,))
    assert [r["symbol"] for r in pairs_base_company_universe(scope_db)] == ["A"]
    audit = reference_audit(scope_db)
    assert audit["pairs_base_company_universe_count"] == 1
    assert audit["pairs_base_company_missing_reference_symbols"] == ["A"]
    assert audit["missing_local_references"] == 4


@pytest.mark.parametrize("invalid_sic", [None, "", "0000", "0", "INVALID", "12345"])
def test_invalid_local_sic_is_outside_scope(scope_db, invalid_sic):
    with scope_db.connect() as connection:
        connection.execute("UPDATE companies SET sic=? WHERE symbol='C'", (invalid_sic,))
    _, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert [r["symbol"] for r in diagnostics] == ["A"]


def test_asset_flags_and_spy_remain_outside_base_scope(scope_db):
    scope_db.upsert_assets([asset("SPY"), asset("CRYPTO"), asset("HALTED")])
    for i, symbol in enumerate(("SPY", "CRYPTO", "HALTED"), 5):
        scope_db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{i:010d}", sic="3571")
        )
    with scope_db.connect() as connection:
        connection.execute("UPDATE assets SET asset_class='CRYPTO' WHERE symbol='CRYPTO'")
        connection.execute("UPDATE assets SET tradable=0 WHERE symbol='HALTED'")
    assert [r["symbol"] for r in pairs_base_company_universe(scope_db)] == ["A"]


def test_many_irrelevant_missing_assets_do_not_block_complete_synthetic_pairs(
    tmp_path, config, client_factory, monkeypatch
):
    db, *_ = seed(tmp_path)
    db.upsert_assets([asset(f"OUT{i:02d}") for i in range(30)])
    calls = populate(db, client_factory)
    monkeypatch.setattr(engine, "observe", forbidden)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    summary = summary_metadata(prepared, preflight=True)
    assert summary["company_security_types_complete"] and summary["ready_for_local_validation"]
    records, _ = db.security_reference_snapshot()
    assert prepared.manifest["security_reference_fingerprint"] == membership_fingerprint(
        records, ["A", "B"]
    )
    audit = reference_audit(db)
    assert audit["missing_local_references"] == 31  # 30 outsiders plus diagnostic SPY.
    assert audit["pairs_base_company_universe_count"] == 2
    assert audit["pairs_base_company_missing_reference_count"] == 0
    with db.connect() as connection:
        connection.execute("DELETE FROM security_reference WHERE symbol='A'")
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    blocked = prepare_pairs_data(db, config, START, END, preflight=True)
    assert not blocked.manifest["discovery_counts"]["company_security_types_complete"]
    assert [
        r["symbol"] for r in blocked.manifest["discovery_counts"]["security_type_diagnostics"]
    ] == ["A"]
    assert len(calls) == 2


def test_future_type_is_visible_and_blocking_only_when_inside_base_scope(scope_db, client_factory):
    populate(scope_db, client_factory, [ticker("A", "CS"), ticker("B", "FUTURE_CODE")])
    universe, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert [r["symbol"] for r in universe] == ["A"] and diagnostics == []
    audit = reference_audit(scope_db)
    assert audit["unknown_provider_type_counts"] == {"FUTURE_CODE": 1}
    assert audit["pairs_base_company_unknown_type_symbols"] == []
    populate(scope_db, client_factory, [ticker("A", "FUTURE_CODE")])
    universe, diagnostics = company_universe(scope_db, with_diagnostics=True)
    assert universe == []
    assert (
        diagnostics[0]["status"] == "SECURITY_TYPE_UNKNOWN"
        and diagnostics[0]["validation_blocking"]
    )
    audit = reference_audit(scope_db)
    assert audit["pairs_base_company_unknown_type_symbols"] == ["A"]
    assert audit["unknown_provider_type_counts"] == {"FUTURE_CODE": 1}


def test_irrelevant_reference_changes_do_not_invalidate_frozen_membership(
    tmp_path, config, client_factory, monkeypatch
):
    db, *_ = seed(tmp_path)
    db.upsert_assets([asset("OUTSIDE")])
    populate(
        db,
        client_factory,
        [ticker("A", cik="1"), ticker("B", cik="2"), ticker("OUTSIDE", "ETF", "999")],
    )
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(prepared.manifest))
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    monkeypatch.setattr(engine, "observe", forbidden)
    with db.connect() as connection:
        connection.execute(
            "UPDATE security_reference SET security_type='UNKNOWN', "
            "provider_security_type='FUTURE_CODE',cik='0000000777', "
            "primary_exchange='XNAS',provider_updated_at='CHANGED' WHERE symbol='OUTSIDE'"
        )
    reused = prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)
    assert reused.manifest == prepared.manifest
    with db.connect() as connection:
        connection.execute("DELETE FROM security_reference WHERE symbol='OUTSIDE'")
    reused = prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)
    assert reused.manifest == prepared.manifest
    # Joining the base scope can affect membership, and must invalidate the manifest.
    db.upsert_company(
        CompanyIdentity(symbol="OUTSIDE", name="OUTSIDE", cik="0000000777", sic="3571")
    )
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)


def test_known_exclusion_inside_base_scope_is_fingerprinted(tmp_path, config, client_factory):
    db, *_ = seed(tmp_path)
    populate(db, client_factory, [ticker("A", "ETF"), ticker("B", cik="2")])
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    records, _ = db.security_reference_snapshot()
    assert prepared.manifest["security_reference_fingerprint"] == membership_fingerprint(
        records, ["A", "B"]
    )
    assert prepared.manifest["security_reference_fingerprint"] != membership_fingerprint(
        records, ["B"]
    )
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps(prepared.manifest))
    with db.connect() as connection:
        connection.execute(
            "UPDATE security_reference SET security_type='STRUCTURED_PRODUCT', "
            "provider_security_type='SP' WHERE symbol='A'"
        )
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)


@pytest.mark.parametrize(
    "old_scope",
    [
        None,
        "ALL_CURRENT_TRADABLE_US_EQUITY",
        "LOCAL_TRADABLE_US_EQUITY_WITH_VALID_SEC_COMPANY_CIK_SIC",
    ],
)
def test_previous_broader_scope_is_rejected_even_with_updated_version_mapping_and_checksum(
    tmp_path, config, old_scope
):
    contract = manifest_contract(config, START, END)
    previous = dict(contract)
    if old_scope is None:
        previous.pop("company_universe_scope")
    else:
        previous["company_universe_scope"] = old_scope
    previous["fingerprint"] = fingerprint(previous)
    path = tmp_path / "old-scope.json"
    path.write_text(json.dumps(previous))
    assert previous["manifest_version"] == 9
    assert previous["provider_type_mapping_version"] == MAPPING_VERSION
    assert contract["company_universe_scope"] == PAIRS_COMPANY_UNIVERSE_SCOPE
    with pytest.raises(ValueError, match=f"{MISMATCH}: company_universe_scope"):
        load_manifest(path, contract)


def test_previous_mapping_requires_new_snapshot_but_local_audit_remains_available(
    scope_db, config, client_factory
):
    populate(scope_db, client_factory, [ticker("A")])
    with scope_db.connect() as connection:
        row = connection.execute(
            "SELECT snapshot_id,metadata FROM security_reference_snapshots"
        ).fetchone()
        metadata = json.loads(row["metadata"])
        metadata["mapping_version"] = "MASSIVE_STOCK_TYPES_V1"
        connection.execute(
            "UPDATE security_reference_snapshots SET metadata=? WHERE snapshot_id=?",
            (json.dumps(metadata), row["snapshot_id"]),
        )
    audit = reference_audit(scope_db)
    assert audit["provider_type_mapping_version"] == "MASSIVE_STOCK_TYPES_V1"
    assert audit["expected_provider_type_mapping_version"] == "MASSIVE_STOCK_TYPES_V2"
    assert not audit["security_reference_mapping_current"]
    with pytest.raises(ValueError, match="SECURITY_REFERENCE_MAPPING_VERSION_MISMATCH"):
        prepare_pairs_data(scope_db, config, START, END, preflight=True)


def test_cli_audit_distinguishes_global_coverage_from_pairs_relevant_gaps_read_only(
    scope_db, config, client_factory, monkeypatch, capsys
):
    for i, symbol in enumerate(("UNKNOWN", "CONFLICT", "ETF_CONFLICT", "MISSING"), 10):
        scope_db.upsert_assets([asset(symbol)])
        scope_db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{i:010d}", sic="3571")
        )
    client, calls = client_factory(
        [
            response(),
            response(
                results=[
                    ticker("A"),
                    ticker("UNKNOWN", "FUTURE_CODE", "999"),
                    ticker("CONFLICT", "CS", "999"),
                    ticker("ETF_CONFLICT", "ETF", "999"),
                    ticker("B", "FUTURE_CODE"),
                ]
            ),
        ]
    )
    sync_security_reference(scope_db, client)
    before = scope_db.path.read_bytes()
    config.storage.database_path = scope_db.path
    monkeypatch.setattr(cli, "load_settings", lambda *a: SimpleNamespace(strategy=config))
    monkeypatch.setattr(cli, "AlpacaDataClient", forbidden)
    monkeypatch.setattr(cli, "SecClient", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(Database, "connect", forbidden)
    from trading_system.data import massive_reference

    monkeypatch.setattr(massive_reference, "MassiveReferenceClient", forbidden)
    assert cli.main(["audit-security-reference"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["pairs_base_company_universe_count"] == 5
    assert audit["pairs_base_company_missing_reference_count"] == 1
    assert audit["pairs_base_company_missing_reference_symbols"] == ["MISSING"]
    assert audit["pairs_base_company_unknown_type_count"] == 1
    assert audit["pairs_base_company_unknown_type_symbols"] == ["UNKNOWN"]
    assert audit["common_stock_cik_mismatch_count"] == 1
    assert audit["common_stock_cik_mismatch_symbols"] == ["CONFLICT"]
    assert audit["missing_local_references"] == 3  # C/D plus the actual base gap.
    assert audit["coverage_fraction"] == 5 / 8
    assert audit["unknown_provider_type_counts"] == {"FUTURE_CODE": 2}
    assert audit["sec_cik_mismatches"] == 3  # Unknown, common-stock and ETF diagnostics.
    assert (
        audit["massive_requests_total"]
        == audit["alpaca_requests_total"]
        == audit["sec_requests_total"]
        == 0
    )
    assert scope_db.path.read_bytes() == before and len(calls) == 2


def test_audit_lists_are_sorted_bounded_and_counts_remain_exact(scope_db):
    for i in range(101):
        symbol = f"GAP{i:03d}"
        scope_db.upsert_assets([asset(symbol)])
        scope_db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{i + 100:010d}", sic="3571")
        )
    audit = reference_audit(scope_db)
    assert audit["pairs_base_company_missing_reference_count"] == 102
    symbols = audit["pairs_base_company_missing_reference_symbols"]
    assert len(symbols) == 100 and symbols == sorted(symbols)
    assert audit["pairs_base_company_missing_reference_symbols_truncated"]
