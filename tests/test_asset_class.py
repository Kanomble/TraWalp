"""Offline asset-class persistence, legacy compatibility and champion isolation."""

import socket
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import requests
from alpaca.trading.enums import AssetClass, AssetStatus

from trading_system.backtest.orb_v1_data import OrbReadOnlyDatabase
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.config import load_settings
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("asset-class tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)


def asset(symbol="TEST", **updates):
    return TradableAsset(
        symbol=symbol, name="Same name", tradable=True, fractionable=False, **updates
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (AssetClass.US_EQUITY, "US_EQUITY"),
        (AssetClass.CRYPTO, "CRYPTO"),
        (" us_equity ", "US_EQUITY"),
        ("us_option", "US_OPTION"),
        (None, "UNKNOWN"),
        ("", "UNKNOWN"),
        ("  ", "UNKNOWN"),
    ],
)
def test_asset_class_normalization(value, expected):
    assert asset(asset_class=value).asset_class == expected
    assert asset().asset_class == "UNKNOWN"


def test_legacy_asset_migration_is_idempotent_and_read_only_reads_do_not_migrate(tmp_path):
    database = Database(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """CREATE TABLE assets (
                symbol TEXT PRIMARY KEY, name TEXT NOT NULL, exchange TEXT,
                tradable INTEGER NOT NULL, fractionable INTEGER NOT NULL,
                shortable INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO assets VALUES ('OLD','Legacy ETF','NYSE',1,0,1,'original');"""
        )
    original_bytes = database.path.read_bytes()
    legacy = OrbReadOnlyDatabase(database.path).list_tradable_assets()
    assert len(legacy) == 1
    assert legacy[0].asset_class == "UNKNOWN"
    assert legacy[0].tradable is True
    assert legacy[0].shortable is True
    assert database.path.read_bytes() == original_bytes

    database.initialize()
    database.initialize()
    assert database.list_tradable_assets() == legacy
    with database.read_only() as connection:
        row = connection.execute("SELECT updated_at,asset_class FROM assets").fetchone()
        assert tuple(row) == ("original", "UNKNOWN")
    database.upsert_assets([asset("OLD", asset_class="us_equity")])
    database.initialize()
    assert database.list_tradable_assets()[0].asset_class == "US_EQUITY"


def test_asset_class_roundtrip_update_reconciliation_and_legacy_callers(tmp_path):
    database = Database(tmp_path / "classes.sqlite3")
    database.initialize()
    database.upsert_assets(
        [asset("A", asset_class="US_EQUITY"), asset("B"), asset("C", asset_class="CRYPTO")]
    )
    assert [a.asset_class for a in database.list_tradable_assets()] == [
        "US_EQUITY",
        "UNKNOWN",
        "CRYPTO",
    ]
    database.upsert_assets([asset("B", asset_class="CRYPTO")])
    database.reconcile_assets(
        [asset("A", asset_class="CRYPTO"), asset("B", asset_class="US_EQUITY")]
    )
    assert [(a.symbol, a.asset_class) for a in database.list_tradable_assets()] == [
        ("A", "CRYPTO"),
        ("B", "US_EQUITY"),
    ]
    # Legacy writers still work and cannot fabricate affirmative US_EQUITY metadata.
    database.upsert_assets([asset("B")])
    assert database.list_tradable_assets()[1].asset_class == "UNKNOWN"
    with database.read_only() as connection:
        assert tuple(
            connection.execute(
                "SELECT tradable,asset_class FROM assets WHERE symbol='C'"
            ).fetchone()
        ) == (0, "CRYPTO")


def test_explicit_asset_sync_persists_provider_class_without_inferring_it_from_request(tmp_path):
    database = Database(tmp_path / "sync.sqlite3")
    database.initialize()
    database.upsert_assets([asset("EQUITY"), asset("CRYPTO"), asset("MISSING")])
    calls = []

    def get_all_assets(request):
        calls.append(request)
        rows = []
        for symbol, asset_class in (
            ("EQUITY", AssetClass.US_EQUITY),
            ("CRYPTO", AssetClass.CRYPTO),
            ("MISSING", None),
        ):
            row = SimpleNamespace(
                symbol=symbol,
                name="ETF or company",
                exchange="NYSE",
                status=AssetStatus.ACTIVE,
                tradable=True,
                fractionable=False,
                shortable=False,
            )
            if asset_class is not None:
                row.asset_class = asset_class
            rows.append(row)
        rows.append(SimpleNamespace(symbol="INACTIVE", tradable=True, status=AssetStatus.INACTIVE))
        rows.append(SimpleNamespace(symbol="UNTRADABLE", tradable=False))
        return rows

    client = AlpacaDataClient(
        "fixture",
        "fixture",
        historical_client=SimpleNamespace(),
        trading_client=SimpleNamespace(get_all_assets=get_all_assets),
    )
    report = DataSynchronizer(database, client, None).sync_assets()
    assert report["assets_received"] == 3
    assert len(calls) == 1
    assert calls[0].asset_class == AssetClass.US_EQUITY
    assert calls[0].status == AssetStatus.ACTIVE
    assert {a.symbol: a.asset_class for a in database.list_tradable_assets()} == {
        "EQUITY": "US_EQUITY",
        "CRYPTO": "CRYPTO",
        "MISSING": "UNKNOWN",
    }


def test_asset_metadata_does_not_change_generic_companies_sec_guards_or_f_champion(tmp_path):
    database = Database(tmp_path / "champion.sqlite3")
    database.initialize()
    champion = asdict(FROZEN_CHAMPION_F)
    configured = load_settings().strategy.model_dump()
    database.upsert_assets([asset("A"), asset("B"), asset("C")])
    companies = [
        CompanyIdentity(cik=f"{i:010}", symbol=symbol, name="Issuer", sic="6798")
        for i, symbol in enumerate(("A", "B", "C"), 1)
    ]
    for company in companies:
        database.upsert_company(company)
    database.set_sync_value("sec_identity_conflicts", "A", {"status": "unresolved"})
    database.reconcile_assets(
        [asset("A", asset_class="US_EQUITY"), asset("B", asset_class="CRYPTO"), asset("C")]
    )
    assert database.list_tradable_companies() == companies
    assert database.list_tradable_asset_symbols() == ["A", "B", "C"]
    assert database.unresolved_sec_identity_conflict_symbols() == {"A"}
    assert asdict(FROZEN_CHAMPION_F) == champion
    assert FROZEN_CHAMPION_F.production_label == "F/configured/C1"
    assert load_settings().strategy.model_dump() == configured
