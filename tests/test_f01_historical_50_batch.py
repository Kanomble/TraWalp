"""Focused offline checks for the historical expansion and Canonical storage audit."""

import csv
import io
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from test_f01_canonical_ingestion import current, normalized, state, write

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import prepare_f01_historical_50_batch as batch  # noqa: E402
import validate_f01_canonical_assurance as assurance  # noqa: E402

from trading_system.data import canonical_bootstrap as clean  # noqa: E402

CIK = "0000000010"
ACC = "0000000099-15-000001"


@pytest.fixture
def d6():
    controls = sorted(batch.CONTROLS)
    universe, candidates, filings = [], [], {}
    columns = ("symbol_label", "planning_cik", "category")
    for i, symbol in enumerate([*controls, *[f"S{n:02}" for n in range(43)]], 1):
        cik = f"{i:010}"
        category = "E11_CONTROL" if symbol in controls else "DATED_MARKET_AND_ISSUER_CANDIDATE"
        universe.append(dict(zip(columns, (symbol, cik, category), strict=True)))
        candidates.append(
            {
                "symbol": symbol,
                "research_cik": cik,
                "category": category,
                "reasons": ["SECURITY_INTERVAL_UNPROVEN"],
            }
        )
        filing = i == 10
        filings[symbol] = {
            "cik": cik,
            "period_counts": {"early": {"missing_original_shard_keys": [ACC] if filing else []}},
            "filing_index_conflicts": [],
            "submissions_member_sha256": "a" * 64,
            "filings": {
                ACC: {
                    "filed": "2015-12-31",
                    "form": "10-K",
                    "acceptance": "2015-12-31T13:00:00Z",
                }
            }
            if filing
            else {},
        }
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(universe)
    return stream.getvalue().encode(), batch.json_bytes(
        {
            "contract": batch.D6_CONTRACT,
            "candidates": candidates,
            "issuer_filing_inventories": filings,
            "estimated_capacity": {
                "missing_distinct_periodic_accessions": 1,
                "reference_filing_count": 1,
                "reference_original_cache_bytes": 12,
                "reference_shard_bytes": 25,
            },
        }
    )


def test_bound_scope_distinct_gap_local_reuse_and_exclusive_publication(d6, tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    manifest, rows = batch.prepare(*d6, root=root)
    assert len(manifest["issuer_scope"]) == 50
    assert manifest["benchmark"] == "SPY"
    assert manifest["d6_gap_current_physical_status"] == {"PHYSICALLY_MISSING": 1}
    assert len(manifest["accessions"]) == 1
    assert manifest["accessions"][0]["cik"] == CIK
    assert manifest["capacity"]["estimated_new_original_bytes"] == 12

    folder = root / CIK / ACC
    folder.mkdir(parents=True)
    (folder / "doc.htm").write_text("fixture")
    assert batch.prepare(*d6, root=root)[1][0]["physical_status"] == "CACHE_INCOMPLETE"
    (folder / "index.json").write_text("{}")
    manifest, rows = batch.prepare(*d6, root=root, issuers=["S02"], limit=1)
    assert len(manifest["issuer_scope"]) == 1
    assert manifest["d6_gap_current_physical_status"] == {"ORIGINAL_PRESENT": 1}
    assert manifest["capacity"]["estimated_new_original_bytes"] == 0
    assert manifest["accessions"][0]["source_admission"] == "REVIEW_REQUIRED"
    assert manifest["accessions"][0]["accounting_admission"] == "REVIEW_REQUIRED"
    assert manifest["accessions"][0]["taxonomy"]["status"] == "DTS_CLOSURE_UNVERIFIED"

    output = tmp_path / "f01-historical-50-test"
    batch.publish(output, manifest, rows)
    saved = json.loads((output / "first_batch_manifest.json").read_bytes())
    for name, expected in saved["artifacts_sha256"].items():
        assert batch.sha((output / name).read_bytes()) == expected
    with pytest.raises(FileExistsError):
        batch.publish(output, manifest, rows)


def test_scope_and_metadata_rejection(d6, tmp_path):
    for scope in (["SPY"], ["S02", "S02"], ["UNKNOWN"]):
        with pytest.raises(ValueError, match="ISSUER_SCOPE"):
            batch.prepare(*d6, root=tmp_path, issuers=scope)
    with pytest.raises(ValueError, match="BATCH_LIMIT"):
        batch.prepare(*d6, root=tmp_path, limit=21)
    with pytest.raises(ValueError, match="DATE_RANGE"):
        batch.prepare(*d6, root=tmp_path, start=date(2000, 1, 1))
    with pytest.raises(ValueError, match="COHORT_OR_CIK"):
        batch.prepare(d6[0].replace(CIK.encode(), b"0000000090"), d6[1], root=tmp_path)
    scoped, _ = batch.prepare(*d6, root=tmp_path, issuers=["AAPL"])
    assert scoped["accessions"] == []


def test_extraction_inventory_remains_review_only(d6, tmp_path):
    filename = f"{CIK}-{ACC}.json"
    path = tmp_path / filename
    path.write_bytes(b"{}")
    record = {
        "cik": CIK,
        "accession": ACC,
        "file": filename,
        "bytes": 2,
        "sha256": batch.sha(b"{}"),
    }
    manifest = batch.json_bytes(
        {
            "contract": "F01_EXTENDED_FILING_EVIDENCE_V1",
            "filings": [record],
        }
    )
    _, rows = batch.prepare(
        *d6,
        root=tmp_path,
        extraction_raw=manifest,
        extraction_root=tmp_path,
    )
    assert rows[0]["extraction"] == "SHARD_SIZE_MATCH_REVIEW_REQUIRED"
    assert rows[0]["qualification"] == "UNVERIFIED_FOR_EXPANSION"
    assert rows[0]["physical_status"] == "PHYSICALLY_MISSING"
    path.unlink()
    _, rows = batch.prepare(
        *d6,
        root=tmp_path,
        extraction_raw=manifest,
        extraction_root=tmp_path,
    )
    assert rows[0]["extraction"] == "SHARD_UNVERIFIED"


def test_same_request_conflict_rejected_without_losing_prior_revision(tmp_path):
    db = normalized(tmp_path)
    a = current("cash", 10)
    changed = a.model_copy(update={"value": 21})
    with db.connect() as connection:
        assert write(connection, [a, a]) == write(connection, [a, a]) == 2
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM forward_occ_v1").fetchone()[0] == 1
        before = state(connection)
        for rows in ([a, changed], [changed, a]):
            with pytest.raises(ValueError, match="UNRESOLVED_CONTENT_CONFLICT"):
                write(connection, rows, request="new", version=2)
            assert state(connection) == before
        assert write(connection, [changed], request="new", version=2) == 3
        assert connection.execute("SELECT count(*) FROM canonical_fact").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_occurrence").fetchone()[0] == 1
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT value FROM forward_occ_versions_v1 ORDER BY dataset_version"
            )
        ] == [("10",), ("21",)]


def test_original_multiplicity_issuer_filing_and_period_keys(tmp_path):
    original = current("cash", 10)
    alias = original.model_copy(update={"symbol": "OLD"})
    issuer = original.model_copy(update={"cik": "0000009999", "symbol": "OTHER"})
    filing = original.model_copy(update={"accession_number": "OTHER-FILING"})
    duration = original.model_copy(update={"period_start": date(2024, 1, 1)})
    db = normalized(tmp_path, [original, alias, issuer, filing, duration])
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM canonical_fact").fetchone()[0] == 4
        rows = connection.execute(
            "SELECT fact_id, original_id, symbol FROM source_occurrence ORDER BY original_id"
        ).fetchall()
        assert [row["symbol"] for row in rows[:2]] == ["TEST", "OLD"]
        assert rows[0]["fact_id"] == rows[1]["fact_id"]
        assert len({row["original_id"] for row in rows}) == 5


@pytest.fixture
def isolated(tmp_path):
    database = tmp_path / "f01-sec-isolated-research-test" / clean.DB_NAME
    manifest = batch.json_bytes(
        {
            "contract": clean.BOOTSTRAP_CONTRACT,
            "classification": clean.CLASSIFICATION,
            "research_scope": "synthetic expansion assurance",
        }
    )
    digest = batch.sha(manifest)
    clean.bootstrap(
        database,
        manifest=manifest,
        manifest_sha256=digest,
        authorized=True,
        expected_code_fingerprint="a" * 64,
        verify_code=lambda: "a" * 64,
    )
    return database, digest


def test_assurance_clean_isolated_and_pit_witness(isolated):
    import sqlite3
    from contextlib import closing

    database, digest = isolated
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert write(connection, [current("cash", 10)]) == 2
        assert write(connection, [current("cash", 11)], request="new", version=2) == 3
    before = database.read_bytes()
    witness = {
        "fact_id": 1,
        "dataset_version": 2,
        "accession": "A",
        "value": "10",
        "decision_at": "2025-02-09T20:00:00Z",
        "accepted_at": "2025-02-09T19:00:00Z",
    }
    report = assurance.inspect_target(database, expected_manifest=digest, witnesses=[witness])
    assert report["status"] == "STORAGE_CHECKS_PASS"
    assert report["counts"]["source_occurrence"] == 1
    assert report["facts_with_multiple_revisions_for_review"] == 1
    assert report["pit"]["status"] == "CHECKED"
    report = assurance.inspect_target(
        database,
        expected_manifest=digest,
        witnesses=[
            witness | {"accepted_at": "2025-02-09T20:00:01Z"},
            witness | {"dataset_version": 3},
        ],
    )
    assert report["status"] == "FINDINGS"
    assert len(report["pit"]["failures"]) == 2
    assert database.read_bytes() == before
    assert not list(database.parent.glob("*.sqlite3-*"))


def test_assurance_detects_drift_and_rejects_unsafe_targets(isolated, monkeypatch):
    import sqlite3
    from contextlib import closing

    database, digest = isolated
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        write(connection, [current("cash", 10)])
        connection.execute("DROP INDEX canonical_instant")
        connection.execute(
            "INSERT INTO canonical_fact SELECT 999,cik,metric,tag,value,unit,period_start,"
            "period_end,filed,fiscal_year,fiscal_period,form,accession_number,frame"
            " FROM canonical_fact"
        )
        connection.execute("UPDATE source_occurrence SET evidence=''")
    report = assurance.inspect_target(database, expected_manifest=digest)
    assert report["status"] == "FINDINGS"
    assert report["schema_error"] == "CANONICAL_SCHEMA_OR_FK_GUARD"
    assert report["violations"]["canonical_identity_collision_groups"] == 1
    assert report["violations"]["missing_source_bindings"] == 1
    with pytest.raises(ValueError, match="MANIFEST_BINDING"):
        assurance.inspect_target(database, expected_manifest="0" * 64)

    def prohibited(*args, **kwargs):
        pytest.fail("Protected target opened")

    monkeypatch.setattr(sqlite3, "connect", prohibited)
    with pytest.raises(ValueError, match="ISOLATED_RESEARCH_TARGET"):
        assurance.inspect_target(
            assurance.REPO / "data" / "trading_system.sqlite3", expected_manifest=digest
        )
    with pytest.raises(ValueError, match="ISOLATED_RESEARCH_TARGET"):
        assurance.inspect_target(
            database.parent.parent / "f01-sec-isolated-research-accounting-e11" / clean.DB_NAME,
            expected_manifest=digest,
        )
    Path(str(database) + "-wal").touch()
    with pytest.raises(ValueError, match="SQLITE_SIDECAR"):
        assurance.inspect_target(database, expected_manifest=digest)


def test_assurance_cli_requires_explicit_authorization(isolated):
    database, digest = isolated
    args = [
        sys.executable,
        "-B",
        str(SCRIPTS / "validate_f01_canonical_assurance.py"),
        "--target",
        str(database),
        "--expect-bootstrap-manifest-sha256",
        digest,
    ]
    assert subprocess.run(args, capture_output=True).returncode == 2
    result = subprocess.run([*args, "--authorize-isolated-read"], capture_output=True, check=True)
    assert json.loads(result.stdout)["status"] == "STORAGE_CHECKS_PASS"
