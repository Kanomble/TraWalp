"""One frozen F run followed by descriptive local attribution; never an optimizer."""

import json
from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from itertools import chain
from pathlib import Path
from time import perf_counter

from trading_system.backtest.champion_edge_attribution import (
    CLOSE_REASONS,
    HORIZONS,
    average,
    candidate_metrics,
    cap_bucket,
    concentration,
    concentration_summary,
    drawdown_bucket,
    exit_attribution,
    holding_attribution,
    idle_summary,
    linear_relationship,
    market_regime_attribution,
    momentum_bucket,
    paired_attribution,
    quantile_attribution,
    rank_group,
    spy_regimes,
)
from trading_system.backtest.champion_edge_data import EdgeCollector, LocalDailyPanel
from trading_system.backtest.discovery_snapshot import data_fingerprint
from trading_system.backtest.engine import BacktestEngine, _backtest_sessions
from trading_system.backtest.f_candidates import (
    ManifestScreenSource,
    ReplaySpool,
    config_fingerprint,
    runtime_fingerprint,
)
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.backtest.liquid_universe import ResearchReadOnlyDatabase
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_definitions import F_CHAMPION_EDGE_DECOMPOSITION_V1 as EDGE_V1
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.data.market_sessions import (
    daily_warmup_start,
    required_daily_warmup_sessions,
    trading_sessions_between,
)

REPORT_NAMES = (
    "candidate_funnel",
    "gate_failures",
    "candidate_forward_returns",
    "rank_attribution",
    "quality_attribution",
    "valuation_attribution",
    "weighted_score_attribution",
    "technical_attribution",
    "trades",
    "exit_attribution",
    "holding_path",
    "holding_day_attribution",
    "blocked_candidates",
    "blocked_opportunity_cost",
    "idle_capital",
    "market_regime_attribution",
    "sector_attribution",
    "symbol_concentration",
    "market_beta",
    "fundamental_component_attribution",
    "market_cap_attribution",
)

CAVEATS = (
    "CURRENT_UNIVERSE_ONLY",
    "NOT_SURVIVORSHIP_CLEAN",
    "2024-2026 was already used extensively for strategy development.",
    "Descriptive, in-sample decomposition; all bucket findings are POST_HOC_DIAGNOSTICS.",
    "No attribution result is independent out-of-sample evidence.",
    "No finding may become a rule without a separately frozen hypothesis "
    "and independent validation period.",
    "Candidate forward returns are uncosted diagnostic outcomes, not simulated F trades.",
    "Occupied-slot opportunity cost is EX_POST_DIAGNOSTIC_ONLY, not a feasible rotation portfolio.",
    "Sector is the current local SIC two-digit proxy; historical classification is not PIT-safe.",
    "Daily OHLC cannot order exit-day extremes; intrabar stop/target exit days "
    "are excluded from open-path marks.",
    "Realized trade PnL, fills and exit reasons are taken directly "
    "from the unchanged champion engine.",
)


def report_paths(directory, stem):
    if not stem or Path(stem).name != stem or any(c in stem for c in "/\\:") or stem in {".", ".."}:
        raise ValueError("Champion edge output stem must be a plain filename stem")
    paths = {name: Path(directory) / f"{stem}_{name}.csv" for name in REPORT_NAMES}
    paths["summary"] = Path(directory) / f"{stem}_summary.json"
    if any(path.exists() for path in paths.values()):
        raise ValueError("CHAMPION_EDGE_OUTPUT_EXISTS: use a fresh stem")
    return paths


def trade_evidence(result, collector, panel, official, regimes):
    trades, path = [], []
    legs = {leg.position_id: leg for leg in result.trades}
    for position in result.positions:
        candidate = collector.candidates[(position.signal_date, position.symbol)]
        row = {
            **candidate,
            **regimes.get(position.signal_date, {}),
            "population": "EXECUTED_F_TRADES",
            "session": str(position.signal_date),
            "position_id": position.position_id,
            "signal_session": str(position.signal_date),
            "entry_session": str(position.entry_date),
            "exit_session": str(position.exit_date),
            "entry_reference": position.entry_reference_price,
            "entry_price": position.entry_price,
            "exit_reference": position.exit_reference_price,
            "exit_price": position.exit_price,
            "exit_reason": position.exit_reason,
            "holding_sessions": position.holding_days,
            "gross_pnl": position.gross_pnl,
            "net_pnl": position.net_pnl,
            "return": position.position_return,
            "quantity": position.initial_quantity,
            "r_multiple": None,
            "r_multiple_status": "UNAVAILABLE_FROM_CHAMPION_OUTPUT",
            "spy_same_holding_window_return": None,
        }
        leg = legs[position.position_id]
        if position.execution_legs != 1 or leg.net_pnl != position.net_pnl:
            raise ValueError("CHAMPION_EDGE_UNEXPECTED_EXECUTION_LEGS")
        if leg.daily_candidate_rank != candidate["f_rank"]:
            raise ValueError("CHAMPION_EDGE_RANK_RECONCILIATION_FAILED")
        spy = panel.by_day.get("SPY", {})
        entry, exit_bar = spy.get(position.entry_date), spy.get(position.exit_date)
        if entry and exit_bar:
            row["spy_same_holding_window_return"] = float(exit_bar.close / entry.open - 1)
        high, low = position.entry_reference_price, position.entry_reference_price
        complete_path = True
        for index, day in enumerate(
            (d for d in official if position.entry_date <= d <= position.exit_date), 1
        ):
            if day == position.exit_date and position.exit_reason not in CLOSE_REASONS:
                break  # no exit-day high/low/close after an un-timed stop or target
            bar = panel.by_day.get(position.symbol, {}).get(day)
            complete_path &= bar is not None
            if bar:
                high, low = max(high, float(bar.high)), min(low, float(bar.low))
            mark = float(bar.close) / position.entry_price - 1 if bar else None
            path.append(
                {
                    "population": "ACTUAL_OPEN_POSITION_SESSION_OBSERVATIONS",
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "session": str(day),
                    "holding_session": index,
                    "checkpoint": index in EDGE_V1.holding_checkpoints,
                    "unrealized_return": mark,
                    "MFE": high / position.entry_reference_price - 1 if complete_path else None,
                    "MAE": low / position.entry_reference_price - 1 if complete_path else None,
                    "path_complete": complete_path,
                    "eventual_net_pnl": position.net_pnl,
                    "eventual_exit_reason": position.exit_reason,
                }
            )
            if index in EDGE_V1.holding_checkpoints:
                row[f"open_path_return_{index}d"] = mark
        for checkpoint in EDGE_V1.holding_checkpoints:
            row.setdefault(f"open_path_return_{checkpoint}d", None)
        trades.append(row)
    if abs(sum(row["net_pnl"] for row in trades) - sum(leg.pnl for leg in result.trades)) > 1e-8:
        raise ValueError("CHAMPION_EDGE_PNL_RECONCILIATION_FAILED")
    return trades, path


def blocked_evidence(collector, panel, official, trades):
    blocked, comparisons = [], []
    positions = {row["position_id"]: row for row in trades}
    for item in collector.blocked:
        candidate = collector.candidates[(date.fromisoformat(item["session"]), item["symbol"])]
        blocked.append({**candidate, **item, "population": "CAPACITY_OCCUPIED_F_CANDIDATES"})
    groups = defaultdict(list)
    for row in blocked:
        groups[(row["session"], row["held_position_id"])].append(row)
    for (session, position_id), candidates in sorted(groups.items()):
        held = positions.get(position_id)
        if held is None:
            continue
        for horizon in HORIZONS:
            valid = [row for row in candidates if row[f"forward_return_{horizon}d"] is not None]
            if not valid:
                continue
            best = max(valid, key=lambda row: (row[f"forward_return_{horizon}d"], row["symbol"]))
            start, end = (
                date.fromisoformat(best["forward_entry_session"]),
                date.fromisoformat(best[f"forward_exit_session_{horizon}d"]),
            )
            bars = panel.by_day.get(held["symbol"], {})
            opening, closing = bars.get(start), bars.get(end)
            remained_open = end < date.fromisoformat(held["exit_session"]) or (
                end == date.fromisoformat(held["exit_session"])
                and held["exit_reason"] in CLOSE_REASONS
            )
            held_return = (
                float(closing.close / opening.open - 1)
                if opening and closing and remained_open
                else None
            )
            comparisons.append(
                {
                    "population": "ALIGNED_HELD_VS_BLOCKED_DIAGNOSTIC",
                    "session": session,
                    "horizon_sessions": horizon,
                    "held_symbol": held["symbol"],
                    "entry_session": str(start),
                    "horizon_end_session": str(end),
                    "held_position_remained_open_through_horizon": remained_open,
                    "held_subsequent_return": held_return,
                    "best_blocked_symbol": best["symbol"],
                    "best_blocked_forward_return": best[f"forward_return_{horizon}d"],
                    "ex_post_difference": best[f"forward_return_{horizon}d"] - held_return
                    if held_return is not None
                    else None,
                    "interpretation": (
                        "EX_POST_DIAGNOSTIC_ONLY; hindsight best is not a feasible selection rule"
                    ),
                }
            )
    for label, predicate in (
        ("ALL_BLOCKED", lambda rank: True),
        ("RANK_1", lambda rank: rank == 1),
        ("RANK_2_3", lambda rank: 2 <= rank <= 3),
        ("RANK_4_PLUS", lambda rank: rank >= 4),
    ):
        members = [row for row in blocked if predicate(row["f_rank"])]
        comparisons.append(
            {
                "population": "CAPACITY_OCCUPIED_F_CANDIDATES",
                "group": label,
                "interpretation": "EX_POST_DIAGNOSTIC_ONLY",
                **candidate_metrics(members),
            }
        )
    return blocked, comparisons


def idle_evidence(result, collector, panel, official, regimes):
    rows = []
    executions = defaultdict(list)
    for row in collector.candidates.values():
        if row["execution_session"]:
            executions[row["execution_session"]].append(row)
    prior = {
        context["execution_session"]: session for session, context in collector.context.items()
    }
    for point in result.equity_curve:
        day = point.date
        candidates = executions[str(day)]
        carried = collector.context.get(prior.get(day), {}).get("positions", ())
        entered = any(row["entry_executed"] for row in candidates)
        if carried:
            state = "IN_POSITION"
        elif entered:
            state = "CASH_ELIGIBLE_F_ENTRY_EXECUTED"
        elif not candidates:
            state = "CASH_NO_ELIGIBLE_F" if day in prior else "CASH_NO_PRIOR_SIGNAL_SESSION"
        elif any(
            row["execution_reason"] in {"missing_next_session_bar", "invalid_atr"}
            for row in candidates
        ):
            state = "CASH_ENTRY_UNAVAILABLE_DATA"
        else:
            state = "CASH_ENTRY_BLOCKED_OTHER"
        previous_index = bisect_left(official, day) - 1
        regime_day = official[previous_index] if previous_index >= 0 else None
        row = {
            **regimes.get(regime_day, {}),
            "population": "EXECUTABLE_CHAMPION_SESSIONS",
            "session": str(day),
            "state": state,
            "signal_session": str(prior[day]) if day in prior else None,
            "regime_as_of": str(regime_day) if regime_day else None,
            "invested": point.session_exposure > 0,
            "session_exposure": point.session_exposure,
            "end_of_day_exposure": point.end_of_day_exposure,
            "equity": point.portfolio_equity,
            "eligible_candidates_for_entry": len(candidates),
            "execution_reasons": sorted(
                {c["execution_reason"] for c in candidates if c["execution_reason"]}
            ),
        }
        forward = (
            panel.forward_returns("SPY", day, official, (1, 5, 10, 20))
            if state == "CASH_NO_ELIGIBLE_F"
            else {}
        )
        for horizon in (1, 5, 10, 20):
            row[f"spy_forward_return_{horizon}d"] = forward.get(f"forward_return_{horizon}d")
        rows.append(row)
    return rows


def beta_evidence(trades, result, panel):
    rows = [
        {
            "population": "EXECUTED_F_TRADES",
            "method": (
                "OLS F net trade return on SPY uncosted entry-open to exit-close return; "
                "equal trade weights; exit-session close is a Daily-resolution proxy "
                "for intrabar F exits"
            ),
            **linear_relationship(
                [(row["spy_same_holding_window_return"], row["return"]) for row in trades]
            ),
        }
    ]
    pairs = []
    spy = panel.by_day.get("SPY", {})
    for before, after in zip(result.equity_curve, result.equity_curve[1:], strict=False):
        a, b = spy.get(before.date), spy.get(after.date)
        if a and b and before.portfolio_equity > 0:
            pairs.append(
                (float(b.close / a.close - 1), after.portfolio_equity / before.portfolio_equity - 1)
            )
    rows.append(
        {
            "population": "CHAMPION_EQUITY_SESSION_INTERVALS_INCLUDING_CASH",
            "method": (
                "OLS net champion equity close-to-close return on SPY matching "
                "represented-session close-to-close return; no filled gaps"
            ),
            **linear_relationship(pairs),
        }
    )
    return rows


def build_tables(collector, result, panel, official):
    candidates = list(collector.candidates.values())
    for row in candidates:
        row.update(
            panel.forward_returns(
                row["symbol"], date.fromisoformat(row["session"]), official, HORIZONS
            )
        )
    regimes = spy_regimes(panel, official)
    trades, holding = trade_evidence(result, collector, panel, official, regimes)
    blocked, opportunity = blocked_evidence(collector, panel, official, trades)
    idle = idle_evidence(result, collector, panel, official, regimes)
    unavailable = {}
    tables = {
        "candidate_funnel": list(collector.funnel.values()),
        "gate_failures": collector.gate_failures,
        "candidate_forward_returns": candidates,
        "rank_attribution": paired_attribution(
            candidates, trades, "f_rank", lambda row: rank_group(row["f_rank"])
        ),
        "quality_attribution": quantile_attribution(
            candidates, trades, "quality_score", unavailable
        ),
        "valuation_attribution": quantile_attribution(
            candidates, trades, "valuation_score", unavailable
        ),
        "weighted_score_attribution": quantile_attribution(
            candidates, trades, "weighted_f_score", unavailable
        ),
        "technical_attribution": [],
        "trades": trades,
        "exit_attribution": exit_attribution(trades),
        "holding_path": holding,
        "holding_day_attribution": holding_attribution(holding),
        "blocked_candidates": blocked,
        "blocked_opportunity_cost": opportunity,
        "idle_capital": idle,
        "market_regime_attribution": market_regime_attribution(
            trades, idle, [regimes[d] for d in collector.funnel], unavailable
        ),
        "sector_attribution": concentration(trades, "sector"),
        "symbol_concentration": concentration(trades, "symbol"),
        "market_beta": beta_evidence(trades, result, panel),
    }
    for field, classifier in (
        ("drawdown_52w", drawdown_bucket),
        ("momentum126", momentum_bucket),
        ("sma20_rising", lambda value: "UNAVAILABLE" if value is None else str(value)),
    ):
        tables["technical_attribution"].extend(
            paired_attribution(
                candidates, trades, field, lambda row, f=field, fn=classifier: fn(row[f])
            )
        )
    for field in ("sma50_distance", "sma200_distance", "atr_price"):
        tables["technical_attribution"].extend(
            quantile_attribution(candidates, trades, field, unavailable)
        )
    components = sorted(
        {
            key
            for row in candidates
            for key in row
            if key.startswith("component_") and row[key] is not None
        }
    )
    if components:
        tables["fundamental_component_attribution"] = [
            item
            for field in components
            for item in quantile_attribution(candidates, trades, field, unavailable)
        ]
    else:
        unavailable["fundamental_component_attribution"] = (
            "NORMALIZED_PIT_SCORE_COMPONENTS_UNAVAILABLE"
        )
    if any(row["pit_market_cap"] is not None for row in candidates):
        tables["market_cap_attribution"] = paired_attribution(
            candidates, trades, "pit_market_cap", lambda row: cap_bucket(row["pit_market_cap"])
        )
    else:
        unavailable["market_cap_attribution"] = "MARKET_CAP_ATTRIBUTION_UNAVAILABLE_PIT"
    if not panel.bars.get("SPY"):
        unavailable["market_diagnostics"] = "LOCAL_SPY_DAILY_UNAVAILABLE"
    target = next(
        (
            row
            for row in tables["exit_attribution"]
            if row["exit_reason"] in {"profit_target", "take_profit"}
        ),
        {},
    )
    evidence = {
        "rank_1_mean_10d_forward_return": average(
            [
                r["forward_return_10d"]
                for r in candidates
                if r["f_rank"] == 1 and r["forward_return_10d"] is not None
            ]
        ),
        "target_exit_positive_pnl_share": target.get("share_total_positive_pnl"),
        "spy_trade_beta": tables["market_beta"][0]["ols_beta"],
        "blocked_rank1_mean_5d_return": average(
            [
                r["forward_return_5d"]
                for r in blocked
                if r["f_rank"] == 1 and r["forward_return_5d"] is not None
            ]
        ),
        "executed_trade_count": len(trades),
        "net_pnl": sum(r["net_pnl"] for r in trades),
        "engine_net_pnl": sum(p.net_pnl for p in result.positions),
        **idle_summary(idle),
        **concentration_summary(tables["symbol_concentration"]),
    }
    return tables, unavailable, evidence


def run_champion_edge_v1(database, config, start, end, directory, *, stem, screen_source=None):
    validate_champion_config(config)
    if start > end:
        raise ValueError("Champion edge start must not be after end")
    paths = report_paths(directory, stem)
    original = config.model_dump(mode="json")
    database = ResearchReadOnlyDatabase(database.path)
    tail = trading_sessions_between(end + timedelta(days=1), end + timedelta(days=60))[:20]
    outcome_end = tail[-1] if tail else end
    warmup = daily_warmup_start(
        start, max(252, required_daily_warmup_sessions(config), config.universe.market_data_days)
    )
    official = trading_sessions_between(warmup, outcome_end)
    summary = {
        **asdict(EDGE_V1),
        "requested_start": str(start),
        "requested_end": str(end),
        "champion_identity": FROZEN_CHAMPION_F.production_label,
        "champion_frozen_contract": {
            **asdict(FROZEN_CHAMPION_F),
            "reentry_enabled": config.position_management.reentry.enabled,
            "reentry_cooldown_days": config.position_management.reentry.cooldown_days,
        },
        "strategy_config_fingerprint": config_fingerprint(config),
        "runtime_fingerprint": runtime_fingerprint(),
        "market_data_feed": config.universe.market_data_feed,
        "market_data_adjustment": config.universe.market_data_adjustment,
        "universe_basis": "CURRENT_UNIVERSE_ONLY",
        "survivorship": "NOT_SURVIVORSHIP_CLEAN",
        "network_used": False,
        "strategy_modified": False,
        "warnings": list(CAVEATS),
        "outcome_data_end_requested": str(outcome_end),
        "forward_horizons": HORIZONS,
        "forward_method": (
            "Next represented portfolio Daily session open to official-session horizon close; "
            "1d is entry-session close; missing endpoints remain null; local tail up to "
            "20 sessions beyond requested end is diagnostics only"
        ),
        "funnel_method": (
            "Canonical candidate-audit first-failure stages; gate evaluated on every screen "
            "record; gate-reached is sequential; all-reason counts only include observable reasons"
        ),
        "holding_path_method": (
            "Official elapsed trading sessions, only while open through close; no marks on "
            "stop/target exit day; no post-exit values; engine holding count remains separate"
        ),
        "quintile_method": (
            "Whole eligible-candidate sample linear 20/40/60/80 percentiles; ties stay together; "
            "minimum 25 observations; never refit to trade subset"
        ),
        "f_ranking": "Existing configured Quality + Valuation only",
        "score_weights": config.scores.total.model_dump(mode="json"),
        "f_gate": config.screen_strategies.quality_value_momentum.model_dump(mode="json"),
        "reports": {},
        "unavailable": {},
        "performance": {},
    }
    try:
        sessions = _backtest_sessions(database, start, end)
    except ValueError as exc:
        summary.update(run_status="DATA_UNAVAILABLE", unavailable={"champion_simulation": str(exc)})
        Path(directory).mkdir(parents=True, exist_ok=True)
        _atomic_text(paths["summary"], json.dumps(summary, indent=2, default=str, allow_nan=False))
        return summary, {"summary": paths["summary"]}
    started = perf_counter()
    fingerprint_diagnostics = {}
    before = data_fingerprint(
        database, config, start=start, end=outcome_end, diagnostics=fingerprint_diagnostics
    )
    summary["database_source_fingerprint"] = before
    summary["source_fingerprint_scope"] = (
        "Existing F discovery content fingerprint, conservatively extended through local "
        "diagnostic tail; current membership/identity/SIC, PIT facts and Daily OHLCV/session keys"
    )
    collector = EdgeCollector(config)
    spool = ReplaySpool()
    try:
        with ProgressPhase("champion_edge_progress", "pit_screens"):
            source = screen_source or HistoricalFeatureScreenSource(
                database, config, sessions[0], sessions[-2]
            )
            for session in sessions[:-1]:
                spool.append(collector.prepare_screen(source.screen(session)))
            source_diagnostics = getattr(source, "diagnostics", None)
            summary["performance"]["screen_source"] = (
                source_diagnostics.as_dict() if source_diagnostics else {"synthetic_source": True}
            )
            del source
        summary["performance"]["screen_sessions"] = len(sessions) - 1
        summary["performance"]["prepare_seconds"] = perf_counter() - started
        official_set = set(official)
        all_sessions = [d for d in database.bar_sessions(warmup, outcome_end) if d in official_set]
        symbols = sorted(collector.seen_symbols | {"SPY"})
        panel = LocalDailyPanel(
            chain.from_iterable(database.iter_bar_batches(symbols, warmup, outcome_end)),
            all_sessions,
            database.bar_date_bounds(),
            database.list_tradable_companies(),
            database.unresolved_sec_identity_conflict_symbols(),
        )
        replay = ManifestScreenSource(None, spool.offsets, stream=spool.file)
        before_sql = database.sql_queries
        started = perf_counter()
        with ProgressPhase("champion_edge_progress", "frozen_champion_once"):
            result = BacktestEngine(
                panel,
                config,
                screen_source=replay,
                audit_observer=collector,
                entry_context_observer=collector.observe_entry_context,
            ).run(start, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset)
        summary["performance"].update(
            champion_simulations=1,
            simulation_seconds=perf_counter() - started,
            simulation_sql_queries=database.sql_queries - before_sql,
            replay_peak_cached_sessions=len(replay.cache),
            daily_panel_symbols=len(panel.bars),
            daily_panel_rows=sum(map(len, panel.bars.values())),
        )
        started = perf_counter()
        tables, unavailable, evidence = build_tables(collector, result, panel, official)
        summary["performance"]["attribution_seconds"] = perf_counter() - started
        if config.model_dump(mode="json") != original:
            raise ValueError("CHAMPION_EDGE_CONFIG_MUTATED")
        validate_champion_config(config)
        if data_fingerprint(database, config, start=start, end=outcome_end) != before:
            raise ValueError("CHAMPION_EDGE_SOURCE_CHANGED_DURING_RUN")
        summary.update(
            run_status="COMPLETE",
            evidence=evidence,
            unavailable=unavailable,
            actual_start=str(sessions[0]),
            actual_end=str(sessions[-1]),
            champion_result_warnings=result.warnings,
        )
        summary["performance"].update(
            source_fingerprint=fingerprint_diagnostics, total_sql_queries=database.sql_queries
        )
        Path(directory).mkdir(parents=True, exist_ok=True)
        written = {"summary": paths["summary"]}
        for name, rows in tables.items():
            if not rows:
                summary["unavailable"][name] = "NO_OBSERVATIONS_FOR_THIS_POPULATION"
                continue
            fields = list(dict.fromkeys(key for row in rows for key in row))
            _atomic_csv(paths[name], rows, fields)
            written[name] = paths[name]
            summary["reports"][name] = {
                "path": str(paths[name]),
                "rows": len(rows),
                "populations": sorted({row["population"] for row in rows}),
            }
        _atomic_text(paths["summary"], json.dumps(summary, indent=2, default=str, allow_nan=False))
        return summary, written
    finally:
        spool.file.close()
