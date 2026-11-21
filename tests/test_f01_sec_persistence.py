"""8F.3A synthetic persistence tests. No provider or existing snapshot access."""

import json
import socket
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.data import sec_persistence as sec
from trading_system.data.canonical_facts import Admission, RequestIdentity, build_snapshot
from trading_system.data.database import Database
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact

COMPANY = CompanyIdentity(cik="0000001234", symbol="TEST", name="Synthetic")
AT = datetime(2026, 11, 20, 12, tzinfo=UTC)
ADMISSION = Admission(True, True, "fixture membership evidence")
FACT = FundamentalFact(
    cik=COMPANY.cik,
    symbol="TEST",
    metric="cash",
    tag="Cash",
    value=Decimal("10"),
    unit="USD",
    filed=date(2026, 10, 31),
    period_end=date(2026, 9, 30),
    form="10-Q",
    accession_number="A",
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network use")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "requests"
    path.mkdir()
    return sec.SecRequestArchive(path)


@pytest.fixture
def legacy(tmp_path):
    db = Database(tmp_path / "legacy.sqlite")
    db.initialize()
    return db


@pytest.fixture
def canonical(tmp_path):
    db = Database(tmp_path / "canonical.sqlite")
    db.initialize()
    with db.connect() as c:
        build_snapshot(c, dataset_id="fixture", source_hash="a" * 64)
    return db


def envelope(observation="one", facts=(FACT,), *, canonical=True, **overrides):
    values = {
        "observation_id": observation,
        "company": COMPANY,
        "identity": RequestIdentity("TEST", COMPANY.cik, "fixture assignment"),
        "retrieved_at": AT,
        "submissions": {"cik": COMPANY.cik, "name": "Synthetic"},
        "companyfacts": {"cik": 1234, "facts": {}},
        "parser_version": "xbrl-fixture-v1",
        "policy_version": "fixture-policy-v1",
        "facts": facts,
        "accessions": ("A", "B"),
        "admissions": {(f.symbol, f.taxonomy): ADMISSION for f in facts} if canonical else {},
    }
    return sec.SecRequestEnvelope.capture(**(values | overrides))


def rows(db, sql):
    with db.connect() as c:
        return [tuple(row) for row in c.execute(sql)]


def test_explicit_schema_isolation_and_legacy_receipt(legacy, canonical, archive):
    with pytest.raises(ValueError, match="LEGACY_WRITER_ON_CANONICAL_SCHEMA"):
        sec.LegacySecPersistence(canonical.path, archive).persist(
            envelope("legacy", canonical=False)
        )
    with pytest.raises(ValueError, match="CANONICAL_SEC_SCHEMA_REQUIRED"):
        sec.CanonicalSecPersistence(legacy.path, archive).persist(envelope(), expected_version=1)
    assert rows(legacy, "SELECT * FROM fundamental_facts") == []
    assert rows(canonical, "SELECT current_version FROM fact_contract") == [(1,)]

    backend = sec.LegacySecPersistence(legacy.path, archive)
    request = envelope("legacy", canonical=False)
    legacy.set_sync_value("sec_submissions_status", COMPANY.cik, {"status": "unavailable"})
    first = backend.persist(request)
    assert first == sec.SecPersistOutcome(None, request.request_id, False, 1)
    assert legacy.sync_value("sec_submissions_status", COMPANY.cik) is None
    legacy.set_sync_value("sec_submissions_status", COMPANY.cik, {"status": "newer"})
    assert backend.persist(request).replayed
    assert legacy.sync_value("sec_submissions_status", COMPANY.cik) == {"status": "newer"}


def test_restart_and_replay_keep_newer_state(canonical, archive):
    request = envelope()
    backend = sec.CanonicalSecPersistence(canonical.path, archive)
    first = backend.persist(request, expected_version=1)
    assert first == sec.SecPersistOutcome(2, request.request_id, False, 1)
    restart_archive = sec.SecRequestArchive(archive.directory)
    restored = restart_archive.load(request.request_id)
    assert restored.archive_bytes() == request.archive_bytes()
    assert restored.facts == (FACT,)
    restarted = sec.CanonicalSecPersistence(canonical.path, restart_archive)
    repeated = envelope("new-observation", accessions=("A", "B", "C"))
    assert (
        repeated.request_id != request.request_id and repeated.payload_hash == request.payload_hash
    )
    assert restarted.persist(repeated, expected_version=2).dataset_version == 3
    canonical.set_sync_value("sec_companyfacts_status", COMPANY.cik, {"status": "newer"})
    canonical.set_sync_value("sec_submissions_status", COMPANY.cik, {"status": "newer"})
    replay = restarted.persist(restored, expected_version=1)
    assert replay == sec.SecPersistOutcome(2, request.request_id, True, 1)
    assert canonical.sync_value("sec_accessions", COMPANY.cik) == ["A", "B", "C"]
    assert canonical.sync_value("sec_companyfacts_status", COMPANY.cik) == {"status": "newer"}
    assert canonical.sync_value("sec_submissions_status", COMPANY.cik) == {"status": "newer"}
    assert rows(canonical, "SELECT COUNT(*) FROM source_occurrence") == [(1,)]
    with pytest.raises(ValueError, match="SEC_REQUEST_ARCHIVE_CONFLICT"):
        restarted.persist(envelope(policy_version="changed-policy"), expected_version=3)
    with pytest.raises(ValueError, match="STALE_DATASET_VERSION"):
        restarted.persist(envelope("stale", accessions=("A", "B", "C")), expected_version=2)
    with pytest.raises(ValueError, match="STALE_SEC_ACCESSIONS"):
        restarted.persist(envelope("stale-accessions"), expected_version=3)
    with canonical.connect() as c:
        c.execute(
            "UPDATE ingestion_receipt SET input_hash='altered' WHERE request_id=?",
            (request.request_id,),
        )
    with pytest.raises(ValueError, match="RECEIPT_INPUT_CONFLICT"):
        restarted.persist(restored, expected_version=3)


def test_corrections_amendments_and_membership(canonical, archive):
    backend = sec.CanonicalSecPersistence(canonical.path, archive)
    for request in (
        envelope(admissions={}),
        envelope("no-anchor", admissions={("TEST", "us-gaap"): Admission(True, False, "fixture")}),
    ):
        with pytest.raises(
            ValueError,
            match="SOURCE_MEMBERSHIP_ADMISSION_REQUIRED|NEW_FACT_REQUIRES_FORWARD_ANCHOR",
        ):
            backend.persist(request, expected_version=1)
    backend.persist(envelope(), expected_version=1)
    correction = FACT.model_copy(update={"value": Decimal("20")})
    backend.persist(envelope("correction", (correction,)), expected_version=2)
    alias = correction.model_copy(update={"symbol": "ALIAS", "taxonomy": "dei"})
    backend.persist(envelope("label", (alias,)), expected_version=3)
    amendment = correction.model_copy(
        update={
            "filed": date(2026, 11, 15),
            "form": "10-Q/A",
            "accession_number": "B",
        }
    )
    backend.persist(envelope("amendment", (amendment,)), expected_version=4)
    assert rows(canonical, "SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=2") == [
        ("10",)
    ]
    assert rows(canonical, "SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=3") == [
        ("20",)
    ]
    assert rows(
        canonical, "SELECT symbol,forward_order_admitted FROM source_occurrence ORDER BY id"
    ) == [
        ("TEST", 1),
        ("ALIAS", 0),
        ("TEST", 1),
    ]
    assert rows(canonical, "SELECT accession_number FROM forward_occ_v1 ORDER BY id") == [
        ("A",),
        ("B",),
    ]


def test_identity_rejection_including_alias_and_company_rename(canonical, archive):
    for key, payload in (
        ("companyfacts", {"cik": True, "facts": {}}),
        ("submissions", {"cik": "9999"}),
    ):
        with pytest.raises(ValueError, match="SEC_PROVIDER_CIK_"):
            envelope(**{key: payload})
    backend = sec.CanonicalSecPersistence(canonical.path, archive)
    canonical.set_sync_value("sec_identity_conflicts", "ALIAS", {"status": "unresolved"})
    alias = FACT.model_copy(update={"symbol": "ALIAS"})
    with pytest.raises(ValueError, match="IDENTITY_ASSIGNMENT_REJECTED"):
        backend.persist(envelope("alias", (alias,)), expected_version=1)
    backend.persist(envelope(), expected_version=1)
    renamed = envelope(
        "rename",
        (),
        company=COMPANY.model_copy(update={"symbol": "OTHER"}),
        identity=RequestIdentity("OTHER", COMPANY.cik, "fixture proposal"),
    )
    with pytest.raises(ValueError, match="IDENTITY_ASSIGNMENT_REJECTED"):
        backend.persist(renamed, expected_version=2)
    assert rows(canonical, "SELECT symbol FROM companies") == [("TEST",)]


@pytest.mark.parametrize("phase", ["write", "before_commit", "after_commit"])
def test_atomic_write_and_commit_reconciliation(canonical, archive, monkeypatch, phase):
    backend = sec.CanonicalSecPersistence(canonical.path, archive)
    canonical.set_sync_value("sec_companyfacts_status", COMPANY.cik, {"status": "before"})
    canonical.set_sync_value("sec_submissions_status", COMPANY.cik, {"status": "before"})
    request = envelope()
    if phase == "write":
        writer = sec.write_company

        def fail_after_write(*args, **kwargs):
            writer(*args, **kwargs)
            raise sqlite3.OperationalError("injected write error")

        monkeypatch.setattr(sec, "write_company", fail_after_write)
    else:

        class FaultyCommit(sqlite3.Connection):
            def commit(self):
                if phase == "after_commit":
                    super().commit()
                raise sqlite3.OperationalError("injected commit error")

        original = backend._connect
        fault = sqlite3.connect(canonical.path, isolation_level=None, factory=FaultyCommit)
        connections = iter((fault,))
        monkeypatch.setattr(backend, "_connect", lambda: next(connections, None) or original())
    if phase == "after_commit":
        result = backend.persist(request, expected_version=1)
        assert result == sec.SecPersistOutcome(
            2, request.request_id, False, 1, commit_reconciled=True
        )
        assert backend.persist(request, expected_version=1).replayed
        assert canonical.sync_value("sec_submissions_status", COMPANY.cik) is None
        assert rows(canonical, "SELECT COUNT(*) FROM source_occurrence") == [(1,)]
    else:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            backend.persist(request, expected_version=1)
        assert rows(canonical, "SELECT current_version FROM fact_contract") == [(1,)]
        for table in ("companies", "canonical_fact", "fact_revision", "source_occurrence"):
            assert rows(canonical, f"SELECT COUNT(*) FROM {table}") == [(0,)]
        assert rows(canonical, "SELECT COUNT(*) FROM ingestion_receipt") == [(1,)]  # snapshot
        assert canonical.sync_value("sec_accessions", COMPANY.cik) is None
        assert canonical.sync_value("sec_companyfacts_status", COMPANY.cik) == {"status": "before"}
        assert canonical.sync_value("sec_submissions_status", COMPANY.cik) == {"status": "before"}
        assert archive.load(request.request_id) == request


def test_archive_detects_corruption(archive):
    request = envelope()
    archive.store(request)
    path = archive.directory / (request.request_id + ".json")
    contents = json.loads(path.read_bytes())
    contents["envelope"]["parser_version"] = "changed"
    path.write_text(json.dumps(contents), encoding="utf-8")
    with pytest.raises(ValueError, match="SEC_REQUEST_ARCHIVE_INTEGRITY_FAILURE"):
        archive.load(request.request_id)
