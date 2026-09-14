"""Explicit frozen champion workflow over the existing local screen and simulator."""

from datetime import date

from trading_system.backtest.engine import BacktestEngine, ScreenSource, evaluate_variant_entry
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.models.backtest import BacktestResult
from trading_system.models.screening import ScreenReport
from trading_system.strategy.screener import Screener


def champion_ranking(report: ScreenReport, config: StrategyConfig) -> ScreenReport:
    """Present the canonical F entry funnel, before portfolio allocation, in a screen."""
    validate_champion_config(config)
    records = []
    for record in report.records:
        evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config)
        records.append(
            record.model_copy(
                update={
                    "eligible": evaluation.eligible,
                    "rank": None,
                    "exclusion_reasons": (
                        () if evaluation.eligible else (evaluation.first_failure,)
                    ),
                    "scores": record.scores.model_copy(update={"total": evaluation.weighted_score}),
                }
            )
        )
    # Same ordering as BacktestEngine._entry_orders; no portfolio-dependent reranking.
    eligible = sorted(
        (record for record in records if record.eligible),
        key=lambda record: (-record.scores.total, record.symbol),
    )
    ranked = [record.model_copy(update={"rank": rank}) for rank, record in enumerate(eligible, 1)]
    return report.model_copy(
        update={
            "strategy_label": FROZEN_CHAMPION_F.production_label,
            "eligible_count": len(ranked),
            "records": tuple(ranked + [record for record in records if not record.eligible]),
        }
    )


def screen_champion(database: Database, config: StrategyConfig, as_of: date) -> ScreenReport:
    validate_champion_config(config)
    report = Screener(database, config).run(as_of, use_market_snapshots=False)
    return champion_ranking(report, config)


def run_champion_backtest(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    screen_source: ScreenSource | None = None,
) -> BacktestResult:
    """Select the champion with no research hooks; never fetch or normalize settings."""
    validate_champion_config(config)
    return BacktestEngine(database, config, screen_source=screen_source).run(
        start,
        end,
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
    )
