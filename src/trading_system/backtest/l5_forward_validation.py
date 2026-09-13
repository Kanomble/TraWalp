"""Two frozen Daily portfolios on a forward holdout; no selection or promotion."""

import json
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trading_system.backtest.capacity_validation import _single_result_comparison
from trading_system.backtest.engine import BacktestEngine, _backtest_sessions
from trading_system.backtest.f_candidates import ManifestScreenSource, ReplaySpool
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.backtest.lifecycle import F_LIFECYCLE_VARIANTS, lifecycle_strategy_config
from trading_system.backtest.lifecycle_validation import _qualify_daily_research, iter_f_candidates
from trading_system.backtest.peer_context import (
    PEER_MEMBERSHIP_BASIS,
    TechnicalPeerContextProvider,
)
from trading_system.backtest.progress import ProgressPhase
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.backtest.validation import (
    _field_union,
    calendar_stability,
    chronological_subperiod_analysis,
    strategy_summary,
    symbol_and_leave_one_out,
)
from trading_system.data.market_sessions import daily_warmup_start
from trading_system.fundamentals.peers import normalize_sic

F_L5_FORWARD_RESEARCH_FAMILY = "research-f-lifecycle-l5-forward-v1"
FORWARD_START = date(2024, 8, 13)
# These are the registered canonical presets, not new lifecycle implementations.
FORWARD_VARIANTS = (
    ("L0_FORWARD", F_LIFECYCLE_VARIANTS[0]),
    ("L5_FORWARD", F_LIFECYCLE_VARIANTS[5]),
)
LIFECYCLE_EVENT_FIELDS = (
    "strategy",
    "position_id",
    "symbol",
    "session",
    "holding_day",
    "trend_health",
    "peer_state",
    "holding_extended",
    "profit_target_deferred",
    "decision",
    "exit_reason",
    "queued_exit_reason",
)
TABLE_NAMES = (
    "metrics",
    "positions",
    "execution_legs",
    "equity_curve",
    "monthly",
    "yearly",
    "chronological_subperiods",
    "symbol_concentration",
    "lifecycle_events",
)


def validate_forward_window(start, end):
    if start < FORWARD_START:
        raise ValueError("L5 forward start must be on or after 2024-08-13")
    if start > end:
        raise ValueError("L5 forward start must not be after end")


@dataclass(frozen=True, slots=True)
class _PeerBar:
    timestamp: datetime
    high: float
    low: float
    close: Decimal
    volume: int


class _ForwardPeerContext(TechnicalPeerContextProvider):
    """Batch preparation only; inherit canonical technical, trend and peer statistics.

    Preserve all current peer members (including financials/REITs excluded from F
    entries). The bounded Daily range includes the exact slope warmup. Every lookup
    still slices at the observed session; missing sessions stay missing. No lazy SQL.
    """

    def __init__(self, database, config, start, end):
        super().__init__(database, config, end)
        self._sics = {c.symbol: normalize_sic(c.sic) for c in self.companies}
        self._histories = {c.symbol: [] for c in self.companies}
        self.bar_batch_queries = 0
        warmup = daily_warmup_start(start, max(25, 20 + config.technical.sma_slope_lookback))
        for batch in database.iter_bar_value_batches(self._histories, warmup, end):
            self.bar_batch_queries += 1
            for symbol, timestamp, high, low, close, volume in batch:
                self._histories[symbol].append(
                    _PeerBar(
                        datetime.fromisoformat(timestamp),
                        float(high),
                        float(low),
                        Decimal(str(close)),
                        int(volume),
                    )
                )
        self._dates = {
            symbol: [bar.timestamp.date() for bar in bars]
            for symbol, bars in self._histories.items()
        }

    def history(self, symbol, session, count=None):
        index = bisect_right(self._dates.get(symbol, ()), min(session, self.end))
        return self._histories.get(symbol, ())[max(0, index - count) if count else 0 : index]

    def _session_groups(self, session):
        # Same canonical min=4 including self; context() excludes self and requires
        # three OTHER peers. Broad baskets also contain narrower-group members.
        available = [
            c.symbol for c in self.companies if self.technical(c.symbol, session) is not None
        ]
        members = {}
        for symbol in available:
            if sic := self._sics[symbol]:
                for width in (4, 3, 2):
                    members.setdefault(f"sic{width}:{sic[:width]}", []).append(symbol)
        groups = {}
        for symbol in available:
            sic = self._sics[symbol]
            groups[symbol] = (
                next(
                    (
                        f"sic{w}:{sic[:w]}"
                        for w in (4, 3, 2)
                        if len(members[f"sic{w}:{sic[:w]}"]) >= 4
                    ),
                    f"sic2:{sic[:2]}",
                )
                if sic
                else None
            )
        return groups, members


@dataclass
class L5ForwardBundle:
    summary: dict
    results: dict
    tables: dict


def _tables(results, events):
    tables = {name: [] for name in TABLE_NAMES}
    tables["lifecycle_events"] = events
    for label, result in results.items():
        comparison = _single_result_comparison(result)
        monthly, _ = calendar_stability(comparison, "month")
        yearly, _ = calendar_stability(comparison, "year")
        symbols, _, _ = symbol_and_leave_one_out(comparison)
        rows = {
            "metrics": [strategy_summary(label, result)],
            "positions": [p.model_dump(mode="json") for p in result.positions],
            "execution_legs": [p.model_dump(mode="json") for p in result.trades],
            "equity_curve": [p.model_dump(mode="json") for p in result.equity_curve],
            "monthly": monthly,
            "yearly": yearly,
            "chronological_subperiods": chronological_subperiod_analysis(comparison),
            "symbol_concentration": symbols,
        }
        for name, values in rows.items():
            tables[name].extend({**row, "strategy": label} for row in values)
    return tables


def _comparison(results, metric_rows):
    before, after = metric_rows
    metrics = {
        key: "position_profit_factor" if key == "profit_factor" else key
        for key in (
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
        )
    }
    old, new = (
        {(p.signal_date, p.symbol, p.entry_timestamp): p for p in results[label].positions}
        for label in ("L0_FORWARD", "L5_FORWARD")
    )
    shared = old.keys() & new.keys()
    return {
        "comparison": "L5_FORWARD - L0_FORWARD",
        "primary": True,
        "metric_deltas": {
            key: after[field] - before[field]
            if after[field] is not None and before[field] is not None
            else None
            for key, field in metrics.items()
        },
        "shared_entry_positions": len(shared),
        "shared_entry_net_pnl_delta": sum(new[k].net_pnl - old[k].net_pnl for k in sorted(shared)),
        "unique_path_net_pnl_delta": (
            sum(new[k].net_pnl for k in sorted(new.keys() - shared))
            - sum(old[k].net_pnl for k in sorted(old.keys() - shared))
        ),
    }


def run_f_l5_forward(database, config, start, end, *, preparation=None):
    validate_forward_window(start, end)
    timings = {}
    with ProgressPhase("l5_forward_progress", "qualification") as phase:
        qualification = _qualify_daily_research(database, config, start, end)
        if qualification["failure_reasons"]:
            raise ValueError(
                "Daily qualification failed: " + "; ".join(qualification["failure_reasons"])
            )
    timings["qualification_seconds"] = phase.seconds
    spool = ReplaySpool()
    try:
        with ProgressPhase("l5_forward_progress", "candidate_discovery") as phase:
            sessions = (
                preparation.sessions
                if preparation is not None
                else tuple(_backtest_sessions(database, start, end))
            )
            source = (
                preparation.screen_source
                if preparation is not None
                else HistoricalFeatureScreenSource(database, config, start, end)
            )
            phase.update(sessions_total=max(0, len(sessions) - 1))
            candidate_count = sum(
                1
                for _ in iter_f_candidates(
                    source, config, sessions, replay_spool=spool, progress=phase
                )
            )
        timings["candidate_discovery_seconds"] = phase.seconds
        replay = ManifestScreenSource(None, spool.offsets, stream=spool.file)
        del source, preparation
        with ProgressPhase("l5_forward_progress", "peer_preparation") as phase:
            context = _ForwardPeerContext(database, config, start, end)
        timings["peer_preparation_seconds"] = phase.seconds
        timings["peer_bar_batch_queries"] = context.bar_batch_queries
        results, events = {}, []
        for label, preset in FORWARD_VARIANTS:
            with ProgressPhase("l5_forward_progress", label.lower()) as phase:
                engine = BacktestEngine(
                    database,
                    lifecycle_strategy_config(config, preset),
                    screen_source=replay,
                    lifecycle_preset=preset,
                    lifecycle_context=context,
                    require_complete_daily_position_bars=True,
                    progress=phase,
                )
                results[label] = engine.run(
                    start, end, variant=FROZEN_CHAMPION_F.variant, preset=FROZEN_CHAMPION_F.preset
                )
            timings[f"{label.lower()}_seconds"] = phase.seconds
            events.extend(
                {**row, "strategy": label}
                for row in getattr(engine.position_manager, "trend_events", ())
            )
        # Only canonical lifecycle decision events; no peer spillover, correlations,
        # forward peer returns, dynamic-profit or intraday diagnostic passes.
        with ProgressPhase("l5_forward_progress", "diagnostics") as phase:
            tables = _tables(results, events)
            day10 = [
                row
                for row in events
                if row["strategy"] == "L5_FORWARD" and row["holding_day"] == 10
            ]
            granted = sum(row["holding_extended"] for row in day10)
            summary = {
                "research_family": F_L5_FORWARD_RESEARCH_FAMILY,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "period_classification": "FORWARD HOLDOUT / RESEARCH",
                "forward_holdout": True,
                "clean_oos": False,
                "frozen_champion": "F/configured/C1",
                "frozen_champion_unchanged": True,
                "challenger": "F/L5/C1 (canonical F-LIFECYCLE-L5)",
                "automatic_winner_selection": False,
                "universe_membership_basis": PEER_MEMBERSHIP_BASIS,
                "survivorship_limitation": (
                    "Current local tradable universe; historical survivorship bias is unresolved"
                ),
                "local_only": True,
                "network_accessed": False,
                "daily_qualification": qualification,
                "variants": [
                    {"strategy": label, **asdict(preset)} for label, preset in FORWARD_VARIANTS
                ],
                "candidate_count": candidate_count,
                "metrics": tables["metrics"],
                "configurations": {label: r.configuration for label, r in results.items()},
                "comparison": _comparison(results, tables["metrics"]),
                "day10_positions_evaluated": len(day10),
                "l5_extensions_granted": granted,
                "l5_extensions_denied": len(day10) - granted,
                "l5_extension_rate": granted / len(day10) if day10 else None,
                "warnings": sorted({w for r in results.values() for w in r.warnings}),
            }
        timings["diagnostics_seconds"] = phase.seconds
        timings["peer_group_sessions_built"] = context._groups.cache_info().misses
        timings["replay_records"] = spool.records
        timings["replay_bytes"] = spool.bytes
        summary["performance"] = timings
        return L5ForwardBundle(summary, results, tables)
    finally:
        spool.file.close()


def l5_forward_output_paths(directory: Path, stem: str):
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("output-stem must be a plain file stem")
    paths = {
        name: directory / f"{stem}_{name}"
        for name in ("summary.json", *(f"{n}.csv" for n in TABLE_NAMES))
    }
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("L5 forward output already exists; use a fresh output-stem")
    return paths


def export_f_l5_forward(bundle, directory: Path, *, stem: str):
    paths = l5_forward_output_paths(directory, stem)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in bundle.tables.items():
        fields = LIFECYCLE_EVENT_FIELDS if name == "lifecycle_events" else ("strategy",)
        _atomic_csv(paths[f"{name}.csv"], rows, list(dict.fromkeys([*fields, *_field_union(rows)])))
    _atomic_text(paths["summary.json"], json.dumps(bundle.summary, indent=2, allow_nan=False))
    return paths
