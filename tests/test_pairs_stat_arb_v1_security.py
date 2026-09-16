"""Authoritative identity boundary; synthetic evidence never comes from price outcomes."""

import json

import pytest
from test_pairs_stat_arb_v1 import END, START, forbidden, seed
from test_pairs_stat_arb_v1 import config as config
from test_pairs_stat_arb_v1 import offline as offline

from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_security as security
from trading_system.backtest.pairs_stat_arb_v1_data import company_universe, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_research import run_pairs_stat_arb_v1


def test_existing_asset_and_sec_identity_cannot_establish_company_security_type(
    tmp_path, config, monkeypatch
):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(engine, "observe", forbidden)
    assert all(not e.resolved for e in security.local_security_types(db, ["A", "B"]).values())
    assert company_universe(db) == []
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="unknown_types", preflight=True
    )
    assert summary["company_security_type_status"] == security.UNAVAILABLE
    assert not summary["company_only_universe_satisfied"]
    assert not summary["company_security_types_complete"]
    assert not summary["ready_for_local_validation"]
    assert summary["pair_session_candidates"] == 0
    assert summary["network_used"] is False
    assert "NEW_AUTHORITATIVE_SOURCE" in summary["metadata_sync_requirement"]
    requirements = json.loads(paths["daily_requirements"].read_text())
    assert (
        requirements["required_sessions"] == []
    )  # No fake Daily remediation for a metadata problem.
    assert {r["symbol"] for r in requirements["company_security_type_requirements"]} == {"A", "B"}
    assert all(
        r["validation_blocking"] and r["status"] == security.UNAVAILABLE
        for r in requirements["company_security_type_requirements"]
    )
    for manifest in (None, paths["pair_candidates"]):
        with pytest.raises(ValueError, match=security.UNAVAILABLE):
            prepare_pairs_data(db, config, START, END, candidate_manifest=manifest)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    with pytest.raises(ValueError, match=security.UNAVAILABLE):
        engine.simulate_prepared_pairs(prepared)


@pytest.mark.parametrize("kind", ["ETF", "ETP", "FUND", "WARRANT", "UNIT", "OTHER", "UNKNOWN"])
def test_only_authoritative_operating_company_common_stock_is_admitted(
    tmp_path, config, monkeypatch, kind
):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(
        security,
        "local_security_types",
        lambda database, symbols: {
            "A": security.SecurityTypeEvidence(kind, None, "SYNTHETIC_TEST_SOURCE"),
            "B": security.SecurityTypeEvidence("COMMON_STOCK", True, "SYNTHETIC_TEST_SOURCE"),
        },
    )
    assert [r["symbol"] for r in company_universe(db)] == ["B"]
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    diagnostic = prepared.manifest["discovery_counts"]["security_type_diagnostics"][0]
    assert diagnostic["status"] == (
        security.UNAVAILABLE if kind == "UNKNOWN" else "EXCLUDED_SECURITY_TYPE"
    )
    assert diagnostic["validation_blocking"] == (kind == "UNKNOWN")


@pytest.mark.parametrize(
    "operating,source", [(None, "SYNTHETIC"), (False, "SYNTHETIC"), (True, None)]
)
def test_common_stock_alone_or_without_source_does_not_establish_operating_company(
    tmp_path, monkeypatch, operating, source
):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(
        security,
        "local_security_types",
        lambda database, symbols: {
            s: security.SecurityTypeEvidence("COMMON_STOCK", operating, source) for s in symbols
        },
    )
    assert company_universe(db) == []


def test_names_and_symbols_do_not_supply_instrument_classification(tmp_path, monkeypatch):
    db, *_ = seed(tmp_path)
    with db.connect() as connection:
        connection.execute(
            "UPDATE assets SET name='iShares SPDR Trust ETF',symbol='IBIT' WHERE symbol='A'"
        )
        connection.execute(
            "UPDATE companies SET name='iShares SPDR Trust ETF',symbol='IBIT' WHERE symbol='A'"
        )
    assert company_universe(db) == []  # Even ordinary-looking B remains unknown.
    # Synthetic source evidence, not the label, controls the company gate.
    monkeypatch.setattr(
        security,
        "local_security_types",
        lambda database, symbols: {
            s: security.SecurityTypeEvidence("COMMON_STOCK", True, "SYNTHETIC_TEST_SOURCE")
            for s in symbols
        },
    )
    assert [r["symbol"] for r in company_universe(db)] == ["B", "IBIT"]
