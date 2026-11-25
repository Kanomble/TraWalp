"""Offline rules and authorization regressions for Task 9.2."""

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_f01_task9_manual_ingestion import KEY, ROW, observed, pipeline, review

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import f01_task9_auto_admission as auto  # noqa: E402

ASSETS = "total_assets"
CASH = "cash"
URI = "https://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"


@pytest.fixture
def case():
    base = pipeline.filing.filing_base(ROW)
    documents = [
        {"filename": name, "url": f"{base}/{name}", "sha256": digest * 64}
        for name, digest in [
            ("index.json", "a"),
            (ROW["primary"], "b"),
            ("quarter.xml", "c"),
            ("quarter_pre.xml", "d"),
        ]
    ]
    original = documents[2]
    asset = observed(original, "22583000000") | {
        "source_document": original["url"],
        "source_line": 4318,
        "decimals": "-6",
        "context_id": "as-filed",
    }
    cash = asset | {
        "concept": asset["concept"].replace("}Assets", "}CashAndCashEquivalentsAtCarryingValue"),
        "value": "3851000000",
        "source_line": 4355,
        "document_ordinal": 2,
    }
    for fact in (asset, cash):
        fact["relationships"] = [
            {
                "linkrole": "https://example.test/role/CondensedConsolidatedBalanceSheets",
                "arcrole": "http://www.xbrl.org/2003/arcrole/parent-child",
                "to": fact["concept"],
                "source_document": documents[3]["url"],
                "source_sha256": "d" * 64,
                "source_line": 5,
            }
        ]
    cover = [
        asset
        | {
            "concept": "{http://xbrl.sec.gov/dei/2014-01-31}" + tag,
            "text_value": value,
            "value": None,
            "source_line": i,
        }
        for i, (tag, value) in enumerate(
            zip(
                auto.DEI,
                (ROW["cik"], ROW["form"], ROW["report_date"], "false"),
                strict=True,
            ),
            1,
        )
    ]
    shard = {
        "contract": "F01_TASK9_ARELLE_FILING_V1",
        "filing": KEY,
        "status": "EXTRACTION_COMPLETE",
        "runtime": {"version": "2.47.5"},
        "documents": [
            {"selected": False, "format": "ORDINARY_HTML", "source": documents[1]},
            {
                "selected": True,
                "format": "XBRL_DOCUMENT",
                "source": original,
                "taxonomy_documents": [{"uri": URI, "sha256": "e" * 64}],
            },
        ],
        "issues": [{"category": "INCOMPATIBLE_DOCUMENT_FORMAT"}],
        "source_documents": documents,
        "observations": [asset, cash, *cover],
    }
    closure = {URI: {"status": "BOUND", "source": {"sha256": "e" * 64}}}
    return deepcopy(ROW), {"documents": documents}, shard, closure, [ROW["primary"], "quarter.xml"]


def test_golden_values_and_batch_digest(case):
    result = auto.evaluate_filing(*case)
    assert result["source"]["decision"] == "AUTO_ADMITTED"
    facts = result["accounting"]
    assert facts[ASSETS]["admitted"]["fact"]["value"] == "22583000000"
    assert facts[CASH]["admitted"]["fact"]["value"] == "3851000000"
    assert facts[CASH]["admitted"]["original"]["source_line"] == 4355
    report = auto.batch_report({KEY: result}, {"current_selection": True})
    assert report["summary"]["admitted_by_metric"] == {ASSETS: 1, CASH: 1}
    assert report["evidence_sha256"] == auto.sha(
        review.json_bytes({key: value for key, value in report.items() if key != "evidence_sha256"})
    )
    assert report["decision_authority"] == "AUTOMATED"
    assert "reviewer" not in report and "reviewed_at" not in report


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("no_dts", "ACTUAL_DTS_BOUND"),
        ("wrong_hash", "ACTUAL_DTS_BOUND"),
        ("wrong_cik", "DEI_EntityCentralIndexKey"),
        ("amendment", "DEI_AmendmentFlag"),
        ("form", "FILING_METADATA"),
        ("missing_acceptance", "FILING_METADATA"),
        ("source_set", "VERIFIED_SOURCE_SET"),
        ("no_entrypoint", "SELECTED_ORIGINAL_XBRL"),
    ],
)
def test_source_fails_closed(case, failure, expected):
    row, source, shard, closure, entrypoints = case
    if failure == "no_dts":
        closure.clear()
    elif failure == "wrong_hash":
        closure[URI]["source"]["sha256"] = "f" * 64
    elif failure == "wrong_cik":
        shard["observations"][2]["text_value"] = "0000000001"
    elif failure == "amendment":
        shard["observations"][-1]["text_value"] = "true"
    elif failure == "form":
        row["form"] = "10-Q/A"
    elif failure == "missing_acceptance":
        row["acceptance"] = None
    elif failure == "source_set":
        source["documents"] = source["documents"][1:]
    elif failure == "no_entrypoint":
        entrypoints.remove("quarter.xml")
    result = auto.evaluate_filing(*case)
    assert result["source"]["decision"] == "BLOCKED"
    assert expected in result["source"]["failures"]
    assert not result["accounting"][ASSETS]["admitted"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("period", "ENTITY_PERIOD_OR_DIMENSIONS"),
        ("dimension", "ENTITY_PERIOD_OR_DIMENSIONS"),
        ("opaque", "ENTITY_PERIOD_OR_DIMENSIONS"),
        ("nil", "NIL_OR_NONDECIMAL"),
        ("unit", "UNIT_VALUE_OR_CONTEXT"),
        ("missing_original", "ORIGINAL_REFERENCE_UNBOUND"),
        ("not_consolidated", "CONSOLIDATED_BALANCE_ROLE_UNBOUND"),
        ("zero", "NONPOSITIVE_VALUE"),
    ],
)
def test_fact_exclusions(case, failure, expected):
    fact = case[2]["observations"][0]
    fact["context"] = deepcopy(fact["context"])
    if failure == "period":
        fact["context"]["instant"] = "2015-05-31"
    elif failure == "dimension":
        fact["context"]["dimensions"] = [{"axis": "Class"}]
    elif failure == "opaque":
        fact["context"]["opaque_context"] = True
    elif failure == "nil":
        fact["is_nil"] = True
    elif failure == "unit":
        fact["unit"] = {"numerator": ["{http://www.xbrl.org/2003/iso4217}EUR"], "denominator": []}
    elif failure == "missing_original":
        fact["source_line"] = None
    elif failure == "not_consolidated":
        fact["relationships"] = []
    elif failure == "zero":
        fact["value"] = "0"
    result = auto.evaluate_filing(*case)["accounting"]
    assert result[ASSETS]["admitted"] is None
    assert result[ASSETS]["candidates"][0]["reason"] == expected
    assert result[CASH]["admitted"] is not None


@pytest.mark.parametrize("different", [False, True])
def test_duplicate_or_conflict_quarantines_metric(case, different):
    facts = case[2]["observations"]
    facts.append(facts[0] | {"source_line": 80, "value": "1" if different else facts[0]["value"]})
    result = auto.evaluate_filing(*case)["accounting"][ASSETS]
    assert result["reason"] == "DUPLICATE_OR_CONFLICTING_ORIGINALS"
    assert result["admitted"] is None


def test_code_and_ruleset_drift_rejects_import(case, monkeypatch):
    filing = {KEY: auto.evaluate_filing(*case)}
    binding = {"current_selection": True, "decision_candidate": {"fingerprint": "a" * 64}}
    before = auto.batch_report(filing, binding)
    auto.require_current_report(before, auto.batch_report(filing, binding))
    for change in ({"current_selection": False}, {"decision_candidate": {"fingerprint": "b" * 64}}):
        with pytest.raises(ValueError, match="ADMISSION_CHANGED"):
            auto.require_current_report(before, auto.batch_report(filing, binding | change))
    monkeypatch.setattr(auto, "RULESET_SHA256", "c" * 64)
    with pytest.raises(ValueError, match="ADMISSION_CHANGED"):
        auto.require_current_report(before, auto.batch_report(filing, binding))


def test_machine_report_never_substitutes_for_operation_approval():
    binding = {"selection_sha256": "a" * 64, "auto_admission_sha256": "b" * 64}
    with pytest.raises(ValueError):
        review.authorization(
            {"contract": auto.CONTRACT, "decision_authority": "AUTOMATED"}, "import", binding
        )
    with pytest.raises(ValueError, match="EXISTING_LINEAGE_APPEND"):
        pipeline.import_or_reader(
            SimpleNamespace(auto_admission="auto.json", append=False),
            None,
            None,
            None,
            None,
            None,
            None,
        )


def test_published_report_is_immutable_and_hash_bound(case, tmp_path):
    report = auto.batch_report({KEY: auto.evaluate_filing(*case)}, {"current_selection": True})
    path = tmp_path / "auto-admission.json"
    digest = pipeline.publish(path, report)
    assert pipeline.publish(path, report) == digest
    assert pipeline.checked(path, digest)[1] == report
    with pytest.raises(ValueError, match="IMMUTABLE_ARTIFACT_CONFLICT"):
        pipeline.publish(path, report | {"status": "BLOCKED"})
    with pytest.raises(ValueError, match="INPUT_HASH_MISMATCH"):
        pipeline.checked(path, "f" * 64)
    assert pipeline.sha(path.read_bytes()) == digest


def test_reference_filing_cannot_enter_automated_append(case, monkeypatch):
    row, source, shard, closure, entrypoints = case
    for document in source["documents"]:
        document["download_completed"] = "2026-11-24T20:00:00Z"
    report = auto.batch_report(
        {KEY: auto.evaluate_filing(row, source, shard, closure, entrypoints)},
        {"current_selection": True},
    )
    selection = {
        "filings": [row],
        "candidate": {"fingerprint": "a" * 64},
        "universe_sha256": "c" * 64,
        "source_generation": "d" * 64,
    }
    initial = pipeline.bootstrap_manifest(selection, {})
    monkeypatch.setattr(pipeline, "automated_report", lambda *args: report)
    monkeypatch.setattr(pipeline, "reviewed_input", lambda *args: report)
    monkeypatch.setattr(pipeline, "checked", lambda *args: (b"", initial))
    monkeypatch.setattr(review, "execute", lambda *args, **kwargs: pytest.fail("DB gate bypassed"))
    args = SimpleNamespace(
        auto_admission="auto.json",
        auto_admission_sha256="b" * 64,
        phase="import",
        append=True,
        bootstrap_manifest="bootstrap.json",
        bootstrap_sha256="e" * 64,
    )
    with pytest.raises(ValueError, match="APPEND_REQUIRES_DISJOINT"):
        pipeline.import_or_reader(
            args, None, selection, "f" * 64, {"filings": {KEY: source}}, {KEY: shard}, {}
        )
