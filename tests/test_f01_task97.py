"""Focused offline tests for the historical backfill orchestration."""

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_f01_task97 as runner  # noqa: E402
from f01_task96_network import replace_checkpoint  # noqa: E402
from f01_task97_taxonomy import TaxonomyBatch, edition, package_options  # noqa: E402


def test_bound_scope_pilot_and_amendment_exclusion():
    def rows(start, count):
        return [
            {
                "cik": f"{index % 50:010}",
                "accession": f"s-{index}",
                "filed": f"{2010 + index % 16}-12-31",
                "form": "10-K",
                "symbol_label": f"I{index % 50}",
            }
            for index in range(start, start + count)
        ]

    pilot = [{"cik": pair[0], "accession": pair[1]} for pair in sorted(runner.PILOT_KEYS)]
    plan = SimpleNamespace(
        groups={
            1: [{"block_id": "archived", "accessions": rows(0, 339)}],
            5: [{"block_id": "new", "accessions": rows(339, 2583) + pilot}],
            6: [{"block_id": "review", "accessions": rows(2922, 28)}],
        }
    )
    # Include COST/ORCL inside the same synthetic 50-member cohort.
    for group in plan.groups.values():
        for row in group[0]["accessions"]:
            if row["cik"] in {"0000000000", "0000000001"}:
                row["cik"] = sorted(runner.PILOT_KEYS)[int(row["cik"])][0]
    scope = runner.scope_for(plan)
    sample = runner.select_pilot(scope)
    assert len(sample) == len(set(sample)) == 24
    assert len({pair[0] for pair in sample}) >= 20
    assert all(scope[pair]["scope"] == "BACKFILL" for pair in sample)
    assert sample == runner.select_pilot(scope)
    plan.groups[6][0]["accessions"].pop()
    with pytest.raises(ValueError, match="TASK97_PLAN_SCOPE_CHANGED"):
        runner.scope_for(plan)


def test_pilot_gate_paces_systemic_issues_without_requiring_every_filing():
    pairs = [("x", str(i)) for i in range(24)]
    result = {
        pair: {
            "original_status": "ORIGINAL_VERIFIED",
            "dts_status": "DTS_COMPLETE",
            "extraction_status": "EXTRACTION_COMPLETE",
        }
        for pair in pairs[:22]
    }
    assert runner.pilot_gate(pairs, result, {"200": 98, "404": 2})["passed"]
    assert not runner.pilot_gate(pairs, result, {"200": 70, "403": 30})["passed"]
    for pair in pairs[10:]:
        result.pop(pair, None)
    assert not runner.pilot_gate(pairs, result, {"200": 100})["passed"]


@pytest.mark.parametrize("closed", [True, False])
def test_scheduler_processes_pilot_before_releasing_bulk(tmp_path, monkeypatch, closed):
    pairs = [("1", f"a{i:02}") for i in range(27)]
    job = runner.Backfill.__new__(runner.Backfill)
    job.root, job.run, job.dirs = tmp_path, tmp_path / "run", {"originals": tmp_path}
    job.run.mkdir()
    job.pilot, job.scope_hash = pairs[:24], "fixture"
    job.scope = {p: {"scope": "BACKFILL", "filed": "2012-10-22"} for p in pairs}
    job.records = {pair: {"original_status": "ORIGINAL_MISSING"} for pair in pairs}
    job.metadata = dict(job.scope)
    job.originals, job.sizes, job.batches, job.taxonomy_events = {}, [], [], []
    job.budget = SimpleNamespace(state={"statuses": {}, "received_bytes": 0})
    steps = []

    def acquisition(pending):
        steps.append(("acquire", len(pending)))
        for pair in pending:
            job.originals[pair] = {"fixture": True}
            job.records[pair]["original_status"] = "ORIGINAL_VERIFIED"

    def process(pending, cache):
        steps.append(("process", len(pending)))
        if closed:
            for pair in pending:
                job.records[pair].update(
                    dts_status="DTS_COMPLETE", extraction_status="EXTRACTION_COMPLETE"
                )

    monkeypatch.setattr(job, "initialize", lambda: None)
    monkeypatch.setattr(job, "acquisition", acquisition)
    monkeypatch.setattr(job, "processing", process)
    monkeypatch.setattr(job, "progress", lambda *_: None)
    monkeypatch.setattr(runner.tax, "safe_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "capacity", lambda *_: {"batch_capacity": 24})
    status, _ = job.run_batches()
    first = [("acquire", 24), ("process", 24)]
    if closed:
        assert status == "COMPLETE"
        assert steps == first + [("acquire", 3), ("process", 3)]
    else:
        assert status == "PARTIAL_BLOCKED"
        assert steps == first


def test_guard_rejects_clock_anomaly_and_preserves_existing_limiter(tmp_path):
    limiter = tmp_path / "limiter.json"
    original = {"until": 1020, "blocked": None}
    runner.publish(limiter, original)
    clock = tmp_path / "clock.json"
    runner.publish(clock, {"wall": 1000.0, "monotonic": 70.0})
    transport = SimpleNamespace(state=limiter)
    with pytest.raises(ValueError, match="SEC_CLOCK_ANOMALY"):
        runner.NetworkGuard(clock, transport, wall=lambda: 999, monotonic=lambda: 71).check()
    with pytest.raises(ValueError, match="SEC_CLOCK_ANOMALY"):
        runner.NetworkGuard(clock, transport, wall=lambda: 1015, monotonic=lambda: 71).check()
    runner.NetworkGuard(clock, transport, wall=lambda: 1010, monotonic=lambda: 2).check()
    assert json.loads(limiter.read_bytes()) == original
    replace_checkpoint(limiter, {"until": 1020, "blocked": "UNVERIFIED_RETRY_AFTER"})
    with pytest.raises(ValueError, match="SEC_LIMITER_MANUAL_REVIEW"):
        runner.NetworkGuard(clock, transport, wall=lambda: 1012, monotonic=lambda: 4).check()


def test_storage_capacity_reserves_actual_peak(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _: SimpleNamespace(free=runner.RESERVE))
    assert runner.capacity(tmp_path, tmp_path, [100])["batch_capacity"] == 0
    monkeypatch.setattr(
        runner.shutil, "disk_usage", lambda _: SimpleNamespace(free=30 * runner.RESERVE)
    )
    small = runner.capacity(tmp_path, tmp_path, [100])
    large = runner.capacity(tmp_path, tmp_path, [50 * runner.MIB])
    assert small["batch_capacity"] == 40 and large["per_filing_bytes"] == 150 * runner.MIB


@pytest.mark.parametrize(
    "status, expected",
    [(401, "PROVIDER_BLOCKED"), (403, "PROVIDER_BLOCKED"), (404, None), (429, "PROVIDER_BLOCKED")],
)
def test_existing_sec_transport_status_journal(tmp_path, status, expected):
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000100000001/index.json"
    budget = runner.Budget(tmp_path / "budget.json", {}, runner.LIMITS, sleep=lambda _: None)

    def reject(request, **kwargs):
        headers = Message()
        headers["Retry-After"] = "15"
        raise urllib.error.HTTPError(url, status, "fixture", headers, io.BytesIO())

    transport = runner.FilingJournal(tmp_path, SimpleNamespace(open=reject), budget)
    opener = runner.CountedSecOpener(transport, budget)
    with pytest.raises(urllib.error.HTTPError):
        opener.open(urllib.request.Request(url), timeout=1)
    assert budget.state["statuses"] == {str(status): 1}
    assert json.loads((tmp_path / "req-01.json").read_bytes())["retry_after"] == "15"
    assert runner.pause_status(f"SEC_HTTP_{status}") == expected


def test_sec_document_resume_reuses_index_after_partial_filing(tmp_path, monkeypatch):
    filing = runner.task9.filing
    row = {
        "cik": "0000000001",
        "accession": "0000000001-12-000001",
        "filed": "2012-12-21",
        "acceptance": "2012-12-21T21:00:00Z",
    }
    base = filing.filing_base(row)
    calls = []
    ready = False

    def mock_document(client, root, target_row, name):
        directory = root.joinpath(*runner.key(target_row), name)
        if directory.exists():
            return filing.cache_read(directory, target_row, name)
        calls.append(name)
        if name != "index.json" and not ready:
            raise ValueError(f"SEC_HTTP_404: {base}/{name}")
        data = (
            json.dumps({"directory": {"name": base.removeprefix("https://www.sec.gov")}}).encode()
            if name == "index.json"
            else b"<xbrl/>"
        )
        directory.mkdir(parents=True)
        (directory / "content").write_bytes(data)
        meta = row | {
            "filename": name,
            "url": f"{base}/{name}",
            "bytes": len(data),
            "sha256": runner.task9.sha(data),
        }
        runner.publish(directory / "source.json", meta)
        return data, meta

    monkeypatch.setattr(filing, "get_document", mock_document)
    monkeypatch.setattr(filing, "documents", lambda *_: ["instance.xml"])
    monkeypatch.setattr(filing, "taxonomy_files", lambda *_: [])
    with pytest.raises(ValueError, match="SEC_HTTP_404"):
        runner.download_sec_filing(None, tmp_path, row)
    ready = True
    result = runner.download_sec_filing(None, tmp_path, row)
    assert calls == ["index.json", "instance.xml", "instance.xml"]
    assert len(result["documents"]) == 2
    assert runner.download_sec_filing(None, tmp_path, row) == result
    assert len(calls) == 3


def test_new_version_package_and_wrong_namespace_quarantine():
    uri = "http://xbrl.fasb.org/us-gaap/2018/elts/us-gaap-2018-01-31.xsd"
    wrong = uri.replace("2018-01-31", "2024-01-31")
    assert edition(wrong) is None
    assert edition(uri) == ("xbrl.fasb.org", "us-gaap", "2018")
    assert edition("http://xbrl.sec.gov/dei/2021q4/dei-2021q4.xsd") == (
        "xbrl.sec.gov",
        "dei",
        "2021q4",
    )
    namespace = "http://fasb.org/us-gaap/2018-01-31"
    options = package_options(uri, {namespace})
    endpoint = next(iter(options))
    manager = TaxonomyBatch.__new__(TaxonomyBatch)
    manager.packages = options

    def archive(ns):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr(
                "elts/us-gaap-2018-01-31.xsd",
                f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{ns}"/>',
            )
        return buffer.getvalue()

    entries = manager.entries(endpoint, archive(namespace), {uri: {namespace}})
    assert entries[uri][1]["namespace"] == namespace
    with pytest.raises(ValueError, match="IMPORT_NAMESPACE_MISMATCH"):
        manager.entries(endpoint, archive(namespace.replace("2018", "2024")), {uri: {namespace}})
    with pytest.raises(zipfile.BadZipFile):
        manager.entries(endpoint, b"corrupt", {uri: {namespace}})


def test_fiscal_coverage_and_immutable_snapshot(tmp_path):
    reporting = runner.reporting
    concept = "{http://fasb.org/us-gaap/2018-01-31}Assets"
    ob = {
        "concept": concept,
        "value": "42",
        "unit": "USD",
        "source": {"sha256": "fixture"},
        "context": {"instant": "2018-01-01"},
    }
    year = {
        "concept": "{http://xbrl.sec.gov/dei/2018-01-31}DocumentFiscalYearFocus",
        "text_value": "2018",
    }
    shard = {"observations": [ob, year]}
    summary = reporting.shard_summary(shard, technical=True, accounting={})
    assert summary["fiscal_year"] == "2018"
    assert summary["metrics"]["total_assets"] == {
        "raw": 1,
        "technical": 1,
        "accounting": 0,
        "pit": 0,
    }
    pair = ("1", "sample")
    record = {pair: {"original_status": "ORIGINAL_VERIFIED", "summary": summary}}
    scope = {
        pair: {
            "cik": "1",
            "accession": "sample",
            "symbol_label": "X",
            "filed": "2018-02-01",
            "acceptance_utc": None,
            "form": "10-Q",
            "scope": "BACKFILL",
        }
    }
    binding = {"scope_fingerprint": "fixture"}
    a = reporting.report(tmp_path, binding, scope, record, {}, "PARTIAL_BLOCKED", None)
    b = reporting.report(tmp_path, binding, scope, record, {}, "PARTIAL_BLOCKED", None)
    assert a == b
    path = Path(a["directory"]) / "fundamental_fact_coverage.csv"
    assert "X,2018,total_assets,1,1,0,0" in path.read_text()


def test_resume_uses_individual_bound_original_without_transport(tmp_path, monkeypatch):
    pair = ("1", "acc")
    state = tmp_path / "1-acc.json"
    source = {"directory": str(tmp_path), "documents": []}
    item = {"extraction_status": "EXTRACTION_COMPLETE", "source_sha256": runner.fingerprint(source)}
    scope = {"scope_fingerprint": "fixture", "code_fingerprint": "scope"}
    runner.publish(state, {"binding": scope, "filing": list(pair), "record": item})
    job = runner.Backfill.__new__(runner.Backfill)
    job.dirs, job.binding, job.scope_hash = {"state": tmp_path}, scope, "fixture"
    job.records, job.originals = {pair: {}}, {pair: {"source": source}}
    resumed = []
    monkeypatch.setattr(job, "attach_processing", lambda p, evidence: resumed.append((p, evidence)))
    job.resume_filing(pair)
    assert resumed == [(pair, item)]
    source["documents"].append({"sha256": "changed"})
    with pytest.raises(ValueError, match="CHECKPOINT_ORIGINAL_BINDING_CHANGED"):
        job.resume_filing(pair)
