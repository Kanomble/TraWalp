"""Pre-outcome eligibility refinement: synthetic local data, no provider transport."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_massive_reference import CATALOG, ticker
from test_massive_reference import offline as offline
from test_pairs_stat_arb_v1 import END, START, forbidden, seed
from test_pairs_stat_arb_v1_security import config as config

from trading_system import cli
from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_security as security
from trading_system.backtest.pairs_stat_arb_v1_data import company_universe, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import MISMATCH
from trading_system.backtest.pairs_stat_arb_v1_research import summary_metadata
from trading_system.backtest.research_registry import (
    FROZEN_CHAMPION_F,
    RESEARCH_FAMILY_STATUS,
    validate_champion_config,
)
from trading_system.data import massive_reference
from trading_system.data.database import Database
from trading_system.data.sec_identity import resolve_sec_identities
from trading_system.data.security_reference import (
    EXCLUDED_IDENTITY_CONFLICT,
    IDENTITY_CONFLICT_CLASSIFICATION_VERSION,
    IDENTITY_CONFLICT_POLICY,
    LOCAL_IDENTITY_STALE_OR_TICKER_REUSE,
    MAPPING_VERSION,
    MASSIVE_REFERENCE_CIK_CONFLICT,
    PAIRS_COMPANY_UNIVERSE_SCOPE,
    UNRESOLVED_CROSS_SOURCE_IDENTITY,
    digest,
    map_record,
    membership_fingerprint,
    normalize_catalog,
    pairs_base_company_universe,
    reference_audit,
)
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import TradableAsset


@pytest.fixture(autouse=True)
def no_sync_or_outcomes(monkeypatch):
    monkeypatch.setattr(massive_reference, "MassiveReferenceClient", forbidden)
    monkeypatch.setattr(cli, "AlpacaDataClient", forbidden)
    monkeypatch.setattr(cli, "SecClient", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)
    monkeypatch.setattr(engine, "observe", forbidden)
    monkeypatch.setattr(engine, "simulate_prepared_pairs", forbidden)
    monkeypatch.setattr(engine, "simulate_trade", forbidden)


def add_company(db, symbol, cik, exchange="NASDAQ"):
    db.upsert_assets(
        [
            TradableAsset(
                symbol=symbol,
                name=symbol,
                exchange=exchange,
                tradable=True,
                fractionable=False,
                asset_class="US_EQUITY",
            )
        ]
    )
    db.upsert_company(CompanyIdentity(symbol=symbol, name=symbol, cik=f"{cik:010d}", sic="3571"))


def store_reference(db, raw):
    """Stage a complete fixture directly, without running any sync/client."""
    catalog = normalize_catalog(CATALOG)
    records = [map_record(r, catalog, "2026-10-14T00:00:00+00:00") for r in raw]
    db.promote_security_reference(
        records,
        {
            "snapshot_id": digest(records),
            "type_catalog": catalog,
            "type_catalog_fingerprint": digest(catalog),
            "mapping_version": MAPPING_VERSION,
            "reference_fingerprint": membership_fingerprint(
                {r["symbol"]: r for r in records}, [r["symbol"] for r in records]
            ),
            "observed_at": "2026-10-14T00:00:00+00:00",
            "sync_success": True,
        },
    )


def test_otc_gate_precedes_all_reference_qualification(tmp_path, monkeypatch):
    db = Database(tmp_path / "otc.sqlite3")
    db.initialize()
    for index, (symbol, exchange) in enumerate(
        [("A", "OTC"), ("B", "  oTc  "), ("C", "NASDAQ"), ("D", "NASDAQ")], 1
    ):
        add_company(db, symbol, index, exchange)
    store_reference(db, [ticker("B", cik="2"), ticker("D", cik="4")])
    original = security.local_security_types
    checked = []

    def local_types(database, symbols, **kwargs):
        checked.extend(symbols)
        return original(database, symbols, **kwargs)

    monkeypatch.setattr(security, "local_security_types", local_types)
    universe, diagnostics = company_universe(db, with_diagnostics=True)
    assert checked == ["C", "D"]
    assert [r["symbol"] for r in universe] == ["D"]
    assert [(r["symbol"], r["status"], r["validation_blocking"]) for r in diagnostics] == [
        ("C", "SECURITY_TYPE_REFERENCE_MISSING", True)
    ]
    audit = reference_audit(db)
    assert audit["pairs_otc_excluded_symbols"] == ["A", "B"]
    assert audit["pairs_non_otc_company_universe_count"] == 2
    assert audit["pairs_non_otc_missing_reference_symbols"] == ["C"]
    assert audit["pairs_reference_identity_blocking_count"] == 1
    assert not audit["company_security_types_complete"]
    # An unresolved OTC type is irrelevant too.
    with db.connect() as connection:
        connection.execute("UPDATE security_reference SET security_type='UNKNOWN' WHERE symbol='B'")
    assert company_universe(db, with_diagnostics=True) == (universe, diagnostics)


def test_exchange_exclusion_uses_no_symbol_or_name_heuristic(tmp_path):
    db = Database(tmp_path / "exchange.sqlite3")
    db.initialize()
    add_company(db, "LOOKSF", 1, "NASDAQ")
    add_company(db, "PRIMARY", 2, "OTC")
    add_company(db, "UNKNOWN_EXCHANGE", 3, None)
    store_reference(
        db, [ticker("LOOKSF"), ticker("PRIMARY", cik="2"), ticker("UNKNOWN_EXCHANGE", cik="3")]
    )
    assert [r["symbol"] for r in company_universe(db)] == ["LOOKSF", "UNKNOWN_EXCHANGE"]


@pytest.mark.parametrize(
    "current,massive,reason",
    [
        ("111", "222", MASSIVE_REFERENCE_CIK_CONFLICT),
        ("222", "222", LOCAL_IDENTITY_STALE_OR_TICKER_REUSE),
        ("222", "333", UNRESOLVED_CROSS_SOURCE_IDENTITY),
        (None, "222", UNRESOLVED_CROSS_SOURCE_IDENTITY),
        ("222", None, UNRESOLVED_CROSS_SOURCE_IDENTITY),
        ("222", "111", UNRESOLVED_CROSS_SOURCE_IDENTITY),
    ],
)
def test_three_source_conflicts_are_explicit_nonblocking_quarantines(
    tmp_path, current, massive, reason
):
    db = Database(tmp_path / "identity.sqlite3")
    db.initialize()
    add_company(db, "CONFLICT", 111)
    db.set_sync_value("sec_reference", "ticker_to_cik", {"CONFLICT": current})
    store_reference(db, [ticker("CONFLICT", cik=massive)])
    before = db.path.read_bytes()
    universe, diagnostics = company_universe(db, with_diagnostics=True)
    assert universe == []
    row = diagnostics[0]
    assert row["status"] == row["quarantine_status"] == EXCLUDED_IDENTITY_CONFLICT
    assert row["identity_conflict_class"] == reason and not row["validation_blocking"]
    assert row["local_cik"] == "0000000111"
    assert row["current_sec_cik"] == (current.zfill(10) if current else None)
    assert row["massive_cik"] == (massive.zfill(10) if massive else None)
    audit = reference_audit(db)
    assert audit["pairs_identity_quarantine_by_reason"] == {reason: 1}
    assert audit["pairs_identity_quarantine_symbols"] == ["CONFLICT"]
    assert audit["eligible_pairs_common_stocks"] == 0
    assert audit["company_security_types_complete"]
    assert db.path.read_bytes() == before


@pytest.mark.parametrize("current", [None, "111"])
def test_missing_massive_cik_is_accepted_without_cached_sec_conflict(tmp_path, current):
    db = Database(tmp_path / "missing-cik.sqlite3")
    db.initialize()
    add_company(db, "VALID", 111)
    db.set_sync_value("sec_reference", "ticker_to_cik", {"VALID": current})
    store_reference(db, [ticker("VALID", cik=None)])
    universe, diagnostics = company_universe(db, with_diagnostics=True)
    assert [r["symbol"] for r in universe] == ["VALID"] and diagnostics == []
    assert universe[0]["cik_cross_check"] == "CIK_MISSING_MASSIVE"
    assert reference_audit(db)["eligible_pairs_common_stocks"] == 1


def observed_shape(tmp_path):
    db, *_ = seed(tmp_path)
    for index in range(22):
        add_company(db, f"OTC{index:02d}", 1000 + index, "OTC")
    conflicts = []
    current = {"A": "1", "B": "2"}
    for index in range(4):
        symbol = f"CONFLICT{index}"
        local, massive = 100 + index, 200 + index
        add_company(db, symbol, local)
        current[symbol] = str(local if index < 2 else massive)
        conflicts.append(ticker(symbol, cik=str(massive)))
    db.set_sync_value("sec_reference", "ticker_to_cik", current)
    store_reference(db, [ticker("A"), ticker("B", cik="2"), *conflicts])
    return db


def test_observed_shape_does_not_block_or_enter_historical_ranking(tmp_path, config, monkeypatch):
    db = observed_shape(tmp_path)
    original = data.load_daily
    loaded = set()

    def load(database, symbols, *args, **kwargs):
        loaded.update(symbols)
        return original(database, symbols, *args, **kwargs)

    monkeypatch.setattr(data, "load_daily", load)
    before = db.path.read_bytes()
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    assert loaded == {"A", "B"}
    assert [r["symbol"] for r in prepared.manifest["universe"]] == ["A", "B"]
    assert all(
        {r["symbol_a"], r["symbol_b"]} == {"A", "B"} for r in prepared.manifest["candidates"]
    )
    summary = summary_metadata(prepared, preflight=True)
    assert summary["company_security_types_complete"] and summary["ready_for_local_validation"]
    assert len(summary["security_type_diagnostics"]) == 4
    assert all(not r["validation_blocking"] for r in summary["security_type_diagnostics"])
    assert db.path.read_bytes() == before


def test_audit_observed_shape_is_read_only_and_zero_network(tmp_path, config, monkeypatch, capsys):
    db = observed_shape(tmp_path)
    before = db.path.read_bytes()
    config.storage.database_path = db.path
    monkeypatch.setattr(cli, "load_settings", lambda *args: SimpleNamespace(strategy=config))
    monkeypatch.setattr(Database, "connect", forbidden)
    monkeypatch.setattr(Database, "initialize", forbidden)
    assert cli.main(["audit-security-reference"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["pairs_otc_excluded_count"] == 22
    assert audit["pairs_otc_excluded_symbols"] == [f"OTC{i:02d}" for i in range(22)]
    assert audit["pairs_identity_quarantine_count"] == 4
    assert audit["pairs_identity_quarantine_symbols"] == [f"CONFLICT{i}" for i in range(4)]
    assert audit["pairs_identity_quarantine_by_reason"] == {
        MASSIVE_REFERENCE_CIK_CONFLICT: 2,
        LOCAL_IDENTITY_STALE_OR_TICKER_REUSE: 2,
    }
    assert audit["pairs_non_otc_company_universe_count"] == 6
    assert audit["pairs_non_otc_missing_reference_count"] == 0
    assert audit["pairs_non_otc_missing_reference_symbols"] == []
    assert audit["pairs_non_otc_unknown_type_count"] == 0
    assert audit["pairs_non_otc_unknown_type_symbols"] == []
    assert audit["pairs_reference_identity_blocking_count"] == 0
    assert audit["company_security_types_complete"]
    assert audit["missing_local_references"] == 23  # OTCs plus diagnostic SPY.
    assert audit["sec_cik_mismatches"] == 4
    assert (
        audit["massive_requests_total"]
        == audit["alpaca_requests_total"]
        == audit["sec_requests_total"]
        == 0
    )
    assert db.path.read_bytes() == before


def test_ticker_reuse_preserves_historical_identity_bars_facts_and_sec_guard(tmp_path, config):
    db, *_ = seed(tmp_path)
    add_company(db, "REUSED", 111)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT 'REUSED',timeframe,timestamp,open,high,low,close,volume,"
            "trade_count,vwap FROM bars WHERE symbol='A'"
        )
    db.upsert_facts(
        [
            FundamentalFact(
                symbol="REUSED",
                cik="0000000111",
                metric="revenue",
                tag="Revenues",
                value=Decimal("123"),
                unit="USD",
                period_end=date(2024, 12, 31),
                filed=date(2025, 2, 1),
                form="10-K",
            )
        ]
    )
    db.set_sync_value("sec_reference", "ticker_to_cik", {"REUSED": "222"})
    store_reference(db, [ticker("A"), ticker("B", cik="2"), ticker("REUSED", cik="222")])
    before = db.path.read_bytes()
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    assert [r["symbol"] for r in prepared.manifest["universe"]] == ["A", "B"]
    assert all(
        "REUSED" not in (r["symbol_a"], r["symbol_b"]) for r in prepared.manifest["candidates"]
    )
    assert (
        prepared.manifest["discovery_counts"]["security_type_diagnostics"][0][
            "identity_conflict_class"
        ]
        == LOCAL_IDENTITY_STALE_OR_TICKER_REUSE
    )
    assert (
        db.path.read_bytes() == before
    )  # All companies, bars, facts and cached history unchanged.
    resolution = resolve_sec_identities(
        {"REUSED"}, {"REUSED": "0000000111"}, {"REUSED": "0000000222"}
    )
    assert resolution.ticker_map["REUSED"] == "0000000111"
    assert resolution.conflicts[0].proposed_cik == "0000000222"


def save_manifest(tmp_path, prepared):
    path = tmp_path / "frozen-v5.json"
    path.write_text(json.dumps(prepared.manifest))
    return path


@pytest.mark.parametrize(
    "change", ["new_conflict", "resolved_conflict", "new_reason", "new_evidence"]
)
def test_identity_evidence_changes_reject_manifest_before_prices(
    tmp_path, config, monkeypatch, change
):
    db = observed_shape(tmp_path)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    path = save_manifest(tmp_path, prepared)
    assert prepared.manifest["manifest_version"] == 8
    assert prepared.manifest["company_universe_scope"] == PAIRS_COMPANY_UNIVERSE_SCOPE
    assert prepared.manifest["identity_conflict_policy"] == IDENTITY_CONFLICT_POLICY
    assert (
        prepared.manifest["identity_conflict_classification_version"]
        == IDENTITY_CONFLICT_CLASSIFICATION_VERSION
    )
    if change == "resolved_conflict":
        with db.connect() as connection:
            connection.execute(
                "UPDATE security_reference SET cik='0000000100' WHERE symbol='CONFLICT0'"
            )
    else:
        with db.read_only() as connection:
            current = json.loads(
                connection.execute(
                    "SELECT value FROM sync_state WHERE source='sec_reference' "
                    "AND key='ticker_to_cik'"
                ).fetchone()["value"]
            )
        current["A" if change == "new_conflict" else "CONFLICT0"] = {
            "new_conflict": "999",
            "new_reason": "200",
            "new_evidence": "777",
        }[change]
        db.set_sync_value("sec_reference", "ticker_to_cik", current)
    monkeypatch.setattr(data, "load_daily", forbidden)
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)


def test_otc_reference_and_cache_are_outside_fingerprints_but_exchange_promotion_is_bound(
    tmp_path, config, monkeypatch
):
    db, *_ = seed(tmp_path)
    add_company(db, "OTC_ONLY", 111, "OTC")
    store_reference(db, [ticker("A"), ticker("B", cik="2"), ticker("OTC_ONLY", cik="222")])
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    records, _ = db.security_reference_snapshot()
    assert prepared.manifest["security_reference_fingerprint"] == membership_fingerprint(
        records, ["A", "B"]
    )
    path = save_manifest(tmp_path, prepared)
    with db.connect() as connection:
        connection.execute(
            "UPDATE security_reference SET cik='0000000333',security_type='UNKNOWN' "
            "WHERE symbol='OTC_ONLY'"
        )
    db.set_sync_value("sec_reference", "ticker_to_cik", {"OTC_ONLY": "333"})
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    reused = prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)
    assert reused.manifest == prepared.manifest
    with db.connect() as connection:
        connection.execute("UPDATE assets SET exchange='NASDAQ' WHERE symbol='OTC_ONLY'")
    assert "OTC_ONLY" in {r["symbol"] for r in pairs_base_company_universe(db)}
    monkeypatch.setattr(data, "load_daily", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=path, preflight=True)


def test_f_and_rejected_families_remain_isolated(config):
    assert FROZEN_CHAMPION_F.production_label == "F/configured/C1"
    validate_champion_config(config)
    for family in (
        "research-orb-v1",
        "research-intraday-reversal-v1",
        "research-market-intraday-momentum-v1",
    ):
        assert RESEARCH_FAMILY_STATUS[family] == "REJECTED"
