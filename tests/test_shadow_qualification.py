"""Causal qualification regressions; no production DB, providers or journal."""

from datetime import date

import pytest
from test_champion_consolidation import offline as offline
from test_champion_consolidation import record

from trading_system.champion import champion_ranking
from trading_system.config import load_strategy_config
from trading_system.models.screening import ScreenReport
from trading_system.shadow.qualification import qualify_f_inputs

SESSION = date(2026, 10, 23)
PREVIOUS = date(2026, 10, 22)


@pytest.fixture
def config():
    return load_strategy_config()


def candidate(*, observation="OBSERVATION_PRESENT", count=320, reasons=(), **changes):
    row = record().model_copy(
        update={
            "as_of": SESSION,
            "market_history_count": count,
            "estimated_market_cap": 2_000_000_000,
            "average_dollar_volume_20d": 15_000_000,
            "current_market_observation": observation,
            "exclusion_reasons": reasons,
        }
    )
    row = row.model_copy(
        update={
            "technical": row.technical.model_copy(update={"market_session": SESSION}),
            "fundamentals": row.fundamentals.model_copy(
                update={"operating_cash_flow_positive": True}
            ),
        }
    )
    for key, value in changes.items():
        if key in {"quality", "valuation"}:
            row = row.model_copy(
                update={
                    "scores": row.scores.model_copy(
                        update={
                            key: getattr(row.scores, key).model_copy(update={"score": value}),
                        }
                    )
                }
            )
        elif key in type(row.technical).model_fields:
            row = row.model_copy(
                update={"technical": row.technical.model_copy(update={key: value})}
            )
        else:
            row = row.model_copy(update={key: value})
    return row


def report(*rows):
    return ScreenReport(
        as_of=SESSION,
        generated_at="offline fixture",
        analyzed_count=len(rows),
        eligible_count=0,
        records=rows,
    )


@pytest.mark.parametrize(
    "observation,count,as_of,market_session",
    [
        ("OBSERVATION_ZERO_VOLUME", 299, SESSION, SESSION),
        ("OBSERVATION_INVALID", 299, SESSION, SESSION),
        ("OBSERVATION_ABSENT", 0, SESSION, None),
        ("OBSERVATION_WRONG_SESSION", 298, PREVIOUS, PREVIOUS),
    ],
)
def test_independent_history_rejection(config, observation, count, as_of, market_session):
    row = candidate(
        observation=observation,
        count=count,
        as_of=as_of,
        market_session=market_session,
        reasons=("insufficient_market_history",),
        quality=None,
        valuation=None,
    )
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {row.symbol: "insufficient_market_history"}
    assert result["unresolved_rank_relevant_count"] == 0


@pytest.mark.parametrize(
    "observation,count,as_of,market_session,reasons",
    [
        ("OBSERVATION_ZERO_VOLUME", 300, SESSION, SESSION, ("insufficient_market_history",)),
        ("OBSERVATION_WRONG_SESSION", 299, PREVIOUS, PREVIOUS, ("insufficient_market_history",)),
        ("UNQUALIFIED", 10, SESSION, SESSION, ("insufficient_market_history",)),
        ("OBSERVATION_ABSENT", 0, SESSION, SESSION, ("insufficient_market_history",)),
        ("OBSERVATION_WRONG_SESSION", 0, PREVIOUS, PREVIOUS, ("insufficient_market_history",)),
        ("OBSERVATION_ZERO_VOLUME", 10, PREVIOUS, PREVIOUS, ("insufficient_market_history",)),
        (
            "OBSERVATION_ZERO_VOLUME",
            10,
            SESSION,
            SESSION,
            ("analysis_error", "insufficient_market_history"),
        ),
        ("OBSERVATION_ZERO_VOLUME", 10, SESSION, SESSION, ()),
    ],
)
def test_no_history_rescue_without_independent_proof(
    config,
    observation,
    count,
    as_of,
    market_session,
    reasons,
):
    row = candidate(
        observation=observation,
        count=count,
        as_of=as_of,
        market_session=market_session,
        reasons=reasons,
        quality=None,
        valuation=None,
    )
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {}
    assert result["unresolved_rank_relevant_count"] == 1


def test_qualified_market_cap_rejection_survives_missing_later_scores(config):
    row = candidate(
        reasons=("missing_or_small_market_cap",),
        estimated_market_cap=500_000_000,
        quality=None,
        valuation=None,
    )
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {row.symbol: "missing_or_small_market_cap"}


def test_quality_rejection_is_price_independent(config):
    row = candidate(observation="OBSERVATION_ZERO_VOLUME", quality=30, valuation=None)
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {row.symbol: "quality_threshold"}


def test_negative_ocf_rejection_survives_missing_current_observation_and_scores(config):
    row = candidate(
        observation="OBSERVATION_ABSENT",
        market_session=None,
        count=0,
        reasons=("positive_operating_cash_flow_required",),
        quality=None,
        valuation=None,
    )
    row = row.model_copy(
        update={
            "fundamentals": row.fundamentals.model_copy(
                update={"operating_cash_flow_positive": False},
            )
        }
    )
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {row.symbol: "positive_operating_cash_flow_required"}


@pytest.mark.parametrize("observation", ["OBSERVATION_INVALID", "OBSERVATION_ZERO_VOLUME"])
def test_invalid_current_derived_values_cannot_prove_rejection(config, observation):
    row = candidate(
        observation=observation,
        quality=None,
        valuation=10,
        price=1,
        momentum126=-0.5,
        sma50=200,
        sma200=200,
        sma20_rising=False,
        drawdown_52w=-0.8,
        estimated_market_cap=10,
        average_dollar_volume_20d=10,
        reasons=("invalid_or_low_price", "missing_or_small_market_cap", "insufficient_liquidity"),
    )
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {}
    assert result["unresolved_rank_relevant_count"] == 1


def test_missing_quality_without_independent_failure_stays_unresolved(config):
    row = candidate(quality=None)
    result = qualify_f_inputs(report(row), SESSION, config)
    assert result["rejected"] == {}
    assert result["incomplete_inputs"] == [
        {"symbol": row.symbol, "reasons": ["QUALITY_UNAVAILABLE"]}
    ]


def test_complete_input_ranking_and_scores_unchanged(config):
    rows = (candidate(symbol="BBB"), candidate(symbol="AAA"), candidate(symbol="LOW", quality=10))
    raw = report(*rows)
    before = champion_ranking(raw, config)
    result = qualify_f_inputs(raw, SESSION, config)
    assert result["unresolved_rank_relevant_count"] == 0
    assert result["deterministically_rejected_count"] == 1
    assert result["fully_evaluated_count"] == 2
    assert champion_ranking(raw, config) == before
    assert [r.symbol for r in before.records if r.eligible] == ["AAA", "BBB"]
