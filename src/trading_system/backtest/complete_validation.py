"""Complete the two tail artifacts after an interrupted validation export.

This recovery entry point never reruns or overwrites the already exported OOS
portfolio results.  Each part runs in a separate process so the large PIT screen
cache is released before the other interval is prepared.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trading_system.backtest.engine import (
    BacktestEngine,
    compare_strategies,
    comparison_intraday_prefetch_metadata,
    prepare_strategy_comparison,
    research_strategy_label,
)
from trading_system.backtest.report import _atomic_csv, _atomic_text
from trading_system.backtest.validation import (
    _field_union,
    _sensitivity_row,
    annotate_trade_path_coverage,
    intraday_session_statuses,
    reference_regression,
    strategy_summary,
)
from trading_system.config import StrategyConfig, load_settings
from trading_system.data.database import Database
from trading_system.data.qualification import DataQualificationReport
from trading_system.models.backtest import (
    PositionManagementPreset,
    StrategyComparisonKind,
    StrategyVariant,
)

OOS_END = date(2026, 4, 30)
REFERENCE_START = date(2026, 5, 1)
REFERENCE_END = date(2026, 8, 12)


def complete_reference(
    database: Database, config: StrategyConfig, reports: Path
) -> Path:
    """Recreate only the already-executed reference-regression tail artifact."""

    target = reports / (
        f"research_reference_regression_{REFERENCE_START}_{REFERENCE_END}.json"
    )
    _require_missing(target)
    preparation = prepare_strategy_comparison(
        database,
        config,
        REFERENCE_START,
        REFERENCE_END,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
    )
    comparison = compare_strategies(
        database,
        config,
        REFERENCE_START,
        REFERENCE_END,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
        preparation=preparation,
        intraday_prefetch=comparison_intraday_prefetch_metadata(
            preparation, enabled=False
        ),
        allow_missing_intraday_data=True,
    )
    regression = reference_regression(comparison)
    if not regression["passed"]:
        raise ValueError("research-reference regression failed during completion")
    _atomic_text(
        target,
        json.dumps(
            {
                **regression,
                "requested_start": REFERENCE_START.isoformat(),
                "requested_end": REFERENCE_END.isoformat(),
                "strategies": [
                    strategy_summary(
                        research_strategy_label(
                            result.strategy_variant,
                            result.position_management_preset,
                        ),
                        result,
                    )
                    for result in comparison.variants
                ],
            },
            indent=2,
        ),
    )
    return target


def complete_sensitivity(
    database: Database, config: StrategyConfig, reports: Path
) -> Path:
    """Recreate only the missing strict/native/trade-path sensitivity artifact."""

    target = reports / "extended_validation_intraday_sensitivity.csv"
    _require_missing(target)
    qualification_path = reports / "extended_validation_data_qualification.json"
    summary_path = reports / (
        "extended_validation_2025-05-01_2026-04-30_summary.json"
    )
    positions_path = reports / (
        "extended_validation_2025-05-01_2026-04-30_positions.csv"
    )
    for required in (qualification_path, summary_path, positions_path):
        if not required.is_file():
            raise FileNotFoundError(f"required completed artifact is missing: {required}")

    qualification_payload = json.loads(qualification_path.read_text(encoding="utf-8"))
    actual_start = date.fromisoformat(
        json.loads(summary_path.read_text(encoding="utf-8"))["actual_qualified_oos"][0]
    )
    full_report = DataQualificationReport.model_validate(
        qualification_payload["intraday"]["full_symbol_sessions"]
    )
    symbols = list(qualification_payload["candidate_discovery"]["candidate_symbols"])
    preparation = prepare_strategy_comparison(
        database,
        config,
        actual_start,
        OOS_END,
        comparison_kind=StrategyComparisonKind.EXTENDED_VALIDATION,
    )
    statuses = intraday_session_statuses(full_report, symbols, preparation.sessions)
    strict = BacktestEngine(
        database,
        config,
        screen_source=preparation.screen_source,
        strict_coverage_sensitivity=True,
        intraday_session_statuses=statuses,
        allow_missing_intraday_data=True,
    ).run(
        actual_start,
        OOS_END,
        variant=StrategyVariant.FULL,
        preset=PositionManagementPreset.INTRADAY_DYNAMIC,
    )
    strict, _ = annotate_trade_path_coverage(database, strict)
    rows = _recovered_sensitivity_rows(config, reports, strict)
    _atomic_csv(target, rows, _field_union(rows))
    return target


def _recovered_sensitivity_rows(
    config: StrategyConfig, reports: Path, strict: Any
) -> list[dict[str, Any]]:
    summary = json.loads(
        (
            reports / "extended_validation_2025-05-01_2026-04-30_summary.json"
        ).read_text(encoding="utf-8")
    )
    native_summary = next(
        item for item in summary["strategies"] if item["strategy"] == "C/intraday-dynamic"
    )
    native_positions: list[SimpleNamespace] = []
    with (
        reports / "extended_validation_2025-05-01_2026-04-30_positions.csv"
    ).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["strategy"] != "C/intraday-dynamic":
                continue
            native_positions.append(
                SimpleNamespace(
                    net_pnl=float(row["net_pnl"]),
                    trade_path_complete=_parse_optional_bool(
                        row["trade_path_complete"]
                    ),
                )
            )
    native = _sensitivity_row(
        "NATIVE", native_positions, config.backtest.initial_capital
    )
    native.update(
        {
            "total_return": native_summary["total_return"],
            "max_drawdown": native_summary["max_drawdown"],
            "profit_factor": native_summary["position_profit_factor"],
            "expectancy": native_summary["expectancy"],
        }
    )
    strict_row = _sensitivity_row(
        "STRICT_FULL_SESSION", strict.positions, strict.initial_capital
    )
    strict_row.update(
        {
            "total_return": strict.metrics.total_return,
            "max_drawdown": strict.metrics.maximum_drawdown,
            "profit_factor": strict.position_metrics.position_profit_factor,
            "expectancy": strict.metrics.expectancy_per_trade,
        }
    )
    trade_path = _sensitivity_row(
        "STRICT_TRADE_PATH",
        [item for item in native_positions if item.trade_path_complete is True],
        config.backtest.initial_capital,
    )
    trade_path["post_hoc_position_filter"] = True
    trade_path["max_drawdown"] = None
    return [native, strict_row, trade_path]


def _parse_optional_bool(value: str) -> bool | None:
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _require_missing(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"completion artifact already exists: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("part", choices=("reference", "sensitivity"))
    args = parser.parse_args()
    settings = load_settings()
    database = Database(settings.strategy.storage.database_path)
    reports = settings.strategy.storage.reports_path
    path = (
        complete_reference(database, settings.strategy, reports)
        if args.part == "reference"
        else complete_sensitivity(database, settings.strategy, reports)
    )
    print(f"Completed validation tail artifact: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
