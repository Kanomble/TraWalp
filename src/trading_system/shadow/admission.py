"""Read-only, point-in-time inception admission shared by preflight and init."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from trading_system.data.market_sessions import latest_completed_trading_session
from trading_system.shadow.contracts import ShadowError, digest, fingerprints, normalized
from trading_system.shadow.inputs import LocalInputs, separate_paths
from trading_system.shadow.modes import ECONOMIC, OBSERVATION_ONLY, validate_mode
from trading_system.shadow.readiness import ShadowInputReadiness, evaluate_shadow_input_readiness


@dataclass(frozen=True)
class InceptionAdmission:
    details: dict
    readiness: ShadowInputReadiness | None
    fingerprints: dict
    input_manifest: dict

    @property
    def selected_mode_ready(self):
        return self.details["selected_mode_ready"]

    def as_dict(self):
        return normalized(self.details)


def evaluate_inception_admission(
    market_path,
    config,
    *,
    journal_path,
    now=None,
    requested_mode=ECONOMIC,
    capital=100.0,
    screen_source=None,
):
    """No journal construction, writes, sync or reservation of a later inception.

    The boundary guard precedes screening, as in init. Failed boundary checks do
    not attest ranking completeness. Init must still recheck the session clock
    immediately before creating its journal.
    """
    validate_mode(requested_mode)
    separate_paths(journal_path, market_path)
    if journal_path.exists():
        raise ShadowError("SHADOW_ALREADY_INITIALIZED", path=str(journal_path))
    if not math.isfinite(capital) or capital <= 0:
        raise ShadowError("SHADOW_INVALID_INITIAL_CAPITAL")
    current = fingerprints(config)
    now = now or datetime.now(UTC)
    latest = latest_completed_trading_session(now)
    details = {
        "contract": "shadow-inception-admission/v1",
        "requested_mode": requested_mode,
        "selected_mode_ready": False,
        "economic_shadow_ready": False,
        "observation_shadow_ready": False,
        "ranking_complete": False,
        "ranking_evaluated": False,
        "transition_boundary_current": False,
        "analysis_session": latest,
        "checked_at": now,
        "point_in_time_only": True,
    }
    readiness, manifest = None, {}
    if not market_path.is_file():
        blockers = ["MARKET_DATABASE_MISSING"]
    else:
        with LocalInputs(market_path).snapshot() as inputs:
            state = inputs.sync_values("dataset").get("forward_sip")
            declared = state.get("end") if isinstance(state, dict) else None
            details["declared_transition_boundary"] = declared
            if not declared:
                blockers = ["SIP_TRANSITION_BOUNDARY_NOT_DECLARED"]
            else:
                try:
                    transition = date.fromisoformat(declared)
                except (TypeError, ValueError):
                    blockers = ["SIP_TRANSITION_BOUNDARY_INVALID"]
                else:
                    blockers = (
                        [] if transition == latest else ["SIP_TRANSITION_BOUNDARY_NOT_CURRENT"]
                    )
            if not blockers:
                readiness = evaluate_shadow_input_readiness(
                    inputs,
                    config,
                    now=now,
                    session=latest,
                    screen_source=screen_source,
                )
                details = {
                    **readiness.as_dict(),
                    **details,
                    "transition_boundary_current": True,
                    "ranking_evaluated": True,
                    "ranking_complete": readiness.details["ranking_complete"],
                    "economic_shadow_ready": readiness.ready,
                    "observation_shadow_ready": readiness.observation_ready,
                    "input_readiness_contract": readiness.details["contract"],
                    "economic_blocking_reasons": readiness.details["blocking_reasons"],
                }
                blockers = readiness.details[
                    "observation_blocking_reasons"
                    if requested_mode == OBSERVATION_ONLY
                    else "blocking_reasons"
                ]
                details["qualification"] = readiness.qualification
            manifest = inputs.provenance
    details["blocking_reasons"] = blockers
    details["selected_mode_ready"] = not blockers
    if readiness is None:
        details["input_readiness_digest"] = digest(
            {
                **details,
                "input_provenance": manifest,
                "fingerprints": current,
            }
        )
    return InceptionAdmission(normalized(details), readiness, current, manifest)
