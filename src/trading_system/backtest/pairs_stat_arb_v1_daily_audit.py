"""Daily availability audit and bounded repair plans; no discovery or trading rules.

An empty provider response and a successfully checked range are not evidence of
listing boundaries, security continuity, or confirmed per-session absence.
"""

import json
from collections import Counter, defaultdict
from datetime import date

from trading_system.backtest.pairs_stat_arb_v1_identity import QUARANTINED, USABLE, session_status
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.data.qualification import provider_range_verified
from trading_system.data.security_identity import identity_epoch
from trading_system.data.security_reference import digest

TRUE_LOCAL_MISSING = "TRUE_LOCAL_MISSING"
PRE_LISTING = "PRE_LISTING_OR_NOT_YET_ACTIVE"
POST_DELISTING = "POST_DELISTING_OR_NO_LONGER_ACTIVE"
MAPPING = "PROVIDER_SYMBOL_MAPPING"
NO_SESSION = "PROVIDER_CONFIRMED_NO_SESSION"
METADATA = "LOCAL_COVERAGE_METADATA_ERROR"
LOGIC = "REQUIREMENT_LOGIC_ERROR"
UNKNOWN = "UNKNOWN_BLOCKING"
CATEGORIES = (
    TRUE_LOCAL_MISSING,
    PRE_LISTING,
    POST_DELISTING,
    MAPPING,
    NO_SESSION,
    METADATA,
    LOGIC,
    UNKNOWN,
)
AUDIT_VERSION = "PAIRS_DAILY_REQUIREMENT_AUDIT_V1"


def classify_requirement(
    row,
    identity,
    *,
    local_present,
    range_checked,
    valid_session=True,
    provider_symbol=None,
    provider_mapping_verified=True,
    provider_result=None,
    provider_bar_expected=False,
    confirmed_session_absence=False,
):
    """Classify one unique requirement without inferring membership from bars.

    provider_result refers to this exact symbol/session/feed/adjustment, after
    normal adapter validation. BAR_RETURNED is positive availability evidence.
    Verified membership and range metadata alone cannot guarantee an IEX bar.
    provider_bar_expected requires independent evidence that this provider/feed
    should contain a native bar for the particular session.
    confirmed_session_absence requires a separate authoritative session assertion;
    the Alpaca empty/404 path must never set it.
    """
    day = date.fromisoformat(row["session"])
    symbol = row["symbol"]
    provider_symbol = provider_symbol or symbol
    classification, reason, blocking, status = (
        UNKNOWN,
        "SESSION_AVAILABILITY_UNPROVEN",
        True,
        "LOCAL_MISSING",
    )
    epoch = identity_epoch(identity)
    if identity.get("identity_status") == QUARANTINED:
        classification, reason = LOGIC, "QUARANTINED_SYMBOL_HAS_REQUIREMENT"
    elif not valid_session or not row.get("roles"):
        classification, reason = LOGIC, "INVALID_SESSION_OR_REQUIREMENT_ROLE"
    elif epoch is not None and not epoch.contains(day):
        classification = PRE_LISTING if day < epoch.effective_start else POST_DELISTING
        reason, blocking, status = (
            "OUTSIDE_VERIFIED_SECURITY_MEMBERSHIP",
            False,
            "VERIFIED_OUTSIDE_EPOCH",
        )
    elif local_present:
        classification = LOGIC if range_checked else METADATA
        reason = (
            "STALE_MISSING_REQUIREMENT"
            if range_checked
            else "BAR_PRESENT_PROVIDER_RANGE_NOT_VERIFIED"
        )
        blocking, status = False, "REQUIRED_PRESENT"
    elif not provider_mapping_verified:
        if provider_symbol != symbol:
            classification, reason = MAPPING, "PROVIDER_ADDRESSING_UNVERIFIED"
        else:
            reason = "CURRENT_PROVIDER_METADATA_UNAVAILABLE"
    elif session_status(identity, day) not in USABLE:
        reason = "IDENTITY_QUALIFICATION_NOT_ESTABLISHED"
    elif confirmed_session_absence:
        classification, reason = NO_SESSION, "AUTHORITATIVE_PROVIDER_SESSION_ABSENCE"
        # Absence is explained but the frozen strategy still needs a native bar.
        # Do not interpolate or silently remove this symbol from Top-N.
    elif provider_result == "BAR_RETURNED":
        classification, reason = TRUE_LOCAL_MISSING, "LOCAL_BAR_ABSENT_PROVIDER_BAR_VERIFIED"
    elif provider_result in {"EMPTY_RESPONSE_UNRESOLVED", "REQUEST_FAILED", "HTTP_404"}:
        reason = (
            "PROVIDER_EMPTY_RESPONSE_UNRESOLVED"
            if provider_result == "EMPTY_RESPONSE_UNRESOLVED"
            else "PROVIDER_REQUEST_UNRESOLVED"
        )
    elif epoch is not None and range_checked and provider_bar_expected:
        classification, reason = (
            TRUE_LOCAL_MISSING,
            "LOCAL_BAR_ABSENT_VERIFIED_MEMBERSHIP_RANGE_CHECKED",
        )
    elif range_checked:
        reason = "PROVIDER_RANGE_CHECKED_SESSION_AVAILABILITY_UNPROVEN"
    return {
        **row,
        "status": status,
        "classification": classification,
        "reason": reason,
        "provider_symbol": provider_symbol,
        "validation_blocking": blocking,
        "action": "SYNC_DAILY" if classification == TRUE_LOCAL_MISSING else "NO_SYNC",
    }


def consecutive_ranges(sessions):
    """Partition observations by adjacency in the official calendar, including holidays."""
    days = sorted(set(sessions))
    if not days:
        return []
    official = trading_sessions_between(days[0], days[-1])
    indexes = {day: index for index, day in enumerate(official)}
    groups = []
    for day in days:
        if day not in indexes:
            raise ValueError("DAILY_AUDIT_NON_TRADING_SESSION")
        if not groups or indexes[day] != indexes[groups[-1][-1]] + 1:
            groups.append([])
        groups[-1].append(day)
    return groups


def repair_plan(classified):
    """Only positive known local gaps may acquire a SYNC_DAILY action."""
    grouped = defaultdict(list)
    for row in classified:
        if row["classification"] not in CATEGORIES:
            raise ValueError("DAILY_AUDIT_UNEXPECTED_CLASSIFICATION")
        grouped[row["symbol"], row["classification"]].append(date.fromisoformat(row["session"]))
    result = []
    for (symbol, classification), days in sorted(grouped.items()):
        for block in consecutive_ranges(days):
            result.append(
                {
                    "symbol": symbol,
                    "classification": classification,
                    "start": str(block[0]),
                    "end": str(block[-1]),
                    "required_sessions": len(block),
                    "sessions": [str(day) for day in block],
                    "action": "SYNC_DAILY" if classification == TRUE_LOCAL_MISSING else "NO_SYNC",
                }
            )
    return result


def blocker_summary(classified):
    symbols = defaultdict(list)
    for row in classified:
        symbols[row["symbol"]].append(row)
    top = []
    for symbol, rows in symbols.items():
        dates = [date.fromisoformat(row["session"]) for row in rows]
        blocks = consecutive_ranges(dates)
        top.append(
            {
                "symbol": symbol,
                "blocking_sessions": sum(row["validation_blocking"] for row in rows),
                "missing_required_sessions": len(rows),
                "roles": dict(Counter(role for row in rows for role in row["roles"].split(";"))),
                "share_of_total_gaps": len(rows) / len(classified),
                "first_missing_session": str(min(dates)),
                "last_missing_session": str(max(dates)),
                "longest_consecutive_gap": max(map(len, blocks)),
                "gap_blocks": len(blocks),
                "classifications": dict(Counter(row["classification"] for row in rows)),
            }
        )
    return {
        "audit_version": AUDIT_VERSION,
        "required_missing_sessions": len(classified),
        "classification_counts": {
            key: sum(row["classification"] == key for row in classified) for key in CATEGORIES
        },
        "top_blocking_symbols": sorted(
            top, key=lambda row: (-row["blocking_sessions"], row["symbol"])
        ),
    }


def audit_daily_requirements(database, manifest, requirements, config, *, observations=()):
    """Audit an existing preflight locally; never rediscover or fetch provider data.

    Explicit diagnostic observations are scoped to the exact original required
    sessions and current feed/adjustment. They are audit inputs only, never an
    implicit report-file dependency of normal Pairs qualification/preflight.
    """
    from trading_system.backtest.liquid_universe import fingerprint
    from trading_system.backtest.pairs_stat_arb_v1_data import load_daily

    if (
        manifest["manifest_version"] not in {7, 8, 9}
        or manifest["fingerprint"] != fingerprint(manifest)
        or requirements["manifest_fingerprint"] != manifest["fingerprint"]
    ):
        raise ValueError("DAILY_AUDIT_MANIFEST_INCONSISTENT")
    feed, adjustment = (
        config.universe.market_data_feed.lower(),
        config.universe.market_data_adjustment,
    )
    if (
        manifest["market_data_feed"].lower() != feed
        or manifest["market_data_adjustment"] != adjustment
    ):
        raise ValueError("DAILY_AUDIT_PROVENANCE_MISMATCH")
    missing = requirements["required_sessions"]
    keys = {(row["symbol"], row["session"]) for row in missing}
    if len(keys) != len(missing) or any(
        row["status"] != "LOCAL_MISSING" or row["sessions"] != 1 for row in missing
    ):
        raise ValueError("DAILY_AUDIT_REQUIREMENTS_INVALID")
    identities = {row["symbol"]: row for row in manifest["universe"]}
    if any(symbol not in identities for symbol, _ in keys):
        raise ValueError("DAILY_AUDIT_NONMEMBER_REQUIREMENT")
    provider_results = {}
    for row in observations:
        key = row["symbol"], row["session"]
        if (
            key not in keys
            or row["provider"] != "ALPACA"
            or row["feed"] != feed
            or row["adjustment"] != adjustment
            or row["provider_symbol"] != row["symbol"]
            or row["status"]
            not in {"BAR_RETURNED", "EMPTY_RESPONSE_UNRESOLVED", "REQUEST_FAILED", "HTTP_404"}
            or key in provider_results
        ):
            raise ValueError("DAILY_AUDIT_PROVIDER_OBSERVATION_INVALID")
        provider_results[key] = row["status"]
    classified, symbol_audits = [], []
    if not missing:
        return {**blocker_summary([]), "requirements": [], "repair_plan": [], "symbols": []}
    start, end = min(day for _, day in keys), max(day for _, day in keys)
    official = set(trading_sessions_between(date.fromisoformat(start), date.fromisoformat(end)))
    symbols = sorted({symbol for symbol, _ in keys})
    bars = load_daily(database, symbols, date.fromisoformat(start), date.fromisoformat(end))
    coverage = database.sync_values("daily_history_coverage")
    assets = {row.symbol: row for row in database.list_tradable_assets()}
    for row in missing:
        symbol, day = row["symbol"], date.fromisoformat(row["session"])
        checked = provider_range_verified(
            coverage.get(symbol), day, day, feed=feed, adjustment=adjustment
        )
        classified.append(
            classify_requirement(
                row,
                identities[symbol],
                local_present=bars.get((symbol, day)) is not None,
                range_checked=checked,
                valid_session=day in official,
                provider_mapping_verified=symbol in assets,
                provider_result=provider_results.get((symbol, str(day))),
            )
        )
    with database.read_only() as connection:
        for symbol in symbols:
            row = identities[symbol]
            bounds = connection.execute(
                "SELECT count(*),min(substr(timestamp,1,10)),max(substr(timestamp,1,10)) "
                "FROM bars WHERE symbol=? AND timeframe='1d'",
                (symbol,),
            ).fetchone()
            cached = connection.execute(
                "SELECT payload FROM raw_sec_cache WHERE cik=? AND endpoint='submissions'",
                (row["cik"],),
            ).fetchone()
            recent = json.loads(cached[0]).get("filings", {}).get("recent", {}) if cached else {}
            filing_context = [
                {"form": form, "filing_date": recent["filingDate"][index]}
                for index, form in enumerate(recent.get("form", []))
                if form in {"8-A12B", "25", "25-NSE"}
            ]
            symbol_audits.append(
                {
                    "symbol": symbol,
                    "provider_symbol": assets[symbol].symbol if symbol in assets else None,
                    "current_tradable": assets[symbol].tradable if symbol in assets else None,
                    "exchange": assets[symbol].exchange if symbol in assets else None,
                    "security_type": row["security_type"],
                    "provider_security_type": row.get("provider_security_type"),
                    "primary_exchange": row.get("primary_exchange"),
                    "reference_ticker": row["symbol"],
                    "identity_status": row["identity_status"],
                    "verified_epoch": row["identity_epoch"],
                    "listing_boundary_verified": identity_epoch(row) is not None,
                    "sec_filing_context_not_interval_proof": filing_context,
                    "local_bar_count": bounds[0],
                    "first_local_observation": bounds[1],
                    "last_local_observation": bounds[2],
                    "provider_range_metadata": coverage.get(symbol),
                }
            )
    return {
        **blocker_summary(classified),
        "source_manifest_fingerprint": manifest["fingerprint"],
        "audit_input_fingerprint": digest(
            {
                "manifest": manifest["fingerprint"],
                "symbols": symbol_audits,
                "observations": observations,
            }
        ),
        "requirements": classified,
        "repair_plan": repair_plan(classified),
        "symbols": symbol_audits,
        "provider_observations": list(observations),
    }


def main(argv=None):
    """Explicit read-only artifact audit; provider calls and sync are not performed."""
    import argparse
    import csv
    from pathlib import Path

    from trading_system.backtest.liquid_universe import ResearchReadOnlyDatabase
    from trading_system.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--daily-requirements", type=Path, required=True)
    parser.add_argument("--provider-observations", type=Path)
    parser.add_argument("--output-stem", required=True)
    args = parser.parse_args(argv)
    if (
        not args.output_stem
        or Path(args.output_stem).name != args.output_stem
        or any(c in args.output_stem for c in "/\\:")
        or args.output_stem in {".", ".."}
    ):
        raise ValueError("Daily audit output stem must be a plain filename stem")
    settings = load_settings()
    directory = settings.strategy.storage.reports_path
    paths = {
        key: directory / f"{args.output_stem}_{key}.{suffix}"
        for key, suffix in (("audit", "json"), ("repair_plan", "json"), ("requirements", "csv"))
    }
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("Daily audit output already exists")
    observations = (
        json.loads(args.provider_observations.read_text(encoding="utf-8"))["observations"]
        if args.provider_observations
        else []
    )
    result = audit_daily_requirements(
        ResearchReadOnlyDatabase(settings.strategy.storage.database_path),
        json.loads(args.candidate_manifest.read_text(encoding="utf-8")),
        json.loads(args.daily_requirements.read_text(encoding="utf-8")),
        settings.strategy,
        observations=observations,
    )
    directory.mkdir(parents=True, exist_ok=True)
    paths["audit"].write_text(json.dumps(result, indent=2), encoding="utf-8")
    paths["repair_plan"].write_text(json.dumps(result["repair_plan"], indent=2), encoding="utf-8")
    fields = [
        "symbol",
        "session",
        "roles",
        "status",
        "classification",
        "reason",
        "provider_symbol",
        "validation_blocking",
        "action",
    ]
    with paths["requirements"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["requirements"])
    print(json.dumps(result["classification_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
