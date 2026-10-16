"""Bounded orchestration of existing discovery and Daily audit/repair contracts."""

import hashlib
from sqlite3 import Error as SQLiteError

from trading_system.backtest.pairs_stat_arb_v1_daily_qualification import SAFE_ROLES
from trading_system.data.security_reference import digest

POLICY = "PAIRS_DAILY_QUALIFICATION_FIXPOINT_V1"
MAX_DAILY_QUALIFICATION_ROUNDS = 8
SUCCESS = "SUCCESSFUL_FIXPOINT"
NO_PROGRESS = "DAILY_QUALIFICATION_NO_PROGRESS"
CYCLE = "DAILY_QUALIFICATION_CYCLE_DETECTED"
MAX_ROUNDS = "DAILY_QUALIFICATION_FIXPOINT_NOT_REACHED"
FAILED = "DAILY_QUALIFICATION_FAILED"
REVIEW = "REVIEW_REQUIRED"


def requirement_source(manifest):
    return digest(
        {
            "provider": "ALPACA",
            "feed": manifest["market_data_feed"],
            "adjustment": manifest["market_data_adjustment"],
            "policy": manifest["daily_qualification_policy"],
        }
    )


def requirement_keys(prepared, *, missing_only=False):
    """Stable (symbol, official session, role, source) inputs, never outcomes."""
    source = requirement_source(prepared.manifest)
    days = [str(day) for day in prepared.sessions]
    indexes = {day: i for i, day in enumerate(days)}
    keys = set()
    for row in prepared.requirements:
        if row["status"] not in {"LOCAL_MISSING", "REQUIRED_PRESENT"}:
            continue
        if missing_only and row["status"] != "LOCAL_MISSING":
            continue
        roles = set(row["roles"].split(";")) & SAFE_ROLES
        if not roles:
            continue
        for day in days[
            indexes[row["session"]] : indexes[row.get("session_end", row["session"])] + 1
        ]:
            keys.update((row["symbol"], day, role, source) for role in roles)
    return keys


def state_inputs(prepared):
    manifest = prepared.manifest
    counts = manifest["discovery_counts"]
    required = requirement_keys(prepared)
    unresolved = requirement_keys(prepared, missing_only=True)
    hasher = hashlib.sha256()
    for key in sorted(required):
        hasher.update((digest(key) + "\n").encode())
    blocked = {
        r["symbol"]
        for r in counts["daily_data_qualification_diagnostics"]
        if r["validation_blocking"]
    }
    return {
        "eligible_symbols": sorted(
            r["symbol"] for r in manifest["universe"] if r["symbol"] not in blocked
        ),
        "provisional_universe_symbols": sorted(r["symbol"] for r in manifest["universe"]),
        "identity_quarantined_symbols": sorted(
            r["symbol"] for r in counts["identity_qualification_diagnostics"] if r["run_excluded"]
        ),
        "daily_quarantined_symbols": sorted(
            r["symbol"] for r in counts["daily_data_qualification_diagnostics"] if r["run_excluded"]
        ),
        "unresolved_requirements": sorted(unresolved),
        "required_daily_inputs": len(required),
        "required_daily_inputs_fingerprint": hasher.hexdigest(),
        "feed_identity": {
            "provider": "ALPACA",
            "feed": manifest["market_data_feed"],
            "adjustment": manifest["market_data_adjustment"],
        },
        "identity_fingerprint": manifest["run_identity_qualification_fingerprint"],
        "daily_qualification_fingerprint": manifest["run_daily_qualification_fingerprint"],
        "source_fingerprint": manifest["source_fingerprint"],
    }


def failure_count(prepared):
    # Newly discovered unreviewed history is distinct from a failed check.
    return sum(
        r["validation_blocking"]
        and r["provider_probe_status"] != "REQUIRED_HISTORY_NOT_ESTABLISHED"
        for r in prepared.manifest["discovery_counts"]["daily_data_qualification_diagnostics"]
    )


def finish(prepared, rounds, reason):
    final_state = state_inputs(prepared)
    result = {
        "policy": POLICY,
        "max_rounds": MAX_DAILY_QUALIFICATION_ROUNDS,
        "daily_qualification_rounds": len(rounds),
        "fixpoint_reached": reason == SUCCESS,
        "fixpoint_reason": reason,
        "rounds": rounds,
        "final_state_inputs": final_state,
        "final_state_fingerprint": digest(final_state),
        "final_daily_qualification_fingerprint": prepared.manifest[
            "run_daily_qualification_fingerprint"
        ],
        "final_source_fingerprint": prepared.manifest["source_fingerprint"],
        "qualification_failures": max(
            rounds[-1]["qualification_failures"], failure_count(prepared)
        ),
        "new_unreviewed_daily_requirements": len(final_state["unresolved_requirements"]),
    }
    result["fingerprint"] = digest(result)
    return prepared, result


def iterate(discover, audit=None, *, on_round=None):
    """Full rediscovery after every audit/repair change; never evaluate outcomes.

    The callbacks are the existing local preparation and targeted auditor. Audits
    receive only new unresolved requirement-role keys. Errors stop immediately.
    Two identical consecutive states mean no progress; an older repeated state is
    a cycle. A success requires an entire discovery with no unresolved inputs.
    """
    rounds, seen_states, seen_universes, reviewed = [], {}, {}, set()
    for number in range(1, MAX_DAILY_QUALIFICATION_ROUNDS + 1):
        prepared = discover()
        state = state_inputs(prepared)
        fp = digest(state)
        unresolved = {tuple(key) for key in state["unresolved_requirements"]}
        new = unresolved - reviewed
        counts = prepared.manifest["discovery_counts"]
        row = {
            "round": number,
            "eligible_symbols": counts["daily_data_qualification"]["eligible"],
            "daily_quarantined_symbols": counts["daily_data_qualification"]["quarantined"],
            "new_requirements": len({(s, day) for s, day, _, _ in new}),
            "new_requirement_roles": len(new),
            "unreviewed_requirements": len(unresolved),
            "audited_requirements": 0,
            "repairs": 0,
            "new_quarantines": 0,
            "qualification_failures": failure_count(prepared),
            "pair_session_candidates": len(prepared.manifest["candidates"]),
            "state_fingerprint": fp,
            "state_inputs": state,
        }
        rounds.append(row)
        if on_round:
            on_round(row)
        if counts["daily_data_qualification"]["policy_result"] == REVIEW:
            return finish(prepared, rounds, REVIEW)
        if row["qualification_failures"] or not counts["daily_data_gate_complete"]:
            return finish(prepared, rounds, FAILED)
        universe_fp = digest(
            {
                key: state[key]
                for key in (
                    "eligible_symbols",
                    "provisional_universe_symbols",
                    "identity_quarantined_symbols",
                    "daily_quarantined_symbols",
                    "feed_identity",
                )
            }
        )
        last_universe = seen_universes.get(universe_fp)
        if last_universe is not None and last_universe != number - 1:
            return finish(prepared, rounds, CYCLE)
        seen_universes[universe_fp] = number
        if not unresolved:
            return finish(prepared, rounds, SUCCESS)
        if fp in seen_states:
            reason = NO_PROGRESS if seen_states[fp] == number - 1 else CYCLE
            return finish(prepared, rounds, reason)
        seen_states[fp] = number
        if number == MAX_DAILY_QUALIFICATION_ROUNDS:
            return finish(prepared, rounds, MAX_ROUNDS)
        if not new:
            return finish(prepared, rounds, NO_PROGRESS)
        if audit is None:
            return finish(prepared, rounds, FAILED)
        try:
            result = audit(prepared, new)
        except (
            SQLiteError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as exc:
            row["qualification_failures"] += 1
            row["error_type"] = type(exc).__name__
            return finish(prepared, rounds, FAILED)
        reviewed.update(new)
        row["audited_requirements"] = result["audited_requirements"]
        row["repairs"] = result["repairs"]
        row["new_quarantines"] = result["new_quarantines"]
        row["qualification_failures"] += result["qualification_failures"]
        row["audit_fingerprint"] = result["fingerprint"]
        if result.get("policy_result") == REVIEW:
            return finish(result.get("blocked_prepared", prepared), rounds, REVIEW)
        if row["qualification_failures"]:
            return finish(result.get("blocked_prepared", prepared), rounds, FAILED)
    raise AssertionError("bounded loop must return")


def attach(prepared, result):
    from trading_system.backtest.liquid_universe import fingerprint

    prepared.manifest["daily_qualification_iteration"] = result
    prepared.manifest["run_daily_fixpoint_fingerprint"] = result["fingerprint"]
    prepared.manifest["fingerprint"] = fingerprint(prepared.manifest)
    prepared.metadata.update(
        {
            "daily_qualification_rounds": result["daily_qualification_rounds"],
            "fixpoint_reached": result["fixpoint_reached"],
            "fixpoint_reason": result["fixpoint_reason"],
            "daily_qualification_iteration": result,
            "daily_qualification_failures": result["qualification_failures"],
            "new_unreviewed_daily_requirements": result["new_unreviewed_daily_requirements"],
        }
    )


def verify_trace(saved, prepared):
    """Validate the immutable trace and its final state against current local inputs."""
    try:
        _verify_trace(saved, prepared)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("PAIRS_STAT_ARB_MANIFEST_MISMATCH: corrupt Daily fixpoint trace") from exc


def _verify_trace(saved, prepared):
    result = saved["daily_qualification_iteration"]
    if (
        result["policy"] != POLICY
        or result["max_rounds"] != MAX_DAILY_QUALIFICATION_ROUNDS
        or result["fingerprint"] != digest({k: v for k, v in result.items() if k != "fingerprint"})
        or saved["run_daily_fixpoint_fingerprint"] != result["fingerprint"]
        or not 1 <= len(result["rounds"]) <= MAX_DAILY_QUALIFICATION_ROUNDS
        or any(r["state_fingerprint"] != digest(r["state_inputs"]) for r in result["rounds"])
        or result["final_state_fingerprint"] != digest(state_inputs(prepared))
        or result["final_state_fingerprint"] != digest(result["final_state_inputs"])
        or result["daily_qualification_rounds"] != len(result["rounds"])
        or result["rounds"][-1]["state_fingerprint"] != result["final_state_fingerprint"]
        or [r["round"] for r in result["rounds"]] != list(range(1, len(result["rounds"]) + 1))
        or result["qualification_failures"] != 0
        or result["new_unreviewed_daily_requirements"] != 0
        or result["final_daily_qualification_fingerprint"]
        != prepared.manifest["run_daily_qualification_fingerprint"]
        or result["final_source_fingerprint"] != prepared.manifest["source_fingerprint"]
        or not result["fixpoint_reached"]
        or result["fixpoint_reason"] != SUCCESS
    ):
        raise ValueError("PAIRS_STAT_ARB_MANIFEST_MISMATCH: Daily fixpoint trace changed")


def prepare_fixpoint(
    database, config, start, end, directory, stem, *, auditor_factory=None, progress=None
):
    """Wire the existing preparation/auditor together; provider creation is lazy."""
    import json
    from collections import Counter
    from pathlib import Path

    from trading_system.backtest.pairs_stat_arb_v1_daily_qualification import (
        qualify_daily,
        summarize,
    )
    from trading_system.backtest.pairs_stat_arb_v1_daily_qualification_audit import audit_and_repair
    from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
    from trading_system.backtest.report import _atomic_text

    synchronizer = None
    network_used = False
    directory = Path(directory)

    def discover():
        if progress:
            progress("Daily qualification: discovering selection and calibration inputs locally")
        return prepare_pairs_data(
            database, config, start, end, preflight=True, qualification_only=True
        )

    def audit(prepared, new_keys):
        nonlocal synchronizer, network_used
        if synchronizer is None:
            synchronizer = auditor_factory()
        number = len(audits) + 1
        directory.mkdir(parents=True, exist_ok=True)
        prefix = directory / f"{stem}_round_{number}"
        if progress:
            count = len({(s, d) for s, d, _, _ in new_keys})
            progress(f"Daily qualification round {number}: auditing {count} new required sessions")

        def save_plan(plan):
            _atomic_text(Path(f"{prefix}_repair_plan.json"), json.dumps(plan, indent=2))

        report = audit_and_repair(
            database,
            prepared.manifest,
            {
                "manifest_fingerprint": prepared.manifest["fingerprint"],
                "required_sessions": [
                    r for r in prepared.requirements if r["status"] == "LOCAL_MISSING"
                ],
            },
            config,
            synchronizer.alpaca,
            lambda symbol, first, last: synchronizer.sync_daily_history(
                [symbol], first, last, incremental=False, include_benchmark=False
            ),
            save_plan=save_plan,
            only_requirement_keys=new_keys,
        )
        _, updated, _, _, _ = qualify_daily(
            database, prepared.manifest["universe"], config, start, end
        )
        previous = prepared.manifest["discovery_counts"]["daily_data_qualification_diagnostics"]
        updated.extend(r for r in previous if r["run_excluded"])
        summary = summarize(updated)
        old_quarantines = {r["symbol"] for r in previous if r["run_excluded"]}
        new_quarantines = {r["symbol"] for r in updated if r["run_excluded"]} - old_quarantines
        network_used |= any(not r.get("reused", False) for r in report["provider_observations"])
        result = {
            "audited_requirements": len(report["provider_observations"]),
            "provider_probed_requirements": sum(
                not r.get("reused", False) for r in report["provider_observations"]
            ),
            "repairs": len(report["repairs"]),
            "new_quarantines": len(new_quarantines),
            "qualification_failures": sum(r["validation_blocking"] for r in updated),
            "policy_result": summary["policy_result"],
            "certificate_fingerprint": report["certificate_fingerprint"],
            "provider_outcomes": dict(
                Counter(r["provider_outcome"] for r in report["provider_observations"])
            ),
        }
        result["fingerprint"] = digest(result)
        _atomic_text(
            Path(f"{prefix}_audit.json"),
            json.dumps(
                {**report, "qualification_after_audit": summary, "iteration_result": result},
                indent=2,
            ),
        )
        audits.append(result)
        if summary["policy_result"] == REVIEW or result["qualification_failures"]:
            # Refresh the report against persisted evidence, with the existing
            # gate stopping before ranking/calibration. No further audit round.
            result["blocked_prepared"] = discover()
        return result

    audits = []
    prepared, result = iterate(discover, audit if auditor_factory else None)
    if result["fixpoint_reached"]:
        try:
            completed = prepare_pairs_data(
                database, config, start, end, preflight=True, frozen_discovery=prepared.manifest
            )
            if digest(state_inputs(completed)) != result["final_state_fingerprint"]:
                raise ValueError("DAILY_FIXPOINT_INPUTS_CHANGED_DURING_FINALIZATION")
            prepared = completed
        except (SQLiteError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result = {
                **result,
                "fixpoint_reached": False,
                "fixpoint_reason": FAILED,
                "qualification_failures": 1,
                "finalization_error_type": type(exc).__name__,
            }
            result["fingerprint"] = digest({k: v for k, v in result.items() if k != "fingerprint"})
    attach(prepared, result)
    prepared.metadata["network_used"] = network_used
    return prepared
