"""Synthetic attribution and invariance: no provider or historical experiment."""

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_champion_consolidation import bar, record
from test_champion_consolidation import config as config
from test_champion_consolidation import offline as offline

from trading_system import cli
from trading_system.backtest import champion_edge_research as research
from trading_system.backtest.champion_edge_attribution import (
    linear_relationship,
    quantile_attribution,
    quantile_definition,
    rank_group,
    spy_regimes,
)
from trading_system.backtest.champion_edge_data import EdgeCollector, LocalDailyPanel
from trading_system.backtest.engine import BacktestEngine, evaluate_variant_entry
from trading_system.backtest.research_definitions import F_CHAMPION_EDGE_DECOMPOSITION_V1 as EDGE_V1
from trading_system.backtest.research_registry import (
    DIAGNOSTIC_RESEARCH_FAMILIES,
    FROZEN_CHAMPION_F,
    INDEPENDENT_RESEARCH_FAMILIES,
    RESEARCH_FAMILY_STATUS,
    validate_champion_config,
)
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.models.backtest import PositionManagementPreset, StrategyVariant
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset
from trading_system.models.scores import FactorScore
from trading_system.models.screening import ScreenReport


def forbidden(*args, **kwargs):
    raise AssertionError("Forbidden network, SQL, source re-evaluation or mutation")


def rich_record(symbol="AAA", *, quality=90, valuation=90, sic="3571"):
    item = record(symbol, quality=quality, valuation=valuation)
    scores = item.scores.model_copy(
        update={
            "quality": item.scores.quality.model_copy(
                update={
                    "factors": (
                        FactorScore(
                            name="revenue_growth",
                            raw_value=0.1,
                            score=quality,
                            configured_weight=1,
                            explanation="Synthetic PIT score",
                        ),
                    )
                }
            )
        }
    )
    return item.model_copy(
        update={
            "scores": scores,
            "sic": sic,
            "estimated_market_cap": 2e9,
            "pit_fact_count": 5,
            "latest_pit_filing_date": date(2023, 12, 1),
            "market_history_count": 300,
        }
    )


class Screens:
    def __init__(self, records, active=None):
        self.records, self.active, self.calls = records, active, []

    def screen(self, session):
        assert session not in self.calls
        self.calls.append(session)
        rows = self.records if self.active is None or session in self.active else ()
        rows = tuple(
            r.model_copy(
                update={
                    "as_of": session,
                    "technical": r.technical.model_copy(update={"market_session": session}),
                }
            )
            for r in rows
        )
        return ScreenReport(
            as_of=session,
            generated_at="synthetic",
            analyzed_count=len(rows),
            eligible_count=0,
            records=rows,
        )


@pytest.fixture
def market(tmp_path):
    db = Database(tmp_path / "edge.sqlite3")
    db.initialize()
    for index, symbol in enumerate(("AAA", "BBB", "CCC"), 1):
        db.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=symbol,
                    tradable=True,
                    fractionable=True,
                    asset_class="US_EQUITY",
                )
            ]
        )
        db.upsert_company(
            CompanyIdentity(cik=f"{index:010d}", symbol=symbol, name=symbol, sic="3571")
        )
    days = trading_sessions_between(date(2024, 1, 2), date(2024, 3, 1))
    db.upsert_bars([bar(symbol, day) for symbol in ("AAA", "BBB", "CCC", "SPY") for day in days])
    return db, days


def run(market, config, tmp_path, *, records=None, active=None, stem="edge", end_index=23):
    db, days = market
    source = Screens(
        records if records is not None else (rich_record(), rich_record("BBB", quality=80)), active
    )
    summary, paths = research.run_champion_edge_v1(
        db, config, days[0], days[end_index], tmp_path, stem=stem, screen_source=source
    )
    return summary, paths, source


def rows(paths, name):
    with paths[name].open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_diagnostic_identity_and_all_frozen_champion_economics(config):
    assert EDGE_V1.research_id == "F-CHAMPION-EDGE-DECOMPOSITION-V1"
    assert EDGE_V1.kind == "DIAGNOSTIC_ONLY"
    assert DIAGNOSTIC_RESEARCH_FAMILIES[EDGE_V1.research_family] == EDGE_V1
    assert RESEARCH_FAMILY_STATUS[EDGE_V1.research_family] == "ACTIVE"
    assert EDGE_V1.research_family not in INDEPENDENT_RESEARCH_FAMILIES
    assert EDGE_V1.research_id not in {item.value for item in StrategyVariant}
    assert EDGE_V1.research_id not in {item.value for item in PositionManagementPreset}
    assert FROZEN_CHAMPION_F.variant is StrategyVariant.QUALITY_VALUE_MOMENTUM
    assert FROZEN_CHAMPION_F.preset is PositionManagementPreset.CONFIGURED
    for field, value in dict(
        max_positions=1,
        max_position_pct=1.0,
        risk_per_trade=0.01,
        atr_period=14,
        atr_stop_multiple=2.0,
        max_stop_loss_pct=0.1,
        profit_target_pct=0.12,
        hard_max_hold_sessions=10,
        slippage_bps=5.0,
        commission_bps=0.0,
    ).items():
        assert getattr(FROZEN_CHAMPION_F, field) == value
    assert config.position_management.reentry.enabled
    assert config.position_management.reentry.cooldown_days == 0
    validate_champion_config(config)


def test_one_unchanged_engine_run_reconciles_trades_and_no_mutation(
    market, config, tmp_path, monkeypatch
):
    db, days = market
    original = config.model_dump()
    protected = [Path("config/strategy.yaml"), Path("src/trading_system/champion.py")]
    before_files = [p.read_bytes() for p in protected]
    records = (rich_record(), rich_record("BBB", quality=80))
    baseline = BacktestEngine(db, config, screen_source=Screens(records)).run(
        days[0], days[23], variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
    )
    original_run = BacktestEngine.run
    results = []

    def guarded(engine, *args, **kwargs):
        with monkeypatch.context() as guard:
            guard.setattr(sqlite3, "connect", forbidden)
            result = original_run(engine, *args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(BacktestEngine, "run", guarded)
    summary, paths, source = run(market, config, tmp_path, records=records)
    assert len(results) == 1
    assert results[0].positions == baseline.positions
    assert results[0].trades == baseline.trades
    assert results[0].equity_curve == baseline.equity_curve
    assert results[0].configuration == baseline.configuration
    assert len(source.calls) == 23
    assert config.model_dump() == original
    assert [p.read_bytes() for p in protected] == before_files
    assert summary["network_used"] is summary["strategy_modified"] is False
    assert summary["performance"]["champion_simulations"] == 1
    assert summary["performance"]["simulation_sql_queries"] == 0
    assert summary["performance"]["replay_peak_cached_sessions"] == 1
    trades = rows(paths, "trades")
    assert sum(float(r["net_pnl"]) for r in trades) == pytest.approx(
        sum(t.pnl for t in baseline.trades)
    )
    assert summary["evidence"]["net_pnl"] == summary["evidence"]["engine_net_pnl"]
    assert all(r["population"] == "EXECUTED_F_TRADES" for r in trades)
    assert all(
        r["population"] == "F_ELIGIBLE_CANDIDATES" for r in rows(paths, "candidate_forward_returns")
    )


def test_funnel_and_existing_gate_precedence(config):
    good = rich_record()
    bad_gate = rich_record("BAD").model_copy(
        update={
            "technical": good.technical.model_copy(
                update={
                    "drawdown_52w": -0.2,
                    "momentum126": -0.1,
                    "sma50": 110,
                    "sma200": 120,
                    "sma20_rising": False,
                }
            )
        }
    )
    missing = rich_record("MISSING").model_copy(
        update={"technical": good.technical.model_copy(update={"sma50": None})}
    )
    history = rich_record("HISTORY").model_copy(
        update={"exclusion_reasons": ("insufficient_market_history",)}
    )
    quality = rich_record("QUALITY", quality=0)
    value = rich_record("VALUE", valuation=0)
    collector = EdgeCollector(config)
    report = Screens((good, bad_gate, missing, history, quality, value)).screen(date(2024, 1, 2))
    collector.prepare_screen(report)
    row = collector.funnel[report.as_of]
    assert row["tradable_current_universe"] == 6
    assert row["market_history_available"] == 5
    assert row["quality_threshold_pass"] == 4
    assert row["valuation_threshold_pass"] == 3
    assert row["f_technical_gate_reached"] == 3
    assert row["f_final_eligible"] == row["ranked_f_candidates"] == 1
    gate = [r for r in collector.gate_failures if r["scope"] == "TECHNICAL_FIRST_FAILURE"]
    assert {r["reason"] for r in gate} == {
        "qv_momentum_not_near_52w_high",
        "qv_momentum_sma50_unavailable",
    }
    all_gate = [
        r["reason"] for r in collector.gate_failures if r["scope"] == "TECHNICAL_ALL_FAILURES"
    ]
    assert {
        "qv_momentum_momentum126_not_positive",
        "qv_momentum_price_not_above_sma50",
        "qv_momentum_price_not_above_sma200",
        "qv_momentum_sma20_not_rising",
    } <= set(all_gate)
    assert (
        collector.candidates[(report.as_of, "AAA")]["weighted_f_score"]
        == evaluate_variant_entry(good, FROZEN_CHAMPION_F.variant, config).score
    )


def test_forward_horizons_count_official_sessions_and_do_not_substitute():
    days = trading_sessions_between(date(2024, 1, 12), date(2024, 2, 16))
    assert days[1] == date(2024, 1, 16)  # holiday weekend
    panel = LocalDailyPanel(
        [
            bar("AAA", day, opening=100, high=130, low=99, close=100 + i)
            for i, day in enumerate(days)
        ],
        days,
        (days[0], days[-1]),
    )
    forward = panel.forward_returns("AAA", days[0], days, (1, 3, 20))
    assert forward["forward_entry_session"] == str(days[1])
    assert forward["forward_return_1d"] == pytest.approx(0.01)
    assert forward["forward_return_3d"] == pytest.approx(0.03)
    del panel.by_day["AAA"][days[3]]
    assert panel.forward_returns("AAA", days[0], days, (3,))["forward_return_3d"] is None
    del panel.by_day["AAA"][days[1]]
    assert panel.forward_returns("AAA", days[0], days, (1,))["forward_return_1d"] is None


def test_fixed_rank_and_quantile_rules_do_not_search_cutoffs():
    assert [rank_group(n) for n in (1, 2, 3, 4, 5, 6, 10, 11)] == [
        "RANK_1",
        "RANK_2_3",
        "RANK_2_3",
        "RANK_4_5",
        "RANK_4_5",
        "RANK_6_10",
        "RANK_6_10",
        "RANK_11_PLUS",
    ]
    candidates = [
        {"quality_score": n, **{f"forward_return_{h}d": 0.01 for h in (1, 3, 5, 10, 20)}}
        for n in range(25)
    ]
    edges, reason = quantile_definition(candidates, "quality_score")
    assert reason is None and edges == pytest.approx([4.8, 9.6, 14.4, 19.2])
    table = quantile_attribution(candidates, [], "quality_score", {})
    assert len(table) == 5 and all(r["interpretation"] == "POST_HOC_DIAGNOSTICS" for r in table)
    assert quantile_definition(candidates[:24], "quality_score")[0] is None
    assert quantile_definition([{"q": 3}] * 25, "q")[0] is None


def test_blocked_is_occupied_only_and_never_becomes_trade(market, config, tmp_path):
    summary, paths, _ = run(market, config, tmp_path)
    blocked = rows(paths, "blocked_candidates")
    funnel = rows(paths, "candidate_funnel")
    assert int(funnel[0]["entry_capacity_reserved"]) == 1
    assert int(funnel[0]["entry_blocked_by_occupied_capacity"]) == 0
    assert all(r["session"] != funnel[0]["session"] for r in blocked)
    assert all(r["symbol"] == "BBB" and r["blocking_held_symbol"] == "AAA" for r in blocked)
    assert {r["symbol"] for r in rows(paths, "trades")} == {"AAA"}
    assert summary["evidence"]["executed_trade_count"] > 0
    assert all(
        "EX_POST_DIAGNOSTIC_ONLY" in r["interpretation"]
        for r in rows(paths, "blocked_opportunity_cost")
    )


def test_sector_block_is_not_falsely_called_sole_capacity_block(config):
    config.portfolio.max_sector_positions = 1
    collector = EdgeCollector(config)
    report = Screens((rich_record("BBB"),)).screen(date(2024, 1, 2))
    collector.prepare_screen(report)
    position = SimpleNamespace(
        symbol="AAA",
        position_id="p",
        entry_date=date(2024, 1, 1),
        holding_days=1,
        last_price=101,
        entry_price=100,
        sector="35",
    )
    collector.observe_entry_context(report, {"AAA": position}, date(2024, 1, 3))
    collector.observe_portfolio_decision(report.as_of, "BBB", "blocked", "max_positions_reached")
    assert not collector.blocked


def test_idle_distinguishes_held_no_candidate_and_unavailable(market, config, tmp_path):
    db, days = market
    _, paths, _ = run(market, config, tmp_path, active={days[0]})
    states = {r["state"] for r in rows(paths, "idle_capital")}
    assert {"CASH_ELIGIBLE_F_ENTRY_EXECUTED", "IN_POSITION", "CASH_NO_ELIGIBLE_F"} <= states
    with db.connect() as connection:
        connection.execute(
            "DELETE FROM bars WHERE symbol='AAA' AND substr(timestamp,1,10)=?", (str(days[1]),)
        )
    _, paths, _ = run(market, config, tmp_path, active={days[0]}, stem="gap")
    assert rows(paths, "idle_capital")[1]["state"] == "CASH_ENTRY_UNAVAILABLE_DATA"


def test_spy_regimes_pit_missing_data_and_future_invariance():
    days = trading_sessions_between(date(2022, 1, 3), date(2024, 1, 31))
    bars = [
        bar("SPY", day, opening=100, low=90, high=200, close=100 + i / 10)
        for i, day in enumerate(days)
    ]
    panel = LocalDailyPanel(bars, days, (days[0], days[-1]))
    original = spy_regimes(panel, days)
    assert original[days[250]]["spy_drawdown_52w"] is None
    assert original[days[251]]["spy_drawdown_52w"] is not None
    assert original[days[199]]["spy_above_sma200"] is True
    panel.by_day["SPY"][days[-1]] = bar("SPY", days[-1], high=1000, close=999)
    updated = spy_regimes(panel, days)
    assert all(original[day] == updated[day] for day in days[:-1])
    del panel.by_day["SPY"][days[198]]
    assert spy_regimes(panel, days)[days[199]]["spy_above_sma200"] is None


def test_holding_paths_end_at_actual_exit_and_ignore_post_exit_prices(market, config, tmp_path):
    db, days = market
    db.upsert_bars([bar("AAA", days[4], high=150, low=99, close=100)])
    _, paths, _ = run(market, config, tmp_path, active={days[0]})
    trades, path = rows(paths, "trades"), rows(paths, "holding_path")
    assert trades[0]["exit_reason"] == "profit_target"
    assert max(r["session"] for r in path) == str(days[3])
    assert all(r["open_path_return_5d"] == "" for r in trades)
    db.upsert_bars([bar("AAA", days[5], high=10000, low=1, close=9000)])
    _, after, _ = run(market, config, tmp_path, active={days[0]}, stem="future")
    assert rows(after, "holding_path") == path
    assert rows(after, "exit_attribution") == rows(paths, "exit_attribution")


def test_optional_diagnostics_and_concentration_reconcile(market, config, tmp_path):
    summary, paths, _ = run(market, config, tmp_path, records=(rich_record(),))
    assert "market_cap_attribution" in paths and "fundamental_component_attribution" in paths
    trades = rows(paths, "trades")
    for table in ("symbol_concentration", "sector_attribution", "exit_attribution"):
        assert sum(float(r["total_net_pnl"]) for r in rows(paths, table)) == pytest.approx(
            sum(float(r["net_pnl"]) for r in trades)
        )
    assert summary["evidence"]["top_1_symbol_trade_count_share"] == 1
    _, missing_paths, _ = run(market, config, tmp_path, records=(record(),), stem="missing")
    payload = json.loads(missing_paths["summary"].read_text())
    assert (
        payload["unavailable"]["market_cap_attribution"] == "MARKET_CAP_ATTRIBUTION_UNAVAILABLE_PIT"
    )
    assert "market_cap_attribution" not in missing_paths


def test_future_diagnostic_returns_cannot_affect_selection(market, config, tmp_path):
    db, days = market
    _, first, _ = run(market, config, tmp_path, end_index=5)
    db.upsert_bars([bar("BBB", day, high=1000, low=99, close=900) for day in days[6:]])
    _, second, _ = run(market, config, tmp_path, end_index=5, stem="different_tail")
    before, after = (
        rows(first, "candidate_forward_returns"),
        rows(second, "candidate_forward_returns"),
    )
    keys = (
        "session",
        "symbol",
        "f_rank",
        "weighted_f_score",
        "selection_outcome",
        "entry_executed",
    )
    assert [[r[k] for k in keys] for r in before] == [[r[k] for k in keys] for r in after]
    assert before != after
    actual = ("symbol", "entry_session", "entry_price", "exit_session", "exit_price", "net_pnl")
    assert [[r[k] for k in actual] for r in rows(first, "trades")] == [
        [r[k] for k in actual] for r in rows(second, "trades")
    ]


def test_config_drift_fails_before_data_access(config, tmp_path):
    config.risk.risk_per_trade = 0.02
    with pytest.raises(ValueError, match="Frozen champion requires"):
        research.run_champion_edge_v1(
            None, config, date(2024, 1, 2), date(2024, 1, 5), tmp_path, stem="bad"
        )


def test_cli_local_readonly_no_strategy_options(market, config, tmp_path, monkeypatch):
    db, days = market
    settings = load_settings()
    settings.strategy.storage.database_path = db.path
    settings.strategy.storage.reports_path = tmp_path
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    monkeypatch.setattr(
        research, "HistoricalFeatureScreenSource", lambda *a: Screens((rich_record(),))
    )
    monkeypatch.setattr(Database, "initialize", forbidden)
    original = sqlite3.connect

    def readonly(path, *args, **kwargs):
        assert "mode=ro" in str(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly)
    args = [
        "analyze-champion-edge-v1",
        "--start",
        str(days[0]),
        "--end",
        str(days[5]),
        "--output-stem",
        "cli_edge",
    ]
    assert cli.main(args) == 0
    for extra in ("--variant", "--strategy", "--max-positions", "--optimize"):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(args + [extra, "X"])


def test_missing_data_reports_unavailable_and_no_empty_tables(tmp_path, config):
    db = Database(tmp_path / "empty.sqlite3")
    db.initialize()
    summary, paths = research.run_champion_edge_v1(
        db, config, date(2024, 1, 2), date(2024, 1, 5), tmp_path, stem="empty"
    )
    assert summary["run_status"] == "DATA_UNAVAILABLE"
    assert set(paths) == {"summary"}


def test_known_beta_statistics():
    values = linear_relationship([(x, 2 * x + 0.1) for x in (0.01, 0.02, 0.03, 0.04)])
    assert values["ols_beta"] == pytest.approx(2)
    assert values["ols_intercept"] == pytest.approx(0.1)
    assert values["r_squared"] == pytest.approx(1)
    assert values["spearman"] == pytest.approx(1)


@pytest.mark.parametrize(
    "case", ["target", "stop", "time", "eob", "missing_tenth", "missing_final"]
)
def test_engine_exit_and_missing_daily_behavior_exactly_preserved(market, config, tmp_path, case):
    db, days = market
    end_index = 5 if case in {"eob", "missing_final"} else 23
    if case == "target":
        db.upsert_bars([bar("AAA", days[2], high=130)])
    elif case == "stop":
        db.upsert_bars([bar("AAA", days[2], low=80)])
    elif case in {"missing_tenth", "missing_final"}:
        missing = days[10] if case == "missing_tenth" else days[end_index]
        with db.connect() as connection:
            connection.execute(
                "DELETE FROM bars WHERE symbol='AAA' AND substr(timestamp,1,10)=?", (str(missing),)
            )
    baseline = BacktestEngine(db, config, screen_source=Screens((rich_record(),), {days[0]})).run(
        days[0], days[end_index], variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
    )
    summary, paths, _ = run(
        market, config, tmp_path, records=(rich_record(),), active={days[0]}, end_index=end_index
    )
    evidence = rows(paths, "trades")[0]
    position = baseline.positions[0]
    assert evidence["exit_reason"] == position.exit_reason
    assert evidence["exit_session"] == str(position.exit_date)
    assert float(evidence["net_pnl"]) == position.net_pnl
    assert float(evidence["entry_price"]) == position.entry_price
    assert float(evidence["exit_price"]) == position.exit_price
    assert summary["evidence"]["net_pnl"] == sum(p.net_pnl for p in baseline.positions)


def test_real_pit_feature_pipeline_prepared_once_with_synthetic_database(
    tmp_path, config, monkeypatch
):
    from test_backtest_features import _database

    db, end = _database(tmp_path)
    start = end - timedelta(days=7)
    original_source = research.HistoricalFeatureScreenSource
    calls = []

    class CountingSource(original_source):
        def screen(self, session):
            assert session not in calls
            calls.append(session)
            return super().screen(session)

    monkeypatch.setattr(research, "HistoricalFeatureScreenSource", CountingSource)
    monkeypatch.setattr(Database, "facts_available_as_of", forbidden)
    summary, paths = research.run_champion_edge_v1(db, config, start, end, tmp_path, stem="pit")
    assert summary["run_status"] == "COMPLETE"
    assert summary["performance"]["screen_sessions"] == len(calls)
    assert summary["performance"]["screen_source"]["companies_universe"] == 1
    assert all(
        int(row["tradable_current_universe"]) == 1 for row in rows(paths, "candidate_funnel")
    )


def test_source_drift_fails_closed_before_export(market, config, tmp_path, monkeypatch):
    original = research.data_fingerprint
    calls = []

    def changed(*args, **kwargs):
        calls.append(1)
        value = original(*args, **kwargs)
        return value if len(calls) == 1 else "drift"

    monkeypatch.setattr(research, "data_fingerprint", changed)
    with pytest.raises(ValueError, match="SOURCE_CHANGED_DURING_RUN"):
        run(market, config, tmp_path)
    assert not (tmp_path / "edge_summary.json").exists()


@pytest.mark.parametrize("field", ["as_of", "market_session", "filing_date"])
def test_future_pit_source_data_refused(config, field):
    day = date(2024, 1, 2)
    item = rich_record()
    if field == "market_session":
        item = item.model_copy(
            update={"technical": item.technical.model_copy(update={field: day + timedelta(days=1)})}
        )
    else:
        item = item.model_copy(
            update={
                "as_of" if field == "as_of" else "latest_pit_filing_date": day + timedelta(days=1)
            }
        )
    report = ScreenReport(
        as_of=day, generated_at="synthetic", analyzed_count=1, eligible_count=0, records=(item,)
    )
    with pytest.raises(ValueError, match="NON_PIT_SCREEN"):
        EdgeCollector(config).prepare_screen(report)


def test_held_opportunity_horizons_are_aligned_and_end_before_exit(market, config, tmp_path):
    db, days = market
    db.upsert_bars([bar("BBB", day, opening=100, high=130, low=99, close=110) for day in days])
    _, paths, _ = run(market, config, tmp_path, active=set(days[:10]))
    comparisons = [
        r
        for r in rows(paths, "blocked_opportunity_cost")
        if r["population"] == "ALIGNED_HELD_VS_BLOCKED_DIAGNOSTIC"
    ]
    valid = [r for r in comparisons if r["held_subsequent_return"]]
    assert valid and all(float(r["held_subsequent_return"]) == 0 for r in valid)
    assert all(float(r["best_blocked_forward_return"]) == pytest.approx(0.1) for r in valid)
    assert all(
        r["held_subsequent_return"] == ""
        for r in comparisons
        if r["held_position_remained_open_through_horizon"] == "False"
    )


def test_exit_concentration_positive_and_negative_shares_reconcile():
    from trading_system.backtest.champion_edge_attribution import concentration, exit_attribution

    trades = [
        dict(
            symbol=symbol,
            sector=sector,
            net_pnl=pnl,
            return_=pnl / 100,
            holding_sessions=days,
            f_rank=rank,
            exit_reason=reason,
        )
        for symbol, sector, pnl, days, rank, reason in (
            ("AAA", "35", 12, 2, 1, "profit_target"),
            ("AAA", "35", -4, 3, 2, "stop_loss"),
            ("BBB", "28", 2, 10, 3, "time_exit"),
        )
    ]
    trades = [{**row, "return": row["return_"]} for row in trades]
    exits = exit_attribution(trades)
    assert sum(row["share_total_positive_pnl"] for row in exits) == pytest.approx(1)
    assert sum(row["share_total_negative_pnl"] for row in exits) == pytest.approx(1)
    for field in ("symbol", "sector"):
        table = concentration(trades, field)
        assert sum(row["trade_count_share"] for row in table) == pytest.approx(1)
        assert sum(row["net_pnl_share"] for row in table) == pytest.approx(1)
