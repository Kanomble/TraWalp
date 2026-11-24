"""Small, offline fixtures for the isolated empty A-V1 bootstrap."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.data import canonical_bootstrap as clean
from trading_system.data.canonical_facts import (
    CONTRACT,
    Admission,
    RequestIdentity,
    require_contract,
)
from trading_system.data.database import Database
from trading_system.data.sec_persistence import SecRequestEnvelope
from trading_system.data.sec_sync_persistence import IsolatedCanonicalSec, SecSyncPersistence
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Bootstrap pins paths on Windows")

CODE = "a" * 64
MANIFEST = json.dumps(
    {
        "contract": clean.BOOTSTRAP_CONTRACT,
        "classification": clean.CLASSIFICATION,
        "research_scope": "Offline fixture",
    }
).encode()
MANIFEST_SHA = hashlib.sha256(MANIFEST).hexdigest()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network use in bootstrap fixture")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def target(tmp_path):
    return tmp_path / "f01-sec-isolated-research-fixture" / clean.DB_NAME


def create(target, **override):
    args = {
        "manifest": MANIFEST,
        "manifest_sha256": MANIFEST_SHA,
        "expected_code_fingerprint": CODE,
        "verify_code": lambda: CODE,
        "authorized": True,
    } | override
    return clean.bootstrap(target, **args)


def rows(db, statement):
    with db.read_only() as connection:
        return [tuple(row) for row in connection.execute(statement)]


def test_empty_lineage_and_schema_guards(target):
    report = create(target)
    db = Database(target)
    db.initialize()
    with db.connect() as connection:
        require_contract(connection)
        assert tuple(connection.execute("SELECT * FROM fact_contract").fetchone()) == (
            1,
            CONTRACT,
            1,
            report["schema_fingerprint"],
        )
    stored, raw = rows(db, "SELECT evidence,manifest FROM research_bootstrap")[0]
    lineage = report["lineage"]
    assert json.loads(stored) == lineage
    assert raw == (target.parent / "bootstrap_manifest.json").read_bytes() == MANIFEST
    assert json.loads((target.parent / "bootstrap_result.json").read_bytes()) == report
    assert datetime.fromisoformat(lineage["created_at_utc"]).tzinfo == UTC
    assert lineage["classification"] == "RESEARCH_ONLY"
    assert lineage["code_fingerprint"] == CODE
    assert lineage["manifest_sha256"] == MANIFEST_SHA
    assert rows(db, "SELECT * FROM fact_dataset") == [(1, lineage["dataset_id"], MANIFEST_SHA)]
    for relation in (
        "fundamental_facts",
        "forward_occ_v1",
        "legacy_occ_versions_v1",
        "canonical_fact",
        "source_occurrence",
        "fact_revision",
        "ingestion_receipt",
        "sync_state",
        "raw_sec_cache",
        "companies",
        "assets",
        "daily_bars",
        "forward_market_observations",
        "symbol_identity_epochs",
    ):
        assert rows(db, f"SELECT * FROM {relation}") == []
    assert (
        rows(
            db,
            "SELECT name FROM sqlite_master WHERE name LIKE 'canonical_migration%' "
            "OR name='canonical_import_archive'",
        )
        == []
    )
    another = target.parent.with_name("f01-sec-isolated-research-second") / clean.DB_NAME
    assert create(another)["lineage"]["dataset_id"] != lineage["dataset_id"]
    with db.connect() as connection:
        connection.execute("DROP VIEW forward_occ_v1")
    with pytest.raises(ValueError, match="CANONICAL_SCHEMA_OR_FK_GUARD"):
        db.initialize()


def test_preflight_authorization_bindings_and_resources(target, monkeypatch):
    plan = clean.preflight(
        target, manifest=MANIFEST, manifest_sha256=MANIFEST_SHA, code_fingerprint=CODE
    )
    assert plan.target == target
    assert plan.free_bytes >= clean.MIN_FREE_BYTES
    assert not target.parent.exists() and not plan.staging.exists()
    with pytest.raises(clean.BootstrapBlocked, match="AUTHORIZATION"):
        create(target, authorized=False)
    with pytest.raises(clean.BootstrapBlocked, match="MANIFEST_BINDING"):
        create(target, manifest=MANIFEST + b" ")
    with pytest.raises(clean.BootstrapBlocked, match="CODE_FINGERPRINT"):
        create(target, verify_code=lambda: "b" * 64)
    monkeypatch.setattr(clean.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})())
    with pytest.raises(clean.BootstrapBlocked, match="INSUFFICIENT_SPACE"):
        create(target)
    assert not target.parent.exists() and not plan.staging.exists()


def test_isolation_and_no_changes_to_existing_target(target, tmp_path, monkeypatch):
    original = tmp_path / "protected"
    original.write_bytes(b"existing fixture")
    target.parent.mkdir()
    os.link(original, target)
    redirect = tmp_path / "junction"
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirect), str(tmp_path / "real")],
        capture_output=True,
    )
    (tmp_path / "real").mkdir()
    with pytest.raises(clean.BootstrapBlocked, match="TARGET_ALREADY_EXISTS"):
        create(target)
    assert target.read_bytes() == original.read_bytes() == b"existing fixture"
    with pytest.raises(clean.BootstrapBlocked, match="TARGET_REQUIRED"):
        create(Path("data") / clean.DB_NAME)
    monkeypatch.setattr(clean, "REPO", tmp_path)
    (tmp_path / "reports").mkdir()
    with pytest.raises(clean.BootstrapBlocked, match="PROTECTED_OR_REDIRECTED"):
        create(tmp_path / "reports" / target.parent.name / target.name)
    if junction.returncode != 0:
        pytest.skip(f"Windows could not create a fixture junction: {junction.stderr!r}")
    with pytest.raises(clean.BootstrapBlocked, match="UNSAFE_DIRECTORY"):
        create(redirect / target.parent.name / target.name)


def test_late_destination_is_never_replaced(target, monkeypatch):
    rename = Path.rename

    def competing_destination(staging, destination):
        destination.mkdir()
        (destination / "keep.txt").write_text("created during bootstrap", encoding="utf-8")
        return rename(staging, destination)

    monkeypatch.setattr(Path, "rename", competing_destination)
    with pytest.raises(OSError):
        create(target)
    assert not target.exists()
    assert (target.parent / "keep.txt").read_text(encoding="utf-8") == "created during bootstrap"


def test_two_concurrent_starts_publish_once(target):
    def attempt():
        try:
            return create(target)
        except (clean.BootstrapBlocked, FileExistsError) as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert rows(Database(target), "SELECT current_version FROM fact_contract") == [(1,)]


@pytest.mark.parametrize("boundary", ["table", "index", "before_commit", "after_publication"])
def test_actual_process_exit_boundaries(target, boundary, tmp_path):
    fixture = tmp_path / "manifest.json"
    fixture.write_bytes(MANIFEST)
    runner = """
import os, sqlite3, sys
from pathlib import Path
from trading_system.data import canonical_bootstrap as clean

boundary, target, manifest, manifest_sha, code = sys.argv[1:]
create_storage = clean.create_storage
publish_readers = clean._publish_readers
rename = Path.rename

def crash_during_storage(connection, **kwargs):
    connection.execute("CREATE TABLE interrupted_storage(x)")
    os._exit(71)
def crash_at_index(connection):
    def stop(action, name, *_):
        if action == sqlite3.SQLITE_CREATE_INDEX and name == "occurrence_legacy_reader":
            os._exit(71)
        return sqlite3.SQLITE_OK
    connection.set_authorizer(stop)
    publish_readers(connection)
def crash_before_commit(connection):
    publish_readers(connection)
    os._exit(71)
def crash_after_publication(source, destination):
    rename(source, destination)
    os._exit(71)

if boundary == "table":
    clean.create_storage = crash_during_storage
elif boundary == "index":
    clean._publish_readers = crash_at_index
elif boundary == "before_commit":
    clean._publish_readers = crash_before_commit
else:
    Path.rename = crash_after_publication
clean.bootstrap(Path(target), manifest=Path(manifest).read_bytes(), manifest_sha256=manifest_sha,
                expected_code_fingerprint=code, verify_code=lambda: code, authorized=True)
"""
    env = {**os.environ, "PYTHONPATH": str(clean.REPO / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    execution = subprocess.run(
        [sys.executable, "-c", runner, boundary, str(target), str(fixture), MANIFEST_SHA, CODE],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert execution.returncode == 71, execution.stderr.decode(errors="replace")
    if boundary == "after_publication":
        report = json.loads((target.parent / "bootstrap_result.json").read_bytes())
        assert (
            report["database_sha256_at_bootstrap"]
            == hashlib.sha256(target.read_bytes()).hexdigest()
        )
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            require_contract(connection, writer=False)
        with pytest.raises(clean.BootstrapBlocked, match="TARGET_ALREADY_EXISTS"):
            create(target)
    else:
        assert not target.parent.exists()
        retained = target.parent.parent / f".{target.parent.name}.bootstrap-incomplete"
        assert retained.is_dir()
        with pytest.raises(clean.BootstrapBlocked, match="STAGING_REQUIRES_MANUAL_REVIEW"):
            create(target)
        # Recover the interrupted SQLite transaction only on the disposable fixture.
        with closing(sqlite3.connect(retained / clean.DB_NAME)) as connection:
            assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                require_contract(connection, writer=False)


def test_prepublication_code_drift_retains_staging(target):
    changed = iter([CODE, "b" * 64])
    with pytest.raises(clean.BootstrapBlocked, match="DRIFT_BEFORE_PUBLICATION"):
        create(target, verify_code=lambda: next(changed))
    assert not target.parent.exists()
    with pytest.raises(clean.BootstrapBlocked, match="STAGING_REQUIRES_MANUAL_REVIEW"):
        create(target)


def test_existing_writer_with_bound_origin_and_revisions(target):
    report = create(target)
    db = Database(target)
    company = CompanyIdentity(cik="0000001234", symbol="TEST", name="Synthetic")
    at = datetime(2026, 11, 20, tzinfo=UTC)
    membership = {("TEST", "us-gaap"): Admission(True, True, "Synthetic membership")}
    candidate = IsolatedCanonicalSec(target.parent, lambda _: membership, "offline-fixture")
    scope = SecSyncPersistence(db, candidate, force=False)
    fact = FundamentalFact(
        cik=company.cik,
        symbol="TEST",
        metric="cash",
        tag="Cash",
        value=Decimal("10"),
        unit="USD",
        period_end=date(2026, 9, 30),
        filed=date(2026, 10, 31),
        form="10-Q",
        accession_number="0000001234-26-000001",
    )

    def request(observation, value, admissions=membership):
        return SecRequestEnvelope.capture(
            observation_id=observation,
            company=company,
            identity=RequestIdentity(company.symbol, company.cik, "Synthetic identity"),
            retrieved_at=at + timedelta(days=int(observation == "correction")),
            submissions={"cik": company.cik},
            companyfacts={"cik": 1234, "facts": {}, "fixture_value": value},
            parser_version="offline-fixture",
            policy_version=candidate.policy_version,
            facts=(fact.model_copy(update={"value": Decimal(value)}),),
            accessions=(fact.accession_number,),
            admissions=admissions,
        )

    assert scope.version() == 1
    archive = target.with_name(target.name + ".sec-requests") / scope.binding
    assert os.path.samefile(scope.archive.directory, archive)
    assert rows(db, "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1") == [
        (report["lineage"]["dataset_id"], MANIFEST_SHA),
    ]
    with pytest.raises(ValueError, match="SOURCE_MEMBERSHIP_ADMISSION_REQUIRED"):
        scope.backend.persist(request("missing-admission", "10", {}), expected_version=1)
    assert scope.version() == 1
    assert rows(db, "SELECT * FROM ingestion_receipt") == []
    initial = request("first", "10")
    assert scope.backend.persist(initial, expected_version=1).dataset_version == 2
    assert (
        scope.backend.persist(request("correction", "20"), expected_version=2).dataset_version == 3
    )
    restarted = SecSyncPersistence(db, candidate, force=False)
    assert restarted.binding == scope.binding
    restored = restarted.archive.load(initial.request_id)
    assert restarted.backend.persist(restored, expected_version=1).replayed
    assert restarted.version() == 3
    assert rows(db, "SELECT version FROM ingestion_receipt ORDER BY version") == [(2,), (3,)]
    assert rows(db, "SELECT value FROM canonical_fact") == [("20",)]
    assert rows(
        db,
        "SELECT original_id,symbol,taxonomy,version,legacy_admitted,"
        "forward_order_admitted FROM source_occurrence",
    ) == [(None, "TEST", "us-gaap", 2, 1, 1)]
    assert rows(db, "SELECT version,value FROM fact_revision ORDER BY version") == [
        (2, "10"),
        (3, "20"),
    ]
    assert rows(
        db, "SELECT dataset_version,value FROM forward_occ_versions_v1 ORDER BY dataset_version"
    ) == [(2, "10"), (3, "20")]


def test_legacy_default(tmp_path):
    db = Database(tmp_path / "legacy.sqlite")
    db.initialize()
    assert rows(db, "SELECT type FROM sqlite_master WHERE name='fundamental_facts'") == [("table",)]
    assert rows(db, "SELECT * FROM fundamental_facts") == []
    assert rows(db, "SELECT name FROM sqlite_master WHERE name='fact_contract'") == []


def test_script_preflight_and_authorized_fixture(target, tmp_path):
    manifest = tmp_path / "reviewed.json"
    manifest.write_bytes(MANIFEST)
    script = clean.REPO / "scripts" / "bootstrap_f01_research_db.py"
    args = [
        sys.executable,
        str(script),
        "--target",
        str(target),
        "--manifest",
        str(manifest),
        "--manifest-sha256",
        MANIFEST_SHA,
    ]
    checked = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    evidence = json.loads(checked.stdout)
    assert evidence["status"] == "PREFLIGHT_ONLY"
    assert not target.parent.exists()
    blocked = subprocess.run([*args, "--execute"], capture_output=True, text=True, timeout=30)
    assert blocked.returncode == 2 and not target.parent.exists()
    published = subprocess.run(
        [
            *args,
            "--execute",
            "--confirm-create-research-only",
            "--expect-code-fingerprint",
            evidence["code_fingerprint"],
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert published.returncode == 0, published.stderr
    assert json.loads(published.stdout) == json.loads(
        (target.parent / "bootstrap_result.json").read_text(encoding="utf-8")
    )
