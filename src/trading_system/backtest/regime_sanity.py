"""Offline diagnostic attribution of one unchanged F/configured/C1 portfolio path.

No strategy settings are returned or modified. Regimes are joined AFTER execution.
Run: python -m trading_system.backtest.regime_sanity (local data only).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from trading_system.backtest.engine import BacktestEngine, entry_blocking_reasons, evaluate_variant_entry
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.backtest.metrics import maximum_drawdown
from trading_system.backtest.qualification import qualify_historical_screen_start
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.backtest.screen_strategies import evaluate_strategy_gate
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between

START = date(2022, 1, 3)
END = date(2026, 8, 12)
STEM = "f_c1_regime_sanity_2022-01-03_2026-08-12_v1"
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "reports"
BIASES = [
    "CURRENT_UNIVERSE_ONLY", "NOT_SURVIVORSHIP_CLEAN",
    "current SIC / classification may not be PIT",
    "historical provider data may be revised",
    "SEC filing availability has day-level granularity",
]


class ReadOnlyDatabase(Database):
    """Route even legacy engine reads through the existing SQLite mode=ro boundary."""

    def connect(self):
        return self.read_only()


@contextmanager
def offline_guard():
    """Deny network attempts, non-read-only connections and SQL writes at runtime."""
    import socket
    from unittest.mock import patch

    counts = {k: 0 for k in (
        "network_calls", "provider_calls", "broker_calls", "database_writes_to_market_data",
        "strategy_changes", "blocked_network_attempts", "blocked_database_write_attempts",
    )}
    original_connect = sqlite3.connect

    def deny_network(*args, **kwargs):
        counts["blocked_network_attempts"] += 1
        raise RuntimeError("OFFLINE_AUDIT_NETWORK_FORBIDDEN")

    forbidden = {getattr(sqlite3, name) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_TABLE",
        "SQLITE_DROP_TABLE", "SQLITE_ALTER_TABLE", "SQLITE_CREATE_INDEX", "SQLITE_DROP_INDEX",
        "SQLITE_CREATE_TRIGGER", "SQLITE_DROP_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_VIEW",
        "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_REINDEX", "SQLITE_ANALYZE",
    )}

    def authorizer(action, arg1, arg2, db_name, trigger):
        if action in forbidden:
            counts["blocked_database_write_attempts"] += 1
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and arg2 is not None:
            counts["blocked_database_write_attempts"] += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(database, *args, **kwargs):
        if not kwargs.get("uri") or "mode=ro" not in str(database):
            counts["blocked_database_write_attempts"] += 1
            raise RuntimeError("OFFLINE_AUDIT_READ_ONLY_CONNECTION_REQUIRED")
        connection = original_connect(database, *args, **kwargs)
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(authorizer)
        return connection

    # The core run imports no provider clients; HTTP and raw-socket boundaries also fail closed.
    import requests
    with (
        patch.object(sqlite3, "connect", connect),
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket.socket, "connect_ex", deny_network),
        patch.object(socket.socket, "sendto", deny_network),
        patch.object(socket, "create_connection", deny_network),
        patch.object(socket, "getaddrinfo", deny_network),
        patch.object(requests.sessions.Session, "request", deny_network),
    ):
        yield counts


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(suffix, value):
    path = OUT / f"{STEM}_{suffix}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        stream.write("\n")
    return path


def write_csv(suffix, rows):
    path = OUT / f"{STEM}_{suffix}.csv"
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def profit_factor(pnls):
    loss = -sum(p for p in pnls if p < 0)
    return sum(p for p in pnls if p > 0) / loss if loss else None


def trade_stats(positions):
    pnls = [p.net_pnl for p in positions]
    returns = [p.position_return for p in positions]
    return {
        "trades": len(positions), "net_pnl": sum(pnls),
        "profit_factor": profit_factor(pnls),
        "win_rate": mean(p > 0 for p in pnls) if pnls else None,
        "average_trade_return": mean(returns) if returns else None,
        "median_trade_return": median(returns) if returns else None,
        "worst_trade": min(returns, default=None), "best_trade": max(returns, default=None),
        "average_holding_period": mean(p.holding_days for p in positions) if positions else None,
        "gross_profit": sum(max(p, 0) for p in pnls),
        "gross_loss": sum(min(p, 0) for p in pnls),
    }


def spy_regimes(bars, sessions):
    """Only a completed exact T bar and its trailing prefix may label session T."""
    ordered = sorted((d, float(c)) for d, c in bars)
    if len({d for d, _ in ordered}) != len(ordered):
        raise ValueError("Duplicate SPY Daily session")
    index = {d: i for i, (d, _) in enumerate(ordered)}
    result = {}
    for session in sessions:
        i = index.get(session)
        if i is None or i < 199:
            raise ValueError(f"Incomplete SPY regime history: {session}")
        prefix = ordered[i - 199:i + 1]
        if [d for d, _ in prefix] != trading_sessions_between(prefix[0][0], session):
            raise ValueError(f"Missing SPY trailing session: {session}")
        close = ordered[i][1]
        sma = mean(c for _, c in prefix)
        momentum = close / ordered[i - 126][1] - 1
        above, positive = close > sma, momentum > 0
        result[session] = {
            "spy_bar_date": session.isoformat(), "spy_close": close,
            "spy_sma200": sma, "spy_momentum126": momentum,
            "regime_a": "ABOVE_SMA200" if above else "BELOW_EQUAL_SMA200",
            "regime_b": "POSITIVE_MOM126" if positive else "NON_POSITIVE_MOM126",
            "regime_c": "BULL" if above and positive else "MIXED" if above or positive else "WEAK",
        }
    return result


def candidate_aggregation(rows):
    values = [row["final_eligible_f_candidates"] for row in rows]
    return {
        "sessions": len(values),
        "median_eligible_candidates_per_session": median(values) if values else None,
        "mean_eligible_candidates_per_session": mean(values) if values else None,
        "sessions_zero_candidates": values.count(0), "sessions_exactly_one_candidate": values.count(1),
        "sessions_more_than_one_candidate": sum(v > 1 for v in values),
    }


class AuditObserver:
    def __init__(self, first_filed):
        self.rows = []
        self.entry_context = {}
        self.first_filed = first_filed
        self.unique = defaultdict(set)
        self.validation_counts = Counter()

    def observe_screen(self, report, variant, config):
        if variant is not FROZEN_CHAMPION_F.variant:
            raise AssertionError("Champion identity drift")
        records = report.records
        evaluations = [(r, evaluate_variant_entry(r, variant, config)) for r in records]
        eligible = sorted(((r, e) for r, e in evaluations if e.eligible),
                          key=lambda pair: (-pair[1].score, pair[0].symbol))
        universe_rejections = {
            "insufficient_market_history", "invalid_or_low_price", "missing_or_small_market_cap",
            "insufficient_liquidity", "reit_excluded", "financial_excluded", "universe_filter_failed",
            "stale_market_data", "identity_conflict", "analysis_error",
        }
        universe = [r for r in records if not universe_rejections.intersection(r.exclusion_reasons)]
        fundamental = [(r, e) for r, e in evaluations
                       if not entry_blocking_reasons(r.exclusion_reasons)
                       and r.scores.quality.score is not None
                       and r.scores.quality.score >= config.backtest.min_quality_score
                       and r.scores.valuation.score is not None
                       and r.scores.valuation.score >= config.backtest.min_valuation_score]
        momentum = [(r, e) for r, e in fundamental
                    if evaluate_strategy_gate(r.technical, variant, config).first_failure is None]
        with_pit = [r for r in records
                    if self.first_filed.get(r.symbol, date.max) <= report.as_of]
        q = [r for r in records if r.scores.quality.score is not None]
        v = [r for r in records if r.scores.valuation.score is not None]
        usable = [r for r in universe if r.pit_fact_count > 0
                  and r.scores.quality.score is not None and r.scores.valuation.score is not None]
        row = {
            "session": report.as_of.isoformat(), "analyzed_symbols": report.analyzed_count,
            "passing_universe_filters": len(universe), "valid_quality": len(q),
            "valid_valuation": len(v), "passing_f_fundamental_thresholds": len(fundamental),
            "passing_f_momentum_gate": len(momentum), "final_eligible_f_candidates": len(eligible),
            "symbols_with_any_stored_pit_facts": len(with_pit),
            "symbols_without_any_stored_pit_facts": report.analyzed_count - len(with_pit),
            "symbols_with_f_engine_pit_fundamentals": sum(r.pit_fact_count > 0 for r in records),
            "symbols_with_usable_pit_quality_and_valuation": len(usable),
            "rejected_insufficient_history": sum("insufficient_market_history" in r.exclusion_reasons for r in records),
            "rejected_stale_daily": sum("stale_market_data" in r.exclusion_reasons for r in records),
            "identity_conflicts": report.identity_conflicts_excluded,
        }
        self.rows.append(row)
        for rank, (record, evaluation) in enumerate(eligible, 1):
            if (record.market_history_count < config.data_quality.min_market_history_days
                    or record.technical.market_session != report.as_of
                    or record.latest_pit_filing_date is None
                    or record.latest_pit_filing_date > report.as_of):
                raise AssertionError("Invalid eligible F history / PIT input")
            self.validation_counts["eligible_candidate_pit_and_history_checks"] += 1
            self.entry_context[(report.as_of, record.symbol)] = {
                "candidate_rank": rank, "candidate_count": len(eligible),
                "f_score": evaluation.score, "quality": record.scores.quality.score,
                "valuation": record.scores.valuation.score,
                "latest_pit_filing_date": record.latest_pit_filing_date.isoformat(),
                "market_history_count": record.market_history_count,
            }
        if report.as_of.year == 2022:
            for label, members in (("analyzed", records), ("any_pit", with_pit),
                                   ("engine_pit", [r for r in records if r.pit_fact_count > 0]),
                                   ("quality", q), ("valuation", v), ("usable_pit", usable)):
                self.unique[label].update(r.symbol for r in members)
        if len(self.rows) % 25 == 0:
            print(f"screen {report.as_of}: {len(self.rows)} sessions; {len(eligible)} candidates", flush=True)

    def observe_portfolio_decision(self, *args, **kwargs):
        pass

    def observe_execution(self, *args, **kwargs):
        pass


def calendar_metrics(result, start, end):
    points = [p for p in result.equity_curve if start <= p.date <= end]
    prior = [p for p in result.equity_curve if p.date < start]
    initial = prior[-1].portfolio_equity if prior else result.initial_capital
    closed = [p for p in result.positions if start <= p.exit_date <= end]
    entered = [p for p in result.positions if start <= p.entry_date <= end]
    stats = trade_stats(closed)
    stats.update({
        "start": points[0].date.isoformat(), "end": points[-1].date.isoformat(),
        "sessions": len(points), "return": points[-1].portfolio_equity / initial - 1,
        "max_drawdown": maximum_drawdown([initial] + [p.portfolio_equity for p in points]),
        "average_exposure": mean(p.exposure for p in points),
        "end_of_day_exposure": mean(p.end_of_day_exposure for p in points),
        "realized_net_pnl": sum(p.net_pnl for p in closed),
        "equity_change_net_pnl": points[-1].portfolio_equity - initial,
        "trades_entered": len(entered), "trade_frequency_per_252_sessions": len(entered) / len(points) * 252,
    })
    return stats


def attribution_tables(result, regimes, observer):
    entries = []
    for p in result.positions:
        context = observer.entry_context[(p.signal_date, p.symbol)]
        entries.append({
            "position_id": p.position_id, "symbol": p.symbol, "signal_date": p.signal_date,
            "entry_date": p.entry_date, "exit_date": p.exit_date,
            **context, "position_return": p.position_return, "net_pnl": p.net_pnl,
            "holding_sessions": p.holding_days,
            **{f"signal_{k}": v for k, v in regimes[p.signal_date].items()},
            # At the next-session open the latest COMPLETED SPY label remains T's.
            "entry_known_regime_a": regimes[p.signal_date]["regime_a"],
            "entry_known_regime_b": regimes[p.signal_date]["regime_b"],
            "entry_known_regime_c": regimes[p.signal_date]["regime_c"],
            "holding_regime_transitions": sum(
                regimes[a]["regime_c"] != regimes[b]["regime_c"]
                for a, b in zip(
                    [d for d in regimes if p.entry_date <= d < p.exit_date],
                    [d for d in regimes if p.entry_date < d <= p.exit_date],
                )
            ),
        })
    sessions = []
    previous = result.initial_capital
    for point in result.equity_curve:
        active = [p for p in result.positions if p.entry_date <= point.date <= p.exit_date]
        pnl = point.portfolio_equity - previous
        prior_session = next((d for d in reversed(regimes) if d < point.date), None)
        sessions.append({
            "session": point.date.isoformat(), **regimes[point.date],
            "portfolio_equity": point.portfolio_equity, "prior_equity": previous,
            "portfolio_session_return": point.portfolio_equity / previous - 1,
            "net_pnl": pnl, "session_exposure": point.exposure,
            "end_of_day_exposure": point.end_of_day_exposure,
            "positions_active_during_session": len(active), "positions_active_eod": point.active_positions,
            "trades_entered": sum(p.entry_date == point.date for p in active),
            "realized_net_pnl": sum(p.net_pnl for p in result.positions if p.exit_date == point.date),
            "active_positions_entered_in_other_regime": sum(
                regimes[p.signal_date]["regime_c"] != regimes[point.date]["regime_c"] for p in active),
            "changed_regime_since_previous_session": prior_session is not None
                and regimes[prior_session]["regime_c"] != regimes[point.date]["regime_c"],
            "lagged_regime_a": regimes[prior_session]["regime_a"] if prior_session else None,
            "lagged_regime_b": regimes[prior_session]["regime_b"] if prior_session else None,
            "lagged_regime_c": regimes[prior_session]["regime_c"] if prior_session else None,
        })
        previous = point.portfolio_equity
    summaries = []
    for period, first, last in (("ALL", START, END), ("2022", START, date(2022, 12, 31)),
                                ("2023_2026", date(2023, 1, 1), END)):
        selected = [s for s in sessions if first.isoformat() <= s["session"] <= last.isoformat()]
        for dim in ("regime_a", "regime_b", "regime_c"):
            for regime in sorted({r[dim] for r in regimes.values()}):
                members = [s for s in selected if s[dim] == regime]
                positions = [p for p in result.positions if first <= p.entry_date <= last
                             and regimes[p.signal_date][dim] == regime]
                stats = trade_stats(positions)
                summaries.append({
                    "period": period, "dimension": dim, "regime": regime,
                    "sessions": len(members), "trades_entered": len(positions),
                    "positions_active_session_observations": sum(s["positions_active_during_session"] for s in members),
                    "sessions_with_active_positions": sum(s["positions_active_during_session"] > 0 for s in members),
                    "portfolio_session_return_arithmetic_mean": mean(s["portfolio_session_return"] for s in members) if members else None,
                    "portfolio_session_return_compounded_attribution": math.prod(1 + s["portfolio_session_return"] for s in members) - 1,
                    "net_pnl_session_mtm": sum(s["net_pnl"] for s in members),
                    "session_gains": sum(max(s["net_pnl"], 0) for s in members),
                    "session_losses": sum(min(s["net_pnl"], 0) for s in members),
                    "entry_cohort_net_pnl": stats["net_pnl"], "entry_cohort_win_rate": stats["win_rate"],
                    "entry_cohort_profit_factor": stats["profit_factor"],
                    "entry_cohort_average_trade_return": stats["average_trade_return"],
                    "average_exposure": mean(s["session_exposure"] for s in members) if members else None,
                    "maximum_drawdown": "NOT_DIRECTLY_COMPARABLE",
                })
    return entries, sessions, summaries


def main():
    OUT.mkdir(exist_ok=True)
    if [p for p in OUT.glob(f"{STEM}_*") if p.suffix in {".json", ".csv", ".md"}]:
        raise FileExistsError(f"Refusing to overwrite existing audit artifacts: {STEM}")
    started = time.monotonic()
    config = StrategyConfig.model_validate(_read_yaml(ROOT / "config/strategy.yaml"))
    validate_champion_config(config)
    config_before = config.model_dump(mode="json")
    database = ReadOnlyDatabase(ROOT / config.storage.database_path)
    source_files = sorted((ROOT / "src/trading_system").rglob("*.py")) + [ROOT / "config/strategy.yaml"]
    source_hashes = {str(p.relative_to(ROOT)): sha256(p) for p in source_files}
    print("Hashing local market database (read-only)", flush=True)
    db_before = sha256(database.path)
    with offline_guard() as safety:
        print("Qualifying requested initial session", flush=True)
        qualification = qualify_historical_screen_start(database, config, START, END, allow_start_shift=True)
        companies = database.list_tradable_companies()
        with database.read_only() as connection:
            warmup = {r[0]: (r[1], r[2]) for r in connection.execute(
                "SELECT symbol,COUNT(*),MAX(substr(timestamp,1,10)) FROM bars "
                "WHERE timeframe='1d' AND timestamp<? GROUP BY symbol", (START.isoformat(),))}
            first_filed = {r[0]: date.fromisoformat(r[1]) for r in connection.execute(
                "SELECT symbol,MIN(filed) FROM fundamental_facts GROUP BY symbol")}
            spy = [(date.fromisoformat(r[0][:10]), float(r[1])) for r in connection.execute(
                "SELECT timestamp,close FROM bars WHERE symbol='SPY' AND timeframe='1d' "
                "AND timestamp<? ORDER BY timestamp", ((END + timedelta(days=1)).isoformat(),))]
            bounds = dict(connection.execute(
                "SELECT 'first',MIN(timestamp) FROM bars WHERE timeframe='1d' UNION ALL "
                "SELECT 'last',MAX(timestamp) FROM bars WHERE timeframe='1d'"))
        sessions = trading_sessions_between(START, END)
        local_sessions = database.bar_sessions(START, END)
        missing_global = sorted(set(sessions) - set(local_sessions))
        regimes = spy_regimes(spy, sessions)
        qualification.update({
            "first_requested_session": START,
            "first_effectively_evaluable_session": qualification["actual_first_qualified_screen_session"],
            "current_analyzed_company_universe": len(companies),
            "symbols_with_300_prior_bars_at_start": sum(warmup.get(c.symbol, (0, None))[0] >= 300 for c in companies),
            "symbols_with_any_pit_facts_at_start": sum(first_filed.get(c.symbol, date.max) <= START for c in companies),
            "symbols_without_any_pit_facts_at_start": sum(first_filed.get(c.symbol, date.max) > START for c in companies),
            "missing_global_xnys_sessions": missing_global, "spy_regime_sessions_qualified": len(regimes),
            "daily_database_bounds": bounds, "database_sha256_before": db_before,
            "history_rule": "Unchanged F bar-count/freshness gates; exact initial warmup checked additionally. No filling.",
            "pit_rule": "filed <= as_of unchanged; scores only where the canonical F source evaluates fundamentals",
            "known_biases": BIASES,
        })
        if not qualification["qualified"] or qualification["actual_first_qualified_screen_session"] != START.isoformat():
            write_json("data_qualification", qualification)
            raise RuntimeError("Requested initial F session not qualified; no economic run performed")
        if missing_global:
            write_json("data_qualification", qualification)
            raise RuntimeError("Missing global portfolio sessions; no economic run performed")
        print(f"Initial qualification passed: {qualification['screenable_symbol_count_at_start']} exact warmups", flush=True)
        print("Preparing unchanged canonical historical feature source", flush=True)
        source = HistoricalFeatureScreenSource(database, config, START, END)
        qualified_sessions = []
        for session in sessions:
            markets = [features.get(session) for symbol, features in source._market.items()
                       if symbol not in source._static_exclusions]
            qualified_sessions.append({
                "session": session.isoformat(),
                "members_after_static_and_identity": source.diagnostics.companies_after_static,
                "symbols_with_300_bars_in_actual_source": sum(m is not None and m.history_count >= 300 for m in markets),
                "symbols_with_fresh_daily_and_300_bars": sum(m is not None and m.history_count >= 300
                    and m.technical.market_session == session for m in markets),
                "symbols_with_stale_daily": sum(m is not None and m.technical.market_session != session for m in markets),
                "symbols_without_source_daily": source.diagnostics.companies_after_static - sum(m is not None for m in markets),
            })
        qualification["all_session_history_qualification"] = {
            "sessions": len(qualified_sessions),
            "minimum_fresh_warm_symbols": min(r["symbols_with_fresh_daily_and_300_bars"] for r in qualified_sessions),
            "initial_session": qualified_sessions[0],
            "method": "Actual canonical source history counts and freshness, per session before economic execution",
        }
        assert all(r["symbols_with_fresh_daily_and_300_bars"] > 0 for r in qualified_sessions)
        write_csv("qualification_sessions", qualified_sessions)
        observer = AuditObserver(first_filed)
        first = source.screen(START)
        observer.observe_screen(first, FROZEN_CHAMPION_F.variant, config)
        qualification["initial_f_screen"] = observer.rows[0]
        qualification["symbols_with_usable_pit_fundamentals_at_start"] = observer.rows[0]["symbols_with_usable_pit_quality_and_valuation"]
        write_json("data_qualification", qualification)
        # Reuse the exact first report, without double-counting the diagnostic observer.
        class FirstScreenCache:
            diagnostics = source.diagnostics

            def screen(self, session):
                return first if session == START else source.screen(session)

        class EngineObserver:
            def observe_screen(self, report, variant, config):
                if report.as_of != START:
                    observer.observe_screen(report, variant, config)

            observe_portfolio_decision = observer.observe_portfolio_decision
            observe_execution = observer.observe_execution

        print("Starting single F/configured/C1 economic path", flush=True)
        result = BacktestEngine(database, config, screen_source=FirstScreenCache(),
                                audit_observer=EngineObserver()).run(
            START, END, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset)
        # Final-session candidates are diagnostic only: the engine correctly queues no final signal.
        observer.observe_screen(source.screen(END), FROZEN_CHAMPION_F.variant, config)
        assert result.strategy_variant is FROZEN_CHAMPION_F.variant
        assert result.position_management_preset is FROZEN_CHAMPION_F.preset
        assert config.model_dump(mode="json") == config_before
        assert result.configuration["portfolio"]["max_positions"] == 1
        assert max(p.active_positions for p in result.equity_curve) <= 1
        entries, session_rows, regime_rows = attribution_tables(result, regimes, observer)
        assert all(r["candidate_rank"] == 1 for r in entries)
        # Inspect actual held paths without changing missing-bar historical semantics.
        held_gaps = []
        for p in result.positions:
            with database.read_only() as connection:
                available = {date.fromisoformat(r[0][:10]) for r in connection.execute(
                    "SELECT timestamp FROM bars WHERE symbol=? AND timeframe='1d' "
                    "AND timestamp>=? AND timestamp<?",
                    (p.symbol, p.entry_date.isoformat(), (p.exit_date + timedelta(days=1)).isoformat()),
                )}
            for d in trading_sessions_between(p.entry_date, p.exit_date):
                if d not in available:
                    held_gaps.append({"position_id": p.position_id, "symbol": p.symbol, "session": d})
        yearly = []
        for year in range(2022, 2027):
            row = {"year": year, **calendar_metrics(result, max(START, date(year, 1, 1)), min(END, date(year, 12, 31)))}
            sr = [s for s in session_rows if s["session"].startswith(str(year))]
            for dimension, label in (("regime_a", "ABOVE_SMA200"), ("regime_b", "POSITIVE_MOM126")):
                row[f"fraction_{label.lower()}"] = mean(s[dimension] == label for s in sr)
                row[f"fraction_{'below_equal_sma200' if dimension == 'regime_a' else 'non_positive_mom126'}"] = 1 - row[f"fraction_{label.lower()}"]
            yearly.append(row)
        monthly = []
        for month in sorted({p.date.strftime("%Y-%m") for p in result.equity_curve}):
            year, m = map(int, month.split("-"))
            first_day = date(year, m, 1)
            last_day = (date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)) - timedelta(days=1)
            monthly.append({"month": month, **calendar_metrics(result, max(START, first_day), min(END, last_day))})
        for row in monthly:
            row["best_month"] = row is max(monthly, key=lambda x: x["return"])
            row["worst_month"] = row is min(monthly, key=lambda x: x["return"])
            row["sign"] = "POSITIVE" if row["return"] > 0 else "NEGATIVE" if row["return"] < 0 else "ZERO"
        overall = {
            **result.metrics.model_dump(mode="json"), **trade_stats(result.positions),
            "average_position_return": result.position_metrics.average_position_return,
            "median_position_return": median(p.position_return for p in result.positions) if result.positions else None,
            "commission_costs": sum(p.transaction_cost for p in result.positions),
            "ending_equity": result.equity_curve[-1].portfolio_equity,
            "initial_capital": result.initial_capital,
        }
        a22 = [r for r in observer.rows if r["session"].startswith("2022")]
        arest = [r for r in observer.rows if not r["session"].startswith("2022")]
        comparison = {
            "2022": {**calendar_metrics(result, START, date(2022, 12, 31)), **candidate_aggregation(a22)},
            "2023_2026": {**calendar_metrics(result, date(2023, 1, 1), END), **candidate_aggregation(arest)},
        }
        comparison["rest_minus_2022_deltas"] = {
            k: comparison["2023_2026"][k] - comparison["2022"][k]
            for k in ("return", "profit_factor", "win_rate", "average_trade_return",
                      "trade_frequency_per_252_sessions", "average_exposure", "mean_eligible_candidates_per_session")
            if comparison["2023_2026"][k] is not None and comparison["2022"][k] is not None
        }
        for key, first_day, last_day in (("2022", START, date(2022, 12, 31)), ("2023_2026", date(2023, 1, 1), END)):
            members = [s for s in session_rows if first_day.isoformat() <= s["session"] <= last_day.isoformat()]
            comparison[key]["spy_regime_mix"] = {r: mean(s["regime_c"] == r for s in members) for r in ("BULL", "MIXED", "WEAK")}
        deep_dive = {
            **comparison["2022"], "pit_unique_symbols": {k: len(v) for k, v in observer.unique.items()},
            "symbols_without_any_pit_facts_entire_2022": len(observer.unique["analyzed"] - observer.unique["any_pit"]),
            "first_session_availability": a22[0], "last_session_availability": a22[-1],
            "monthly": [r for r in monthly if r["month"].startswith("2022")],
            "regimes": [r for r in regime_rows if r["period"] == "2022"],
            "trades": [r for r in entries if r["entry_date"].year == 2022],
        }
        write_json("2022_deep_dive", deep_dive)
        write_csv("yearly", yearly)
        write_csv("monthly", monthly)
        write_csv("candidate_availability", observer.rows)
        write_csv("trade_entry_regimes", entries)
        write_csv("regime_session_attribution", session_rows)
        write_csv("regime_summary", regime_rows)
        write_csv("equity", [p.model_dump(mode="json") for p in result.equity_curve])
        write_csv("positions", [p.model_dump(mode="json", exclude={"score_history"}) for p in result.positions])
        write_json("engine_result", result.model_dump(mode="json"))
        print("Economic path complete; verifying immutable inputs", flush=True)
        assert all(sha256(ROOT / p) == digest for p, digest in source_hashes.items())
        db_after = sha256(database.path)
        assert db_before == db_after
        assert abs(sum(s["net_pnl"] for s in session_rows) - (overall["ending_equity"] - result.initial_capital)) < 1e-8
        assert abs(sum(p.net_pnl for p in result.positions) - (overall["ending_equity"] - result.initial_capital)) < 1e-8
        summary = {
            "strategy": FROZEN_CHAMPION_F.production_label, "requested_start": START, "requested_end": END,
            "actual_start": result.actual_start, "actual_end": result.actual_end,
            "overall": overall, "yearly": yearly, "comparison": comparison,
            "candidate_availability_all": candidate_aggregation(observer.rows),
            "monthly_positive": sum(r["sign"] == "POSITIVE" for r in monthly),
            "monthly_negative": sum(r["sign"] == "NEGATIVE" for r in monthly),
            "monthly_zero": sum(r["sign"] == "ZERO" for r in monthly),
            "best_month": max(monthly, key=lambda r: r["return"]),
            "worst_month": min(monthly, key=lambda r: r["return"]),
            "held_daily_gaps": held_gaps, "safety": safety,
            "database_sha256_before": db_before, "database_sha256_after": db_after,
            "source_file_sha256": source_hashes, "configuration": result.configuration,
            "validation_counts": dict(observer.validation_counts), "known_biases": BIASES,
            "performance_diagnostics": result.performance_diagnostics,
            "engine_warnings": result.warnings,
            "elapsed_seconds": time.monotonic() - started,
            "interpretation": "POST_HOC_DIAGNOSTIC_ATTRIBUTION_ONLY; no entry filter or strategy recommendation",
        }
        write_json("summary", summary)
        print(json.dumps({"overall": overall, "yearly": yearly, "held_gaps": held_gaps, "safety": safety}, default=str), flush=True)


if __name__ == "__main__":
    main()
