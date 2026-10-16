"""Pairs run-level data quality policy over the existing security interval resolver.

Current reference qualification is not historical interval verification. Ordinary
current securities may be researched without an epoch; concrete unresolved risks
exclude the symbol for the whole run. No price/volume or outcome values are read.
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from trading_system.data.security_identity import (
    ACTIVE,
    OUTSIDE,
    UNKNOWN,
    SymbolIdentityEpoch,
    current_identity_epoch,
    identity_epoch,
    same_epoch_window,
)
from trading_system.data.security_reference import digest

QUARANTINED = "IDENTITY_QUARANTINED"
CURRENT = "CURRENT_SECURITY_NO_IDENTITY_WARNING"
CONFLICT = "IDENTITY_EVIDENCE_CONFLICT"
UNRESOLVED = "UNRESOLVED_HISTORICAL_SECURITY_IDENTITY"
FAILED = "IDENTITY_QUALIFICATION_FAILED"
POLICY = "PAIRS_RUN_GLOBAL_IDENTITY_QUALIFICATION_V1"
# A long disconnected history is only a warning when independently accompanied
# by a current-issuer exchange registration in the missing interval. Neither
# observation establishes security identity or an effective interval boundary.
DISCONTINUITY_DAYS = 365
USABLE = {ACTIVE, CURRENT}


def session_status(identity, session):
    if identity.get("identity_status") == QUARANTINED:
        return QUARANTINED
    epoch = identity_epoch(identity)
    if epoch is not None:
        return ACTIVE if epoch.contains(session) else OUTSIDE
    return CURRENT if identity.get("identity_status") == CURRENT else UNKNOWN


def window_status(identities, symbols, sessions):
    """Keep verified interval checks strict; never synthesize epochs for current rows."""
    symbols = tuple(symbols)
    verified = []
    for symbol in symbols:
        row = identities.get(symbol, {})
        if row.get("identity_status") == QUARANTINED:
            return QUARANTINED
        if identity_epoch(row) is not None:
            verified.append(symbol)
        elif row.get("identity_status") != CURRENT:
            return UNKNOWN
    state = same_epoch_window(identities, verified, sessions)
    return state if state != ACTIVE or len(verified) == len(symbols) else CURRENT


def _history_warnings(database, rows, end):
    """Only timestamp discontinuities plus cached SEC registration, never bar prices.

    A missing bar, a prefix, or even a long data gap alone remains a data gap.
    Read only through the signal end, excluding the outcome tail. Inputs and
    missing-cache markers participate in the qualification fingerprint.
    """
    warnings, inputs = defaultdict(list), {}
    with database.read_only() as connection:
        for row in rows:
            symbol = row["symbol"]
            dates = [
                date.fromisoformat(r[0][:10])
                for r in connection.execute(
                    "SELECT timestamp FROM bars WHERE symbol=? AND timeframe='1d' "
                    "AND timestamp<? ORDER BY timestamp",
                    (symbol, str(end + timedelta(days=1))),
                )
            ]
            gaps = [
                (a, b)
                for a, b in zip(dates, dates[1:], strict=False)
                if (b - a).days >= DISCONTINUITY_DAYS
            ]
            if not gaps:
                inputs[symbol] = {"discontinuities": []}
                continue
            cached = connection.execute(
                "SELECT payload FROM raw_sec_cache WHERE cik=? AND endpoint='submissions'",
                (row["cik"],),
            ).fetchone()
            filings = []
            if cached is not None:
                payload = json.loads(cached[0])
                recent = payload["filings"]["recent"]
                forms, filed = recent["form"], recent["filingDate"]
                accessions = recent["accessionNumber"]
                if not (len(forms) == len(filed) == len(accessions)):
                    raise ValueError("IDENTITY_SEC_REGISTRATION_EVIDENCE_INVALID")
                for form, day, accession in zip(forms, filed, accessions, strict=True):
                    if form == "8-A12B" and date.fromisoformat(day) <= end:
                        filings.append({"filing_date": day, "accession": accession})
            inputs[symbol] = {
                "discontinuities": [[str(a), str(b)] for a, b in gaps],
                "current_cik": row["cik"],
                "registration_cache_present": cached is not None,
                "registrations": filings,
            }
            for a, b in gaps:
                registrations = [f for f in filings if str(a) < f["filing_date"] <= str(b)]
                if registrations:
                    warnings[symbol].append(
                        {
                            "trigger": "DISCONTINUOUS_HISTORY_WITH_CURRENT_ISSUER_REGISTRATION",
                            "previous_observation": str(a),
                            "next_observation": str(b),
                            "registrations": registrations,
                        }
                    )
    return warnings, inputs


def qualify_identities(database, universe, start, end):
    """Resolve known unusable identities before SIC2/ADV/Top-N/pair discovery.

    Invalid/corrupt inputs raise: callers must block the preflight, never convert
    a failed read or classification into a resolved universe exclusion.
    """
    resolved_epochs = {}
    for row in universe:
        # Validate all evidence, including hints, before deciding known unusable.
        # Unverified suspicion cannot veto a verified interval. The resolver
        # itself remains unchanged and sees all verified competing assertions.
        verified = [
            record
            for record in row["identity_epoch_evidence"]
            if SymbolIdentityEpoch.from_record(record).verified
        ]
        resolved_epochs[row["symbol"]] = current_identity_epoch(row, verified)
    symbols = {row["symbol"] for row in universe}
    actions = defaultdict(list)
    for action in database.security_corporate_actions(symbols):
        # These are the persisted fields of the existing lifecycle ingestion.
        # Exclude provider raw bodies and request strings from manifests/reports.
        safe = {
            key: action[key]
            for key in (
                "provider",
                "event_key",
                "action_type",
                "old_symbol",
                "new_symbol",
                "raw_fingerprint",
                "observed_at",
            )
        }
        if safe["action_type"] != "name_changes":
            raise ValueError("IDENTITY_LIFECYCLE_CLASSIFICATION_UNEXPECTED")
        if (
            any(
                not isinstance(safe[key], str) or not safe[key].strip()
                for key in ("provider", "event_key", "raw_fingerprint")
            )
            or datetime.fromisoformat(safe["observed_at"]).tzinfo is None
        ):
            raise ValueError("IDENTITY_LIFECYCLE_EVIDENCE_INVALID")
        for symbol in {safe["old_symbol"], safe["new_symbol"]} & symbols:
            actions[symbol].append(safe)
    histories, history_inputs = _history_warnings(
        database, [row for row in universe if resolved_epochs[row["symbol"]] is None], end
    )
    qualified, diagnostics = [], []
    for row in universe:
        symbol = row["symbol"]
        records = row["identity_epoch_evidence"]
        epoch = resolved_epochs[symbol]
        conflicting = epoch is None and any(
            current_identity_epoch(row, [record]) is not None for record in records
        )
        risk = bool(records or actions[symbol] or histories[symbol])
        state = (
            QUARANTINED
            if conflicting
            else ACTIVE
            if epoch is not None
            else QUARANTINED
            if risk
            else CURRENT
        )
        outside = epoch is not None and (
            epoch["effective_start"] > str(end)
            or (epoch["effective_end"] is not None and epoch["effective_end"] <= str(start))
        )
        if outside:
            state = OUTSIDE
        inputs = {
            "policy": POLICY,
            "discontinuity_days": DISCONTINUITY_DAYS,
            "epochs": records,
            "lifecycle": actions[symbol],
            "history": history_inputs.get(symbol),
        }
        sources = sorted(
            {r["source"] for r in records}
            | {r["provider"] for r in actions[symbol]}
            | ({"LOCAL_BAR_TIMESTAMPS", "CACHED_SEC_SUBMISSIONS"} if histories[symbol] else set())
        )
        diagnostic = {
            "symbol": symbol,
            "identity_status": state,
            "status": state,
            "reason": CONFLICT if conflicting else UNRESOLVED if state == QUARANTINED else None,
            "evidence_source": sources or ["CURRENT_LOCAL_SECURITY_REFERENCE"],
            "evidence_fingerprint": digest(inputs),
            "risk_signals": histories[symbol],
            "run_excluded": state in {QUARANTINED, OUTSIDE},
            "validation_blocking": False,
            "record_kind": "SYMBOL_IDENTITY",
            "input_scope": "RUN_IDENTITY_QUALIFICATION",
            "roles": "HISTORICAL_SECURITY_IDENTITY",
        }
        diagnostics.append(diagnostic)
        if not diagnostic["run_excluded"]:
            qualified.append({**row, "identity_epoch": epoch, "identity_status": state})
    return qualified, diagnostics
