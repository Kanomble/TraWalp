"""Run-scoped Daily availability evidence, independent of security identity.

Only an explicit targeted audit may write evidence. Normal discovery reads the
database; it never reads research reports or calls a provider. A successful empty
probe establishes source unavailability, not listing dates or trading activity.
"""

from collections import Counter
from dataclasses import asdict
from datetime import date
from sqlite3 import Error as SQLiteError

from trading_system.backtest import research_definitions as definitions
from trading_system.data.security_reference import digest

POLICY = "PAIRS_RUN_GLOBAL_DAILY_QUALIFICATION_V1"
STORE = "pairs_daily_qualification"
QUARANTINED = "DAILY_DATA_QUARANTINED"
FAILED = "DAILY_QUALIFICATION_FAILED"
USABLE = "DAILY_HISTORY_USABLE"
REPAIRED = "DAILY_HISTORY_REPAIRED"
UNAVAILABLE = "CONFIGURED_PROVIDER_REQUIRED_HISTORY_UNAVAILABLE"
# Operational breadth guard, fixed before measuring strategy results. Ten percent
# requires explicit review well before exclusions reach dozens of percent.
MAX_QUARANTINE_FRACTION = 0.10
SAFE_ROLES = {
    "LIQUIDITY_SELECTION_INTERNAL_GAP",
    "SELECTED_ADV_WINDOW",
    "SELECTED_PREVIOUS_CLOSE",
    "PAIR_CALIBRATION_AND_OBSERVATION",
}


def scope(config, start, end):
    return {
        "policy": POLICY,
        "provider": "ALPACA",
        "feed": config.universe.market_data_feed.lower(),
        "adjustment": config.universe.market_data_adjustment,
        "start": str(start),
        "end": str(end),
        "definition": asdict(definitions.PAIRS_STAT_ARB_V1),
        "min_price": config.universe.min_price,
        "min_adv20": config.universe.min_avg_dollar_volume_20d,
    }


def diagnostic(symbol, status=USABLE, **values):
    return {
        "symbol": symbol,
        "daily_data_status": status,
        "status": status,
        "reason": UNAVAILABLE if status == QUARANTINED else status,
        "required_sessions": 0,
        "missing_sessions": 0,
        "provider_probe_status": "NOT_REQUIRED_OR_NOT_YET_AUDITED",
        "repair_attempted": False,
        "repair_decision": "NOT_REQUIRED",
        "run_excluded": status == QUARANTINED,
        "validation_blocking": status == FAILED,
        "record_kind": "SYMBOL_DAILY_QUALIFICATION",
        "input_scope": "RUN_DAILY_DATA_QUALIFICATION",
        "roles": "DAILY_DATA_QUALITY",
        **values,
    }


def breadth_guard(rows):
    excluded = sum(row["run_excluded"] for row in rows)
    fraction = excluded / len(rows) if rows else 0.0
    return {
        "policy_result": "REVIEW_REQUIRED" if fraction > MAX_QUARANTINE_FRACTION else "ACCEPTED",
        "quarantined_fraction": fraction,
        "review_threshold_fraction": MAX_QUARANTINE_FRACTION,
        "threshold_basis": "FIXED_OPERATIONAL_BREADTH_GUARD_NOT_PERFORMANCE_TUNED",
    }


def summarize(rows):
    counts = Counter(row["daily_data_status"] for row in rows)
    return {
        "eligible": counts[USABLE] + counts[REPAIRED],
        "quarantined": counts[QUARANTINED],
        "unresolved_blocking": counts[FAILED],
        "identity_qualified_universe": len(rows),
        **breadth_guard(rows),
    }


def qualify_daily(database, universe, config, start, end):
    """Exclude certified unavailable symbols before SIC2/ADV/ranking/calibration.

    The certificate records genuine requirements from an earlier frozen preflight,
    not inferred Cartesian history requirements. No correlation/outcome fields are
    persisted or inspected. New unaudited gaps remain ordinary blocking gaps.
    Restoring all certified sessions automatically clears an old exclusion.
    """
    from trading_system.backtest.pairs_stat_arb_v1_data import load_daily

    binding = scope(config, start, end)
    rows = []
    try:
        certificate = database.sync_values(STORE).get(digest(binding))
        if certificate is None:
            evidence = {}
        else:
            if certificate["scope"] != binding or certificate["fingerprint"] != digest(
                {k: v for k, v in certificate.items() if k != "fingerprint"}
            ):
                raise ValueError("DAILY_QUALIFICATION_CERTIFICATE_INVALID")
            evidence = certificate["symbols"]
        for identity in universe:
            symbol = identity["symbol"]
            proof = evidence.get(symbol)
            if proof is None:
                rows.append(diagnostic(symbol))
                continue
            if proof["identity_fingerprint"] != digest(identity):
                raise ValueError("DAILY_QUALIFICATION_IDENTITY_CHANGED_REAUDIT_REQUIRED")
            sessions = proof["sessions"]
            if not sessions or len({r["session"] for r in sessions}) != len(sessions):
                raise ValueError("DAILY_QUALIFICATION_SESSIONS_INVALID")
            days = [date.fromisoformat(r["session"]) for r in sessions]
            bars = load_daily(
                database, [symbol], min(days), max(days), identity_by_symbol={symbol: identity}
            )
            missing = [
                r for r in sessions if bars.get((symbol, date.fromisoformat(r["session"]))) is None
            ]
            failed = (
                not proof["audit_completed"]
                or not proof["local_recheck_completed"]
                or not proof["repair_plan_completed"]
                or any(
                    r["provider_result"] not in {"BAR_RETURNED", "EMPTY_RESPONSE_UNRESOLVED"}
                    for r in sessions
                )
            )
            if failed:
                status = FAILED
            elif not missing:
                status = REPAIRED
            elif all(r["provider_result"] == "EMPTY_RESPONSE_UNRESOLVED" for r in missing):
                status = QUARANTINED
            else:
                status = (
                    FAILED  # Positive availability without successful ingestion is not absence.
                )
            rows.append(
                diagnostic(
                    symbol,
                    status,
                    required_sessions=len(sessions),
                    missing_sessions=len(missing),
                    required_range_fingerprint=digest(sessions),
                    evidence_fingerprint=digest(proof),
                    provider_probe_status="FAILED" if failed else "COMPLETED_SUCCESSFULLY",
                    repair_attempted=proof["repair_attempted"],
                    repair_decision=proof["repair_decision"],
                )
            )
    except (SQLiteError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # Corrupt/read/system states cannot be relabeled as known unavailability.
        rows = [
            diagnostic(
                r["symbol"], FAILED, provider_probe_status="EVIDENCE_READ_OR_VALIDATION_FAILED"
            )
            for r in universe
        ]
    summary = summarize(rows)
    excluded = {r["symbol"] for r in rows if r["run_excluded"]}
    complete = not summary["unresolved_blocking"] and summary["policy_result"] == "ACCEPTED"
    fingerprint = digest({"scope": binding, "symbols": rows, "guard": summary})
    return (
        [r for r in universe if r["symbol"] not in excluded],
        rows,
        summary,
        complete,
        fingerprint,
    )


def report_daily(rows, required, missing):
    """Finalize transparent diagnostics after discovery without changing exclusions."""
    required_counts = Counter(
        symbol for (symbol, _), roles in required.items() if set(roles) & SAFE_ROLES
    )
    missing_counts = Counter(
        r["symbol"] for r in missing if set(r["roles"].split(";")) & SAFE_ROLES
    )
    result = []
    for row in rows:
        if row["run_excluded"]:
            result.append(row)
        elif missing_counts[row["symbol"]]:
            result.append(
                {
                    **row,
                    **diagnostic(
                        row["symbol"],
                        FAILED,
                        required_sessions=required_counts[row["symbol"]],
                        missing_sessions=missing_counts[row["symbol"]],
                        provider_probe_status="REQUIRED_HISTORY_NOT_ESTABLISHED",
                        repair_attempted=row["repair_attempted"],
                        repair_decision=row["repair_decision"],
                    ),
                }
            )
        else:
            result.append({**row, "required_sessions": required_counts[row["symbol"]]})
    return result, summarize(result)
