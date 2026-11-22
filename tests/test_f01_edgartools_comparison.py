"""Small contract checks for the offline EdgarTools evidence comparison."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluate_f01_edgartools import deny_network, numeric, source_matches  # noqa: E402


def test_exact_source_match_and_wrong_period():
    fact = {
        "concept": "gaap:DebtCurrent",
        "context_ref": "report",
        "entity_scheme": "http://www.sec.gov/CIK",
        "entity_identifier": "0000000001",
        "period_instant": "2026-08-31",
        "is_dimensioned": False,
        "currency": "USD",
        "value": "0",
        "decimals": "-3",
    }
    original = {
        "context_id": "report",
        "context": {
            "scheme": "http://www.sec.gov/CIK",
            "entity": "0000000001",
            "startDate": None,
            "endDate": None,
            "instant": "2026-08-31",
            "dimensions": [],
            "opaque_context": False,
        },
        "concept": "{urn:gaap}DebtCurrent",
        "unit": {"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []},
        "value": "0",
        "decimals": "-3",
    }
    assert source_matches(fact, original, {"gaap": "urn:gaap"})
    assert not source_matches(
        {**fact, "period_instant": "2026-09-01"}, original, {"gaap": "urn:gaap"}
    )
    assert not source_matches({**fact, "is_nil": True}, original, {"gaap": "urn:gaap"})

    original["context"]["dimensions"] = [{"axis": "{urn:gaap}Axis", "member": "{urn:gaap}Member"}]
    dimensioned = {**fact, "is_dimensioned": True, "dim_gaap_Axis": "gaap:Member"}
    assert source_matches(dimensioned, original, {"gaap": "urn:gaap"})
    assert not source_matches(
        {**dimensioned, "dim_gaap_Axis": "gaap:Other"}, original, {"gaap": "urn:gaap"}
    )


def test_missing_numeric_is_distinct_from_zero():
    assert numeric("NaN") is None
    assert numeric(None) is None
    assert numeric("0") == "0"


def test_offline_hook_rejects_connection_and_lookup():
    with pytest.raises(OSError, match="EDGARTOOLS_OFFLINE_NETWORK_DENIED"):
        deny_network("socket.connect", (None, ("www.sec.gov", 443)))
    with pytest.raises(OSError, match="EDGARTOOLS_OFFLINE_NETWORK_DENIED"):
        deny_network("socket.getaddrinfo", ("www.sec.gov", 443))
