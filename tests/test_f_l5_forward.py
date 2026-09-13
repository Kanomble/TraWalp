"""Small offline forward fixtures; canonical lifecycle code is the independent oracle."""

import csv
import json
import socket
from datetime import date
from types import SimpleNamespace

import pytest
import requests
import test_f_lifecycle_v2 as fixtures

from trading_system import cli
from trading_system.backtest import l5_forward_validation as research
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.lifecycle import (
    F_LIFECYCLE_VARIANTS,
    LifecyclePositionManager,
    PeerTrendState,
    TrendHealthState,
    lifecycle_strategy_config,
)
from trading_system.backtest.peer_context import TechnicalPeerContextProvider
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset

config = fixtures.config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Forward validation must never access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def case(tmp_path, config, monkeypatch):
    database = Database(tmp_path / "forward.sqlite3")
    database.initialize()
    sessions = trading_sessions_between(date(2024, 8, 13), date(2024, 9, 25))
    history = trading_sessions_between(date(2024, 6, 3), sessions[-1])
    sics = {
        "AAA": "2834",
        "BBB": "2834",
        "CCC": "2834",
        "DDD": "2834",
        "EAA": "2851",
        "EBB": "2851",
        "ECC": "2852",
        "EDD": "2852",
        "FAA": "7372",
        "FBB": "7372",
        "FCC": "7372",
        "FDD": "7372",
        "GAA": "7399",
        "HAA": "123",
        "MISSING": None,
        "MALFORMED": "bad",
        "LATE": "2834",
        "SPY": "2834",
    }
    for index, (symbol, sic) in enumerate(sics.items(), 1):
        database.upsert_company(
            CompanyIdentity(cik=str(index).zfill(10), symbol=symbol, name=symbol, sic=sic)
        )
        database.upsert_assets(
            [TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=True)]
        )
        database.upsert_bars(
            [
                fixtures.bar(symbol, session, 100 + i / 100)
                for i, session in enumerate(history if symbol != "LATE" else sessions[5:])
            ]
        )
    monkeypatch.setattr(
        research, "_qualify_daily_research", lambda *args: {"ready": True, "failure_reasons": []}
    )
    # A read-only validation must not initialize or migrate the fixture DB either.
    monkeypatch.setattr(
        database, "initialize", lambda: pytest.fail("Validation must not mutate the DB")
    )
    return database, config, sessions


def run(case):
    database, config, sessions = case
    return research.run_f_l5_forward(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=SimpleNamespace(sessions=sessions, screen_source=fixtures.Screens(sessions[0])),
    )


def reference(case, preset=None):
    database, config, sessions = case
    engine = BacktestEngine(
        database,
        lifecycle_strategy_config(config, preset) if preset else config,
        screen_source=fixtures.Screens(sessions[0]),
        lifecycle_preset=preset,
        lifecycle_context=TechnicalPeerContextProvider(database, config, sessions[-1]),
        require_complete_daily_position_bars=True,
    )
    result = engine.run(
        sessions[0],
        sessions[-1],
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
    )
    return result, engine


def test_frozen_control_and_existing_l5_are_exact_with_replay(case, tmp_path, monkeypatch):
    original = case[1].model_dump()
    expected_control, _ = reference(case)
    expected_l5, engine = reference(case, F_LIFECYCLE_VARIANTS[5])
    assert isinstance(engine.position_manager, LifecyclePositionManager)
    assert research.FORWARD_VARIANTS[1][1] is F_LIFECYCLE_VARIANTS[5]
    assert engine.position_manager.preset.defer_profit_target is False
    assert engine.position_manager.profit_events == []

    # The validator must not build the canonical per-session DataFrame or run
    # unrelated lifecycle reporting. Its inherited peer statistics are tested below.
    monkeypatch.setattr(
        "trading_system.backtest.peer_context.assign_peer_groups",
        lambda *a, **k: pytest.fail("Forward peer preparation constructed a DataFrame"),
    )
    bundle = run(case)
    assert list(bundle.results) == ["L0_FORWARD", "L5_FORWARD"]
    for label, expected in (("L0_FORWARD", expected_control), ("L5_FORWARD", expected_l5)):
        assert bundle.results[label].model_dump(exclude={"generated_at"}) == expected.model_dump(
            exclude={"generated_at"}
        )
    assert [p.holding_days for p in bundle.results["L0_FORWARD"].positions] == [10]
    assert [p.holding_days for p in bundle.results["L5_FORWARD"].positions] == [20]
    assert bundle.tables["lifecycle_events"] == [
        {**row, "strategy": "L5_FORWARD"} for row in engine.position_manager.trend_events
    ]
    assert not any(row["profit_target_deferred"] for row in bundle.tables["lifecycle_events"])
    assert case[1].model_dump() == original
    summary = bundle.summary
    assert summary["day10_positions_evaluated"] == summary["l5_extensions_granted"] == 1
    assert summary["l5_extensions_denied"] == 0
    assert summary["l5_extension_rate"] == 1.0
    assert summary["forward_holdout"] is True and summary["clean_oos"] is False
    assert summary["automatic_winner_selection"] is False
    assert summary["frozen_champion"] == "F/configured/C1"
    comparison = summary["comparison"]
    assert comparison["comparison"] == "L5_FORWARD - L0_FORWARD"
    assert set(comparison["metric_deltas"]) == {
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe",
        "sortino",
        "profit_factor",
        "expectancy",
        "win_rate",
        "positions",
        "average_holding_period",
        "exposure",
    }
    assert comparison["shared_entry_positions"] == 1
    assert (
        comparison["shared_entry_net_pnl_delta"]
        == expected_l5.positions[0].net_pnl - expected_control.positions[0].net_pnl
    )
    assert comparison["unique_path_net_pnl_delta"] == 0
    for name in (
        "candidate_discovery_seconds",
        "l0_forward_seconds",
        "l5_forward_seconds",
        "diagnostics_seconds",
    ):
        assert summary["performance"][name] >= 0
    assert summary["performance"]["peer_bar_batch_queries"] == 1
    assert summary["performance"]["peer_group_sessions_built"] == 20
    paths = research.export_f_l5_forward(bundle, tmp_path, stem="forward")
    assert set(paths) == {"summary.json", *(f"{name}.csv" for name in research.TABLE_NAMES)}
    assert json.loads(paths["summary.json"].read_text())["comparison"] == comparison
    with paths["lifecycle_events.csv"].open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 20
    with pytest.raises(FileExistsError):
        research.export_f_l5_forward(bundle, tmp_path, stem="forward")


@pytest.mark.parametrize(
    "trend,peers",
    [
        (TrendHealthState.WEAKENING, PeerTrendState.CONFIRMED),
        (TrendHealthState.HEALTHY, PeerTrendState.WEAK),
        (TrendHealthState.HEALTHY, PeerTrendState.UNAVAILABLE),
        (TrendHealthState.UNAVAILABLE, PeerTrendState.CONFIRMED),
    ],
)
def test_either_failed_condition_denies_extension(case, monkeypatch, trend, peers):
    monkeypatch.setattr(research._ForwardPeerContext, "trend", lambda *args: trend)
    monkeypatch.setattr(research._ForwardPeerContext, "peer_state", lambda *args: peers)
    bundle = run(case)
    assert bundle.results["L5_FORWARD"].positions[0].holding_days == 10
    assert bundle.results["L5_FORWARD"].positions[0].exit_reason == "time_exit"
    assert bundle.summary["l5_extensions_granted"] == 0
    assert bundle.summary["l5_extensions_denied"] == 1


def test_extension_not_revoked_and_hard_day20_exit(case, monkeypatch):
    day10 = case[2][10]
    monkeypatch.setattr(
        research._ForwardPeerContext,
        "trend",
        lambda self, symbol, session: (
            TrendHealthState.HEALTHY if session <= day10 else TrendHealthState.WEAKENING
        ),
    )
    monkeypatch.setattr(
        research._ForwardPeerContext,
        "peer_state",
        lambda self, symbol, session: (
            PeerTrendState.CONFIRMED if session <= day10 else PeerTrendState.WEAK
        ),
    )
    bundle = run(case)
    position = bundle.results["L5_FORWARD"].positions[0]
    assert position.holding_days == 20 and position.exit_reason == "time_exit"
    assert bundle.summary["l5_extensions_granted"] == 1
    assert all(
        row["holding_extended"]
        for row in bundle.tables["lifecycle_events"]
        if row["holding_day"] >= 10
    )


@pytest.mark.parametrize("kind,reason", [("stop", "stop_loss"), ("target", "profit_target")])
def test_canonical_stop_and_target_stay_active_during_extension(case, kind, reason):
    database, _, sessions = case
    kwargs = {"low": 1} if kind == "stop" else {"high": 1000}
    database.upsert_bars([fixtures.bar("AAA", sessions[12], 101, **kwargs)])
    expected, engine = reference(case, F_LIFECYCLE_VARIANTS[5])
    actual = run(case).results["L5_FORWARD"]
    assert actual.model_dump(exclude={"generated_at"}) == expected.model_dump(
        exclude={"generated_at"}
    )
    assert actual.positions[0].exit_reason == reason
    assert actual.positions[0].exit_date == sessions[12]
    assert engine.position_manager.profit_events == []


def test_batched_peer_context_exact_pit_and_no_lazy_sql(case, monkeypatch):
    database, config, sessions = case
    canonical = TechnicalPeerContextProvider(database, config, sessions[-1])
    observed = (sessions[0], sessions[19], sessions[-1])
    expected_groups = {s: canonical._groups(s) for s in observed}
    expected = {
        (c.symbol, s): (canonical.trend(c.symbol, s), canonical.context(c.symbol, s))
        for c in canonical.companies
        for s in observed
    }
    prepared = research._ForwardPeerContext(database, config, sessions[0], sessions[-1])
    assert prepared.bar_batch_queries == 1
    monkeypatch.setattr(
        database, "bars_available_as_of", lambda *a, **k: pytest.fail("Per-peer SQL")
    )
    monkeypatch.setattr(
        database, "read_only", lambda *a, **k: pytest.fail("SQL during peer decisions")
    )
    for session in observed:
        assert prepared._groups(session) == expected_groups[session]
        for company in prepared.companies:
            assert (
                prepared.trend(company.symbol, session),
                prepared.context(company.symbol, session),
            ) == expected[company.symbol, session]
    groups, members = prepared._groups(sessions[0])
    assert groups["AAA"] == "sic4:2834"
    assert groups["EAA"] == "sic3:285"
    assert groups["GAA"] == "sic2:73"
    assert "FAA" in members["sic2:73"]
    assert groups["HAA"] == "sic2:01"
    assert groups["MISSING"] is None and groups["MALFORMED"] is None
    assert "LATE" not in groups  # Future listing history cannot enter earlier peers.
    assert "LATE" in prepared._groups(sessions[-1])[0]
    prepared.technical.cache_clear()
    prepared.context.cache_clear()
    prepared._groups.cache_clear()
    # Future prices were loaded but cannot affect an earlier decision.
    prepared._histories["AAA"][-1] = prepared._histories["AAA"][-1].__class__(
        prepared._histories["AAA"][-1].timestamp, 9999, 1, 9999, 10000
    )
    assert prepared.context("AAA", sessions[0]) == expected["AAA", sessions[0]][1]


def test_cli_rejects_development_dates_before_any_work(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "run_f_l5_forward", lambda *args: pytest.fail("Runner reached before date guard")
    )
    monkeypatch.setattr(Database, "initialize", lambda *args: pytest.fail("Database mutation"))
    assert (
        cli.main(
            [
                "validate-f-l5-forward",
                "--start",
                "2024-08-12",
                "--end",
                "2026-08-12",
                "--output-stem",
                "forward",
            ]
        )
        == 1
    )
    assert "on or after 2024-08-13" in capsys.readouterr().err
    with pytest.raises(ValueError, match="2024-08-13"):
        research.run_f_l5_forward(None, None, date(2024, 8, 12), date(2026, 8, 12))
    with pytest.raises(ValueError, match="after end"):
        research.validate_forward_window(date(2024, 8, 14), date(2024, 8, 13))
    research.validate_forward_window(date(2024, 8, 13), date(2026, 8, 12))
