"""Local operational state + one canonical screen, reusable by init and run."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading_system.champion import champion_ranking
from trading_system.data.market_sessions import (
    latest_completed_trading_session,
    regular_session_bounds,
)
from trading_system.data.observations import current_session_observation
from trading_system.shadow.contracts import digest, normalized
from trading_system.shadow.qualification import qualify_f_inputs
from trading_system.strategy.screener import Screener

READINESS_CONTRACT = "shadow-input-readiness/v1"
# A weekend plus a one-day exchange holiday. This is an operational snapshot
# policy, not a requirement for a new SEC filing from every issuer every day.
SNAPSHOT_MAX_AGE = timedelta(hours=96)


@dataclass(frozen=True)
class ShadowInputReadiness:
    details: dict
    raw_report: object
    ranked_report: object
    qualification: dict

    @property
    def ready(self):
        return self.details["ready"]

    def as_dict(self):
        return normalized(self.details)


def evaluate_shadow_input_readiness(
    database, config, *, now=None, session=None, screen_source=None
):
    """No writes, clients, sync or per-symbol readiness SQL queries.

    Database must be a LocalInputs pinned read-only snapshot. Screening uses the
    exact screen_champion production operations once; the raw report is retained
    because champion_ranking intentionally replaces legacy exclusion reasons.
    """
    now = now or datetime.now(UTC)
    latest = latest_completed_trading_session(now)
    session = session or latest
    regular_session_bounds(session)
    if session > latest:
        raise ValueError("Readiness requires a completed official session")
    states = database.sync_values("dataset")
    raw = (
        screen_source.screen(session)
        if screen_source is not None
        else Screener(database, config).run(session, use_market_snapshots=False)
    )
    ranked = champion_ranking(raw, config)
    qualification = qualify_f_inputs(raw, session, config, ranked=ranked)
    blocking, warnings = [], []

    def check_state(key, *, sec=False):
        state = states.get(key)
        if not isinstance(state, dict):
            blocking.append(f"{key.upper()}_STATE_MISSING")
            return {"status": "missing"}
        status = state.get("status")
        # SEC can be partial ONLY because the canonical identity guard has safely
        # excluded conflicts; provider/parse errors remain blocking.
        resolved_partial = (
            sec
            and status == "partial"
            and state.get("identity_conflicts", 0) > 0
            and not state.get("errors", 0)
            and qualification["unresolved_rank_relevant_count"] == 0
        )
        if status != "success" and not resolved_partial:
            blocking.append(f"{key.upper()}_STATE_{str(status or 'missing').upper()}")
        if state.get("errors", 0):
            blocking.append(f"{key.upper()}_ERRORS_PRESENT")
        if resolved_partial:
            warnings.append("SEC_IDENTITY_CONFLICTS_DETERMINISTICALLY_EXCLUDED")
        try:
            success = datetime.fromisoformat(
                str(state["last_completed_at" if resolved_partial else "last_success_at"])
            )
            age = now - success
            if age < timedelta(0) or age > SNAPSHOT_MAX_AGE:
                blocking.append(f"{key.upper()}_SNAPSHOT_STALE")
            if sec and success < regular_session_bounds(session)[1]:
                blocking.append("SEC_REFRESH_BEFORE_ANALYSIS_CLOSE")
        except (KeyError, TypeError, ValueError):
            blocking.append(f"{key.upper()}_SUCCESS_TIME_INVALID")
        return state

    asset = check_state("asset_universe")
    sec = check_state("sec", sec=True)
    # Both existing workflows can refresh Daily inputs. A more recent failed or
    # running attempt cannot be hidden by an older successful workflow.
    daily_keys = [
        key for key in ("daily_history", "historical_bars") if isinstance(states.get(key), dict)
    ]
    daily_key = max(
        daily_keys,
        key=lambda key: str(
            states[key].get("last_started_at") or states[key].get("last_success_at") or ""
        ),
        default="daily_history",
    )
    daily = check_state(daily_key)
    requested_feed, requested_adjustment = (
        config.universe.market_data_feed,
        config.universe.market_data_adjustment,
    )
    if daily.get("feed") != requested_feed or daily.get("adjustment") != requested_adjustment:
        blocking.append("DAILY_FEED_ADJUSTMENT_UNVERIFIED")
    spy_bar = database.bars_on_session({"SPY"}, session).get("SPY")
    spy = {"status": current_session_observation(spy_bar, session)}
    if spy["status"] != "OBSERVATION_PRESENT":
        blocking.append("SPY_CURRENT_OBSERVATION_UNUSABLE")
    if not raw.records:
        blocking.append("SOURCE_UNIVERSE_EMPTY")
    if qualification["unresolved_rank_relevant_count"]:
        blocking.append("RANKING_UNRESOLVED")
    details = {
        "contract": READINESS_CONTRACT,
        "ready": not blocking,
        "latest_completed_session": latest,
        "analysis_session": session,
        "asset_state": asset,
        "daily_state": daily,
        "sec_state": sec,
        "spy_state": spy,
        "source_universe_count": qualification["source_universe_count"],
        "fully_qualified_count": qualification["fully_evaluated_count"]
        + qualification["deterministically_rejected_count"],
        "unresolved_rank_relevant_count": qualification["unresolved_rank_relevant_count"],
        "qualification_digest": digest(qualification),
        "feature_set_digest": digest(sorted(raw.records, key=lambda row: row.symbol)),
        "ranking_digest": digest([row for row in ranked.records if row.eligible]),
        "blocking_reasons": sorted(set(blocking)),
        "warnings": warnings,
    }
    details = normalized(details)
    details["input_readiness_digest"] = digest(details)
    return ShadowInputReadiness(details, raw, ranked, qualification)
