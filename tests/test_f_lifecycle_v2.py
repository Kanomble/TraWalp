"""Small deterministic lifecycle/entry experiments, never a historical research job."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest.engine import BacktestEngine
from trading_system.backtest.entry_quality import (
    F_INTRADAY_ENTRY_VARIANTS,
    EntryQualityStatus,
    next_executable_bar,
    opening_weakness_decision,
)
from trading_system.backtest.lifecycle import (
    F_LIFECYCLE_VARIANTS,
    LifecyclePositionManager,
    PeerTrendState,
    TrendHealthState,
    lifecycle_strategy_config,
    previous_session,
)
from trading_system.backtest.lifecycle_diagnostics import LifecycleDiagnostics, gap_observation
from trading_system.backtest.peer_context import TechnicalPeerContextProvider, peer_confirmation
from trading_system.backtest.position_manager import ExitReason, PositionAction, PositionState
from trading_system.backtest.presets import position_management_preset
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.cli import _parser
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import regular_session_bounds, trading_sessions_between
from trading_system.models.backtest import PositionManagementPreset
from trading_system.models.fundamentals import CompanyIdentity, FundamentalMetrics
from trading_system.models.market_data import BarTimeframe, DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def bar(symbol, session, price=100, *, high=None, low=None, opening=None):
    return DailyBar(
        symbol=symbol,
        timestamp=datetime.combine(session, datetime.min.time(), UTC),
        open=Decimal(str(price if opening is None else opening)),
        high=Decimal(str(price if high is None else high)),
        low=Decimal(str(price if low is None else low)),
        close=Decimal(str(price)),
        volume=10000,
    )


def record(symbol="AAA"):
    score = ScoreBreakdown(name="fixture", score=90, factors=(), available_factor_count=4)
    return ScreenRecord.model_construct(
        symbol=symbol,
        sic="2834",
        eligible=True,
        exclusion_reasons=(),
        scores=StockScores(
            quality=score, valuation=score, opportunity=score, timing=score, total=90
        ),
        fundamentals=FundamentalMetrics(),
        technical=TechnicalSnapshot(
            price=100,
            sma20=95,
            sma50=90,
            sma200=80,
            sma20_rising=True,
            momentum126=0.2,
            momentum5=0.01,
            drawdown_52w=-0.01,
            atr14=2,
        ),
    )


class Screens:
    def __init__(self, signal, records=None):
        self.signal = signal
        self.records = (record(),) if records is None else records

    def screen(self, session):
        records = self.records if session == self.signal else ()
        return ScreenReport(
            as_of=session,
            requested_as_of=session,
            effective_market_session=session,
            generated_at="2024-01-01T00:00:00+00:00",
            records=records,
            analyzed_count=len(records),
            eligible_count=len(records),
        )


class Context:
    def __init__(self, trend=TrendHealthState.HEALTHY, peers=PeerTrendState.CONFIRMED):
        self.health = trend
        self.peers = peers
        self.observed = []

    def trend(self, symbol, session):
        self.observed.append(session)
        return self.health

    def peer_state(self, symbol, session):
        return self.peers


@pytest.fixture
def config():
    load_settings.cache_clear()
    return load_settings().strategy


@pytest.fixture
def local_market(tmp_path):
    database = Database(tmp_path / "lifecycle.sqlite3")
    database.initialize()
    sessions = trading_sessions_between(date(2024, 1, 2), date(2024, 2, 23))
    database.upsert_bars(
        [
            bar(symbol, session, 100 + i / 100)
            for symbol in ("AAA", "SPY")
            for i, session in enumerate(sessions)
        ]
    )
    return database, sessions


def run(database, sessions, config, identity=None, context=None, **kwargs):
    return BacktestEngine(
        database,
        lifecycle_strategy_config(config, identity) if identity else config,
        screen_source=Screens(sessions[0]),
        lifecycle_preset=identity,
        lifecycle_context=context,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        **kwargs,
    ).run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)


@pytest.mark.parametrize("capacity_hook", [False, True])
def test_l0_exact_c1_regression(local_market, config, capacity_hook):
    database, sessions = local_market
    baseline = run(database, sessions, config)
    control = run(
        database,
        sessions,
        config,
        F_LIFECYCLE_VARIANTS[0],
        entry_capacity_provider=(lambda _: 1) if capacity_hook else None,
    )
    assert baseline.positions
    assert control.positions == baseline.positions
    assert control.trades == baseline.trades
    assert control.equity_curve == baseline.equity_curve
    assert control.metrics.total_return == baseline.metrics.total_return
    assert control.metrics.maximum_drawdown == baseline.metrics.maximum_drawdown
    assert control.configuration == baseline.configuration


@pytest.mark.parametrize(("index", "days"), [(1, 15), (2, 20), (3, 30)])
def test_fixed_holds_change_only_holding(local_market, config, index, days):
    original = config.model_dump()
    identity = F_LIFECYCLE_VARIANTS[index]
    changed = lifecycle_strategy_config(config, identity).model_dump()
    assert changed["position_management"]["max_hold"]["days"] == days
    changed["position_management"]["max_hold"]["days"] = original["position_management"][
        "max_hold"
    ]["days"]
    assert changed == original
    result = run(*local_market, config, identity)
    assert result.positions[0].holding_days == days
    assert result.positions[0].exit_reason == "max_hold"
    assert config.model_dump() == original


@pytest.mark.parametrize(
    ("index", "health", "peers", "days"),
    [
        (4, TrendHealthState.HEALTHY, PeerTrendState.UNAVAILABLE, 20),
        (4, TrendHealthState.WEAKENING, PeerTrendState.CONFIRMED, 10),
        (4, TrendHealthState.UNAVAILABLE, PeerTrendState.CONFIRMED, 10),
        (5, TrendHealthState.HEALTHY, PeerTrendState.CONFIRMED, 20),
        (5, TrendHealthState.HEALTHY, PeerTrendState.WEAK, 10),
        (5, TrendHealthState.HEALTHY, PeerTrendState.UNAVAILABLE, 10),
    ],
)
def test_conditional_extension(local_market, config, index, health, peers, days):
    result = run(*local_market, config, F_LIFECYCLE_VARIANTS[index], Context(health, peers))
    assert result.positions[0].holding_days == days


def position():
    return PositionState(
        symbol="AAA",
        position_id="P1",
        signal_date=date(2024, 1, 2),
        entry_date=date(2024, 1, 3),
        entry_reference_price=100,
        entry_price=100,
        quantity=1,
        initial_quantity=1,
        position_value=100,
        stop_price=96,
        target_price=112,
        entry_commission=0,
        initial_entry_commission=0,
        entry_slippage=0,
        quality_score=90,
        valuation_score=90,
        opportunity_score=90,
        timing_score=90,
        entry_score=90,
        sector="28",
        variant=FROZEN_CHAMPION_F.variant,
        last_price=100,
        holding_days=5,
    )


def manager(config, context, index=6):
    identity = F_LIFECYCLE_VARIANTS[index]
    configured = lifecycle_strategy_config(config, identity)
    management = position_management_preset(
        configured.position_management,
        PositionManagementPreset.CONFIGURED,
        legacy_max_holding_days=configured.backtest.max_holding_days,
    )
    result = LifecyclePositionManager(management, preset=identity, context=context)
    result.start_session(date(2024, 1, 9))
    return result


def test_conditional_does_not_exit_early_or_recheck_after_grant(config):
    ctx = Context(TrendHealthState.WEAKENING)
    mgr = manager(config, ctx, 4)
    p = position()
    assert mgr.evaluate_close(p, 101, current_score=90).action is PositionAction.HOLD
    p.holding_days = 10
    ctx.health = TrendHealthState.HEALTHY
    assert mgr.evaluate_close(p, 101, current_score=90).action is PositionAction.HOLD
    p.holding_days = 15
    ctx.health = TrendHealthState.WEAKENING
    assert mgr.evaluate_close(p, 101, current_score=90).action is PositionAction.HOLD
    p.holding_days = 20
    assert mgr.evaluate_close(p, 101, current_score=90).reason is ExitReason.MAX_HOLD


def test_target_deferral_prior_context_and_later_executable_exit(config):
    ctx = Context()
    mgr, p = manager(config, ctx), position()
    target = bar("AAA", date(2024, 1, 9), 111, high=113, low=99, opening=100)
    assert mgr.evaluate_intrabar(p, target).action is PositionAction.HOLD
    assert mgr.profit_events[0]["profit_target_deferred"] is True
    assert ctx.observed == [date(2024, 1, 8)]
    assert p.target_price == 112
    ctx.health = TrendHealthState.WEAKENING
    assert mgr.evaluate_close(p, 111, current_score=90).action is PositionAction.HOLD
    mgr.start_session(date(2024, 1, 10))
    decision = mgr.evaluate_open(p, bar("AAA", date(2024, 1, 10), 109))
    assert decision.reason is ExitReason.LIFECYCLE_TREND
    assert decision.reference_price == 109


@pytest.mark.parametrize(
    ("health", "peers"),
    [
        (TrendHealthState.WEAKENING, PeerTrendState.CONFIRMED),
        (TrendHealthState.HEALTHY, PeerTrendState.WEAK),
        (TrendHealthState.UNAVAILABLE, PeerTrendState.UNAVAILABLE),
    ],
)
def test_weak_target_uses_configured_exit(config, health, peers):
    mgr = manager(config, Context(health, peers))
    decision = mgr.evaluate_intrabar(
        position(), bar("AAA", date(2024, 1, 9), 112, high=113, low=99)
    )
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.reference_price == 112


def test_deferred_stops_and_hard_maximum_remain_active(config):
    mgr, p = manager(config, Context()), position()
    assert mgr.evaluate_intrabar(p, bar("AAA", date(2024, 1, 9), 112)).action is PositionAction.HOLD
    assert (
        mgr.evaluate_intrabar(p, bar("AAA", date(2024, 1, 10), 100, high=115, low=95)).reason
        is ExitReason.STOP_LOSS
    )
    assert mgr.evaluate_open(p, bar("AAA", date(2024, 1, 10), 90)).reference_price == 90
    p.holding_days = 20
    assert mgr.evaluate_close(p, 115, current_score=90).reason is ExitReason.MAX_HOLD


def test_stop_precedes_new_target_deferral(config):
    mgr = manager(config, Context())
    decision = mgr.evaluate_intrabar(
        position(), bar("AAA", date(2024, 1, 9), 100, high=115, low=95)
    )
    assert decision.reason is ExitReason.STOP_LOSS
    assert not mgr.deferred
    assert not mgr.profit_events


@pytest.mark.parametrize(
    ("count", "ratio", "expected"),
    [
        (3, 2 / 3, PeerTrendState.CONFIRMED),
        (4, 0.5, PeerTrendState.WEAK),
        (3, 0.0, PeerTrendState.WEAK),
        (2, 1.0, PeerTrendState.UNAVAILABLE),
    ],
)
def test_peer_confirmation_exact_boundary(count, ratio, expected):
    assert peer_confirmation(count, ratio) is expected


def test_pit_peers_trend_missing_history_and_correlations(tmp_path, config):
    db = Database(tmp_path / "peers.sqlite3")
    db.initialize()
    sessions = trading_sessions_between(date(2023, 9, 1), date(2024, 1, 31))
    for index, symbol in enumerate(("AAA", "BBB", "CCC", "DDD"), 1):
        db.upsert_company(
            CompanyIdentity(cik=str(index).zfill(10), symbol=symbol, name=symbol, sic="2834")
        )
        from trading_system.models.market_data import TradableAsset

        db.upsert_assets(
            [TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=True)]
        )
        db.upsert_bars(
            [bar(symbol, s, 90 + i / 10 + (i % 3) / 100) for i, s in enumerate(sessions)]
        )
    cutoff = sessions[-2]
    provider = TechnicalPeerContextProvider(db, config, sessions[-1])
    before = provider.context("AAA", cutoff)
    assert before.peer_count_valid == 3
    assert before.state is PeerTrendState.CONFIRMED
    assert before.peer_symbols == ("BBB", "CCC", "DDD")
    assert provider.trend("AAA", cutoff) is TrendHealthState.HEALTHY
    assert provider.trend("AAA", sessions[2]) is TrendHealthState.UNAVAILABLE
    assert provider.correlations("AAA", ["BBB"], cutoff)[
        "max_correlation_to_open_positions"
    ] == pytest.approx(1)
    assert provider.correlations("AAA", [], cutoff)["mean_correlation_to_open_positions"] is None
    db.upsert_bars([bar("BBB", sessions[-1], 1)])
    after = TechnicalPeerContextProvider(db, config, sessions[-1])
    assert after.context("AAA", cutoff) == before
    assert after.trend("BBB", sessions[-1]) is TrendHealthState.WEAKENING
    assert after.context("AAA", sessions[-1]) != before
    # Endpoint lookbacks must not fill missing observations.
    assert after.forward_bars("AAA", sessions[-2], 5) == []


def native_session(session, *, weak=False):
    opening, closing = regular_session_bounds(session)
    output = []
    for i in range(int((closing - opening).total_seconds() // 900)):
        close = 99 if weak and i == 1 else 100
        output.append(
            DailyBar(
                symbol="AAA",
                timeframe=BarTimeframe.MINUTES_15,
                timestamp=opening + timedelta(minutes=15 * i),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(98),
                close=Decimal(close),
                volume=100,
                vwap=Decimal(100),
            )
        )
    return output


def test_intraday_two_completed_bars_only_and_next_executable_open():
    session = date(2024, 1, 3)
    bars = native_session(session)
    decision = opening_weakness_decision(bars, session, 100)
    assert decision.status is EntryQualityStatus.PASSED
    altered = bars[:2] + [b.model_copy(update={"close": Decimal(1)}) for b in bars[2:]]
    assert opening_weakness_decision(altered, session, 100) == decision
    execution = next_executable_bar(bars, decision, session)
    assert execution == bars[2]
    assert execution.timestamp == bars[1].timestamp + timedelta(minutes=15)
    assert next_executable_bar(bars[:2] + bars[3:], decision, session) == bars[3]
    assert (
        opening_weakness_decision(native_session(session, weak=True), session, 100).status
        is EntryQualityStatus.VETO
    )
    assert (
        opening_weakness_decision(native_session(session, weak=True), session, 98).status
        is EntryQualityStatus.PASSED
    )


@pytest.mark.parametrize("missing", ["first", "second", "vwap", "volume", "previous_close"])
def test_intraday_unavailable_is_explicit(missing):
    session = date(2024, 1, 3)
    bars = native_session(session)
    previous = 100
    if missing in {"first", "second"}:
        del bars[0 if missing == "first" else 1]
    elif missing == "vwap":
        bars[1] = bars[1].model_copy(update={"vwap": None})
    elif missing == "volume":
        bars = [b.model_copy(update={"volume": 0}) for b in bars]
    else:
        previous = None
    assert (
        opening_weakness_decision(bars, session, previous).status is EntryQualityStatus.UNAVAILABLE
    )


def test_entry_engine_no_retroactive_daily_low_and_stop_still_active(local_market, config):
    db, sessions = local_market
    native = native_session(sessions[1])
    # Previous opening range would stop out a retrospective entry; it must be excluded.
    native[0] = native[0].model_copy(update={"low": Decimal(1)})
    db.upsert_bars(native)
    db.upsert_bars([bar("AAA", sessions[1], 100, high=101, low=1)])
    result = run(db, sessions, config, opening_weakness_veto=True)
    p = result.positions[0]
    assert p.entry_timestamp == native[2].timestamp
    assert p.entry_delayed_from_open
    assert p.holding_days == 10
    native[3] = native[3].model_copy(update={"low": Decimal(90)})
    db.upsert_bars(native)
    stopped = run(db, sessions, config, opening_weakness_veto=True)
    assert stopped.positions[0].holding_days == 1
    assert stopped.positions[0].exit_reason == "stop_loss"


def test_gap_calculation_and_no_atr_substitute():
    values = gap_observation(100, 104, 2)
    assert values["gap_return"] == pytest.approx(0.04)
    assert values["gap_in_ATR"] == 2
    assert gap_observation(100, 96, 2)["gap_in_ATR"] == -2
    assert gap_observation(100, 104, None)["gap_in_ATR"] is None


def test_fixed_family_cli_no_sweeps_or_champion_change():
    assert [p.research_id for p in F_LIFECYCLE_VARIANTS] == [f"F-LIFECYCLE-L{i}" for i in range(7)]
    assert [p.research_id for p in F_INTRADAY_ENTRY_VARIANTS] == [
        "F-INTRADAY-ENTRY-I0",
        "F-INTRADAY-ENTRY-I1",
    ]
    for command in (
        "validate-f-lifecycle-v2",
        "preflight-f-intraday-entry",
        "validate-f-intraday-entry",
    ):
        parsed = _parser().parse_args(
            [command, "--start", "2024-01-02", "--end", "2024-01-04", "--output-stem", "fixture"]
        )
        assert set(vars(parsed)) == {"command", "start", "end", "output_stem", "config", "verbose"}
    assert FROZEN_CHAMPION_F.label == "F/configured"


def test_candidate_observer_keeps_capacity_blocked_candidates(local_market, config):
    db, sessions = local_market
    context = TechnicalPeerContextProvider(db, config, sessions[-1])
    observer = LifecycleDiagnostics(context, config)
    report = Screens(sessions[0], (record("AAA"), record("BBB"))).screen(sessions[0])
    observer.observe_entry_context(report, {"AAA": position()}, sessions[1])
    assert len(observer.candidates) == 2
    assert [r["candidate_rank"] for r in observer.candidates.values()] == [1, 2]


def test_separate_research_hooks_reject_automatic_combination(local_market, config):
    with pytest.raises(ValueError, match="separately"):
        run(*local_market, config, F_LIFECYCLE_VARIANTS[0], opening_weakness_veto=True)


def test_previous_session_handles_weekends_and_holidays():
    assert previous_session(date(2024, 1, 16)) == date(2024, 1, 12)


def research_preparation(monkeypatch, local_market):
    from trading_system.backtest import lifecycle_validation as validation

    db, sessions = local_market
    preparation = SimpleNamespace(sessions=tuple(sessions), screen_source=Screens(sessions[0]))
    # Small deterministic screens replace SEC history; the actual engine/portfolio/reporting run.
    monkeypatch.setattr(validation, "_prepare", lambda *args: (preparation, {"ready": True}))
    return validation, db, sessions, preparation


def test_local_daily_runner_exact_variants_full_cost_reruns_and_reports(
    monkeypatch, local_market, config, tmp_path
):
    import csv
    import json
    import socket

    import requests

    validation, db, sessions, _ = research_preparation(monkeypatch, local_market)

    def forbidden(*args, **kwargs):
        raise AssertionError("Research must never access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    calls = []
    original = BacktestEngine.run

    def counted(self, *args, **kwargs):
        calls.append(
            (
                self.lifecycle_preset.research_id,
                self.config.backtest.slippage_bps,
                self.config.backtest.commission_bps,
                self.entry_capacity_provider,
            )
        )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BacktestEngine, "run", counted)
    original_config = config.model_dump()
    bundle = validation.run_f_lifecycle_v2(db, config, sessions[0], sessions[-1])
    assert len(calls) == 28
    assert all(capacity is None for _, _, _, capacity in calls)
    assert {(slippage, commission) for _, slippage, commission, _ in calls} == {
        (5, 0),
        (10, 0),
        (15, 0),
        (5, 5),
    }
    assert set(bundle.results) == {p.research_id for p in F_LIFECYCLE_VARIANTS}
    assert len(bundle.tables["cost_stress"]) == 28
    assert config.model_dump() == original_config
    paths = validation.export_f_lifecycle_research(bundle, tmp_path, stem="daily_fixture")
    payload = json.loads(paths["summary.json"].read_text())
    assert payload["period_classification"] == "DEVELOPMENT / RESEARCH"
    assert payload["clean_oos"] is False
    assert payload["automatic_winner_selection"] is False
    assert payload["historical_peer_membership_verified"] is False
    assert payload["portfolio_reruns"] == 28
    assert all(p.exists() for p in paths.values())
    for table in (
        "positions",
        "execution_legs",
        "monthly",
        "yearly",
        "chronological_subperiods",
        "holding_duration_analysis",
        "entry_gap_analysis",
        "peer_context",
        "peer_spillover",
        "correlation",
        "symbol_concentration",
    ):
        assert bundle.tables[table], table
    with paths["dynamic_profit_events.csv"].open(newline="") as source:
        headers = next(csv.reader(source))
    assert set(validation.DIAGNOSTIC_FIELDS["dynamic_profit_events"]) <= set(headers)
    assert len(bundle.tables["entry_gap_analysis"]) == 7
    assert all(row["position_result"] is not None for row in bundle.tables["entry_gap_analysis"])
    with pytest.raises(FileExistsError):
        validation.export_f_lifecycle_research(bundle, tmp_path, stem="daily_fixture")


def test_preflight_sync_compatibility_and_qualification_then_i0_i1(
    monkeypatch, local_market, config, tmp_path
):
    import json
    import socket

    from trading_system.data.intraday_remediation import candidate_requirements_from_report

    validation, db, sessions, _ = research_preparation(monkeypatch, local_market)

    def forbidden(*args, **kwargs):
        raise AssertionError("No network, provider sync or preflight backtest")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    original_run = BacktestEngine.run
    monkeypatch.setattr(BacktestEngine, "run", forbidden)
    report, requirements = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1]
    )
    assert report["backtest_executed"] is False
    assert report["intraday_qualified"] is False
    assert report["missing_symbol_sessions"][0]["status"] == "INTRADAY_UNAVAILABLE"
    paths = validation.export_f_intraday_entry_preflight(
        report, requirements, tmp_path, stem="entry_preflight"
    )
    parsed = candidate_requirements_from_report(
        json.loads(paths["intraday_candidates.json"].read_text()),
        start=sessions[0],
        end=sessions[-1],
        timeframes=(BarTimeframe.MINUTES_15,),
    )
    assert len(parsed) == 1
    assert parsed[0].symbol == "AAA"
    assert parsed[0].session == sessions[1]
    with pytest.raises(ValueError, match="INTRADAY_UNAVAILABLE"):
        validation.run_f_intraday_entry(db, config, sessions[0], sessions[-1])
    db.upsert_bars(native_session(sessions[1], weak=True))
    qualified, _ = validation.build_f_intraday_entry_preflight(
        db, config, sessions[0], sessions[-1]
    )
    assert qualified["intraday_qualified"] is True  # Veto is a valid research outcome.
    monkeypatch.setattr(BacktestEngine, "run", original_run)
    bundle = validation.run_f_intraday_entry(db, config, sessions[0], sessions[-1])
    assert len(bundle.results) == 2
    assert bundle.results["F-INTRADAY-ENTRY-I0"].positions
    assert not bundle.results["F-INTRADAY-ENTRY-I1"].positions
    assert bundle.tables["entry_quality_events"][0]["status"] == "OPENING_WEAKNESS_VETO"
    control = run(db, sessions, config)
    assert bundle.results["F-INTRADAY-ENTRY-I0"].positions == control.positions
    assert bundle.results["F-INTRADAY-ENTRY-I0"].equity_curve == control.equity_curve
    assert bundle.summary["portfolio_reruns"] == 2


def test_real_daily_qualification_fails_closed(local_market, config):
    from trading_system.backtest.lifecycle_validation import run_f_lifecycle_v2

    db, sessions = local_market
    with pytest.raises(ValueError, match="Daily qualification failed"):
        run_f_lifecycle_v2(db, config, sessions[0], sessions[-1])


def test_dynamic_engine_profit_diagnostics_link_eventual_exit(local_market, config):
    from trading_system.backtest.lifecycle_diagnostics import profit_event_row

    db, sessions = local_market
    db.upsert_bars([bar("AAA", sessions[4], 113, high=114, low=100, opening=100)])
    identity = F_LIFECYCLE_VARIANTS[6]
    engine = BacktestEngine(
        db,
        lifecycle_strategy_config(config, identity),
        screen_source=Screens(sessions[0]),
        lifecycle_preset=identity,
        lifecycle_context=Context(),
    )
    result = engine.run(sessions[0], sessions[-1], variant=FROZEN_CHAMPION_F.variant)
    p = result.positions[0]
    assert p.holding_days == 20
    assert len(engine.position_manager.profit_events) == 1
    provider = TechnicalPeerContextProvider(db, config, sessions[-1])
    row = profit_event_row(provider, result, engine.position_manager.profit_events[0], p)
    assert row["eventual_exit_date"] == p.exit_date.isoformat()
    assert row["final_return"] == p.position_return
    assert row["additional_return_after_deferral"] < 0
    assert row["MFE_after_original_target"] == 0
    assert row["MAE_after_original_target"] < 0


def test_pending_peer_exit_still_loses_to_gap_stop(config):
    ctx = Context()
    mgr, p = manager(config, ctx), position()
    mgr.evaluate_intrabar(p, bar("AAA", date(2024, 1, 9), 113))
    ctx.peers = PeerTrendState.WEAK
    mgr.evaluate_close(p, 113, current_score=90)
    assert mgr.pending_exits[p.position_id] is ExitReason.LIFECYCLE_PEERS
    decision = mgr.evaluate_open(p, bar("AAA", date(2024, 1, 10), 90))
    assert decision.reason is ExitReason.STOP_LOSS


def test_engine_missing_intraday_skips_explicitly(local_market, config):
    result = run(*local_market, config, opening_weakness_veto=True)
    assert not result.positions
    assert result.skipped_entries["INTRADAY_UNAVAILABLE"] == 1


def test_real_preparation_small_local_smoke_and_business_data_unchanged(tmp_path, config):
    from trading_system.backtest.lifecycle_validation import run_f_lifecycle_v2
    from trading_system.models.market_data import TradableAsset

    db = Database(tmp_path / "real_preparation.sqlite3")
    db.initialize()
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="2834"))
    db.upsert_assets([TradableAsset(symbol="AAA", name="AAA", tradable=True, fractionable=True)])
    history = trading_sessions_between(date(2022, 7, 1), date(2024, 1, 4))
    db.upsert_bars(
        [
            bar(symbol, session, 100 + index / 10)
            for symbol in ("AAA", "SPY")
            for index, session in enumerate(history)
        ]
    )
    with db.read_only() as connection:
        before = list(connection.iterdump())
    bundle = run_f_lifecycle_v2(db, config, date(2024, 1, 2), date(2024, 1, 4))
    assert bundle.summary["daily_qualification"]["ready"]
    assert len(bundle.results) == 7
    assert all(len(r.equity_curve) == 3 for r in bundle.results.values())
    with db.read_only() as connection:
        assert list(connection.iterdump()) == before


def test_broad_sic_basket_includes_peers_with_narrower_own_group(tmp_path, config):
    from trading_system.models.market_data import TradableAsset

    db = Database(tmp_path / "sic_fallback.sqlite3")
    db.initialize()
    sessions = trading_sessions_between(date(2023, 11, 1), date(2024, 1, 3))
    for i, symbol in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE"), 1):
        db.upsert_company(
            CompanyIdentity(
                cik=str(i).zfill(10),
                symbol=symbol,
                name=symbol,
                sic="2834" if symbol == "AAA" else "2833",
            )
        )
        db.upsert_assets(
            [TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=True)]
        )
        db.upsert_bars([bar(symbol, s, 90 + n / 10) for n, s in enumerate(sessions)])
    provider = TechnicalPeerContextProvider(db, config, sessions[-1])
    assert provider.context("AAA", sessions[-1]).peer_group == "sic3:283"
    assert provider.context("AAA", sessions[-1]).peer_count_valid == 4
    assert provider.context("BBB", sessions[-1]).peer_group == "sic4:2833"
    assert provider.context("BBB", sessions[-1]).peer_count_valid == 3
    assert provider.context("BBB", sessions[19]).peer_count_valid == 3
    assert provider.trend("BBB", sessions[19]) is TrendHealthState.UNAVAILABLE


def test_missing_daily_exit_session_cannot_silently_extend_past_hard_max(local_market, config):
    db, sessions = local_market
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='AAA' AND timestamp LIKE ?",
            (f"{sessions[20].isoformat()}%",),
        )
    with pytest.raises(ValueError, match="DAILY_POSITION_DATA_UNAVAILABLE"):
        run(
            db,
            sessions,
            config,
            F_LIFECYCLE_VARIANTS[4],
            Context(),
            require_complete_daily_position_bars=True,
        )
