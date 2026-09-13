"""Three small local regressions for the fixed I0/I1 observable candidate support."""

from types import SimpleNamespace

import pytest
import test_f_lifecycle_v2 as fixtures

from trading_system.backtest import lifecycle_validation as validation
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.entry_quality import EntryQualityStatus, missing_session_timestamps
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.data.intraday_remediation import (
    PROVIDER_OBSERVATION_SOURCE,
    CandidateIntradayRequirement,
    IntradayRequirementStatus,
    _observation_key,
    _observation_payload,
)
from trading_system.models.market_data import BarTimeframe

config = fixtures.config
local_market = fixtures.local_market


def _observe_absence(
    database, config, execution, *, status=IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT
):
    requirement = CandidateIntradayRequirement("AAA", execution, BarTimeframe.MINUTES_15)
    database.set_sync_value(
        PROVIDER_OBSERVATION_SOURCE,
        _observation_key(requirement),
        _observation_payload(
            "AAA",
            BarTimeframe.MINUTES_15,
            execution,
            feed=config.universe.market_data_feed,
            adjustment=config.universe.market_data_adjustment,
            extended_hours=False,
            status=status,
            missing_timestamps=missing_session_timestamps([], execution),
            error="fixture provider failure"
            if status is IntradayRequirementStatus.PROVIDER_CHECK_FAILED
            else None,
        ),
    )


def _manifest(monkeypatch, database, sessions, config, tmp_path):
    qualification = {"ready": True, "failure_reasons": []}
    source = fixtures.Screens(sessions[0])
    preparation = SimpleNamespace(sessions=sessions, screen_source=source)
    report, requirements = validation.build_f_intraday_entry_preflight(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        qualification=qualification,
    )
    try:
        paths = validation.export_f_intraday_entry_preflight(
            report, requirements, tmp_path, stem="support"
        )
    finally:
        requirements.close()
    monkeypatch.setattr(validation, "_qualify_daily_research", lambda *args: qualification)
    return paths["intraday_candidates.json"]


def test_absent_rank_one_is_consumed_without_promoting_present_rank_two(local_market, config):
    database, sessions = local_market
    sessions = sessions[:5]
    assert config.portfolio.max_positions == 1
    database.upsert_bars([fixtures.bar("BBB", session) for session in sessions])
    database.upsert_bars(
        [
            bar.model_copy(update={"symbol": "BBB"})
            for bar in fixtures.native_session(sessions[1], weak=False)
        ]
    )
    source = fixtures.Screens(sessions[0], (fixtures.record("AAA"), fixtures.record("BBB")))
    statuses = {
        ("AAA", sessions[1]): IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value,
        ("BBB", sessions[1]): IntradayRequirementStatus.REQUIRED_PRESENT.value,
    }
    for veto in (False, True):
        engine = BacktestEngine(
            database,
            config,
            screen_source=source,
            entry_common_support=True,
            opening_weakness_veto=veto,
            intraday_session_statuses=statuses,
            require_complete_daily_position_bars=True,
        )
        result = engine.run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
        assert not result.positions and not result.trades
        assert result.skipped_entries[EntryQualityStatus.PROVIDER_ABSENT.value] == 1
        assert len(engine.entry_quality_events) == 1
        event = engine.entry_quality_events[0]
        assert (event["symbol"], event["candidate_rank"], event["candidate_count"]) == ("AAA", 1, 2)
        assert event["decision_timestamp"] is None and event["actual_entry_timestamp"] is None


def test_full_baseline_trades_while_both_common_support_runs_consume_signal(
    monkeypatch, local_market, config, tmp_path
):
    database, sessions = local_market
    sessions = sessions[:5]
    _observe_absence(database, config, sessions[1])
    path = _manifest(monkeypatch, database, sessions, config, tmp_path)
    bundle = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], candidate_manifest=path
    )
    assert list(bundle.results) == [
        "F-INTRADAY-ENTRY-I0_FULL",
        "F-INTRADAY-ENTRY-I0_COMMON_SUPPORT",
        "F-INTRADAY-ENTRY-I1_COMMON_SUPPORT",
    ]
    full = bundle.results["F-INTRADAY-ENTRY-I0_FULL"]
    reference = BacktestEngine(
        database,
        config,
        screen_source=fixtures.Screens(sessions[0]),
        require_complete_daily_position_bars=True,
    ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
    assert full.model_dump(exclude={"generated_at"}) == reference.model_dump(
        exclude={"generated_at"}
    )
    assert [position.symbol for position in full.positions] == ["AAA"]
    for name in ("I0_COMMON_SUPPORT", "I1_COMMON_SUPPORT"):
        result = bundle.results[f"F-INTRADAY-ENTRY-{name}"]
        assert not result.trades and not result.positions
        assert result.skipped_entries[EntryQualityStatus.PROVIDER_ABSENT.value] == 1
    summary = bundle.summary
    assert summary["portfolio_reruns"] == 3
    assert summary["provider_absent_candidate_sessions"] == 1
    assert summary["common_support_candidate_sessions"] == 0
    assert summary["provider_absent_signals_consumed_I0"] == 1
    assert summary["provider_absent_signals_consumed_I1"] == 1
    assert {row["status"] for row in bundle.tables["entry_quality_events"]} == {
        "PROVIDER_CONFIRMED_ABSENT"
    }
    assert (
        summary["comparisons"]["opening_weakness_effect"]["comparison"]
        == "I1_COMMON_SUPPORT - I0_COMMON_SUPPORT"
    )
    assert summary["comparisons"]["opening_weakness_effect"]["primary"] is True
    assert summary["comparisons"]["opening_weakness_effect"]["metric_deltas"]["total_return"] == 0
    assert (
        summary["comparisons"]["coverage_support_bias"]["metric_deltas"]["total_return"]
        == -full.metrics.total_return
    )
    assert summary["performance"]["peer_group_sessions_built"] == 0


def test_validation_accepts_ready_states_and_rejects_unresolved_provider_evidence(
    monkeypatch, local_market, config, tmp_path
):
    database, sessions = local_market
    sessions = sessions[:5]
    path = _manifest(monkeypatch, database, sessions, config, tmp_path)

    def run():
        return validation.run_f_intraday_entry(
            database, config, sessions[0], sessions[-1], candidate_manifest=path
        )

    with pytest.raises(ValueError, match="NOT_READY_LOCAL_GAPS"):
        run()
    _observe_absence(
        database, config, sessions[1], status=IntradayRequirementStatus.PROVIDER_CHECK_FAILED
    )
    with pytest.raises(ValueError, match="NOT_READY_PROVIDER_VERIFICATION_FAILURE"):
        run()
    _observe_absence(database, config, sessions[1])
    assert (
        run().summary["intraday_qualification"]["qualification_status"]
        == "READY_WITH_PROVIDER_ABSENCE"
    )
    # A later successful native sync makes the same Daily manifest fully supported.
    database.upsert_bars(fixtures.native_session(sessions[1], weak=True))
    bundle = run()
    assert bundle.summary["intraday_qualification"]["qualification_status"] == "READY"
    assert bundle.summary["provider_absent_candidate_sessions"] == 0
    assert bundle.summary["common_support_candidate_sessions"] == 1
    assert bundle.summary["provider_absent_signals_consumed_I0"] == 0
    assert bundle.summary["provider_absent_signals_consumed_I1"] == 0
    assert {row["status"] for row in bundle.tables["entry_quality_events"]} == {
        "OPENING_WEAKNESS_VETO"
    }
