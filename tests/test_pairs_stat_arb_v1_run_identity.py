"""Focused offline regressions for the run-global, outcome-independent gate."""

import json
from datetime import date
from sqlite3 import OperationalError

import pytest
from test_pairs_stat_arb_v1 import START, forbidden, seed, synthetic_epoch
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types
from test_pairs_stat_arb_v1_epochs import add_bar
from test_pairs_stat_arb_v1_identity_quarantine import add_company
from test_pairs_stat_arb_v1_security import config as config

from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_identity as policy
from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import MISMATCH
from trading_system.backtest.pairs_stat_arb_v1_research import (
    run_pairs_stat_arb_v1,
    summary_metadata,
)
from trading_system.data.database import Database
from trading_system.data.security_identity import ACTIVE, OUTSIDE, UNKNOWN, epoch_status
from trading_system.data.security_lifecycle import normalize_name_changes


def add_action(db, symbol):
    records = normalize_name_changes(
        {
            "name_changes": [
                {"old_symbol": symbol, "new_symbol": "SUCCESSOR", "process_date": "2022-01-03"}
            ]
        },
        [symbol],
        date(2020, 1, 1),
        START,
        observed_at="2026-10-14T00:00:00+00:00",
    )
    db.ingest_security_corporate_actions(records)


def diagnostic(prepared, symbol):
    return next(
        row
        for row in prepared.manifest["discovery_counts"]["identity_qualification_diagnostics"]
        if row["symbol"] == symbol
    )


def add_bad(db, symbol):
    add_company(db, symbol, 3)
    add_action(db, symbol)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT ?,timeframe,timestamp,open,high,low,close,900000000,"
            "trade_count,vwap FROM bars WHERE symbol='A'",
            (symbol,),
        )


@pytest.mark.parametrize("symbol", ["BAD_IDENTITY", "XYZ"])
def test_risky_symbol_is_run_quarantined_before_top_n_and_never_reaches_pairs(
    tmp_path, config, monkeypatch, symbol
):
    db, *_ = seed(tmp_path, end=START)
    add_bad(db, symbol)
    monkeypatch.setattr(engine, "observe", forbidden)
    monkeypatch.setattr(engine, "simulate_trade", forbidden)
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    row = diagnostic(prepared, symbol)
    assert row["identity_status"] == policy.QUARANTINED
    assert row["reason"] == policy.UNRESOLVED and row["run_excluded"]
    assert not row["validation_blocking"]
    assert {r["symbol"] for r in prepared.manifest["universe"]} == {"A", "B"}
    assert {r["symbol"] for r in prepared.manifest["selected"][str(START)]["35"]} == {"A", "B"}
    assert prepared.manifest["candidates"]
    assert all(
        {r["symbol_a"], r["symbol_b"]} == {"A", "B"} for r in prepared.manifest["candidates"]
    )
    assert not any(s == symbol for s, _ in prepared.bars)
    assert not any(r["symbol"] == symbol for r in prepared.requirements)
    counts = prepared.manifest["discovery_counts"]
    assert counts["internal_selection_gaps"] == counts["unresolved_selection_membership"] == []
    assert summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


def test_verified_active_wins_over_unresolved_lifecycle_and_unverified_epoch(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_action(db, "A")
    epoch = synthetic_epoch("A", "0000000001").record()
    epoch.update(verified=False, source="UNVERIFIED_HINT", evidence_reference="hint")
    with db.connect() as c:
        columns = tuple(epoch)
        c.execute(
            f"INSERT INTO symbol_identity_epochs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(epoch.values()),
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert diagnostic(prepared, "A")["identity_status"] == ACTIVE
    assert prepared.manifest["candidates"]


def test_verified_outside_is_nonmember_without_quarantine_or_membership_gap(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_action(db, "A")
    with db.connect() as c:
        c.execute("UPDATE symbol_identity_epochs SET effective_start='2026-01-01' WHERE symbol='A'")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert diagnostic(prepared, "A")["identity_status"] == OUTSIDE
    assert prepared.manifest["discovery_counts"]["identity_qualification"]["quarantined"] == 0
    assert prepared.manifest["discovery_counts"]["selection_membership_resolved"]
    assert not any(r["symbol"] == "A" for r in prepared.manifest["universe"])


def test_ordinary_security_without_any_epoch_remains_usable_without_claiming_verification(
    tmp_path, config
):
    db, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute("DELETE FROM symbol_identity_epochs")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert all(r["identity_status"] == policy.CURRENT for r in prepared.manifest["universe"])
    assert all(epoch_status(r, START) == UNKNOWN for r in prepared.manifest["universe"])
    assert all(r["identity_epoch"] is None for r in prepared.manifest["universe"])
    assert prepared.manifest["candidates"][0]["calibration_status"] == "ELIGIBLE"
    assert summary_metadata(prepared, preflight=True)["ready_for_local_validation"]
    # Replay uses the same explicit current-reference qualification for outcomes.
    evaluations, signals = engine.simulate_prepared_pairs(prepared)
    assert evaluations and signals


# Local metadata/timestamp samples recorded in the existing Daily-gap diagnosis.
# These are development fixtures, never imported by production qualification.
@pytest.mark.parametrize(
    "symbol,cik,older,newer,registration",
    [
        ("CAI", 2019410, "2021-11-22", "2025-06-18", "2025-06-13"),
        ("FLY", 1860160, "2021-08-02", "2025-08-07", "2025-08-06"),
        ("JATT", 2112446, "2023-03-20", "2026-04-17", "2026-04-16"),
        ("ALPS", 2025774, "2023-07-20", "2025-10-31", "2025-10-27"),
        ("BLUW", 2050501, "2021-09-09", "2025-07-31", "2025-06-09"),
        ("DSAC", 2082149, "2022-07-29", "2026-02-05", "2025-12-08"),
        ("BPAC", 2064177, "2023-06-05", "2026-01-06", "2025-11-10"),
    ],
)
def test_known_local_history_samples_and_bpac_use_generic_registration_warning(
    tmp_path, config, symbol, cik, older, newer, registration
):
    db = Database(tmp_path / "local_sample.sqlite3")
    db.initialize()
    add_company(db, symbol, cik)
    for day in (older, newer):
        add_bar(db, symbol, date.fromisoformat(day))
    db.cache_sec_payload(
        f"{cik:010d}",
        "submissions",
        {
            "filings": {
                "recent": {
                    "form": ["8-A12B"],
                    "filingDate": [registration],
                    "accessionNumber": ["local-sample"],
                }
            }
        },
    )
    prepared = prepare_pairs_data(db, config, START, date(2026, 8, 12), preflight=True)
    row = diagnostic(prepared, symbol)
    assert row["identity_status"] == policy.QUARANTINED
    assert row["evidence_source"] == ["CACHED_SEC_SUBMISSIONS", "LOCAL_BAR_TIMESTAMPS"]
    assert (
        row["risk_signals"][0]["trigger"]
        == "DISCONTINUOUS_HISTORY_WITH_CURRENT_ISSUER_REGISTRATION"
    )
    assert prepared.manifest["universe"] == []
    assert prepared.manifest["discovery_counts"]["unresolved_selection_membership"] == []


def test_unverified_epoch_alone_is_concrete_risk_but_never_an_identity_proof(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute("UPDATE symbol_identity_epochs SET verified=0 WHERE symbol='A'")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert diagnostic(prepared, "A")["identity_status"] == policy.QUARANTINED


def test_ordinary_internal_data_gap_still_blocks_without_becoming_quarantine(tmp_path, config):
    db, sessions, *_ = seed(tmp_path, end=START)
    missing = sessions[sessions.index(START) - 7]
    with db.connect() as c:
        c.execute("DELETE FROM symbol_identity_epochs WHERE symbol='A'")
        c.execute("DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing}%",))
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert diagnostic(prepared, "A")["identity_status"] == policy.CURRENT
    assert any(r["symbol"] == "A" and r["status"] == "LOCAL_MISSING" for r in prepared.requirements)
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


@pytest.mark.parametrize("corrupt", ["epoch", "lifecycle"])
def test_corrupt_identity_evidence_blocks_instead_of_becoming_quarantine(tmp_path, config, corrupt):
    db, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        if corrupt == "epoch":
            c.execute(
                "UPDATE symbol_identity_epochs SET evidence_type='FIRST_BAR' WHERE symbol='A'"
            )
    if corrupt == "lifecycle":
        add_action(db, "A")
        with db.connect() as c:
            c.execute("UPDATE security_corporate_actions SET observed_at='invalid'")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    summary = summary_metadata(prepared, preflight=True)
    assert not summary["ready_for_local_validation"]
    assert not summary["identity_qualification_complete"]
    assert summary["identity_qualification"]["quarantined"] == 0


def test_true_verified_evidence_conflict_quarantines_entire_run(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute(
            "INSERT INTO symbol_identity_epochs "
            "SELECT symbol,stable_security_identity,'0000099999',"
            "effective_start,effective_end,'OTHER',evidence_type,verified,observed_at,'conflict' "
            "FROM symbol_identity_epochs WHERE symbol='A'"
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert diagnostic(prepared, "A")["identity_status"] == policy.QUARANTINED
    assert diagnostic(prepared, "A")["reason"] == policy.CONFLICT
    assert not any(r["symbol"] == "A" for r in prepared.manifest["universe"])


def test_quarantine_evidence_change_invalidates_manifest_even_for_excluded_symbol(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_bad(db, "XYZ")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(prepared.manifest), encoding="utf-8")
    with db.connect() as c:
        c.execute("UPDATE security_corporate_actions SET raw_fingerprint='changed'")
    updated = prepare_pairs_data(db, config, START, START, preflight=True)
    assert (
        updated.manifest["run_identity_qualification_fingerprint"]
        != prepared.manifest["run_identity_qualification_fingerprint"]
    )
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, START, candidate_manifest=path)


def test_prices_and_outcome_tail_do_not_influence_identity_qualification(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_bad(db, "XYZ")
    first = prepare_pairs_data(db, config, START, START, preflight=True)
    with db.connect() as c:
        c.execute("UPDATE bars SET close=close*9,volume=0 WHERE symbol='XYZ'")
        c.execute("UPDATE bars SET close=close*9,volume=0 WHERE timestamp>'2024-01-02'")
    second = prepare_pairs_data(db, config, START, START, preflight=True)
    assert (
        first.manifest["run_identity_qualification_fingerprint"]
        == second.manifest["run_identity_qualification_fingerprint"]
    )


def test_failed_qualification_is_blocking_and_never_quarantines_all(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path, end=START)

    def failed(*args, **kwargs):
        raise OperationalError("synthetic read failure")

    monkeypatch.setattr(policy, "_history_warnings", failed)
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, START, tmp_path, stem="failed", preflight=True
    )
    assert not summary["ready_for_local_validation"]
    assert not summary["identity_qualification_complete"]
    assert summary["identity_qualification"] == {
        "eligible": 0,
        "quarantined": 0,
        "verified_outside_run": 0,
        "unresolved_blocking": 2,
    }
    assert summary["pair_session_candidates"] == 0
    assert all(
        row["identity_status"] == policy.FAILED
        for row in summary["identity_qualification_diagnostics"]
    )
    assert paths["summary"].exists()
    with pytest.raises(ValueError, match=policy.FAILED):
        prepare_pairs_data(db, config, START, START)
