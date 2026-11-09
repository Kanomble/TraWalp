"""Explicit frozen champion workflow over the existing local screen and simulator."""

from datetime import date

from trading_system.backtest.data_integrity import DataIntegrityMode
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
    from trading_system.data.forward_market import CONTRACT
    from trading_system.shadow.contracts import canonical_json, digest
    from trading_system.shadow.inputs import LocalInputs
    from trading_system.shadow.qualification import qualified_forward_screen

    with LocalInputs(database.path).snapshot() as inputs:
        report = Screener(inputs, config).run(as_of, use_market_snapshots=False)
        manifest = {
            **inputs.provenance,
            "session": report.effective_market_session or report.as_of,
            "requested_as_of": as_of,
        }
        content_hash = digest(manifest)
        path = config.storage.reports_path / "forward_inputs" / f"{content_hash}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(canonical_json(manifest))
        except FileExistsError:
            if path.read_text(encoding="utf-8") != canonical_json(manifest):
                raise ValueError("FORWARD_MANIFEST_HASH_COLLISION_OR_CORRUPTION") from None
        report = report.model_copy(
            update={
                "market_data_contract_id": CONTRACT["market_data_contract_id"],
                "market_input_manifest_hash": content_hash,
                "market_input_manifest_path": str(path),
            }
        )
    return qualified_forward_screen(
        report, report.effective_market_session or report.as_of, config
    )[0]


def run_champion_backtest(
    database: Database,
    config: StrategyConfig,
    start: date,
    end: date,
    *,
    screen_source: ScreenSource | None = None,
    data_integrity_mode: DataIntegrityMode = DataIntegrityMode.LEGACY_RESEARCH,
) -> BacktestResult:
    """Local historical replay; forward callers must explicitly select STRICT.

    Legacy research remains the default to preserve published historical results.
    The complete Shadow preflight API lives in backtest.data_integrity; no journal
    or broker execution is implemented here.
    """
    validate_champion_config(config)
    return BacktestEngine(
        database, config, screen_source=screen_source, data_integrity_mode=data_integrity_mode
    ).run(
        start,
        end,
        variant=FROZEN_CHAMPION_F.variant,
        preset=FROZEN_CHAMPION_F.preset,
    )
