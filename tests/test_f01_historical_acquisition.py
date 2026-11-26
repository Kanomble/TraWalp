"""Offline regressions for the bound Task-9.6 acquisition workflow."""

import hashlib
import io
import json
import sqlite3
import sys
import urllib.error
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "reports")]

from f01_readiness_inventory import read_database  # noqa: E402
from f01_task96_contract import CONTRACT, authorization, identity  # noqa: E402
from f01_task96_evidence import LINEAGES, checked_original, proof_facts  # noqa: E402
from f01_task96_fundamentals import COVER, choose_shares, observed_shares, ocf_ttm  # noqa: E402
from f01_task96_network import (  # noqa: E402
    PAGE_BYTES,
    Budget,
    CountedSecOpener,
    alpaca_fetch,
    parse_spy,
    scope_for,
    spy_download,
    spy_execution_scope,
    verify_sec_listing,
)
from f01_task96_process import extract  # noqa: E402

CIK = "0000320187"
ACC = "0000320187-16-000242"
FILING = {
    "cik": CIK,
    "accession": ACC,
    "filed": "2016-01-06",
    "form": "10-Q",
    "acceptance": "2016-01-06T16:00:00+00:00",
    "primary": "q.htm",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_cik_accession_scope():
    valid = FILING | {"symbol_label": "NKE"}
    identity(valid, {CIK: "NKE"})
    with pytest.raises(ValueError, match="D6_CIK_ACCESSION"):
        identity(valid, {CIK: "OTHER"})
    with pytest.raises(ValueError, match="D6_CIK_ACCESSION"):
        identity(valid | {"accession": "../wrong"}, {CIK: "NKE"})


def test_approval_binds_exact_scope_limits_and_hash(tmp_path):
    draft = {
        "contract": CONTRACT,
        "scope": "example",
        "decision": "PENDING",
        "authorized_by": "",
        "authorized_at": "",
        "provider_terms_evidence": "",
        "retention_rights_evidence": "",
        "backup_restore_evidence": "",
        "limits": {"max_requests": 3, "max_bytes": 64, "requests_per_minute": 30},
    }
    path = tmp_path / "approval.json"
    p = {"authorization_draft": draft}
    raw = json.dumps(draft).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="EXPLICIT_BLOCK_AUTHORIZATION"):
        authorization(p, path, digest(raw))
    result = draft | {
        "decision": "APPROVED",
        "authorized_by": "Synthetic operator",
        "authorized_at": "2026-11-25T12:00:00+00:00",
        "provider_terms_evidence": "fixture",
        "retention_rights_evidence": "fixture",
        "backup_restore_evidence": "fixture",
    }
    raw = json.dumps(result).encode()
    path.write_bytes(raw)
    assert authorization(p, path, digest(raw)) == result
    with pytest.raises(ValueError, match="INPUT_HASH_MISMATCH"):
        authorization(p, path, "0" * 64)
    result["limits"] = result["limits"] | {"max_requests": 4}
    raw = json.dumps(result).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="EXPLICIT_BLOCK_AUTHORIZATION"):
        authorization(p, path, digest(raw))


def fake_document(folder, name, raw):
    from prepare_f01_filing_evidence import filing_base

    target = folder / name
    target.mkdir()
    (target / "content").write_bytes(raw)
    metadata = {
        "filename": name,
        "url": f"{filing_base(FILING)}/{name}",
        "bytes": len(raw),
        "sha256": digest(raw),
        **{k: FILING[k] for k in ("cik", "accession", "filed", "acceptance")},
    }
    (target / "source.json").write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


def test_document_set_binding_detects_consistent_local_replacement(tmp_path):
    listing = json.dumps({"directory": {"item": [{"name": "q.htm"}]}}).encode()
    a = fake_document(tmp_path, "index.json", listing)
    b = fake_document(tmp_path, "q.htm", b"fixture original")
    assert checked_original(tmp_path, FILING, [a, b])["documents"] == [a, b]
    with pytest.raises(ValueError, match="DOCUMENT_SET_NOT_BOUND"):
        checked_original(tmp_path, FILING, [a])
    other = b"rewritten original even with a matching local sidecar"
    (tmp_path / "q.htm/content").write_bytes(other)
    (tmp_path / "q.htm/source.json").write_text(
        json.dumps(b | {"sha256": digest(other), "bytes": len(other)}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="ORIGINAL_PROVENANCE_MISMATCH"):
        checked_original(tmp_path, FILING, [a, b])


def test_cross_lineage_only_exact_fact_and_source():
    from decimal import Decimal

    pair = CIK, ACC
    fact = {
        "cik": CIK,
        "accession_number": ACC,
        "metric": "cash",
        "tag": "Cash",
        "unit": "USD",
        "period_start": None,
        "period_end": "2015-11-30",
        "filed": "2016-01-06",
        "value": "50",
    }
    match = {"source": {"filename": "q.htm", "sha256": "trusted", "cik": CIK, "accession": ACC}}
    inputs = {pair: [("witness", {"fact": fact, "original": {"matches": [match]}})]}
    source = {
        "status": "ORIGINAL_VERIFIED",
        "documents": [{"filename": "q.htm", "sha256": "trusted"}],
    }
    links = {pair: {LINEAGES[1]: {"stored_facts": 1}}}
    assert proof_facts(pair, inputs, source, links, set()) == []
    exact = (*list(fact.values())[:-1], Decimal("50"))
    assert proof_facts(pair, inputs, source, links, {exact})[0]["trace_reference"] == "witness"
    altered = source | {"documents": [{"filename": "q.htm", "sha256": "altered"}]}
    assert proof_facts(pair, inputs, altered, links, {exact}) == []


def test_shares_only_cover_tag_with_bound_instant():
    raw = {"accn": ACC, "filed": "2016-01-06", "form": "10-Q", "end": "2015-12-27", "val": 45}
    meta = {
        ACC: {"filed": "2016-01-06", "form": "10-Q", "acceptance_utc": "2016-01-06T16:00:00+00:00"}
    }
    status, candidate = observed_shares(*COVER, "shares", raw, meta)
    assert status == "COVER_TAG_CANDIDATE" and not candidate["original_pit_qualified"]
    assert (
        observed_shares(
            "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", raw, meta
        )[1]
        is None
    )
    assert observed_shares(*COVER, "shares", raw | {"start": "2015-11-30"}, meta)[1] is None
    assert observed_shares(*COVER, "shares", raw | {"val": 4.5}, meta)[1] is None
    assert observed_shares(*COVER, "shares", raw | {"dimensions": ["ClassA"]}, meta)[1] is None
    draft = candidate | {"cik": CIK, "ordinal": 1}
    assert len(choose_shares([draft, draft | {"ordinal": 2}])[0]) == 1
    assert not choose_shares([draft, draft | {"reported_value": "90"}])[0]


def quarter(i, start, end, acceptance):
    return {
        "id": i,
        "start": start,
        "end": end,
        "filed": acceptance[:10],
        "value": "10",
        "form": "10-Q",
        "report_date": end,
        "acceptance_utc": acceptance,
    }


def test_ttm_uses_four_direct_timed_quarters_and_excludes_ytd():
    rows = [
        quarter(1, "2022-01-01", "2022-03-31", "2022-05-01T12:00:00+00:00"),
        quarter(2, "2022-04-01", "2022-06-30", "2022-08-01T12:00:00+00:00"),
        quarter(3, "2022-07-01", "2022-09-30", "2022-11-01T12:00:00+00:00"),
        quarter(4, "2022-10-01", "2022-12-31", "2023-02-01T12:00:00+00:00"),
    ]
    result = ocf_ttm(rows)[0]
    assert result["candidate_ttm_value"] == "40" and not result["original_pit_qualified"]
    assert ocf_ttm([rows[0], rows[1] | {"start": "2022-01-01"}, *rows[2:]]) == []
    late = rows[1] | {"acceptance_utc": "2023-05-01T12:00:00+00:00"}
    assert ocf_ttm([rows[0], late, *rows[2:]]) == []


def budget(path, **changes):
    limits = {"max_requests": 3, "max_bytes": PAGE_BYTES * 20, "requests_per_minute": 60} | changes
    return Budget(path, {"approved": "yes"}, limits, clock=lambda: 10, sleep=lambda _s: None)


def test_retry_budget_and_restart_charges_uncertain_transfer(tmp_path):
    path = tmp_path / "budget.json"
    b = budget(path)
    b.reserve(200)
    b.finish(14, 200)
    b.reserve(200)
    recovered = budget(path)
    assert recovered.state["requests"] == 2 and recovered.state["received_bytes"] == 14
    assert recovered.state["uncertain_bytes"] == 200
    recovered.reserve(20)
    recovered.finish(0, 403)
    with pytest.raises(ValueError, match="HTTP_REQUEST_BUDGET_EXHAUSTED"):
        recovered.reserve(1)


def test_counted_sec_http_error_and_verified_bytes(tmp_path):
    class Transport:
        def open(self, request, *, timeout):
            if request == "error":
                raise urllib.error.HTTPError("https://www.sec.gov", 429, "retry", {}, None)
            return SimpleNamespace(
                status=200, headers={}, read=io.BytesIO(b"doc").read, close=lambda: None
            )

    b = budget(tmp_path / "budget.json")
    client = CountedSecOpener(Transport(), b)
    with pytest.raises(urllib.error.HTTPError):
        client.open("error", timeout=30)
    with closing(client.open("doc", timeout=30)) as response:
        assert response.read(8) == b"doc"
    assert b.state["statuses"] == {"429": 1, "200": 1}
    assert b.state["received_bytes"] == 3


def test_alpaca_denial_or_unverified_retry_after_is_persistent(tmp_path):
    class Response:
        headers = {}
        status_code = 429

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    sdk = SimpleNamespace(
        _session=SimpleNamespace(get=lambda *_, **__: Response()), _get_default_headers=dict
    )
    path = tmp_path / "budget.json"
    with pytest.raises(ValueError, match="ALPACA_429"):
        alpaca_fetch(sdk, {}, budget(path))
    with pytest.raises(ValueError, match="PROVIDER_RATE_LIMIT_MANUAL_REVIEW_REQUIRED"):
        alpaca_fetch(sdk, {}, budget(path))


def spy_page(day, token=None):
    return json.dumps(
        {
            "bars": {
                "SPY": [{"t": f"{day}T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 10}]
            },
            "next_page_token": token,
        }
    ).encode()


def test_spy_pagination_resume_same_variant_and_quarantine(tmp_path):
    days = ["2016-01-04", "2016-01-05"]
    scope = {"network_session_windows": [days]}
    requested = []

    def first(query):
        requested.append(query)
        if query.get("page_token"):
            raise ValueError("interrupted")
        return spy_page(days[0], "next")

    with pytest.raises(ValueError, match="interrupted"):
        spy_download(tmp_path, scope, first)
    assert requested[0]["feed"] == "sip" and requested[0]["adjustment"] == "all"
    requested.clear()

    def final(query):
        requested.append(query)
        return spy_page(days[1])

    assert spy_download(tmp_path, scope, final)["sessions_verified"] == days
    assert len(requested) == 1 and requested[0]["page_token"] == "next"
    with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
        parse_spy(spy_page("2016-01-06"), days, set())
    with pytest.raises(ValueError, match="DUPLICATE"):
        parse_spy(spy_page(days[0]), days, {days[0]})


def test_issuer_epoch_and_early_spy_never_authorize(monkeypatch):
    import f01_task96_network as net

    monkeypatch.setattr(net, "current_code", lambda: "test")
    market = {"start": "2009-01-05", "end": "2009-01-05", "provider": "UNRESOLVED", "sessions": 1}
    plan = SimpleNamespace(
        blocks={"p": ("SPY", market), "i": ("ISSUER_DAILY", market)},
        binding=lambda: {"bound": True},
    )
    assert scope_for(plan, "p", spy=set())["status"] == "BLOCKED_PRE2016_PROVIDER_UNRESOLVED"
    assert "SECURITY_EPOCH" in scope_for(plan, "i")["status"]


def test_dts_discovery_retries_before_accounting_and_match(tmp_path, monkeypatch):
    import f01_task96_process as processor

    monkeypatch.setattr(processor, "SHARD_ROOT", tmp_path)
    calls = SimpleNamespace(value=0)
    monkeypatch.setattr(
        processor,
        "closure_for",
        lambda *a: {"http://www.xbrl.org/schema.xsd": {"status": "BOUND"}},
    )
    monkeypatch.setattr(processor.task9.filing, "verified_sec_plugin", lambda _: True)
    monkeypatch.setattr(
        processor.task9.filing,
        "ArelleProcessor",
        lambda _: SimpleNamespace(release={"version": "2.47.5"}, run=lambda **_: {}),
    )

    def extract_shard(job, *_):
        calls.value += 1
        if calls.value == 1:
            raise processor.task9.bulk.Blocked("EXTRACTION_OR_DTS_INCOMPLETE")
        (job / f"{ACC}-extraction.json").write_bytes(b"shard")
        return {"observations": []}

    monkeypatch.setattr(processor.task9, "extract_filing", extract_shard)
    monkeypatch.setattr(processor.task9.filing, "cache_read", lambda *a: (b"{}", {}))
    monkeypatch.setattr(processor.task9.filing, "documents", lambda *a: [])
    monkeypatch.setattr(processor.auto, "evaluate_filing", lambda *a: {"x": "fixture"})
    monkeypatch.setattr(
        processor.auto, "batch_report", lambda *a: {"summary": {"facts_admitted": 0}}
    )
    db = SimpleNamespace(execute=lambda *a: [])
    resolver = SimpleNamespace(arelle=tmp_path, materialize=lambda *_: None)
    result = extract(FILING, {"directory": str(tmp_path)}, "block", "candidate", resolver, db)
    assert calls.value == 2 and result["status"] == "ACCOUNTING_REVIEW_REQUIRED"
    assert result["pit"] == "PIT_EVIDENCE_INCOMPLETE"


def test_pinned_audit_does_not_mutate_sqlite_or_sidecars(tmp_path):
    path = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sample (id INT)")
    raw = path.read_bytes()
    with read_database(path, {}, digest(raw)) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("DROP TABLE sample")
    assert path.read_bytes() == raw and list(tmp_path.iterdir()) == [path]


def test_download_index_cannot_claim_another_cik_or_accession():
    from prepare_f01_filing_evidence import filing_base

    name = filing_base(FILING).removeprefix("https://www.sec.gov")
    verify_sec_listing({"directory": {"name": name}}, FILING)
    changed = name.rsplit("/", 1)[0] + "/wrong"
    with pytest.raises(ValueError, match="CIK_OR_ACCESSION"):
        verify_sec_listing({"directory": {"name": changed}}, FILING)


def test_spy_execution_narrows_to_current_missing_sessions_once(tmp_path):
    days = ["2016-01-04", "2016-01-05", "2016-01-06"]
    scope = {"network_session_windows": [days], "authorization_draft": {"scope": "bound"}}
    first = spy_execution_scope(tmp_path, scope, lambda: {days[1]})
    assert first["network_session_windows"] == [[days[0]], [days[2]]]
    assert first["locally_reused_after_approval"] == [days[1]]
    assert spy_execution_scope(tmp_path, scope, lambda: set()) == first
