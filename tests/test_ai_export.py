import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system import cli
from trading_system.ai.export import NoAICandidatesError, export_ai_candidates
from trading_system.config import StorageConfig, load_settings
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def _breakdown(name: str, score: float) -> ScoreBreakdown:
    return ScoreBreakdown(
        name=name,
        score=score,
        factors=(),
        available_factor_count=1,
    )


def _record(
    symbol: str,
    rank: int,
    score: float,
    *,
    missing_metrics: bool = False,
) -> ScreenRecord:
    scores = StockScores(
        quality=_breakdown("quality", score - 1),
        valuation=_breakdown("valuation", score - 2),
        opportunity=_breakdown("opportunity", score - 3),
        timing=_breakdown("timing", score - 4),
        total=score,
    )
    technical = (
        TechnicalSnapshot(price=125.0, momentum5=0.04, volatility=0.31)
        if missing_metrics
        else TechnicalSnapshot(
            market_session=date(2026, 8, 10),
            price=125.0,
            sma20=120.0,
            sma50=115.0,
            sma200=100.0,
            sma20_rising=True,
            rsi14=58.2,
            momentum5=0.04,
            momentum20=0.11,
            momentum20_improving=True,
            momentum63=0.18,
            momentum126=0.25,
            volatility=0.31,
            atr14=3.875,
            relative_volume=1.42,
            drawdown_52w=-0.12,
        )
    )
    fundamentals = (
        FundamentalMetrics()
        if missing_metrics
        else FundamentalMetrics(
            revenue_growth=0.18,
            eps_growth=0.24,
            operating_cash_flow_growth=0.16,
            operating_cash_flow_positive=True,
            operating_margin=0.21,
            roic=0.19,
            debt_to_ebitda=0.8,
            market_cap=Decimal("200000000000"),
            pe=28.5,
            fcf_yield=0.035,
        )
    )
    return ScreenRecord(
        symbol=symbol,
        name=f"{symbol} Corp",
        as_of=date(2026, 8, 10),
        rank=rank,
        eligible=True,
        average_dollar_volume_20d=50_000_000,
        fundamentals=fundamentals,
        technical=technical,
        scores=scores,
    )


def _report(*records: ScreenRecord) -> ScreenReport:
    return ScreenReport(
        as_of=date(2026, 8, 10),
        requested_as_of=date(2026, 8, 11),
        effective_market_session=date(2026, 8, 10),
        generated_at="2026-08-11T10:30:00+02:00",
        analyzed_count=len(records),
        eligible_count=len(records),
        records=records,
    )


def test_export_orders_candidates_enforces_limit_and_creates_directory(tmp_path) -> None:
    output = tmp_path / "missing" / "nested" / "candidates.json"
    report = _report(
        _record("CCC", 3, 81.0),
        _record("AAA", 1, 89.0),
        _record("BBB", 2, 85.0, missing_metrics=True),
    )

    result = export_ai_candidates(
        report,
        limit=2,
        output_path=output,
        generated_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
    )

    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert result.path == output
    assert parsed["candidate_count"] == 2
    assert [candidate["symbol"] for candidate in parsed["candidates"]] == ["AAA", "BBB"]
    assert parsed["candidates"][0]["technical"]["above_sma_200"] is True
    assert parsed["candidates"][0]["risk"]["atr_pct"] == pytest.approx(3.1)


def test_export_preserves_missing_values_as_json_null(tmp_path) -> None:
    output = tmp_path / "candidates.json"

    export_ai_candidates(
        _report(_record("NULL", 1, 80.0, missing_metrics=True)),
        output_path=output,
        generated_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
    )

    candidate = json.loads(output.read_text(encoding="utf-8"))["candidates"][0]
    assert candidate["technical"]["sma_20"] is None
    assert candidate["technical"]["above_sma_20"] is None
    assert candidate["fundamentals"]["revenue_growth_yoy"] is None
    assert candidate["risk"]["atr_pct"] is None


def test_export_uses_default_timestamped_filename(tmp_path) -> None:
    result = export_ai_candidates(
        _report(_record("AAA", 1, 89.0)),
        output_directory=tmp_path,
        generated_at=datetime(2026, 8, 11, 10, 30, tzinfo=UTC),
    )

    assert result.path == tmp_path / "ai_candidates_2026-08-11_103000.json"
    assert result.path.is_file()
    assert json.loads(result.path.read_text(encoding="utf-8"))["strategy"]["name"] == (
        "short_term_long_swing"
    )


def test_export_without_eligible_candidates_fails_without_creating_file(tmp_path) -> None:
    output = tmp_path / "missing" / "candidates.json"

    with pytest.raises(NoAICandidatesError, match="No eligible screened candidates"):
        export_ai_candidates(_report(), output_path=output)

    assert not output.exists()
    assert not output.parent.exists()


def test_export_ai_cli_respects_limit_without_api_credentials(
    tmp_path, monkeypatch, capsys
) -> None:
    report = _report(_record("AAA", 1, 89.0), _record("BBB", 2, 85.0))

    class CandidateScreener:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _as_of: date) -> ScreenReport:
            return report

    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "test.sqlite3", reports_path=tmp_path / "reports"
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "Screener", CandidateScreener)
    output = tmp_path / "exports" / "one.json"

    result = cli.main(
        [
            "export-ai",
            "--as-of",
            "2026-08-11",
            "--limit",
            "1",
            "--output",
            str(output),
        ]
    )

    console = capsys.readouterr().out
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert parsed["candidate_count"] == 1
    assert parsed["candidates"][0]["symbol"] == "AAA"
    assert "AI candidate export complete" in console
    assert "Top quant candidate: AAA (89.0)" in console

    class EmptyScreener(CandidateScreener):
        def run(self, _as_of: date) -> ScreenReport:
            return _report()

    monkeypatch.setattr(cli, "Screener", EmptyScreener)
    empty_output = tmp_path / "exports" / "empty.json"

    assert cli.main(["export-ai", "--output", str(empty_output)]) == 1
    assert "No eligible screened candidates" in capsys.readouterr().err
    assert not empty_output.exists()
