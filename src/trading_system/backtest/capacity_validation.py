"""Controlled F/configured portfolio-capacity research and reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

from trading_system.backtest.engine import BacktestEngine, prepare_strategy_comparison
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.research_registry import (
    F_CAPACITY_RESEARCH_FAMILY,
    F_CAPACITY_RESEARCH_VARIANTS,
    FROZEN_CHAMPION_F,
    FCapacityResearchVariant,
)
from trading_system.backtest.universe_provenance import audit_universe_provenance
from trading_system.backtest.validation import (
    CANONICAL_COST_STRESS_CASES,
    _cost_row,
    _field_union,
    calendar_stability,
    chronological_subperiod_analysis,
    strategy_summary,
    symbol_and_leave_one_out,
)
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.models.backtest import (
    BacktestPosition,
    BacktestResult,
    BacktestTrade,
    StrategyComparison,
    StrategyComparisonKind,
)


@dataclass(frozen=True)
class FCapacityResearchBundle:
    """Aggregate evidence for the four pre-registered capacity variants."""

    requested_start: date
    requested_end: date
    results: dict[int, BacktestResult]
    capacity_rows: list[dict[str, Any]]
    utilization_rows: list[dict[str, Any]]
    entry_rank_rows: list[dict[str, Any]]
    monthly_rows: list[dict[str, Any]]
    yearly_rows: list[dict[str, Any]]
    chronological_subperiod_rows: list[dict[str, Any]]
    symbol_rows: list[dict[str, Any]]
    concentration: dict[str, Any]
    time_stability: dict[str, Any]
    cost_rows: list[dict[str, Any]]
    universe_provenance: dict[str, Any]
    period_classification: str


def capacity_strategy_config(config: StrategyConfig, max_positions: int) -> StrategyConfig:
    """Copy the frozen configuration while overriding only portfolio capacity."""

    if max_positions not in {item.max_positions for item in F_CAPACITY_RESEARCH_VARIANTS}:
        raise ValueError("F capacity research permits only max_positions 1, 2, 3, or 5")
    return config.model_copy(
        update={"portfolio": config.portfolio.model_copy(update={"max_positions": max_positions})}
    )


def capacity_utilization_row(
    label: str,
    result: BacktestResult,
    configured_max_positions: int,
) -> dict[str, Any]:
    """Summarize end-of-day capacity usage from the existing equity curve."""

    observations = [point.active_positions for point in result.equity_curve]
    average_positions = mean(observations) if observations else None
    row: dict[str, Any] = {
        "research_family": F_CAPACITY_RESEARCH_FAMILY,
        "strategy": label,
        "configured_max_positions": configured_max_positions,
        "observation_basis": "end_of_day_equity_curve",
        "sessions_observed": len(observations),
        "maximum_simultaneous_positions_observed": max(observations, default=0),
        "average_simultaneous_positions": average_positions,
        "median_simultaneous_positions": median(observations) if observations else None,
        "sessions_at_capacity": sum(value >= configured_max_positions for value in observations),
        "sessions_at_capacity_rate": (
            sum(value >= configured_max_positions for value in observations) / len(observations)
            if observations
            else None
        ),
        "capacity_utilization_rate": (
            average_positions / configured_max_positions if average_positions is not None else None
        ),
        "average_portfolio_exposure": result.metrics.exposure,
        "end_of_day_exposure": result.metrics.end_of_day_exposure,
        "sessions_above_configured_capacity": sum(
            value > configured_max_positions for value in observations
        ),
    }
    row.update(
        {
            f"sessions_with_{level}_positions": observations.count(level)
            for level in range(configured_max_positions + 1)
        }
    )
    return row


def entry_rank_analysis_rows(
    label: str,
    result: BacktestResult,
    configured_max_positions: int,
) -> list[dict[str, Any]]:
    """Aggregate closed-position outcomes by the unchanged F candidate rank at entry."""

    ranked_positions: dict[int, list[BacktestPosition]] = {}
    for position in result.positions:
        if position.daily_candidate_rank is not None:
            ranked_positions.setdefault(position.daily_candidate_rank, []).append(position)
    maximum_rank = max(5, max(ranked_positions, default=0))
    rows: list[dict[str, Any]] = []
    for rank in range(1, maximum_rank + 1):
        positions = ranked_positions.get(rank, [])
        pnls = [position.net_pnl for position in positions]
        rows.append(
            {
                "research_family": F_CAPACITY_RESEARCH_FAMILY,
                "strategy": label,
                "configured_max_positions": configured_max_positions,
                "candidate_rank_at_entry": rank,
                "positions": len(positions),
                "win_rate": (
                    sum(position.net_pnl > 0 for position in positions) / len(positions)
                    if positions
                    else None
                ),
                "average_return": (
                    mean(position.position_return for position in positions) if positions else None
                ),
                "net_pnl": sum(pnls),
                "profit_factor": _profit_factor(pnls),
                "average_holding_period": (
                    mean(position.holding_days for position in positions) if positions else None
                ),
                "mean_mfe": (
                    mean(position.maximum_favorable_excursion for position in positions)
                    if positions
                    else None
                ),
                "mean_mae": (
                    mean(position.maximum_adverse_excursion for position in positions)
                    if positions
                    else None
                ),
            }
        )
    return rows


def capacity_metric_rows(
    results: dict[int, BacktestResult],
    cost_rows: list[dict[str, Any]],
    *,
    time_stability: dict[str, Any] | None = None,
    concentration: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build comparable metrics and deltas without choosing a winning capacity."""

    _require_complete_capacity_set(results)
    rows: list[dict[str, Any]] = []
    stability = time_stability or {}
    concentrations = concentration or {}
    for identity in F_CAPACITY_RESEARCH_VARIANTS:
        result = results[identity.max_positions]
        label = identity.label
        row = strategy_summary(label, result)
        utilization = capacity_utilization_row(label, result, identity.max_positions)
        rank_counts = {
            rank: sum(position.daily_candidate_rank == rank for position in result.positions)
            for rank in range(1, 6)
        }
        monthly = stability.get(label, {}).get("monthly", {})
        yearly = stability.get(label, {}).get("yearly", {})
        concentration_row = concentrations.get(label, {})
        initial_position_notionals = [
            position.entry_price * position.initial_quantity for position in result.positions
        ]
        stressed = next(
            (
                cost
                for cost in cost_rows
                if cost["configured_max_positions"] == identity.max_positions
                and cost["cost_case"] == "3X_SLIPPAGE"
            ),
            None,
        )
        row.update(
            {
                "research_family": F_CAPACITY_RESEARCH_FAMILY,
                "research_identity": label,
                "strategy_composition": FROZEN_CHAMPION_F.label,
                "frozen_champion_control": identity.frozen_champion_control,
                "configured_max_positions": identity.max_positions,
                "profit_factor": row["position_profit_factor"],
                "unique_traded_symbols": len({position.symbol for position in result.positions}),
                "average_initial_position_notional": (
                    mean(initial_position_notionals) if initial_position_notionals else None
                ),
                "median_initial_position_notional": (
                    median(initial_position_notionals) if initial_position_notionals else None
                ),
                "maximum_simultaneous_positions_observed": utilization[
                    "maximum_simultaneous_positions_observed"
                ],
                "average_simultaneous_positions": utilization["average_simultaneous_positions"],
                "median_simultaneous_positions": utilization["median_simultaneous_positions"],
                "sessions_at_capacity": utilization["sessions_at_capacity"],
                "capacity_utilization_rate": utilization["capacity_utilization_rate"],
                "top_1_pnl_concentration": concentration_row.get("top_1_pnl_concentration"),
                "top_3_pnl_concentration": concentration_row.get("top_3_pnl_concentration"),
                "top_5_pnl_concentration": concentration_row.get("top_5_pnl_concentration"),
                "positive_months": monthly.get("positive_months"),
                "negative_months": monthly.get("negative_months"),
                "best_month": monthly.get("best_month"),
                "worst_month": monthly.get("worst_month"),
                "positive_years": yearly.get("positive_years"),
                "negative_years": yearly.get("negative_years"),
                "entries_with_candidate_rank": sum(
                    position.daily_candidate_rank is not None for position in result.positions
                ),
                "entries_without_candidate_rank": sum(
                    position.daily_candidate_rank is None for position in result.positions
                ),
                "rank_above_5_entries": sum(
                    position.daily_candidate_rank is not None and position.daily_candidate_rank > 5
                    for position in result.positions
                ),
                "profitable_under_15bps": (
                    stressed["total_return"] > 0
                    if stressed is not None and stressed["total_return"] is not None
                    else None
                ),
                "profit_factor_above_1_under_15bps": (
                    stressed["profit_factor"] > 1
                    if stressed is not None and stressed["profit_factor"] is not None
                    else None
                ),
                "sharpe_positive_under_15bps": (
                    stressed["sharpe"] > 0
                    if stressed is not None and stressed["sharpe"] is not None
                    else None
                ),
            }
        )
        row.update({f"rank_{rank}_entries": rank_counts[rank] for rank in range(1, 6)})
        rows.append(row)

    baseline = rows[0]
    delta_fields = {
        "return_delta_vs_capacity_1": "total_return",
        "cagr_delta_vs_capacity_1": "cagr",
        "max_drawdown_delta_vs_capacity_1": "max_drawdown",
        "sharpe_delta_vs_capacity_1": "sharpe",
        "sortino_delta_vs_capacity_1": "sortino",
        "profit_factor_delta_vs_capacity_1": "profit_factor",
        "exposure_delta_vs_capacity_1": "exposure",
        "turnover_delta_vs_capacity_1": "turnover",
        "modeled_cost_delta_vs_capacity_1": "total_modeled_execution_cost",
        "position_count_delta_vs_capacity_1": "positions",
    }
    for row in rows:
        for delta_name, metric_name in delta_fields.items():
            row[delta_name] = _difference(row[metric_name], baseline[metric_name])
        incremental_exposure = row["exposure_delta_vs_capacity_1"]
        incremental_return = row["return_delta_vs_capacity_1"]
        row["incremental_return_per_incremental_exposure"] = (
            incremental_return / incremental_exposure
            if incremental_return is not None and incremental_exposure
            else None
        )
    return rows


def run_f_capacity_research(
    database: Database,
    config: StrategyConfig,
    requested_start: date,
    requested_end: date,
) -> FCapacityResearchBundle:
    """Run the pre-registered capacity family using one shared local PIT screen cache."""

    _validate_frozen_control_config(config, requested_start, requested_end)
    preparation = prepare_strategy_comparison(
        database,
        config,
        requested_start,
        requested_end,
        comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F,
    )
    results = {
        identity.max_positions: _run_capacity_variant(
            database,
            config,
            preparation.screen_source,
            identity,
            requested_start,
            requested_end,
        )
        for identity in F_CAPACITY_RESEARCH_VARIANTS
    }
    cost_results: dict[str, dict[int, BacktestResult]] = {"BASELINE": results}
    for case, slippage_bps, commission_bps in CANONICAL_COST_STRESS_CASES:
        cost_config = config.model_copy(
            update={
                "backtest": config.backtest.model_copy(
                    update={
                        "slippage_bps": slippage_bps,
                        "commission_bps": commission_bps,
                    }
                )
            }
        )
        cost_results[case] = {
            identity.max_positions: _run_capacity_variant(
                database,
                cost_config,
                preparation.screen_source,
                identity,
                requested_start,
                requested_end,
            )
            for identity in F_CAPACITY_RESEARCH_VARIANTS
        }
    cost_rows = _capacity_cost_rows(cost_results)

    monthly_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    chronological_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    concentration: dict[str, Any] = {}
    time_stability: dict[str, Any] = {}
    utilization_rows: list[dict[str, Any]] = []
    entry_rank_rows: list[dict[str, Any]] = []
    for identity in F_CAPACITY_RESEARCH_VARIANTS:
        result = results[identity.max_positions]
        comparison = _single_result_comparison(result)
        capacity_monthly, monthly_summary = calendar_stability(comparison, "month")
        capacity_yearly, yearly_summary = calendar_stability(comparison, "year")
        capacity_subperiods = chronological_subperiod_analysis(comparison)
        capacity_symbols, capacity_concentration, _ = symbol_and_leave_one_out(comparison)
        _tag_rows(capacity_monthly, identity)
        _tag_rows(capacity_yearly, identity)
        _tag_rows(capacity_subperiods, identity)
        _tag_rows(capacity_symbols, identity)
        monthly_rows.extend(capacity_monthly)
        yearly_rows.extend(capacity_yearly)
        chronological_rows.extend(capacity_subperiods)
        symbol_rows.extend(capacity_symbols)
        time_stability[identity.label] = {
            "monthly": monthly_summary[FROZEN_CHAMPION_F.label],
            "yearly": yearly_summary[FROZEN_CHAMPION_F.label],
        }
        concentration[identity.label] = capacity_concentration[FROZEN_CHAMPION_F.label]
        utilization_rows.append(
            capacity_utilization_row(identity.label, result, identity.max_positions)
        )
        entry_rank_rows.extend(
            entry_rank_analysis_rows(identity.label, result, identity.max_positions)
        )

    return FCapacityResearchBundle(
        requested_start=requested_start,
        requested_end=requested_end,
        results=results,
        capacity_rows=capacity_metric_rows(
            results,
            cost_rows,
            time_stability=time_stability,
            concentration=concentration,
        ),
        utilization_rows=utilization_rows,
        entry_rank_rows=entry_rank_rows,
        monthly_rows=monthly_rows,
        yearly_rows=yearly_rows,
        chronological_subperiod_rows=chronological_rows,
        symbol_rows=symbol_rows,
        concentration=concentration,
        time_stability=time_stability,
        cost_rows=cost_rows,
        universe_provenance=audit_universe_provenance(database, requested_start, requested_end),
        period_classification=(
            "DEVELOPMENT_OVERLAP"
            if requested_start <= FROZEN_CHAMPION_F.development_cutoff
            else "POST_DEVELOPMENT_RESEARCH_NOT_CHAMPION_OOS"
        ),
    )


def export_f_capacity_research(
    bundle: FCapacityResearchBundle,
    output_directory: Path,
    *,
    stem: str,
) -> dict[str, Path]:
    """Export a fresh, non-overwriting capacity-research evidence bundle."""

    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": output_directory / f"{stem}_summary.json",
        "summary_csv": output_directory / f"{stem}_summary.csv",
        "capacity_metrics": output_directory / f"{stem}_capacity_metrics.csv",
        "capacity_utilization": output_directory / f"{stem}_capacity_utilization.csv",
        "entry_rank_analysis": output_directory / f"{stem}_entry_rank_analysis.csv",
        "monthly": output_directory / f"{stem}_monthly.csv",
        "yearly": output_directory / f"{stem}_yearly.csv",
        "chronological_subperiods": output_directory / f"{stem}_chronological_subperiods.csv",
        "symbol_concentration": output_directory / f"{stem}_symbol_concentration.csv",
        "cost_stress": output_directory / f"{stem}_cost_stress.csv",
        "positions": output_directory / f"{stem}_positions.csv",
        "execution_legs": output_directory / f"{stem}_execution_legs.csv",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"F capacity research export already exists: {existing[0]}")

    payload = {
        "report_type": "f_configured_portfolio_capacity_research",
        "research_family": F_CAPACITY_RESEARCH_FAMILY,
        "research_status": "historical research hypotheses",
        "automatic_winner_selection": False,
        "frozen_champion": {
            "strategy": FROZEN_CHAMPION_F.label,
            "max_positions": 1,
            "unchanged": True,
        },
        "frozen_development_cutoff": FROZEN_CHAMPION_F.development_cutoff.isoformat(),
        "requested_start": bundle.requested_start.isoformat(),
        "requested_end": bundle.requested_end.isoformat(),
        "period_classification": bundle.period_classification,
        "capacity_variants": [
            {
                "research_id": identity.research_id,
                "label": identity.label,
                "strategy": FROZEN_CHAMPION_F.label,
                "max_positions": identity.max_positions,
                "frozen_champion_control": identity.frozen_champion_control,
            }
            for identity in F_CAPACITY_RESEARCH_VARIANTS
        ],
        "shared_configuration": {
            "strategy_variant": FROZEN_CHAMPION_F.variant.value,
            "position_management": FROZEN_CHAMPION_F.preset.value,
            "risk_per_trade": bundle.results[1].configuration["risk"]["risk_per_trade"],
            "max_position_pct": bundle.results[1].configuration["portfolio"]["max_position_pct"],
            "max_sector_positions": bundle.results[1].configuration["portfolio"][
                "max_sector_positions"
            ],
        },
        "invariants": {
            "only_varied_field": "portfolio.max_positions",
            "f_screen": "unchanged",
            "candidate_ranking": "unchanged",
            "entry": "unchanged",
            "configured_management": "unchanged",
            "risk_per_trade": "unchanged",
            "max_position_pct": "unchanged",
            "cost_scenarios": "canonical",
        },
        "capacity_metrics": bundle.capacity_rows,
        "capacity_utilization": bundle.utilization_rows,
        "entry_rank_analysis": bundle.entry_rank_rows,
        "time_stability": bundle.time_stability,
        "chronological_subperiods": bundle.chronological_subperiod_rows,
        "symbol_concentration": bundle.concentration,
        "cost_stress": {
            "methodology": "FULL_PORTFOLIO_RERUN",
            "scenarios": bundle.cost_rows,
        },
        "survivorship_status": bundle.universe_provenance["survivorship_status"],
        "universe_provenance_status": bundle.universe_provenance["universe_provenance"],
        "universe_provenance": bundle.universe_provenance,
        "warnings": list(
            dict.fromkeys(
                warning for result in bundle.results.values() for warning in result.warnings
            )
        ),
        "methodology_note": (
            "Historical comparison only; capacities 2, 3, and 5 are research hypotheses, "
            "not parameter-optimization evidence or replacements for the frozen champion."
        ),
    }
    _atomic_text(paths["summary_json"], json.dumps(payload, indent=2))
    summary_fields = [
        "configured_max_positions",
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe",
        "sortino",
        "profit_factor",
        "positions",
        "unique_traded_symbols",
        "exposure",
        "maximum_simultaneous_positions_observed",
        "turnover",
        "total_modeled_execution_cost",
        "top_1_pnl_concentration",
        "top_3_pnl_concentration",
        "return_delta_vs_capacity_1",
        "exposure_delta_vs_capacity_1",
    ]
    _atomic_csv(
        paths["summary_csv"],
        [{field: row.get(field) for field in summary_fields} for row in bundle.capacity_rows],
        summary_fields,
    )
    _atomic_csv(
        paths["capacity_metrics"],
        bundle.capacity_rows,
        _field_union(bundle.capacity_rows),
    )
    _atomic_csv(
        paths["capacity_utilization"],
        bundle.utilization_rows,
        _field_union(bundle.utilization_rows),
    )
    _atomic_csv(
        paths["entry_rank_analysis"],
        bundle.entry_rank_rows,
        _field_union(bundle.entry_rank_rows),
    )
    _atomic_csv(paths["monthly"], bundle.monthly_rows, _field_union(bundle.monthly_rows))
    _atomic_csv(paths["yearly"], bundle.yearly_rows, _field_union(bundle.yearly_rows))
    _atomic_csv(
        paths["chronological_subperiods"],
        bundle.chronological_subperiod_rows,
        _field_union(bundle.chronological_subperiod_rows),
    )
    _atomic_csv(
        paths["symbol_concentration"],
        bundle.symbol_rows,
        _field_union(bundle.symbol_rows),
    )
    _atomic_csv(paths["cost_stress"], bundle.cost_rows, _field_union(bundle.cost_rows))
    position_rows = _tagged_model_rows(bundle.results, "positions")
    execution_rows = _tagged_model_rows(bundle.results, "trades")
    _atomic_csv(
        paths["positions"],
        position_rows,
        ["strategy", "configured_max_positions", *BacktestPosition.model_fields],
    )
    _atomic_csv(
        paths["execution_legs"],
        execution_rows,
        ["strategy", "configured_max_positions", *BacktestTrade.model_fields],
    )
    return paths


def format_f_capacity_research_summary(bundle: FCapacityResearchBundle) -> str:
    """Format a compact terminal handoff without selecting a winner."""

    lines = [
        "F/configured capacity research complete (historical comparison only).",
        f"Period classification: {bundle.period_classification}.",
        "Capacity  Return     Sharpe     PF       Exposure   Max concurrent",
    ]
    for row in bundle.capacity_rows:
        lines.append(
            f"{row['configured_max_positions']:>8}  "
            f"{_format_percent(row['total_return']):>8}  "
            f"{_format_number(row['sharpe']):>8}  "
            f"{_format_number(row['profit_factor']):>7}  "
            f"{_format_percent(row['exposure']):>9}  "
            f"{row['maximum_simultaneous_positions_observed']:>14}"
        )
    lines.append("No automatic winner was selected; max_positions=1 remains frozen champion.")
    return "\n".join(lines)


def _run_capacity_variant(
    database: Database,
    config: StrategyConfig,
    screen_source: Any,
    identity: FCapacityResearchVariant,
    start: date,
    end: date,
) -> BacktestResult:
    capacity_config = capacity_strategy_config(config, identity.max_positions)
    result = BacktestEngine(
        database,
        capacity_config,
        screen_source=screen_source,
    ).run(start, end, variant=identity.variant, preset=identity.preset)
    if (
        result.strategy_variant is not FROZEN_CHAMPION_F.variant
        or result.position_management_preset is not FROZEN_CHAMPION_F.preset
        or result.configuration["portfolio"]["max_positions"] != identity.max_positions
    ):
        raise ValueError(f"{identity.label} did not preserve its registered identity")
    return result


def _capacity_cost_rows(
    cost_results: dict[str, dict[int, BacktestResult]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, results in cost_results.items():
        _require_complete_capacity_set(results)
        for identity in F_CAPACITY_RESEARCH_VARIANTS:
            row = _cost_row(case, identity.label, results[identity.max_positions])
            row.update(
                {
                    "research_family": F_CAPACITY_RESEARCH_FAMILY,
                    "configured_max_positions": identity.max_positions,
                }
            )
            rows.append(row)
    return rows


def _single_result_comparison(result: BacktestResult) -> StrategyComparison:
    return StrategyComparison(
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        actual_start=result.actual_start,
        actual_end=result.actual_end,
        generated_at=result.generated_at,
        variants=(result,),
        shared_screen_sessions=len(result.equity_curve),
        comparison_kind=StrategyComparisonKind.RESEARCH_CHAMPION_F,
        warnings=result.warnings,
    )


def _tag_rows(rows: list[dict[str, Any]], identity: FCapacityResearchVariant) -> None:
    for row in rows:
        row.update(
            {
                "strategy": identity.label,
                "research_family": F_CAPACITY_RESEARCH_FAMILY,
                "configured_max_positions": identity.max_positions,
            }
        )


def _tagged_model_rows(results: dict[int, BacktestResult], attribute: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for identity in F_CAPACITY_RESEARCH_VARIANTS:
        for item in getattr(results[identity.max_positions], attribute):
            rows.append(
                {
                    "strategy": identity.label,
                    "configured_max_positions": identity.max_positions,
                    **item.model_dump(mode="json"),
                }
            )
    return rows


def _validate_frozen_control_config(
    config: StrategyConfig, requested_start: date, requested_end: date
) -> None:
    if requested_start > requested_end:
        raise ValueError("F capacity research start must not be after end")
    if config.portfolio.max_positions != 1:
        raise ValueError("F capacity research requires frozen champion max_positions=1")
    if config.portfolio.max_position_pct != 1.0 or config.risk.risk_per_trade != 0.01:
        raise ValueError(
            "F capacity research requires frozen max_position_pct=1.0 and risk_per_trade=0.01"
        )
    if config.backtest.slippage_bps != 5 or config.backtest.commission_bps != 0:
        raise ValueError("F capacity research requires the canonical 5 bps / 0 bps baseline")


def _require_complete_capacity_set(results: dict[int, BacktestResult]) -> None:
    expected = [item.max_positions for item in F_CAPACITY_RESEARCH_VARIANTS]
    if sorted(results) != expected:
        raise ValueError(f"F capacity results must contain exactly {expected}")


def _difference(value: float | int | None, baseline: float | int | None) -> float | int | None:
    if value is None or baseline is None:
        return None
    return value - baseline


def _profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    return gross_profit / gross_loss if gross_loss else None


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _format_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"
