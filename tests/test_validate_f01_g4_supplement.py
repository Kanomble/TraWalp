"""Targeted F01 G4 recovery probes; synthetic inputs only, no real DB access."""
import copy
import importlib.util
import sqlite3
import sys
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "f01_g4_supplement", Path(__file__).resolve().parents[1] / "scripts/validate_f01_g4_supplement.py"
)
supp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supp
SPEC.loader.exec_module(supp)
SESSION = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("Network forbidden"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("Network forbidden"))


@pytest.fixture
def full_peers():
    from trading_system.config import load_strategy_config
    from trading_system.models.fundamentals import CompanyIdentity, FundamentalMetrics
    from trading_system.models.signals import TechnicalSnapshot
    from trading_system.strategy.screener import _PreparedCandidate

    config = load_strategy_config()
    prepared = []
    for i in range(config.peers.min_peer_count):
        prepared.append(_PreparedCandidate(
            company=CompanyIdentity(cik=str(100 + i).zfill(10), symbol=f"S{i}", name="Synthetic", sic="3571"),
            fundamentals=FundamentalMetrics(
                revenue_growth=0.3, eps_growth=0.5, operating_cash_flow_growth=0.5,
                operating_cash_flow_positive=True, operating_margin=0.35, roic=0.35,
                debt_to_ebitda=0.5, market_cap=Decimal("2000000000"), pe=5,
                enterprise_value=Decimal("2100000000"), ev_to_ebitda=5, ev_to_ebit=5, fcf_yield=0.15),
            technical=TechnicalSnapshot(
                market_session=SESSION, price=100, sma20=95, sma50=90, sma200=80,
                sma20_rising=True, rsi14=50, rsi_recovery=True, momentum5=0.1,
                momentum20=-0.1, momentum20_improving=True, momentum63=-0.1, momentum126=0.2,
                volatility=0.3, atr14=2, relative_volume=1.5, drawdown_52w=-0.05),
            average_dollar_volume_20d=20000000, base_exclusions=[], data_warnings=[],
            analysis_date=SESSION, market_history_count=320, pit_fact_count=30,
            estimated_market_cap=2000000000, current_market_observation="OBSERVATION_PRESENT"))
    return prepared, config


def test_full_peer_context_uses_production_rank1(full_peers):
    prepared, config = full_peers
    narrow = supp.score_context(copy.deepcopy(prepared[:1]), [], config, SESSION, forward=False)
    assert narrow["quality_value_comparison"]["records"][0]["peer_group"] is None
    assert narrow["prepared"]["S0"]["data_warnings"] == []
    assert not narrow["f_ranking"]
    full = supp.score_context(copy.deepcopy(prepared), [], config, SESSION, forward=False)
    assert full["f_ranking"][0]["rank"] == 1
    assert full["quality_value_ranking"][0]["rank"] == 1
    assert all(r["peer_group"] == "sic4:3571" for r in full["quality_value_comparison"]["records"])
    supp.compare_context(full, copy.deepcopy(full), "synthetic_full")
    complete = {p.company.symbol: {"status": "complete"} for p in prepared}
    forward = supp.score_context(copy.deepcopy(prepared), [], config, SESSION, forward=True, history=complete)
    assert forward["ranking_valid"] and forward["f_ranking"][0]["rank"] == 1


def test_ranking_drift_is_detected(full_peers):
    prepared, config = full_peers
    left = supp.score_context(copy.deepcopy(prepared), [], config, SESSION, forward=False)
    changed = copy.deepcopy(left)
    changed["f_ranking"][0]["score"] += 0.1
    with pytest.raises(supp.g3.Blocked, match="f_ranking"):
        supp.compare_context(left, changed, "synthetic")


def test_unresolved_forward_history_stays_fail_closed(full_peers):
    prepared, config = full_peers
    history = {p.company.symbol: {"status": "incomplete", "missing_sessions": ["2026-10-22"]}
               for p in prepared}
    context = supp.score_context(copy.deepcopy(prepared), [], config, SESSION, forward=True, history=history)
    assert context["qualification"]["unresolved_rank_relevant_count"] == len(prepared)
    assert not context["ranking_valid"]
    assert not context["f_ranking"] and not context["quality_value_ranking"]
    assert all(r["rank"] is None and not r["eligible"] for r in context["f_comparison"]["records"])
    assert context["raw_records_are_diagnostic_when_unqualified"]
    with pytest.raises(supp.g3.Blocked, match="UNPROVEN_REAL"):
        supp.validate_clearances({"phase": "ranking", "clears": ["forward_cik_sip/F: no rank-1 witness in fixed cross-section"],
            "modes": {"forward_cik_sip": {"evidence": context, "parity": True,
                "source_context_fingerprint": supp.digest(context), "work_context_fingerprint": supp.digest(context),
                "universe": {"selection": "ALL_PRODUCTION_TRADABLE_COMPANIES"}}}})


def test_ticker_reuse_alias_and_unknown_are_distinct():
    evidence = supp.synthetic_identity_regression()
    assert evidence["status"] == "PASS" and evidence["real_reuse_credit"] is False
    classified = {r["symbol"]: r for r in evidence["classifications"]}
    assert classified["REUSE"]["classification"] == "VERIFIED_TICKER_REUSE"
    assert classified["ALIAS"]["classification"] == "ALIAS_OR_SHARE_CLASS"
    with pytest.raises(supp.g3.Blocked, match="UNPROVEN_REAL"):
        supp.validate_clearances({"phase": "identity", "census": [], "identity_epochs": [],
            "real_reuse_witness_count": 0, "synthetic_regression": evidence,
            "clears": ["Missing real boundary witness: ticker_reuse"]})


class Monitor:
    connection = None
    lock = threading.Lock()
    completed_probes = 0
    def check(self):
        pass
    def tick(self, *_):
        pass
    def progress(self):
        return 0


def test_seal_and_fingerprint_drift(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    monitor = Monitor()
    monitor.logfile = (attempt / "progress.jsonl").open("x", encoding="utf-8")
    try:
        report = {"phase": "identity", "status": "INSUFFICIENT_EVIDENCE", "gaps": ["real evidence missing"],
                  "binding": {"synthetic_fixture": True}, "references_unchanged": True, "execution_enabled": False}
        directory = supp.publish(attempt, report, monitor)
        assert supp.load_supplement(directory, report["binding"], monitor) == report
        name = directory / "fingerprint.json"
        name.write_text('{"contract":"wrong"}', encoding="utf-8")
        with pytest.raises(supp.g3.Blocked, match="SEAL_DRIFT"):
            supp.load_supplement(directory, report["binding"], monitor)
        # Even a refreshed outer seal cannot conceal an inconsistent inner FP.
        seal = supp.g3.read_json(directory / "seal.json")
        seal[name.name] = supp.g3.sha(name)
        (directory / "seal.json").write_text(__import__("json").dumps(seal), encoding="utf-8")
        with pytest.raises(supp.g3.Blocked, match="FINGERPRINT_DRIFT"):
            supp.load_supplement(directory, report["binding"], monitor)
    finally:
        monitor.logfile.close()


def test_reuse_preserves_successes_and_never_waives_real_evidence(monkeypatch):
    # Metadata-only combination must never touch any SQLite connection.
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("Unexpected DB access"))
    original = {"status": "INSUFFICIENT_EVIDENCE", "matrix": [{"ok": True}] * 3,
                "non_fact_tables": {"companies": {"verified": True}}, "full_stream_proof": "bound G3",
                "gaps": ["Missing real boundary witness: ticker_reuse"], "observed_metrics": ["cash"]}
    ranking = {"phase": "ranking", "status": "PASS", "clears": [], "gaps": [], "modes": {}}
    identity = {"phase": "identity", "status": "INSUFFICIENT_EVIDENCE", "clears": [],
                "gaps": ["No authoritative real ticker-reuse witness"], "census": [], "identity_epochs": []}
    result = supp.combine(original, [ranking, identity])
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["reused_successful_evidence"]["pit_accounting_comparisons"] == 3
    assert not result["reused_successful_evidence"]["reran_g3_streams_or_ledger"]
    assert not result["reused_successful_evidence"]["reran_non_fact_inventory"]
    assert not result["real_evidence_requirement_waived"]
    failed = {"phase": "ranking", "status": "BLOCKED", "clears": [], "gaps": ["Synthetic detected drift"]}
    assert supp.combine(original, [failed, identity])["status"] == "NO_GO"
