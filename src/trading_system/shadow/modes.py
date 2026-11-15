"""Explicit runtime admission and immutable observation evidence, never economic rules."""

from collections import Counter
from datetime import date, datetime, timedelta

from trading_system.backtest.data_integrity import (
    DataIntegrityError,
    validate_session_position_data,
)
from trading_system.data.forward_market import CONTRACT
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import DailyBar
from trading_system.shadow.contracts import (
    FULL,
    MANAGEMENT_WITH_BLOCKED_SIGNALS,
    SESSION_ADMISSION_CONTRACT,
    STRICT_CONTRACT_VERSION,
    ShadowError,
    digest,
    normalized,
)
from trading_system.shadow.readiness import READINESS_CONTRACT, SNAPSHOT_MAX_AGE
from trading_system.shadow.state import require

OBSERVATION_ONLY = "OBSERVATION_ONLY"
ECONOMIC = "ECONOMIC"
MODES = {OBSERVATION_ONLY, ECONOMIC}


def validate_mode(mode):
    if mode not in MODES:
        raise ShadowError("SHADOW_INVALID_MODE", mode=mode)
    return mode


def evidence_manifest(readiness, input_manifest_hash):
    return {
        "contract": "shadow-observation-evidence/v1",
        "session": readiness.details["analysis_session"],
        "input_manifest_hash": input_manifest_hash,
        "readiness": readiness.as_dict(),
        "qualification": readiness.qualification,
    }


def observation_payload(evidence, content_hash):
    qa = evidence["qualification"]
    count = qa["unresolved_rank_relevant_count"]
    certificate = {
        "session": evidence["session"],
        "qualification_digest": digest(qa),
        "rank_blocking_unresolved_count": count,
        "ranking_complete": count == 0,
        "economic_action_allowed": False,
        "contract": "shadow-observation-certificate/v1",
    }
    return {
        "session": evidence["session"],
        "market_data_manifest_hash": evidence["input_manifest_hash"],
        "observation_manifest_hash": content_hash,
        "qualification_summary": {k: v for k, v in qa.items() if k.endswith("count")},
        "unresolved_count": count,
        "unresolved_reason_hash": digest(qa["incomplete_inputs"]),
        "ranking_complete": count == 0,
        "economic_action_allowed": False,
        "ranking_published": False,
        "certificate_hash": digest(certificate),
    }


def blocked_payload(evidence, content_hash):
    payload = observation_payload(evidence, content_hash)
    return {
        "rank_blocking_unresolved_count": payload["unresolved_count"],
        "observation_manifest_hash": content_hash,
        "certificate_hash": payload["certificate_hash"],
        "reason_summary": dict(
            sorted(
                Counter(
                    reason
                    for item in evidence["qualification"]["incomplete_inputs"]
                    for reason in item.get("reasons", [])
                ).items()
            )
        ),
    }


def validate_evidence(evidence, session, load_manifest, *, economic=False):
    require(
        evidence["contract"] == "shadow-observation-evidence/v1", "observation evidence contract"
    )
    require(evidence["session"] == session, "observation evidence session")
    ready, qualification = evidence["readiness"], evidence["qualification"]
    require(ready["analysis_session"] == session, "observation analysis session")
    require(
        ready["observation_shadow_ready"] is True and not ready["observation_blocking_reasons"],
        "observation infrastructure readiness",
    )
    require(
        ready["observation_blocking_reasons"]
        == [r for r in ready["blocking_reasons"] if r != "RANKING_UNRESOLVED"],
        "observation blocker separation",
    )
    require(
        ready["input_readiness_digest"]
        == digest({k: v for k, v in ready.items() if k != "input_readiness_digest"}),
        "observation readiness digest",
    )
    require(
        ready["qualification_digest"] == digest(qualification), "observation qualification digest"
    )
    count = qualification["unresolved_rank_relevant_count"]
    require(
        all(type(v) is int and v >= 0 for k, v in qualification.items() if k.endswith("count")),
        "observation count types",
    )
    require(count == len(qualification["incomplete_inputs"]), "observation unresolved census")
    require(ready["unresolved_rank_relevant_count"] == count, "observation unresolved count")
    require(
        qualification["source_universe_count"] > 0
        and qualification["source_universe_count"]
        == qualification["deterministically_rejected_count"]
        + qualification["fully_evaluated_count"]
        + count,
        "observation source census",
    )
    require(
        all(item["symbol"] is not None for item in qualification["incomplete_inputs"]),
        "observation structural input failure",
    )
    require(
        len({item["symbol"] for item in qualification["incomplete_inputs"]}) == count,
        "observation unique unresolved census",
    )
    require(
        ("RANKING_UNRESOLVED" in ready["blocking_reasons"]) == bool(count),
        "observation ranking blockers",
    )
    require(ready["ranking_complete"] == (count == 0), "observation ranking completeness")
    require(
        ready["economic_shadow_ready"] == ready["ready"] == (not ready["blocking_reasons"]),
        "economic readiness separation",
    )
    require(
        ready["input_provenance_hash"] == evidence["input_manifest_hash"],
        "observation input reference",
    )
    inputs = load_manifest(evidence["input_manifest_hash"])
    require(all(inputs.get(k) == v for k, v in CONTRACT.items()), "observation SIP contract")
    if economic:
        require(ready["economic_shadow_ready"] and count == 0, "activation economic readiness")
    return qualification


def session_admission_manifest(evidence, *, purpose, state, fingerprints_digest, bars):
    """Prepare evidence only; neither selects admission nor changes runner policy."""
    return normalized(
        {
            "contract": SESSION_ADMISSION_CONTRACT,
            "purpose": purpose,
            "evidence": evidence,
            "state_digest": digest(state),
            "fingerprints_digest": fingerprints_digest,
            "critical_bars_digest": digest(bars),
            "strict_contract_version": STRICT_CONTRACT_VERSION,
        }
    )


def validate_session_admission(admission, session, load_manifest, *, state, fingerprint, now):
    """New evidence is ECONOMIC-session-only; old activation evidence stays strict."""
    require(admission["contract"] == SESSION_ADMISSION_CONTRACT, "session admission contract")
    purpose = admission["purpose"]
    require(purpose in {FULL, MANAGEMENT_WITH_BLOCKED_SIGNALS}, "session purpose")
    require(admission["state_digest"] == digest(state), "admission state")
    require(admission["fingerprints_digest"] == fingerprint, "admission fingerprint")
    require(
        admission["strict_contract_version"] == STRICT_CONTRACT_VERSION, "admission strict contract"
    )
    evidence = admission["evidence"]
    validate_evidence(evidence, session, load_manifest, economic=purpose == FULL)
    ready = evidence["readiness"]
    require(ready["contract"] == READINESS_CONTRACT, "admission readiness contract")
    require(all(ready.get(k) == v for k, v in CONTRACT.items()), "admission readiness SIP contract")
    require(ready["forward_sip_transition_boundary"] == session, "admission SIP boundary")
    if purpose == MANAGEMENT_WITH_BLOCKED_SIGNALS:
        require(ready["blocking_reasons"] == ["RANKING_UNRESOLVED"], "limited admission blockers")
        require(
            ready["ready"] is False and ready["economic_shadow_ready"] is False,
            "limited economic readiness remains false",
        )
        require(
            evidence["qualification"]["unresolved_rank_relevant_count"] > 0,
            "limited admission needs unresolved ranking",
        )
    # Independently check recorded upstream state, even after hostile rehashing of
    # readiness booleans/digests. Mirrors the existing readiness/v1 freshness policy.
    for name in ("asset_state", "daily_state", "sec_state"):
        value = ready[name]
        partial = (
            name == "sec_state"
            and value.get("status") == "partial"
            and value.get("identity_conflicts", 0) > 0
            and not value.get("errors", 0)
        )
        require(value.get("status") == "success" or partial, "admission upstream status")
        require(
            not value.get("errors", 0) and not value.get("database_failures", 0),
            "admission upstream errors",
        )
        completed = datetime.fromisoformat(
            value["last_completed_at" if partial else "last_success_at"]
        )
        require(timedelta(0) <= now - completed <= SNAPSHOT_MAX_AGE, "admission upstream freshness")
        if name == "sec_state":
            require(
                completed >= regular_session_bounds(date.fromisoformat(session))[1],
                "admission SEC before close",
            )
    require(ready["spy_state"]["status"] == "OBSERVATION_PRESENT", "admission SPY readiness")
    return purpose


def validate_admission_preflight(admission, payload, state, session, anchor, now, load_manifest):
    """Replay CC-01 with the pre-session state and attest bars from SIP provenance."""
    require(
        digest(payload["observed_bars"]) == admission["critical_bars_digest"],
        "admission critical bars binding",
    )
    bars = {s: DailyBar.model_validate(b) for s, b in payload["observed_bars"].items()}
    position, pending = state["position"], state["pending"]
    held = {position["symbol"]: date.fromisoformat(position["entry_date"])} if position else {}
    intents = (
        {pending["order"]["record"]["symbol"]: date.fromisoformat(pending["order"]["signal_date"])}
        if pending
        else {}
    )
    try:
        validate_session_position_data(
            date.fromisoformat(session),
            bars=bars,
            held_positions=held,
            pending_entries=intents,
            previous_processed_session=anchor,
            now=now,
        )
    except DataIntegrityError as exc:
        require(False, "admission strict preflight: " + exc.code)
    inputs = load_manifest(admission["evidence"]["input_manifest_hash"])
    observations = [row for rows in inputs["market_inputs"].values() for row in rows]
    for symbol in set(held) | set(intents) | {"SPY"}:
        matches = [r for r in observations if r["symbol"] == symbol and r["session"] == session]
        require(bool(matches), "admission critical SIP source missing")
        for source in matches:
            require(
                all(
                    source.get(k) == CONTRACT[k]
                    for k in ("provider", "feed", "adjustment", "timeframe")
                ),
                "admission critical SIP contract",
            )
            require(
                bool(source["sync_reference"])
                and datetime.fromisoformat(source["retrieved_at"]).utcoffset() is not None,
                "admission critical SIP provenance",
            )
            bar = DailyBar.model_validate(source)
            if symbol in held or symbol in intents:
                require(
                    normalized(bar) == normalized(bars[symbol]),
                    "admission critical SIP bar mismatch",
                )
            if symbol == "SPY":
                try:
                    validate_session_position_data(
                        date.fromisoformat(session),
                        bars={symbol: bar},
                        held_positions={symbol: date.fromisoformat(session)},
                        pending_entries={},
                        previous_processed_session=anchor,
                        now=now,
                    )
                except DataIntegrityError as exc:
                    require(False, "admission SPY preflight: " + exc.code)
