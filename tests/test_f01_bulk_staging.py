"""Task 9.4 synthetic bulk staging and restart tests."""

import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_f01_historical_50_bulk_staging as staging  # noqa: E402

ACC = "0000000010-10-000001"
FILED = "2010-04-01"
PERIOD = "2010-03-31"
STAMP = datetime(2010, 4, 1, 18, tzinfo=UTC)


def fact(*, accession=ACC, value=10, period=PERIOD, filed=FILED, **fields):
    return {
        "accn": accession,
        "val": value,
        "end": period,
        "filed": filed,
        "form": "10-Q",
        **fields,
    }


def payload(rows):
    return {"facts": {"us-gaap": {"Assets": {"units": {"USD": rows}}}}}


def test_raw_retention_quality_and_pit_are_independent():
    other = "0000000010-10-000002"
    rows = [
        fact(),
        fact(value="10.00"),
        fact(accession=other),
        fact(period="2009-12-31"),
        fact(start="2010-04-02"),
        fact(accession="bad"),
    ]
    data = payload(rows)
    data["facts"]["custom"] = {"Example": {"units": {"USD": [fact(value="0.12345678912345678")]}}}
    obs, candidates, coverage, _, counts = staging.rules.process(
        data,
        {ACC: (date.fromisoformat(FILED), "10-Q", STAMP)},
        set(),
    )
    assert len(obs) == 6
    assert counts["before_start_excluded"] == 1
    assert json.loads(obs[0][4])["val"] == "0.12345678912345678"  # custom namespace survives
    assert rows[0]["accn"] == candidates[0][5] and candidates[0][-1] == 2
    assert candidates[1][-2] is None
    assert coverage[2010, "total_assets"]["with_acceptance"] == 1
    assert counts["INVALID_PERIOD_OR_FILED"] == counts["INVALID_ACCESSION"] == 1


def test_conflicts_and_contexts_are_not_filled():
    p = payload([fact(), fact(value=11), fact(filed="2010-05-01")])
    p["facts"]["us-gaap"]["LongTermDebt"] = {"units": {"USD": [fact()]}}
    obs, candidates, coverage, filings, counts = staging.rules.process(
        p,
        {ACC: (date.fromisoformat(FILED), "10-Q", STAMP)},
        set(),
    )
    assert not candidates
    assert coverage[2010, "total_assets"]["ambiguous"] == 2
    assert filings[ACC]["problems"] == {"SUBMISSIONS_FORM_OR_FILED_MISMATCH", "AMBIGUOUS_CANDIDATE"}
    assert counts["UNMAPPED_OR_SCOPE_UNVERIFIED"] == 1  # debt component is not total debt
    assert all(row[-1] is None for row in obs)


@pytest.fixture
def synthetic_batch(tmp_path, monkeypatch):
    expected, cohort, submissions = {}, {}, {}
    cf = tmp_path / "companyfacts.zip"
    sec = tmp_path / "submissions.zip"
    sec.touch()
    with ZipFile(cf, "w") as z:
        for i in range(1, 51):
            cik, symbol = f"{i:010}", f"SYM{i}"
            accession = f"{cik}-10-000001"
            name = f"CIK{cik}.json"
            raw = json.dumps(payload([fact(accession=accession)]) | {"cik": cik}).encode()
            z.writestr(name, raw)
            columns = {
                "accessionNumber": [accession],
                "filingDate": [FILED],
                "form": ["10-Q"],
                "acceptanceDateTime": [STAMP.isoformat()],
            }
            submissions[cik] = ({"filings": {"recent": columns}}, "meta", [])
            cohort[symbol] = {"symbol_label": symbol, "planning_cik": cik}
            expected[symbol] = {
                "companyfacts_member_sha256": staging.d6.sha(raw),
                "submissions_member_sha256": "meta",
                "submissions_history_member_sha256": {},
            }
    monkeypatch.setattr(staging.pilot, "submissions_members", lambda *_: submissions.copy())
    monkeypatch.setattr(staging, "cache_roots", lambda: [])
    monkeypatch.setattr(staging, "TARGET", tmp_path / "f01-companyfacts-staging-test")
    plan = {
        "snapshot_retrieval_ended_at_utc": STAMP.isoformat(),
        "snapshot_retrieval_ended_utc": STAMP.isoformat(),
        "classification": staging.CLASSIFICATION,
    }
    return (
        cohort,
        expected,
        {"companyfacts": cf, "submissions": sec},
        {"companyfacts": set(), "submissions": set()},
        plan,
    )


def test_full_cohort_publication_and_resume(synthetic_batch, monkeypatch, tmp_path):
    target = staging.TARGET
    fingerprint = staging.d6.sha(staging.d6.json_bytes(synthetic_batch[-1]))
    actual_scan = staging.scan

    def interrupt(*, inputs_record, consume):
        def interrupted_consumer(*args):
            consume(*args)
            raise RuntimeError("simulated interruption")

        return actual_scan(inputs_record=inputs_record, consume=interrupted_consumer)

    monkeypatch.setattr(staging, "scan", interrupt)
    with pytest.raises(RuntimeError, match="simulated"):
        staging.execute_import(synthetic_batch, expected=fingerprint, resume=False, target=target)
    assert not target.exists()
    staging_folder = tmp_path / f".{target.name}.incomplete"
    with closing(sqlite3.connect(staging_folder / staging.DB)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM issuer").fetchone() == (1,)
    monkeypatch.setattr(staging, "scan", actual_scan)
    with pytest.raises(ValueError, match="EXPLICIT_RESUME"):
        staging.execute_import(synthetic_batch, expected=fingerprint, resume=False, target=target)
    final = staging.execute_import(
        synthetic_batch, expected=fingerprint, resume=True, target=target
    )
    assert final["status"] == "STAGING_PUBLISHED"
    manifest = json.loads((target / "preflight.json").read_bytes())
    assert manifest["coverage_rows"] == 50 * len(staging.rules.METRICS)
    assert manifest["totals"]["acceptance_timestamp_bound"] == 50
    assert manifest["acquisition_groups"] == 3
    assert final["issuers"] == 50
    with closing(
        sqlite3.connect((target / staging.DB).as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as c:
        assert c.execute("SELECT COUNT(*) FROM retrospective_fundamentals").fetchone() == (50,)
        assert c.execute(
            "SELECT DISTINCT original_pit_status FROM retrospective_fundamentals"
        ).fetchone() == ("UNVERIFIED_HISTORICAL_ORIGINAL",)
    with pytest.raises(ValueError, match="ALREADY_EXISTS"):
        staging.execute_import(synthetic_batch, expected=fingerprint, resume=True, target=target)


def test_separate_authorization_and_exact_target(synthetic_batch, tmp_path, capsys):
    assert staging.main(["import"]) == 3
    assert "SEPARATE_STAGING_IMPORT_AUTHORIZATION_REQUIRED" in capsys.readouterr().err
    with pytest.raises(ValueError, match="EXACT_ISOLATED"):
        staging.execute_import(synthetic_batch, expected="invalid", resume=False, target=tmp_path)
    with pytest.raises(ValueError, match="PREFLIGHT_SHA"):
        staging.execute_import(
            synthetic_batch,
            expected="invalid",
            resume=False,
            target=staging.TARGET,
        )
    assert not staging.TARGET.exists()
