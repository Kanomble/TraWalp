"""Bounded synthetic availability policy regressions; no provider transport/outcomes."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_pairs_stat_arb_v1 import END, START, seed
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types
from test_pairs_stat_arb_v1_identity_quarantine import add_company
from test_pairs_stat_arb_v1_run_identity import add_bad
from test_pairs_stat_arb_v1_security import config as config

from trading_system.backtest import pairs_stat_arb_v1_daily_qualification as policy
from trading_system.backtest.pairs_stat_arb_v1_daily_qualification_audit import audit_and_repair
from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import MISMATCH
from trading_system.backtest.pairs_stat_arb_v1_research import summary_metadata
from trading_system.data.security_reference import digest


def missing_fixture(tmp_path, config, symbol="A", *, dilute=False, end=START):
    db, *_ = seed(tmp_path, end=end)
    if symbol != "A":
        add_company(db, symbol, 3)
        with db.connect() as c:
            c.execute(
                "INSERT INTO bars SELECT ?,timeframe,timestamp,open,high,low,close,"
                "900000000,trade_count,vwap FROM bars WHERE symbol='A'",
                (symbol,),
            )
    if dilute:
        for i in range(20):
            add_company(db, f"GOOD{i}", i + 10)
    with db.connect() as c:
        saved = tuple(
            c.execute(
                "SELECT * FROM bars WHERE symbol=? AND substr(timestamp,1,10)=?",
                (symbol, str(START)),
            ).fetchone()
        )
        c.execute(
            "DELETE FROM bars WHERE symbol=? AND substr(timestamp,1,10)=?", (symbol, str(START))
        )
    prepared = prepare_pairs_data(db, config, START, end, preflight=True)
    requirements = {
        "manifest_fingerprint": prepared.manifest["fingerprint"],
        "required_sessions": [r for r in prepared.requirements if r["status"] == "LOCAL_MISSING"],
    }
    assert any(r["symbol"] == symbol for r in requirements["required_sessions"])
    return db, prepared, requirements, saved


def run_audit(
    db,
    prepared,
    requirements,
    config,
    saved,
    *,
    result="EMPTY",
    error=None,
    ingest=True,
    invalid=False,
):
    plans, calls = [], []
    symbol = saved[0]

    def probe(symbols, start, end):
        assert symbols == [symbol]
        if error:
            raise error
        return (
            [
                SimpleNamespace(
                    symbol=symbol, timestamp=datetime.combine(START, datetime.min.time(), UTC)
                )
            ]
            if result == "BAR"
            else []
        )

    def sync(symbol, start, end):
        assert plans and plans[0][0]["action"] == "SYNC_DAILY"
        calls.append((symbol, start, end))
        if ingest:
            with db.connect() as c:
                c.execute(f"INSERT INTO bars VALUES ({','.join('?' for _ in saved)})", saved)
            db.set_sync_value(
                "daily_history_coverage",
                symbol,
                {
                    "start": str(start),
                    "end_exclusive": str(end + timedelta(days=1)),
                    "feed": config.universe.market_data_feed.lower(),
                    "adjustment": config.universe.market_data_adjustment,
                },
            )
        return {"bars_received": 1, "bars_inserted": int(ingest), "errors": 0}

    provider = SimpleNamespace(
        daily_bars=probe, last_bar_diagnostics={"invalid_bars": int(invalid)}
    )
    result = audit_and_repair(
        db, prepared.manifest, requirements, config, provider, sync, save_plan=plans.append
    )
    return result, calls


def qualified(db, prepared, config):
    return policy.qualify_daily(db, prepared.manifest["universe"], config, START, START)


def test_single_gap_positive_provider_bar_repairs_before_qualification(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    result, calls = run_audit(db, prepared, requirements, config, saved, result="BAR")
    assert calls == [("A", START, START)]
    assert result["repair_plan"][0]["classification"] == "TRUE_LOCAL_MISSING"
    universe, rows, summary, complete, _ = qualified(db, prepared, config)
    assert complete and summary["quarantined"] == 0
    assert next(r for r in rows if r["symbol"] == "A")["daily_data_status"] == policy.REPAIRED
    assert {r["symbol"] for r in universe} == {"A", "B"}


@pytest.mark.parametrize("symbol", ["A", "XYZ"])
def test_successful_empty_audit_is_run_quarantined_without_symbol_list(tmp_path, config, symbol):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config, symbol, dilute=True)
    result, calls = run_audit(db, prepared, requirements, config, saved)
    assert not calls and all(r["action"] == "NO_SYNC" for r in result["repair_plan"])
    universe, rows, summary, complete, _ = qualified(db, prepared, config)
    row = next(r for r in rows if r["symbol"] == symbol)
    assert row["daily_data_status"] == policy.QUARANTINED
    assert row["reason"] == policy.UNAVAILABLE and row["run_excluded"]
    assert complete and summary["quarantined"] == 1
    assert not any(r["symbol"] == symbol for r in universe)


@pytest.mark.parametrize(
    "error", [TimeoutError(), PermissionError("authentication"), RuntimeError("rate limit")]
)
def test_technical_failures_block_without_exclusion(tmp_path, config, error):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    _, calls = run_audit(db, prepared, requirements, config, saved, error=error)
    universe, rows, summary, complete, _ = qualified(db, prepared, config)
    assert not calls and not complete and summary["unresolved_blocking"] == 1
    assert summary["quarantined"] == 0 and len(universe) == 2
    assert rows[0]["daily_data_status"] == policy.FAILED
    rerun = prepare_pairs_data(db, config, START, START, preflight=True)
    assert not summary_metadata(rerun, preflight=True)["ready_for_local_validation"]


@pytest.mark.parametrize("ingest,invalid", [(False, False), (True, True)])
def test_failed_ingestion_or_parser_validation_is_not_unavailability(
    tmp_path, config, ingest, invalid
):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    run_audit(
        db, prepared, requirements, config, saved, result="BAR", ingest=ingest, invalid=invalid
    )
    _, _, summary, complete, _ = qualified(db, prepared, config)
    assert not complete and summary["quarantined"] == 0 and summary["unresolved_blocking"] == 1


def test_unaudited_single_gap_and_missing_metadata_never_quarantine(tmp_path, config):
    db, prepared, *_ = missing_fixture(tmp_path, config)
    _, _, summary, _, _ = qualified(db, prepared, config)
    assert summary["quarantined"] == 0
    report = summary_metadata(prepared, preflight=True)
    assert report["daily_data_qualification"]["unresolved_blocking"] == 1
    counts = prepared.manifest["discovery_counts"]
    assert counts["daily_data_qualification"] == report["daily_data_qualification"]
    assert (
        counts["daily_data_qualification_diagnostics"]
        == report["daily_data_qualification_diagnostics"]
    )
    assert not counts["daily_data_qualification_complete"]
    assert counts["daily_data_gate_complete"]
    assert not report["ready_for_local_validation"]


def test_complete_symbol_remains_eligible_without_certificate(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    report = summary_metadata(prepared, preflight=True)
    assert report["ready_for_local_validation"]
    assert report["daily_data_qualification"]["eligible"] == 2
    assert report["daily_data_qualification"]["quarantined"] == 0


def test_run_global_exclusion_precedes_sic_adv_top_n_pairs_and_requirements(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(
        tmp_path, config, "XYZ", dilute=True, end=END
    )
    run_audit(db, prepared, requirements, config, saved)
    rerun = prepare_pairs_data(db, config, START, END, preflight=True)
    assert not any(r["symbol"] == "XYZ" for r in rerun.manifest["universe"])
    assert not any(r["symbol"] == "XYZ" for r in rerun.requirements)
    assert not any(symbol == "XYZ" for symbol, _ in rerun.bars)
    assert not any("XYZ" in {r["symbol_a"], r["symbol_b"]} for r in rerun.manifest["candidates"])
    assert not any(
        r["symbol"] == "XYZ"
        for groups in rerun.manifest["selected"].values()
        for names in groups.values()
        for r in names
    )
    assert rerun.manifest["discovery_counts"]["internal_selection_gaps"] == []
    assert rerun.manifest["candidates"]  # GOOD A/B continue; no outcomes executed.
    assert len(rerun.manifest["selected"]) > 1


def test_identity_quarantine_stays_separate_without_daily_requirements(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_bad(db, "IDENTITY_RISK")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    summary = summary_metadata(prepared, preflight=True)
    assert summary["identity_qualification"]["quarantined"] == 1
    assert summary["daily_data_qualification"]["quarantined"] == 0
    assert not any(r["symbol"] == "IDENTITY_RISK" for r in prepared.requirements)


def test_feed_change_invalidates_qualification_and_manifest(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    run_audit(db, prepared, requirements, config, saved)
    *_, old_fp = qualified(db, prepared, config)
    changed = config.model_copy(deep=True)
    changed.universe.market_data_feed = "sip"
    _, _, summary, _, new_fp = qualified(db, prepared, changed)
    assert old_fp != new_fp and summary["quarantined"] == 0
    path = tmp_path / "old_manifest.json"
    path.write_text(json.dumps(prepared.manifest))
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, changed, START, START, candidate_manifest=path, preflight=True)


def test_mass_quarantine_is_review_required_and_preflight_stops(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    run_audit(db, prepared, requirements, config, saved)
    _, _, summary, complete, _ = qualified(db, prepared, config)
    assert not complete and summary["policy_result"] == "REVIEW_REQUIRED"
    assert summary["quarantined_fraction"] == 0.5
    rerun = prepare_pairs_data(db, config, START, START, preflight=True)
    assert not summary_metadata(rerun, preflight=True)["ready_for_local_validation"]


def test_corrupt_certificate_blocks_instead_of_quarantining(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    run_audit(db, prepared, requirements, config, saved)
    db.set_sync_value(policy.STORE, digest(policy.scope(config, START, START)), "corrupt")
    _, _, summary, complete, _ = qualified(db, prepared, config)
    assert not complete and summary["quarantined"] == 0
    assert summary["unresolved_blocking"] == 2


def test_restored_coverage_clears_exclusion_and_changes_fingerprint(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    run_audit(db, prepared, requirements, config, saved)
    *_, old_fp = qualified(db, prepared, config)
    with db.connect() as c:
        c.execute(f"INSERT INTO bars VALUES ({','.join('?' for _ in saved)})", saved)
    _, rows, summary, complete, new_fp = qualified(db, prepared, config)
    assert complete and summary["quarantined"] == 0 and old_fp != new_fp
    assert rows[0]["daily_data_status"] == policy.REPAIRED


def test_outcome_only_gap_cannot_create_availability_exclusion(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    requirements["required_sessions"][0]["roles"] = "POSSIBLE_OUTCOME_ONLY"
    result, calls = run_audit(db, prepared, requirements, config, saved)
    assert result["provider_observations"] == [] and not calls
    assert qualified(db, prepared, config)[2]["quarantined"] == 0


def test_requirement_role_must_be_proven_before_provider_probe(tmp_path, config):
    db, prepared, requirements, saved = missing_fixture(tmp_path, config)
    requirements["required_sessions"][0]["roles"] = "PAIR_CALIBRATION_AND_OBSERVATION"
    with pytest.raises(ValueError, match="REQUIREMENT_ROLE_NOT_PROVEN"):
        run_audit(db, prepared, requirements, config, saved)
