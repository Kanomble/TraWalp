"""Fail-closed Pairs manifest tests using synthetic data only."""

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import pytest
from test_pairs_stat_arb_v1 import END, START, forbidden, seed
from test_pairs_stat_arb_v1 import config as config
from test_pairs_stat_arb_v1 import offline as offline

from trading_system.backtest import pairs_stat_arb_v1_data as data
from trading_system.backtest.liquid_universe import fingerprint
from trading_system.backtest.pairs_stat_arb_v1 import PAIRS_V1
from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
from trading_system.backtest.pairs_stat_arb_v1_manifest import (
    MISMATCH,
    load_manifest,
    manifest_contract,
)
from trading_system.backtest.pairs_stat_arb_v1_research import run_pairs_stat_arb_v1


def save(path, payload):
    payload["fingerprint"] = fingerprint(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def different(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    return "CHANGED"


@pytest.mark.parametrize("field", list(asdict(PAIRS_V1)))
def test_every_primary_economic_semantic_rejected_even_with_recomputed_checksum(
    tmp_path, config, field
):
    contract = manifest_contract(config, START, END)
    payload = deepcopy(contract)
    payload["strategy_definition"][field] = different(payload["strategy_definition"][field])
    path = save(tmp_path / "changed.json", payload)
    with pytest.raises(ValueError, match=MISMATCH):
        load_manifest(path, contract)


@pytest.mark.parametrize(
    "field",
    [
        "manifest_type",
        "manifest_version",
        "coverage_semantics",
        "source_input_scope",
        "hypothesis",
        "requested_start",
        "requested_end",
        "signal_start",
        "signal_end",
        "signal_sessions",
        "outcome_tail_sessions",
        "history_sessions",
        "min_price_source",
        "min_price",
        "min_adv20_source",
        "min_adv20",
        "universe_semantics",
        "sic_semantics",
        "market_data_feed",
        "market_data_adjustment",
        "signal_z_deferred",
        "status_at_creation",
    ],
)
def test_contract_drift_rejected_independent_of_checksum(tmp_path, config, field):
    contract = manifest_contract(config, START, END)
    payload = deepcopy(contract)
    payload[field] = different(payload[field])
    path = save(tmp_path / "changed.json", payload)
    with pytest.raises(ValueError, match=MISMATCH):
        load_manifest(path, contract)


def test_missing_manifest_never_falls_back_to_discovery(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, END, candidate_manifest=tmp_path / "absent.json")


@pytest.mark.parametrize(
    "change",
    [
        "dates",
        "identity",
        "sic",
        "source",
        "duplicate_pair",
        "reverse_pair",
        "pair_stats",
        "top_six",
        "corrupt_json",
    ],
)
def test_local_contract_and_membership_drift(tmp_path, config, change):
    db, *_ = seed(tmp_path)
    prepared = prepare_pairs_data(db, config, START, END, preflight=True)
    payload = prepared.manifest
    path = tmp_path / "pairs.json"
    end = END
    if change == "dates":
        end += timedelta(days=1)
    elif change in {"identity", "sic", "source"}:
        with db.connect() as connection:
            if change == "identity":
                connection.execute("UPDATE companies SET cik='0000009999' WHERE symbol='A'")
            elif change == "sic":
                connection.execute("UPDATE companies SET sic='6021' WHERE symbol='A'")
            else:
                connection.execute("UPDATE bars SET volume=volume+1 WHERE symbol='A'")
    elif change == "duplicate_pair":
        payload["candidates"].append(payload["candidates"][0])
    elif change == "reverse_pair":
        payload["candidates"][0].update(symbol_a="B", symbol_b="A")
    elif change == "pair_stats":
        payload["candidates"][0]["correlation"] = 0.999
    elif change == "top_six":
        names = payload["selected"][str(START)]["35"]
        names.extend([names[0]] * 4)
    save(path, payload)
    if change == "corrupt_json":
        path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match=MISMATCH):
        prepare_pairs_data(db, config, START, end, candidate_manifest=path)


def test_tail_remediation_does_not_change_frozen_membership(tmp_path, config):
    db, _, _, tail, _ = seed(tmp_path, end=START)
    with db.connect() as connection:
        connection.execute("DELETE FROM bars WHERE symbol='A' AND timestamp>=?", (str(tail[0]),))
    original = prepare_pairs_data(db, config, START, START, preflight=True)
    path = save(tmp_path / "pairs.json", original.manifest)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars SELECT 'A',timeframe,timestamp,open,high,low,close,volume,"
            "trade_count,vwap FROM bars WHERE symbol='B' AND timestamp>=?",
            (str(tail[0]),),
        )
    prepared = prepare_pairs_data(db, config, START, START, candidate_manifest=path)
    assert original.manifest == prepared.manifest
    assert prepared.metadata["outcome_data_end"] == str(tail[-1])


def test_adjustment_provenance_conflict_and_raw_rejected(tmp_path, config):
    db, *_ = seed(tmp_path)
    db.set_sync_value("daily_history_coverage", "A", {"feed": "iex", "adjustment": "raw"})
    with pytest.raises(ValueError, match="PROVENANCE_MISMATCH"):
        prepare_pairs_data(db, config, START, END)
    config.universe.market_data_adjustment = "raw"
    with pytest.raises(ValueError, match="REQUIRES_ADJUSTED_DAILY"):
        prepare_pairs_data(db, config, START, END)


def test_explicit_discovery_and_immutable_reports(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    with pytest.raises(ValueError, match="requires a manifest"):
        run_pairs_stat_arb_v1(db, config, START, END, tmp_path, stem="missing")
    run_pairs_stat_arb_v1(db, config, START, END, tmp_path, stem="once", preflight=True)
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    with pytest.raises(FileExistsError):
        run_pairs_stat_arb_v1(db, config, START, END, tmp_path, stem="once", preflight=True)


def test_normal_manifest_validation_exports_without_discovery(tmp_path, config, monkeypatch):
    db, *_ = seed(tmp_path)
    before, paths = run_pairs_stat_arb_v1(
        db, config, START, END, tmp_path, stem="frozen", preflight=True
    )
    monkeypatch.setattr(data, "discover_pairs", forbidden)
    after, outputs = run_pairs_stat_arb_v1(
        db,
        config,
        START,
        END,
        tmp_path,
        stem="outcomes",
        candidate_manifest=paths["pair_candidates"],
    )
    assert after["manifest_fingerprint"] == before["manifest_fingerprint"]
    assert after["candidate_source"] == "MANIFEST"
    assert "trades" in outputs
