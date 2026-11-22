"""Focused tests using the actual pinned Arelle worker and local fixture taxonomy."""

import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_f01_accounting_semantic_qualification as semantic_audit  # noqa: E402
import prepare_f01_filing_evidence as evidence  # noqa: E402
import preview_f01_accounting_rules as accounting_rules  # noqa: E402
import resolve_f01_accounting_evidence as resolution  # noqa: E402

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
    processor.sec_plugin = os.getenv("F01_ARELLE_TEST_SEC_PLUGIN")
    processor.release = processor.run(probe=True)
    assert processor.release["version"] == evidence.ARELLE_VERSION
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


def test_advanced_arelle_views_and_original_rounding_interval(processor):
    (processor.directory / "sample.xsd").write_text(SCHEMA, encoding="utf-8")
    (processor.directory / "sample_pre.xml").write_text(LINKBASE, encoding="utf-8")
    html = processor.directory / "sample.htm"
    html.write_text(INLINE, encoding="utf-8")
    result = processor.run(document=str(html), advanced_concepts=["InterestExpenseNonoperating"])
    summary = result["advanced_validation"]
    assert summary["calculation_2_1_arcs"] == 1
    assert summary["calculation_1_1_arcs"] == 0
    assert summary["formulae_in_dts"] is False
    assert summary["views"]["presentation"]
    (fact,) = result["observations"]
    assert fact["arelle_rounding_interval"] == ["-179650000", "-179550000", True, True]
    assert fact["context_unit_group"] == fact["document_ordinal"]


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


def test_pinned_sec_transform_loads_with_arelle_2475(processor):
    if not processor.sec_plugin:
        pytest.skip("Set F01_ARELLE_TEST_SEC_PLUGIN to the verified EDGAR transform directory")
    plugin = Path(processor.sec_plugin)
    assert {name: evidence.pilot.file_sha(plugin / name) for name in evidence.SEC_PLUGIN_FILES} == (
        evidence.SEC_PLUGIN_FILES
    )
    (processor.directory / "sample.xsd").write_text(SCHEMA, encoding="utf-8")
    (processor.directory / "sample_pre.xml").write_text(LINKBASE, encoding="utf-8")
    path = processor.directory / "sample.htm"
    path.write_text(
        INLINE.replace(
            "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12",
            "http://www.sec.gov/inlineXBRL/transformation/2015-08-31",
        ).replace('t:num-dot-decimal">179.6', 't:numwordsen">two'),
        encoding="utf-8",
    )
    result = processor.run(document=str(path))
    assert result["issues"] == []
    assert str(result["observations"][0]["value"]) in {"-2E+6", "-2000000"}


def test_ordinary_html_leaves_separate_instance_as_entrypoint(processor):
    folder = processor.directory / "filing"
    row = ROW | {"primary": "sample.htm"}
    names = ("sample.htm", "sample.xml")
    listing = {"directory": {"item": [{"name": name} for name in names]}}
    write_cached(folder, names[0], b"<!doctype html><html><body>Ordinary</body></html>")
    write_cached(folder, names[1], b"<xbrl/>")
    seen = []

    def process(**request):
        seen.append(Path(request["document"]).name)
        return {"issues": [], "observations": []}

    processor.run = process
    _, documents, issues, sources = evidence.extract_cached(row, folder, listing, processor)
    assert seen == ["sample.xml"]
    assert documents[0]["format"] == "ORDINARY_HTML"
    assert issues[0]["category"] == "INCOMPATIBLE_DOCUMENT_FORMAT"
    assert len(sources) == 2


def accounting_pair(value="100", decimals="-2", unit="USD"):
    measure = f"{{{evidence.ISO}}}USD" if unit == "USD" else f"{{{evidence.XBRL}}}pure"
    fact = {
        "taxonomy": "us-gaap",
        "tag": "InterestExpenseNonoperating",
        "unit": unit,
        "period_start": "2025-07-01",
        "period_end": "2025-09-30",
        "value": value,
        "accession_number": ROW["accession"],
    }
    row = {
        "concept": "{http://fasb.org/us-gaap/2025}InterestExpenseNonoperating",
        "unit": {"numerator": [measure], "denominator": []},
        "context": {
            "scheme": "https://www.sec.gov/CIK",
            "entity": "59478",
            "startDate": fact["period_start"],
            "endDate": fact["period_end"],
            "instant": None,
            "opaque_context": [],
            "dimensions": [],
        },
        "value": value,
        "decimals": decimals,
        "document_ordinal": 4,
        "source": {"accession": ROW["accession"], "sha256": "a" * 64},
    }
    return fact, row


def test_accounting_pure_preserves_value_and_unit():
    fact, row = accounting_pair("0.15", "2", "pure")
    assert evidence.join(fact, [row], CIK)["fact"] == fact
    assert evidence.join(fact, [row], CIK)["status"] == "VERIFIED_SOURCE_FACT"
    assert evidence.join(fact | {"unit": "percent"}, [row], CIK)["status"] == "MISSING_EVIDENCE"


def test_accounting_complete_duplicates_and_dimensional_provenance():
    fact, row = accounting_pair()
    duplicate = row | {"document_ordinal": 6, "context_id": "another"}
    segment = row | {
        "document_ordinal": 8,
        "context": row["context"]
        | {
            "dimensions": [{"axis": "{urn:sample}SegmentAxis", "member": "{urn:sample}Member"}],
        },
    }
    joined = evidence.join(fact, [segment, row, duplicate], CIK)
    assert joined == evidence.join(fact, [duplicate, segment, row], CIK)
    assert joined["reason"] == "COMPLETE_DUPLICATES"
    assert joined["source_candidates"] == [4, 6, 8]
    assert joined["whole_entity_candidates"] == [4, 6]


def test_accounting_numeric_precision_conflicts_and_closed_intervals():
    fact, row = accounting_pair("2500", "-2")
    other = row | {"document_ordinal": 7, "value": "2000", "decimals": "-3"}
    assert evidence.join(fact, [row, other], CIK)["reason"] == "CONSISTENT_DUPLICATES_MOST_PRECISE"
    assert (
        evidence.join(fact | {"value": "2000"}, [row, other], CIK)["status"]
        == "CANDIDATE_EQUIVALENCE"
    )
    conflicting = other | {"value": "2400", "decimals": "-2"}
    assert evidence.join(fact, [row, conflicting], CIK)["status"] == "CONFLICT"


def test_accounting_distinct_documents_and_opaque_context_stay_open():
    fact, row = accounting_pair()
    other = row | {"document_ordinal": 7, "source": row["source"] | {"sha256": "b" * 64}}
    assert evidence.join(fact, [row, other], CIK)["status"] == "UNRESOLVED_CONTEXT"
    opaque = row | {"context": row["context"] | {"opaque_context": ["unparsed"]}}
    assert evidence.join(fact, [opaque], CIK)["status"] == "UNRESOLVED_CONTEXT"


def test_exact_dei_quarterly_namespace_still_requires_entity_unit_and_context():
    fact, row = accounting_pair()
    fact.update(
        taxonomy="dei",
        tag=semantic_audit.SHARE_TAG,
        unit="shares",
        period_start=None,
        period_end="2025-09-30",
    )
    row.update(
        concept=semantic_audit.DEI,
        unit={"numerator": [f"{{{evidence.XBRL}}}shares"], "denominator": []},
    )
    row["context"].update(startDate=None, endDate=None, instant=fact["period_end"])
    assert evidence.join(fact, [row], CIK)["status"] == "VERIFIED_SOURCE_FACT"
    other_namespace = row | {"concept": row["concept"].replace("2021q4", "2021q3")}
    assert evidence.join(fact, [other_namespace], CIK)["status"] == "MISSING_EVIDENCE"
    assert evidence.join(fact, [row], "0000000001")["status"] == "MISSING_EVIDENCE"
    class_context = row["context"] | {"dimensions": [{"axis": "axis", "member": "class"}]}
    assert (
        evidence.join(fact, [row | {"context": class_context}], CIK)["status"]
        == "UNRESOLVED_CONTEXT"
    )


def test_tradable_class_preview_respects_cutoff_and_conflict():
    existing = {"acceptance": "2025-12-31T14:00:00-05:00", "filed": "2025-12-31"}
    after_close = existing | {"acceptance": "2025-12-31T16:01:00-05:00"}
    row = {"context": {"instant": "2025-12-20"}, "value": "100"}
    rival = row | {"value": "200"}
    stale = row | {"context": {"instant": "2024-01-01"}}
    session = date(2025, 12, 31)
    admitted = (existing, row, [])
    assert semantic_audit.select_class_snapshot([(after_close, rival, []), admitted], session) == [
        admitted
    ]
    assert not semantic_audit.select_class_snapshot([admitted, (existing, rival, [])], session)
    assert not semantic_audit.select_class_snapshot([(existing, stale, [])], session)


def test_cover_holdings_must_bind_the_exact_class_amount_and_measurement():
    cover = (
        "Class B ordinary shares, par value $0.01 per share, outstanding as of "
        "December 4, 2025 was 250 (which number includes 12 issued shares held by the registrant)"
    )
    measured = date(2025, 12, 4)
    assert accounting_rules.net_class_shares(cover, "Class B ordinary shares", measured, 250) == (
        238,
        12,
    )
    assert (
        accounting_rules.net_class_shares(cover, "Class A ordinary shares", measured, 250) is None
    )
    assert (
        accounting_rules.net_class_shares(cover, "Class B ordinary shares", measured, 251) is None
    )
    assert (
        accounting_rules.net_class_shares(cover, "Class B ordinary shares", date(2025, 12, 5), 250)
        is None
    )
    conflict = cover + " " + cover.replace("includes 12", "includes 20")
    assert (
        accounting_rules.net_class_shares(conflict, "Class B ordinary shares", measured, 250)
        is None
    )


def test_finance_lease_is_exact_to_the_selected_balance_without_implicit_zero():
    key = (ROW["accession"], "2025-09-30")
    assets = SimpleNamespace(
        sources=[SimpleNamespace(accession_number=key[0], period_end=date.fromisoformat(key[1]))]
    )
    lease = {"value": "0", "source": {"sha256": "hash"}, "document_ordinal": 1}
    assert accounting_rules.lease_selection({key: [lease]}, assets)["value"] == "0"
    assert accounting_rules.lease_selection({("other", key[1]): [lease]}, assets)["value"] is None
    conflict = accounting_rules.lease_selection({key: [lease, lease | {"value": "1"}]}, assets)
    assert (conflict["value"], conflict["reason"]) == (None, "CONFLICT")


def test_extended_inventory_merges_warmup_amendments_and_diagnosis():
    seed = ROW | {"tier": "INITIAL", "probes": [["2025-12-31", "reference", "interest"]]}
    rows = [seed]
    cases = {
        ROW["accession"]: (date(2025, 10, 30), "10-Q", None),
        "0000059478-15-000001": (date(2015, 2, 1), "10-K/A", None),
        "0000059478-24-000065": (date(2024, 2, 1), "10-K", None),
        "0000059478-26-999999": (date(2026, 12, 1), "10-K", None),
        "0000059478-25-999999": (date(2025, 10, 30), "8-K", None),
    }
    evidence.add_extended_rows(rows, ROW, cases)
    by_id = {row["accession"]: row for row in rows}
    assert len(rows) == len(by_id)
    assert by_id[ROW["accession"]]["coverage_reasons"] == [
        "BOUND_DIAGNOSIS",
        "INITIAL",
        "RESEARCH_PERIOD",
    ]
    assert by_id["0000059478-15-000001"]["coverage_reasons"] == ["EXTENDED", "WARMUP"]
    assert by_id["0000059478-24-000065"]["coverage_reasons"] == [
        "ACCOUNTING_AUDIT",
        "EXTENDED",
        "RESEARCH_PERIOD",
    ]
    assert not any(acc.endswith("999999") for acc in by_id)


def test_extended_extraction_preserves_non_numeric_facts_and_bound_shards(processor, monkeypatch):
    row = ROW | {
        "primary": "sample.htm",
        "tier": "INITIAL",
        "probes": [],
        "filed": "2025-10-30",
        "acceptance": "2025-10-30T15:15:28+00:00",
    }
    directory = processor.directory
    folder = directory / CIK / ROW["accession"]
    listing = {
        "directory": {
            "item": [{"name": name} for name in ("sample.htm", "sample.xsd", "sample_pre.xml")]
        }
    }
    schema = SCHEMA.replace(
        "</schema>",
        '<element name="TradingSymbol" type="x:stringItemType" '
        'substitutionGroup="x:item" x:periodType="duration"/></schema>',
    )
    inline = INLINE.replace(
        "</body>", '<ix:nonNumeric name="g:TradingSymbol" contextRef="q">LLY</ix:nonNumeric></body>'
    )
    for name, content in {
        "index.json": json.dumps(listing),
        "sample.htm": inline,
        "sample.xsd": schema,
        "sample_pre.xml": LINKBASE,
    }.items():
        write_cached(folder, name, content.encode())
    # Exercise the package requirement using the isolated plugin when selected by the caller.
    if not processor.sec_plugin:
        pytest.skip("Set F01_ARELLE_TEST_SEC_PLUGIN to the verified EDGAR transform directory")
    candidate = {"head": "abc", "fingerprint": "test"}
    cache = evidence.extended_cache([row], directory, candidate)
    hashes = {
        "cache_sha256": evidence.sha(evidence.bulk.json_bytes(cache)),
        "taxonomy_coverage_sha256": "fixture",
    }
    coverage = {
        "candidate": candidate,
        "draft_sha256": hashes["cache_sha256"],
        "offline_coverage": True,
        "sec_plugin_verified": True,
        "sec_plugin_commit": evidence.EDGAR_COMMIT,
        "urls": {"fixture": {"status": "BOUND"}},
    }
    target = directory / "extract"
    evidence.write_extended_draft(
        [row], directory, {}, processor, candidate, cache, coverage, hashes, target
    )
    path = target / "manifest.json"
    _, manifest = evidence.bulk.read_metadata(path)
    ((entry, parsed),) = evidence.extended_records(path, manifest)
    (symbol_fact,) = [
        fact for fact in parsed["observations"] if fact["concept"].endswith("}TradingSymbol")
    ]
    assert symbol_fact["text_value"] == "LLY"
    assert symbol_fact["source"]["sha256"] == evidence.sha(inline.encode())
    monkeypatch.setattr(evidence.pilot, "pilot_spec", lambda: SimpleNamespace(active=[ROW]))
    review = directory / "review"
    resolution.reconcile_extended(path, manifest, cache, coverage, hashes, candidate, review)
    _, review_file = evidence.bulk.read_metadata(review / entry["file"])
    assert review_file["review_domains"]["shares_and_classes"] == [symbol_fact["document_ordinal"]]
    assert review_file["reported_acceptance"] == row["acceptance"]
    assert not any(review_file["semantic_evidence"].values())
    with pytest.raises(evidence.bulk.Blocked, match="EXTENDED_TAXONOMY_COVERAGE_NOT_BOUND"):
        evidence.verify_extended_inputs(
            cache, coverage | {"draft_sha256": "stale"}, hashes["cache_sha256"], candidate
        )
    with (target / entry["file"]).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(evidence.bulk.Blocked, match="EXTENDED_SHARD_BINDING_MISMATCH"):
        list(evidence.extended_records(path, manifest))
