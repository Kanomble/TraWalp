"""Focused tests for the Accounting handoff without SQLite or provider access."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_f01_accounting_handoff as handoff  # noqa: E402

from trading_system.models.fundamentals import FundamentalFact


def sample_template():
    external = {
        "lineage": {
            "classification": "RESEARCH_ONLY",
            "research_scope": handoff.SCOPE,
            "dataset_id": "new-dataset",
            "code_fingerprint": "candidate",
        },
        "database": "isolated",
        "schema_fingerprint": "schema",
        "database_sha256_at_bootstrap": "empty",
    }
    code = {"fingerprint": "candidate"}
    return external, code


def test_handoff_requires_a_new_bootstrap_with_exact_scope_and_code():
    external, code = sample_template()
    assert handoff.template({}, {}, external, code)["binding"]["lineage"]["dataset_id"] == (
        "new-dataset"
    )
    with pytest.raises(ValueError, match="NEW_CANDIDATE"):
        handoff.template({}, {}, external, {"fingerprint": "previous"})
    external["lineage"]["research_scope"] = "older-pilot"
    with pytest.raises(ValueError, match="NEW_CANDIDATE"):
        handoff.template({}, {}, external, code)


def test_review_rejects_pending_denied_and_rebound_lineage():
    external, code = sample_template()
    expected = handoff.template({}, {"E8/DIRECT": {}}, external, code)
    reviewed = expected | {"reviewer": "Independent reviewer"}
    with pytest.raises(ValueError, match="FULL_RESEARCH_POLICY_REVIEW_REQUIRED"):
        handoff.validate_review(expected, reviewed)
    with pytest.raises(ValueError, match="FULL_RESEARCH_POLICY_REVIEW_REQUIRED"):
        handoff.validate_review(expected, reviewed | {"decisions": {"E8/DIRECT": "DENY"}})
    approved = reviewed | {"decisions": {"E8/DIRECT": "APPROVE_RESEARCH"}}
    handoff.validate_review(expected, approved)
    with pytest.raises(ValueError, match="ACCOUNTING_REVIEW_BINDING_DRIFT"):
        handoff.validate_review(
            expected,
            approved
            | {
                "binding": approved["binding"]
                | {
                    "target": "different-target",
                }
            },
        )


def test_source_parity_rejects_changes_even_if_values_match():
    reports = dict.fromkeys(
        (
            "coverage",
            "trial_debt_references",
            "policy_qualification",
            "open_technical_facts",
            "inputs",
            "rule_catalog",
            "rule_catalog_sha256",
        ),
        {},
    )
    old = reports | {
        "source_facts": {"one": "same file"},
        "probes": [
            {"symbol": f"test{i}", "session": "2026-11-20", "preview": {"value": "1"}}
            for i in range(77)
        ],
    }
    new = old | {"probes": [p | {"financing": {}} for p in old["probes"]]}
    handoff.validate_parity(old, new)
    with pytest.raises(ValueError, match="E8_E10_VALUE_OR_SOURCE_DRIFT"):
        handoff.validate_parity(old, new | {"source_facts": {"one": "different file"}})


def test_acceptance_cutoff_is_in_new_york_and_rejects_future_filing():
    e10 = {
        "probes": [{"session": "2026-11-20", "preview": {"sources": ["one"]}, "financing": {}}],
        "source_facts": {"one": {"original": {"acceptance": "2026-11-20T21:00:00+00:00"}}},
    }
    handoff.source_bindings(e10)
    e10["source_facts"]["one"]["original"]["acceptance"] = "2026-11-20T21:00:01+00:00"
    with pytest.raises(ValueError, match="ORIGINAL_LOOK_AHEAD"):
        handoff.source_bindings(e10)


def test_projection_checks_exact_source_keys_and_values():
    raw = {
        "cik": "0000000123",
        "symbol": "TEST",
        "metric": "total_assets",
        "taxonomy": "us-gaap",
        "tag": "Assets",
        "value": "100",
        "unit": "USD",
        "period_end": "2026-09-30",
        "filed": "2026-10-28",
        "form": "10-Q",
        "accession_number": "0000000123-26-000001",
    }
    evidence = {"one": {"fact": raw}}
    exact = FundamentalFact.model_validate(raw)
    conflicting = FundamentalFact.model_validate(raw | {"value": "101"})
    assert handoff.projection_summary(evidence, [exact, exact]) == {"exact_planned_fact": 1}
    assert handoff.projection_summary(evidence, []) == {"evidence_only": 1}
    assert handoff.projection_summary(evidence, [conflicting]) == {"same_key_conflict": 1}
    with pytest.raises(ValueError, match="PLANNED_CANONICAL_KEY_CONFLICT"):
        handoff.projection_summary(evidence, [exact, conflicting])
