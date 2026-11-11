"""Synthetic offline history/readiness evidence; never opens a journal."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_champion_consolidation import bar
from test_champion_consolidation import offline as offline
from test_forward_sip_contract import db as db
from test_forward_sip_contract import seed
from test_shadow_execution import ready_states
from test_shadow_qualification import SESSION, candidate, report

from trading_system.config import load_strategy_config
from trading_system.data.forward_market import sync_forward
from trading_system.data.market_sessions import (
    full_history_request_window,
    trading_sessions_between,
)
from trading_system.shadow.inputs import LocalInputs
from trading_system.shadow.readiness import evaluate_shadow_input_readiness


def test_short_sip_history_cannot_certify_economic_readiness(db):
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    seed(db, [bar("SPY", SESSION), bar("AAA", SESSION)])
    source = SimpleNamespace(
        screen=lambda day: report(
            candidate(
                count=1,
                reasons=("insufficient_market_history",),
            )
        )
    )
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs,
            load_strategy_config(),
            now=now,
            session=SESSION,
            screen_source=source,
        )
    assert not result.ready
    assert result.observation_ready
    assert result.qualification["rejected"] == {}
    assert result.details["blocking_reasons"] == ["RANKING_UNRESOLVED"]


def test_real_screener_short_history_remains_unresolved(db):
    from trading_system.models.fundamentals import CompanyIdentity
    from trading_system.models.market_data import TradableAsset

    db.upsert_assets(
        [
            TradableAsset(
                symbol="AAA",
                name="AAA",
                tradable=True,
                fractionable=True,
                asset_class="us_equity",
            )
        ]
    )
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571"))
    seed(db, [bar("AAA", SESSION), bar("SPY", SESSION)])
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs, load_strategy_config(), now=now, session=SESSION
        )
    assert len(result.raw_report.records) == 1
    row = result.raw_report.records[0]
    assert row.market_history_count == 1
    assert row.current_market_observation == "OBSERVATION_PRESENT"
    assert "insufficient_market_history" in row.exclusion_reasons
    assert result.qualification["rejected"] == {}
    assert not result.ready
    assert result.observation_ready


@pytest.mark.parametrize("gate", ["momentum", "liquidity"])
def test_history_derived_failure_needs_coverage(db, gate):
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    seed(db, [bar("AAA", SESSION), bar("SPY", SESSION)])
    row = (
        candidate(count=300, momentum126=-0.5)
        if gate == "momentum"
        else candidate(
            count=300,
            average_dollar_volume_20d=1,
            reasons=("insufficient_liquidity",),
        )
    )
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs,
            load_strategy_config(),
            now=now,
            session=SESSION,
            screen_source=SimpleNamespace(screen=lambda day: report(row)),
        )
    assert not result.ready
    assert result.qualification["rejected"] == {}


@pytest.mark.parametrize(
    "case,complete",
    [
        ("full", True),
        ("missing_prefix", False),
        ("young", False),
        ("zero_placeholder", False),
        ("interior_gap", False),
        ("older_rows_mask_gap", False),
    ],
)
def test_snapshot_history_evidence(db, case, complete):
    config = load_strategy_config()
    assert config.data_quality.min_market_history_days == 300
    start, _ = full_history_request_window(SESSION, 301)
    days = trading_sessions_between(start.date(), SESSION)
    values = [bar("AAA", day) for day in days[-300:]]
    if case == "missing_prefix":
        values = values[10:]
    elif case == "young":
        # Fixture truth: issuer listed ten sessions ago. No listing evidence
        # exists in the production schema, so it must remain undecidable there.
        values = values[-10:]
    elif case == "zero_placeholder":
        values[150] = values[150].model_copy(update={"volume": 0})
    elif case in {"interior_gap", "older_rows_mask_gap"}:
        del values[150]
        if case == "older_rows_mask_gap":
            values.insert(0, bar("AAA", days[0]))
    seed(db, values + [bar("SPY", SESSION)])
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    row = candidate(
        count=len(values), reasons=() if len(values) >= 300 else ("insufficient_market_history",)
    )
    with LocalInputs(db.path).snapshot() as inputs:
        # Stored count and source coverage are deliberately different concepts.
        assert len(inputs.bars_available_as_of("AAA", SESSION, limit=300)) == len(values)
        result = evaluate_shadow_input_readiness(
            inputs,
            config,
            now=now,
            session=SESSION,
            screen_source=SimpleNamespace(screen=lambda day: report(row)),
        )
    assert result.ready is complete
    assert result.observation_ready
    evidence = result.details["history_coverage"]["AAA"]
    assert evidence["listing_history"] == "unknown"
    assert (evidence["status"] == "complete") is complete
    assert result.qualification["rejected"] == {}


@pytest.mark.parametrize("reason", ["financial_excluded", "reit_excluded", "identity_conflict"])
def test_proven_exclusion_does_not_require_history(db, reason):
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    ready_states(db, now)
    seed(db, [bar("SPY", SESSION)])
    row = candidate(
        count=0, observation="OBSERVATION_ABSENT", reasons=(reason, "insufficient_market_history")
    )
    with LocalInputs(db.path).snapshot() as inputs:
        result = evaluate_shadow_input_readiness(
            inputs,
            load_strategy_config(),
            now=now,
            session=SESSION,
            screen_source=SimpleNamespace(screen=lambda day: report(row)),
        )
    assert result.ready
    assert result.qualification["rejected"] == {"AAA": reason}


@pytest.mark.parametrize("failure", ["partial", "exception", "iterator_exception", "invalid"])
def test_sync_acquisition_is_not_coverage_proof(db, failure):
    def response(*args):
        if failure == "exception":
            raise RuntimeError("synthetic provider failure")
        if failure == "iterator_exception":

            def stream():
                yield bar("AAA", SESSION)
                raise RuntimeError("synthetic pagination failure")

            return stream()
        if failure == "invalid":
            return [bar("AAA", SESSION).model_copy(update={"high": 0})]
        return [bar("AAA", SESSION)]

    client = SimpleNamespace(
        feed=SimpleNamespace(value="sip"),
        adjustment=SimpleNamespace(value="all"),
        daily_bars=response,
    )
    if failure == "partial":
        assert sync_forward(db, client, SESSION, SESSION, ["AAA"]) == 1
        state = db.sync_values("dataset")["forward_sip"]
        assert state["status"] == "success"
        assert state["coverage_status"] == "incomplete"
        assert state["coverage_missing_sessions"] == {"SPY": 1}
    else:
        with pytest.raises((RuntimeError, ValueError)):
            sync_forward(db, client, SESSION, SESSION, ["AAA"])
        state = db.sync_values("dataset")["forward_sip"]
        assert state["status"] == "failed"
        assert state["errors"] == 1
        assert "last_success_at" not in state
