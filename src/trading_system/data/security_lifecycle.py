"""Corporate-action evidence ingestion; promotion requires separately reviewed intervals."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from trading_system.data.alpaca_corporate_actions import lifecycle_scope
from trading_system.data.security_identity import SymbolIdentityEpoch, current_identity_epoch
from trading_system.data.security_reference import (
    SOURCE,
    SOURCE_VERSION,
    cached_sec_ticker_ciks,
    digest,
    pairs_security_qualification,
)

CONFLICT = "IDENTITY_EVIDENCE_CONFLICT"


def normalize_name_changes(payload, symbols, start, end, *, observed_at=None):
    """Keep provider fields verbatim; process_date never becomes effective_date."""
    observed_at = observed_at or datetime.now(UTC).isoformat()
    reference = f"GET /v1/corporate-actions;symbols={','.join(symbols)};start={start};end={end}"
    records = []
    for kind, rows in payload.items():
        for raw in rows:
            action_id = raw.get("id")
            natural = {
                k: raw.get(k)
                for k in ("old_symbol", "new_symbol", "old_cusip", "new_cusip", "process_date")
            }
            records.append(
                {
                    "provider": "ALPACA",
                    "event_key": f"id:{action_id}"
                    if action_id
                    else f"natural:{digest([kind, natural])}",
                    "provider_action_id": action_id,
                    "action_type": kind,
                    **natural,
                    "effective_date": raw.get("effective_date"),
                    "observed_at": observed_at,
                    "request_reference": reference,
                    "raw_fingerprint": digest([kind, raw]),
                    "raw_payload": json.dumps(raw, sort_keys=True, allow_nan=False),
                }
            )
    return records


def load_lifecycle_verifications(path):
    """Explicitly supplied, externally reviewed complete interval evidence, not provider JSON."""
    if path is None:
        return []
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError("LIFECYCLE_VERIFICATION_FILE_TOO_LARGE")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(rows, list)
            or len(rows) > 1000
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError
        return rows
    except (ValueError, UnicodeError):
        raise ValueError("LIFECYCLE_VERIFICATION_FILE_INVALID") from None


def resolve_verified_symbol_identity_epochs(action, identity, verification=None):
    """A NameChange is an edge, never complete lifecycle or issuer/class evidence.

    The optional supplement supplies two externally reviewed SymbolIdentityEpochs
    and their CUSIP association. It is the temporal/security evidence source; this
    verifier checks consistency and binding, not external document authenticity.
    """

    def unresolved(reason, status="UNVERIFIED_EVIDENCE"):
        return {"epochs": [], "status": status, "reason": reason}

    if action["provider"] != "ALPACA" or action["action_type"] != "name_changes":
        return unresolved("UNSUPPORTED_ACTION_TYPE")
    if verification is None:
        return unresolved("NAME_CHANGE_REQUIRES_SECURITY_CLASS_ISSUER_AND_INTERVAL_PROOF")
    try:
        UUID(action["provider_action_id"])
        if verification["provider"] != "ALPACA" or (
            verification["provider_action_id"] != action["provider_action_id"]
        ):
            return unresolved(CONFLICT, CONFLICT)
        before, after = [SymbolIdentityEpoch.from_record(row) for row in verification["epochs"]]
        if not all(
            e.verified and not e.source.upper().startswith("ALPACA") for e in (before, after)
        ):
            return unresolved("INDEPENDENT_VERIFIED_COMPLETE_INTERVALS_REQUIRED")
        if any(not action[k] or not verification.get(k) for k in ("old_cusip", "new_cusip")):
            return unresolved("CUSIP_ASSOCIATION_UNPROVEN")
        if any(verification[k] != action[k] for k in ("old_cusip", "new_cusip")):
            return unresolved(CONFLICT, CONFLICT)
        if before.symbol != action["old_symbol"] or after.symbol != action["new_symbol"]:
            return unresolved(CONFLICT, CONFLICT)
        if (
            not before.cik
            or not after.cik
            or not all(e.stable_security_identity for e in (before, after))
        ):
            return unresolved("SECURITY_CLASS_AND_ISSUER_CONTINUITY_UNPROVEN")
        if (
            before.cik != after.cik
            or before.stable_security_identity != after.stable_security_identity
        ):
            return unresolved(CONFLICT, CONFLICT)
        if before.symbol == after.symbol:
            return unresolved("NAME_CHANGE_IS_NOT_A_SYMBOL_CHANGE")
        if before.effective_end != after.effective_start:
            return unresolved("EXACT_SYMBOL_CHANGE_BOUNDARY_REQUIRED")
        if identity and identity["qualification_status"] == "EXCLUDED_IDENTITY_CONFLICT":
            return unresolved(CONFLICT, CONFLICT)
        if identity is None or identity["qualification_status"] != "ELIGIBLE_COMMON_STOCK":
            return unresolved("CURRENT_COMMON_STOCK_BINDING_UNAVAILABLE")
        if not any(
            identity.get(k)
            for k in ("share_class_figi", "composite_figi", "stable_security_identity")
        ):
            return unresolved("CURRENT_SECURITY_IDENTIFIER_UNAVAILABLE")
        if current_identity_epoch(identity, [after.record()]) is None:
            return unresolved(CONFLICT, CONFLICT)
        if not after.contains(datetime.fromisoformat(identity["observed_at"]).date()):
            return unresolved(CONFLICT, CONFLICT)
        # The effective boundary comes ONLY from the reviewed source, not process_date.
        return {
            "epochs": [
                replace(
                    e,
                    source=f"ALPACA_CORPORATE_ACTIONS+{e.source}",
                    evidence_reference=f"ALPACA:{action['provider_action_id']};{e.evidence_reference}",
                    observed_at=action["observed_at"],
                )
                for e in (before, after)
            ],
            "status": "VERIFIED_SYMBOL_CHANGE",
            "reason": None,
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return unresolved("LIFECYCLE_VERIFICATION_INCOMPLETE_OR_INVALID")


def local_lifecycle_identities(database, symbols):
    """Current references can corroborate a binding, never provide historical boundaries."""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    current_sec = cached_sec_ticker_ciks(database)
    with database.read_only() as c:
        rows = c.execute(
            "SELECT c.symbol,c.cik,r.cik AS reference_cik,r.security_type,"
            "r.source,r.source_version,"
            "r.share_class_figi,r.composite_figi,r.observed_at,r.market,r.locale,r.active "
            "FROM companies c LEFT JOIN security_reference r ON r.symbol=c.symbol "
            "AND r.snapshot_id=(SELECT snapshot_id FROM security_reference_active "
            "WHERE singleton=1) "
            f"WHERE c.symbol IN ({','.join('?' for _ in symbols)})",
            symbols,
        )
        result = {}
        for row in rows:
            row = dict(row)
            qualification = pairs_security_qualification(
                reference_present=row["source"] is not None,
                resolved=(
                    row["source"] == SOURCE
                    and row["source_version"] == SOURCE_VERSION
                    and row["market"] == "stocks"
                    and row["locale"] == "us"
                    and row["active"] == 1
                ),
                security_type=row["security_type"],
                local_cik=row["cik"],
                current_sec_cik=current_sec.get(row["symbol"]),
                massive_cik=row["reference_cik"],
            )
            row["qualification_status"] = qualification["status"]
            result[row["symbol"]] = row
        return result


def sync_security_lifecycle(database, client, symbols, start, end, *, limit=100, verifications=()):
    symbols = lifecycle_scope(symbols, start, end, limit)
    report = {
        "symbols_requested": len(symbols),
        "corporate_actions_received": 0,
        "symbol_changes_received": 0,
        "raw_evidence_inserted": 0,
        "verified_epochs_created": 0,
        "unverified_evidence": 0,
        "identity_conflicts": 0,
        "symbols_resolved": [],
        "symbols_unresolved": symbols,
        "errors": 0,
        "unresolved_sample": [],
        "error_sample": [],
        "provider_query_status": "NOT_COMPLETED",
        "provider_result_limited": False,
        "epoch_verification_source": "EXPLICIT_REVIEWED_SUPPLEMENT_ONLY",
    }
    try:
        payload = client.name_changes(symbols, start, end, limit=limit)
    except ValueError as exc:
        code = str(exc)
        if (
            not code.startswith("LIFECYCLE_")
            or len(code) > 80
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in code)
        ):
            code = "LIFECYCLE_PROVIDER_REQUEST_FAILED"
        report.update(errors=1, error_sample=[code])
        report["unresolved_sample"] = [
            {"symbol": s, "reason": "PROVIDER_QUERY_FAILED"} for s in symbols
        ]
        return report
    records = normalize_name_changes(payload, symbols, start, end)
    report.update(
        corporate_actions_received=len(records),
        symbol_changes_received=sum(
            r["action_type"] == "name_changes"
            and bool(r["old_symbol"] and r["new_symbol"])
            and r["old_symbol"] != r["new_symbol"]
            for r in records
        ),
        provider_query_status="SUCCEEDED",
        provider_result_limited=len(records) >= limit,
    )
    # No database mutation for failed provider requests. Success only stores evidence.
    database.initialize()
    inserted, raw_conflicts = database.ingest_security_corporate_actions(records)
    report.update(raw_evidence_inserted=inserted, identity_conflicts=len(raw_conflicts))
    context_symbols = {s for r in records for s in (r["old_symbol"], r["new_symbol"]) if s}
    identities = local_lifecycle_identities(database, context_symbols)
    candidates, resolved, reasons = [], set(), {}
    for record in records:
        matches = [
            v
            for v in verifications
            if v.get("provider") == "ALPACA"
            and v.get("provider_action_id") == record["provider_action_id"]
        ]
        if record["event_key"] in raw_conflicts or len(matches) > 1:
            outcome = {"epochs": [], "status": CONFLICT, "reason": CONFLICT}
            if record["event_key"] not in raw_conflicts:
                report["identity_conflicts"] += 1
        else:
            outcome = resolve_verified_symbol_identity_epochs(
                record, identities.get(record["new_symbol"]), matches[0] if matches else None
            )
            report["identity_conflicts"] += outcome["status"] == CONFLICT
        if outcome["epochs"]:
            candidates.extend(outcome["epochs"])
            resolved.add(record["new_symbol"])
        else:
            report["unverified_evidence"] += 1
            for symbol in symbols:
                if symbol in (record["old_symbol"], record["new_symbol"]):
                    reasons[symbol] = outcome["reason"]
    if report["provider_result_limited"]:
        report.update(errors=1, error_sample=["LIFECYCLE_PROVIDER_RESULT_LIMIT_REACHED"])
    if report["identity_conflicts"] or report["provider_result_limited"]:
        report["unverified_evidence"] = len(records)
        reasons.update(
            {
                s: CONFLICT if report["identity_conflicts"] else "PROVIDER_RESULT_LIMIT_REACHED"
                for s in symbols
            }
        )
        resolved.clear()
    else:
        try:
            report["verified_epochs_created"] = database.upsert_symbol_identity_epochs(
                candidates, protect_existing=True
            )
        except ValueError:
            report["identity_conflicts"] += 1
            report["unverified_evidence"] = len(records)
            resolved.clear()
            reasons.update({s: CONFLICT for s in symbols})
    report["symbols_resolved"] = sorted(set(symbols) & resolved)
    report["symbols_unresolved"] = sorted(set(symbols) - resolved)
    report["unresolved_sample"] = [
        {"symbol": s, "reason": reasons.get(s, "NO_RETURNED_EVENT_IS_NOT_IDENTITY_EVIDENCE")}
        for s in report["symbols_unresolved"]
    ]
    return report
