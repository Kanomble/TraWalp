"""Exercise the public SEC sync entry points with synthetic inputs only."""

import sqlite3
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import requests
from test_sync import IncrementalSec, _company_facts, _daily_directory, _seed_assets

from trading_system.config import load_strategy_config
from trading_system.data import sec_persistence
from trading_system.data.canonical_facts import Admission, build_snapshot
from trading_system.data.database import Database
from trading_system.data.sec_force import SecWriteDatabase
from trading_system.data.sec_sync_persistence import (
    PENDING,
    IsolatedCanonicalSec,
    SecSyncPersistence,
)
from trading_system.data.sync import DataSynchronizer
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.screening import ScreenReport
from trading_system.shadow.readiness import evaluate_shadow_input_readiness

CIK = "0000001234"
NOW = datetime(2026, 11, 20, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No HTTP in the integration suite")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


class Provider(IncrementalSec):
    value = 100

    def daily_master_index_directory(self, year, quarter):
        if (year, quarter) == (NOW.year, (NOW.month - 1) // 3 + 1):
            return _daily_directory(NOW.date())
        return _daily_directory()

    def company_facts(self, cik):
        self.fact_calls += 1
        return {**_company_facts(self.accession, self.value), "cik": cik}


def scope(directory, *, allow=True):
    def admissions(request):
        return {
            (fact.symbol, fact.taxonomy): Admission(True, True, "synthetic fixture decision")
            for fact in request.facts
            if allow
        }

    return IsolatedCanonicalSec(directory, admissions, "synthetic-policy-v1")


@pytest.fixture
def canonical(tmp_path):
    directory = tmp_path / "f01-sec-isolated-fixture"
    directory.mkdir()
    db = Database(directory / "f01-sec-candidate.sqlite3")
    db.initialize()
    _seed_assets(db, "TEST")
    with db.connect() as connection:
        build_snapshot(connection, dataset_id="fixture", source_hash="a" * 64)
    return db


@pytest.fixture
def legacy(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3")
    db.initialize()
    _seed_assets(db, "TEST")
    return db


def sync(db, provider, candidate=None):
    return DataSynchronizer(db, None, provider, clock=lambda: NOW, isolated_canonical_sec=candidate)


def values(db, query):
    with db.read_only() as connection:
        return [tuple(row) for row in connection.execute(query)]


def readiness(db):
    inputs = SimpleNamespace(
        sync_values=db.sync_values,
        history_coverage=lambda *_: {},
        bars_on_session=lambda *_: {},
        provenance={},
    )
    screen = SimpleNamespace(
        screen=lambda session: ScreenReport(
            as_of=session,
            generated_at=NOW.isoformat(),
            analyzed_count=0,
            eligible_count=0,
            records=(),
        )
    )
    return evaluate_shadow_input_readiness(
        inputs,
        load_strategy_config(),
        now=NOW,
        session=date(2026, 11, 20),
        screen_source=screen,
    )


def test_legacy_full_and_incremental_use_receipts(legacy):
    provider = Provider()
    synchronizer = sync(legacy, provider)
    assert synchronizer.sync_sec_full()["facts_processed"] == 1
    assert synchronizer.sync_sec_incremental()["facts_processed"] == 0
    assert provider.fact_calls == 1
    assert len(legacy.sync_values(sec_persistence.LEGACY_RECEIPTS)) == 1
    assert values(legacy, "SELECT value FROM fundamental_facts") == [("100",)]
    assert legacy.sync_value("dataset", "sec")["status"] == "success"


def test_canonical_correction_and_older_confirmed_request_keep_newer_state(canonical, monkeypatch):
    candidate = scope(canonical.path.parent)
    provider = Provider()
    original = SecSyncPersistence.submit
    markers = []

    def submit(self, envelope):
        outcome = original(self, envelope)
        markers.append(canonical.sync_value(PENDING, CIK))
        return outcome

    monkeypatch.setattr(SecSyncPersistence, "submit", submit)
    synchronizer = sync(canonical, provider, candidate)
    assert synchronizer.sync_sec_full()["facts_processed"] == 1
    provider.value = 110
    assert synchronizer.sync_sec_full()["facts_processed"] == 1
    assert values(
        canonical, "SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=2"
    ) == [("100",)]
    assert values(
        canonical, "SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=3"
    ) == [("110",)]
    canonical.set_sync_value(PENDING, CIK, markers[0])
    canonical.set_sync_value("sec_submissions_status", CIK, {"status": "newer"})
    before = canonical.sync_value("dataset", "sec")
    assert sync(canonical, provider, candidate).sync_sec_full()["replayed_receipts"] == 1
    assert canonical.sync_value("dataset", "sec") == before
    assert canonical.sync_value("sec_submissions_status", CIK) == {"status": "newer"}
    assert values(canonical, "SELECT current_version FROM fact_contract") == [(3,)]
    assert values(canonical, "SELECT count(*) FROM source_occurrence") == [(1,)]
    assert provider.fact_calls == 2
    assert "RANKING_UNRESOLVED" in readiness(canonical).details["blocking_reasons"]


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_faults_and_restart(canonical, monkeypatch, after_commit):
    candidate = scope(canonical.path.parent)
    provider = Provider()
    original = sec_persistence.CanonicalSecPersistence._connect

    class BrokenCommit(sqlite3.Connection):
        def commit(self):
            if after_commit:
                super().commit()
            raise sqlite3.OperationalError("synthetic commit fault")

    def faulty(self):
        return sqlite3.connect(self.path, isolation_level=None, factory=BrokenCommit)

    with monkeypatch.context() as m:
        m.setattr(sec_persistence.CanonicalSecPersistence, "_connect", faulty)
        result = sync(canonical, provider, candidate).sync_sec_full()
    if after_commit:
        assert result["errors"] == 0 and result["reconciled_commits"] == 1
    else:
        assert result["errors"] == result["database_failures"] == 1
        assert canonical.sync_value(PENDING, CIK)
        assert values(canonical, "SELECT count(*) FROM canonical_fact") == [(0,)]
        assert values(canonical, "SELECT count(*) FROM fact_revision") == [(0,)]
        assert values(canonical, "SELECT count(*) FROM ingestion_receipt") == [(1,)]
        assert canonical.sync_value("sec_accessions", CIK) is None
        assert canonical.sync_value("dataset", "sec")["last_success_at"] is None
        assert "SEC_ERRORS_PRESENT" in readiness(canonical).details["blocking_reasons"]
        assert sec_persistence.CanonicalSecPersistence._connect is original
        assert sync(canonical, provider, candidate).sync_sec_full()["facts_processed"] == 1
    assert values(canonical, "SELECT count(*) FROM source_occurrence") == [(1,)]
    assert values(canonical, "SELECT current_version FROM fact_contract") == [(2,)]
    assert canonical.sync_value(PENDING, CIK) is None
    assert provider.fact_calls == 1


def test_wrong_schema_target_and_marker_fail_closed(legacy, canonical):
    provider = Provider()
    candidate = scope(canonical.path.parent)
    with pytest.raises(ValueError, match="SCHEMA_SCOPE_MISMATCH"):
        sync(canonical, provider).sync_sec_full()
    with pytest.raises(ValueError, match="ISOLATED_TARGET_REQUIRED"):
        sync(legacy, provider, candidate).sync_sec_full()
    assert provider.submission_calls == provider.fact_calls == 0
    canonical.set_sync_value(PENDING, CIK, {"binding": "another target", "force": False})
    assert sync(canonical, provider, candidate).sync_sec_full()["database_failures"] == 1
    assert provider.fact_calls == 0


def test_canonical_incremental_cursor_and_stage_failure_are_atomic(canonical, monkeypatch):
    candidate = scope(canonical.path.parent)
    provider = Provider()
    original = canonical.set_sync_value

    def fail_finalize(source, key, value, **kwargs):
        original(source, key, value, **kwargs)
        if (source, key) == ("dataset", "sec") and value["status"] == "success":
            raise sqlite3.OperationalError("synthetic stage finalization failure")

    with monkeypatch.context() as m:
        m.setattr(canonical, "set_sync_value", fail_finalize)
        with pytest.raises(sqlite3.OperationalError, match="finalization failure"):
            sync(canonical, provider, candidate).sync_sec_incremental()
    assert values(canonical, "SELECT current_version FROM fact_contract") == [(2,)]
    assert canonical.sync_value(PENDING, CIK) is not None
    assert canonical.sync_value("sec_change_detection", "daily_master_index") is None
    assert canonical.sync_value("dataset", "sec")["status"] == "running"
    assert sync(canonical, provider, candidate).sync_sec_incremental()["replayed_receipts"] == 1
    assert canonical.sync_value("dataset", "sec")["status"] == "running"
    assert sync(canonical, provider, candidate).sync_sec_incremental()["errors"] == 0
    assert canonical.sync_value("sec_change_detection", "daily_master_index") is not None
    assert canonical.sync_value("dataset", "sec")["status"] == "success"
    assert provider.fact_calls == 1


def test_force_uses_guarded_legacy_writer_and_separate_stage(legacy):
    legacy.upsert_company(CompanyIdentity(cik=CIK, symbol="TEST", name="Synthetic"))
    guarded = SecWriteDatabase(legacy.path)
    provider = Provider()
    result = sync(guarded, provider).sync_sec_full(["TEST"], companyfacts_only=True)
    assert result["facts_processed"] == 1
    assert guarded.authorized_write_tables == {"companies", "fundamental_facts", "sync_state"}
    assert provider.submission_calls == provider.ticker_calls == 0
    assert len(legacy.sync_values(sec_persistence.LEGACY_RECEIPTS)) == 1
    assert legacy.sync_value("dataset", "sec") is None


@pytest.mark.parametrize("identity", [False, True])
def test_admission_or_identity_failure(canonical, identity):
    provider = Provider()
    if identity:
        provider.submissions = lambda _: {"cik": 9999}
    result = sync(canonical, provider, scope(canonical.path.parent, allow=False)).sync_sec_full()
    assert result["errors"] == 1
    assert result["parse_failures" if identity else "database_failures"] == 1
    assert values(canonical, "SELECT current_version FROM fact_contract") == [(1,)]
    assert canonical.sync_value("dataset", "sec")["status"] == "partial"
