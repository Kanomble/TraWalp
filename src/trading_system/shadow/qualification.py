"""Causal input completeness around the unchanged production F evaluator.

Any independently observed mandatory failure proves rejection. Unknown values
are never interpreted as a failure. Only potentially eligible rows need all
downstream inputs; only the canonical rank one needs ATR for a new intent.
"""

from collections import Counter

from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.backtest.screen_strategies import evaluate_strategy_gate
from trading_system.champion import champion_ranking


def _independent_history_failure(row, session, config):
    """Stored row counts cannot prove listing age or historical unavailability.

    There is currently no authoritative listing-history evidence in ScreenRecord.
    A short history therefore cannot independently prove strategy ineligibility.
    """
    return False


def qualify_f_inputs(report, session, config, *, ranked=None, history_coverage=None):
    # False requests input qualification before any cross-sectional F ranking.
    ranked = ranked if ranked is not None else champion_ranking(report, config)
    rejected, evaluated, unresolved = {}, [], []
    eligible = [] if ranked is False else [row for row in ranked.records if row.eligible]
    structural = []
    if report.as_of != session or not report.records:
        structural.append({"symbol": None, "reason": "EMPTY_OR_WRONG_SESSION_CROSS_SECTION"})
    if report.analyzed_count != len(report.records) or len(
        {r.symbol for r in report.records}
    ) != len(report.records):
        structural.append({"symbol": None, "reason": "CROSS_SECTION_MEMBERSHIP_INCONSISTENT"})
    for row in sorted(report.records, key=lambda r: r.symbol):
        reasons = set(row.exclusion_reasons)
        tech, scores = row.technical, row.scores
        history_known = (
            history_coverage.get(row.symbol, {}).get("status") == "complete"
            if history_coverage is not None
            else row.market_history_count >= config.data_quality.min_market_history_days
        )
        observed = (
            row.current_market_observation == "OBSERVATION_PRESENT"
            and row.as_of == session
            and tech.market_session == session
            and "stale_market_data" not in reasons
        )
        failures = sorted(
            reasons.intersection({"identity_conflict", "financial_excluded", "reit_excluded"})
        )
        # These are canonical hard rejections, qualified by their actual evidence.
        for reason, known in (
            ("invalid_or_low_price", observed and tech.price is not None),
            ("missing_or_small_market_cap", observed and row.estimated_market_cap is not None),
            (
                "insufficient_liquidity",
                history_known and observed and row.average_dollar_volume_20d is not None,
            ),
            (
                "positive_operating_cash_flow_required",
                row.fundamentals.operating_cash_flow_positive is False,
            ),
        ):
            if reason in reasons and known:
                failures.append(reason)
        if (
            row.as_of == session
            and "stale_market_data" not in reasons
            and scores.quality.score is not None
            and scores.quality.score < config.backtest.min_quality_score
        ):
            failures.append("quality_threshold")
        if (
            observed
            and scores.valuation.score is not None
            and scores.valuation.score < config.backtest.min_valuation_score
        ):
            failures.append("valuation_threshold")
        gate = evaluate_strategy_gate(tech, FROZEN_CHAMPION_F.variant, config)
        if observed and history_known:
            failures.extend(
                reason for reason in gate.blocking_reasons if "unavailable" not in reason
            )
        if failures:
            rejected[row.symbol] = failures[0]
            continue
        missing = []
        if not history_known:
            missing.append("MARKET_HISTORY_UNPROVEN")
        if not observed:
            missing.append(f"CURRENT_SESSION:{row.current_market_observation}")
        for name in ("analysis_error", "no_point_in_time_fundamentals", "stale_market_data"):
            if name in reasons:
                missing.append(name)
        for name in ("quality", "valuation"):
            if getattr(scores, name).score is None:
                missing.append(f"{name.upper()}_UNAVAILABLE")
        missing.extend(reason for reason in gate.blocking_reasons if "unavailable" in reason)
        evaluation = (
            evaluate_variant_entry(row, FROZEN_CHAMPION_F.variant, config) if not missing else None
        )
        if evaluation is not None and not evaluation.eligible:
            # Unrecognized hard exclusions are not silently declared resolved.
            missing.append(f"UNRESOLVED_CANONICAL_EXCLUSION:{evaluation.first_failure}")
        if missing:
            unresolved.append({"symbol": row.symbol, "reasons": sorted(set(missing))})
        else:
            evaluated.append(row.symbol)
    if eligible:
        selected = min(eligible, key=lambda r: r.rank)
        if (selected.technical.atr14 is None or selected.technical.atr14 <= 0) and not any(
            item["symbol"] == selected.symbol for item in unresolved
        ):
            unresolved.append(
                {"symbol": selected.symbol, "reasons": ["SELECTED_ATR14_UNAVAILABLE"]}
            )
            if selected.symbol in evaluated:
                evaluated.remove(selected.symbol)
    return {
        "source_universe_count": len(report.records),
        "deterministically_rejected_count": len(rejected),
        "fully_evaluated_count": len(evaluated),
        "eligible_count": len(eligible),
        "unresolved_rank_relevant_count": len(unresolved) + len(structural),
        "incomplete_inputs": structural + unresolved,
        "rejected": rejected,
        "rejection_summary": dict(sorted(Counter(rejected.values()).items())),
    }


def qualified_forward_screen(raw, session, config, *, history_coverage=None):
    """Never rank candidates against an unresolved economic cross-section."""
    qualification = qualify_f_inputs(
        raw, session, config, ranked=False, history_coverage=history_coverage
    )
    if not qualification["unresolved_rank_relevant_count"]:
        ranked = champion_ranking(raw, config)
        qualification = qualify_f_inputs(
            raw, session, config, ranked=ranked, history_coverage=history_coverage
        )
        if not qualification["unresolved_rank_relevant_count"]:
            return ranked, qualification
    # Keep diagnostic features for held risk management, but publish no ranking.
    qualification["eligible_count"] = 0
    return raw.model_copy(
        update={
            "eligible_count": 0,
            "records": tuple(
                row.model_copy(update={"eligible": False, "rank": None}) for row in raw.records
            ),
        }
    ), qualification
