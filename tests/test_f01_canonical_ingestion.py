"""A-V1 implementation tests. Synthetic SQLite only; network is forbidden."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from itertools import permutations

import pytest
from test_champion_consolidation import offline as offline
from test_f01_duplicate_impact import ASOF, current, inputs, ranked, scored, seeded
from test_f01_survivor_resolution import read

from trading_system.data.canonical_facts import (
    Admission,
    RequestIdentity,
    build_snapshot,
    install_writer_guard,
    write_company,
)
from trading_system.fundamentals.debug import debug_fundamentals
from trading_system.fundamentals.quality import accounting_state_as_of
from trading_system.models.fundamentals import CompanyIdentity

COMPANY = CompanyIdentity(cik="0000001234", symbol="TEST", name="Synthetic")
IDENTITY = RequestIdentity("TEST", COMPANY.cik, "fixture verified assignment")
ADMIT = Admission(True, True, "synthetic source evidence")


def normalized(tmp_path, facts=()):
    db = seeded(tmp_path, facts)
    with db.connect() as c:
        build_snapshot(c, dataset_id="fixture", source_hash="a" * 64)
    return db


def write(c, facts, request="request", version=1, **kwargs):
    return write_company(
        c,
        COMPANY,
        facts,
        ["A", "AMEND"],
        request_id=request,
        payload_hash="b" * 64,
        retrieved_at="2026-11-15T00:00:00Z",
        identity=kwargs.pop("identity", IDENTITY),
        admissions=kwargs.pop("admissions", {("TEST", "us-gaap"): ADMIT}),
        expected_version=version,
        **kwargs,
    )


def state(c):
    names = [
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return {n: c.execute("SELECT * FROM " + n + " ORDER BY rowid").fetchall() for n in names}


@pytest.mark.parametrize("order", sorted(set(permutations("AABB"))))
def test_real_implementation_lossless_readers_selectors_scores(tmp_path, order):
    a = current("cash", 10).model_copy(update={"symbol": "OLD"})
    b = current("cash", 200, "B")
    da, dbb = current("debt_current", 10), current("debt_current", 200, "B")
    facts = inputs() + [a if x == "A" else b for x in order] + [da, dbb, da]
    facts += [
        current("unrecognized_metric", 5),
        a.model_copy(update={"cik": "0000009999"}),
        b.model_copy(update={"accession_number": "AMEND", "filed": date(2025, 3, 4)}),
    ]
    db = seeded(tmp_path, facts)
    queries = [
        (r, s, d)
        for r, s in [("legacy", "OLD"), ("legacy", "TEST"), ("forward", "TEST")]
        for d in [date(2025, 2, 8), ASOF, date(2025, 3, 4)]
    ]
    before = [read(db, r, d, s) for r, s, d in queries]
    batch = list(db.iter_fact_batches(["OLD", "TEST"], ASOF, retain_latest_periods=2))
    with db.connect() as c:
        original = [tuple(r) for r in c.execute("SELECT * FROM fundamental_facts ORDER BY id")]
        build_snapshot(c, dataset_id="synthetic", source_hash="a" * 64)
        assert original == [tuple(r) for r in c.execute("SELECT * FROM legacy_occ_v1 ORDER BY id")]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    for (r, s, d), expected in zip(queries, before, strict=True):
        actual = read(db, r, d, s)
        assert actual == expected
        assert accounting_state_as_of(actual, d) == accounting_state_as_of(expected, d)
        assert debug_fundamentals(s, actual, d) == debug_fundamentals(s, expected, d)
        assert scored(actual, d) == scored(expected, d)
        assert ranked([scored(actual, d)[1]]) == ranked([scored(expected, d)[1]])
    assert batch == list(db.iter_fact_batches(["OLD", "TEST"], ASOF, retain_latest_periods=2))


def test_new_mixed_replay_correction_membership_amendment_versions(tmp_path):
    db = normalized(tmp_path)
    a = current("cash", 10).model_copy(update={"accession_number": None, "frame": None})
    duration = current("revenue", 30).model_copy(update={"period_start": date(2024, 10, 1)})
    with db.connect() as c:
        install_writer_guard(c)
        assert write(c, [a, duration, current("unknown", 5)]) == 2
        ids = [tuple(r) for r in c.execute("SELECT id,metric FROM forward_occ_v1 ORDER BY id")]
        assert [r[1] for r in ids] == ["cash", "revenue", "unknown"]
        assert write(c, [a, duration, current("unknown", 5)]) == 2
        assert write(c, [a], request="full-repeat", version=2) == 3
        assert c.execute("SELECT COUNT(*) FROM source_occurrence").fetchone()[0] == 3
        correction = a.model_copy(update={"value": 20})
        write(c, [correction], request="correction", version=3)
        assert (
            c.execute(
                "SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=2 "
                "AND metric='cash'"
            ).fetchone()[0]
            == "10"
        )
        alias = correction.model_copy(update={"symbol": "OLD", "taxonomy": "dei"})
        write(c, [alias], request="alias", version=4, admissions={("OLD", "dei"): ADMIT})
        assert c.execute("SELECT COUNT(*) FROM forward_occ_v1").fetchone()[0] == 3
        assert c.execute("SELECT value FROM legacy_occ_v1 WHERE symbol='OLD'").fetchone()[0] == "20"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM legacy_occ_versions_v1 WHERE dataset_version=4 "
                "AND symbol='OLD'"
            ).fetchone()[0]
            == 0
        )
        amendment = a.model_copy(update={"accession_number": "AMEND", "filed": date(2025, 3, 4)})
        write(c, [amendment], request="amend", version=5)
        assert c.execute("SELECT COUNT(*) FROM canonical_fact").fetchone()[0] == 4
        assert [tuple(r) for r in c.execute("SELECT id,metric FROM forward_occ_v1 ORDER BY id")][
            :3
        ] == ids
        with pytest.raises(ValueError, match="RECEIPT_INPUT_CONFLICT"):
            write(c, [correction])


@pytest.mark.parametrize(
    "table",
    [
        "companies",
        "canonical_fact",
        "fact_revision",
        "source_occurrence",
        "ingestion_receipt",
        "sync_state",
        "fact_contract",
        "fact_dataset",
    ],
)
def test_failure_at_each_write_rolls_back_even_when_external_caller_catches(tmp_path, table):
    db = normalized(tmp_path)
    with db.connect() as c:
        c.execute(
            f"CREATE TRIGGER fail_write BEFORE "
            f"{'UPDATE' if table == 'fact_contract' else 'INSERT'} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        c.execute("BEGIN IMMEDIATE")
        before = state(c)
        with pytest.raises(sqlite3.DatabaseError, match="injected"):
            write(c, [current("cash", 10)])
        assert state(c) == before
        assert c.in_transaction


def test_external_rollback_and_admission_identity_failures(tmp_path):
    db = normalized(tmp_path)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        before = state(c)
        write(c, [current("cash", 10)])
        c.rollback()
        assert state(c) == before
        for bad in [
            current("cash", 10).model_copy(update={"cik": "0000009999"}),
            current("cash", 10).model_copy(update={"symbol": "UNADMITTED"}),
        ]:
            with pytest.raises(ValueError):
                write(c, [current("revenue", 50), bad])
            assert state(c) == before
        with pytest.raises(ValueError):
            write(c, [], identity=RequestIdentity("TEST", COMPANY.cik, "evidence", True))
        assert state(c) == before


def test_concurrent_same_request_and_restart(tmp_path):
    db = normalized(tmp_path)

    def worker(_):
        with db.connect() as c:
            return write(c, [current("cash", 10)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(worker, range(2))) == [2, 2]
    assert worker(3) == 2
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ingestion_receipt").fetchone()[0] == 2
        with pytest.raises(ValueError, match="STALE_DATASET"):
            write(c, [], request="stale")
        c.execute("BEGIN IMMEDIATE")
        other = sqlite3.connect(db.path, timeout=0)
        other.execute("PRAGMA foreign_keys=ON")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                write(other, [current("cash", 10)], request="locked", version=2)
        finally:
            other.close()
        c.rollback()


def test_schema_cross_guards_and_snapshot_conflict(tmp_path):
    legacy = seeded(tmp_path, [current("cash", 10), current("cash", 20)])
    with legacy.connect() as c:
        before = state(c)
        with pytest.raises(sqlite3.OperationalError):
            write(c, [])
        with pytest.raises(ValueError, match="UNRESOLVED_CONTENT"):
            build_snapshot(c, dataset_id="conflict", source_hash="a" * 64)
        assert state(c) == before
    clean = tmp_path / "clean"
    clean.mkdir()
    db = normalized(clean)
    with pytest.raises(ValueError, match="LEGACY_WRITER"):
        db.upsert_sec_company_update(COMPANY, [current("cash", 10)], ["A"])
    with db.connect() as c:
        install_writer_guard(c)
        for sql in [
            "DELETE FROM canonical_fact",
            'UPDATE source_occurrence SET symbol="X"',
            "CREATE TABLE bypass(x)",
            'INSERT INTO assets(symbol) VALUES ("X")',
        ]:
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "unknown"},
        {"tag": "other"},
        {"unit": "EUR"},
        {"period_start": date(2024, 1, 1)},
        {"period_end": date(2024, 12, 30)},
        {"filed": date(2025, 2, 10)},
        {"accession_number": "NEW"},
        {"frame": "CY2024"},
    ],
)
def test_identity_dimensions_remain_distinct(tmp_path, change):
    db = normalized(tmp_path)
    a = current("cash", 10)
    with db.connect() as c:
        write(c, [a, a.model_copy(update=change)])
        assert c.execute("SELECT COUNT(*) FROM canonical_fact").fetchone()[0] == 2


def test_correction_preserves_every_imported_occurrence_and_old_version(tmp_path):
    a = current("cash", 10)
    db = normalized(tmp_path, [a, a.model_copy(update={"symbol": "OLD"}), a])
    with db.connect() as c:
        write(c, [a.model_copy(update={"value": 20})])
        assert [r[0] for r in c.execute("SELECT value FROM forward_occ_v1")] == ["20"] * 3
        assert [
            r[0]
            for r in c.execute("SELECT value FROM forward_occ_versions_v1 WHERE dataset_version=1")
        ] == ["10"] * 3
        assert c.execute("SELECT COUNT(*) FROM source_occurrence").fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO canonical_fact SELECT NULL,cik,metric,tag,value,unit,"
                "period_start,period_end,filed,fiscal_year,fiscal_period,form,"
                "accession_number,frame FROM canonical_fact"
            )


@pytest.mark.parametrize("status", ["unresolved", "resolved"])
def test_persisted_identity_conflict_admission(tmp_path, status):
    db = normalized(tmp_path)
    db.set_sync_value("sec_identity_conflicts", "TEST", {"status": status})
    with db.connect() as c:
        if status == "unresolved":
            before = state(c)
            with pytest.raises(ValueError, match="IDENTITY_ASSIGNMENT"):
                write(c, [current("cash", 10)])
            assert state(c) == before
        else:
            write(c, [current("cash", 10)])


def test_schema_drift_fails_before_mutation(tmp_path):
    db = normalized(tmp_path)
    with db.connect() as c:
        c.execute("DROP INDEX canonical_instant")
        before = state(c)
        with pytest.raises(ValueError, match="SCHEMA_OR_FK"):
            write(c, [current("cash", 10)])
        assert state(c) == before


def test_later_source_admission_is_versioned_and_never_moves_forward_anchor(tmp_path):
    db = normalized(tmp_path, [current("cash", 10)])
    alias = current("cash", 10).model_copy(update={"symbol": "OLD"})
    with db.connect() as c:
        write(c, [alias], admissions={("OLD", "us-gaap"): Admission(False, False, "pending")})
        assert c.execute("SELECT COUNT(*) FROM legacy_occ_v1 WHERE symbol='OLD'").fetchone()[0] == 0
        write(c, [alias], request="admitted", version=2, admissions={("OLD", "us-gaap"): ADMIT})
        assert c.execute("SELECT COUNT(*) FROM legacy_occ_v1 WHERE symbol='OLD'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM forward_occ_v1").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM legacy_occ_versions_v1 WHERE dataset_version=2 "
                "AND symbol='OLD'"
            ).fetchone()[0]
            == 0
        )


def test_failure_during_status_clear_restores_complete_company(tmp_path):
    db = normalized(tmp_path, [current("cash", 10)])
    db.set_sync_value("sec_companyfacts_status", COMPANY.cik, {"status": "failed"})
    with db.connect() as c:
        c.execute(
            "CREATE TRIGGER fail_status BEFORE DELETE ON sync_state "
            "BEGIN SELECT RAISE(ABORT,'status failure'); END"
        )
        before = state(c)
        with pytest.raises(sqlite3.DatabaseError, match="status failure"):
            write(c, [current("cash", 20)])
        assert state(c) == before
