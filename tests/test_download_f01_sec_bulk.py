"""Small ZIPs and in-process HTTP responses. No real network or databases."""

import hashlib
import io
import json
import os
import socket
import sys
import urllib.error
import zipfile
from collections import defaultdict, namedtuple
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import download_f01_sec_bulk as bulk  # noqa: E402

CIK = "0000001234"
SLUG = "synthetic-v1"
ETAG = '"fixture-generation-one"'
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Local Windows volumes and publication")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        for name, obj in entries:
            archive.writestr(name, obj if isinstance(obj, bytes) else bulk.json_bytes(obj))
    return stream.getvalue()


def small_archives():
    base = f"CIK{CIK}"
    supplement = f"{base}-submissions-001.json"
    return {
        "companyfacts": zipped([(base + ".json", {"cik": 1234, "facts": {}})]),
        "submissions": zipped(
            [
                (
                    base + ".json",
                    {"cik": CIK, "filings": {"recent": {}, "files": [{"name": supplement}]}},
                ),
                (supplement, {"accessionNumber": ["synthetic-old-1"]}),
            ]
        ),
    }


class Response(io.BytesIO):
    def __init__(self, url, status, headers, body=b""):
        super().__init__(body)
        self.url = url
        self.status = status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = str(value)

    def geturl(self):
        return self.url


class SecDouble:
    def __init__(self):
        self.archives = small_archives()
        self.calls = []
        self.etags = defaultdict(lambda: ETAG)
        self.interrupt = False
        self.force_200_on_range = False
        self.range_supported = True
        self.unknown_size = False

    def open(self, request, *, timeout):
        name = next(name for name, url in bulk.URLS.items() if request.full_url == url)
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["user-agent"] == "Fixture Research contact@example.org"
        assert headers["accept-encoding"] == "identity" and timeout == 30
        self.calls.append((name, request.method, headers))
        stored = self.archives[name]
        head = {
            "ETag": self.etags[name],
            "Last-Modified": "Wed, 11 Nov 2026 03:00:00 GMT",
            "Accept-Ranges": "bytes" if self.range_supported else "none",
        }
        if request.method == "HEAD":
            if not self.unknown_size:
                head["Content-Length"] = len(stored)
            return Response(request.full_url, 200, head)
        if "range" in headers and not self.force_200_on_range:
            offset = int(headers["range"].removeprefix("bytes=").removesuffix("-"))
            head["Content-Length"] = len(stored) - offset
            head["Content-Range"] = f"bytes {offset}-{len(stored) - 1}/{len(stored)}"
            return Response(request.full_url, 206, head, stored[offset:])
        head["Content-Length"] = len(stored)
        if self.interrupt and name == "companyfacts":
            stored = stored[:19]
        return Response(request.full_url, 200, head, stored)


@pytest.fixture
def plan(tmp_path):
    roots = {key: tmp_path / key for key in bulk.ROOTS}
    for path in roots.values():
        path.mkdir()
    source = tmp_path / "plan.json"
    document = {key: str(path) for key, path in roots.items()}
    document["budget_bytes"] = dict.fromkeys(bulk.BUDGETS, 1024)
    source.write_bytes(bulk.json_bytes(document))
    return source, bulk.Layout.load(source)


def client(opener):
    return bulk.SecHttp(
        "Fixture Research contact@example.org",
        opener=opener,
        pause=lambda _: None,
    )


def acquire(layout, opener, *, resume=False):
    return bulk.download(layout, SLUG, client(opener), resume=resume, progress=lambda _: None)


def test_local_capacity_and_network_authorization(plan, monkeypatch, capsys):
    path, layout = plan
    monkeypatch.setattr(bulk, "SecHttp", lambda _: pytest.fail("Network client constructed"))
    assert bulk.main(["--layout", str(path)]) == 3
    local = json.loads(capsys.readouterr().out)
    assert local["status"] == "CAPACITY_UNDETERMINED"
    assert local["archive_bytes"] == dict.fromkeys(bulk.URLS)
    for modes in (
        ["--download", "--generation", SLUG, "--authorize-network", "--user-agent", "fixture"],
        ["--network-preflight", "--user-agent", "fixture"],
    ):
        with pytest.raises(SystemExit) as exc:
            bulk.main(["--layout", str(path), *modes])
        assert exc.value.code == 2
    assert not list(layout.paths["temporary"].iterdir())


def test_authorized_metadata_only_preflight_and_local_replay(plan, monkeypatch, capsys, tmp_path):
    path, layout = plan
    opener = SecDouble()
    bounded = client(opener)
    monkeypatch.setattr(bulk, "SecHttp", lambda _: bounded)
    args = ["--layout", str(path)]
    assert (
        bulk.main([*args, "--network-preflight", "--authorize-network", "--user-agent", "fixture"])
        == 0
    )
    raw = capsys.readouterr().out
    assert [method for _, method, _ in opener.calls] == ["HEAD", "HEAD"]
    observed = json.loads(raw)
    assert observed["status"] == "CAPACITY_OK"
    assert observed["original_zip_total_bytes"] == sum(map(len, opener.archives.values()))
    metadata = tmp_path / "sizes.json"
    metadata.write_text(raw, encoding="utf-8")
    assert bulk.main([*args, "--sizes-from", str(metadata)]) == 0
    assert json.loads(capsys.readouterr().out)["archive_bytes"] == observed["archive_bytes"]
    assert len(opener.calls) == 2 and not list(layout.paths["temporary"].iterdir())


@pytest.mark.parametrize(("size_unknown", "message"), [(True, "SIZE_UNKNOWN"), (False, "CAPACITY")])
def test_download_blocks_unknown_or_insufficient_space(plan, monkeypatch, size_unknown, message):
    _, layout = plan
    opener = SecDouble()
    opener.unknown_size = size_unknown
    disk = namedtuple("Disk", "free")
    monkeypatch.setattr(bulk.shutil, "disk_usage", lambda _: disk(free=1))
    with pytest.raises(bulk.Blocked, match=message):
        acquire(layout, opener)
    assert all(method == "HEAD" for _, method, _ in opener.calls)
    assert not list(layout.paths["temporary"].iterdir())


def test_verified_publication_manifest_and_no_overwrite(plan):
    _, layout = plan
    opener = SecDouble()
    result = acquire(layout, opener)
    file = Path(result["manifest"])
    manifest = json.loads(file.read_bytes())
    assert file.name == bulk.MANIFEST_NAME
    assert file.read_bytes() == bulk.json_bytes(manifest)
    assert manifest["quality"] == bulk.QUALITY and not manifest["shared_sec_generation_verified"]
    for name, data in opener.archives.items():
        entry = manifest["archives"][name]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert Path(entry["local_path"]).read_bytes() == data
        assert entry["integrity"]["status"] == "VERIFIED"
    assert manifest["archives"]["submissions"]["integrity"]["historical_submission_chunks"] == 1
    assert bulk.verify_generation(layout, SLUG, result["manifest_sha256"])["status"] == "VERIFIED"
    with pytest.raises(bulk.Blocked, match="MANIFEST_SHA256"):
        bulk.verify_generation(layout, SLUG, "a" * 64)
    before = file.read_bytes()
    with pytest.raises(bulk.Blocked, match="ALREADY_EXISTS"):
        acquire(layout, opener)
    assert file.read_bytes() == before
    assert len(opener.calls) == 4


def test_interrupted_download_resumes_only_bound_bytes(plan):
    _, layout = plan
    remote = SecDouble()
    remote.interrupt = True
    with pytest.raises(bulk.Blocked, match="BODY_INTERRUPTED"):
        acquire(layout, remote)
    stage = layout.paths["temporary"] / f"{SLUG}.incomplete"
    partial = stage / "publish/companyfacts.zip"
    assert partial.read_bytes() == remote.archives["companyfacts"][:19]
    assert not (layout.paths["originals"] / SLUG).exists()
    with pytest.raises(bulk.Blocked, match="REQUIRES_COMPLETE_DOWNLOAD"):
        bulk.finalize_offline(layout, SLUG, progress=lambda _: None)
    with pytest.raises(bulk.Blocked, match="INCOMPLETE_GENERATION_EXISTS"):
        acquire(layout, remote)
    remote.interrupt = False
    result = acquire(layout, remote, resume=True)
    assert result["status"] == "PUBLISHED"
    _, _, headers = next(
        (name, method, hdr) for name, method, hdr in remote.calls if "range" in hdr
    )
    assert headers["range"] == "bytes=19-"
    assert headers["if-match"] == headers["if-range"] == ETAG
    manifest = json.loads(Path(result["manifest"]).read_bytes())
    assert [segment["offset"] for segment in manifest["archives"]["companyfacts"]["segments"]] == [
        0,
        19,
    ]


@pytest.mark.parametrize(
    "fault", ["generation", "weak", "no-range", "full-response", "local-prefix"]
)
def test_resume_rejects_drift_without_appending(plan, fault):
    _, layout = plan
    remote = SecDouble()
    remote.interrupt = True
    if fault == "weak":
        remote.etags["companyfacts"] = 'W/"weak"'
    with pytest.raises(bulk.Blocked, match="BODY_INTERRUPTED"):
        acquire(layout, remote)
    partial = layout.paths["temporary"] / f"{SLUG}.incomplete/publish/companyfacts.zip"
    if fault == "generation":
        remote.etags["companyfacts"] = '"changed"'
    elif fault == "no-range":
        remote.range_supported = False
    elif fault == "full-response":
        remote.force_200_on_range = True
    elif fault == "local-prefix":
        partial.write_bytes(b"x" * partial.stat().st_size)
    before = partial.read_bytes()
    remote.interrupt = False
    with pytest.raises(bulk.Blocked, match="GENERATION|RANGE|PARTIAL_SHA256"):
        acquire(layout, remote, resume=True)
    assert partial.read_bytes() == before
    assert not list(layout.paths["originals"].iterdir())


@pytest.mark.parametrize(
    "fault",
    [
        "invalid-zip",
        "size",
        "sha",
        "traversal",
        "duplicate",
        "cik",
        "missing-cik",
        "null-cik",
        "crc",
        "chunk",
        "empty-submissions",
    ],
)
def test_archive_validation_rejects_unsafe_or_broken_data(tmp_path, fault):
    member = f"CIK{CIK}.json"
    valid = {"cik": CIK, "facts": {}}
    data = zipped([(member, valid)])
    endpoint = "companyfacts"
    match = "MISMATCH"
    if fault == "invalid-zip":
        data, match = data[:-20], "INVALID_ZIP"
    elif fault == "traversal":
        data, match = zipped([("../" + member, valid)]), "UNSAFE_UNEXPECTED"
    elif fault == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            data = zipped([(member, valid), (member, valid)])
        match = "UNSAFE_UNEXPECTED"
    elif fault == "cik":
        data = zipped([(member, {"cik": 9999, "facts": {}})])
    elif fault == "missing-cik":
        data, match = zipped([(member, {"facts": {}})]), "ZIP_MEMBER_CIK_REQUIRED"
    elif fault == "null-cik":
        data = zipped([(member, {"cik": None, "facts": {}})])
    elif fault == "crc":
        data, match = data.replace(CIK.encode() + b'"', b'0000009999"'), "INVALID_ZIP"
    elif fault == "empty-submissions":
        endpoint, match = "submissions", "ZIP_MEMBER_CIK_REQUIRED"
        data = zipped([(member, {})])
    elif fault == "chunk":
        endpoint, match = "submissions", "CHUNKS_INCOMPLETE"
        data = zipped(
            [
                (
                    member,
                    {
                        "cik": CIK,
                        "filings": {
                            "recent": {},
                            "files": [{"name": f"CIK{CIK}-submissions-001.json"}],
                        },
                    },
                )
            ]
        )
    archive = tmp_path / "small.zip"
    archive.write_bytes(data)
    expected = "1" * 64 if fault == "sha" else hashlib.sha256(data).hexdigest()
    with pytest.raises(bulk.Blocked, match=match):
        bulk.validate_archive(archive, endpoint, len(data) + int(fault == "size"), expected)


def test_empty_companyfacts_are_excluded_and_sorted(tmp_path):
    archive = tmp_path / "companyfacts.zip"
    empty_members = ["CIK0000003521.json", "CIK0000000001.json"]
    data = zipped(
        [
            (empty_members[0], {}),
            (f"CIK{CIK}.json", {"cik": CIK, "facts": {}}),
            (empty_members[1], {}),
        ]
    )
    archive.write_bytes(data)
    result = bulk.validate_archive(
        archive, "companyfacts", len(data), hashlib.sha256(data).hexdigest()
    )
    assert result["status"] == "VERIFIED" and result["members"] == 3
    assert result["issuers"] == 1
    assert result["empty_companyfacts"] == {
        "classification": "EMPTY_COMPANYFACTS",
        "count": 2,
        "members": sorted(empty_members),
        "import_eligible": False,
    }


@pytest.mark.parametrize("tamper", [False, True])
def test_offline_finalization_uses_completed_checkpoints_without_http(
    plan, monkeypatch, capsys, tamper
):
    layout_path, layout = plan
    remote = SecDouble()
    empty_member = "CIK0000003521.json"
    remote.archives["companyfacts"] = zipped(
        [(empty_member, {}), (f"CIK{CIK}.json", {"cik": 1234, "facts": {}})]
    )
    remote.etags["companyfacts"] = 'W/"offline-does-not-need-a-current-strong-etag"'

    def interrupt_before_publication(*_):
        raise bulk.Blocked("synthetic stop after both checkpoints")

    with monkeypatch.context() as patch:
        patch.setattr(bulk, "publish_generation", interrupt_before_publication)
        with pytest.raises(bulk.Blocked, match="both checkpoints"):
            acquire(layout, remote)
    stage = layout.paths["temporary"] / f"{SLUG}.incomplete"
    if tamper:
        staged_zip = stage / "publish/companyfacts.zip"
        data = staged_zip.read_bytes()
        staged_zip.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    monkeypatch.setattr(bulk, "SecHttp", lambda *_: pytest.fail("Offline mode constructed HTTP"))
    args = ["--layout", str(layout_path), "--finalize-offline", "--generation", SLUG]
    with pytest.raises(SystemExit) as exc:
        bulk.main([*args, "--authorize-network"])
    assert exc.value.code == 2
    capsys.readouterr()
    outcome = bulk.main(args)
    output = capsys.readouterr()
    assert len(remote.calls) == 4
    if tamper:
        assert outcome == 3 and "ARCHIVE_SHA256_MISMATCH" in output.err
        assert not list(layout.paths["originals"].iterdir())
    else:
        assert outcome == 0
        published = json.loads(output.out)
        report = bulk.verify_generation(layout, SLUG, published["manifest_sha256"])
        assert report["archives"]["companyfacts"]["empty_companyfacts"]["members"] == [empty_member]
        assert report["archives"]["companyfacts"]["issuers"] == 1
        assert bulk.main(args) == 3
        assert "ORIGINAL_GENERATION_ALREADY_EXISTS" in capsys.readouterr().err


def staged_census_inputs(layout, monkeypatch, archives):
    remote = SecDouble()
    remote.archives = archives

    def stop_before_publication(*_):
        raise bulk.Blocked("keep synthetic ZIPs staged")

    with monkeypatch.context() as patch:
        patch.setattr(bulk, "publish_generation", stop_before_publication)
        with pytest.raises(bulk.Blocked, match="keep synthetic ZIPs staged"):
            acquire(layout, remote)
    monkeypatch.setattr(bulk, "SecHttp", lambda *_: pytest.fail("Census constructed HTTP"))
    return layout.paths["temporary"] / f"{SLUG}.incomplete"


@pytest.mark.parametrize("blocked", [False, True])
def test_structure_census_collects_payload_findings_and_checks_pilot(
    plan, monkeypatch, capsys, blocked
):
    layout_path, layout = plan
    spec = json.loads((bulk.REPO / "docs/f01_sec_bulk_pilot_spec.json").read_bytes())
    facts, submissions = [], []
    for issuer in spec["issuers"]:
        cik = issuer["cik"]
        bad_facts = blocked and issuer["symbol"] == "AAPL"
        bad_submissions = blocked and issuer["symbol"] == "ACN"
        facts.append(
            (
                issuer["companyfacts_member"],
                {
                    "cik": cik,
                    "facts": {} if bad_facts else {"us-gaap": {}},
                },
            )
        )
        ref = [{"name": f"CIK{cik}-submissions-001.json"}] if bad_submissions else []
        submissions.append(
            (
                issuer["submissions_member"],
                {
                    "cik": cik,
                    "filings": {"recent": {}, "files": ref},
                },
            )
        )
    missing_cik = "CIK0000741313.json"
    facts.extend(
        [
            ("CIK0000003521.json", {}),
            (missing_cik, {"entityName": "Synthetic", "facts": {"us-gaap": {}}}),
        ]
    )
    if blocked:
        facts.extend(
            [
                ("CIK0000007777.json", {"cik": 9999, "facts": {"dei": {}}}),
                ("CIK0000007778.json", {"entityName": "Missing both"}),
                ("CIK0000007779.json", []),
            ]
        )
    archives = {"companyfacts": zipped(facts), "submissions": zipped(submissions)}
    stage = staged_census_inputs(layout, monkeypatch, archives)
    args = ["--layout", str(layout_path), "--structure-census", "--generation", SLUG]
    assert bulk.main(args) == (3 if blocked else 0)
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == ("CENSUS_BLOCKED" if blocked else "CENSUS_COMPLETE")
    report_path = Path(output["report"])
    assert report_path.name == bulk.CENSUS_NAME
    report = json.loads(report_path.read_bytes())
    assert report["publication_authorized"] is False and report["import_authorized"] is False
    company = report["archives"]["companyfacts"]
    assert company["integrity"]["status"] == "VERIFIED"
    assert company["integrity"]["members_crc_verified"] == len(facts)
    categories = company["categories"]
    assert categories["VALID"]["count"] == 8 - int(blocked)
    assert categories["EMPTY_COMPANYFACTS"]["key_groups"] == [
        {"keys": [], "members": ["CIK0000003521.json"]},
    ]
    missing = categories["MISSING_INTERNAL_CIK"]
    assert missing["disposition"] == "QUARANTINED" and not missing["import_eligible"]
    assert {"keys": ["entityName", "facts"], "members": [missing_cik]} in missing["key_groups"]
    results = {issuer["symbol"]: issuer["endpoints"] for issuer in report["pilot"]["issuers"]}
    assert len(results) == 8 and results["LLY"]["companyfacts"]["status"] == "CLEAR"
    if blocked:
        for category in ("CIK_MISMATCH", "EMPTY_FACTS", "MISSING_FACTS", "INVALID_PAYLOAD"):
            assert categories[category]["count"] == 1
        assert results["AAPL"]["companyfacts"]["status"] == "AFFECTED"
        assert results["ACN"]["submissions"]["status"] == "AFFECTED"
        assert report["archives"]["submissions"]["categories"]["INVALID_SUBMISSIONS"]["count"] == 1
    else:
        assert not report["review_blockers"]
        with pytest.raises(bulk.Blocked, match="ZIP_MEMBER_CIK_REQUIRED"):
            bulk.finalize_offline(layout, SLUG, progress=lambda _: None)
    before = report_path.read_bytes()
    assert bulk.main(args) == 3 and report_path.read_bytes() == before
    assert not list(layout.paths["originals"].iterdir())
    for endpoint, archive in archives.items():
        assert (stage / "publish" / f"{endpoint}.zip").read_bytes() == archive


def test_census_reports_archive_damage_and_keeps_scanning_other_members(plan, monkeypatch):
    _, layout = plan
    valid = {"cik": 1234, "facts": {"dei": {}}}
    filename = f"CIK{CIK}.json"
    with pytest.warns(UserWarning, match="Duplicate name"):
        companyfacts = zipped(
            [
                (filename, valid),
                (filename, valid),
                ("../" + filename, valid),
                (
                    "CIK0000005678.json",
                    {"cik": 5678, "facts": {"test": {"marker": "crc-original"}}},
                ),
                ("CIK0000005679.json", {}),
            ]
        )
    stage = staged_census_inputs(
        layout, monkeypatch, small_archives() | {"companyfacts": companyfacts}
    )
    damaged = stage / "publish/companyfacts.zip"
    damaged.write_bytes(companyfacts.replace(b"crc-original", b"crc-modified"))
    output = bulk.structure_census(layout, SLUG, progress=lambda _: None)
    assert output["status"] == "CENSUS_BLOCKED"
    report = json.loads(Path(output["report"]).read_bytes())
    company = report["archives"]["companyfacts"]
    assert company["integrity"]["status"] == "BLOCKED"
    assert company["integrity"]["members_crc_verified"] == 4
    reasons = {issue["reason"] for issue in company["integrity"]["issues"]}
    assert reasons == {
        "ARCHIVE_SHA256_MISMATCH",
        "DUPLICATE_MEMBER",
        "UNSAFE_OR_UNEXPECTED_MEMBER",
        "MEMBER_INTEGRITY_FAILED",
    }
    assert company["categories"]["UNKNOWN_STRUCTURE"]["count"] == 3
    assert company["categories"]["EMPTY_COMPANYFACTS"]["count"] == 1
    assert not company["categories"]["VALID"]["import_eligible"]
    assert report["archives"]["submissions"]["integrity"]["members_crc_verified"] == 2


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("none", None),
        ("census", "REVIEWED_CENSUS_BINDING_MISMATCH"),
        ("checkpoint", "CHECKPOINT_SHA256_MISMATCH"),
        ("zip", "ARCHIVE_SHA256_MISMATCH"),
        ("placeholder", "REVIEWED_NON_DATA_MEMBER_CONTENT_MISMATCH"),
        ("extra-txt", "CENSUS_ARCHIVE_INTEGRITY_UNPROVEN"),
        ("pilot", "CENSUS_PILOT_VALID_PRIMARY_REQUIRED"),
    ],
)
def test_reviewed_finalization_only_publishes_bound_quarantine(
    plan, monkeypatch, capsys, fault, reason
):
    layout_path, layout = plan
    raw_spec = (bulk.REPO / "docs/f01_sec_bulk_pilot_spec.json").read_bytes()
    issuers = json.loads(raw_spec)["issuers"]
    facts, submissions = [], []
    for issuer in issuers:
        empty = fault == "pilot" and issuer["symbol"] == "AAPL"
        facts.append(
            (
                issuer["companyfacts_member"],
                {
                    "cik": issuer["cik"],
                    "facts": {} if empty else {"us-gaap": {}},
                },
            )
        )
        submissions.append(
            (
                issuer["submissions_member"],
                {
                    "cik": issuer["cik"],
                    "filings": {"recent": {}, "files": []},
                },
            )
        )
    facts.extend(
        [
            ("CIK0000003521.json", {}),
            ("CIK0000003522.json", {"cik": 3522, "facts": {}}),
            ("CIK0000741313.json", {"entityName": "No CIK", "facts": {"dei": {}}}),
        ]
    )
    placeholder = b"Different string" if fault == "placeholder" else bulk.NON_DATA_BYTES
    submissions.append((bulk.NON_DATA_MEMBER, placeholder))
    if fault == "extra-txt":
        submissions.append(("extra.txt", b"Unknown"))
    data = {"companyfacts": zipped(facts), "submissions": zipped(submissions)}
    stage = staged_census_inputs(layout, monkeypatch, data)
    census_path = Path(bulk.structure_census(layout, SLUG, progress=lambda _: None)["report"])
    raw_census = census_path.read_bytes()
    census_sha = hashlib.sha256(raw_census).hexdigest()
    review = {
        "contract": bulk.REVIEW_CONTRACT,
        "generation": SLUG,
        "census_sha256": census_sha,
        "pilot_spec_sha256": hashlib.sha256(raw_spec).hexdigest(),
        "archives": {
            name: {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "members": len(facts if name == "companyfacts" else submissions),
                "checkpoint_sha256": hashlib.sha256(
                    (stage / f"{name}.checkpoint.json").read_bytes()
                ).hexdigest(),
            }
            for name, raw in data.items()
        },
        "companyfacts_quarantine_counts": {
            "EMPTY_COMPANYFACTS": 1,
            "EMPTY_FACTS": 1 + int(fault == "pilot"),
            "MISSING_INTERNAL_CIK": 1,
        },
        "submissions_non_data_member": {
            "member": bulk.NON_DATA_MEMBER,
            "bytes": len(placeholder),
            "sha256": hashlib.sha256(placeholder).hexdigest(),
        },
    }
    policy = layout_path.with_name("review.json")
    policy.write_bytes(bulk.json_bytes(review))
    monkeypatch.setattr(bulk, "REVIEW_PATH", policy)
    if fault == "census":
        census_path.write_bytes(raw_census + b"\n")
    elif fault == "checkpoint":
        checkpoint = stage / "submissions.checkpoint.json"
        checkpoint.write_bytes(checkpoint.read_bytes() + b"\n")
    elif fault == "zip":
        (stage / "publish/companyfacts.zip").write_bytes(b"x" + data["companyfacts"][1:])
    args = [
        "--layout",
        str(layout_path),
        "--finalize-offline",
        "--generation",
        SLUG,
        "--reviewed-census",
        str(census_path),
        "--census-sha256",
        census_sha,
    ]
    if reason:
        assert bulk.main([*args, "--authorize-reviewed-publication"]) == 3
        assert reason in capsys.readouterr().err
        assert not list(layout.paths["originals"].iterdir())
        return
    with pytest.raises(SystemExit) as blocked:
        bulk.main(args)
    assert blocked.value.code == 2
    capsys.readouterr()
    assert bulk.main([*args, "--authorize-reviewed-publication"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "SEC_BULK_ARCHIVES_VERIFIED"
    manifest = json.loads(Path(result["manifest"]).read_bytes())
    assert (
        manifest["qualified_research_issuers"] == [] and not manifest["trading_universe_qualified"]
    )
    assert all(
        state["status"] == "STRUCTURE_CLEAR"
        for issuer in manifest["pilot_structure"]
        for state in issuer["endpoints"].values()
    )
    assert manifest["archives"]["submissions"]["integrity"]["excluded_members"] == [
        {
            "member": "placeholder.txt",
            "classification": "NON_DATA_ARCHIVE_MEMBER",
            "import_eligible": False,
        },
    ]
    assert manifest["archives"]["companyfacts"]["integrity"]["excluded_member_count"] == 3
    for name, raw in data.items():
        assert (layout.paths["originals"] / SLUG / f"{name}.zip").read_bytes() == raw
    assert bulk.verify_generation(layout, SLUG, result["manifest_sha256"])["status"] == (
        "SEC_BULK_ARCHIVES_VERIFIED"
    )
    assert bulk.main([*args, "--authorize-reviewed-publication"]) == 3
    assert "ORIGINAL_GENERATION_ALREADY_EXISTS" in capsys.readouterr().err


def test_manifest_is_stable_and_records_distinct_upstream_observations(tmp_path):
    validations = dict.fromkeys(bulk.URLS, {"status": "VERIFIED"})
    states = {
        name: {
            "retrieval_started_at_utc": f"2026-11-{day}T03:00:00+00:00",
            "retrieval_ended_at_utc": f"2026-11-{day}T03:01:00+00:00",
            "archive_bytes": 42,
            "prefix_sha256": "a" * 64,
            "http": {"ETag": f'"{day}"'},
            "segments": [],
        }
        for name, day in zip(bulk.URLS, (19, 20), strict=True)
    }
    left = bulk.source_manifest(SLUG, tmp_path, states, validations)
    right = bulk.source_manifest(SLUG, tmp_path, dict(reversed(list(states.items()))), validations)
    assert bulk.json_bytes(left) == bulk.json_bytes(right)
    assert left["archives"]["companyfacts"]["http"] != left["archives"]["submissions"]["http"]
    assert not left["shared_sec_generation_verified"]


def test_rate_limit_retry_is_bounded():
    class Limited:
        calls = 0

        def open(self, request, *, timeout):
            self.calls += 1
            headers = Message()
            headers["Retry-After"] = "2"
            raise urllib.error.HTTPError(request.full_url, 429, "fixture", headers, io.BytesIO())

    double = Limited()
    delays = []
    http = bulk.SecHttp("Fixture Research contact@example.org", opener=double, pause=delays.append)
    with pytest.raises(bulk.Blocked, match="SEC_HTTP_429"):
        http.open("companyfacts", "HEAD")
    assert double.calls == 3 and delays.count(2) == 2
    with pytest.raises(bulk.Blocked, match="RETRY_AFTER_EXCEEDS"):
        bulk.retry_delay("300", 429, 0)
