from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest import preflight as preflight_module
from trading_system.backtest.engine import BacktestEngine, IntradayPrefetchRequirement
from trading_system.backtest.qualification import qualify_historical_screen_start
from trading_system.backtest.research_registry import (
    comparison_strategy_label,
    research_family_runs,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.sync import DataSynchronizer
from trading_system.data.universe import UniverseSnapshot
from trading_system.models.backtest import StrategyComparisonKind
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, MarketDataBar, TradableAsset
from trading_system.strategy.screener import Screener

START = date(2025, 5, 1)
END = date(2025, 5, 2)


def _config():
    load_settings.cache_clear()
    return load_settings().strategy


def _sessions(config=None) -> tuple[date, ...]:
    resolved = config or _config()
    first = daily_warmup_start(START, required_daily_warmup_sessions(resolved))
    return tuple(trading_sessions_between(first, END))


def _bar(symbol: str, session: date) -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        timeframe=BarTimeframe.DAY_1,
        timestamp=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000_000,
        trade_count=100,
        vwap=Decimal("100"),
    )


def _database(
    tmp_path,
    histories: dict[str, tuple[date, ...] | list[date]],
    *,
    conflicts: tuple[str, ...] = (),
) -> Database:
    database = Database(tmp_path / "preflight-qualification.sqlite3")
    database.initialize()
    companies = sorted(set(histories) - {"SPY"})
    database.upsert_assets(
        [
            TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=True)
            for symbol in companies
        ]
    )
    for index, symbol in enumerate(companies, start=1):
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    database.upsert_bars(
        [_bar(symbol, session) for symbol, sessions in histories.items() for session in sessions]
    )
    for symbol in conflicts:
        database.set_sync_value(
            "sec_identity_conflicts",
            symbol,
            {"symbol": symbol, "status": "unresolved", "source": "fixture"},
        )
    return database


def _mark_provider_range_verified(
    database: Database,
    symbols: tuple[str, ...],
    first: date,
    end: date,
) -> None:
    for symbol in symbols:
        database.set_sync_value(
            "daily_history_coverage",
            symbol,
            {
                "start": datetime.combine(first, datetime.min.time(), tzinfo=UTC).isoformat(),
                "end_exclusive": datetime.combine(
                    end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ).isoformat(),
                "feed": "iex",
                "adjustment": "all",
            },
        )


def _successful_preparation(monkeypatch, requirements=()):
    calls = []

    def prepare(*args, **kwargs):
        calls.append(kwargs["comparison_kind"])
        return SimpleNamespace(intraday_requirements=tuple(requirements))

    monkeypatch.setattr(preflight_module, "prepare_strategy_comparison", prepare)
    return calls


def _build(database: Database, monkeypatch):
    calls = _successful_preparation(monkeypatch)
    report, candidates = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )
    return report, candidates, calls


def test_perfect_daily_universe_is_ready_and_complete_zero_is_unambiguous(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    database = _database(tmp_path, {"AAA": sessions, "SPY": sessions})

    report, candidates, calls = _build(database, monkeypatch)

    assert report["daily_ready"] is True
    assert report["daily_global_readiness"]["ready"] is True
    assert report["candidate_discovery"] == {
        "status": "COMPLETE",
        "candidate_symbols": [],
        "candidate_sessions": 0,
        "discovery_error": None,
    }
    assert candidates["discovery_complete"] is True
    assert candidates["candidate_symbols"] == candidates["candidate_sessions"] == []
    assert calls == [StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID]
    assert report["recommended_manual_sync_daily_history_command"] is None


def test_later_ipo_edge_is_diagnostic_and_does_not_block_discovery(tmp_path, monkeypatch) -> None:
    sessions = _sessions()
    database = _database(
        tmp_path,
        {"AAA": sessions, "IPO": sessions[-20:], "SPY": sessions},
    )

    report, _, calls = _build(database, monkeypatch)

    diagnostics = report["daily_symbol_diagnostics"]
    assert report["daily_ready"] is True
    assert calls
    assert diagnostics["symbols_rejected_initially_for_insufficient_history"] == 1
    assert diagnostics["symbols_with_edge_or_lifecycle_gaps"] == 1
    assert diagnostics["edge_or_lifecycle_missing_sessions"] > 0
    assert diagnostics["internal_missing_sessions"] == 0


def test_irrelevant_internal_gap_remains_diagnostic_without_global_kill_switch(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    gap_index = len(sessions) // 2
    gapped = sessions[:gap_index] + sessions[gap_index + 1 :]
    database = _database(
        tmp_path,
        {"AAA": sessions, "GAP": gapped, "SPY": sessions},
    )
    _mark_provider_range_verified(database, ("GAP",), sessions[0], END)

    report, _, calls = _build(database, monkeypatch)

    diagnostics = report["daily_symbol_diagnostics"]
    assert report["daily_ready"] is True
    assert calls
    assert diagnostics["symbols_with_internal_gaps"] == 1
    assert diagnostics["internal_missing_sessions"] == 1
    assert diagnostics["coverage_metadata_mismatches"] == 1
    assert report["recommended_manual_sync_daily_history_command"] is None


def test_production_screen_still_rejects_299_bar_symbol() -> None:
    config = _config()
    snapshot = UniverseSnapshot(
        symbol="AAA",
        latest_price=Decimal("100"),
        average_price_20d=Decimal("100"),
        average_volume_20d=Decimal("1000000"),
        shares_outstanding=Decimal("100000000"),
        sic="3571",
    )
    screener = Screener(SimpleNamespace(), config)

    assert "insufficient_market_history" in screener._universe_exclusions(snapshot, 299)
    assert "insufficient_market_history" not in screener._universe_exclusions(snapshot, 300)


def test_missing_spy_warmup_blocks_discovery_and_recommends_daily_sync(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    missing = len(sessions) // 2
    database = _database(
        tmp_path,
        {"AAA": sessions, "SPY": sessions[:missing] + sessions[missing + 1 :]},
    )
    monkeypatch.setattr(
        preflight_module,
        "prepare_strategy_comparison",
        lambda *args, **kwargs: pytest.fail("preparation must not run without SPY warmup"),
    )

    report, candidates = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )

    assert report["daily_ready"] is False
    assert report["daily_global_readiness"]["benchmark_warmup_complete"] is False
    assert "SPY benchmark warmup" in report["daily_global_readiness"]["failure_reasons"][0]
    assert report["candidate_discovery"]["status"] == "NOT_COMPLETE"
    assert candidates["discovery_complete"] is False
    assert "sync-daily-history" in report["recommended_manual_sync_daily_history_command"]


def test_requested_period_end_missing_blocks_preparation(tmp_path, monkeypatch) -> None:
    sessions = _sessions()
    database = _database(tmp_path, {"AAA": sessions[:-1], "SPY": sessions[:-1]})
    monkeypatch.setattr(
        preflight_module,
        "prepare_strategy_comparison",
        lambda *args, **kwargs: pytest.fail("preparation must not run without the final session"),
    )

    report, _ = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )

    assert report["daily_ready"] is False
    assert report["daily_global_readiness"]["requested_period_end_present"] is False
    assert "required final XNYS session" in report["daily_global_readiness"]["failure_reasons"][-1]
    assert "sync-daily-history" in report["recommended_manual_sync_daily_history_command"]


def test_identity_conflict_is_excluded_by_shared_operational_guard(tmp_path, monkeypatch) -> None:
    sessions = _sessions()
    database = _database(
        tmp_path,
        {"AAA": sessions, "CONFLICTED": (), "SPY": sessions},
        conflicts=("CONFLICTED",),
    )

    report, _, calls = _build(database, monkeypatch)

    diagnostics = report["daily_symbol_diagnostics"]
    assert report["daily_ready"] is True
    assert calls
    assert diagnostics["symbols_considered"] == 3
    assert diagnostics["identity_conflicts_excluded"] == 1
    assert diagnostics["identity_conflict_symbols"] == ["CONFLICTED"]
    assert diagnostics["qualification_symbol_count_after_exclusions"] == 2
    assert report["daily_qualification"]["symbols_checked"] == 2


def test_conflict_selection_is_api_driven_and_contains_no_ticker_exceptions() -> None:
    preflight_source = inspect.getsource(preflight_module.build_compare_preflight)
    qualification_source = inspect.getsource(qualify_historical_screen_start)
    sync_source = inspect.getsource(DataSynchronizer._sync_daily_history)
    combined = preflight_source + qualification_source + sync_source

    assert "EQR" not in combined
    assert "PARA" not in combined
    assert "unresolved_sec_identity_conflict_symbols" in preflight_source
    assert "unresolved_sec_identity_conflict_symbols" in qualification_source
    assert "unresolved_sec_identity_conflict_symbols" in sync_source


def test_verified_provider_range_with_only_lifecycle_absence_has_no_resync_advice(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    database = _database(tmp_path, {"IPO": sessions[-20:], "SPY": sessions})
    _mark_provider_range_verified(database, ("IPO", "SPY"), sessions[0], END)
    monkeypatch.setattr(
        preflight_module,
        "prepare_strategy_comparison",
        lambda *args, **kwargs: pytest.fail("no screenable member means no preparation"),
    )

    report, _ = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )

    assert report["daily_ready"] is False
    assert report["daily_symbol_diagnostics"]["coverage_metadata_mismatches"] == 1
    assert report["recommended_manual_sync_daily_history_command"] is None
    assert "already verified" in report["recommended_manual_sync_daily_history_reason"]


def test_preparation_value_error_is_preserved_without_masking_global_readiness(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    database = _database(tmp_path, {"AAA": sessions, "SPY": sessions})
    monkeypatch.setattr(
        preflight_module,
        "prepare_strategy_comparison",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("precise fixture failure")),
    )

    report, candidates = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )

    assert report["daily_ready"] is True
    assert report["candidate_discovery"]["status"] == "NOT_COMPLETE"
    assert report["candidate_discovery"]["discovery_error"] == "precise fixture failure"
    assert candidates["discovery_complete"] is False
    assert candidates["discovery_error"] == "precise fixture failure"
    assert report["recommended_manual_sync_daily_history_command"] is None


def test_candidate_report_and_intraday_recommendation_are_candidate_driven(
    tmp_path, monkeypatch
) -> None:
    sessions = _sessions()
    database = _database(tmp_path, {"AAA": sessions, "SPY": sessions})
    requirement = SimpleNamespace(
        candidate_execution_sessions=(("AAA", END),),
        first_execution_sessions=(("AAA", END),),
        comparison_sessions=(END,),
        timeframe=BarTimeframe.MINUTES_15,
    )
    _successful_preparation(monkeypatch, (requirement,))
    intraday = preflight_module._empty_intraday_preflight(_config()) | {
        "candidate_symbols": ["AAA"],
        "candidate_symbol_count": 1,
        "candidate_sessions": 1,
        "required_sessions": [
            {
                "symbol": "AAA",
                "session": END.isoformat(),
                "timeframe": "15m",
                "candidate_paths": [
                    "F0/C-intraday-dynamic",
                    "F3/C-intraday-thesis-recovery",
                    "F5/C-intraday-first-hour-pullback-f0-management",
                ],
                "requirement_type": "candidate_session",
            }
        ],
        "intraday_ready": False,
    }
    monkeypatch.setattr(preflight_module, "_intraday_preflight", lambda *args: intraday)
    report, candidates = preflight_module.build_compare_preflight(
        database,
        _config(),
        START,
        END,
        comparison_kind=StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID,
    )

    paths = preflight_module.export_compare_preflight(
        report,
        candidates,
        tmp_path / "reports",
        stem="candidate-driven",
    )
    exported = json.loads(paths["preflight"].read_text(encoding="utf-8"))
    exported_candidates = json.loads(paths["intraday_candidates"].read_text(encoding="utf-8"))
    command = exported["recommended_manual_sync_intraday_command"]

    assert exported_candidates["discovery_complete"] is True
    assert exported_candidates["candidate_discovery_status"] == "COMPLETE"
    assert exported_candidates["candidate_symbols"] == [{"symbol": "AAA"}]
    assert exported_candidates["candidate_sessions"] == [
        {"symbol": "AAA", "execution_session": END.isoformat()}
    ]
    assert "sync-intraday" in command
    assert "--timeframes 15m" in command
    assert f"--candidates-report {paths['intraday_candidates']}" in command
    assert "--candidate-gaps-only" in command
    assert "--output-stem candidate-driven_intraday_remediation" in command
    assert "--universe all" not in command
    assert exported_candidates["required_sessions"][0]["candidate_paths"] == [
        "F0/C-intraday-dynamic",
        "F3/C-intraday-thesis-recovery",
        "F5/C-intraday-first-hour-pullback-f0-management",
    ]
    assert exported_candidates["potential_position_ranges"] == [
        {
            "symbol": "AAA",
            "first_execution_session": END.isoformat(),
            "last_potential_session": END.isoformat(),
            "timeframe": "15m",
            "candidate_paths": [
                "F0/C-intraday-dynamic",
                "F3/C-intraday-thesis-recovery",
                "F5/C-intraday-first-hour-pullback-f0-management",
            ],
        }
    ]


def test_intraday_preflight_qualifies_every_potential_holding_session(tmp_path) -> None:
    sessions = tuple(trading_sessions_between(date(2025, 5, 1), date(2025, 5, 5)))
    database = Database(tmp_path / "potential-holding.sqlite3")
    database.initialize()
    opening = datetime.combine(sessions[0], datetime.min.time(), tzinfo=UTC)
    requirement = IntradayPrefetchRequirement(
        timeframe=BarTimeframe.MINUTES_15,
        variants=(),
        symbols=("AAA",),
        first_execution_sessions=(("AAA", sessions[0]),),
        candidate_execution_sessions=(("AAA", sessions[0]),),
        comparison_sessions=sessions,
        requested_start=opening - timedelta(days=2),
        requested_end=datetime.combine(
            sessions[-1] + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        ),
        warmup_bars=0,
        extended_hours=False,
    )
    preparation = SimpleNamespace(intraday_requirements=(requirement,))

    report = preflight_module._intraday_preflight(
        database,
        _config(),
        preparation,
        ["F0/C", "F3/C", "F5/C"],
        sessions[0],
        sessions[-1],
    )

    assert report["candidate_path_qualification"]["candidate_symbol_sessions_required"] == len(
        sessions
    )
    assert [item["requirement_type"] for item in report["required_sessions"]] == [
        "candidate_session",
        *("potential_open_position_session" for _ in sessions[1:]),
    ]
    assert report["required_local_missing_count"] == len(sessions)
    assert [item["strategy"] for item in report["not_required_for_candidate_path"]] == [
        "C/configured",
        "D/configured",
        "E/configured",
        "F/configured",
    ]


def test_preflight_family_is_exact_and_build_path_does_not_run_backtest(monkeypatch) -> None:
    kind = StrategyComparisonKind.RESEARCH_INTRADAY_HYBRID
    labels = [comparison_strategy_label(kind, *run) for run in research_family_runs(kind)]

    assert labels == [
        "F0/C-intraday-dynamic",
        "F3/C-intraday-thesis-recovery",
        "F5/C-intraday-first-hour-pullback-f0-management",
    ]
    monkeypatch.setattr(
        BacktestEngine,
        "run",
        lambda *args, **kwargs: pytest.fail("preflight must not execute BacktestEngine.run"),
    )
    assert "BacktestEngine" not in inspect.getsource(preflight_module.build_compare_preflight)
