"""Bounded orchestration and real local adapter integration; no network/outcomes."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_pairs_daily_qualification import missing_fixture
from test_pairs_stat_arb_v1 import START, forbidden, seed
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types
from test_pairs_stat_arb_v1_identity_quarantine import add_company
from test_pairs_stat_arb_v1_run_identity import add_bad
from test_pairs_stat_arb_v1_security import config as config

from trading_system.backtest import pairs_stat_arb_v1 as engine
from trading_system.backtest import pairs_stat_arb_v1_daily_fixpoint as loop
from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest import pairs_stat_arb_v1_research as research
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.pairs_stat_arb_v1_daily_qualification import STORE, scope
from trading_system.backtest.pairs_stat_arb_v1_daily_qualification_audit import provider_failure
from trading_system.data.security_reference import digest


def stage(label="A", *, missing="XYZ", quarantined=(), guard=False):
    unresolved = missing is not None
    rows = [
        {
            "symbol": s,
            "run_excluded": True,
            "validation_blocking": False,
            "provider_probe_status": "COMPLETED_SUCCESSFULLY",
        }
        for s in quarantined
    ]
    rows.append(
        {
            "symbol": missing or "GOOD",
            "run_excluded": False,
            "validation_blocking": unresolved,
            "provider_probe_status": "REQUIRED_HISTORY_NOT_ESTABLISHED"
            if unresolved
            else "NOT_REQUIRED",
        }
    )
    return SimpleNamespace(
        sessions=[START],
        metadata={},
        requirements=[
            {
                "symbol": missing,
                "session": str(START),
                "session_end": str(START),
                "roles": "PAIR_CALIBRATION_AND_OBSERVATION",
                "status": "LOCAL_MISSING",
            }
        ]
        if unresolved
        else [],
        manifest={
            "universe": [{"symbol": label}],
            "candidates": [{"symbol_a": label}],
            "market_data_feed": "IEX",
            "market_data_adjustment": "all",
            "daily_qualification_policy": "V1",
            "source_fingerprint": label,
            "run_identity_qualification_fingerprint": "IDENTITY_FIXED",
            "run_daily_qualification_fingerprint": digest([label, quarantined]),
            "discovery_counts": {
                "daily_data_gate_complete": not guard,
                "identity_qualification_diagnostics": [
                    {"symbol": "IDENTITY_RISK", "run_excluded": True}
                ],
                "daily_data_qualification_diagnostics": rows,
                "daily_data_qualification": {
                    "eligible": 10,
                    "quarantined": len(quarantined),
                    "policy_result": "REVIEW_REQUIRED" if guard else "ACCEPTED",
                },
            },
        },
    )


def sequence(stages):
    calls = []

    def discover():
        item = stages[min(len(calls), len(stages) - 1)]
        calls.append(item)
        return item

    return discover, calls


def audited(**changes):
    result = {
        "audited_requirements": 1,
        "repairs": 0,
        "new_quarantines": 0,
        "qualification_failures": 0,
        "policy_result": "ACCEPTED",
        **changes,
    }
    return {**result, "fingerprint": digest(result)}


def test_complete_inputs_reach_fixpoint_once_without_provider():
    discover, calls = sequence([stage(missing=None)])
    _, result = loop.iterate(discover, forbidden)
    assert result["fixpoint_reached"] and result["daily_qualification_rounds"] == 1
    assert len(calls) == 1 and result["fixpoint_reason"] == loop.SUCCESS


def test_state_fingerprint_ignores_correlation_and_trade_outcome_fields():
    prepared = stage()
    changed = deepcopy(prepared)
    changed.manifest["candidates"][0].update(correlation=-0.9, net_pair_return=-999)
    assert digest(loop.state_inputs(prepared)) == digest(loop.state_inputs(changed))


def test_new_requirement_then_quarantine_requires_full_new_discovery():
    discover, calls = sequence([stage(), stage("B", missing=None, quarantined=("XYZ",))])
    keys = []
    _, result = loop.iterate(
        discover, lambda prepared, new: keys.append(new) or audited(new_quarantines=1)
    )
    assert result["fixpoint_reached"] and len(calls) == 2
    assert {key[0] for key in keys[0]} == {"XYZ"}
    assert [r["new_requirements"] for r in result["rounds"]] == [1, 0]


def test_cascading_replacements_each_take_the_same_generic_path():
    discover, calls = sequence(
        [
            stage(missing="FIRST"),
            stage("B", missing="SECOND", quarantined=("FIRST",)),
            stage("C", missing=None, quarantined=("FIRST", "SECOND")),
        ]
    )
    audited_symbols = []

    def audit(prepared, new):
        audited_symbols.extend(key[0] for key in new)
        return audited(new_quarantines=1)

    _, result = loop.iterate(discover, audit)
    assert result["fixpoint_reached"] and len(calls) == 3
    assert audited_symbols == ["FIRST", "SECOND"]
    assert all(
        r["state_inputs"]["identity_quarantined_symbols"] == ["IDENTITY_RISK"]
        for r in result["rounds"]
    )


def test_provider_failure_stops_without_quarantine():
    discover, calls = sequence([stage()])
    _, result = loop.iterate(discover, lambda *_: audited(qualification_failures=1))
    assert not result["fixpoint_reached"] and result["fixpoint_reason"] == loop.FAILED
    assert result["qualification_failures"] == 1 and len(calls) == 1
    assert result["rounds"][0]["new_quarantines"] == 0


def test_no_progress_detects_repeated_unresolved_state_without_reaudit():
    discover, calls = sequence([stage()])
    audits = []
    _, result = loop.iterate(discover, lambda *_: audits.append(True) or audited())
    assert result["fixpoint_reason"] == loop.NO_PROGRESS
    assert len(calls) == 2 and len(audits) == 1


def test_nonadjacent_repeat_is_a_cycle():
    first = stage()
    discover, calls = sequence([first, stage("B", missing="OTHER"), first])
    _, result = loop.iterate(discover, lambda *_: audited())
    assert result["fixpoint_reason"] == loop.CYCLE and len(calls) == 3


def test_universe_cycle_cannot_hide_behind_changed_evidence_fingerprints():
    first = stage()
    returned = deepcopy(first)
    returned.manifest["source_fingerprint"] = "CHANGED_DAILY_SOURCE"
    discover, _ = sequence([first, stage("B", missing="OTHER"), returned])
    _, result = loop.iterate(discover, lambda *_: audited())
    assert result["fixpoint_reason"] == loop.CYCLE


def test_max_rounds_is_finite_and_last_round_cannot_start_more_repairs():
    discover, calls = sequence(
        [stage(str(i), missing=f"X{i}") for i in range(loop.MAX_DAILY_QUALIFICATION_ROUNDS)]
    )
    audits = []
    _, result = loop.iterate(discover, lambda *_: audits.append(True) or audited())
    assert result["fixpoint_reason"] == loop.MAX_ROUNDS
    assert len(calls) == loop.MAX_DAILY_QUALIFICATION_ROUNDS
    assert len(audits) == loop.MAX_DAILY_QUALIFICATION_ROUNDS - 1


@pytest.mark.parametrize("during_audit", [False, True])
def test_safety_guard_stops_immediately(during_audit):
    discover, calls = sequence([stage(guard=not during_audit)])
    _, result = loop.iterate(discover, lambda *_: audited(policy_result="REVIEW_REQUIRED"))
    assert result["fixpoint_reason"] == loop.REVIEW and not result["fixpoint_reached"]
    assert len(calls) == 1


def integration(tmp_path, config, monkeypatch, *, bars=False):
    db, _, _, saved = missing_fixture(tmp_path, config, "XYZ", dilute=True)
    add_bad(db, "IDENTITY_BAD")
    # The issuer table keys CIK; keep these two synthetic issuers distinct.
    add_company(db, "XYZ", 400)
    monkeypatch.setattr(engine, "observe", forbidden)
    monkeypatch.setattr(engine, "simulate_trade", forbidden)
    monkeypatch.setattr(research, "simulate_prepared_pairs", forbidden)
    probes, syncs, calibration_calls = [], [], []
    calibrate = data.calibration

    def calibration(*args, **kwargs):
        calibration_calls.append(True)
        return calibrate(*args, **kwargs)

    monkeypatch.setattr(data, "calibration", calibration)

    def probe(symbols, first, last):
        assert calibration_calls == []  # Iteration never computes correlation.
        probes.append((symbols, first, last))
        return (
            [
                SimpleNamespace(
                    symbol="XYZ", timestamp=datetime.combine(START, datetime.min.time(), UTC)
                )
            ]
            if bars
            else []
        )

    def sync(symbols, first, last, **kwargs):
        assert symbols == ["XYZ"] and not kwargs["include_benchmark"]
        assert not kwargs["incremental"]
        syncs.append((symbols, first, last))
        with db.connect() as c:
            c.execute(f"INSERT INTO bars VALUES ({','.join('?' for _ in saved)})", saved)
        db.set_sync_value(
            "daily_history_coverage",
            "XYZ",
            {
                "start": str(first),
                "end_exclusive": str(last + timedelta(days=1)),
                "feed": config.universe.market_data_feed.lower(),
                "adjustment": config.universe.market_data_adjustment,
            },
        )
        return {"bars_received": 1, "bars_inserted": 1, "errors": 0}

    synchronizer = SimpleNamespace(
        alpaca=SimpleNamespace(daily_bars=probe, last_bar_diagnostics={}), sync_daily_history=sync
    )
    return db, synchronizer, probes, syncs, calibration_calls


@pytest.mark.parametrize("bars", [False, True])
def test_real_auditor_quarantines_or_repairs_then_rediscovery_finds_fixpoint(
    tmp_path, config, monkeypatch, bars
):
    db, synchronizer, probes, syncs, calibration_calls = integration(
        tmp_path, config, monkeypatch, bars=bars
    )
    summary, paths = research.run_pairs_stat_arb_v1(
        db,
        config,
        START,
        START,
        tmp_path / "reports",
        stem="run",
        preflight=True,
        daily_auditor_factory=lambda: synchronizer,
    )
    assert summary["ready_for_local_validation"] and summary["fixpoint_reached"]
    assert summary["daily_qualification_rounds"] == 2
    assert len(probes) == 1 and len(syncs) == int(bars) and calibration_calls
    assert summary["identity_qualification"]["quarantined"] == 1
    assert summary["identity_qualification"]["unresolved_blocking"] == 0
    assert summary["daily_data_qualification"]["quarantined"] == int(not bars)
    assert (
        summary["daily_qualification_failures"] == summary["new_unreviewed_daily_requirements"] == 0
    )
    assert summary["network_used"]
    assert list((tmp_path / "reports").glob("run_round_1_repair_plan.json"))
    manifest = json.loads(paths["pair_candidates"].read_text())
    assert manifest["manifest_version"] == 9
    if not bars:
        assert not any(r["symbol"] == "XYZ" for r in manifest["universe"])
        assert not any("XYZ" in {r["symbol_a"], r["symbol_b"]} for r in manifest["candidates"])
    # Existing certificates and local repairs are reused without another client.
    next_summary, _ = research.run_pairs_stat_arb_v1(
        db,
        config,
        START,
        START,
        tmp_path / "reports",
        stem="next",
        preflight=True,
        daily_auditor_factory=forbidden,
    )
    assert next_summary["fixpoint_reached"] and next_summary["daily_qualification_rounds"] == 1


def test_qualification_only_discovery_never_calls_calibration_or_outcomes(
    tmp_path, config, monkeypatch
):
    db, *_ = seed(tmp_path, end=START)
    monkeypatch.setattr(data, "calibration", forbidden)
    monkeypatch.setattr(engine, "simulate_trade", forbidden)
    prepared = data.prepare_pairs_data(
        db, config, START, START, preflight=True, qualification_only=True
    )
    assert prepared.manifest["candidates"]
    assert {r["calibration_status"] for r in prepared.manifest["candidates"]} == {
        "QUALIFICATION_PENDING"
    }
    assert prepared.metadata["calibration_deferred"]


def test_real_provider_timeout_stays_failed_without_quarantine(tmp_path, config, monkeypatch):
    db, synchronizer, _, syncs, calibration_calls = integration(tmp_path, config, monkeypatch)

    def timeout(*args, **kwargs):
        raise TimeoutError()

    synchronizer.alpaca.daily_bars = timeout
    summary, _ = research.run_pairs_stat_arb_v1(
        db,
        config,
        START,
        START,
        tmp_path / "reports",
        stem="timeout",
        preflight=True,
        daily_auditor_factory=lambda: synchronizer,
    )
    assert not summary["ready_for_local_validation"] and not summary["fixpoint_reached"]
    assert summary["fixpoint_reason"] == loop.FAILED
    assert summary["daily_qualification_failures"] == 1
    assert summary["daily_data_qualification"]["quarantined"] == 0
    assert not syncs and not calibration_calls


@pytest.mark.parametrize("change", ["feed", "policy", "round", "evidence"])
def test_frozen_manifest_invalidates_feed_policy_round_state_or_evidence(
    tmp_path, config, monkeypatch, change
):
    db, sync, *_ = integration(tmp_path, config, monkeypatch)
    _, paths = research.run_pairs_stat_arb_v1(
        db,
        config,
        START,
        START,
        tmp_path / "reports",
        stem="bound",
        preflight=True,
        daily_auditor_factory=lambda: sync,
    )
    payload = json.loads(paths["pair_candidates"].read_text())
    if change == "feed":
        payload["market_data_feed"] = "SIP"
    elif change == "policy":
        payload["daily_fixpoint_policy"] = "OTHER_POLICY"
    elif change == "round":
        payload["daily_qualification_iteration"]["rounds"][0]["state_fingerprint"] = "OTHER_STATE"
        result = payload["daily_qualification_iteration"]
        result["fingerprint"] = digest({k: v for k, v in result.items() if k != "fingerprint"})
        payload["run_daily_fixpoint_fingerprint"] = result["fingerprint"]
    else:
        key = digest(scope(config, START, START))
        evidence = db.sync_values(STORE)[key]
        evidence["symbols"]["XYZ"]["sessions"][0]["observed_at"] = "2026-10-17T00:00:00+00:00"
        evidence["fingerprint"] = digest({k: v for k, v in evidence.items() if k != "fingerprint"})
        db.set_sync_value(STORE, key, evidence)
    payload["fingerprint"] = fingerprint(payload)
    candidate = tmp_path / "changed.json"
    candidate.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="PAIRS_STAT_ARB_MANIFEST_MISMATCH"):
        data.prepare_pairs_data(db, config, START, START, candidate_manifest=candidate)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError(), "TRANSIENT_ERROR"),
        (PermissionError(), "AUTH_ERROR"),
        (SimpleNamespace(status_code=429), "RATE_LIMIT_ERROR"),
        (TypeError(), "PARSER_ERROR"),
        (ValueError("ALPACA_UNEXPECTED_BAR_RESPONSE"), "INVALID_RESPONSE"),
    ],
)
def test_sanitized_provider_failure_categories(exc, expected):
    assert provider_failure(exc) == expected
