"""Small offline archives and the actual existing Canonical writer."""

import json
import os
import socket
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_f01_sec_bulk_pilot as pilot  # noqa: E402

from trading_system.data.sec_persistence import CanonicalSecPersistence  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows canonical path pins")
CODE = {"head": "fixture", "fingerprint": "a" * 64}
BOOTSTRAP_CODE = "b" * 64


def test_nonoperating_interest_keeps_same_key_conflict_quarantined():
    accession = "0000001234-24-000001"
    original = {
        "start": "2024-04-01",
        "end": "2024-06-30",
        "val": 100,
        "accn": accession,
        "filed": "2024-08-08",
        "form": "10-Q",
        "frame": "CY2024Q2",
    }
    tag = "InterestExpenseNonoperating"
    payload = {
        "cik": "0000001234",
        "facts": {"us-gaap": {tag: {"units": {"USD": [original, original | {"val": 101}]}}}},
    }
    rules = {"us-gaap": pilot.Admission(True, True, "synthetic evidence")}
    filings = {accession: (date(2024, 8, 8), "10-Q", None)}

    facts, counts, reasons, exceptions = pilot.classify_facts(payload, "X", rules, filings, set())

    assert facts == []
    assert counts["same_key_conflict_groups"] == 1
    assert reasons["UNORDERED_SAME_KEY_CONFLICT"] == 2
    assert exceptions[0]["key"]["tag"] == tag
    assert {row["value"] for row in exceptions[0]["observations"]} == {"100", "101"}


def sql(target, statement, args=()):
    with pilot.Database(target).read_only() as connection:
        return [tuple(row) for row in connection.execute(statement, args)]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    def network(*_, **__):
        pytest.fail("Network must not be used")

    monkeypatch.setattr(socket.socket, "connect", network)
    monkeypatch.setattr(socket.socket, "connect_ex", network)
    monkeypatch.setattr(pilot, "RESEARCH_ROOT", tmp_path)
    monkeypatch.setattr(pilot, "code_binding", lambda: CODE)
    roots = {key: tmp_path / key for key in pilot.bulk.ROOTS}
    for root in roots.values():
        root.mkdir()
    layout_file = tmp_path / "layout.json"
    layout_file.write_bytes(
        pilot.bulk.json_bytes(
            {key: str(path) for key, path in roots.items()}
            | {"budget_bytes": dict.fromkeys(pilot.bulk.BUDGETS, 16 * 1024**2)}
        )
    )
    target = roots["research_db"] / "f01-sec-isolated-research-test" / pilot.bootstrap.DB_NAME
    manifest = pilot.bulk.json_bytes(
        {
            "contract": pilot.bootstrap.BOOTSTRAP_CONTRACT,
            "classification": "RESEARCH_ONLY",
            "research_scope": "synthetic fixture",
        }
    )
    boot = pilot.bootstrap.bootstrap(
        target,
        manifest=manifest,
        manifest_sha256=pilot.sha(manifest),
        expected_code_fingerprint=BOOTSTRAP_CODE,
        verify_code=lambda: BOOTSTRAP_CODE,
        authorized=True,
    )
    scope = pilot.pilot_spec()
    issuers = scope.active
    originals = roots["originals"] / pilot.GENERATION
    originals.mkdir()
    payloads = {"companyfacts": {}, "submissions": {}}
    original, amendment, earlier, conflict = (f"0000001234-20-{n:06d}" for n in range(1, 5))
    for item in issuers:
        cik = item["cik"]
        instant = {
            "val": 10,
            "filed": "2020-02-05",
            "end": "2019-12-31",
            "form": "10-K",
            "accn": original,
            "fy": 2019,
            "fp": "FY",
        }
        duration = instant | {"val": 200, "start": "2019-01-01"}
        cash = [
            instant,
            instant | {"val": 11, "frame": "CY2019Q4I"},
            instant | {"val": 12, "filed": "2020-03-02", "form": "10-K/A", "accn": amendment},
            instant | {"val": 30, "filed": "2017-02-05", "end": "2016-12-31", "accn": earlier},
        ]
        if item is issuers[0]:
            cash += [
                instant,
                instant | {"val": 21, "accn": conflict},
                instant | {"val": 22, "accn": conflict},
            ]
        history_name = f"CIK{cik}-submissions-001.json"
        payloads["companyfacts"][item["companyfacts_member"]] = {
            "cik": cik,
            "facts": {
                "us-gaap": {
                    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": cash}},
                    "Revenues": {"units": {"USD": [duration]}},
                    "SalesRevenueNet": {"units": {"USD": [duration | {"val": 201}]}},
                }
            },
        }
        payloads["submissions"][item["submissions_member"]] = {
            "cik": cik,
            "name": f"Synthetic {cik}",
            "filings": {
                "recent": {
                    "accessionNumber": [original, amendment, conflict],
                    "filingDate": ["2020-02-05", "2020-03-02", "2020-02-05"],
                    "form": ["10-K", "10-K/A", "10-K"],
                    "acceptanceDateTime": ["2020-02-05T20:00:01Z", None, "2020-02-05T20:00:01Z"],
                },
                "files": [{"name": history_name}],
            },
        }
        payloads["submissions"][history_name] = {
            "accessionNumber": [earlier],
            "filingDate": ["2017-02-05"],
            "form": ["10-K"],
        }
    # Archive contains extra issuers/members; selected-name reads must leave them untouched.
    payloads["companyfacts"]["CIK0000000001.json"] = {}
    for exclusion in scope.excluded:
        for cik in exclusion["ciks"]:
            for members in payloads.values():
                members[f"CIK{cik}.json"] = {"excluded_fixture": True}
    source = {
        "contract": pilot.bulk.MANIFEST_CONTRACT,
        "generation": pilot.GENERATION,
        "quality": pilot.bulk.QUALITY,
        "reviewed_census": {"pilot_spec_sha256": pilot.SOURCE_PILOT_SHA},
        "qualified_research_issuers": [],
        "trading_universe_qualified": False,
        "archives": {},
        "pilot_structure": [
            {
                "cik": item["cik"],
                "symbol": item["symbol"],
                "endpoints": {
                    n: {"member": item[f"{n}_member"], "status": "STRUCTURE_CLEAR"}
                    for n in pilot.bulk.URLS
                },
            }
            for item in scope.source_issuers
        ],
    }
    for name, members in payloads.items():
        path = originals / f"{name}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, record in members.items():
                archive.writestr(member, pilot.bulk.json_bytes(record))
        exclusions = (
            [{"member": "CIK0000000001.json", "import_eligible": False}]
            if name == "companyfacts"
            else []
        )
        source["archives"][name] = {
            "local_path": str(path),
            "endpoint": name,
            "bytes": path.stat().st_size,
            "sha256": pilot.file_sha(path),
            "archive_generation": f"{pilot.GENERATION}/{name}",
            "url": pilot.bulk.URLS[name],
            "retrieval_ended_at_utc": "2026-11-21T18:00:00Z",
            "integrity": {
                "status": "VERIFIED",
                "excluded_members": exclusions,
                "excluded_member_count": len(exclusions),
                "structurally_valid_members": 8,
            },
        }
    source_file = originals / pilot.bulk.MANIFEST_NAME

    def bind_source():
        raw = pilot.bulk.json_bytes(source)
        source_file.write_bytes(raw)
        monkeypatch.setattr(pilot, "SOURCE_SHA", pilot.sha(raw))

    bind_source()

    def verify(layout, generation, digest):
        assert (
            pilot.file_sha(layout.paths["originals"] / generation / pilot.bulk.MANIFEST_NAME)
            == digest
        )
        for entry in source["archives"].values():
            assert pilot.file_sha(Path(entry["local_path"])) == entry["sha256"]
        return {"status": pilot.bulk.REVIEWED_STATUS}

    monkeypatch.setattr(pilot.bulk, "verify_generation", verify)
    admission = pilot.admission_template(scope)
    admission["reviewer"] = "synthetic independent reviewer"
    for cik, entry in admission["issuers"].items():
        entry["identity_decision"] = "ADMIT_RESEARCH"
        for taxonomy, decision in [(None, None), *entry["memberships"].items()]:
            evidence_path = roots["import_artifacts"] / f"{cik}-{taxonomy}.json"
            evidence = pilot.bulk.json_bytes(
                {
                    "contract": "F01_SEC_BULK_RESEARCH_EVIDENCE_V1",
                    "scope": "RESEARCH_ONLY",
                    "cik": cik,
                    "symbol": entry["symbol"],
                    "taxonomy": taxonomy,
                    "source_reference": "synthetic, independently pinned fixture evidence",
                }
            )
            evidence_path.write_bytes(evidence)
            reference = {"path": str(evidence_path), "sha256": pilot.sha(evidence)}
            if decision is None:
                entry["identity_evidence"] = reference
            else:
                decision.update(
                    decision="ADMIT_RESEARCH", canonical_anchor=True, evidence=reference
                )
    approval = roots["import_artifacts"] / "pilot-admission.json"

    def args():
        admission["manifest_sha256"] = pilot.SOURCE_SHA
        raw = pilot.bulk.json_bytes(admission)
        approval.write_bytes(raw)
        return [
            "--layout",
            str(layout_file),
            "--target",
            str(target),
            "--admission",
            str(approval),
            "--admission-sha256",
            pilot.sha(raw),
            "--expect-code-fingerprint",
            CODE["fingerprint"],
        ]

    return target, roots, issuers, boot, source, bind_source, admission, args


def test_zip_pilot_import_and_idempotent_replay(fixture, capsys, monkeypatch):
    target, _, issuers, boot, _, _, _, args = fixture
    seen = []
    read_member = pilot.zip_member

    def track(archive, name, endpoint, excluded):
        seen.append(name)
        return read_member(archive, name, endpoint, excluded)

    monkeypatch.setattr(pilot, "zip_member", track)
    launch = args()
    assert pilot.main(launch) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "PREFLIGHT_ONLY" and preview["already_committed"] == 0
    assert preview["expected_receipts"] == 7
    assert preview["binding"]["bootstrap_code_fingerprint"] == BOOTSTRAP_CODE
    assert pilot.file_sha(target) == boot["database_sha256_at_bootstrap"]
    flags = ["--execute", "--authorize-research-import"]
    assert pilot.main([*launch, *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    report_path = Path(result["report"])
    report = json.loads(report_path.read_bytes())
    assert result["status"] == pilot.IMPORTED and report["dataset_version"] == 8
    assert report["expected_receipts"] == report["confirmed_receipts"] == 7
    assert {item["symbol"]: item["status"] for item in report["excluded_issuers"]} == {
        "BAC": "BLOCKED_UNRESOLVED_REPORTING_ENTITY",
        "JSM": "BLOCKED_IDENTITY_CONFLICT",
    }
    assert not report["historical_pit_qualified"] and not report["trading_universe_qualified"]
    assert sorted(report["artifact_sha256"]) == sorted([pilot.STATS, pilot.AUDIT, pilot.QUARANTINE])
    assert set(sql(target, "SELECT symbol,cik FROM companies")) == {
        (item["symbol"], item["cik"]) for item in issuers
    }
    assert sql(target, "SELECT COUNT(*) FROM ingestion_receipt") == [(7,)]
    assert sql(target, "SELECT DISTINCT cik FROM canonical_fact ORDER BY cik") == [
        (item["cik"],) for item in issuers
    ]
    assert set(sql(target, "SELECT DISTINCT symbol FROM source_occurrence")) == {
        (item["symbol"],) for item in issuers
    }
    assert sql(target, "SELECT COUNT(*) FROM sync_state WHERE source='dataset'") == [(0,)]
    first = issuers[0]["cik"]
    cash = sql(target, "SELECT value FROM canonical_fact WHERE cik=? AND metric='cash'", (first,))
    assert sorted(cash) == [("10",), ("11",), ("12",), ("30",)]
    assert sql(
        target,
        "SELECT COUNT(*) FROM canonical_fact WHERE cik=? AND period_start IS NOT NULL",
        (first,),
    ) == [(2,)]
    first_stats = report["issuers"][0]
    assert first_stats["duplicate_occurrence_reduction"] == 1
    assert first_stats["excluded_facts"] == 2
    quarantine = json.loads((report_path.parent / pilot.QUARANTINE).read_bytes())
    conflict = quarantine["issuers"][0]["exceptions"][0]
    assert {item["value"] for item in conflict["observations"]} == {"21", "22"}
    original_report = report_path.read_bytes()
    before = pilot.file_sha(target)
    assert pilot.main([*launch, *flags, "--resume"]) == 0
    assert json.loads(capsys.readouterr().out)["existing_result"] is True
    assert pilot.file_sha(target) == before and report_path.read_bytes() == original_report
    assert not {"CIK0000070858.json", "CIK0001593538.json", "CIK0002152623.json"}.intersection(seen)


def test_receipt_resume_after_commit_before_success_report(fixture, monkeypatch, capsys):
    target, roots, _, _, _, _, _, args = fixture
    launch = [*args(), "--execute", "--authorize-research-import"]
    commit = CanonicalSecPersistence.persist

    def interrupt_after_commit(self, request, *, expected_version):
        commit(self, request, expected_version=expected_version)
        (self.archive.directory / ".sec-1234abcd").write_bytes(b"incomplete scratch")
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(CanonicalSecPersistence, "persist", interrupt_after_commit)
        assert pilot.main(launch) == 130
    assert sql(target, "SELECT COUNT(*) FROM ingestion_receipt") == [(1,)]
    capsys.readouterr()
    intent = next(roots["import_artifacts"].glob("f01-pilot-*/binding.json"))
    valid_intent = intent.read_bytes()
    bound = json.loads(valid_intent)
    intent.write_bytes(pilot.bulk.json_bytes(bound | {"pilot_spec_sha256": pilot.SOURCE_PILOT_SHA}))
    before = pilot.file_sha(target)
    assert pilot.main([*launch, "--resume"]) == 3
    assert "PILOT_RESUME_BINDING_MISMATCH" in capsys.readouterr().err
    assert pilot.file_sha(target) == before
    intent.write_bytes(valid_intent)
    assert pilot.main([*launch, "--resume"]) == 0
    result = json.loads(capsys.readouterr().out)
    report = json.loads(Path(result["report"]).read_bytes())
    assert report["issuers"][0]["replayed"] is True
    assert report["resource_samples"]["retained_incomplete_request_archive_bytes"] == 18
    assert sql(target, "SELECT COUNT(*) FROM ingestion_receipt") == [(7,)]
    assert sql(target, "SELECT current_version FROM fact_contract") == [(8,)]


@pytest.mark.parametrize("fault", ["admission", "membership", "scope", "quarantine", "cik"])
def test_source_scope_and_admission_fail_before_writes(fixture, capsys, fault):
    target, _, issuers, boot, source, bind_source, admission, args = fixture
    item = issuers[0]
    expected = "RESEARCH_IDENTITY"
    if fault == "admission":
        admission["issuers"][item["cik"]]["identity_decision"] = "PENDING"
    elif fault == "membership":
        admission["issuers"][item["cik"]]["memberships"]["us-gaap"].update(
            decision="PENDING", canonical_anchor=False
        )
        expected = "EXPLICIT_RESEARCH_CANONICAL_ANCHOR_REQUIRED"
    elif fault == "scope":
        admission["issuers"]["0000070858"] = {"symbol": "BAC"}
        expected = "ADMISSION_PLAN_BINDING_MISMATCH"
    elif fault == "quarantine":
        entry = source["archives"]["companyfacts"]["integrity"]
        entry["excluded_members"].append(
            {"member": item["companyfacts_member"], "import_eligible": False}
        )
        entry["excluded_member_count"] += 1
        bind_source()
        expected = "QUARANTINED_SOURCE_MEMBER"
    else:
        entry = source["archives"]["companyfacts"]
        path = Path(entry["local_path"])
        with zipfile.ZipFile(path) as archive:
            records = {member: archive.read(member) for member in archive.namelist()}
        name = item["companyfacts_member"]
        records[name] = pilot.bulk.json_bytes(json.loads(records[name]) | {"cik": 1})
        with zipfile.ZipFile(path, "w") as archive:
            for member, raw in records.items():
                archive.writestr(member, raw)
        entry.update(bytes=path.stat().st_size, sha256=pilot.file_sha(path))
        bind_source()
        expected = "COMPANYFACTS_CIK_MISMATCH"
    assert pilot.main([*args(), "--execute", "--authorize-research-import"]) == 3
    assert expected in capsys.readouterr().err
    assert pilot.file_sha(target) == boot["database_sha256_at_bootstrap"]
