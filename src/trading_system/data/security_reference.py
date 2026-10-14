"""Current reference snapshot mapping, identity checks and local audit; no prices."""

import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime

SOURCE = "MASSIVE"
SOURCE_VERSION = "MASSIVE_V3_REFERENCE_TICKERS"
REFERENCE_SEMANTICS = "CURRENT_REFERENCE_SECURITY_TYPE_NOT_PIT"
MAPPING_VERSION = "MASSIVE_STOCK_TYPES_V2"
PAIRS_COMPANY_UNIVERSE_SCOPE = "LOCAL_TRADABLE_NON_OTC_US_EQUITY_WITH_VALID_SEC_COMPANY_CIK_SIC"
IDENTITY_CONFLICT_POLICY = "SYMBOL_QUARANTINE_NONBLOCKING"
IDENTITY_CONFLICT_CLASSIFICATION_VERSION = "LOCAL_CACHED_SEC_MASSIVE_CIK_V1"
EXCLUDED_IDENTITY_CONFLICT = "EXCLUDED_IDENTITY_CONFLICT"
MASSIVE_REFERENCE_CIK_CONFLICT = "MASSIVE_REFERENCE_CIK_CONFLICT"
LOCAL_IDENTITY_STALE_OR_TICKER_REUSE = "LOCAL_IDENTITY_STALE_OR_TICKER_REUSE"
UNRESOLVED_CROSS_SOURCE_IDENTITY = "UNRESOLVED_CROSS_SOURCE_IDENTITY"
CIK_CONFLICT_POLICY = (
    "SEC_CANONICAL;COMMON_STOCK_LOCAL_MASSIVE_OR_CACHED_SEC_CONFLICT_QUARANTINED;"
    "MASSIVE_MISSING_ACCEPTED_WITHOUT_CACHED_SEC_CONFLICT"
)
SECURITY_TYPES = (
    "COMMON_STOCK",
    "PREFERRED_STOCK",
    "ETF",
    "ETP",
    "ETN",
    "FUND",
    "ADR",
    "WARRANT",
    "UNIT",
    "RIGHT",
    "STRUCTURED_PRODUCT",
    "BOND",
    "OTHER",
    "UNKNOWN",
)

# Exact provider catalog descriptions, never ticker/name heuristics. A newly
# introduced code remains UNKNOWN until this reviewed mapping is extended.
_MAPPING = {
    "CS": ("COMMON_STOCK", {"Common Stock"}),
    "PFD": ("PREFERRED_STOCK", {"Preferred Stock"}),
    "ETF": ("ETF", {"Exchange Traded Fund"}),
    "ETS": ("ETF", {"Single-security ETF"}),
    "ETP": ("ETP", {"Exchange Traded Product"}),
    "ETV": ("ETP", {"Exchange Traded Vehicle"}),
    "ETN": ("ETN", {"Exchange Traded Note"}),
    "FUND": ("FUND", {"Fund"}),
    "ADRC": ("ADR", {"American Depository Receipt Common"}),
    "ADRP": ("ADR", {"American Depository Receipt Preferred"}),
    "ADRR": ("ADR", {"American Depository Receipt Rights"}),
    "ADRW": ("ADR", {"American Depository Receipt Warrants"}),
    "WARRANT": ("WARRANT", {"Warrant"}),
    "UNIT": ("UNIT", {"Unit"}),
    "RIGHT": ("RIGHT", {"Rights"}),
    "SP": ("STRUCTURED_PRODUCT", {"Structured Product"}),
    "BOND": ("BOND", {"Bond"}),
    "OTHER": ("OTHER", {"Other"}),
}
REFERENCE_FIELDS = (
    "symbol",
    "security_type",
    "provider_security_type",
    "source",
    "source_version",
    "market",
    "locale",
    "active",
    "primary_exchange",
    "cik",
    "composite_figi",
    "share_class_figi",
    "provider_updated_at",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def normalize_cik(value):
    value = str(value or "").strip()
    return value.zfill(10) if re.fullmatch(r"[0-9]{1,10}", value) and int(value) else None


def cik_state(local, provider):
    local, provider = normalize_cik(local), normalize_cik(provider)
    if local is None:
        return "CIK_MISSING_LOCAL"
    if provider is None:
        return "CIK_MISSING_MASSIVE"
    return "CIK_MATCH" if local == provider else "CIK_MISMATCH"


def valid_sic(value):
    value = str(value or "").strip()
    return value.zfill(4) if re.fullmatch(r"[0-9]{3,4}", value) and int(value) else None


def _local_company_metadata(database):
    """Read only local asset/identity metadata; exclude invalid CIKs before hydration.

    This reader does not change CompanyIdentity validation or the general/F
    database helpers. It is used only by the Pairs base gate and reference audit.
    """
    with database.read_only() as connection:
        connection.execute("BEGIN")
        assets = [
            {
                "symbol": row["symbol"],
                "tradable": bool(row["tradable"]),
                "asset_class": dict(row).get("asset_class", "UNKNOWN"),
                "exchange": dict(row).get("exchange"),
            }
            for row in connection.execute("SELECT * FROM assets WHERE tradable=1 ORDER BY symbol")
        ]
        companies = {
            row["symbol"]: dict(row)
            for row in connection.execute(
                "SELECT c.symbol,c.cik,c.sic FROM companies c JOIN assets a ON a.symbol=c.symbol "
                "WHERE a.tradable=1 ORDER BY c.symbol"
            )
        }
    return assets, companies


def _pairs_company_rows(assets, companies):
    """Company gate before exchange exclusion, also used for OTC audit counts."""
    base = []
    for asset in assets:
        company = companies.get(asset["symbol"])
        if (
            not asset["tradable"]
            or asset["asset_class"] != "US_EQUITY"
            or asset["symbol"] == "SPY"
            or company is None
            or normalize_cik(company["cik"]) is None
        ):
            continue
        sic = valid_sic(company["sic"])
        if sic is None:
            continue
        base.append(
            {
                "symbol": asset["symbol"],
                "cik": company["cik"],
                "sic": sic,
                "sic2": sic[:2],
                "tradable": True,
                "asset_class": asset["asset_class"],
                "exchange": str(asset.get("exchange") or "").strip().upper(),
            }
        )
    return sorted(base, key=lambda row: row["symbol"])


def pairs_base_company_universe(database, *, assets=None, companies=None):
    """Frozen non-OTC local company gate, before any Massive qualification."""
    if assets is None or companies is None:
        local_assets, local_companies = _local_company_metadata(database)
        assets = local_assets if assets is None else assets
        companies = local_companies if companies is None else companies
    return [r for r in _pairs_company_rows(assets, companies) if r["exchange"] != "OTC"]


def cached_sec_ticker_ciks(database):
    """Read the existing SEC cache; never refresh it or migrate issuer histories."""
    with database.read_only() as connection:
        row = connection.execute(
            "SELECT value FROM sync_state WHERE source=? AND key=?",
            ("sec_reference", "ticker_to_cik"),
        ).fetchone()
    if row is None:
        return {}
    try:
        values = json.loads(row["value"])
        if not isinstance(values, dict):
            raise ValueError("not a ticker map")
    except (ValueError, TypeError) as exc:
        raise ValueError("PAIRS_SEC_IDENTITY_CACHE_INVALID") from exc
    return {symbol: normalize_cik(cik) for symbol, cik in values.items()}


def pairs_security_qualification(
    *, reference_present, resolved, security_type, local_cik, current_sec_cik, massive_cik
):
    """Static eligibility only; conflict provenance is evidence, not issuer repair."""
    local, current, massive = map(normalize_cik, (local_cik, current_sec_cik, massive_cik))
    reason = None
    if not reference_present:
        status, blocking = "SECURITY_TYPE_REFERENCE_MISSING", True
    elif not resolved:
        status, blocking = "SECURITY_TYPE_UNKNOWN", True
    elif security_type != "COMMON_STOCK":
        status, blocking = "EXCLUDED_SECURITY_TYPE", False
    elif (massive and local != massive) or (current and local != current):
        status, blocking = EXCLUDED_IDENTITY_CONFLICT, False
        if local == current and massive and massive != current:
            reason = MASSIVE_REFERENCE_CIK_CONFLICT
        elif current and current == massive and local != current:
            reason = LOCAL_IDENTITY_STALE_OR_TICKER_REUSE
        else:
            reason = UNRESOLVED_CROSS_SOURCE_IDENTITY
    else:
        status, blocking = "ELIGIBLE_COMMON_STOCK", False
    return {
        "status": status,
        "validation_blocking": blocking,
        "local_cik": local,
        "current_sec_cik": current,
        "massive_cik": massive,
        "cik_cross_check": cik_state(local, massive),
        "identity_conflict_class": reason,
        "quarantine_status": EXCLUDED_IDENTITY_CONFLICT if reason else None,
    }


def identity_qualification_fingerprint(rows):
    """Bind every non-OTC base member, including quarantines and missing markers."""
    fields = (
        "symbol",
        "exchange",
        "sic",
        "local_cik",
        "current_sec_cik",
        "massive_cik",
        "identity_conflict_class",
        "quarantine_status",
        "status",
        "validation_blocking",
    )
    return digest(
        [{k: row.get(k) for k in fields} for row in sorted(rows, key=lambda r: r["symbol"])]
    )


def normalize_catalog(records):
    catalog = []
    codes = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("asset_class") != "stocks"
            or record.get("locale") != "us"
            or not isinstance(record.get("code"), str)
            or not record["code"]
            or not isinstance(record.get("description"), str)
            or record["code"] in codes
        ):
            raise ValueError("MASSIVE_REFERENCE_INVALID_TYPE_CATALOG")
        codes.add(record["code"])
        catalog.append({k: record[k] for k in ("code", "description", "asset_class", "locale")})
    if not catalog:
        raise ValueError("MASSIVE_REFERENCE_INVALID_TYPE_CATALOG")
    return sorted(catalog, key=lambda r: r["code"])


def canonical_type(code, catalog):
    description = next((r["description"] for r in catalog if r["code"] == code), None)
    category, descriptions = _MAPPING.get(code, ("UNKNOWN", set()))
    return category if description in descriptions else "UNKNOWN"


def map_record(record, catalog, observed_at):
    symbol = record.get("ticker")
    if not isinstance(symbol, str) or not symbol or len(symbol) > 64:
        raise ValueError("MASSIVE_REFERENCE_INVALID_RECORD")
    if record.get("market") != "stocks" or record.get("locale") != "us":
        raise ValueError("MASSIVE_REFERENCE_UNEXPECTED_UNIVERSE")
    if record.get("active") is not True:
        raise ValueError("MASSIVE_REFERENCE_UNEXPECTED_UNIVERSE")
    code = record.get("type")
    if code is not None and not isinstance(code, str):
        raise ValueError("MASSIVE_REFERENCE_INVALID_RECORD")
    fields = {
        "symbol": symbol,
        "security_type": canonical_type(code, catalog),
        "provider_security_type": code,
        "source": SOURCE,
        "source_version": SOURCE_VERSION,
        "market": "stocks",
        "locale": "us",
        "active": True,
        "cik": normalize_cik(record.get("cik")),
    }
    for local, provider in (
        ("primary_exchange", "primary_exchange"),
        ("composite_figi", "composite_figi"),
        ("share_class_figi", "share_class_figi"),
        ("provider_updated_at", "last_updated_utc"),
    ):
        value = record.get(provider)
        if value is not None and not isinstance(value, str):
            raise ValueError("MASSIVE_REFERENCE_INVALID_RECORD")
        fields[local] = value
    if record.get("cik") not in (None, "") and fields["cik"] is None:
        raise ValueError("MASSIVE_REFERENCE_INVALID_CIK")
    # Only approved reference fields participate; provider bodies/URLs aren't stored.
    raw = {
        k: record.get(k)
        for k in (
            "ticker",
            "type",
            "market",
            "locale",
            "active",
            "cik",
            "primary_exchange",
            "composite_figi",
            "share_class_figi",
            "last_updated_utc",
        )
    }
    return {**fields, "observed_at": observed_at, "raw_fingerprint": digest(raw)}


def membership_fingerprint(records, symbols):
    # Missing rows and known exclusions participate, not just selected common stocks.
    return digest(
        [
            {k: records[s].get(k) for k in REFERENCE_FIELDS}
            if s in records
            else {"symbol": s, "reference_missing": True}
            for s in sorted(set(symbols))
        ]
    )


def reference_audit(database, *, now=None):
    records, snapshot = database.security_reference_snapshot()
    assets, companies = _local_company_metadata(database)
    assets = [a for a in assets if a["asset_class"] == "US_EQUITY"]
    current_sec = cached_sec_ticker_ciks(database)
    otc_excluded = [
        r["symbol"] for r in _pairs_company_rows(assets, companies) if r["exchange"] == "OTC"
    ]
    base = {
        r["symbol"]: r
        for r in pairs_base_company_universe(database, assets=assets, companies=companies)
    }
    symbols = {a["symbol"] for a in assets}
    matched = symbols & records.keys()
    states = Counter(
        cik_state(companies[s]["cik"] if s in companies else None, records[s]["cik"])
        for s in matched
    )
    distribution = Counter(r["security_type"] for r in records.values())
    qualifications = {}
    for symbol, identity in base.items():
        row = records.get(symbol, {})
        qualifications[symbol] = pairs_security_qualification(
            reference_present=symbol in records,
            resolved=(
                row.get("source") == SOURCE
                and row.get("source_version") == SOURCE_VERSION
                and row.get("market") == "stocks"
                and row.get("locale") == "us"
                and row.get("active")
                and row.get("security_type") in SECURITY_TYPES
                and row.get("security_type") != "UNKNOWN"
            ),
            security_type=row.get("security_type"),
            local_cik=identity["cik"],
            current_sec_cik=current_sec.get(symbol),
            massive_cik=row.get("cik"),
        )
    eligible = sum(q["status"] == "ELIGIBLE_COMMON_STOCK" for q in qualifications.values())
    base_missing = sorted(
        s for s, q in qualifications.items() if q["status"] == "SECURITY_TYPE_REFERENCE_MISSING"
    )
    base_unknown = sorted(
        s for s, q in qualifications.items() if q["status"] == "SECURITY_TYPE_UNKNOWN"
    )
    quarantined = sorted(
        s for s, q in qualifications.items() if q["status"] == EXCLUDED_IDENTITY_CONFLICT
    )
    common_conflicts = sorted(
        s
        for s in base.keys() & records.keys()
        if records[s]["security_type"] == "COMMON_STOCK"
        and cik_state(base[s]["cik"], records[s]["cik"]) == "CIK_MISMATCH"
    )
    pairs_diagnostics = {}
    for key, values in (
        ("pairs_base_company_missing_reference", base_missing),
        ("pairs_base_company_unknown_type", base_unknown),
        ("common_stock_cik_mismatch", common_conflicts),
        ("pairs_otc_excluded", otc_excluded),
        ("pairs_identity_quarantine", quarantined),
        ("pairs_non_otc_missing_reference", base_missing),
        ("pairs_non_otc_unknown_type", base_unknown),
    ):
        pairs_diagnostics.update(
            {
                key + "_count": len(values),
                key + "_symbols": values[:100],
                key + "_symbols_truncated": len(values) > 100,
            }
        )
    observed = snapshot.get("observed_at") if snapshot else None
    age = (
        ((now or datetime.now(UTC)) - datetime.fromisoformat(observed)).total_seconds()
        if observed
        else None
    )
    return {
        "provider": SOURCE,
        "reference_semantics": REFERENCE_SEMANTICS,
        "reference_snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
        "security_reference_fingerprint": snapshot.get("reference_fingerprint")
        if snapshot
        else None,
        "type_catalog_fingerprint": snapshot.get("type_catalog_fingerprint") if snapshot else None,
        "provider_type_mapping_version": snapshot.get("mapping_version") if snapshot else None,
        "expected_provider_type_mapping_version": MAPPING_VERSION,
        "security_reference_mapping_current": bool(
            snapshot and snapshot.get("mapping_version") == MAPPING_VERSION
        ),
        "snapshot_age_seconds": age,
        "provider_records_received": len(records),
        "local_symbols_total": len(symbols),
        "local_symbols_matched": len(matched),
        "coverage_fraction": len(matched) / len(symbols) if symbols else None,
        "security_type_counts": {kind: distribution[kind] for kind in SECURITY_TYPES},
        "unknown_provider_type_counts": dict(
            Counter(
                r["provider_security_type"] or "MISSING"
                for r in records.values()
                if r["security_type"] == "UNKNOWN"
            )
        ),
        "missing_local_references": len(symbols - matched),
        "sec_cik_matches": states["CIK_MATCH"],
        "sec_cik_mismatches": states["CIK_MISMATCH"],
        "cik_unavailable": states["CIK_MISSING_LOCAL"] + states["CIK_MISSING_MASSIVE"],
        "cik_states": dict(states),
        "eligible_pairs_common_stocks": eligible,
        "company_universe_scope": PAIRS_COMPANY_UNIVERSE_SCOPE,
        "pairs_base_company_universe_count": len(base),
        "pairs_non_otc_company_universe_count": len(base),
        "identity_conflict_policy": IDENTITY_CONFLICT_POLICY,
        "identity_conflict_classification_version": IDENTITY_CONFLICT_CLASSIFICATION_VERSION,
        "pairs_identity_quarantine_by_reason": dict(
            sorted(
                Counter(qualifications[s]["identity_conflict_class"] for s in quarantined).items()
            )
        ),
        "pairs_reference_identity_blocking_count": len(base_missing) + len(base_unknown),
        "company_security_types_complete": not base_missing and not base_unknown,
        "outside_pairs_company_universe_count": len(symbols - base.keys()),
        **pairs_diagnostics,
        "massive_requests_total": 0,
        "alpaca_requests_total": 0,
        "sec_requests_total": 0,
        "paid_features_used": False,
    }


def sync_security_reference(database, client):
    """Fetch entirely before one transactional promotion; interruption keeps old data."""
    started = time.monotonic()
    observed = datetime.now(UTC).isoformat()
    catalog = normalize_catalog(client.get_ticker_types())
    records = {}
    for raw in client.iterate_stock_tickers():
        row = map_record(raw, catalog, observed)
        if row["symbol"] in records:
            raise ValueError("MASSIVE_REFERENCE_DUPLICATE_SYMBOL")
        records[row["symbol"]] = row
    if not records:
        raise ValueError("MASSIVE_REFERENCE_EMPTY_SNAPSHOT")
    reference_fingerprint = membership_fingerprint(records, records)
    catalog_fingerprint = digest(catalog)
    snapshot_id = digest(
        {
            "reference": reference_fingerprint,
            "catalog": catalog_fingerprint,
            "mapping": MAPPING_VERSION,
            "observed_at": observed,
        }
    )
    metadata = {
        "snapshot_id": snapshot_id,
        "reference_fingerprint": reference_fingerprint,
        "type_catalog_fingerprint": catalog_fingerprint,
        "type_catalog": catalog,
        "mapping_version": MAPPING_VERSION,
        "observed_at": observed,
        "source": SOURCE,
        "source_version": SOURCE_VERSION,
        "reference_semantics": REFERENCE_SEMANTICS,
        "sync_success": True,
        **client.diagnostics,
    }
    database.promote_security_reference(records.values(), metadata)
    return {
        **reference_audit(database),
        **client.diagnostics,
        "status": "SUCCESS",
        "required_access_tier": "STOCKS_BASIC_FREE_COMPATIBLE",
        "design": "Massive reference integration designed for Stocks Basic Free.",
        "elapsed_seconds": time.monotonic() - started,
    }
