"""Frozen economics and opt-in isolation, using only small local fixtures."""

import json
import socket
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from trading_system import cli
from trading_system.ai.export import build_ai_candidate_export
from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.research_registry import (
    F_ISOLATED_RESEARCH_FAMILIES,
    FROZEN_CHAMPION_F,
    RESEARCH_FAMILY_STATUS,
    ResearchStatus,
    validate_research_registry,
)
from trading_system.champion import champion_ranking, run_champion_backtest, screen_champion
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import PositionManagementPreset, StrategyVariant
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot
from trading_system.strategy.reporting import export_report, load_report


def forbidden(*args, **kwargs):
    raise AssertionError("Champion must remain local and free of research overlays")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)


@pytest.fixture
def config():
    return load_settings().strategy.model_copy(deep=True)


def record(symbol="AAA", *, atr=2, quality=90, valuation=90):
    def score(name, value):
        return ScoreBreakdown(name=name, score=value, factors=(), available_factor_count=1)

    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=date(2024, 1, 2),
        # F must not inherit the legacy screen's total-score rejection or recovery gate.
        eligible=False,
        exclusion_reasons=("total_score_below_minimum",),
        fundamentals=FundamentalMetrics(),
        scores=StockScores(
            quality=score("quality", quality),
            valuation=score("valuation", valuation),
            opportunity=score("opportunity", 5),
            timing=score("timing", 5),
            total=40,
        ),
        technical=TechnicalSnapshot(
            market_session=date(2024, 1, 2),
            price=100,
            sma20=95,
            sma50=90,
            sma200=80,
            sma20_rising=True,
            momentum126=0.2,
            momentum5=-0.1,
            rsi_recovery=False,
            relative_volume=0.5,
            drawdown_52w=-0.01,
            atr14=atr,
        ),
    )


class Screens:
    def __init__(self, signal, records):
        self.signal, self.records = signal, records

    def screen(self, session):
        records = self.records if session == self.signal else ()
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2024-01-02T21:00:00+00:00",
            analyzed_count=len(records),
            eligible_count=0,
            records=records,
        )


def bar(symbol, session, *, opening=100, high=101, low=99, close=100):
    return DailyBar(
        symbol=symbol,
        timestamp=datetime.combine(session, datetime.min.time(), UTC),
        open=Decimal(str(opening)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=10000,
    )


@pytest.fixture
def market(tmp_path):
    database = Database(tmp_path / "champion.sqlite3")
    database.initialize()
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 1, 23))
    database.upsert_bars(
        [bar(symbol, session) for symbol in ("AAA", "BBB", "SPY") for session in sessions]
    )
    return database, sessions


def run(market, config, *, atr=2):
    database, sessions = market
    return run_champion_backtest(
        database,
        config,
        sessions[0],
        sessions[-1],
        screen_source=Screens(sessions[0], (record("BBB", atr=atr), record(atr=atr))),
    )


def test_frozen_champion_daily_economics_and_research_hooks(market, config, monkeypatch):
    from trading_system.backtest import entry_quality, intraday_risk, lifecycle, market_regime

    monkeypatch.setattr(entry_quality, "opening_weakness_decision", forbidden)
    monkeypatch.setattr(lifecycle.LifecyclePositionManager, "__init__", forbidden)
    monkeypatch.setattr(intraday_risk.IntradayRiskOverlay, "__init__", forbidden)
    monkeypatch.setattr(market_regime.MarketRegimeCapacitySchedule, "__init__", forbidden)
    database, sessions = market
    original = config.model_dump()
    result = run(market, config)
    assert config.model_dump() == original
    assert result.strategy_variant is StrategyVariant.QUALITY_VALUE_MOMENTUM
    assert result.strategy_variant.value == "F"
    assert result.position_management_preset is PositionManagementPreset.CONFIGURED
    assert result.configuration["portfolio"]["max_positions"] == 1
    assert result.configuration["backtest"]["slippage_bps"] == 5
    assert result.configuration["backtest"]["commission_bps"] == 0
    assert len(result.positions) == 1
    position = result.positions[0]
    assert position.symbol == "AAA"  # F score tie: symbol order, same as the engine.
    assert position.signal_date == sessions[0]
    assert position.entry_date == sessions[1]
    assert position.entry_reference_price == 100
    assert position.entry_price == pytest.approx(100.05)
    assert position.initial_quantity == pytest.approx(100 * 0.01 / 4)
    assert position.holding_days == 10
    assert position.exit_date == sessions[10]
    assert position.exit_reference_price == 100
    assert position.exit_price == pytest.approx(99.95)
    assert position.exit_reason == "time_exit"  # Historical configured report spelling.
    assert position.transaction_cost == 0
    assert all(point.active_positions <= 1 for point in result.equity_curve)
    assert all(not trade.entry_delayed_from_open for trade in result.trades)
    baseline = BacktestEngine(
        database,
        config,
        screen_source=Screens(sessions[0], (record("BBB"), record())),
    ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
    assert result.positions == baseline.positions
    assert result.trades == baseline.trades
    assert result.equity_curve == baseline.equity_curve


@pytest.mark.parametrize(
    "atr,opening,high,low,reason,reference",
    [
        (2, 90, 120, 89, "stop_loss", 90),  # Existing-position opening gap precedes target.
        (2, 100, 120, 90, "stop_loss", 96.05),  # Ambiguous Daily range: stop first.
        (20, 100, 101, 89, "stop_loss", 100.05 * 0.9),  # ATR distance capped at 10%.
        (2, 100, 120, 99, "profit_target", 100.05 * 1.12),
        (2, 120, 121, 90, "profit_target", 120),  # Known opening target before later low.
    ],
)
def test_configured_gap_stop_target_precedence(
    market,
    config,
    atr,
    opening,
    high,
    low,
    reason,
    reference,
):
    database, sessions = market
    database.upsert_bars(
        [
            bar("AAA", sessions[2], opening=opening, high=high, low=low),
        ]
    )
    position = run(market, config, atr=atr).positions[0]
    assert position.exit_date == sessions[2]
    assert position.exit_reason == reason
    assert position.exit_reference_price == pytest.approx(reference)
    assert position.exit_price == pytest.approx(reference * 0.9995)


def test_target_precedes_tenth_session_close_and_is_not_deferred(market, config):
    database, sessions = market
    database.upsert_bars([bar("AAA", sessions[10], high=115)])
    position = run(market, config).positions[0]
    assert position.holding_days == 10
    assert position.exit_reason == "profit_target"
    assert position.exit_reference_price == pytest.approx(100.05 * 1.12)


def test_execution_session_future_close_does_not_change_entry(market, config):
    database, sessions = market
    baseline = run(market, config).positions[0]
    database.upsert_bars([bar("AAA", sessions[1], high=110, close=109)])
    changed = run(market, config).positions[0]
    assert (changed.entry_date, changed.entry_price, changed.initial_quantity) == (
        baseline.entry_date,
        baseline.entry_price,
        baseline.initial_quantity,
    )


@pytest.fixture
def missing_tenth_session_bar(market, config):
    """Remove only the held symbol's bar after verifying the nominal day-10 exit."""
    database, sessions = market
    baseline = run(market, config).positions[0]
    assert baseline.symbol == "AAA"
    assert baseline.entry_date == sessions[1]
    assert baseline.exit_date == sessions[10]
    assert baseline.holding_days == 10
    assert baseline.exit_reason == "time_exit"
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol=? AND timestamp LIKE ?",
            ("AAA", f"{sessions[10]}%"),
        )
    return market


def test_missing_daily_bar_preserves_historical_compatibility_behavior(
    missing_tenth_session_bar,
    config,
):
    """HISTORICAL_COMPATIBILITY_BEHAVIOR; not the future paper/shadow data policy."""
    database, sessions = missing_tenth_session_bar
    result = BacktestEngine(
        database,
        config,
        screen_source=Screens(sessions[0], (record("BBB"), record())),
        require_complete_daily_position_bars=False,
    ).run(
        sessions[0],
        sessions[-1],
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
    )
    assert len(result.positions) == 1
    position = result.positions[0]
    assert position.entry_date == sessions[1]
    assert (
        next(point for point in result.equity_curve if point.date == sessions[10]).active_positions
        == 1
    )
    assert not any(trade.exit_date == sessions[10] for trade in result.trades)
    assert position.exit_date == sessions[11]
    assert position.holding_days == 11
    assert position.exit_reason == "time_exit"
    assert position.exit_reference_price == 100


def test_missing_daily_bar_strict_mode_fails_closed(missing_tenth_session_bar, config):
    database, sessions = missing_tenth_session_bar
    with pytest.raises(
        ValueError,
        match=f"DAILY_POSITION_DATA_UNAVAILABLE on {sessions[10]}: AAA",
    ):
        BacktestEngine(
            database,
            config,
            screen_source=Screens(sessions[0], (record("BBB"), record())),
            require_complete_daily_position_bars=True,
        ).run(
            sessions[0],
            sessions[-1],
            variant=FROZEN_CHAMPION_F.variant,
            preset=FROZEN_CHAMPION_F.preset,
        )


def test_champion_wrapper_preserves_historical_missing_daily_bar_semantics(
    missing_tenth_session_bar,
    config,
):
    """Historical/research wrapper only; CC-01 remains a PRE_PAPER_BLOCKER."""
    database, sessions = missing_tenth_session_bar
    historical = BacktestEngine(
        database,
        config,
        screen_source=Screens(sessions[0], (record("BBB"), record())),
        require_complete_daily_position_bars=False,
    ).run(
        sessions[0],
        sessions[-1],
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
    )
    champion = run_champion_backtest(
        database,
        config,
        sessions[0],
        sessions[-1],
        screen_source=Screens(sessions[0], (record("BBB"), record())),
    )
    assert champion.positions[0].exit_date == sessions[11]
    assert champion.positions == historical.positions
    assert champion.trades == historical.trades
    assert champion.equity_curve == historical.equity_curve
    assert champion.configuration == historical.configuration


@pytest.mark.parametrize(
    "path,value",
    [
        ("portfolio.max_positions", 2),
        ("portfolio.max_position_pct", 0.5),
        ("risk.risk_per_trade", 0.02),
        ("risk.atr_stop_multiple", 3),
        ("risk.max_stop_loss_pct", 0.2),
        ("backtest.profit_target_pct", 0.2),
        ("backtest.slippage_bps", 10),
        ("backtest.commission_bps", 1),
        ("backtest.max_holding_days", 20),
        ("position_management.max_hold.days", 20),
        ("position_management.max_hold.mode", "review"),
        ("position_management.bar_timeframe", BarTimeframe.MINUTES_15),
        ("position_management.stop_loss.percent", 0.05),
        ("position_management.take_profit.enabled", False),
        ("position_management.atr_trailing_stop.atr_period", 20),
        ("position_management.reentry.cooldown_days", 2),
        ("position_management.reentry.enabled", False),
        *[
            (f"position_management.{name}.enabled", True)
            for name in (
                "trailing_stop",
                "atr_trailing_stop",
                "signal_decay",
                "partial_take_profit",
                "portfolio_rotation",
                "profit_lock",
            )
        ],
    ],
)
def test_champion_refuses_execution_drift_before_reading_data(config, path, value):
    parent = config
    *sections, field = path.split(".")
    for section in sections:
        parent = getattr(parent, section)
    setattr(parent, field, value)
    before = config.model_dump()
    with pytest.raises(ValueError, match="Frozen champion requires"):
        run_champion_backtest(None, config, date(2024, 1, 2), date(2024, 1, 3))
    assert config.model_dump() == before


def test_champion_ranking_screen_and_ai_share_f_gates_and_order(config, tmp_path):
    report = Screens(date(2024, 1, 2), (record("BBB"), record(), record("BAD", quality=20))).screen(
        date(2024, 1, 2)
    )
    original = report.model_dump()
    ranked = champion_ranking(report, config)
    assert report.model_dump() == original
    assert ranked.strategy_label == "F/configured/C1"
    assert [(item.symbol, item.rank) for item in ranked.records if item.eligible] == [
        ("AAA", 1),
        ("BBB", 2),
    ]
    assert ranked.records[0].scores.total == 90
    assert ranked.records[-1].exclusion_reasons == ("quality_threshold",)
    _, path = export_report(ranked, tmp_path)
    assert load_report(path) == ranked
    payload = build_ai_candidate_export(ranked)
    assert payload.strategy.name == "F/configured/C1"
    assert [(item.symbol, item.quant_score) for item in payload.candidates] == [
        ("AAA", 90),
        ("BBB", 90),
    ]


def test_screen_champion_disables_current_snapshots(config, monkeypatch):
    def screen(self, session, *, use_market_snapshots):
        assert use_market_snapshots is False
        return Screens(session, (record(),)).screen(session)

    monkeypatch.setattr("trading_system.champion.Screener.run", screen)
    assert screen_champion(None, config, date(2024, 1, 2)).eligible_count == 1


def test_registry_preserves_exact_rejected_family_ids_and_legacy_roles():
    validate_research_registry()
    assert set(ResearchStatus) == {
        ResearchStatus.ACTIVE,
        ResearchStatus.CHAMPION,
        ResearchStatus.REJECTED,
        ResearchStatus.ARCHIVED,
    }
    assert RESEARCH_FAMILY_STATUS["F/configured/C1"] is ResearchStatus.CHAMPION
    rejected = {
        name for name, status in RESEARCH_FAMILY_STATUS.items() if status is ResearchStatus.REJECTED
    }
    assert rejected == {
        "research-orb-v1",
        "research-intraday-reversal-v1",
        "research-market-intraday-momentum-v1",
        "research-f-capacity",
        "research-f-regime-capacity",
        "research-f-lifecycle-v2",
        "research-f-intraday-entry-quality",
        "research-f-intraday-risk-v1",
        "research-f-lifecycle-l5-forward-v1",
    }
    assert set(F_ISOLATED_RESEARCH_FAMILIES) <= rejected
    with pytest.raises(TypeError):
        RESEARCH_FAMILY_STATUS["research-f-capacity"] = ResearchStatus.ACTIVE


def test_generic_cli_defaults_preserved_and_champion_conflicts_refused():
    args = cli._parser().parse_args(["backtest", "--start", "2024-01-02", "--end", "2024-01-03"])
    assert (args.variant, args.strategy, args.champion) == ("C", "configured", False)
    for option in ("--variant=F", "--strategy=configured", "--var=F"):
        with pytest.raises(SystemExit) as error:
            cli.main(
                ["backtest", "--champion", "--start", "2024-01-02", "--end", "2024-01-03", option]
            )
        assert error.value.code == 2


def test_fresh_champion_cli_import_and_execution_never_load_research(market, tmp_path):
    database, sessions = market
    code = """
import importlib.abc
import socket
import sys
blocked = {
    "capacity_validation", "regime_capacity_validation", "lifecycle_validation",
    "lifecycle_daily_preflight", "l5_forward_validation", "intraday_risk_validation",
    "validation", "peer_context", "lifecycle_diagnostics", "intraday_diagnostics",
    "entry_quality", "intraday_risk", "lifecycle", "market_regime", "first_hour_pullback",
    "intraday_isolation", "intraday_next", "intraday_hybrid", "preflight",
    "orb_v1", "orb_v1_data", "orb_v1_manifest", "orb_v1_research",
    "intraday_reversal_v1", "intraday_reversal_v1_data", "intraday_reversal_v1_manifest",
    "intraday_reversal_v1_research", "liquid_universe", "native_candidate_data",
    "market_intraday_momentum_v1", "market_intraday_momentum_v1_data",
    "market_intraday_momentum_v1_manifest", "market_intraday_momentum_v1_research",
}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith("trading_system.backtest.")
                and fullname.rsplit(".", 1)[-1] in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
def forbidden(*args, **kwargs):
    raise AssertionError("network")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
import requests
requests.Session.request = forbidden
from trading_system import cli
from trading_system.champion import run_champion_backtest, screen_champion
from trading_system.config import load_settings
from trading_system.data.database import Database
from datetime import date
db = Database(sys.argv[1])
cfg = load_settings().strategy
cli._parser().parse_args(["screen", "--champion"])
run_champion_backtest(db, cfg, date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]))
screen_champion(db, cfg, date.fromisoformat(sys.argv[3]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(database.path), str(sessions[0]), str(sessions[-1])],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_champion_cli_screen_export_and_backtest_are_local(market, config, tmp_path, monkeypatch):
    database, sessions = market
    config.storage.database_path = database.path
    config.storage.reports_path = tmp_path / "reports"
    settings = load_settings().model_copy(update={"strategy": config})
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)

    def screen(self, session, *, use_market_snapshots):
        assert use_market_snapshots is False
        return Screens(session, (record(),)).screen(session)

    monkeypatch.setattr("trading_system.champion.Screener.run", screen)
    assert cli.main(["screen", "--champion", "--as-of", str(sessions[0])]) == 0
    screen_path = config.storage.reports_path / "champion" / f"screen_{sessions[0]}.json"
    assert load_report(screen_path).strategy_label == "F/configured/C1"
    output = tmp_path / "ai.json"
    assert (
        cli.main(["export-ai", "--champion", "--as-of", str(sessions[0]), "--output", str(output)])
        == 0
    )
    assert json.loads(output.read_text())["strategy"]["name"] == "F/configured/C1"
    assert (
        cli.main(
            [
                "backtest",
                "--champion",
                "--start",
                str(sessions[0]),
                "--end",
                str(sessions[-1]),
                "--output-stem",
                "fixture_champion",
            ]
        )
        == 0
    )
    result = json.loads((config.storage.reports_path / "fixture_champion.json").read_text())
    assert result["strategy_variant"] == "F"
    assert result["position_management_preset"] == "configured"
    assert result["configuration"]["portfolio"]["max_positions"] == 1
