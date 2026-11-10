"""Explicit runtime admission and immutable observation evidence, never economic rules."""

from collections import Counter

from trading_system.data.forward_market import CONTRACT
from trading_system.shadow.contracts import ShadowError, digest
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
