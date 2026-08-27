from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_system.backtest import engine as engine_module
from trading_system.backtest import preflight as preflight_module
from trading_system.backtest.intraday_hybrid import intraday_hybrid_label
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import (
    comparison_strategy_label,
    lifecycle_for_preset,
    research_family_runs,
    research_metadata,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.intraday_remediation import candidate_requirements_from_report
from trading_system.models.backtest import (
    PositionManagementPreset,
    ResearchLifecycle,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.market_data import BarTimeframe

KIND = StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID
F0_PATHS = {
    "F0/C-intraday-dynamic",
    "F3/C-intraday-thesis-recovery",
    "F5/C-intraday-first-hour-pullback-f0-management",
}
F_INTRADAY_PATH = "F-intraday/F-intraday-dynamic"
F5_RUN = (
    StrategyVariant.FULL,
    PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
)
F_INTRADAY_RUN = (
    StrategyVariant.QUALITY_VALUE_MOMENTUM,
    PositionManagementPreset.INTRADAY_DYNAMIC,
)


def _config():
    load_settings.cache_clear()
    return load_settings().strategy


class _FixtureScreens:
    def __init__(self, records_by_session: dict[date, tuple[SimpleNamespace, ...]]) -> None:
        self.records_by_session = records_by_session

    def screen(self, session: date) -> SimpleNamespace:
        return SimpleNamespace(records=self.records_by_session.get(session, ()))


def test_f_intraday_composes_f_selection_with_unmodified_f0_management() -> None:
    runs = research_family_runs(KIND)
    f0_run = (StrategyVariant.FULL, PositionManagementPreset.INTRADAY_DYNAMIC)
    f_intraday_run = (
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.INTRADAY_DYNAMIC,
    )

    assert runs == (
        f0_run,
        (StrategyVariant.FULL, PositionManagementPreset.F3_INTRADAY_THESIS_RECOVERY),
        (
            StrategyVariant.FULL,
            PositionManagementPreset.F5_INTRADAY_FIRST_HOUR_PULLBACK_F0_MANAGEMENT,
        ),
        f_intraday_run,
    )
    assert research_metadata(*f0_run).display_name == "F0/C-intraday-dynamic"
    assert research_metadata(*f_intraday_run).display_name == F_INTRADAY_PATH
    assert intraday_hybrid_label(*f0_run) == "F0/C-intraday-dynamic"
    assert intraday_hybrid_label(*f_intraday_run) == F_INTRADAY_PATH
    assert research_metadata(
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
        PositionManagementPreset.CONFIGURED,
    ).display_name == "F/configured"
    assert lifecycle_for_preset(
        PositionManagementPreset.INTRADAY_DYNAMIC,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
    ) is ResearchLifecycle.ACTIVE_RESEARCH

    labels = {comparison_strategy_label(KIND, *run) for run in runs}
    assert F_INTRADAY_PATH in labels
    assert len(labels) == len(runs)
    assert F_INTRADAY_PATH not in {"F/configured", "F0/C-intraday-dynamic"}

    config = _config()
    original = config.position_management.model_dump(mode="json")
    f0_management = position_management_preset(
        config.position_management,
        f0_run[1],
        legacy_max_holding_days=config.backtest.max_holding_days,
    )
    f_intraday_management = position_management_preset(
        config.position_management,
        f_intraday_run[1],
        legacy_max_holding_days=config.backtest.max_holding_days,
    )

    assert f_intraday_management == f0_management
    assert not f_intraday_management.max_hold.enabled
    assert config.position_management.model_dump(mode="json") == original


def test_f_intraday_candidate_paths_are_provenanced_and_physically_deduplicated(
    tmp_path, monkeypatch
) -> None:
    sessions = (date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5))
    source = _FixtureScreens(
        {
            sessions[0]: tuple(SimpleNamespace(symbol=symbol) for symbol in ("A", "B", "C")),
        }
    )
    eligible = {
        StrategyVariant.FULL: {"A", "B"},
        StrategyVariant.QUALITY_VALUE_MOMENTUM: {"B", "C"},
    }
    monkeypatch.setattr(
        engine_module,
        "evaluate_variant_entry",
        lambda record, variant, config: SimpleNamespace(
            eligible=record.symbol in eligible[variant]
        ),
    )
    config = _config()
    runs = research_family_runs(KIND)
    requirements = engine_module.determine_intraday_comparison_requirements(
        config,
        runs,
        source,
        sessions,
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.symbols == ("A", "B", "C")
    paths = {path.variant: path for path in requirement.candidate_path_requirements}
    assert set(paths) == {
        StrategyVariant.FULL,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
    }
    c_symbols = {
        symbol
        for symbol, _session in paths[StrategyVariant.FULL].first_execution_sessions
    }
    assert c_symbols == {
        "A",
        "B",
    }
    assert {
        symbol
        for symbol, _session in paths[
            StrategyVariant.QUALITY_VALUE_MOMENTUM
        ].first_execution_sessions
    } == {"B", "C"}
    assert paths[StrategyVariant.FULL].runs == runs[:3]
    assert paths[StrategyVariant.QUALITY_VALUE_MOMENTUM].runs == (runs[-1],)

    database = Database(tmp_path / "f-intraday.sqlite3")
    database.initialize()
    labels = [comparison_strategy_label(KIND, *run) for run in runs]
    preparation = SimpleNamespace(runs=runs, intraday_requirements=requirements)
    intraday = preflight_module._intraday_preflight(
        database,
        config,
        preparation,
        labels,
        sessions[0],
        sessions[-1],
    )
    candidate_report = preflight_module._candidate_report(
        sessions[0],
        sessions[-1],
        KIND,
        preparation,
        intraday,
        None,
    )
    physical = candidate_requirements_from_report(
        candidate_report,
        start=sessions[0],
        end=sessions[-1],
        timeframes=(requirement.timeframe,),
    )

    assert len(physical) == 6
    by_key = {(item.symbol, item.session): item for item in physical}
    assert set(by_key) == {
        (symbol, session) for symbol in ("A", "B", "C") for session in sessions[1:]
    }
    assert set(by_key[("A", sessions[1])].candidate_paths) == F0_PATHS
    assert set(by_key[("C", sessions[1])].candidate_paths) == {F_INTRADAY_PATH}
    assert set(by_key[("B", sessions[1])].candidate_paths) == F0_PATHS | {
        F_INTRADAY_PATH
    }
    assert by_key[("B", sessions[1])].requirement_type == "candidate_session"
    assert set(by_key[("B", sessions[2])].candidate_paths) == F0_PATHS | {
        F_INTRADAY_PATH
    }
    assert by_key[("B", sessions[2])].requirement_type == (
        "potential_open_position_session"
    )


@pytest.mark.parametrize(
    ("runs", "f5_applicable"),
    (
        pytest.param((F_INTRADAY_RUN,), False, id="f-intraday-only"),
        pytest.param((F5_RUN,), True, id="f5-only"),
        pytest.param((F5_RUN, F_INTRADAY_RUN), True, id="shared-f5-and-f-intraday"),
    ),
)
def test_f5_preflight_diagnostics_follow_candidate_path_provenance(
    tmp_path,
    monkeypatch,
    runs,
    f5_applicable: bool,
) -> None:
    session = date(2025, 5, 2)
    opening = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    candidate_session = (("AAA", session),)
    path_requirements = tuple(
        engine_module.IntradayCandidatePathRequirement(
            variant=variant,
            runs=((variant, preset),),
            first_execution_sessions=candidate_session,
            candidate_execution_sessions=candidate_session,
        )
        for variant, preset in runs
    )
    requirement = engine_module.IntradayPrefetchRequirement(
        timeframe=BarTimeframe.MINUTES_15,
        variants=tuple(variant for variant, _preset in runs),
        symbols=("AAA",),
        first_execution_sessions=candidate_session,
        candidate_execution_sessions=candidate_session,
        comparison_sessions=(session,),
        requested_start=opening - timedelta(days=1),
        requested_end=opening + timedelta(days=1),
        warmup_bars=0,
        extended_hours=False,
        candidate_path_requirements=path_requirements,
    )
    labels = [comparison_strategy_label(KIND, *run) for run in runs]
    preparation = SimpleNamespace(runs=runs, intraday_requirements=(requirement,))
    database = Database(tmp_path / "f5-path-scope.sqlite3")
    database.initialize()
    planner_calls: list[date] = []

    def fixture_plan(plan_session, session_bars, prior_bars):
        planner_calls.append(plan_session)
        return SimpleNamespace(
            failure_reason="missing_pullback_execution_bar",
            entry_timestamp=None,
        )

    monkeypatch.setattr(preflight_module, "plan_first_hour_pullback", fixture_plan)
    report = preflight_module._intraday_preflight(
        database,
        _config(),
        preparation,
        labels,
        session,
        session,
    )

    assert len(report["candidate_session_details"]) == 1
    detail = report["candidate_session_details"][0]
    assert detail["f5_diagnostics_applicable"] is f5_applicable
    assert len(planner_calls) == int(f5_applicable)
    assert report["missing_required_first_hour_f5_sessions"] == int(f5_applicable)
    assert report["missing_f5_pullback_execution_bars"] == int(f5_applicable)
    if f5_applicable:
        assert len(detail["missing_required_first_hour_timestamps"]) == 4
        assert detail["f5_entry_plan_status"] == "missing_pullback_execution_bar"
    else:
        assert detail["missing_required_first_hour_timestamps"] is None
        assert detail["f5_entry_plan_status"] is None
