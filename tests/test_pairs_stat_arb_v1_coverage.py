"""Synthetic coverage: insufficient selection history is not a remediation backlog."""

import csv
import json
from datetime import date

import pytest
from test_pairs_stat_arb_v1 import END, START, identities, seed, synthetic
from test_pairs_stat_arb_v1 import config as config
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types

from trading_system.backtest.pairs_stat_arb_v1_data import discover_pairs, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_research import (
    run_pairs_stat_arb_v1,
    summary_metadata,
)
from trading_system.backtest.pairs_stat_arb_v1_selection import selection_inputs
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset


def add_short_history(db, first_session):
    db.upsert_assets(
        [
            TradableAsset(
                symbol="NEW",
                name="Current company",
                tradable=True,
                fractionable=False,
                asset_class="US_EQUITY",
            )
        ]
    )
    db.upsert_company(
        CompanyIdentity(symbol="NEW", name="Current company", cik="0000000999", sic="3571")
    )
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT 'NEW',timeframe,timestamp,open,high,low,close,volume,"
            "trade_count,vwap FROM bars WHERE symbol='B' AND timestamp>=?",
            (str(first_session),),
        )


def test_shortened_history_is_visible_nonblocking_and_export_is_bounded(tmp_path, config):
    end = date(2025, 5, 20)
    db, _, days, _, _ = seed(tmp_path, end=end)
    first_session = days[2]
    add_short_history(db, first_session)
    prepared = prepare_pairs_data(db, config, START, end, preflight=True)
    assert "NEW" in {row["symbol"] for row in prepared.manifest["universe"]}
    for day in days[:21]:  # Two absent sessions followed by nineteen insufficient windows.
        assert "NEW" not in {
            r["symbol"] for names in prepared.manifest["selected"][str(day)].values() for r in names
        }
    assert "NEW" in {
        r["symbol"]
        for names in prepared.manifest["selected"][str(days[21])].values()
        for r in names
    }
    diagnostics = [
        r
        for r in prepared.coverage
        if r["symbol"] == "NEW" and r["status"] == "INSUFFICIENT_SELECTION_HISTORY"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["sessions"] == 21
    assert diagnostics[0]["validation_blocking"] is False
    assert not any(
        r["symbol"] == "NEW" and r["status"] == "LOCAL_MISSING_FETCHABLE" for r in prepared.coverage
    )
    assert all(
        r["session"] >= str(first_session) for r in prepared.requirements if r["symbol"] == "NEW"
    )
    assert any(r["status"] == "INSUFFICIENT_CALIBRATION_HISTORY" for r in prepared.coverage)
    assert all(
        r["calibration_status"] == "INCOMPLETE_CALIBRATION"
        for r in prepared.manifest["candidates"]
        if "NEW" in (r["symbol_a"], r["symbol_b"])
    )
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, end, tmp_path, stem="short_history", preflight=True
    )
    assert "CURRENT_UNIVERSE_ONLY" in summary["limitations"]
    assert summary["unavailable_liquidity_symbol_sessions"] == 21
    assert summary["local_missing_data"] == 0
    assert summary["ready_for_local_validation"] is True
    requirements = json.loads(paths["daily_requirements"].read_text())
    assert requirements["required_sessions"] == []
    assert requirements["source_fingerprint_inputs"]["input_scope"] == "SOURCE_FINGERPRINT_INPUT"
    assert requirements["source_fingerprint_inputs"]["validation_blocking"] is False
    assert all(
        r["input_scope"] == "VALIDATION_BLOCKING_REQUIREMENT"
        for r in requirements["required_ranges"]
    )
    with paths["coverage"].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) < 30
    assert len(requirements["required_ranges"]) < 25
    assert (
        sum(r["sessions"] for r in requirements["required_ranges"])
        == summary["blocking_required_symbol_sessions"]
    )


@pytest.mark.parametrize("gap_kind", ["calibration", "outcome"])
def test_genuine_missing_required_candidate_bar_stays_blocking_fetchable(
    tmp_path, config, gap_kind
):
    db, sessions, _, tail, _ = seed(tmp_path)
    # Choose a candidate calibration gap outside ADV20, or an outcome-only tail gap.
    day = sessions[17] if gap_kind == "calibration" else tail[0]
    # For the tail case, freeze a signal on the requested end.
    signal_start = END if gap_kind == "outcome" else START
    with db.connect() as connection:
        connection.execute("DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{day}%",))
    summary, paths = run_pairs_stat_arb_v1(
        db, config, signal_start, END, tmp_path, stem=f"gap_{gap_kind}", preflight=True
    )
    assert not summary["ready_for_local_validation"]
    assert summary["local_missing_data"] == 1
    exported = json.loads(paths["daily_requirements"].read_text())
    assert len(exported["required_sessions"]) == 1
    gap = exported["required_sessions"][0]
    assert gap["symbol"] == "A" and gap["session"] == str(day)
    assert gap["status"] == "LOCAL_MISSING_FETCHABLE"
    assert gap["input_scope"] == "VALIDATION_BLOCKING_REQUIREMENT"
    assert gap["validation_blocking"] and gap["sessions"] == 1


def test_selection_history_diagnostic_does_not_use_future_listing_observations(tmp_path, config):
    db, _, days, _, _ = seed(tmp_path)
    add_short_history(db, days[2])
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    earlier = days[:2]
    original = discover_pairs(
        prepared.manifest["universe"], prepared.bars, prepared.sessions, earlier, config
    )
    without_future = {key: bar for key, bar in prepared.bars.items() if key[1] <= earlier[-1]}
    changed = discover_pairs(
        prepared.manifest["universe"], without_future, prepared.sessions, earlier, config
    )
    assert original == changed
    assert (
        original[2]["selection_history_diagnostics"][0]["status"]
        == "INSUFFICIENT_SELECTION_HISTORY"
    )
    assert summary_metadata(prepared, preflight=True)["ready_for_local_validation"] is True


def test_fingerprint_only_missing_inputs_do_not_expand_required_coverage(tmp_path, config):
    db, *_ = seed(tmp_path)
    baseline = prepare_pairs_data(db, config, START, END, preflight=True)
    for index in range(25):
        symbol = f"EMPTY{index}"
        db.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    tradable=True,
                    fractionable=False,
                    asset_class="US_EQUITY",
                )
            ]
        )
        db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{1000 + index:010d}", sic="3571")
        )
    expanded = prepare_pairs_data(db, config, START, END, preflight=True)
    assert expanded.requirements == baseline.requirements
    assert expanded.manifest["source_fingerprint"] != baseline.manifest["source_fingerprint"]
    assert len(expanded.coverage) == len(baseline.coverage) + 25  # One diagnostic per extra name.
    assert summary_metadata(expanded, preflight=True)["ready_for_local_validation"] is True


def seed_six(tmp_path, *, end=END):
    db, sessions, days, tail, bars = seed(tmp_path, end=end)
    # A's ADV is highest; decreasing C-F liquidity makes F the deterministic sixth.
    for symbol, volume in zip("CDEF", (900_000, 800_000, 700_000, 600_000), strict=True):
        db.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    tradable=True,
                    fractionable=False,
                    asset_class="US_EQUITY",
                )
            ]
        )
        db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{ord(symbol):010d}", sic="3571")
        )
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO bars SELECT ?,timeframe,timestamp,open,high,low,close,?,"
                "trade_count,vwap FROM bars WHERE symbol='B'",
                (symbol, volume),
            )
    return db, sessions, days, tail, bars


def test_internal_gap_blocks_group_never_uses_rank_six_and_exports_once(tmp_path, config):
    db, sessions, days, *_ = seed_six(tmp_path)
    # Another SIC2 continues forming pairs while SIC2 35 is unresolved.
    for symbol, source in (("X", "A"), ("Y", "B")):
        db.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    tradable=True,
                    fractionable=False,
                    asset_class="US_EQUITY",
                )
            ]
        )
        db.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=f"{ord(symbol):010d}", sic="6021")
        )
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO bars SELECT ?,timeframe,timestamp,open,high,low,close,volume,"
                "trade_count,vwap FROM bars WHERE symbol=?",
                (symbol, source),
            )
    baseline = prepare_pairs_data(db, config, START, END, preflight=True)
    assert [r["symbol"] for r in baseline.manifest["selected"][str(START)]["35"]] == list("ABCDE")
    missing_day = sessions[sessions.index(START) - 7]
    with db.connect() as connection:
        saved = tuple(
            connection.execute(
                "SELECT * FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing_day}%",)
            ).fetchone()
        )
        connection.execute(
            "DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing_day}%",)
        )
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="internal_gap", preflight=True
    )
    manifest = json.loads(paths["pair_candidates"].read_text())
    for groups in manifest["selected"].values():
        assert "35" not in groups  # Neither A nor rank-six F is frozen for this group.
        assert [r["symbol"] for r in groups["60"]] == ["X", "Y"]
    assert all(r["sic2"] == "60" for r in manifest["candidates"])
    assert summary["unresolved_sic2_sessions"] == len(days)
    assert not summary["selection_membership_resolved"]
    assert not summary["ready_for_local_validation"]
    assert summary["unavailable_liquidity_symbol_sessions"] == len(days)
    assert summary["selection_history_diagnostics"] == []
    unresolved = summary["unresolved_selection_membership"]
    assert all(
        r["status"] == "SELECTION_MEMBERSHIP_UNRESOLVED" and r["symbols"] == ["A"]
        for r in unresolved
    )
    requirements = json.loads(paths["daily_requirements"].read_text())
    assert len(requirements["required_sessions"]) == summary["local_missing_data"] == 1
    gap = requirements["required_sessions"][0]
    assert gap["symbol"] == "A" and gap["session"] == str(missing_day)
    assert gap["sic2"] == "35"
    assert gap["affected_signal_sessions"] == [str(d) for d in days]
    assert gap["roles"] == "LIQUIDITY_SELECTION_INTERNAL_GAP"
    assert gap["status"] == "LOCAL_MISSING_FETCHABLE"
    assert gap["input_scope"] == "VALIDATION_BLOCKING_REQUIREMENT" and gap["validation_blocking"]
    with paths["coverage"].open(newline="", encoding="utf-8") as handle:
        exported = [r for r in csv.DictReader(handle) if r["status"] == "LOCAL_MISSING_FETCHABLE"]
    assert len(exported) == 1 and exported[0]["session"] == str(missing_day)
    with db.connect() as connection:
        connection.execute(f"INSERT INTO bars VALUES ({','.join('?' for _ in saved)})", saved)
    restored = prepare_pairs_data(db, config, START, END, preflight=True)
    assert restored.manifest["selected"] == baseline.manifest["selected"]
    assert restored.manifest["discovery_counts"]["unresolved_selection_membership"] == []
    assert summary_metadata(restored, preflight=True)["ready_for_local_validation"]


@pytest.mark.parametrize("offset", [1, 7])
def test_previous_close_or_adv_internal_gap_is_blocking(tmp_path, config, offset):
    db, sessions, *_ = seed_six(tmp_path, end=START)
    missing = sessions[sessions.index(START) - offset]
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing}%",)
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert prepared.manifest["selected"][str(START)] == {}
    assert prepared.manifest["discovery_counts"]["internal_selection_gaps"][0]["session"] == str(
        missing
    )
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


def test_price_gate_excludes_before_requiring_adv_remediation(tmp_path, config):
    db, sessions, *_ = seed_six(tmp_path, end=START)
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{sessions[53]}%",)
        )
        connection.execute(
            "UPDATE bars SET open=1,high=1,low=1,close=1 WHERE symbol='A' AND timestamp LIKE ?",
            (f"{sessions[59]}%",),
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert [r["symbol"] for r in prepared.manifest["selected"][str(START)]["35"]] == list("BCDEF")
    assert prepared.manifest["discovery_counts"]["internal_selection_gaps"] == []
    assert prepared.manifest["discovery_counts"]["unavailable_liquidity_symbol_sessions"] == 0
    assert not any(r["symbol"] == "A" for r in prepared.requirements)
    assert summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


def test_history_before_calibration_still_establishes_internal_gap(tmp_path, config):
    db, sessions, *_ = seed(tmp_path, end=START)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT symbol,timeframe,'2020-01-02T00:00:00+00:00',"
            "open,high,low,close,volume,"
            "trade_count,vwap FROM bars WHERE symbol='A' AND timestamp LIKE ?",
            (f"{sessions[0]}%",),
        )
        connection.execute(
            "DELETE FROM bars WHERE symbol='A' AND timestamp>=?", (str(sessions[0]),)
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    gaps = prepared.manifest["discovery_counts"]["internal_selection_gaps"]
    assert len(gaps) == 20 and all(r["symbol"] == "A" for r in gaps)
    assert not prepared.manifest["discovery_counts"]["selection_history_diagnostics"]
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


def test_prefix_and_internal_classification_use_only_observations_through_t(config):
    sessions, days, _, bars = synthetic()
    # First observation occurs after the earlier signal. It cannot establish history at T.
    bars = {(s, d): b for (s, d), b in bars.items() if s != "A" or d >= days[2]}
    earlier = days[:2]
    original = selection_inputs(identities(), bars, sessions, earlier, config, {"A": days[2]})
    truncated = {key: b for key, b in bars.items() if key[1] <= earlier[-1]}
    assert original == selection_inputs(identities(), truncated, sessions, earlier, config)
    assert (
        original[1]["selection_history_diagnostics"][0]["status"]
        == "INSUFFICIENT_SELECTION_HISTORY"
    )
    assert not original[1]["internal_selection_gaps"]
    # During warmup, a hole AFTER the first observation is still an internal gap.
    bars.pop(("A", days[3]))
    inputs, counts = selection_inputs(identities(), bars, sessions, [days[4]], config)
    assert inputs[str(days[4])] == {}
    assert counts["internal_selection_gaps"][0]["session"] == str(days[3])
    truncated = {key: b for key, b in bars.items() if key[1] <= days[4]}
    assert (inputs, counts) == selection_inputs(
        identities(), truncated, sessions, [days[4]], config
    )


def test_old_history_anchor_keeps_a_calibration_prefix_gap_blocking(tmp_path, config):
    db, sessions, *_ = seed(tmp_path, end=START)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT symbol,timeframe,'2020-01-02T00:00:00+00:00',"
            "open,high,low,close,volume,trade_count,vwap FROM bars "
            "WHERE symbol='A' AND timestamp LIKE ?",
            (f"{sessions[0]}%",),
        )
        connection.execute(
            "DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{sessions[0]}%",)
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    gaps = [r for r in prepared.requirements if r["status"] == "LOCAL_MISSING_FETCHABLE"]
    assert len(gaps) == 1 and gaps[0]["session"] == str(sessions[0])
    assert gaps[0]["roles"] == "PAIR_CALIBRATION_AND_OBSERVATION"
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]
