"""Offline verification of the historical taxonomy and Arelle contracts."""

import io
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ("scripts", "reports", "src")]

import f01_arelle_adapter as adapter  # noqa: E402
import f01_task96_taxonomy as tax  # noqa: E402
import f01_task96_taxonomy_acquisition as acquisition  # noqa: E402
from f01_task96_process import verified_worker_output  # noqa: E402

URI = next(iter(tax.REQUIRED))
NS = tax.REQUIRED[URI]
SOURCE = acquisition.SOURCES["fasb-2012-zip"][0]
DEP = URI.rsplit("/", 1)[0] + "/dep.xsd"
THIRD = URI.rsplit("/", 1)[0] + "/third.xml"
AGENT = "Test operator operator@example.org"


def schema(ns=NS, children=""):
    return (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{ns}">'
        f"{children}</xs:schema>"
    ).encode()


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for path, raw in files.items():
            package.writestr(path, raw)
    return output.getvalue()


def native(files, *, extra=""):
    return archive(
        files
        | {
            "release/META-INF/taxonomyPackage.xml": (
                '<taxonomyPackage xmlns="http://xbrl.org/2016/taxonomy-package">'
                "<identifier>urn:fixture:2012</identifier><version>2012</version></taxonomyPackage>"
            ),
            "release/META-INF/catalog.xml": (
                '<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">'
                '<rewriteURI uriStartString="http://xbrl.fasb.org/us-gaap/2012/" '
                f'rewritePrefix="../taxonomy/"/>{extra}</catalog>'
            ),
        }
    )


def test_exact_uri_namespace_and_native_catalog():
    member = "release/taxonomy/elts/us-gaap-2012-01-31.xsd"
    wrong = URI.replace("http://", "https://")
    package, bindings = tax.bind_archive(
        native({member: schema()}),
        source=SOURCE,
        candidates={URI, wrong},
    )
    assert package["kind"] == "NATIVE_PACKAGE"
    assert set(bindings) == {URI}
    assert bindings[URI][1]["package_member"] == member
    with pytest.raises(ValueError, match="NAMESPACE_MISMATCH"):
        tax.bind_archive(
            native({member: schema(NS.replace("2012", "2024"))}), source=SOURCE, candidates={URI}
        )


def test_legacy_requires_reviewed_member_and_hash(tmp_path):
    name = "folder/us-gaap-2012-01-31.xsd"
    original = tmp_path / "published.zip"
    payload = archive({name: schema()})
    original.write_bytes(payload)
    _, entries = tax.bind_archive(payload, source=SOURCE, candidates={URI})
    assert not entries
    record = {
        "source_url": SOURCE,
        "provenance": "fixture source",
        "sha256": tax.task9.sha(payload),
        "bytes": len(payload),
        "uris": {
            URI: {
                "member": name,
                "sha256": tax.task9.sha(schema()),
                "namespace": NS,
            }
        },
    }
    manifest = tmp_path / "review.json"
    sha = tax.publish(manifest, record)
    cache = tmp_path / "taxonomy" / "v1"
    first = acquisition.register(original, manifest, sha, root=cache)
    assert acquisition.register(original, manifest, sha, root=cache) == first
    assert list((cache / "objects").iterdir()) == [cache / "objects" / record["sha256"]]
    assert original.read_bytes() == payload
    assert tax.Resolver(cache).read(URI)[1]["package"]["kind"] == "LEGACY_ZIP"


def test_malformed_conflicting_and_incomplete_archives(tmp_path):
    member = "release/taxonomy/elts/us-gaap-2012-01-31.xsd"
    conflicting = (
        '<rewriteURI uriStartString="http://xbrl.fasb.org/us-gaap/2012/" '
        'rewritePrefix="../other/"/>'
    )
    with pytest.raises(ValueError, match="CONFLICTING_TAXONOMY_CATALOG"):
        tax.bind_archive(
            native({member: schema()}, extra=conflicting), source=SOURCE, candidates={URI}
        )
    with pytest.raises(ValueError, match="UNSAFE_OR_DUPLICATE"):
        tax.bind_archive(archive({"../outside.xsd": schema()}), source=SOURCE, candidates={URI})
    scope = acquisition.acquisition_plan({URI})["blocks"]["fasb-2012-zip"]
    with pytest.raises(ValueError, match="REQUIRED_RELEASE_MEMBERS_MISSING"):
        acquisition.release_entries(archive({"unverified.xsd": schema()}), scope)
    with pytest.raises(ValueError, match="INVALID_PACKAGE_MEMBER"):
        tax.bind_archive(
            native({"release/taxonomy/elts/other.xsd": schema()}), source=SOURCE, candidates={URI}
        )


def test_recursive_import_linkbase_and_missing_dependency(tmp_path, monkeypatch):
    start = schema(children='<xs:import namespace="urn:dep" schemaLocation="dep.xsd"/>')
    dep = schema(
        "urn:dep",
        (
            "<xs:annotation><xs:appinfo><link:linkbaseRef "
            'xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="third.xml"/>'
            "</xs:appinfo></xs:annotation>"
        ),
    )
    values = {URI: start, DEP: dep}
    monkeypatch.setattr(tax.TAX, "bound_input", lambda *_: None)
    monkeypatch.setattr(
        tax.Resolver,
        "_read",
        lambda _, uri: tax.taxonomy_entry(values[uri], uri, {}) if uri in values else None,
    )
    assert tax.Resolver(tmp_path).closure({URI: {NS}})[THIRD]["status"] == "MISSING_EVIDENCE"
    values[THIRD] = b'<linkbase xmlns="http://www.xbrl.org/2003/linkbase"/>'
    assert all(e["status"] == "BOUND" for e in tax.Resolver(tmp_path).closure({URI: {NS}}).values())
    values[DEP] = schema("urn:other")
    closure = tax.Resolver(tmp_path).closure({URI: {NS}})
    assert closure[DEP]["reason"] == "IMPORT_NAMESPACE_MISMATCH"


def test_taxonomy_authorization_and_resume_never_redownload_published_response(
    tmp_path, monkeypatch
):
    scope = acquisition.acquisition_plan({URI})["blocks"]["us-gaap-2012-01-31"]
    sha = tax.publish(tmp_path / "scope.json", scope)
    draft = scope["authorization_draft"]
    draft_sha = tax.publish(tmp_path / "pending.json", draft)
    approval = draft | {
        "decision": "APPROVED",
        "authorized_by": "fixture",
        "authorized_at": "2026-11-26T10:10:00Z",
        "provider_terms_evidence": "fixture",
        "retention_rights_evidence": "fixture",
        "backup_restore_evidence": "fixture",
    }
    ap_sha = tax.publish(tmp_path / "approved.json", approval)
    root = tmp_path / "taxonomy" / "v1"
    calls = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(schema()))}

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            return Response(schema())

    monkeypatch.setattr(acquisition.shutil, "disk_usage", lambda _: SimpleNamespace(free=2**40))
    monkeypatch.setattr(tax.TAX, "bound_input", lambda *_: None)
    monkeypatch.setattr(tax.task9.bulk.urllib.request, "build_opener", lambda *_: Opener())
    args = (tmp_path / "scope.json", sha)
    with pytest.raises(ValueError, match="EXPLICIT_BLOCK_AUTHORIZATION"):
        acquisition.acquire(*args, tmp_path / "pending.json", draft_sha, AGENT, root=root)
    assert not calls and not root.exists()

    store = tax.store

    def interruption(*_):
        raise OSError("interrupted after receipt publication")

    monkeypatch.setattr(tax, "store", interruption)
    with pytest.raises(OSError, match="interrupted"):
        acquisition.acquire(*args, tmp_path / "approved.json", ap_sha, AGENT, root=root)
    monkeypatch.setattr(tax, "store", store)
    result = acquisition.acquire(*args, tmp_path / "approved.json", ap_sha, AGENT, root=root)
    assert result["uris"][URI]["sha256"] == tax.task9.sha(schema())
    assert acquisition.acquire(*args, tmp_path / "approved.json", ap_sha, AGENT, root=root) == {
        "status": "TAXONOMY_ALREADY_BOUND",
    }
    assert calls == [(scope["url"], 30)]


def test_socket_guard_rejects_network():
    with pytest.raises(OSError, match="NETWORK"):
        adapter.prohibit_network("socket.connect", ())


def test_pinned_arelle_reads_exact_verified_overlay(tmp_path, monkeypatch):
    site = os.environ.get("F01_ARELLE_TEST_SITE")
    if not site:
        pytest.skip("Arelle fixture runtime requires F01_ARELLE_TEST_SITE")
    xbrli = "http://www.xbrl.org/2003/instance"
    xbrl_schema = "http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"
    actual = schema(
        children=(
            f'<xs:import namespace="{xbrli}" schemaLocation="{xbrl_schema}"/>'
            f'<xs:element xmlns:xbrli="{xbrli}" name="Fact" type="xbrli:monetaryItemType" '
            'substitutionGroup="xbrli:item" xbrli:periodType="instant"/>'
        )
    )
    resolver = tax.Resolver(tmp_path, arelle=Path(site).parent)
    base = resolver._read
    monkeypatch.setattr(
        resolver,
        "_read",
        lambda uri: (
            tax.taxonomy_entry(actual, URI, {"source_url": SOURCE}) if uri == URI else base(uri)
        ),
    )
    closure = resolver.closure({URI: {NS}})
    resolver.materialize(tmp_path, closure)
    doc = tmp_path / "instance.xml"
    doc.write_text(
        (
            f'<xbrl xmlns="{xbrli}" xmlns:t="{NS}" xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:iso="http://www.xbrl.org/2003/iso4217">'
            f'<link:schemaRef xlink:type="simple" xlink:href="{URI}"/>'
            '<context id="C"><entity><identifier scheme="urn:fixture">1</identifier></entity>'
            "<period><instant>2012-11-25</instant></period></context>"
            '<unit id="U"><measure>iso:USD</measure></unit><t:Fact contextRef="C" unitRef="U" '
            'decimals="0">42</t:Fact></xbrl>'
        ),
        encoding="utf-8",
    )
    worker = tax.task9.filing.ArelleProcessor.__new__(tax.task9.filing.ArelleProcessor)
    worker.directory, worker.site, worker.cache = tmp_path, Path(site), tmp_path / "cache"
    worker.packages, worker.sec_plugin = [], None
    assert worker.run(probe=True)["version"] == "2.47.5"
    output = verified_worker_output(worker.run, document=str(doc), all_facts=True)
    assert output["issues"] == []
    assert len(output["observations"]) == 1
    found = output["observations"][0]
    assert found["concept"] == f"{{{NS}}}Fact"
    assert found["context"]["instant"] == "2012-11-25"
    assert found["unit"]["numerator"] == ["{http://www.xbrl.org/2003/iso4217}USD"]
    assert found["value"] == "42"
    bound = {row["uri"]: row["sha256"] for row in output["taxonomy_documents"]}
    assert bound[URI] == tax.task9.sha(actual)
    original = doc.read_bytes()
    missing = doc.with_name("missing.xml")
    missing.write_bytes(original.replace(URI.encode(), DEP.encode()))
    assert worker.run(document=str(missing), all_facts=True)["issues"]
    assert doc.read_bytes() == original
