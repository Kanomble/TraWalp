import json
import socket
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.data import canonical_bootstrap as bootstrap
from trading_system.data.canonical_facts import Admission, RequestIdentity
from trading_system.data.database import Database
from trading_system.data.sec_persistence import SecRequestEnvelope
from trading_system.data.sec_sync_persistence import IsolatedCanonicalSec, SecSyncPersistence
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_f01_sec_bulk_pilot as q  # noqa: E402

RETRIEVAL = "2026-11-21T18:01:00+00:00"
ACCESSION = "0000000001-20-000001"
HASH = "a" * 64


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Offline qualification tried to access the network")

    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected)


def sample_fact(cik="0000000001", symbol="T1", **extra):
    return FundamentalFact(
        **(
            {
                "cik": cik,
                "symbol": symbol,
                "metric": "revenue",
                "tag": "Revenues",
                "value": Decimal(0),
                "unit": "USD",
                "period_start": date(2019, 10, 1),
                "period_end": date(2019, 12, 31),
                "filed": date(2020, 1, 2),
                "form": "10-Q",
                "accession_number": ACCESSION,
                "fiscal_year": 2020,
                "fiscal_period": "Q1",
            }
            | extra
        )
    )


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    target = tmp_path / "f01-sec-isolated-research-fixture" / bootstrap.DB_NAME
    manifest = q.bulk.json_bytes(
        {
            "contract": bootstrap.BOOTSTRAP_CONTRACT,
            "classification": bootstrap.CLASSIFICATION,
            "research_scope": "Synthetic qualification fixtures",
        }
    )
    external = bootstrap.bootstrap(
        target,
        manifest=manifest,
        manifest_sha256=q.pilot.sha(manifest),
        expected_code_fingerprint=HASH,
        verify_code=lambda: HASH,
        authorized=True,
    )
    lineage = external["lineage"]
    monkeypatch.setattr(q.pilot, "RESEARCH_ROOT", tmp_path)
    monkeypatch.setattr(q, "TARGET", target)
    monkeypatch.setattr(q, "DATASET_ID", lineage["dataset_id"])
    monkeypatch.setattr(q, "COUNTS", dict.fromkeys(q.COUNTS, 14))
    monkeypatch.setattr(q, "ANCHORS", (date(2020, 1, 2), date(2020, 6, 30)))
    db = Database(target)
    admission = Admission(True, True, "synthetic-source")
    candidate = IsolatedCanonicalSec(target.parent, lambda _: {}, "fixture")
    persistence = SecSyncPersistence(db, candidate, force=False)
    active, requests, receipts, identities, rules = [], [], [], {}, {}
    for i in range(1, 8):
        cik, symbol = f"{i:010d}", f"T{i}"
        company = CompanyIdentity(cik=cik, symbol=symbol, name=symbol, sic="1111")
        identity = RequestIdentity(symbol, cik, "synthetic-identity")
        fact = sample_fact(cik, symbol)
        shares = sample_fact(
            cik,
            symbol,
            metric="shares_outstanding",
            taxonomy="dei",
            tag="EntityCommonStockSharesOutstanding",
            period_start=None,
            unit="shares",
            value=Decimal(100),
        )
        submissions = {
            "cik": cik,
            "fiscalYearEnd": "0930",
            "filings": {
                "recent": {
                    "accessionNumber": [ACCESSION],
                    "filingDate": ["2020-01-02"],
                    "form": ["10-Q"],
                    "acceptanceDateTime": ["2020-01-02T22:00:00+00:00"],
                    "primaryDocument": ["sample.htm"],
                    "reportDate": ["2019-12-31"],
                }
            },
            "_f01_bulk_evidence": {
                "manifest_sha256": q.pilot.SOURCE_SHA,
                "source_quality": q.bulk.QUALITY,
                "companyfacts_member_sha256": HASH,
                "submissions_member_sha256": HASH,
                "companyfacts_retrieved_at": RETRIEVAL,
                "submissions_retrieved_at": RETRIEVAL,
                "history": [],
            },
        }
        envelope = SecRequestEnvelope.capture(
            observation_id=f"fixture-{i}",
            company=company,
            identity=identity,
            retrieved_at=datetime.fromisoformat(RETRIEVAL),
            submissions=submissions,
            companyfacts={"cik": cik, "facts": {}},
            facts=[fact, shares],
            accessions=[ACCESSION],
            parser_version="fixture",
            policy_version="fixture",
            admissions={(symbol, taxonomy): admission for taxonomy in ("us-gaap", "dei")},
        )
        persistence.backend.persist(envelope, expected_version=i)
        active.append({"symbol": symbol, "cik": cik})
        requests.append([envelope.request_id, envelope.input_hash])
        receipts.append({"cik": cik, "dataset_version": i + 1})
        rules[cik] = (identity, {"us-gaap": admission, "dei": admission})
        identities[cik] = {
            "identity_checks": {"catalog": {"security_type": "COMMON_STOCK"}},
            "sources": {
                "companyfacts": {"member_sha256": HASH},
                "submissions": {"member_sha256": HASH, "history_members": []},
            },
        }
    info = target.stat()
    report = {
        "database_bytes_after": info.st_size,
        "issuers": receipts,
        "binding": {
            "active_issuers": active,
            "requests": requests,
            "target_file_identity": [info.st_dev, info.st_ino],
            "schema_fingerprint": external["schema_fingerprint"],
            "bootstrap_manifest_sha256": lineage["manifest_sha256"],
            "bootstrap_database_sha256": external["database_sha256_at_bootstrap"],
            "bootstrap_code_fingerprint": HASH,
        },
    }
    return target, report, rules, identities, persistence


def test_read_only_end_to_end(snapshot, tmp_path):
    target, report, rules, identities, _ = snapshot
    before = target.read_bytes()
    legacy = Database(tmp_path / "legacy.sqlite3")
    legacy.initialize()
    legacy.upsert_company(CompanyIdentity(cik="0000000001", symbol="T1", name="T1"))
    legacy.upsert_facts([sample_fact(value=Decimal(12))])
    output = q.run_phases(report, rules, identities, [*q.PHASES, "legacy"], legacy.path)
    assert target.read_bytes() == before
    assert output["data_errors"] == []
    assert len(output["coverage"]) == 49 and len(output["accounting"]) == 14
    assert output["accounting"][0]["selected_shares"]["taxonomy"] == "dei"
    assert output["accounting"][0]["ranking_status"] == "EVIDENCE_MISSING_MARKET_AND_PEERS"
    assert output["pit"][0]["matrix"][0]["accessions_by_issue"]["ACCEPTED_AFTER_SESSION_CLOSE"] == [
        ACCESSION
    ]
    assert (
        "sample.htm"
        in output["pit"][0]["original_filing_collection_candidates"][0]["primary_document_url"]
    )
    assert output["legacy"][0]["status"] == "DIFFERENCES_OBSERVED"
    assert output["legacy"][-1]["status"] == "EVIDENCE_MISSING_EXACT_SYMBOL_CIK"
    path = tmp_path / "published"
    status, result = q.publish(path, {"input_report_sha256": HASH}, output, q.PHASES)
    document = json.loads(result.read_bytes())
    assert status == "EVIDENCE_MISSING_FOR_HISTORICAL_USE"
    assert not document["historical_pit_qualified"] and not document["ranking_validated"]
    assert len(list(path.iterdir())) == 4
    with pytest.raises(ValueError, match="OUTPUT_ALREADY_EXISTS"):
        q.publish(path, {"input_report_sha256": HASH}, output, q.PHASES)


def test_rejects_archive_corruption_and_pending_sqlite(snapshot):
    target, report, rules, identities, persistence = snapshot
    journal = target.with_name(target.name + "-journal")
    journal.write_bytes(b"synthetic journal")
    with pytest.raises(q.QualificationBlocked, match="SIDECAR_PRESENT"):
        q.run_phases(report, rules, identities, ["coverage"], None)
    journal.unlink()
    path = persistence.archive.directory / (report["binding"]["requests"][0][0] + ".json")
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="SEC_REQUEST_ARCHIVE_INTEGRITY_FAILURE"):
        q.run_phases(report, rules, identities, ["coverage"], None)


def test_coverage_distinguishes_zero_from_absence_and_pit_versions():
    row = sample_fact(period_end=date(2020, 1, 2))
    amended = row.model_copy(
        update={"value": Decimal(44), "accession_number": "amended", "form": "10-Q/A"}
    )
    cutoff = datetime(2020, 1, 2, 21, tzinfo=UTC)
    filings = {ACCESSION: (row.filed, row.form, cutoff), "amended": (row.filed, "10-Q/A", None)}
    record = next(q.coverage([row], filings, {}, "0930"))
    assert record["numeric_zero_facts"] == 1
    assert "revenue" not in record["missing_metrics"]
    assert "net_income" in record["missing_metrics"]
    issues = q.pit_issues([row, amended], filings, set(), cutoff)
    assert issues["COMPARATIVE_VALUES_REQUIRE_ORIGINAL_CONTEXT"] == [ACCESSION, "amended"]
    assert issues["AMENDMENT_REQUIRES_VERSION_CHAIN"] == issues["MISSING_ACCEPTANCE"] == ["amended"]


def test_metadata_only_and_legacy_flag_validation(monkeypatch, capsys):
    monkeypatch.setattr(q, "preflight", lambda: ({}, {}, {}, {}))
    monkeypatch.setattr(q, "run_phases", lambda *args: pytest.fail("database phases reached"))
    assert q.main([]) == 0
    assert json.loads(capsys.readouterr().out)["sqlite_opened"] is False
    with pytest.raises(SystemExit) as exc:
        q.main(["--phase", "legacy", "--output", "unused"])
    assert exc.value.code == 2
