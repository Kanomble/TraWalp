"""Local scope selection through the real Daily pipeline with an offline provider."""

import json
import socket
from datetime import date

import pytest
import requests
from test_daily_history import DailyAlpaca, _bar

from trading_system import cli
from trading_system.config import StorageConfig, load_settings
from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset

START, END = date(2025, 4, 21), date(2025, 4, 22)
COMMAND = ["sync-daily-history", "--start", str(START), "--end", str(END)]


def forbidden(*args, **kwargs):
    raise AssertionError("Scope resolution must not call this dependency")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(DataSynchronizer, "sync_assets", forbidden)


@pytest.fixture
def local_sync(tmp_path, monkeypatch):
    database = Database(tmp_path / "scope.sqlite3")
    database.initialize()
    definitions = [
        ("AAPL", "ETF Trust", "US_EQUITY", True),
        ("SPY", "Index ETF", "US_EQUITY", True),
        ("GLD", "Commodity Trust", "US_EQUITY", True),
        ("CONFLICT", "Company", "US_EQUITY", True),
        ("COIN", "Common Stock", "CRYPTO", True),
        ("UNKNOWN", "Company", "UNKNOWN", True),
        ("OPTION", "Company", "US_OPTION", True),
        ("INACTIVE", "Company", "US_EQUITY", False),
    ]
    database.upsert_assets(
        [
            TradableAsset(
                symbol=symbol,
                name=name,
                asset_class=asset_class,
                tradable=tradable,
                fractionable=False,
            )
            for symbol, name, asset_class, tradable in definitions
        ]
    )
    for index, symbol in enumerate(("AAPL", "UNKNOWN", "COIN", "CONFLICT", "INACTIVE"), 1):
        database.upsert_company(CompanyIdentity(cik=f"{index:010}", symbol=symbol, name=symbol))
    database.set_sync_value("sec_identity_conflicts", "CONFLICT", {"status": "unresolved"})
    provider = DailyAlpaca([_bar(symbol, END) for symbol, *_ in definitions])
    synchronizer = DataSynchronizer(
        database, provider, None, market_data_batch_size=2, daily_overlap_bars=1
    )
    settings = load_settings().model_copy(deep=True)
    settings.strategy.storage = StorageConfig(
        database_path=database.path, reports_path=tmp_path / "reports"
    )
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)

    def factory(_settings, selected_database, *, with_alpaca, with_sec):
        assert selected_database.path == database.path
        assert with_alpaca and not with_sec
        return synchronizer

    monkeypatch.setattr(cli, "_synchronizer", factory)
    return database, provider, synchronizer


@pytest.mark.parametrize("scope", [[], ["--universe", "companies"]])
def test_default_and_explicit_companies_retain_legacy_membership_and_guards(
    local_sync, monkeypatch, capsys, scope
):
    database, provider, _ = local_sync
    monkeypatch.setattr(Database, "list_tradable_assets", forbidden)
    assert cli.main(COMMAND + scope) == 0
    report = json.loads(capsys.readouterr().out)
    # No asset-class gate in the legacy company path; SPY is still added independently.
    assert [symbol for batch, *_ in provider.calls for symbol in batch] == [
        "AAPL",
        "COIN",
        "SPY",
        "UNKNOWN",
    ]
    assert report["symbols_requested"] == 4
    assert report["identity_conflict_sample"] == ["CONFLICT"]
    assert report["incremental"] is True
    assert report["universe_scope"] == {"universe": "COMPANIES"}
    assert [c.symbol for c in database.list_tradable_companies()] == [
        "AAPL",
        "COIN",
        "CONFLICT",
        "UNKNOWN",
    ]


@pytest.mark.parametrize("full", [False, True])
def test_explicit_symbols_remain_normalized_deduplicated_and_supported(
    local_sync, monkeypatch, capsys, full
):
    _, provider, _ = local_sync
    monkeypatch.setattr(Database, "list_tradable_companies", forbidden)
    arguments = COMMAND + ["--symbols", "gld, aapl,AAPL,CONFLICT"]
    assert cli.main(arguments + (["--full-window"] if full else [])) == 0
    report = json.loads(capsys.readouterr().out)
    assert [symbol for batch, *_ in provider.calls for symbol in batch] == ["AAPL", "GLD", "SPY"]
    assert report["identity_conflict_sample"] == ["CONFLICT"]
    assert report["incremental"] is not full
    assert report["universe_scope"] == {"universe": "SYMBOLS"}


def test_us_equity_cli_uses_one_asset_load_no_sec_and_existing_batches(
    local_sync, monkeypatch, capsys
):
    database, provider, _ = local_sync
    for method in (
        "list_tradable_companies",
        "company_symbol_to_cik",
        "unresolved_sec_identity_conflict_symbols",
    ):
        monkeypatch.setattr(Database, method, forbidden)
    original = Database.list_tradable_assets
    loads = []

    def counted(self):
        loads.append(self.path)
        return list(reversed(original(self)))

    monkeypatch.setattr(Database, "list_tradable_assets", counted)
    assert cli.main(COMMAND + ["--universe", "us-equity"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert loads == [database.path]
    assert [batch for batch, *_ in provider.calls] == [("AAPL", "CONFLICT"), ("GLD", "SPY")]
    for key, value in {
        "universe": "US_EQUITY",
        "tradable_assets_total": 7,
        "selected_us_equity_symbols": 4,
        "unknown_asset_class_excluded": 1,
        "other_asset_classes_excluded": 2,
    }.items():
        assert report["universe_scope"][key] == value
        assert database.dataset_states()["daily_history"]["universe_scope"][key] == value
    for key, value in {
        "identity_conflicts_skipped": 0,
        "symbols_requested": 4,
        "request_batches": 2,
        "bars_inserted": 4,
        "timeframe": "1d",
        "feed": "iex",
        "adjustment": "all",
    }.items():
        assert report[key] == value
        assert database.dataset_states()["daily_history"][key] == value
    assert report["symbols_without_older_data"] == 4  # Later availability is not an error.
    assert report["errors"] == 0

    provider.calls.clear()
    assert cli.main(COMMAND + ["--universe", "us-equity"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert loads == [database.path, database.path]
    assert all(
        start.date() == END for _, start, _ in provider.calls
    )  # Verified correction overlap.
    assert repeated["bars_inserted"] == repeated["bars_updated"] == 0


def test_us_equity_defensively_deduplicates_and_does_not_inject_benchmark(local_sync, monkeypatch):
    database, provider, synchronizer = local_sync
    with database.connect() as connection:
        connection.execute("UPDATE assets SET asset_class='UNKNOWN' WHERE symbol='SPY'")
    assets = database.list_tradable_assets()
    monkeypatch.setattr(database, "list_tradable_assets", lambda: [*reversed(assets), assets[0]])
    monkeypatch.setattr(database, "unresolved_sec_identity_conflict_symbols", forbidden)
    result = synchronizer.sync_daily_history(None, START, END, universe="us-equity")
    assert [symbol for batch, *_ in provider.calls for symbol in batch] == [
        "AAPL",
        "CONFLICT",
        "GLD",
    ]
    assert result["universe_scope"]["selected_us_equity_symbols"] == 3


def test_empty_us_equity_scope_never_falls_back_to_companies_or_benchmark(local_sync, monkeypatch):
    database, provider, synchronizer = local_sync
    with database.connect() as connection:
        connection.execute("UPDATE assets SET asset_class='UNKNOWN'")
    monkeypatch.setattr(database, "list_tradable_companies", forbidden)
    monkeypatch.setattr(database, "unresolved_sec_identity_conflict_symbols", forbidden)
    with pytest.raises(ValueError, match="symbol selection is empty"):
        synchronizer.sync_daily_history(None, START, END, universe="us-equity")
    assert provider.calls == []


@pytest.mark.parametrize("scope", ["companies", "us-equity"])
def test_cli_rejects_combined_symbol_and_universe_scope_before_setup(monkeypatch, scope):
    monkeypatch.setattr(cli, "load_settings", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main(COMMAND + ["--symbols", "AAPL", "--universe", scope])
    assert exc.value.code == 2


def test_api_rejects_us_equity_with_explicit_symbols(local_sync):
    _, provider, synchronizer = local_sync
    with pytest.raises(ValueError, match="exclusive"):
        synchronizer.sync_daily_history(["AAPL"], START, END, universe="us-equity")
    assert provider.calls == []
