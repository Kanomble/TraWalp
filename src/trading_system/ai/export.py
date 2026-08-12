"""Translate existing screen results into a reusable AI candidate payload."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading_system.ai.schemas import (
    AICandidate,
    AICandidateExport,
    AIFundamentalMetrics,
    AIRiskMetrics,
    AIScoreSummary,
    AITechnicalMetrics,
)
from trading_system.models.screening import ScreenRecord, ScreenReport

DEFAULT_LIMIT = 20
DEFAULT_OUTPUT_DIRECTORY = Path("output")


class NoAICandidatesError(RuntimeError):
    """Raised when a screen has no eligible candidates to export."""


@dataclass(frozen=True)
class AICandidateExportResult:
    path: Path
    payload: AICandidateExport


def build_ai_candidate_export(
    report: ScreenReport,
    *,
    limit: int = DEFAULT_LIMIT,
    generated_at: datetime | None = None,
) -> AICandidateExport:
    """Build an AI payload from the existing ranked, eligible screen records."""

    if limit <= 0:
        raise ValueError("AI candidate export limit must be positive")
    eligible = sorted(
        (record for record in report.records if record.eligible),
        key=lambda record: (record.rank is None, record.rank or 0, record.symbol),
    )
    if not eligible:
        raise NoAICandidatesError("No eligible screened candidates are available for AI export.")

    candidates = tuple(_candidate(record) for record in eligible[:limit])
    timestamp = generated_at or datetime.now().astimezone()
    if timestamp.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    return AICandidateExport(
        generated_at=timestamp,
        screen_as_of=report.as_of,
        candidate_count=len(candidates),
        candidates=candidates,
    )


def export_ai_candidates(
    report: ScreenReport,
    *,
    limit: int = DEFAULT_LIMIT,
    output_path: str | Path | None = None,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    generated_at: datetime | None = None,
) -> AICandidateExportResult:
    """Write a deterministic JSON export and return its path and validated payload."""

    payload = build_ai_candidate_export(report, limit=limit, generated_at=generated_at)
    path = (
        Path(output_path)
        if output_path is not None
        else Path(output_directory)
        / f"ai_candidates_{payload.generated_at.strftime('%Y-%m-%d_%H%M%S')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)
    return AICandidateExportResult(path=path, payload=payload)


def _candidate(record: ScreenRecord) -> AICandidate:
    if record.rank is None or record.scores.total is None:
        raise ValueError(f"Eligible screen record lacks rank or total score: {record.symbol}")
    technical = record.technical
    fundamentals = record.fundamentals
    return AICandidate(
        symbol=record.symbol,
        company_name=record.name,
        as_of=record.as_of,
        market_session=technical.market_session,
        price=technical.price,
        quant_score=record.scores.total,
        rank=record.rank,
        average_dollar_volume_20d=record.average_dollar_volume_20d,
        scores=AIScoreSummary(
            quality=record.scores.quality.score,
            valuation=record.scores.valuation.score,
            opportunity=record.scores.opportunity.score,
            timing=record.scores.timing.score,
        ),
        technical=AITechnicalMetrics(
            return_5d=technical.momentum5,
            return_20d=technical.momentum20,
            return_63d=technical.momentum63,
            return_126d=technical.momentum126,
            rsi_14=technical.rsi14,
            rsi_recovery=technical.rsi_recovery,
            sma_20=technical.sma20,
            sma_50=technical.sma50,
            sma_200=technical.sma200,
            above_sma_20=_above(technical.price, technical.sma20),
            above_sma_50=_above(technical.price, technical.sma50),
            above_sma_200=_above(technical.price, technical.sma200),
            sma_20_rising=technical.sma20_rising,
            momentum_20_improving=technical.momentum20_improving,
            volume_ratio_20d=technical.relative_volume,
        ),
        fundamentals=AIFundamentalMetrics(
            revenue_growth_yoy=fundamentals.revenue_growth,
            eps_growth_yoy=fundamentals.eps_growth,
            operating_cash_flow_growth_yoy=fundamentals.operating_cash_flow_growth,
            operating_cash_flow_positive=fundamentals.operating_cash_flow_positive,
            operating_margin=fundamentals.operating_margin,
            effective_tax_rate=fundamentals.effective_tax_rate,
            roic=fundamentals.roic,
            debt_to_ebitda=fundamentals.debt_to_ebitda,
            market_cap=_decimal_float(fundamentals.market_cap),
            price_to_earnings=fundamentals.pe,
            enterprise_value=_decimal_float(fundamentals.enterprise_value),
            ev_to_ebitda=fundamentals.ev_to_ebitda,
            ev_to_ebit=fundamentals.ev_to_ebit,
            free_cash_flow_yield=fundamentals.fcf_yield,
        ),
        risk=AIRiskMetrics(
            annualized_volatility_20d=technical.volatility,
            atr_14=technical.atr14,
            atr_pct=_atr_percent(technical.atr14, technical.price),
            drawdown_from_52w_high=technical.drawdown_52w,
        ),
        data_warnings=record.data_warnings,
    )


def _above(price: float | None, average: float | None) -> bool | None:
    return price > average if price is not None and average is not None else None


def _atr_percent(atr: float | None, price: float | None) -> float | None:
    return atr / price * 100 if atr is not None and price is not None and price > 0 else None


def _decimal_float(value: object) -> float | None:
    return None if value is None else float(value)
