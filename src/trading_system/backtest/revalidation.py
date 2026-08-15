"""Deterministic post-audit comparison and hypothesis diagnostics for exported reports."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from trading_system.backtest.report import _atomic_csv, _atomic_text

COMPARISON_METRICS = (
    "total_return",
    "cagr",
    "maximum_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "positions_closed",
    "execution_legs",
    "position_win_rate",
    "execution_leg_win_rate",
    "profit_factor",
    "position_profit_factor",
    "average_win",
    "average_loss",
    "average_position_holding_period",
    "trading_costs",
    "slippage_costs",
    "exposure",
)
POSITION_FIELDS = (
    "exit_timestamp",
    "exit_reference_price",
    "exit_price",
    "exit_reason",
    "holding_days",
    "initial_quantity",
    "gross_pnl",
    "net_pnl",
    "position_return",
    "maximum_favorable_excursion",
    "maximum_adverse_excursion",
    "profit_capture_ratio",
    "profit_giveback",
)
LEG_FIELDS = (
    "exit_timestamp",
    "exit_reference_price",
    "exit_price",
    "exit_reason",
    "quantity",
    "gross_pnl",
    "net_pnl",
    "return_pct",
    "holding_days",
)
POST_EXIT_FIELDS = (
    "post_exit_return_1d",
    "post_exit_return_3d",
    "post_exit_return_5d",
    "post_exit_return_10d",
    "post_exit_mfe_1d",
    "post_exit_mfe_3d",
    "post_exit_mfe_5d",
    "post_exit_mfe_10d",
    "post_exit_mae_1d",
    "post_exit_mae_3d",
    "post_exit_mae_5d",
    "post_exit_mae_10d",
)


def generate_revalidation_artifacts(
    report_directory: Path,
    *,
    old_stem: str,
    new_stem: str,
) -> dict[str, Path]:
    """Compare already-exported runs without recomputing screens or trades."""

    old_summary = _read_csv(report_directory / f"{old_stem}.csv")
    new_summary = _read_csv(report_directory / f"{new_stem}.csv")
    old_positions = _read_csv(report_directory / f"{old_stem}_positions.csv")
    new_positions = _read_csv(report_directory / f"{new_stem}_positions.csv")
    old_legs = _read_csv(report_directory / f"{old_stem}_execution_legs.csv")
    new_legs = _read_csv(report_directory / f"{new_stem}_execution_legs.csv")
    old_post = _read_csv(report_directory / f"{old_stem}_post_exit_analysis.csv")
    new_post = _read_csv(report_directory / f"{new_stem}_post_exit_analysis.csv")

    comparison = _comparison_diff(old_summary, new_summary, old_positions, new_positions)
    position_diff = _record_diff(
        old_positions,
        new_positions,
        keys=("strategy", "position_id"),
        fields=POSITION_FIELDS,
        level="position",
    )
    leg_diff = _record_diff(
        old_legs,
        new_legs,
        keys=("strategy", "execution_leg_id"),
        fields=LEG_FIELDS,
        level="execution_leg",
    )
    post_exit_diff = _record_diff(
        old_post,
        new_post,
        keys=("strategy", "position_id"),
        fields=POST_EXIT_FIELDS,
        level="post_exit",
    )
    candidate_path = report_directory / f"{new_stem}_candidate_audit_variants.json"
    candidate_audits = _read_json(candidate_path) if candidate_path.exists() else {}
    hypotheses = _hypothesis_diagnostics(
        new_positions,
        new_legs,
        summary={row["strategy"]: row for row in new_summary},
        candidate_audits=candidate_audits,
    )
    cost_stress = _cost_stress(report_directory, new_stem, new_summary)

    paths = {
        "comparison_diff_json": report_directory / f"{new_stem}_old_vs_new.json",
        "comparison_diff_csv": report_directory / f"{new_stem}_old_vs_new.csv",
        "position_diff": report_directory / f"{new_stem}_position_diff.csv",
        "execution_leg_diff": report_directory / f"{new_stem}_execution_leg_diff.csv",
        "post_exit_diff": report_directory / f"{new_stem}_post_exit_diff.csv",
        "hypotheses": report_directory / f"{new_stem}_hypothesis_revalidation.json",
        "cost_stress": report_directory / f"{new_stem}_cost_stress.csv",
    }
    payload = {
        "old_stem": old_stem,
        "new_stem": new_stem,
        "strategy_comparison": comparison,
        "position_differences": len(position_diff),
        "execution_leg_differences": len(leg_diff),
        "post_exit_differences": len(post_exit_diff),
    }
    _atomic_text(paths["comparison_diff_json"], json.dumps(payload, indent=2))
    _write_rows(paths["comparison_diff_csv"], comparison)
    _write_rows(paths["position_diff"], position_diff)
    _write_rows(paths["execution_leg_diff"], leg_diff)
    _write_rows(paths["post_exit_diff"], post_exit_diff)
    _atomic_text(paths["hypotheses"], json.dumps(hypotheses, indent=2))
    _write_rows(paths["cost_stress"], cost_stress)
    return paths


def _comparison_diff(
    old_rows: list[dict[str, str]],
    new_rows: list[dict[str, str]],
    old_positions: list[dict[str, str]],
    new_positions: list[dict[str, str]],
) -> list[dict[str, Any]]:
    old_by_strategy = {row["strategy"]: row for row in old_rows}
    new_by_strategy = {row["strategy"]: row for row in new_rows}
    pnl = {
        "old": _position_pnl_by_strategy(old_positions),
        "new": _position_pnl_by_strategy(new_positions),
    }
    output = []
    for strategy in sorted(old_by_strategy.keys() | new_by_strategy.keys()):
        old = old_by_strategy.get(strategy, {})
        new = new_by_strategy.get(strategy, {})
        row: dict[str, Any] = {"strategy": strategy}
        materially_different = False
        for metric in COMPARISON_METRICS:
            old_value = _number(old.get(metric))
            new_value = _number(new.get(metric))
            row[f"{metric}_old"] = old_value
            row[f"{metric}_new"] = new_value
            row[f"{metric}_absolute_delta"] = _delta(old_value, new_value)
            row[f"{metric}_relative_delta"] = _relative_delta(old_value, new_value)
            materially_different |= _different(old_value, new_value)
        for metric in ("gross_pnl", "net_pnl"):
            old_value = pnl["old"].get(strategy, {}).get(metric)
            new_value = pnl["new"].get(strategy, {}).get(metric)
            row[f"{metric}_old"] = old_value
            row[f"{metric}_new"] = new_value
            row[f"{metric}_absolute_delta"] = _delta(old_value, new_value)
            row[f"{metric}_relative_delta"] = _relative_delta(old_value, new_value)
            materially_different |= _different(old_value, new_value)
        row["materially_different"] = materially_different
        row["attribution"] = _strategy_attribution(strategy, materially_different)
        output.append(row)
    return output


def _record_diff(
    old_rows: list[dict[str, str]],
    new_rows: list[dict[str, str]],
    *,
    keys: tuple[str, ...],
    fields: tuple[str, ...],
    level: str,
) -> list[dict[str, Any]]:
    old = {tuple(row[key] for key in keys): row for row in old_rows}
    new = {tuple(row[key] for key in keys): row for row in new_rows}
    output = []
    for identity in sorted(old.keys() | new.keys()):
        before = old.get(identity)
        after = new.get(identity)
        status = "both"
        if before is None:
            status = "new_only"
        elif after is None:
            status = "old_only"
        changed_fields = []
        row: dict[str, Any] = {
            **dict(zip(keys, identity, strict=True)),
            "record_status": status,
        }
        for field in fields:
            old_raw = before.get(field, "") if before else ""
            new_raw = after.get(field, "") if after else ""
            old_value = _number(old_raw)
            new_value = _number(new_raw)
            if old_value is None and old_raw:
                old_value = old_raw
            if new_value is None and new_raw:
                new_value = new_raw
            row[f"{field}_old"] = old_value
            row[f"{field}_new"] = new_value
            if not _values_equal(old_value, new_value):
                changed_fields.append(field)
                if isinstance(old_value, float) and isinstance(new_value, float):
                    row[f"{field}_delta"] = new_value - old_value
        if status != "both" or changed_fields:
            strategy = identity[0]
            row["changed_fields"] = ",".join(changed_fields)
            row["attribution"] = _record_attribution(
                strategy,
                changed_fields,
                level=level,
                before=before,
                after=after,
            )
            output.append(row)
    return output


def _hypothesis_diagnostics(
    positions: list[dict[str, str]],
    legs: list[dict[str, str]],
    *,
    summary: dict[str, dict[str, str]],
    candidate_audits: dict[str, Any],
) -> dict[str, Any]:
    intraday = [row for row in positions if row["strategy"] == "C/intraday-dynamic"]
    entry_bar = [row for row in intraday if row["entry_timestamp"] == row["exit_timestamp"]]
    survivors = [row for row in intraday if row not in entry_bar]
    trail_entry_bar = [row for row in entry_bar if row["exit_reason"] == "atr_trailing_stop"]
    profit_targets = [row for row in positions if row["exit_reason"] == "profit_target"]
    max_hold = [
        row
        for row in positions
        if row["strategy"] == "C/fixed-stop-max-hold" and row["exit_reason"] == "max_hold"
    ]
    dynamic = [row for row in positions if row["strategy"] == "C/dynamic-hold"]
    dynamic_extremes = sorted(
        (
            {
                "symbol": row["symbol"],
                "entry_date": row["entry_date"],
                "realized_return": _number(row["position_return"]),
                "mfe": _number(row["maximum_favorable_excursion"]),
                "giveback": _number(row["profit_giveback"]),
            }
            for row in dynamic
        ),
        key=lambda item: item["giveback"] or 0,
        reverse=True,
    )
    score_variants = {}
    for strategy in ("A/configured", "B/configured", "C/configured"):
        selected = [row for row in positions if row["strategy"] == strategy]
        candidate = candidate_audits.get(strategy, {})
        summary_row = summary.get(strategy, {})
        score_variants[strategy] = {
            "candidate_occurrences": candidate.get("candidate_occurrences"),
            "unique_candidate_symbols": candidate.get("unique_candidate_symbols"),
            "entries": len(selected),
            "position_win_rate": _win_rate(selected),
            "average_return": _average(selected, "position_return"),
            "maximum_drawdown": _number(summary_row.get("maximum_drawdown")),
            "profit_factor": _number(summary_row.get("position_profit_factor")),
            "average_quality": _average(selected, "quality_score"),
            "average_valuation": _average(selected, "valuation_score"),
            "average_opportunity": _average(selected, "opportunity_score"),
            "average_timing": _average(selected, "timing_score"),
        }
    return {
        "H0-A Opening Failure": {
            "status": "SUPPORTED" if len(entry_bar) >= len(survivors) else "PARTIALLY SUPPORTED",
            "all_intraday_positions": len(intraday),
            "entry_bar_exit_rate": len(entry_bar) / len(intraday) if intraday else None,
            "entry_bar_exits": _position_group(entry_bar),
            "survived_first_bar": _position_group(survivors),
        },
        "H0-B ATR Trail Timing": {
            "status": "INCONCLUSIVE",
            "trail_exits_in_entry_bar": len(trail_entry_bar),
            "reason": (
                "Reports persist exit execution but not the first trail-activation timestamp or "
                "bar index; activation timing cannot be reconstructed reliably."
            ),
        },
        "H0-C 12% Full Target": {
            "status": _target_hypothesis_status(profit_targets),
            "strong_runner_5d_count": sum(
                (_number(row["post_exit_mfe_5d"]) or 0) >= 0.05
                for row in profit_targets
            ),
            "positive_post_exit_5d_count": sum(
                (_number(row["post_exit_return_5d"]) or 0) > 0
                for row in profit_targets
            ),
            **_post_exit_group(profit_targets),
        },
        "H0-D Hard Max Hold": {
            "status": _max_hold_status(max_hold),
            "positive_post_exit_5d_count": sum(
                (_number(row["post_exit_return_5d"]) or 0) > 0 for row in max_hold
            ),
            "positive_post_exit_10d_count": sum(
                (_number(row["post_exit_return_10d"]) or 0) > 0 for row in max_hold
            ),
            **_post_exit_group(max_hold),
        },
        "H0-E Dynamic Hold Giveback": {
            "status": "SUPPORTED"
            if any(
                (item["mfe"] or 0) > 0.05 and (item["realized_return"] or 0) < 0
                for item in dynamic_extremes
            )
            else "PARTIALLY SUPPORTED",
            "positions": len(dynamic),
            "average_giveback": _average(dynamic, "profit_giveback"),
            "extreme_cases": dynamic_extremes[:10],
        },
        "H0-F A/B vs C Selection": {
            "status": "SUPPORTED"
            if score_variants["A/configured"]["average_return"]
            and score_variants["B/configured"]["average_return"]
            and score_variants["C/configured"]["average_return"]
            and score_variants["A/configured"]["average_return"]
            > score_variants["C/configured"]["average_return"]
            and score_variants["B/configured"]["average_return"]
            > score_variants["C/configured"]["average_return"]
            else "NOT SUPPORTED",
            "variants": score_variants,
        },
        "execution_leg_count": len(legs),
    }


def _cost_stress(
    directory: Path, new_stem: str, baseline: list[dict[str, str]]
) -> list[dict[str, Any]]:
    cases = {
        "baseline": baseline,
        "2x_slippage": _read_csv(directory / f"{new_stem}_cost_2x.csv"),
        "3x_slippage": _read_csv(directory / f"{new_stem}_cost_3x.csv"),
        "commission_5bps": _read_csv(directory / f"{new_stem}_commission_5bps.csv"),
    }
    indexed = {
        name: {row["strategy"]: row for row in rows} for name, rows in cases.items()
    }
    output = []
    for strategy in sorted(indexed["baseline"]):
        baseline_return = _number(indexed["baseline"][strategy]["total_return"])
        row: dict[str, Any] = {"strategy": strategy, "baseline_return": baseline_return}
        for name in ("2x_slippage", "3x_slippage", "commission_5bps"):
            stressed = _number(indexed[name][strategy]["total_return"])
            row[f"{name}_return"] = stressed
            row[f"{name}_delta"] = _delta(baseline_return, stressed)
            row[f"{name}_edge_lost"] = bool(
                baseline_return is not None
                and stressed is not None
                and baseline_return > 0 >= stressed
            )
        output.append(row)
    return output


def _position_pnl_by_strategy(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = defaultdict(
        lambda: {"gross_pnl": 0.0, "net_pnl": 0.0}
    )
    for row in rows:
        values[row["strategy"]]["gross_pnl"] += _number(row["gross_pnl"]) or 0.0
        values[row["strategy"]]["net_pnl"] += _number(row["net_pnl"]) or 0.0
    return dict(values)


def _position_group(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "win_rate": _win_rate(rows),
        "average_return": _average(rows, "position_return"),
        "average_mfe": _average(rows, "maximum_favorable_excursion"),
        "average_mae": _average(rows, "maximum_adverse_excursion"),
    }


def _post_exit_group(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "positions": len(rows),
        "average_realized_return": _average(rows, "position_return"),
        **{f"average_{field}": _average(rows, field) for field in POST_EXIT_FIELDS},
    }


def _target_hypothesis_status(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "INCONCLUSIVE"
    strong = sum((_number(row["post_exit_mfe_5d"]) or 0) >= 0.05 for row in rows)
    return "SUPPORTED" if strong / len(rows) >= 0.5 else "PARTIALLY SUPPORTED"


def _max_hold_status(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "INCONCLUSIVE"
    continued = sum((_number(row["post_exit_return_5d"]) or 0) > 0 for row in rows)
    return "SUPPORTED" if continued / len(rows) >= 0.5 else "NOT SUPPORTED"


def _average(rows: list[dict[str, str]], field: str) -> float | None:
    values = [_number(row.get(field)) for row in rows]
    selected = [value for value in values if value is not None]
    return fmean(selected) if selected else None


def _win_rate(rows: list[dict[str, str]]) -> float | None:
    values = [_number(row.get("position_return")) for row in rows]
    selected = [value for value in values if value is not None]
    return sum(value > 0 for value in selected) / len(selected) if selected else None


def _strategy_attribution(strategy: str, changed: bool) -> str:
    if not changed:
        return "unchanged"
    if "partial" in strategy:
        return "execution-correctness candidate: TRW-005/TRW-006; verify leg diff"
    if "atr-trailing" in strategy:
        return "execution-correctness candidate: TRW-005; verify leg diff"
    if "intraday" in strategy:
        return "execution-correctness candidate: TRW-007; verify timestamps"
    return "cause not established"


def _record_attribution(
    strategy: str,
    fields: list[str],
    *,
    level: str,
    before: dict[str, str] | None,
    after: dict[str, str] | None,
) -> str:
    old_reason = before.get("exit_reason") if before else None
    new_reason = after.get("exit_reason") if after else None
    if old_reason in {"take_profit", "max_hold"} and new_reason in {
        "profit_target",
        "time_exit",
    }:
        return "confirmed TRW-008: canonical exit-reason normalization only"
    if old_reason == "stop_loss" and new_reason == "atr_trailing_stop":
        return "confirmed TRW-005: higher active ATR protection stop won"
    if set(fields) <= {"initial_quantity", "quantity", "gross_pnl", "net_pnl"}:
        return "downstream sizing/P&L effect after an earlier corrected execution"
    if "intraday" in strategy and any("timestamp" in field for field in fields):
        return "possible TRW-007; temporal evidence required"
    if "partial" in strategy:
        return "possible TRW-005/TRW-006; rule ordering evidence required"
    if "atr-trailing" in strategy:
        return "possible TRW-005; stop-level evidence required"
    if level == "post_exit":
        return "downstream of changed exit; direct cause not independently established"
    return "cause not established"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["status"]
    _atomic_csv(path, rows, fields)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _delta(old: float | None, new: float | None) -> float | None:
    return None if old is None or new is None else new - old


def _relative_delta(old: float | None, new: float | None) -> float | None:
    return None if old in (None, 0) or new is None else (new - old) / abs(old)


def _different(old: float | None, new: float | None) -> bool:
    if old is None or new is None:
        return old != new
    return not math.isclose(old, new, rel_tol=1e-12, abs_tol=1e-12)


def _values_equal(old: Any, new: Any) -> bool:
    if isinstance(old, float) and isinstance(new, float):
        return not _different(old, new)
    return old == new
