import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.backtest.candidate_audit import (
    FUNNEL_STAGES,
    HistoricalCandidateAuditCollector,
    distribution,
)
from trading_system.backtest.engine import BacktestEngine, evaluate_variant_entry
from trading_system.backtest.report import export_candidate_audit
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.backtest import StrategyVariant
from trading_system.models.fundamentals import FundamentalMetrics
from trading_system.models.market_data import DailyBar
from trading_system.models.scores import ScoreBreakdown, StockScores
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.models.signals import TechnicalSnapshot


def _score(name: str, value: float | None) -> ScoreBreakdown:
    return ScoreBreakdown(
        name=name,
        score=value,
        factors=(),
        available_factor_count=1 if value is not None else 0,
        reason_score_unavailable=None if value is not None else "missing inputs",
    )


def _record(
    symbol: str,
    *,
    quality: float | None = 90,
    valuation: float | None = 85,
    opportunity: float | None = 80,
    timing: float | None = 75,
    exclusions: tuple[str, ...] = (),
    price: float | None = 110,
    sma20: float | None = 100,
    rsi_recovery: bool | None = True,
    momentum5: float | None = 0.01,
    relative_volume: float | None = 1.4,
    market_history_count: int = 252,
    pit_fact_count: int = 20,
) -> ScreenRecord:
    return ScreenRecord(
        symbol=symbol,
        name=symbol,
        as_of=date(2024, 1, 5),
        sic="3571",
        eligible=not exclusions,
        exclusion_reasons=exclusions,
        average_dollar_volume_20d=20_000_000,
        fundamentals=FundamentalMetrics(
            revenue_growth=0.2,
            eps_growth=0.2,
            operating_cash_flow_growth=0.1,
            operating_cash_flow_positive=True,
            operating_margin=0.2,
            roic=0.15,
            debt_to_ebitda=1.0,
            fcf_yield=0.06,
        ),
        technical=TechnicalSnapshot(
            market_session=date(2024, 1, 5),
            price=price,
            sma20=sma20,
            sma50=100,
            sma200=100,
            sma20_rising=True,
            rsi14=45,
            rsi_recovery=rsi_recovery,
            momentum5=momentum5,
            momentum126=0.10,
            atr14=5,
            relative_volume=relative_volume,
            drawdown_52w=-0.05,
            drawdown_63d=-0.10,
            recovery_from_63d_low=0.10,
            max_drawdown_126d=-0.20,
            sma200_distance=0.10,
        ),
        scores=StockScores(
            quality=_score("quality", quality),
            valuation=_score("valuation", valuation),
            opportunity=_score("opportunity", opportunity),
            timing=_score("timing", timing),
            total=(
                None
                if any(value is None for value in (quality, valuation, opportunity, timing))
                else 82
            ),
        ),
        market_history_count=market_history_count,
        pit_fact_count=pit_fact_count,
        estimated_market_cap=2_000_000_000,
        latest_pit_filing_date=date(2024, 1, 2) if pit_fact_count else None,
        latest_pit_period_end=date(2023, 9, 30) if pit_fact_count else None,
    )


def _report(session: date, records: tuple[ScreenRecord, ...]) -> ScreenReport:
    return ScreenReport(
        as_of=session,
        requested_as_of=session,
        effective_market_session=session,
        generated_at="2024-01-05T22:00:00+00:00",
        analyzed_count=len(records),
        eligible_count=sum(record.eligible for record in records),
        records=records,
    )


def _config():
    load_settings.cache_clear()
    return load_settings().strategy


def test_entry_evaluation_distinguishes_missing_data_from_threshold_failure() -> None:
    config = _config()

    missing = evaluate_variant_entry(_record("MISS", quality=None), StrategyVariant.FULL, config)
    rejected = evaluate_variant_entry(
        _record("LOW", quality=config.backtest.min_quality_score - 1),
        StrategyVariant.FULL,
        config,
    )

    assert missing.first_failure == rejected.first_failure == "quality_threshold"
    assert missing.failure_detail == "quality_score_unavailable"
    assert rejected.failure_detail == "quality_threshold"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"rsi_recovery": True, "momentum5": -0.1, "relative_volume": 0.5}, True),
        ({"rsi_recovery": False, "momentum5": 0.1, "relative_volume": 0.5}, True),
        ({"rsi_recovery": False, "momentum5": -0.1, "relative_volume": 2.0}, True),
        ({"rsi_recovery": False, "momentum5": -0.1, "relative_volume": 0.5}, False),
    ],
)
def test_recovery_gate_audit_uses_canonical_entry_logic(updates, expected) -> None:
    evaluation = evaluate_variant_entry(_record("REC", **updates), StrategyVariant.FULL, _config())

    assert evaluation.recovery_gate_pass is expected
    assert evaluation.eligible is expected


def test_missing_recovery_inputs_are_not_treated_as_false_signals() -> None:
    config = _config()
    missing = evaluate_variant_entry(
        _record(
            "MISSING",
            rsi_recovery=None,
            momentum5=None,
            relative_volume=None,
        ),
        StrategyVariant.FULL,
        config,
    )
    rejected = evaluate_variant_entry(
        _record(
            "FALSE",
            rsi_recovery=False,
            momentum5=-0.1,
            relative_volume=0.5,
        ),
        StrategyVariant.FULL,
        config,
    )

    assert missing.failure_detail == "recovery_inputs_unavailable"
    assert missing.recovery_gate_pass is None
    assert rejected.failure_detail == "recovery_signal_required"
    assert rejected.recovery_gate_pass is False


def test_candidate_funnel_conserves_first_failures_and_categories() -> None:
    config = _config()
    session = date(2024, 1, 5)
    records = (
        _record(
            "IDENTITY",
            exclusions=("identity_conflict",),
            market_history_count=0,
            pit_fact_count=0,
        ),
        _record(
            "NOHISTORY",
            exclusions=("insufficient_market_history",),
            market_history_count=0,
            pit_fact_count=0,
        ),
        _record("LOWQUALITY", quality=config.backtest.min_quality_score - 1),
        _record("ELIGIBLE"),
    )
    collector = HistoricalCandidateAuditCollector(config, StrategyVariant.FULL)

    collector.observe_screen(_report(session, records), StrategyVariant.FULL, config)
    summary = collector._session_model(collector._states[session])  # noqa: SLF001

    assert summary.universe_total == 4
    assert summary.identity_valid == 3
    assert summary.market_history_available == 2
    assert summary.quality_threshold_pass == 1
    assert summary.eligible_candidates == 1
    assert summary.data_quality_failures == 2
    assert summary.strategy_rejections == 1
    for stage, incoming in summary.stage_incoming.items():
        passed_field = dict(FUNNEL_STAGES)[stage]
        assert incoming == summary.stage_rejected.get(stage, 0) + getattr(summary, passed_field)


def test_distribution_and_threshold_quantiles_are_deterministic() -> None:
    summary = distribution([1, 2, 3, 4, 5])

    assert summary.count == 5
    assert summary.mean == 3
    assert summary.median == 3
    assert summary.p10 == pytest.approx(1.4)
    assert summary.p90 == pytest.approx(4.6)


class _Screens:
    def __init__(self, records: tuple[ScreenRecord, ...]) -> None:
        self.records = records

    def screen(self, session: date) -> ScreenReport:
        return _report(session, self.records)


def _bar(symbol: str, session: date) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        timestamp=datetime(session.year, session.month, session.day, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000_000,
    )


def test_portfolio_blockers_are_separate_from_three_eligible_candidates(tmp_path) -> None:
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    tuesday = date(2024, 1, 9)
    records = (_record("AAA"), _record("BBB"), _record("CCC"))
    database = Database(tmp_path / "audit.sqlite3")
    database.initialize()
    database.upsert_bars(
        [
            _bar(symbol, session)
            for symbol in ("AAA", "BBB", "CCC")
            for session in (friday, monday, tuesday)
        ]
    )
    config = _config().model_copy(
        update={
            "portfolio": _config().portfolio.model_copy(update={"max_positions": 1}),
            "backtest": _config().backtest.model_copy(
                update={
                    "min_quality_score": 0,
                    "min_valuation_score": 0,
                    "min_opportunity_score": 0,
                    "min_timing_score": 0,
                    "min_total_score": 0,
                }
            ),
        }
    )
    collector = HistoricalCandidateAuditCollector(config, StrategyVariant.FULL)

    result = BacktestEngine(
        database,
        config,
        screen_source=_Screens(records),
        audit_observer=collector,
    ).run(friday, tuesday)
    audit = collector.finalize(result)

    first = audit.sessions[0]
    second = audit.sessions[1]
    assert first.eligible_candidates == 3
    assert first.entry_orders_created == 1
    assert first.portfolio_blockers == {"max_positions_reached": 2}
    assert first.actual_entries == 1
    assert second.eligible_candidates == 3
    assert second.actual_entries == 0
    assert second.portfolio_blockers == {
        "already_holding_symbol": 1,
        "max_positions_reached": 2,
    }
    assert audit.candidate_symbols == ("AAA", "BBB", "CCC")
    assert len(audit.entry_symbols) == 1
    assert audit.data_coverage["relative_volume"]["relative_volume_missing"] == 0
    paths = export_candidate_audit(audit, tmp_path)
    candidate_payload = json.loads(paths["intraday_candidates"].read_text(encoding="utf-8"))
    assert {item["symbol"] for item in candidate_payload["candidates"]} == {
        "AAA",
        "BBB",
        "CCC",
    }


def test_future_sessions_change_only_later_audit_records() -> None:
    config = _config()
    first = date(2024, 1, 5)
    later = date(2024, 1, 8)
    collector = HistoricalCandidateAuditCollector(config, StrategyVariant.FULL)
    collector.observe_screen(_report(first, (_record("AAA"),)), StrategyVariant.FULL, config)
    before = collector._session_model(collector._states[first])  # noqa: SLF001
    collector.observe_screen(
        _report(later, (_record("AAA", quality=1),)), StrategyVariant.FULL, config
    )
    after = collector._session_model(collector._states[first])  # noqa: SLF001

    assert before == after


def test_d_audit_exposes_c_score_rejection_reason_and_loss_path_evidence() -> None:
    config = _config()
    session = date(2024, 1, 5)
    record = _record("DAMAGED").model_copy(
        update={
            "technical": _record("DAMAGED").technical.model_copy(
                update={"max_drawdown_126d": -0.47}
            )
        }
    )
    assert evaluate_variant_entry(record, StrategyVariant.FULL, config).eligible
    collector = HistoricalCandidateAuditCollector(config, StrategyVariant.LOSS_AWARE_RECOVERY)

    collector.observe_screen(
        _report(session, (record,)), StrategyVariant.LOSS_AWARE_RECOVERY, config
    )

    near_miss = collector._near_misses[0]  # noqa: SLF001
    assert near_miss.failed_at == "loss_aware_max_drawdown_exceeded"
    assert near_miss.variant_score == pytest.approx(
        evaluate_variant_entry(record, StrategyVariant.FULL, config).score
    )
    assert near_miss.technical_evidence["max_drawdown_126d"] == -0.47
    assert near_miss.technical_evidence["recovery_from_63d_low"] == 0.10


@pytest.mark.parametrize(
    "variant",
    [
        StrategyVariant.TREND_PULLBACK,
        StrategyVariant.QUALITY_VALUE_MOMENTUM,
    ],
)
def test_e_and_f_candidate_audit_retains_entry_evidence(variant: StrategyVariant) -> None:
    config = _config()
    session = date(2024, 1, 5)
    collector = HistoricalCandidateAuditCollector(config, variant)

    collector.observe_screen(_report(session, (_record("AAA"),)), variant, config)

    state = collector._candidates[(session, "AAA")]  # noqa: SLF001
    assert state.technical_evidence == {
        "price": 110,
        "sma20": 100,
        "sma50": 100,
        "sma200": 100,
        "sma20_rising": True,
        "momentum5": 0.01,
        "momentum126": 0.10,
        "drawdown_52w": -0.05,
        "drawdown_63d": -0.10,
        "recovery_from_63d_low": 0.10,
        "max_drawdown_126d": -0.20,
        "sma200_distance": 0.10,
    }


def test_future_pit_provenance_is_reported_as_pipeline_inconsistency() -> None:
    config = _config()
    session = date(2024, 1, 5)
    record = _record("LEAK").model_copy(
        update={
            "latest_pit_filing_date": date(2024, 1, 8),
            "technical": _record("LEAK").technical.model_copy(
                update={"market_session": date(2024, 1, 8)}
            ),
        }
    )
    collector = HistoricalCandidateAuditCollector(config, StrategyVariant.FULL)

    collector.observe_screen(_report(session, (record,)), StrategyVariant.FULL, config)

    assert collector._pipeline_inconsistencies == {  # noqa: SLF001
        "future_filing_used": 1,
        "future_market_bar_used": 1,
    }
