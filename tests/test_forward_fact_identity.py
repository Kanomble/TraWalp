"""Forward accounting facts follow verified issuer identity, never ticker guesses."""

from datetime import date
from unittest.mock import Mock

import pytest
from test_sec_force_pilot import local_scope as local_scope

from trading_system.data.database import Database
from trading_system.data.sec_force import SecWriteDatabase
from trading_system.data.sync import DataSynchronizer
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.shadow.inputs import LocalInputs


def fact(symbol, cik="0000000001", filed=date(2026, 11, 1)):
    return FundamentalFact(
        cik=cik,
        symbol=symbol,
        metric="operating_cash_flow",
        tag="NetCashProvidedByUsedInOperatingActivities",
        value=100,
        unit="USD",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        filed=filed,
        form="10-Q",
        accession_number=f"{cik}-{symbol}-{filed}",
    )


@pytest.fixture
def issuer_db(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="CURRENT", name="Issuer"))
    db.upsert_facts(
        [fact("PREVIOUS"), fact("CURRENT", "0000000002"), fact("FUTURE", filed=date(2026, 11, 9))]
    )
    return db


def test_forward_facts_use_verified_cik_preserve_source_labels_and_pit(issuer_db):
    with LocalInputs(issuer_db.path).snapshot() as inputs:
        facts = inputs.facts_available_as_of("CURRENT", date(2026, 11, 6))
        assert len(facts) == 1
        assert facts[0].symbol == "PREVIOUS"
        assert facts[0].cik == "0000000001"
        assert inputs.provenance["identity_inputs"]["fundamental_cik|CURRENT"] == "0000000001"
        assert inputs.facts_available_as_of("CURRENT", date(2026, 11, 6), "revenue") == []
    # Legacy research semantics are explicitly unchanged.
    assert issuer_db.facts_available_as_of("CURRENT", date(2026, 11, 6))[0].cik == "0000000002"


def test_forward_fact_identity_quarantine_has_no_symbol_fallback(issuer_db, monkeypatch):
    with LocalInputs(issuer_db.path).snapshot() as inputs:
        monkeypatch.setattr(inputs, "unresolved_sec_identity_conflict_symbols", lambda: {"CURRENT"})
        assert inputs.facts_available_as_of("CURRENT", date(2026, 11, 6)) == []


def test_no_canonical_company_means_no_issuer_fact_read(issuer_db):
    with LocalInputs(issuer_db.path).snapshot() as inputs:
        assert inputs.facts_available_as_of("PREVIOUS", date(2026, 11, 6)) == []


def test_bounded_official_refresh_repairs_supported_fact_gap_despite_known_accession(local_scope):
    db, _ = local_scope
    db.set_sync_value("sec_accessions", "0000000001", ["already-known"])
    assert not db.facts_available_as_of("AAA", date(2026, 11, 6))
    sec = Mock(request_counts={})
    sec.company_facts.return_value = {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2026-01-01",
                                "end": "2026-03-31",
                                "filed": "2026-05-01",
                                "val": 100,
                                "form": "10-Q",
                                "accn": "already-known",
                            }
                        ]
                    }
                }
            }
        },
    }
    sec.submissions.side_effect = AssertionError("No submissions refresh")
    sec.ticker_to_cik.side_effect = AssertionError("No identity reassignment")
    guarded = SecWriteDatabase(db.path)
    result = DataSynchronizer(guarded, None, sec).sync_sec_full(["AAA"], companyfacts_only=True)
    assert result["companies_updated"] == 1
    assert db.facts_available_as_of("AAA", date(2026, 11, 6))[0].value == 100
    assert guarded.authorized_write_tables == {"companies", "fundamental_facts", "sync_state"}
    assert result["submissions_requests"] == result["ticker_map_requests"] == 0
