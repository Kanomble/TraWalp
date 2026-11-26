"""Exercise the one-shot contract with offline transport and processor fixtures."""

import io
import sys
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_f01_task962 as runner  # noqa: E402

tax = runner.tax
URI = next(iter(tax.REQUIRED))
SEC_URI = next(uri for uri in tax.REQUIRED if "/invest/" in uri)
FASB_ZIP = next(iter(runner.PACKAGES))
UNKNOWN = "http://www.xbrl.org/unbound.xsd"


def schema(uri=URI, body=""):
    namespace = tax.REQUIRED.get(uri, "urn:dep")
    return (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{namespace}">'
        f"{body}</xs:schema>"
    ).encode()


def archive(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zip_file:
        for name, data in members.items():
            zip_file.writestr(name, data)
    return output.getvalue()


def test_only_final_exact_taxonomy_paths_are_eligible():
    assert runner.official_endpoint(FASB_ZIP)
    assert runner.official_endpoint(SEC_URI.replace("http:", "https:"))
    for blocked in (
        SEC_URI,
        "https://www.sec.gov/Archives/filing.xml",
        "https://xbrl.sec.gov/dei/2012/../2013/dei-2013-01-31.xsd",
        "https://xbrl.sec.gov/dei/2012/dei-2013-01-31.xsd",
        "https://xbrl.sec.gov/dei/2012/dei-2012-05-31.xsd",
        "https://xbrl.sec.gov/dei/2012/draft-dei-2012-01-31.xsd",
        "https://xbrl.sec.gov/dei/2012/dei-2012-01-31.xsd?override=1",
        "https://xbrl.sec.gov.evil.test/dei/2012/dei-2012-01-31.xsd",
    ):
        assert not runner.official_endpoint(blocked)


def test_archive_binds_only_versioned_recursive_members():
    follow = URI.rsplit("/", 1)[0] + "/dep.xsd"
    raw = archive(
        {
            "elts/us-gaap-2012-01-31.xsd": schema(
                body='<xs:import namespace="urn:dep" schemaLocation="dep.xsd"/>',
            ),
            "elts/dep.xsd": schema(follow),
            "elts/unrelated.xsd": schema(),
        }
    )
    entries = runner.package_entries(raw, FASB_ZIP, {URI: {tax.REQUIRED[URI]}})
    assert entries.keys() == {URI, follow}
    assert entries[follow][1]["package_member"] == "elts/dep.xsd"
    with pytest.raises(ValueError, match="IMPORT_NAMESPACE_MISMATCH"):
        runner.package_entries(raw, FASB_ZIP, {URI: {"urn:wrong"}})
    with pytest.raises(zipfile.BadZipFile):
        runner.package_entries(b"<!doctype html>", FASB_ZIP, {URI: {tax.REQUIRED[URI]}})


class Response(io.BytesIO):
    status = 200

    def __init__(self, url, body, length):
        super().__init__(body)
        self.url = url
        self.headers = Message()
        self.headers["Content-Length"] = str(length)

    def geturl(self):
        return self.url


def client(tmp_path, handler, monkeypatch):
    calls = []

    def open_request(request, **_):
        calls.append((request.method, request.full_url))
        return handler(request)

    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _: SimpleNamespace(free=2**40))
    responses = tmp_path / "rsp"
    responses.mkdir(exist_ok=True)
    run = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
    run.mkdir()
    session = runner.Downloads(
        run,
        responses,
        tmp_path / "limiter.json",
        {"fixture": True},
        opener=SimpleNamespace(open=open_request),
    )
    session.budget.sleep = lambda _: None
    return session, calls


def test_head_get_budget_journal_and_checkpoint_resume(tmp_path, monkeypatch):
    url = URI.replace("http:", "https:")
    body = schema()

    def handler(req):
        return Response(req.full_url, body if req.method == "GET" else b"", len(body))

    session, seen = client(tmp_path, handler, monkeypatch)
    data, receipt = session.fetch(url)
    assert data == body and receipt["sha256"] == runner.task9.sha(body)
    assert seen == [("HEAD", url), ("GET", url)]
    assert session.budget.state["received_bytes"] == len(body)
    assert len(list(session.run.glob("req-*.json"))) == 2
    restarted, seen = client(tmp_path, handler, monkeypatch)
    assert restarted.fetch(url) == (data, receipt)
    assert not seen and not restarted.budget.state["requests"]


def test_transient_http_retry_counts_against_same_run(tmp_path, monkeypatch):
    url = URI.replace("http:", "https:")
    remaining = iter([503, 200, 200])
    body = schema()

    def handler(req):
        code = next(remaining)
        if code != 200:
            raise urllib.error.HTTPError(url, code, "fixture", Message(), io.BytesIO())
        return Response(url, body if req.method == "GET" else b"", len(body))

    session, seen = client(tmp_path, handler, monkeypatch)
    assert session.fetch(url)[0] == body
    assert seen == [("HEAD", url), ("HEAD", url), ("GET", url)]
    assert session.budget.state["statuses"] == {"503": 1, "200": 2}


def test_limits_prevent_transport_after_request_or_byte_exhaustion(tmp_path, monkeypatch):
    url = URI.replace("http:", "https:")
    session, seen = client(tmp_path, lambda req: Response(req.full_url, b"", 4096), monkeypatch)
    session.budget.state["requests"] = 29
    with pytest.raises(ValueError, match="HTTP_REQUEST_BUDGET_EXHAUSTED"):
        session.fetch(url)
    assert seen == [("HEAD", url)]
    session.budget.state["requests"] = 0
    session.budget.state["received_bytes"] = runner.LIMITS["max_bytes"] - 4095
    with pytest.raises(ValueError, match="HTTP_BYTE_BUDGET_EXHAUSTED"):
        session.fetch(url)
    assert seen == [("HEAD", url)] * 2


@pytest.mark.parametrize("status", [302, 403, 404, 429])
def test_http_errors_are_journaled_and_never_promoted(tmp_path, monkeypatch, status):
    url = URI.replace("http:", "https:")

    def reject(req):
        headers = Message()
        headers["Location"] = "https://different.invalid/"
        headers["Retry-After"] = "30"
        raise urllib.error.HTTPError(req.full_url, status, "fixture", headers, io.BytesIO())

    session, seen = client(tmp_path, reject, monkeypatch)
    with pytest.raises(ValueError, match=f"HTTP_{status}"):
        session.fetch(url)
    assert seen == [("HEAD", url)]
    assert not list(session.responses.iterdir())
    assert session.budget.state["statuses"] == {str(status): 1}
    if status == 429:
        with pytest.raises(ValueError, match="HTTP_429"):
            session.fetch(FASB_ZIP)
        assert seen == [("HEAD", url)]


def test_partial_run_keeps_verified_package_and_never_attempts_unapproved_uri(
    tmp_path, monkeypatch
):
    root = tmp_path / "out"
    root.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    rows = [
        {"cik": cik, "accession": acc, "acceptance": "2012-12-21T00:00:00Z", "symbol": symbol}
        for (cik, acc), symbol in zip(
            (filing_id.split("/") for filing_id in runner.DIAGNOSES), ("COST", "ORCL"), strict=True
        )
    ]
    bound = {}
    attempted = []
    partial = True

    def store(_cache, _raw, _url, entries, _receipt):
        bound.update(
            {uri: {"status": "BOUND", "source": info} for uri, (_, info) in entries.items()}
        )

    def progress(_, current_rows, *_args, **_kwargs):
        findings = []
        for row, target in zip(current_rows, (URI, SEC_URI), strict=True):
            filing_id = "/".join(runner.key(row))
            closure = {target: bound.get(target, {"status": "MISSING_EVIDENCE"})}
            if target == SEC_URI and partial:
                closure[UNKNOWN] = {"status": "MISSING_EVIDENCE"}
            path = root / f"{runner.fingerprint(closure)[:16]}.json"
            ready = all(entry["status"] == "BOUND" for entry in closure.values())
            closure_sha = runner.publish(
                path,
                {
                    "status": "TAXONOMY_CLOSURE_READY" if ready else "TAXONOMY_PENDING",
                    "filings": {filing_id: closure},
                },
            )
            item = {
                "cik": row["cik"],
                "accession": row["accession"],
                "status": "DTS_INCOMPLETE",
                "closure_path": str(path),
                "closure_sha256": closure_sha,
            }
            if ready:
                proof = root / f"acct-{row['symbol']}.json"
                proof_sha = runner.publish(
                    proof,
                    {
                        "ruleset": {"id": "fixture"},
                        "filings": {
                            filing_id: {
                                "accounting": {
                                    "cash": {
                                        "admitted": None,
                                        "reason": "DUPLICATE_OR_CONFLICTING_ORIGINALS",
                                    },
                                }
                            }
                        },
                    },
                )
                shard_sha = runner.publish(
                    root / f"{row['accession']}-extraction.json",
                    {
                        "status": "EXTRACTION_COMPLETE",
                        "runtime": {"version": "2.47.5"},
                        "observations": [{"fixture": True}],
                        "documents": [
                            {
                                "selected": True,
                                "taxonomy_documents": [
                                    {
                                        "uri": target,
                                        "sha256": closure[target]["source"]["sha256"],
                                    }
                                ],
                            }
                        ],
                    },
                )
                item.update(
                    extraction_status="EXTRACTION_COMPLETE",
                    accounting_path=str(proof),
                    accounting_sha256=proof_sha,
                    shard_sha256=shard_sha,
                    facts_extracted=1,
                    offline_evidence_path=str(root),
                )
            findings.append(item)
        return {"filings": findings}

    def fetch(url):
        attempted.append(url)
        if url == FASB_ZIP:
            return archive({"elts/us-gaap-2012-01-31.xsd": schema()}), {"url": url}
        if url == SEC_URI.replace("http:", "https:") and not partial:
            return schema(SEC_URI), {"url": url}
        raise ValueError(f"HTTP_404: {url}")

    monkeypatch.setattr(runner, "progress_for", progress)
    monkeypatch.setattr(tax, "store", store)
    downloads = SimpleNamespace(fetch=fetch, hold=None)
    _, filings, accounting, blockers, receipts, _ = runner.recover(rows, {}, root, cache, downloads)
    assert list(receipts) == [FASB_ZIP]
    assert sum(item["dts_status"] == "DTS_COMPLETE" for item in filings.values()) == 1
    assert len(accounting) == 1 and any(item["resource"] == UNKNOWN for item in blockers)
    assert UNKNOWN.replace("http:", "https:") not in attempted

    partial = False
    attempted.clear()
    _, filings, accounting, blockers, _, _ = runner.recover(rows, {}, root, cache, downloads)
    assert len(accounting) == 2 and not blockers
    assert all(item["dts_status"] == "DTS_COMPLETE" for item in filings.values())
    assert FASB_ZIP not in attempted


def test_failed_original_recheck_publishes_failure_before_transport(tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    run = output / "run"
    run.mkdir()

    def blocked(_):
        raise ValueError("ORIGINAL_RECHECK_BLOCKED: fixture")

    monkeypatch.setattr(runner, "Plan", lambda: object())
    monkeypatch.setattr(runner, "load_inputs", blocked)
    result = runner.execute(output, run)
    assert result["status"] == "FAILED"
    assert Path(result["manifest"]).is_file()
    assert not (output / "responses").exists()
