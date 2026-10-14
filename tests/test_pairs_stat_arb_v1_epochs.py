"""Small offline regressions for the Pairs-only security interval boundary."""

import json
from datetime import UTC, date, datetime

import pytest
from test_pairs_stat_arb_v1 import START, identities, seed, synthetic, synthetic_epoch
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types
from test_pairs_stat_arb_v1_security import config as config

from trading_system.backtest.pairs_stat_arb_v1 import calibration, observe, simulate_prepared_pairs
from trading_system.backtest.pairs_stat_arb_v1_data import (
    discover_pairs,
    load_daily,
    load_history_anchors,
    prepare_pairs_data,
)
from trading_system.backtest.pairs_stat_arb_v1_manifest import MISMATCH, calendar_window
from trading_system.backtest.pairs_stat_arb_v1_research import (
    run_pairs_stat_arb_v1,
    summary_metadata,
)
from trading_system.backtest.pairs_stat_arb_v1_selection import selection_inputs
from trading_system.data.database import Database
from trading_system.data.security_identity import (
    ACTIVE,
    OUTSIDE,
    UNKNOWN,
    current_identity_epoch,
    epoch_status,
)
from trading_system.models.market_data import DailyBar


def add_bar(db, symbol, day, price=100):
    db.upsert_bars(
        [
            DailyBar(
                symbol=symbol,
                timestamp=datetime.combine(day, datetime.min.time(), UTC),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000_000,
            )
        ]
    )


def test_foreign_old_ticker_bars_cannot_anchor_current_security(tmp_path, config):
    db = Database(tmp_path / "epochs.sqlite3")
    db.initialize()
    add_bar(db, "A", date(2021, 1, 4))
    add_bar(db, "A", date(2025, 6, 18))
    row = identities(("A",))[0]
    row["identity_epoch"] = synthetic_epoch("A", row["cik"], date(2025, 6, 18)).record()
    mapping = {"A": row}
    assert load_history_anchors(db, ["A"], date(2024, 1, 2), identity_by_symbol=mapping) == {}
    assert load_history_anchors(db, ["A"], date(2026, 1, 2), identity_by_symbol=mapping) == {
        "A": date(2025, 6, 18)
    }
    assert load_history_anchors(db, ["A"], date(2026, 1, 2)) == {}
    sessions, days, _ = calendar_window(date(2024, 1, 2), date(2024, 1, 2))
    bars = load_daily(db, ["A"], date(2020, 1, 1), date(2026, 1, 2))
    selected, _, counts = discover_pairs(
        [row], bars, sessions, days, config, {"A": date(2021, 1, 4)}
    )
    assert selected[str(days[0])] == {}
    assert counts["internal_selection_gaps"] == []
    assert counts["selection_membership_resolved"]
    assert counts["identity_epoch_diagnostics"][0]["status"] == OUTSIDE
    assert not counts["identity_epoch_diagnostics"][0]["validation_blocking"]


def test_missing_input_inside_verified_epoch_stays_blocking_and_fetchability_unknown(
    tmp_path, config
):
    db, sessions, *_ = seed(tmp_path, end=START)
    missing = sessions[sessions.index(START) - 7]
    with db.connect() as c:
        c.execute("DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing}%",))
        c.execute(
            "INSERT INTO sync_state VALUES ('daily_history_coverage','A',?,?)",
            (
                "2026-10-14",
                json.dumps(
                    {
                        "start": str(sessions[0]),
                        "end_exclusive": "2026-01-01",
                        "feed": config.universe.market_data_feed.lower(),
                        "adjustment": config.universe.market_data_adjustment,
                    }
                ),
            ),
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    gap = next(r for r in prepared.requirements if r["status"] == "LOCAL_MISSING")
    assert gap["session"] == str(missing) and gap["validation_blocking"]
    assert gap["identity_epoch_status"] == ACTIVE
    assert gap["provider_session_absence"] == "NOT_ESTABLISHED"
    assert gap["provider_range_verified"] is True
    assert gap["provider_range_status"] == "PROVIDER_RANGE_CHECKED"
    assert gap["provider_session_absence_status"] == "PROVIDER_SESSION_ABSENCE_UNKNOWN"
    assert gap["fetchability"] == "UNKNOWN"
    assert not prepared.manifest["discovery_counts"]["selection_membership_resolved"]
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]


def test_unknown_identity_with_old_and_new_bars_remains_unresolved(tmp_path, config):
    db, sessions, *_ = seed(tmp_path, end=START)
    add_bar(db, "A", date(2021, 1, 4))
    with db.connect() as c:
        c.execute("DELETE FROM symbol_identity_epochs WHERE symbol='A'")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    row = next(r for r in prepared.manifest["universe"] if r["symbol"] == "A")
    assert row["identity_epoch"] is None
    assert epoch_status(row, START) == UNKNOWN
    assert not any(s == "A" for s, _ in prepared.bars)
    diagnostic = prepared.manifest["discovery_counts"]["identity_epoch_diagnostics"][0]
    assert diagnostic["status"] == UNKNOWN and diagnostic["validation_blocking"]
    assert not prepared.manifest["discovery_counts"]["selection_membership_resolved"]
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]
    with pytest.raises(ValueError, match="SELECTION_MEMBERSHIP_UNRESOLVED"):
        prepare_pairs_data(db, config, START, START)


@pytest.mark.parametrize("leg", ["A", "B"])
def test_calibration_cannot_cross_epoch_for_either_leg(tmp_path, config, leg):
    db, sessions, days, _, bars = seed(tmp_path, end=START)
    rows = identities()
    current = next(r for r in rows if r["symbol"] == leg)
    current["identity_epoch"] = synthetic_epoch(leg, current["cik"], sessions[30]).record()
    # Explicitly alter the temporary fixture; production ingestion never replaces evidence.
    with db.connect() as c:
        c.execute(
            "UPDATE symbol_identity_epochs SET effective_start=? WHERE symbol=?",
            (str(sessions[30]), leg),
        )
    # Even an unfiltered raw bar map must not join the two blocks.
    mapping = {r["symbol"]: r for r in rows}
    stats = calibration(bars, sessions, 60, "A", "B", identity_by_symbol=mapping)
    assert stats == {"calibration_status": "IDENTITY_EPOCH_CROSSING"}
    assert observe(bars, sessions, 60, "A", "B", identity_by_symbol=mapping) is None
    selected, candidates, _ = discover_pairs(rows, bars, sessions, days, config)
    assert selected[str(START)]["35"]
    assert candidates[0]["calibration_status"] == "IDENTITY_EPOCH_CROSSING"
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert prepared.manifest["candidates"][0]["calibration_status"] == "IDENTITY_EPOCH_CROSSING"
    assert not any(s == leg and d < sessions[30] for s, d in prepared.bars)
    assert not any(
        r["symbol"] == leg and r["session"] < str(sessions[30]) for r in prepared.requirements
    )


def test_adv_and_previous_close_cannot_use_foreign_epoch_prices(config):
    sessions, days, _, bars = synthetic(end=START)
    row = identities(("A",))[0]
    row["identity_epoch"] = synthetic_epoch("A", row["cik"], sessions[59]).record()
    inputs, counts = selection_inputs([row], bars, sessions, days, config)
    assert inputs[str(START)] == {}
    assert counts["internal_selection_gaps"] == []
    assert counts["selection_history_diagnostics"][0]["status"] == "INSUFFICIENT_SELECTION_HISTORY"
    assert counts["selection_membership_resolved"]


def test_epoch_end_protects_outcomes_and_readiness(tmp_path, config):
    db, sessions, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute(
            "UPDATE symbol_identity_epochs SET effective_end=? WHERE symbol='A'",
            (str(sessions[62]),),
        )
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert prepared.manifest["candidates"][0]["calibration_status"] == "ELIGIBLE"
    assert prepared.metadata["identity_epoch_outcome_requirements"] == 4
    assert not any(s == "A" and d >= sessions[62] for s, d in prepared.bars)
    assert not summary_metadata(prepared, preflight=True)["ready_for_local_validation"]
    with pytest.raises(ValueError, match="IDENTITY_EPOCH_OUTCOME_UNRESOLVED"):
        simulate_prepared_pairs(prepared)


@pytest.mark.parametrize(
    "kind", ["CURRENT_REFERENCE", "FIRST_BAR", "LONG_GAP", "SEC_8_A", "SEC_424B4", "COMPANY_NAME"]
)
def test_non_temporal_sources_cannot_be_verified(kind):
    row = synthetic_epoch("A", "0000000001").record()
    row["evidence_type"] = kind
    from trading_system.data.security_identity import SymbolIdentityEpoch

    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_REQUIRED"):
        SymbolIdentityEpoch.from_record(row)


def test_conflicting_mapping_and_current_identity_drift_fail_closed():
    row = identities(("A",))[0]
    epoch = row["identity_epoch"]
    assert current_identity_epoch(row, [epoch]) == epoch
    assert current_identity_epoch({**row, "cik": "0000009999"}, [epoch]) is None
    assert current_identity_epoch(row, [{**epoch, "verified": False}]) is None
    foreign = {**epoch, "cik": "0000009999", "evidence_reference": "foreign"}
    assert current_identity_epoch(row, [epoch, foreign]) is None
    figi = {**epoch, "stable_security_identity": "SHARE_CLASS_FIGI:old"}
    assert current_identity_epoch({**row, "share_class_figi": "new"}, [figi]) is None


def test_anchor_requires_usable_unique_bar(tmp_path):
    db, sessions, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute(
            "UPDATE bars SET close='NaN' WHERE symbol='A' AND timestamp LIKE ?",
            (f"{sessions[0]}%",),
        )
        c.execute(
            "INSERT INTO bars SELECT symbol,timeframe,replace(timestamp,'00:00:00','01:00:00'),"
            "open,high,low,close,volume,trade_count,vwap FROM bars "
            "WHERE symbol='A' AND timestamp LIKE ?",
            (f"{sessions[1]}%",),
        )
    mapping = {r["symbol"]: r for r in identities()}
    anchors = load_history_anchors(db, ["A"], START, identity_by_symbol=mapping)
    assert anchors == {"A": sessions[2]}


def test_epoch_evidence_change_invalidates_saved_manifest(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    manifest = tmp_path / "pairs.json"
    manifest.write_text(json.dumps(prepared.manifest), encoding="utf-8")
    with db.connect() as c:
        c.execute("UPDATE symbol_identity_epochs SET effective_start='2023-01-01' WHERE symbol='A'")
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, START, candidate_manifest=manifest)


def test_small_report_exports_unknown_identity_requirements(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    with db.connect() as c:
        c.execute("DELETE FROM symbol_identity_epochs")
    summary, paths = run_pairs_stat_arb_v1(
        db, config, START, START, tmp_path, stem="unknown", preflight=True
    )
    doc = json.loads(paths["daily_requirements"].read_text())
    assert len(doc["identity_epoch_requirements"]) == 2
    assert doc["required_sessions"] == []
    assert not summary["ready_for_local_validation"]
