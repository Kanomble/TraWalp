"""Mock-only snapshot, mapping and non-destructive migration tests."""

import json
import sqlite3
from datetime import datetime

import pytest
from test_massive_reference import CATALOG, DUMMY_KEY, response, ticker
from test_massive_reference import client_factory as client_factory
from test_massive_reference import clock as clock
from test_massive_reference import offline as offline

from trading_system.data.database import Database
from trading_system.data.massive_reference import MassiveReferenceError
from trading_system.data.security_reference import (
    MAPPING_VERSION,
    SECURITY_TYPES,
    canonical_type,
    cik_state,
    normalize_catalog,
    normalize_cik,
    reference_audit,
    sync_security_reference,
)
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "reference.sqlite3")
    database.initialize()
    database.upsert_assets(
        [
            TradableAsset(
                symbol=s, name=s, tradable=True, fractionable=False, asset_class="US_EQUITY"
            )
            for s in ("A", "B", "MISSING")
        ]
    )
    database.upsert_company(CompanyIdentity(symbol="A", cik="0000000001", name="A", sic="3571"))
    database.upsert_company(CompanyIdentity(symbol="B", cik="0000000002", name="B", sic="3571"))
    return database


def successful_sync(database, client_factory):
    client, calls = client_factory(
        [response(), response(results=[ticker("A"), ticker("B", "ETF", "2")])]
    )
    summary = sync_security_reference(database, client)
    return summary, calls


def test_complete_sync_only_allowed_endpoints_and_local_audit(db, client_factory):
    summary, calls = successful_sync(db, client_factory)
    assert len(calls) == summary["massive_requests_total"] == 2
    assert summary["massive_requests_ticker_types"] == summary["massive_requests_ticker_pages"] == 1
    assert summary["provider_records_received"] == summary["local_symbols_matched"] == 2
    assert summary["missing_local_references"] == 1
    assert (
        summary["security_type_counts"]["COMMON_STOCK"]
        == summary["security_type_counts"]["ETF"]
        == 1
    )
    assert set(summary["security_type_counts"]) == set(SECURITY_TYPES)
    assert summary["sec_cik_matches"] == 2 and summary["eligible_pairs_common_stocks"] == 1
    assert summary["paid_features_used"] is False and summary["allowed_endpoints_only"]
    rows, metadata = db.security_reference_snapshot()
    assert rows["A"]["provider_security_type"] == "CS"
    assert rows["B"]["security_type"] == "ETF"
    assert metadata["sync_success"] and metadata["type_catalog"] == normalize_catalog(CATALOG)
    assert DUMMY_KEY not in json.dumps([summary, rows, metadata])
    now = datetime.fromisoformat(metadata["observed_at"])
    audit = reference_audit(db, now=now)
    assert audit["massive_requests_total"] == 0 and audit["snapshot_age_seconds"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure", [response(403), response(402), KeyboardInterrupt(), response(400)]
)
def test_partial_pagination_or_interruption_preserves_prior_snapshot(db, client_factory, failure):
    successful_sync(db, client_factory)
    before = db.security_reference_snapshot()
    client, calls = client_factory(
        [
            response(),
            response(
                results=[ticker("NEW1")],
                next_url="https://api.massive.com/v3/reference/tickers?cursor=2",
            ),
            response(
                results=[ticker("NEW2")],
                next_url="https://api.massive.com/v3/reference/tickers?cursor=3",
            ),
            failure,
        ]
    )
    with pytest.raises((MassiveReferenceError, KeyboardInterrupt)):
        sync_security_reference(db, client)
    assert db.security_reference_snapshot() == before
    assert len(calls) == 4
    with db.read_only() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM security_reference_snapshots").fetchone()[0]
            == 1
        )


def test_promotion_database_failure_rolls_back_rows_pointer_and_success_metadata(
    db, client_factory
):
    successful_sync(db, client_factory)
    before = db.security_reference_snapshot()
    with db.connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_new_reference BEFORE INSERT ON security_reference "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    client, _ = client_factory([response(), response(results=[ticker("NEW")])])
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        sync_security_reference(db, client)
    assert db.security_reference_snapshot() == before
    with db.read_only() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM security_reference_snapshots").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("CS", "COMMON_STOCK"),
        ("ETF", "ETF"),
        ("ETS", "ETF"),
        ("ETV", "ETP"),
        ("SP", "STRUCTURED_PRODUCT"),
        ("PFD", "PREFERRED_STOCK"),
        ("ADRC", "ADR"),
        ("WARRANT", "WARRANT"),
        ("UNIT", "UNIT"),
        ("RIGHT", "RIGHT"),
        ("ETP", "ETP"),
        ("ETN", "ETN"),
        ("FUND", "FUND"),
        ("BOND", "BOND"),
        ("OTHER", "OTHER"),
        ("FUTURE_CODE", "UNKNOWN"),
        ("ABSENT", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_catalog_validated_canonical_mapping(code, expected):
    assert canonical_type(code, normalize_catalog(CATALOG)) == expected


def test_common_stock_requires_matching_catalog_and_new_codes_fail_closed():
    assert canonical_type("CS", []) == "UNKNOWN"
    catalog = [
        {
            "code": "CS",
            "description": "A changed definition",
            "asset_class": "stocks",
            "locale": "us",
        }
    ]
    assert canonical_type("CS", normalize_catalog(catalog)) == "UNKNOWN"
    catalog[0].update(code="FUTURE_COMMON", description="Common Stock")
    assert canonical_type("FUTURE_COMMON", normalize_catalog(catalog)) == "UNKNOWN"


AUTHORITATIVE_V2_CATALOG = [
    {"asset_class": "stocks", "code": "ETS", "description": "Single-security ETF", "locale": "us"},
    {
        "asset_class": "stocks",
        "code": "ETV",
        "description": "Exchange Traded Vehicle",
        "locale": "us",
    },
    {"asset_class": "stocks", "code": "RIGHT", "description": "Rights", "locale": "us"},
    {"asset_class": "stocks", "code": "SP", "description": "Structured Product", "locale": "us"},
]


@pytest.mark.parametrize(
    "code,expected",
    [("ETS", "ETF"), ("ETV", "ETP"), ("RIGHT", "RIGHT"), ("SP", "STRUCTURED_PRODUCT")],
)
def test_v2_mapping_uses_exact_user_supplied_authoritative_catalog(code, expected):
    assert MAPPING_VERSION == "MASSIVE_STOCK_TYPES_V2"
    assert canonical_type(code, normalize_catalog(AUTHORITATIVE_V2_CATALOG)) == expected
    changed = [
        {**row, "description": row["description"] + " "} if row["code"] == code else row
        for row in AUTHORITATIVE_V2_CATALOG
    ]
    assert canonical_type(code, normalize_catalog(changed)) == "UNKNOWN"


def test_v2_old_right_description_and_future_codes_remain_unknown():
    catalog = normalize_catalog(
        [
            {"asset_class": "stocks", "code": "RIGHT", "description": "Right", "locale": "us"},
            {
                "asset_class": "stocks",
                "code": "NEW_ETS",
                "description": "Single-security ETF",
                "locale": "us",
            },
            {
                "asset_class": "stocks",
                "code": "NEW_CS",
                "description": "Common Stock",
                "locale": "us",
            },
        ]
    )
    assert all(
        canonical_type(code, catalog) == "UNKNOWN" for code in ("RIGHT", "NEW_ETS", "NEW_CS")
    )


def test_v2_snapshot_retains_provider_codes_and_structured_product_counts(db, client_factory):
    rows = [ticker(code, code, "1") for code in ("ETS", "ETV", "RIGHT", "SP")]
    client, _ = client_factory([response(results=AUTHORITATIVE_V2_CATALOG), response(results=rows)])
    summary = sync_security_reference(db, client)
    records, metadata = db.security_reference_snapshot()
    assert metadata["mapping_version"] == "MASSIVE_STOCK_TYPES_V2"
    assert summary["security_type_counts"]["STRUCTURED_PRODUCT"] == 1
    assert summary["unknown_provider_type_counts"] == {}
    assert {r["provider_security_type"] for r in records.values()} == {"ETS", "ETV", "RIGHT", "SP"}


@pytest.mark.parametrize(
    "local,provider,expected",
    [
        ("1", "0000000001", "CIK_MATCH"),
        ("1", "2", "CIK_MISMATCH"),
        ("1", None, "CIK_MISSING_MASSIVE"),
        (None, "1", "CIK_MISSING_LOCAL"),
        (None, None, "CIK_MISSING_LOCAL"),
    ],
)
def test_cik_cross_check(local, provider, expected):
    assert cik_state(local, provider) == expected
    assert normalize_cik("1") == "0000000001"


@pytest.mark.parametrize("value", [None, "", "0", "0000000000", "1.0", "abc", "12345678901"])
def test_invalid_cik_is_not_an_identity(value):
    assert normalize_cik(value) is None


def test_migration_idempotent_non_destructive_and_legacy_local_reader(db, client_factory):
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO bars (symbol,timeframe,timestamp,open,high,low,close,volume) "
            "VALUES ('A','1Day','2025-01-01','1','1','1','1',100)"
        )
        connection.execute("DROP TABLE security_reference")
        connection.execute("DROP TABLE security_reference_active")
        connection.execute("DROP TABLE security_reference_snapshots")
    assert db.security_reference_snapshot() == ({}, None)
    tables = ("assets", "companies", "bars", "fundamental_facts")
    with db.read_only() as connection:
        before = {t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables}
    db.initialize()
    db.initialize()
    successful_sync(db, client_factory)
    db.initialize()
    with db.read_only() as connection:
        after = {t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables}
    assert before == after


def test_unknown_codes_are_preserved_and_auditable(db, client_factory):
    client, _ = client_factory([response(), response(results=[ticker("A", "FUTURE_CODE")])])
    summary = sync_security_reference(db, client)
    assert summary["unknown_provider_type_counts"] == {"FUTURE_CODE": 1}
    assert summary["security_type_counts"]["UNKNOWN"] == 1
    assert summary["eligible_pairs_common_stocks"] == 0


def test_full_paginated_sync_uses_one_catalog_and_one_limiter(db, client_factory):
    from urllib.parse import urlsplit

    from test_massive_reference import assert_rolling_cap

    pages = [
        response(
            results=[ticker(f"S{i}")],
            next_url=(
                f"https://api.massive.com/v3/reference/tickers?cursor={i + 1}" if i < 6 else None
            ),
        )
        for i in range(7)
    ]
    client, calls = client_factory([response(), *pages])
    summary = sync_security_reference(db, client)
    assert summary["provider_records_received"] == summary["massive_requests_ticker_pages"] == 7
    assert summary["massive_requests_ticker_types"] == 1
    assert summary["massive_requests_total"] == 8
    assert {urlsplit(c["url"]).path for c in calls} == {
        "/v3/reference/tickers",
        "/v3/reference/tickers/types",
    }
    assert_rolling_cap([c["time"] for c in calls])


@pytest.mark.parametrize("records", [[], [ticker(), ticker()], [ticker(cik="INVALID")]])
def test_invalid_complete_snapshot_is_not_promoted(db, client_factory, records):
    successful_sync(db, client_factory)
    before = db.security_reference_snapshot()
    client, _ = client_factory([response(), response(results=records)])
    with pytest.raises(ValueError, match="MASSIVE_REFERENCE_"):
        sync_security_reference(db, client)
    assert db.security_reference_snapshot() == before
