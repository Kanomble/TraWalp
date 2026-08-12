"""Human- and machine-readable Milestone-3 screen reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from trading_system.models.fundamentals import FundamentalDebugReport
from trading_system.models.scores import ScoreBreakdown
from trading_system.models.screening import MarketDebug, PeerDebug, ScreenRecord, ScreenReport


def report_paths(report: ScreenReport, directory: Path) -> tuple[Path, Path]:
    stem = f"screen_{report.as_of.isoformat()}"
    return directory / f"{stem}.csv", directory / f"{stem}.json"


def export_report(report: ScreenReport, directory: str | Path) -> tuple[Path, Path]:
    """Export all analyzed records atomically; eligibility/rank identify the shortlist."""

    output_directory = Path(directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = report_paths(report, output_directory)
    rows = [_flat_record(record) for record in report.records]
    _atomic_text(csv_path, pd.DataFrame(rows).to_csv(index=False))
    payload = report.model_dump(mode="json")
    _atomic_text(json_path, json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    return csv_path, json_path


def load_report(path: str | Path) -> ScreenReport:
    return ScreenReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def format_screen_table(report: ScreenReport, *, limit: int | None = None) -> str:
    eligible = [record for record in report.records if record.eligible]
    if limit is not None:
        eligible = eligible[:limit]
    headers = ("Rank", "Symbol", "Total", "Quality", "Value", "Opportunity", "Timing")
    rows = [
        (
            str(record.rank),
            record.symbol,
            _score(record.scores.total),
            _score(record.scores.quality.score),
            _score(record.scores.valuation.score),
            _score(record.scores.opportunity.score),
            _score(record.scores.timing.score),
        )
        for record in eligible
    ]
    if not rows:
        return "No stocks passed the configured hard filters."
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(7)]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


def format_explanation(record: ScreenRecord) -> str:
    fundamentals = record.fundamentals
    technical = record.technical
    relative_pe = _factor_raw(record.scores.valuation, "relative_pe")
    relative_ev = _factor_raw(record.scores.valuation, "relative_ev_ebitda")
    status = "ELIGIBLE" if record.eligible else "EXCLUDED"
    lines = [
        f"{record.symbol} — {record.name}",
        f"As of: {record.as_of.isoformat()} | Status: {status} | "
        f"SIC: {record.sic or 'N/A'} | Peer: {record.peer_group or 'N/A'}",
    ]
    if record.exclusion_reasons:
        lines.append(f"Exclusion reasons: {', '.join(record.exclusion_reasons)}")
    lines.extend(
        [
            "",
            f"QUALITY: {_score(record.scores.quality.score)}/100",
            f"Revenue Growth: {_percent(fundamentals.revenue_growth)}",
            f"EPS Growth: {_percent(fundamentals.eps_growth)}",
            f"OCF Growth: {_percent(fundamentals.operating_cash_flow_growth)}",
            f"Positive OCF: {_value(fundamentals.operating_cash_flow_positive)}",
            f"Operating Margin: {_percent(fundamentals.operating_margin)}",
            f"ROIC: {_percent(fundamentals.roic)}",
            f"Debt/EBITDA: {_multiple(fundamentals.debt_to_ebitda)}",
            *_factor_lines(record.scores.quality),
            "",
            f"VALUATION: {_score(record.scores.valuation.score)}/100",
            f"Price: {_money(technical.price)}",
            f"Market Cap: {_money(fundamentals.market_cap)}",
            f"P/E: {_multiple(fundamentals.pe)}",
            f"Industry Median P/E: {_multiple(record.industry_medians.get('pe'))}",
            f"Relative P/E: {_multiple(relative_pe)}",
            f"EV/EBITDA: {_multiple(fundamentals.ev_to_ebitda)}",
            f"Industry Median EV/EBITDA: {_multiple(record.industry_medians.get('ev_to_ebitda'))}",
            f"Relative EV/EBITDA: {_multiple(relative_ev)}",
            f"EV/EBIT fallback: {_multiple(fundamentals.ev_to_ebit)}",
            f"Industry Median EV/EBIT: {_multiple(record.industry_medians.get('ev_to_ebit'))}",
            f"FCF Yield: {_percent(fundamentals.fcf_yield)}",
            *_factor_lines(record.scores.valuation),
            "",
            f"OPPORTUNITY: {_score(record.scores.opportunity.score)}/100",
            f"52W Drawdown: {_percent(technical.drawdown_52w)}",
            f"1M Return (20d): {_percent(technical.momentum20)}",
            f"3M Return (63d): {_percent(technical.momentum63)}",
            f"6M Return (126d): {_percent(technical.momentum126)}",
            f"Annualized Volatility: {_percent(technical.volatility)}",
            *_factor_lines(record.scores.opportunity),
            "",
            f"TIMING: {_score(record.scores.timing.score)}/100",
            f"RSI14: {_number(technical.rsi14)}",
            f"RSI Recovery: {_value(technical.rsi_recovery)}",
            f"SMA20: {_money(technical.sma20)} | Rising: {_value(technical.sma20_rising)}",
            f"SMA50: {_money(technical.sma50)} | SMA200: {_money(technical.sma200)}",
            f"Momentum5: {_percent(technical.momentum5)}",
            f"Momentum20 improving: {_value(technical.momentum20_improving)}",
            f"Relative Volume: {_multiple(technical.relative_volume)}",
            f"ATR14: {_money(technical.atr14)}",
            *_factor_lines(record.scores.timing),
            "",
            f"TOTAL: {_score(record.scores.total)}/100",
        ]
    )
    if record.data_warnings:
        lines.extend(["", f"Data warnings: {', '.join(record.data_warnings)}"])
    return "\n".join(lines)


def format_peer_debug(debug: PeerDebug) -> str:
    return "\n".join(
        [
            debug.symbol,
            f"SIC: {debug.sic or 'N/A'}",
            f"Exact 4-digit peers: {debug.exact_peer_count}",
            f"3-digit fallback peers: {debug.three_digit_peer_count}",
            f"2-digit fallback peers: {debug.two_digit_peer_count}",
            f"Selected peer group: {debug.selected_group or 'N/A'}",
            f"Selected peer count: {debug.selected_peer_count}",
            f"Minimum peer count: {debug.minimum_peer_count}",
            f"Valid P/E peers: {debug.valid_pe_count}",
            f"Median P/E: {_multiple(debug.median_pe)}",
            f"Valid EV/EBITDA peers: {debug.valid_ev_ebitda_count}",
            f"Median EV/EBITDA: {_multiple(debug.median_ev_ebitda)}",
            f"Valid EV/EBIT fallback peers: {debug.valid_ev_ebit_count}",
            f"Median EV/EBIT: {_multiple(debug.median_ev_ebit)}",
        ]
    )


def format_fundamental_debug(report: FundamentalDebugReport) -> str:
    lines = [report.symbol, f"As of: {report.as_of.isoformat()}"]
    for item in report.items:
        lines.extend(
            [
                "",
                f"{item.name}: {_value(item.value)} {item.unit or ''}".rstrip(),
                f"  XBRL Concept: {', '.join(item.xbrl_concepts) or 'N/A'}",
                f"  Source Filing: {', '.join(item.source_filings) or 'N/A'}",
                f"  Fiscal Period: {', '.join(item.fiscal_periods) or 'N/A'}",
                "  Filed Date: "
                + (", ".join(value.isoformat() for value in item.filed_dates) or "N/A"),
                f"  Unit: {item.unit or 'N/A'}",
                f"  Formula: {item.formula}",
            ]
        )
    return "\n".join(lines)


def format_market_debug(debug: MarketDebug) -> str:
    lines = [
        debug.symbol,
        f"Requested as-of: {debug.requested_as_of.isoformat()}",
        f"Effective completed session: {debug.effective_market_session.isoformat()}",
        f"Actual latest bar session: {_value(debug.actual_latest_bar_session)}",
        f"Requested Alpaca start: {debug.requested_alpaca_start.isoformat()}",
        f"Requested Alpaca end (exclusive): {debug.requested_alpaca_end_exclusive.isoformat()}",
        f"Feed: {debug.feed}",
        f"Adjustment: {debug.adjustment}",
        f"Bar count: {debug.bar_count}",
        "Last 10 completed daily bars:",
    ]
    lines.extend(
        f"  {bar.timestamp.date()} O={bar.open} H={bar.high} L={bar.low} "
        f"C={bar.close} V={bar.volume}"
        for bar in debug.last_bars
    )
    lines.extend(
        [
            f"Latest completed close: {_money(debug.latest_completed_close)}",
            f"SMA20: {_money(debug.sma20)}",
            f"SMA50: {_money(debug.sma50)}",
            f"SMA200: {_money(debug.sma200)}",
            f"RSI14: {_number(debug.rsi14)}",
            f"Momentum5: {_percent(debug.momentum5)}",
            f"Momentum20: {_percent(debug.momentum20)}",
            f"Momentum63: {_percent(debug.momentum63)}",
            f"52W High: {_money(debug.high_52w)}",
            f"Drawdown: {_percent(debug.drawdown_52w)}",
            f"ATR14: {_money(debug.atr14)}",
            f"Average Volume20 (prior sessions): {_number(debug.average_volume20)}",
            f"Relative Volume: {_multiple(debug.relative_volume)}",
        ]
    )
    return "\n".join(lines)


def _flat_record(record: ScreenRecord) -> dict[str, Any]:
    fundamentals = record.fundamentals.model_dump(mode="json")
    technical = record.technical.model_dump(mode="json")
    row: dict[str, Any] = {
        "rank": record.rank,
        "symbol": record.symbol,
        "name": record.name,
        "as_of": record.as_of.isoformat(),
        "eligible": record.eligible,
        "exclusion_reasons": "|".join(record.exclusion_reasons),
        "data_warnings": "|".join(record.data_warnings),
        "sic": record.sic,
        "peer_group": record.peer_group,
        "total_score": record.scores.total,
        "quality_score": record.scores.quality.score,
        "valuation_score": record.scores.valuation.score,
        "opportunity_score": record.scores.opportunity.score,
        "timing_score": record.scores.timing.score,
        "average_dollar_volume_20d": record.average_dollar_volume_20d,
        **fundamentals,
        **technical,
    }
    row.update(
        {f"industry_median_{name}": value for name, value in record.industry_medians.items()}
    )
    for breakdown in (
        record.scores.quality,
        record.scores.valuation,
        record.scores.opportunity,
        record.scores.timing,
    ):
        for factor in breakdown.factors:
            prefix = f"factor_{breakdown.name}_{factor.name}"
            row[f"{prefix}_raw"] = factor.raw_value
            row[f"{prefix}_score"] = factor.score
            row[f"{prefix}_configured_weight"] = factor.configured_weight
            row[f"{prefix}_normalized_available_weight"] = factor.normalized_available_weight
            row[f"{prefix}_effective_weight"] = factor.effective_weight
    return row


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _factor_raw(breakdown: ScoreBreakdown, name: str) -> float | bool | None:
    factor = next((factor for factor in breakdown.factors if factor.name == name), None)
    return factor.raw_value if factor else None


def _factor_lines(breakdown: ScoreBreakdown) -> list[str]:
    lines = [
        f"  · {factor.name}: {_score(factor.score)}/100 "
        f"(configured {factor.configured_weight:.1%}, "
        f"normalized available {factor.normalized_available_weight:.1%}, "
        f"effective {factor.effective_weight:.1%})"
        for factor in breakdown.factors
    ]
    if breakdown.reason_score_unavailable:
        lines.append(f"  Score unavailable: {breakdown.reason_score_unavailable}")
    return lines


def _score(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}"


def _number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _multiple(value: float | bool | None) -> str:
    return "N/A" if value is None or isinstance(value, bool) else f"{value:.2f}x"


def _money(value: object) -> str:
    return "N/A" if value is None else f"${float(value):,.2f}"


def _value(value: object) -> str:
    return "N/A" if value is None else str(value)
