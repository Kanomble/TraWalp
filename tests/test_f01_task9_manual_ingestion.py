"""Task 9 offline contracts and end-to-end SQLite fixture."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import f01_task9_admission as review  # noqa: E402
import run_f01_historical_50_manual as pipeline  # noqa: E402

from trading_system.data import canonical_bootstrap as clean  # noqa: E402
from trading_system.data.canonical_facts import require_contract  # noqa: E402

ROW = {
    "cik": "0000320187",
    "accession": "0000320187-16-000242",
    "filed": "2016-01-06",
    "form": "10-Q",
    "acceptance": "2016-01-06T22:17:58Z",
    "report_date": "2015-11-30",
    "primary": "nke-11302015x10q.htm",
    "symbol": "NKE",
    "registrant": "Synthetic reporting issuer",
}
KEY = review.filing_key(ROW)
CODE = "a" * 64
SELECTION_SHA = "b" * 64
BINDING = pipeline.review_binding(SELECTION_SHA, "c" * 64, "d" * 64)


def observed(source, value="123", ordinal=1):
    return {
        "concept": "{http://fasb.org/us-gaap/2015-01-31}Assets",
        "context": {
            "entity": ROW["cik"],
            "scheme": "https://www.sec.gov/CIK",
            "dimensions": [],
            "opaque_context": False,
            "instant": ROW["report_date"],
            "startDate": None,
            "endDate": None,
        },
        "unit": {"numerator": [f"{{{pipeline.filing.ISO}}}USD"], "denominator": []},
        "value": value,
        "is_nil": False,
        "source": source,
        "document_ordinal": ordinal,
        "relationships": [],
    }


def approved(template):
    return template | {
        "decision": "APPROVED",
        "reviewer": "Synthetic test only",
        "reviewed_at": "2026-11-24T18:00:00Z",
    }


def approvals(shard, binding):
    src = approved(
        pipeline.template(
            review.SOURCE,
            binding,
            {
                KEY: {
                    "decision": "APPROVED",
                    "issuer_identity": "VERIFIED_REPORTING_ENTITY",
                    "completeness": "VERIFIED",
                    "amendment": "ORIGINAL_FILING",
                    "reference": "Synthetic source",
                    "source_memberships": ["us-gaap"],
                },
            },
        )
    )
    acct = approved(
        pipeline.template(
            review.ACCOUNTING,
            binding | {"source_review_sha256": review.sha(review.json_bytes(src))},
            {KEY: pipeline.accounting_candidates(shard, ROW)},
        )
    )
    for decision in acct["filings"][KEY]["facts"].values():
        decision.update(
            decision="APPROVED",
            scope="CONSOLIDATED",
            version="AS_FILED",
            reference="Synthetic accounting proof",
        )
    return src, acct


def test_select_rejects_unbound_accessions_identity_and_amendment():
    row = ROW | {"filing_index_conflicted": False}
    parent = {"accessions": [row]}
    inv = {
        "NKE": {"cik": ROW["cik"], "filings": {ROW["accession"]: row}, "filing_index_conflicts": []}
    }
    assert pipeline.selected_rows(parent, inv, [ROW["accession"]]) == [row]
    with pytest.raises(ValueError, match="OUTSIDE"):
        pipeline.selected_rows(parent, inv, ["0000000000-16-000001"])
    with pytest.raises(ValueError, match="ONE_TO_FOUR"):
        pipeline.selected_rows(parent, inv, [ROW["accession"]] * 2)
    with pytest.raises(ValueError, match="UNRESOLVED"):
        pipeline.selected_rows(
            parent, {"NKE": inv["NKE"] | {"cik": "0000000001"}}, [ROW["accession"]]
        )
    amendment = row | {"form": "10-Q/A"}
    with pytest.raises(ValueError, match="AMENDMENT"):
        pipeline.selected_rows(
            {"accessions": [amendment]},
            {
                "NKE": inv["NKE"]
                | {
                    "filings": {ROW["accession"]: amendment},
                }
            },
            [ROW["accession"]],
        )


@pytest.fixture
def cache(tmp_path):
    job = tmp_path / "job"
    root = job / "originals"
    index = {"directory": {"item": [{"name": ROW["primary"]}]}}
    content = {
        ROW["primary"]: b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"/>',
        "index.json": json.dumps(index).encode(),
    }
    root.mkdir(parents=True)

    class Response(io.BytesIO):
        status = 200

        def __init__(self, data):
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}

    def open_url(url, method):
        assert url.startswith(pipeline.filing.filing_base(ROW))
        assert method == "GET"
        return Response(content[url.rsplit("/", 1)[-1]])

    pipeline.filing.acquire([ROW], root, SimpleNamespace(open_url=open_url))
    return job


def test_sources_bound_and_verified_with_tamper_detection(cache):
    selection = {"filings": [ROW], "source_generation": "0" * 64}
    result = pipeline.verify_sources(cache, selection, SELECTION_SHA)
    assert result["status"] == "SOURCE_VERIFIED"
    folder = Path(result["filings"][KEY]["directory"])
    content = folder / ROW["primary"] / "content"
    content.write_bytes(b"x" * content.stat().st_size)
    with pytest.raises(ValueError, match="CACHE_SHA256"):
        pipeline.verify_sources(cache, selection, SELECTION_SHA)


def test_missing_dependencies_and_extraction_resume(cache):
    selection = {"filings": [ROW], "source_generation": "0" * 64}
    source = pipeline.verify_sources(cache, selection, SELECTION_SHA)["filings"][KEY]
    assert (
        pipeline.taxonomy.coverage({"https://www.xbrl.org/not-present.xsd"}, lambda url: None)[
            "https://www.xbrl.org/not-present.xsd"
        ]["status"]
        == "MISSING_EVIDENCE"
    )
    state = {"broken": True, "runs": 0}

    def processor_run(**request):
        state["runs"] += 1
        doc = request["document"]
        if state["broken"]:
            return {
                "issues": [{"code": "IOerror", "reason": "offline missing.xsd"}],
                "observations": [],
                "taxonomy_documents": [],
            }
        item = observed(None) | {"source_document": doc}
        dependency = {"resolved_path": doc, "uri": doc, "sha256": source["documents"][1]["sha256"]}
        return {"issues": [], "observations": [item], "taxonomy_documents": [dependency]}

    processor = SimpleNamespace(
        directory=cache, cache=cache / "tax", release={"version": "2.47.5"}, run=processor_run
    )
    with pytest.raises(ValueError, match="EXTRACTION_OR_DTS_INCOMPLETE"):
        pipeline.extract_filing(cache, ROW, source, {}, CODE, processor)
    assert not (cache / f"{ROW['accession']}-extraction.json").exists()
    assert list(cache.glob("*quarantine*.json"))
    state["broken"] = False
    shard = pipeline.extract_filing(cache, ROW, source, {}, CODE, processor)
    assert shard["status"] == "EXTRACTION_COMPLETE"
    assert pipeline.extract_filing(cache, ROW, source, {}, CODE, processor) == shard
    assert state["runs"] == 2


@pytest.fixture
def shard():
    source = {"filename": ROW["primary"], "sha256": "0" * 64}
    return {
        "status": "EXTRACTION_COMPLETE",
        "source_documents": [source],
        "observations": [observed(source), observed(source, ordinal=2)],
    }


def test_separate_reviews_schema_and_expected_values(shard):
    source, accounting = approvals(shard, BINDING)
    facts = review.admitted_facts(ROW, shard, source, accounting, BINDING)
    assert len(facts) == 2  # Physical provenance remains distinct in the shard.
    assert len({fact for fact, _ in facts.values()}) == 1
    with pytest.raises(ValueError, match="ADMISSION_REQUIRED"):
        review.admitted_facts(ROW, shard, source | {"decision": "UNREVIEWED"}, accounting, BINDING)
    with pytest.raises(ValueError, match="ADMISSION_REQUIRED"):
        review.admitted_facts(ROW, shard, source, accounting | {"decision": "BLOCKED"}, BINDING)
    with pytest.raises(ValueError):
        review.SourceReview.model_validate(source | {"unknown": 1})
    with pytest.raises(ValueError, match="EXTRACTION_INCOMPLETE"):
        review.admitted_facts(
            ROW, shard | {"status": "TAXONOMY_PENDING"}, source, accounting, BINDING
        )
    next(iter(accounting["filings"][KEY]["facts"].values()))["expected_value"] = "124"
    with pytest.raises(ValueError, match="EXPECTED_VALUE"):
        review.admitted_facts(ROW, shard, source, accounting, BINDING)


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "f01-sec-isolated-research-test" / clean.DB_NAME
    raw = pipeline.d6.json_bytes(
        {
            "contract": clean.BOOTSTRAP_CONTRACT,
            "classification": clean.CLASSIFICATION,
            "research_scope": "Task 9 synthetic fixture",
        }
    )
    digest = review.sha(raw)
    clean.bootstrap(
        path,
        manifest=raw,
        manifest_sha256=digest,
        expected_code_fingerprint=CODE,
        verify_code=lambda: CODE,
        authorized=True,
    )
    return path, digest


def test_filing_atomic_receipt_resume_and_e13_boundary(target, shard):
    path, digest = target
    source, accounting = approvals(shard, BINDING)
    facts = review.admitted_facts(ROW, shard, source, accounting, BINDING)
    binding = BINDING | {"retrieved_at": "2026-11-24T10:00:00Z"}
    args = (path, path, digest, CODE)
    assert review.execute(*args, [(ROW, facts, binding)])["receipts"][0]["existing_receipt"] is None

    conflicts = facts | {
        "conflicting-witness": (
            next(iter(facts.values()))[0].model_copy(update={"value": 124}),
            "synthetic",
        )
    }
    with pytest.raises(ValueError, match="UNRESOLVED_CONTENT_CONFLICT"):
        review.execute(*args, [(ROW, conflicts, binding)], write=True)
    with review.open_target(path, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM ingestion_receipt").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM canonical_fact").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM fact_dataset").fetchone() == (1,)

    first = review.execute(*args, [(ROW, facts, binding)], write=True)["receipts"][0]
    later = ROW | {
        "accession": "0000320187-16-000264",
        "filed": "2016-04-05",
        "acceptance": "2016-04-05T20:14:43Z",
    }
    newer = {
        key: (
            fact.model_copy(
                update={
                    "accession_number": later["accession"],
                    "filed": datetime.fromisoformat(later["filed"]).date(),
                }
            ),
            reference,
        )
        for key, (fact, reference) in facts.items()
    }
    assert (
        review.execute(*args, [(later, newer, binding)], write=True)["receipts"][0][
            "dataset_version"
        ]
        == 3
    )
    replay = review.execute(*args, [(ROW, facts, binding)], write=True)["receipts"][0]
    assert first | {"replayed": True} == replay
    with pytest.raises(ValueError, match="RECEIPT_INPUT_CONFLICT"):
        review.execute(*args, [(ROW, facts, binding | {"changed_payload": "x"})], write=True)
    with review.open_target(path, read_only=True) as connection:
        require_contract(connection, writer=False)
        assert connection.execute("SELECT count(*) FROM canonical_fact").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM source_occurrence").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM ingestion_receipt").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM fact_dataset").fetchone() == (3,)
        assert review.reader_boundary(connection, ROW, facts, 2)["matched_original_facts"] == 1
        with pytest.raises(ValueError, match="EXACT_ORIGINAL"):
            review.reader_boundary(connection, ROW, facts, 1)


def test_target_guards_and_bootstrap_reuse_refusal(target, monkeypatch):
    path, digest = target
    with pytest.raises(ValueError, match="TARGET_ALREADY_EXISTS"):
        clean.preflight(
            path,
            manifest=(path.parent / "bootstrap_manifest.json").read_bytes(),
            manifest_sha256=digest,
            code_fingerprint=CODE,
        )

    def fail(*args, **kwargs):
        pytest.fail("Wrong target opened")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(ValueError, match="EXACT_TARGET"):
        review.execute(pipeline.d6.REPO / "data/trading_system.sqlite3", path, digest, CODE, [])


def test_persisted_rate_limit_cannot_be_reset_by_client_restart(tmp_path):
    now = [1_000.0]
    state = tmp_path / "limiter.json"

    def fail(request, *, timeout):
        raise urllib.error.HTTPError("https://www.sec.gov", 429, "", {"Retry-After": "120"}, None)

    opener = pipeline.PersistedSecOpener(state, SimpleNamespace(open=fail), clock=lambda: now[0])
    with pytest.raises(urllib.error.HTTPError):
        opener.open(object(), timeout=1)
    after = pipeline.PersistedSecOpener(
        state,
        SimpleNamespace(open=lambda *a, **k: "started"),
        clock=lambda: now[0],
        pause=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    with pytest.raises(ValueError, match="LIMITER_MANUAL_REVIEW"):
        after.open(object(), timeout=1)
    now[0] = 1_121
    assert after.open(object(), timeout=1) == "started"


def test_cli_gates_import_and_reader_with_synthetic_inputs(cache, monkeypatch, capsys):
    job = cache
    selection = {
        "filings": [ROW],
        "candidate": {"fingerprint": CODE},
        "universe_sha256": "a" * 64,
        "source_generation": "b" * 64,
    }
    monkeypatch.setattr(pipeline, "build_selection", lambda values: selection)
    monkeypatch.setattr(pipeline, "job_path", lambda path: job)
    selection_path = job / "selection.json"
    selection_sha = pipeline.publish(selection_path, selection)
    common = [
        "--job",
        str(job),
        "--selection",
        str(selection_path),
        "--selection-sha256",
        selection_sha,
    ]

    def invoke(phase, *options):
        assert pipeline.main([phase, *common, *options]) == 0
        return json.loads(capsys.readouterr().out)

    acquisition = invoke("download")
    assert acquisition["authorization_template"]["decision"] == "UNREVIEWED"
    assert pipeline.main(["download", *common, "--execute"]) == 3
    assert "APPROVAL_FILE" in capsys.readouterr().err

    verified = invoke("verify", "--execute")
    source_sha = verified["manifest_sha256"]
    source = verified["filings"][KEY]["documents"]
    shard = {
        "filing": KEY,
        "status": "EXTRACTION_COMPLETE",
        "observations": [observed(source[1])],
        "source_documents": source,
    }
    shard_sha = pipeline.publish(job / f"{ROW['accession']}-extraction.json", shard)
    extraction_sha = pipeline.publish(
        job / "extraction-manifest.json",
        {
            "contract": "F01_TASK9_EXTRACTION_MANIFEST_V1",
            "status": "EXTRACTION_COMPLETE",
            "code_fingerprint": CODE,
            "source_sha256": source_sha,
            "filings": {KEY: shard_sha},
        },
    )
    evidence = ["--source-sha256", source_sha, "--extraction-sha256", extraction_sha]
    assert invoke("source-template", *evidence)["decision"] == "UNREVIEWED"
    source_review, accounting = approvals(
        shard,
        pipeline.review_binding(selection_sha, source_sha, extraction_sha),
    )
    src_path = job / "source-review.json"
    acct_path = job / "accounting-review.json"
    evidence += [
        "--source-review",
        str(src_path),
        "--source-review-sha256",
        pipeline.publish(src_path, source_review),
    ]
    assert invoke("accounting-template", *evidence)["decision"] == "UNREVIEWED"
    evidence += [
        "--accounting-review",
        str(acct_path),
        "--accounting-review-sha256",
        pipeline.publish(acct_path, accounting),
    ]
    database = job / "f01-sec-isolated-research-test" / clean.DB_NAME
    monkeypatch.setattr(pipeline.d6, "TARGET", database)
    boot = invoke("bootstrap-manifest", *evidence)
    boot_file = job / "bootstrap.json"
    boot_sha = pipeline.publish(boot_file, boot)
    assert len(boot["research_scope"]) <= 1024
    clean.bootstrap(
        database,
        manifest=boot_file.read_bytes(),
        manifest_sha256=boot_sha,
        expected_code_fingerprint=CODE,
        verify_code=lambda: CODE,
        authorized=True,
    )
    evidence += ["--bootstrap-manifest", str(boot_file), "--bootstrap-sha256", boot_sha]
    preflight = invoke("import", *evidence)
    assert preflight["status"] == "IMPORT_READY"
    assert not preflight["receipts"][0]["existing_receipt"]
    approval = job / "import-approval.json"
    approval_sha = pipeline.publish(approval, approved(preflight["authorization_template"]))
    flags = ["--execute", "--approval", str(approval), "--approval-sha256", approval_sha]
    first = invoke("import", *evidence, *flags)
    replay = invoke("import", *evidence, *flags)
    assert first["receipts"][0] | {"replayed": True} == replay["receipts"][0]
    assert invoke("reader", *evidence, "--execute")["status"] == "VALIDATION_PASSED"


def test_append_requires_disjoint_selection_and_existing_initial_receipts(target):
    path, digest = target
    selection = {
        "universe_sha256": CODE,
        "source_generation": CODE,
        "candidate": {"fingerprint": CODE},
        "filings": [ROW],
    }
    bootstrap = pipeline.bootstrap_manifest(selection, BINDING)
    other = ROW | {"accession": "0000320187-16-000264"}
    proposed = pipeline.bootstrap_manifest(selection | {"filings": [other]}, BINDING)
    predecessor = pipeline.compatible_bootstrap(bootstrap, proposed, True)
    with pytest.raises(ValueError, match="APPEND_REQUIRES_DISJOINT"):
        pipeline.compatible_bootstrap(bootstrap, bootstrap, True)
    with pytest.raises(ValueError, match="INITIAL_REVIEW_BATCH"):
        review.execute(path, path, digest, CODE, [], predecessors=predecessor)


def test_native_rollback_journal_resume_on_exact_synthetic_target(target, shard):
    import subprocess

    path, digest = target
    child = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA cache_size=3")
c.execute("BEGIN IMMEDIATE")
c.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?)",
              [(f"{i:010d}", f"X{i}", "x"*3000, None, None, "test") for i in range(200)])
os._exit(17)
"""
    assert subprocess.run([sys.executable, "-B", "-c", child, str(path)]).returncode == 17
    assert path.with_name(path.name + "-journal").exists()
    args = (path, path, digest, CODE)
    with pytest.raises(ValueError, match="ROLLBACK_JOURNAL"):
        review.execute(*args, [])
    src, acct = approvals(shard, BINDING)
    facts = review.admitted_facts(ROW, shard, src, acct, BINDING)
    receipt = review.execute(
        *args,
        [(ROW, facts, BINDING | {"retrieved_at": "2026-11-24T10:00:00Z"})],
        write=True,
    )["receipts"][0]
    assert receipt["dataset_version"] == 2
    with review.open_target(path, read_only=True) as connection:
        assert connection.execute("SELECT cik FROM companies").fetchall() == [(ROW["cik"],)]


def test_task9_with_pinned_arelle_worker(tmp_path):
    import os

    from test_prepare_f01_filing_evidence import INLINE, LINKBASE, SCHEMA

    site = os.getenv("F01_ARELLE_TEST_SITE")
    if not site:
        pytest.skip("Arelle fixture runtime requires F01_ARELLE_TEST_SITE")
    processor = pipeline.filing.ArelleProcessor.__new__(pipeline.filing.ArelleProcessor)
    processor.directory = tmp_path
    processor.site = Path(site)
    processor.cache = tmp_path / "runtime-cache"
    processor.cache.mkdir()
    processor.packages = []
    processor.sec_plugin = os.getenv("F01_ARELLE_TEST_SEC_PLUGIN")
    processor.release = processor.run(probe=True)
    original = tmp_path / "source"
    original.mkdir()
    row = ROW | {"primary": "sample.htm"}
    docs = {"sample.htm": INLINE, "sample.xsd": SCHEMA, "sample_pre.xml": LINKBASE}
    index = json.dumps({"directory": {"item": [{"name": name} for name in docs]}})
    entries = []
    for name, content in {"index.json": index, **docs}.items():
        file = original / name
        file.mkdir()
        raw = content.encode()
        (file / "content").write_bytes(raw)
        record = {
            "filename": name,
            "cik": row["cik"],
            "accession": row["accession"],
            "url": pipeline.filing.filing_base(row) + "/" + name,
            "bytes": len(raw),
            "sha256": pipeline.sha(raw),
        }
        pipeline.bulk.write_new(file / "source.json", pipeline.d6.json_bytes(record))
        entries.append(record)
    closure = pipeline.taxonomy.coverage(
        {"http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"},
        lambda url: pipeline.taxonomy.bound_input(pipeline.filing.ARELLE_ROOT, url),
    )
    assert all(item["status"] == "BOUND" for item in closure.values())
    shard = pipeline.extract_filing(
        tmp_path,
        row,
        {"directory": str(original), "documents": entries},
        closure,
        CODE,
        processor,
    )
    assert shard["status"] == "EXTRACTION_COMPLETE"
    assert len(shard["observations"]) == 1
    assert shard["observations"][0]["source"]["filename"] == "sample.htm"


DTS_URI = "http://www.xbrl.org/2006/xbrldi-2006.xsd"
DTS_BYTES = b"<schema/>"


@pytest.fixture
def actual_dts_case(cache, monkeypatch):
    selection = {"filings": [ROW], "source_generation": "0" * 64}
    verified = pipeline.verify_sources(cache, selection, SELECTION_SHA)
    source = verified["filings"][KEY]
    runtime = cache / "runtime"
    runtime.mkdir()
    local = pipeline.taxonomy.local_cache_path(runtime, DTS_URI)
    local.parent.mkdir(parents=True)
    local.write_bytes(DTS_BYTES)
    binding = local.with_name(local.name + pipeline.taxonomy.FILE_BINDING)
    pipeline.publish(binding, pipeline.taxonomy.metadata(DTS_URI, DTS_BYTES, {}))
    monkeypatch.setattr(pipeline.filing, "ARELLE_ROOT", runtime)
    monkeypatch.setattr(pipeline.filing, "verified_sec_plugin", lambda root: True)
    return cache, selection, verified, source, local


def test_actual_dts_legacy_quarantine_repairs_offline_closure(actual_dts_case):
    job, selection, verified, source, local = actual_dts_case
    old = pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"])
    assert old["filings"][KEY] == {}

    # The old finding mentions no URL. The selected document retains the actual DTS.
    dependency = {
        "uri": DTS_URI,
        "sha256": pipeline.sha(DTS_BYTES),
        "resolved_path": str(job / "untrusted-observed-location.xsd"),
    }
    diagnostic = {
        "contract": "F01_TASK9_ARELLE_FILING_V1",
        "status": "TAXONOMY_PENDING",
        "filing": KEY,
        "runtime": {"version": pipeline.filing.ARELLE_VERSION},
        "source_documents": source["documents"],
        "issues": [{"code": "UNBOUND_ACTUAL_DTS", "reason": "Dependency hash not bound"}],
        "documents": [
            {"selected": False, "format": "ORDINARY_HTML"},
            {"selected": True, "format": "XBRL_DOCUMENT", "taxonomy_documents": [dependency]},
        ],
    }
    old_bytes = pipeline.d6.json_bytes(diagnostic)
    quarantine = job / f"{ROW['accession']}-quarantine-{pipeline.sha(old_bytes)[:12]}.json"
    pipeline.publish(quarantine, diagnostic)
    report = pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"])
    assert report["status"] == "TAXONOMY_CLOSURE_READY"
    assert report["filings"][KEY][DTS_URI]["source"]["sha256"] == pipeline.sha(local.read_bytes())
    assert report != old
    new_path = job / f"taxonomy-{pipeline.sha(pipeline.d6.json_bytes(report))[:12]}.json"
    assert pipeline.publish(new_path, report) == pipeline.publish(new_path, report)
    assert pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"]) == report
    assert quarantine.read_bytes() == old_bytes

    local.write_bytes(b"<tampered/>")
    with pytest.raises(ValueError, match="TAXONOMY_CACHE_BINDING_MISMATCH"):
        pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"])
    local.unlink()
    missing = pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"])
    assert missing["status"] == "TAXONOMY_PENDING"
    assert missing["filings"][KEY][DTS_URI]["status"] == "MISSING_EVIDENCE"


def test_actual_dts_runtime_requires_matching_uri_and_verified_hash(actual_dts_case, monkeypatch):
    job, selection, verified, source, _ = actual_dts_case
    raw_obs = observed(source["documents"][1])
    dependency = {"uri": DTS_URI, "sha256": pipeline.sha(DTS_BYTES)}

    def extract(row, folder, listing, processor, **kwargs):
        return (
            [raw_obs],
            [{"selected": True, "taxonomy_documents": [dependency]}],
            [{"code": "ORDINARY_HTML", "category": "INCOMPATIBLE_DOCUMENT_FORMAT"}],
            source["documents"][1:],
        )

    monkeypatch.setattr(pipeline.filing, "extract_cached", extract)
    processor = SimpleNamespace(release={"version": pipeline.filing.ARELLE_VERSION})
    # A hash-authorized copy for a different URL cannot qualify the actual URI.
    wrong_url = {
        "http://www.xbrl.org/others.xsd": {
            "status": "BOUND",
            "source": {"sha256": dependency["sha256"]},
        }
    }
    with pytest.raises(ValueError, match="EXTRACTION_OR_DTS_INCOMPLETE"):
        pipeline.extract_filing(job, ROW, source, wrong_url, CODE, processor)
    quarantine = next(job.glob("*quarantine*.json"))
    before = quarantine.read_bytes()
    assert json.loads(before)["issues"][-1]["dependencies"] == [dependency]
    report = pipeline.taxonomy_report(job, selection, verified, BINDING["source_sha256"])
    shard = pipeline.extract_filing(job, ROW, source, report["filings"][KEY], CODE, processor)
    assert shard["status"] == "EXTRACTION_COMPLETE"
    assert (
        pipeline.extract_filing(job, ROW, source, report["filings"][KEY], CODE, processor) == shard
    )
    assert quarantine.read_bytes() == before


def test_actual_dts_reselection_preserves_verified_original_report(tmp_path):
    legacy = tmp_path / "verified-originals.json"
    previous = {
        "contract": "F01_TASK9_VERIFIED_ORIGINALS_V1",
        "status": "SOURCE_VERIFIED",
        "selection_sha256": SELECTION_SHA,
    }
    original = pipeline.publish(legacy, previous)
    current = "e" * 64
    path = pipeline.original_report_path(tmp_path, current)
    assert path != legacy
    new_hash = pipeline.publish(path, previous | {"selection_sha256": current})
    assert pipeline.original_record(tmp_path, SELECTION_SHA, original)[1] == previous
    assert pipeline.original_record(tmp_path, current, new_hash)[1]["selection_sha256"] == current
    assert pipeline.sha(legacy.read_bytes()) == original
