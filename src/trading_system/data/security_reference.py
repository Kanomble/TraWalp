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
MAPPING_VERSION = "MASSIVE_STOCK_TYPES_V1"
CIK_CONFLICT_POLICY = "SEC_CANONICAL;BOTH_PRESENT_MUST_MATCH;MASSIVE_MISSING_ACCEPTED"
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
    "ETP": ("ETP", {"Exchange Traded Product"}),
    "ETN": ("ETN", {"Exchange Traded Note"}),
    "FUND": ("FUND", {"Fund"}),
    "ADRC": ("ADR", {"American Depository Receipt Common"}),
    "ADRP": ("ADR", {"American Depository Receipt Preferred"}),
    "ADRR": ("ADR", {"American Depository Receipt Rights"}),
    "ADRW": ("ADR", {"American Depository Receipt Warrants"}),
    "WARRANT": ("WARRANT", {"Warrant"}),
    "UNIT": ("UNIT", {"Unit"}),
    "RIGHT": ("RIGHT", {"Right"}),
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
    assets = [a for a in database.list_tradable_assets() if a.asset_class == "US_EQUITY"]
    companies = {c.symbol: c for c in database.list_tradable_companies()}
    symbols = {a.symbol for a in assets}
    matched = symbols & records.keys()
    states = Counter(
        cik_state(companies[s].cik if s in companies else None, records[s]["cik"]) for s in matched
    )
    distribution = Counter(r["security_type"] for r in records.values())
    eligible = sum(
        records[s]["security_type"] == "COMMON_STOCK"
        and records[s]["source"] == SOURCE
        and records[s]["source_version"] == SOURCE_VERSION
        and records[s]["market"] == "stocks"
        and records[s]["locale"] == "us"
        and records[s]["active"]
        and s in companies
        and normalize_cik(companies[s].cik) is not None
        and valid_sic(companies[s].sic) is not None
        and cik_state(companies[s].cik, records[s]["cik"]) != "CIK_MISMATCH"
        and s != "SPY"
        for s in matched
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
        "massive_requests_total": 0,
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
