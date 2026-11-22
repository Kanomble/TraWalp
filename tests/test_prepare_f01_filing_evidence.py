"""Focused tests using the actual pinned Arelle worker and local fixture taxonomy."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_f01_filing_evidence as evidence  # noqa: E402

CIK = "0000059478"
ROW = {"cik": CIK, "accession": "0000059478-25-000254", "symbol": "LLY"}
SCHEMA = """<schema xmlns="http://www.w3.org/2001/XMLSchema"
 xmlns:x="http://www.xbrl.org/2003/instance"
 xmlns:l="http://www.xbrl.org/2003/linkbase" xmlns:xl="http://www.w3.org/1999/xlink"
 targetNamespace="http://fasb.org/us-gaap/2025" elementFormDefault="qualified">
 <annotation><appinfo><l:linkbaseRef xl:type="simple" xl:href="sample_pre.xml"
   xl:arcrole="http://www.w3.org/1999/xlink/properties/linkbase"/></appinfo></annotation>
 <import namespace="http://www.xbrl.org/2003/instance"
   schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>
 <element name="InterestExpenseNonoperating" type="x:monetaryItemType"
   substitutionGroup="x:item" x:periodType="duration" id="interest"/>
 <element name="TotalDebt" type="x:monetaryItemType"
   substitutionGroup="x:item" x:periodType="instant" id="debt"/>
 </schema>"""
LINKBASE = """<linkbase xmlns="http://www.xbrl.org/2003/linkbase"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <presentationLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
 <loc xlink:type="locator" xlink:href="sample.xsd#debt" xlink:label="a"/>
 <loc xlink:type="locator" xlink:href="sample.xsd#interest" xlink:label="b"/>
 <presentationArc xlink:type="arc" xlink:arcrole="http://www.xbrl.org/2003/arcrole/parent-child"
 xlink:from="a" xlink:to="b" order="1"/>
 </presentationLink>
 <calculationLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
 <loc xlink:type="locator" xlink:href="sample.xsd#debt" xlink:label="a"/>
 <loc xlink:type="locator" xlink:href="sample.xsd#interest" xlink:label="b"/>
 <calculationArc xlink:type="arc" xlink:arcrole="http://www.xbrl.org/2003/arcrole/summation-item"
 xlink:from="a" xlink:to="b" order="1" weight="1"/>
 </calculationLink></linkbase>"""
RESOURCES = """<x:context id="q"><x:entity>
 <x:identifier scheme="http://www.sec.gov/CIK">59478</x:identifier>
 <x:segment><d:explicitMember dimension="g:ClassAxis">g:CommonMember</d:explicitMember></x:segment>
 </x:entity><x:period><x:startDate>2025-07-01</x:startDate>
 <x:endDate>2025-09-30</x:endDate></x:period></x:context>
 <x:unit id="usd"><x:measure>iso:USD</x:measure></x:unit>"""
INLINE = f"""<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:x="http://www.xbrl.org/2003/instance"
 xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:iso="http://www.xbrl.org/2003/iso4217"
 xmlns:g="http://fasb.org/us-gaap/2025" xmlns:l="http://www.xbrl.org/2003/linkbase"
 xmlns:xl="http://www.w3.org/1999/xlink"
 xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
 <head><title>Fixture</title></head><body><div style="display:none"><ix:header>
 <ix:references><l:schemaRef xl:type="simple" xl:href="sample.xsd"/></ix:references>
 <ix:resources>{RESOURCES}</ix:resources></ix:header></div>
 <table id="income"><caption>Income Statement</caption><tr><td>
 <ix:nonFraction name="g:InterestExpenseNonoperating" contextRef="q" unitRef="usd"
 decimals="-5" scale="6" sign="-" format="t:num-dot-decimal">179.6</ix:nonFraction>
 </td></tr></table></body></html>"""


@pytest.fixture
def processor(tmp_path):
    site = os.getenv("F01_ARELLE_TEST_SITE")
    if not site:
        pytest.skip("Set F01_ARELLE_TEST_SITE to the isolated Arelle package directory")
    processor = evidence.ArelleProcessor.__new__(evidence.ArelleProcessor)
    processor.directory = tmp_path
    processor.site = Path(site)
    processor.cache = tmp_path / "cache"
    processor.cache.mkdir()
    processor.packages = []
    assert processor.run(probe=True)["version"] == evidence.ARELLE_VERSION
    return processor


def write_cached(folder, name, contents):
    path = folder / name
    path.mkdir(parents=True)
    (path / "content").write_bytes(contents)
    meta = ROW | {
        "filename": name,
        "url": evidence.filing_base(ROW) + "/" + name,
        "bytes": len(contents),
        "sha256": evidence.sha(contents),
    }
    (path / "source.json").write_text(json.dumps(meta), encoding="utf-8")


def test_arelle_inline_context_dimension_and_original_source_binding(processor):
    row = ROW | {"primary": "sample.htm"}
    folder = processor.directory / "filing"
    listing = {
        "directory": {
            "item": [{"name": name} for name in ("sample.htm", "sample.xsd", "sample_pre.xml")]
        }
    }
    write_cached(folder, "sample.htm", INLINE.encode())
    write_cached(folder, "sample.xsd", SCHEMA.encode())
    write_cached(folder, "sample_pre.xml", LINKBASE.encode())
    facts, documents, issues, sources = evidence.extract_cached(row, folder, listing, processor)
    assert issues == []
    assert documents[0]["selected"]
    assert len(sources) == 3
    assert len(facts) == 1
    fact = facts[0]
    assert fact["source"]["sha256"] == evidence.sha(INLINE.encode())
    assert {rel["source_sha256"] for rel in fact["relationships"]} == {
        evidence.sha(LINKBASE.encode())
    }
    assert {rel["weight"] for rel in fact["relationships"]} == {None, "1"}
    assert (fact["scale"], fact["sign"], fact["decimals"]) == ("6", "-", "-5")
    assert fact["context"]["entity"] == "59478"
    assert fact["context"]["startDate"] == "2025-07-01"
    assert fact["context"]["endDate"] == "2025-09-30"
    assert fact["context"]["dimensions"][0]["member"].endswith("}CommonMember")
    joined = evidence.join(
        {
            "taxonomy": "us-gaap",
            "tag": "InterestExpenseNonoperating",
            "unit": "USD",
            "period_start": "2025-07-01",
            "period_end": "2025-09-30",
            "value": "-179600000",
        },
        facts,
        CIK,
    )
    assert joined["status"] == "UNRESOLVED_CONTEXT"
    assert joined["source_candidates"] == [fact["document_ordinal"]]
    assert fact["table_captions"] == ["Income Statement"]


def test_arelle_missing_taxonomy_leaves_explicit_offline_gap(processor):
    document = processor.directory / "missing.htm"
    document.write_text(
        INLINE.replace('xl:href="sample.xsd"', 'xl:href="https://example.invalid/missing.xsd"'),
        encoding="utf-8",
    )
    result = processor.run(document=str(document))
    assert result["issues"] or any("evidence_issue" in item for item in result["observations"])
    assert not any(item["value"] is not None for item in result["observations"])


def test_arelle_plain_xbrl_instant(processor):
    (processor.directory / "sample.xsd").write_text(SCHEMA, encoding="utf-8")
    (processor.directory / "sample_pre.xml").write_text(LINKBASE, encoding="utf-8")
    path = processor.directory / "sample.xml"
    path.write_text(
        """<xbrl xmlns="http://www.xbrl.org/2003/instance"
        xmlns:g="http://fasb.org/us-gaap/2025" xmlns:iso="http://www.xbrl.org/2003/iso4217"
        xmlns:l="http://www.xbrl.org/2003/linkbase" xmlns:xl="http://www.w3.org/1999/xlink">
        <l:schemaRef xl:type="simple" xl:href="sample.xsd"/>
        <context id="i"><entity><identifier scheme="http://www.sec.gov/CIK">59478</identifier>
        </entity><period><instant>2025-09-30</instant></period></context>
        <unit id="usd"><measure>iso:USD</measure></unit>
        <g:TotalDebt contextRef="i" unitRef="usd" decimals="INF">20</g:TotalDebt></xbrl>""",
        encoding="utf-8",
    )
    result = processor.run(document=str(path))
    assert result["issues"] == []
    (fact,) = result["observations"]
    assert fact["value"] == "20"
    assert fact["context"]["instant"] == "2025-09-30"
