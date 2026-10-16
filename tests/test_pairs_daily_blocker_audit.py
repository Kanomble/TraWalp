"""Small synthetic regressions for availability classification, not strategy tuning."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from test_pairs_stat_arb_v1 import START, identities, seed, synthetic_epoch
from test_pairs_stat_arb_v1 import offline as offline
from test_pairs_stat_arb_v1 import synthetic_security_types as synthetic_security_types
from test_pairs_stat_arb_v1_run_identity import add_bad
from test_pairs_stat_arb_v1_security import config as config

from trading_system.backtest import pairs_stat_arb_v1_daily_audit as audit
from trading_system.backtest.pairs_stat_arb_v1_data import prepare_pairs_data
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database


def requirement(day=START, symbol="A"):
    return {
        "symbol": symbol,
        "session": str(day),
        "roles": "SELECTED_ADV_WINDOW",
        "status": "LOCAL_MISSING",
        "validation_blocking": True,
        "sessions": 1,
    }


def test_true_missing_in_verified_membership_and_checked_provider_range():
    row = audit.classify_requirement(
        requirement(),
        identities()[0],
        local_present=False,
        range_checked=True,
        provider_bar_expected=True,
    )
    assert row["classification"] == audit.TRUE_LOCAL_MISSING
    assert row["validation_blocking"] and row["action"] == "SYNC_DAILY"


@pytest.mark.parametrize(
    "day,expected",
    [(date(2019, 1, 2), audit.PRE_LISTING), (date(2026, 1, 2), audit.POST_DELISTING)],
)
def test_requirement_outside_verified_membership_is_nonmember_and_never_synced(day, expected):
    identity = identities()[0]
    identity["identity_epoch"] = synthetic_epoch(
        "A", identity["cik"], date(2020, 1, 1), date(2025, 12, 31)
    ).record()
    row = audit.classify_requirement(
        requirement(day), identity, local_present=False, range_checked=True
    )
    assert row["classification"] == expected
    assert row["status"] != "LOCAL_MISSING" and not row["validation_blocking"]
    assert row["action"] == "NO_SYNC"


def test_present_bar_with_unverified_metadata_has_metadata_specific_diagnosis():
    row = audit.classify_requirement(
        requirement(), identities()[0], local_present=True, range_checked=False
    )
    assert row["classification"] == audit.METADATA
    assert row["status"] == "REQUIRED_PRESENT" and row["action"] == "NO_SYNC"


@pytest.mark.parametrize(
    "result", [None, "EMPTY_RESPONSE_UNRESOLVED", "HTTP_404", "REQUEST_FAILED"]
)
def test_ordinary_current_identity_and_empty_response_remain_unknown(result):
    identity = {
        **identities()[0],
        "identity_epoch": None,
        "identity_status": "CURRENT_SECURITY_NO_IDENTITY_WARNING",
    }
    row = audit.classify_requirement(
        requirement(), identity, local_present=False, range_checked=True, provider_result=result
    )
    assert row["classification"] == audit.UNKNOWN
    assert row["validation_blocking"] and row["action"] == "NO_SYNC"


def test_positive_session_availability_can_verify_ordinary_current_local_missing():
    identity = {
        **identities()[0],
        "identity_epoch": None,
        "identity_status": "CURRENT_SECURITY_NO_IDENTITY_WARNING",
    }
    row = audit.classify_requirement(
        requirement(),
        identity,
        local_present=False,
        range_checked=True,
        provider_result="BAR_RETURNED",
    )
    assert row["classification"] == audit.TRUE_LOCAL_MISSING


def test_unverified_addressing_requires_mapping_review_without_sync():
    row = audit.classify_requirement(
        requirement(),
        identities()[0],
        local_present=False,
        range_checked=True,
        provider_symbol="OTHER",
        provider_mapping_verified=False,
    )
    assert row["classification"] == audit.MAPPING and row["action"] == "NO_SYNC"


def test_targeted_plan_only_syncs_true_missing_and_splits_official_session_gaps():
    rows = [
        {**requirement(date(2024, 1, 2)), "classification": audit.TRUE_LOCAL_MISSING},
        {**requirement(date(2024, 1, 4)), "classification": audit.TRUE_LOCAL_MISSING},
        {**requirement(date(2024, 1, 5), "UNKNOWN"), "classification": audit.UNKNOWN},
    ]
    plan = audit.repair_plan(rows)
    assert len(plan) == 3
    assert all(
        row["classification"] == audit.TRUE_LOCAL_MISSING
        for row in plan
        if row["action"] == "SYNC_DAILY"
    )
    assert all(row["start"] == row["end"] for row in plan)


def test_identity_quarantine_still_produces_no_daily_requirements(tmp_path, config):
    db, *_ = seed(tmp_path, end=START)
    add_bad(db, "XYZ")
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    assert not any(row["symbol"] == "XYZ" for row in prepared.requirements)


@pytest.mark.parametrize("mapping", [{}, {"XYZ.A": "XYZ-A"}])
def test_generic_class_share_provider_mapping_preserves_canonical_db_key(tmp_path, mapping):
    provider = mapping.get("XYZ.A", "XYZ.A")
    bar = SimpleNamespace(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=10,
        high=12,
        low=9,
        close=11,
        volume=100,
        trade_count=4,
        vwap=10.5,
    )
    requests = []

    def get_bars(request):
        requests.append(request)
        return SimpleNamespace(data={provider: [bar]})

    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=SimpleNamespace(get_stock_bars=get_bars),
        trading_client=SimpleNamespace(),
        symbol_mapping=mapping,
    )
    bars = client.daily_bars(
        ["XYZ.A"], datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC)
    )
    assert requests[0].symbol_or_symbols == [provider]
    assert bars[0].symbol == "XYZ.A"
    db = Database(tmp_path / "mapping.sqlite3")
    db.initialize()
    db.upsert_bars(bars)
    with db.read_only() as c:
        assert [row[0] for row in c.execute("SELECT symbol FROM bars")] == ["XYZ.A"]


@pytest.mark.parametrize("batch_size", [1, 200])
def test_mapping_collision_fails_closed_before_provider_call(batch_size):
    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=SimpleNamespace(),
        trading_client=SimpleNamespace(),
        symbol_mapping={"X.A": "X-A"},
    )
    with pytest.raises(ValueError, match="MAPPING_COLLISION"):
        client.daily_bars(
            ["X.A", "X-A"],
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
            batch_size=batch_size,
        )


def test_asset_alias_collision_is_not_silently_merged():
    def assets(request):
        return [
            SimpleNamespace(
                symbol=symbol,
                name=symbol,
                exchange="NASDAQ",
                tradable=True,
                fractionable=False,
                shortable=False,
                asset_class="us_equity",
                status="active",
            )
            for symbol in ("X.A", "X-A")
        ]

    client = AlpacaDataClient(
        "key",
        "secret",
        historical_client=SimpleNamespace(),
        trading_client=SimpleNamespace(get_all_assets=assets),
        symbol_mapping={"X.A": "X-A"},
    )
    with pytest.raises(ValueError, match="MAPPING_COLLISION"):
        client.list_tradable_us_equities()


def test_verified_membership_and_checked_iex_range_alone_do_not_prove_bar_availability():
    row = audit.classify_requirement(
        requirement(),
        identities()[0],
        local_present=False,
        range_checked=True,
    )
    assert row["classification"] == audit.UNKNOWN
    assert row["action"] == "NO_SYNC"


def test_missing_provider_metadata_does_not_invent_an_alias():
    row = audit.classify_requirement(
        requirement(),
        identities()[0],
        local_present=False,
        range_checked=True,
        provider_mapping_verified=False,
    )
    assert row["classification"] == audit.UNKNOWN
    assert row["action"] == "NO_SYNC"


def test_existing_artifact_audit_binds_positive_observation_and_rejects_other_feed(
    tmp_path, config
):
    db, sessions, *_ = seed(tmp_path, end=START)
    # Choose a calibration input before the signal, outside the outcome tail.
    missing = sessions[sessions.index(START) - 30]
    with db.connect() as c:
        c.execute("DELETE FROM bars WHERE symbol='A' AND timestamp LIKE ?", (f"{missing}%",))
    prepared = prepare_pairs_data(db, config, START, START, preflight=True)
    doc = {
        "manifest_fingerprint": prepared.manifest["fingerprint"],
        "required_sessions": [
            row for row in prepared.requirements if row["status"] == "LOCAL_MISSING"
        ],
    }
    observation = {
        "symbol": "A",
        "session": str(missing),
        "provider_symbol": "A",
        "provider": "ALPACA",
        "feed": config.universe.market_data_feed.lower(),
        "adjustment": config.universe.market_data_adjustment,
        "status": "BAR_RETURNED",
    }
    result = audit.audit_daily_requirements(
        db, prepared.manifest, doc, config, observations=[observation]
    )
    assert result["classification_counts"][audit.TRUE_LOCAL_MISSING] == 1
    assert len(result["requirements"]) == 1
    assert result["repair_plan"][0]["sessions"] == [str(missing)]
    with pytest.raises(ValueError, match="PROVIDER_OBSERVATION_INVALID"):
        audit.audit_daily_requirements(
            db, prepared.manifest, doc, config, observations=[{**observation, "feed": "other"}]
        )
