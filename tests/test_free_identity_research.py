"""Synthetic parser/contract fixtures only; no network, real keys or production DB."""

import socket
from copy import deepcopy

import pytest
import requests

from trading_system.data.free_identity_research import classify_graph, transition_discrepancies
from trading_system.data.nasdaq_lifecycle_research import (
    NasdaqLifecycleResearchClient,
    parse_nasdaq_notice,
)
from trading_system.data.openfigi_research import OpenFigiResearchClient, parse_openfigi
from trading_system.data.sec_identity_research import filing_url, parse_sec_filing
from trading_system.data.security_identity import LIFECYCLE_EVIDENCE, UNKNOWN


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in free-identity unit tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)


def notice(include_effective=True):
    # Deliberately missing tr/td end tags, as in the real public ECA layout.
    cells = {
        "Current Symbol": "OLD",
        "New Symbol": "XYZ",
        "Current CUSIP": "111111111",
        "New CUSIP": "222222222",
    }
    if include_effective:
        cells["Marketplace Effective Date"] = "Friday, September 10, 2021"
    return (
        "<p>Thursday, September 9, 2021</p><table>"
        + "".join(
            f"<tr><th><strong>{key}:</strong></th><td>{value}" for key, value in cells.items()
        )
        + "</table>"
    )


def sec_record():
    html = '<ix:nonNumeric name="dei:EntityCentralIndexKey">0000000001</ix:nonNumeric>'
    html += '<ix:nonNumeric name="dei:TradingSymbol" contextRef="common">XYZ</ix:nonNumeric>'
    html += '<ix:nonNumeric name="dei:EntityRegistrantName">Example</ix:nonNumeric>'
    return parse_sec_filing(
        html,
        cik="0000000001",
        accession="0000000001-21-000001",
        filing_date="2021-09-15",
        source_reference="https://www.sec.gov/example",
    )


def graph():
    n = parse_nasdaq_notice(notice(), "ECA2021-1")["records"][0]
    return {
        "symbol": "XYZ",
        "current": {
            "ticker": "XYZ",
            "cik": "0000000001",
            "type": "CS",
            "share_class_figi": "CLASS1",
            "composite_figi": "COMP1",
        },
        "sec": [sec_record()],
        "nasdaq": [n],
        "openfigi": [
            {
                "query": {"idType": "ID_CUSIP", "idValue": "222222222"},
                "mappings": [
                    {
                        "shareClassFIGI": "CLASS1",
                        "compositeFIGI": "COMP1",
                        "securityType2": "Common Stock",
                    }
                ],
            }
        ],
        "reviewed_interval": {
            "symbol": "XYZ",
            "cik": "0000000001",
            "source": "NASDAQ_TRADER",
            "effective_start": "2021-09-10",
            "effective_end": None,
            "security_type": "COMMON_STOCK",
            "stable_security_identity": "SHARE_CLASS_FIGI:CLASS1",
            "evidence_type": LIFECYCLE_EVIDENCE,
            "coverage": "COMPLETE_SECURITY_SYMBOL_MEMBERSHIP",
            "source_reference": n["source_reference"],
            "review_reference": "SYNTHETIC EXTERNALLY REVIEWED COMPLETE MEMBERSHIP FIXTURE",
            "observed_at": "2026-10-15T00:00:00+00:00",
        },
    }


def test_nasdaq_marketplace_effective_date_is_parsed_separately_from_publication():
    record = parse_nasdaq_notice(notice(), "ECA2021-1")["records"][0]
    assert record["effective_date"] == "2021-09-10"
    assert record["old_cusip"] == "111111111" and record["new_cusip"] == "222222222"
    assert not record["complete_interval"]


def test_nasdaq_publication_date_is_never_an_effective_date_fallback():
    record = parse_nasdaq_notice(notice(False), "ECA2021-1")["records"][0]
    assert record["effective_date"] is None


def test_nasdaq_transfer_keeps_ordinary_leg_and_exact_as_of_heading():
    html = "<table><tr><th>Issue before</th><th>Issue after</th>"
    html += "<th>NYSE Ticker/CUSIP through Monday, March 20, 2023</th>"
    html += (
        "<th>NASDAQ Ticker/CUSIP as of Tuesday, March 21, 2023</th>"
        "<th>Category</th><th>Lot</th></tr>"
    )
    html += (
        "<tr><td>Old</td><td>New</td><td>Ordinary<p>AAA: G50752107</p>Units AAA U: G50752115</td>"
    )
    html += (
        "<td>Ordinary<p>BBB: G9TY5A101</p>Warrants BBBW: G9TY5A119</td>"
        "<td>Capital</td><td>100</td></tr></table>"
    )
    records = parse_nasdaq_notice(html, "dtn2023-7")["records"]
    assert len(records) == 1
    assert records[0]["old_symbol"] == "AAA" and records[0]["new_symbol"] == "BBB"
    assert records[0]["old_valid_through"] == "2023-03-20"
    assert records[0]["effective_date"] == "2023-03-21"


def test_openfigi_cusip_shareclass_mapping_is_not_an_interval():
    jobs = [{"idType": "ID_CUSIP", "idValue": "111111111"}]
    result = parse_openfigi(
        jobs, [{"data": [{"shareClassFIGI": "CLASS1", "compositeFIGI": "COMP1", "figi": "VENUE1"}]}]
    )[0]
    assert result["mappings"][0]["shareClassFIGI"] == "CLASS1"
    assert not result["interval_proof"] and "effective_start" not in result
    assert parse_openfigi(jobs, [{"warning": "No identifier found."}])[0]["mappings"] == []


def test_sec_inline_facts_bind_only_issuer_and_symbol():
    record = sec_record()
    assert record["issuer_binding_confirmed"]
    assert record["document_ciks"] == ["0000000001"] and record["trading_symbols"] == ["XYZ"]
    assert record["filing_date"] == "2021-09-15" and not record["interval_proof"]


@pytest.mark.parametrize("field", ["SHARE_CLASS_FIGI:CLASS1", "COMPOSITE_FIGI:COMP1"])
def test_only_fully_reviewed_complete_evidence_can_be_a_nonpromoted_candidate(field):
    g = graph()
    g["reviewed_interval"]["stable_security_identity"] = field
    outcome = classify_graph(g)
    assert outcome["classification"] == "VERIFIED_EPOCH_CANDIDATE"
    assert outcome["identity_status"] == UNKNOWN
    assert outcome["recommendation"] == "PROMOTE_IN_SEPARATE_TASK"
    assert "verified" not in outcome["candidate_epoch"]


def test_same_cik_without_any_stable_security_id_is_insufficient():
    g = graph()
    g["current"].update(share_class_figi=None, composite_figi=None)
    assert classify_graph(g)["classification"] == "INSUFFICIENT"


def test_missing_figi_in_cusip_bridge_cannot_produce_a_candidate():
    g = graph()
    g["openfigi"][0]["mappings"] = [{"securityType2": "Common Stock"}]
    assert classify_graph(g)["classification"] != "VERIFIED_EPOCH_CANDIDATE"


def test_same_composite_cannot_override_a_conflicting_share_class_bridge():
    g = graph()
    g["reviewed_interval"]["stable_security_identity"] = "COMPOSITE_FIGI:COMP1"
    g["openfigi"][0]["mappings"][0]["shareClassFIGI"] = "OTHER_CLASS"
    assert classify_graph(g)["classification"] == "CONFLICT"


def test_conflicting_bound_current_shareclass_ids_are_conflict_without_mutating_input():
    g = graph()
    g["current_security_claims"] = [
        {"symbol": "XYZ", "cik": "0000000001", "share_class_figi": "OTHER"}
    ]
    before = deepcopy(g)
    assert classify_graph(g)["classification"] == "CONFLICT" and g == before


@pytest.mark.parametrize("missing", ["effective_start", "review_reference", "coverage"])
def test_missing_interval_proof_or_boundary_is_not_a_candidate(missing):
    g = graph()
    g["reviewed_interval"].pop(missing)
    result = classify_graph(g)
    assert result["classification"] == "CORROBORATING_ONLY" and result["candidate_epoch"] is None
    assert result["recommendation"] == "KEEP_BLOCKED"


def test_event_point_and_current_cik_do_not_create_complete_interval_or_issuer_bridge():
    g = graph()
    g["reviewed_interval"] = None
    assert classify_graph(g)["classification"] == "CORROBORATING_ONLY"
    g = graph()
    g["sec"] = []
    assert classify_graph(g)["classification"] != "VERIFIED_EPOCH_CANDIDATE"


def test_process_date_is_not_compared_as_marketplace_effective_date():
    n = graph()["nasdaq"][0]
    a = {
        "old_symbol": "OLD",
        "new_symbol": "XYZ",
        "old_cusip": n["old_cusip"],
        "new_cusip": n["new_cusip"],
        "process_date": "2021-09-21",
        "provider_action_id": "fixture",
    }
    assert transition_discrepancies([n], [a]) == []
    a["old_cusip"] = "333333333"
    disagreement = transition_discrepancies([n], [a])[0]
    assert disagreement["reason"] == "CUSIP_BEFORE_DISAGREEMENT"
    assert disagreement["scope"] == "HISTORICAL_TRANSITION_UNRECONCILED_EVENT_ALIGNMENT"


def test_clients_refuse_large_or_nonapproved_requests_before_transport():
    n, f = NasdaqLifecycleResearchClient(), OpenFigiResearchClient()
    try:
        with pytest.raises(ValueError, match="SCOPE_REFUSED"):
            n.notice("https://other.test")
        with pytest.raises(ValueError, match="SCOPE_REFUSED"):
            f.mapping([{}] * 6)
        with pytest.raises(ValueError, match="JOB_REFUSED"):
            f.mapping([{"idType": "ID_CUSIP", "idValue": "bad?key=secret", "exchCode": "US"}])
        with pytest.raises(ValueError, match="REFERENCE_REFUSED"):
            filing_url("0000000001", "0000000001-21-000001", "../other.htm")
        assert n.requests == f.requests == 0
    finally:
        n.close()
        f.close()
