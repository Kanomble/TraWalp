"""Synthetic coverage: insufficient selection history is not a remediation backlog."""

import csv
import json
from datetime import date

import pytest
from test_pairs_stat_arb_v1 import END, START, seed
from test_pairs_stat_arb_v1 import config as config
from test_pairs_stat_arb_v1 import offline as offline

from trading_system.backtest.pairs_stat_arb_v1_data import discover_pairs, prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_research import (
    run_pairs_stat_arb_v1,
    summary_metadata,
)
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
