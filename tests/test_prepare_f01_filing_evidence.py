import io
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_f01_filing_evidence as evidence  # noqa: E402

CIK = "0000059478"
ROW = {"cik": CIK, "accession": "0000059478-25-000254", "symbol": "LLY"}
XHTML = b"""<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:x="http://www.xbrl.org/2003/instance"
 xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:iso="http://www.xbrl.org/2003/iso4217"
 xmlns:g="http://fasb.org/us-gaap/2025"
 xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
 <x:context id="q"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">59478</x:identifier>
 </x:entity><x:period><x:startDate>2025-07-01</x:startDate>
 <x:endDate>2025-09-30</x:endDate></x:period></x:context>
 <x:context id="instant"><x:entity>
 <x:identifier scheme="http://www.sec.gov/CIK">59478</x:identifier>
 <x:segment><d:explicitMember dimension="g:ClassAxis">g:CommonMember</d:explicitMember>
 </x:segment></x:entity><x:period><x:instant>2025-09-30</x:instant></x:period></x:context>
 <x:unit id="usd"><x:measure>iso:USD</x:measure></x:unit>
 <table id="income"><caption>Income Statement</caption>
 <ix:nonFraction name="g:InterestExpenseNonoperating" contextRef="q" unitRef="usd"
    decimals="-5" scale="6" sign="-" format="t:num-dot-decimal">179.6</ix:nonFraction>
 <ix:nonFraction name="g:LongTermDebt" contextRef="instant" unitRef="usd"
    decimals="INF">600</ix:nonFraction></table></html>"""


def observation():
    return {
        "taxonomy": "us-gaap",
        "tag": "InterestExpenseNonoperating",
        "unit": "USD",
        "period_start": "2025-07-01",
        "period_end": "2025-09-30",
        "value": "-179600000",
    }


def test_extract_context_scope_transform_and_reporting_location():
    duration, instant = list(evidence.extract(XHTML))
    assert duration["value"] == "-1.796E+8"
    assert (duration["scale"], duration["sign"], duration["decimals"]) == ("6", "-", "-5")
    assert duration["context"]["startDate"] == "2025-07-01"
    assert duration["table_captions"] == ["Income Statement"]
    assert instant["context"]["instant"] == "2025-09-30"
    assert instant["context"]["dimensions"][0]["member"].endswith("}CommonMember")
    assert evidence.join(observation(), [duration], CIK)["status"] == "VERIFIED_SOURCE_FACT"
    assert evidence.join(observation(), [duration, duration], CIK)["status"] == "UNRESOLVED_CONTEXT"
    assert evidence.join(observation(), [], CIK)["status"] == "MISSING_EVIDENCE"
    assert evidence.join(observation() | {"value": "0"}, [duration], CIK)["status"] == "CONFLICT"


def test_rounding_depends_on_original_decimals():
    assert evidence.rounding(Decimal(179_600_000), "-5") == (179_550_000, 179_650_000)
    assert evidence.rounding(Decimal(10), "INF") == (10, 10)
    assert evidence.rounding(Decimal(10), None) is None


def test_lly_rounding_is_only_a_candidate_when_intervals_overlap():
    prototype = next(evidence.extract(XHTML))

    def item(start, end, value, decimals):
        return prototype | {
            "context": prototype["context"] | {"startDate": start, "endDate": end},
            "value": value,
            "decimals": decimals,
        }

    files = {
        "0000059478-25-000254": {
            "observations": [
                item("2025-07-01", "2025-09-30", "179600000", "-5"),
                item("2025-01-01", "2025-09-30", "672400000", "-5") | {"document_ordinal": 100},
            ]
        },
        "0000059478-25-000204": {
            "observations": [
                item("2025-01-01", "2025-06-30", "492700000", "-5"),
            ]
        },
    }
    assert evidence.lly_rounding(files)["status"] == "CANDIDATE_EQUIVALENCE"
    for filing in files.values():
        for fact in filing["observations"]:
            fact["decimals"] = "INF"
    assert evidence.lly_rounding(files)["status"] == "CONFLICT"
    assert evidence.lly_rounding({})["status"] == "MISSING_EVIDENCE"


def test_reject_external_entities():
    with pytest.raises(ValueError, match="UNSUPPORTED_XML_DECLARATION"):
        list(evidence.extract(b'<!DOCTYPE test [<!ENTITY ext SYSTEM "file:///x">]><test/>'))


def test_interrupted_document_restarts_and_verified_cache_is_reused(tmp_path):
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "4"}

    class Client:
        calls = 0

        def open_url(self, url, method):
            self.calls += 1
            assert url == evidence.filing_base(ROW) + "/sample.htm" and method == "GET"
            return Response(b"xx" if self.calls == 1 else b"abcd")

    client = Client()
    with pytest.raises(ValueError, match="INCOMPLETE_SEC_DOCUMENT"):
        evidence.get_document(client, tmp_path, ROW, "sample.htm")
    data, meta = evidence.get_document(client, tmp_path, ROW, "sample.htm")
    assert data == b"abcd" and meta["sha256"] == evidence.sha(data)
    assert evidence.get_document(client, tmp_path, ROW, "sample.htm")[1] == meta
    assert client.calls == 2
    path = tmp_path / CIK / ROW["accession"] / "sample.htm" / "content"
    path.write_bytes(b"wxyz")
    with pytest.raises(ValueError, match="CACHE_SHA256_MISMATCH"):
        evidence.get_document(client, tmp_path, ROW, "sample.htm")
    assert client.calls == 2
