"""F02: raw synthetic SDK responses; assets and state live only in temporary SQLite."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_champion_consolidation import offline as offline

from trading_system.config import load_strategy_config
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.sync import DataSynchronizer

OLD = datetime(2024, 1, 2, 22, tzinfo=UTC)
NOW = OLD + timedelta(days=1)


def raw(symbol, **changes):
    return (
        dict(
            symbol=symbol,
            name=symbol,
            exchange="NYSE",
            status="active",
            tradable=True,
            fractionable=True,
            shortable=False,
            **{"class": "us_equity"},
        )
        | changes
    )


class Response:
    def __init__(self):
        self.rows = [raw(s) for s in ("AAA", "BBB", "CCC")]
        self.error = None
        self.calls = 0

    def get_all_assets(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


@pytest.fixture
def setup(tmp_path):
    response = Response()
    client = AlpacaDataClient(
        "fixture", "fixture", historical_client=SimpleNamespace(), trading_client=response
    )
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    sync = DataSynchronizer(db, client, None, clock=lambda: OLD)
    sync.sync_assets()
    sync.clock = lambda: NOW
    return db, client, response, sync


def asset_rows(db):
    with db.connect() as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM assets ORDER BY symbol")]


def test_f02_known_malformed_asset_never_deactivates_or_reports_success(setup):
    db, client, response, sync = setup
    before = asset_rows(db)
    response.rows = [raw("AAA", name="Changed AAA"), raw("BBB"), {"symbol": "CCC"}]
    error = None
    try:
        sync.sync_assets()
    except ValueError as exc:
        error = str(exc)
    state = db.dataset_states()["asset_universe"]
    assert error == "ALPACA_ASSET_SNAPSHOT_INCOMPLETE", (
        "F02: unqualified snapshot promoted",
        db.list_tradable_asset_symbols(),
        state["status"],
        state.get("errors"),
    )
    assert asset_rows(db) == before
    assert state["status"] == "failed" and state["errors"] == 1
    assert state["last_success_at"] == OLD.isoformat()
    assert state["last_completed_at"] == OLD.isoformat()
    assert state["last_started_at"] == NOW.isoformat()
    assert state["assets_skipped_invalid"] == 1
    assert state["asset_parse_error_samples"] == [{"symbol": "CCC", "reason": "invalid status"}]
    assert state["assets_parsed"] == 2 and state["raw_assets_received"] == 3
    assert state["asset_snapshot_qualified"] is False
    assert "ALPACA_ASSET_SNAPSHOT_INCOMPLETE" in state["error"]
    assert "assets_deactivated" not in state and "records_updated" not in state
    assert response.calls == 2  # Reached the real adapter parser, not an earlier guard.


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "NEW"},
        raw("CCC", tradable="true"),
        raw("CCC", status="unrecognized"),
        raw("CCC", exchange=None),
    ],
)
def test_any_malformed_row_blocks_all_updates_even_for_unknown_asset(setup, bad, monkeypatch):
    db, client, response, sync = setup
    before = asset_rows(db)
    response.rows = [raw("AAA", name="Changed"), raw("BBB"), raw("CCC"), raw("DDD"), bad]
    monkeypatch.setattr(db, "reconcile_assets", lambda *a, **kw: pytest.fail("promotion reached"))
    with pytest.raises(ValueError, match="ASSET_SNAPSHOT_INCOMPLETE"):
        sync.sync_assets()
    assert asset_rows(db) == before
    state = db.dataset_states()["asset_universe"]
    assert state["errors"] == 1 and state["assets_skipped_invalid"] == 1
    assert state["assets_parsed"] == 4 and state["raw_assets_received"] == 5
    assert state["last_success_at"] == OLD.isoformat()


@pytest.mark.parametrize("exit_kind", ["absent", "not_tradable", "inactive"])
def test_legitimate_exit_remains_authoritative_and_atomic(setup, exit_kind):
    db, client, response, sync = setup
    response.rows = [raw("AAA"), raw("BBB")]
    if exit_kind == "not_tradable":
        response.rows.append(raw("CCC", tradable=False))
    elif exit_kind == "inactive":
        response.rows.append(raw("CCC", status="inactive"))
    result = sync.sync_assets()
    assert db.list_tradable_asset_symbols() == ["AAA", "BBB"]
    assert result["assets_deactivated"] == 1 and result["errors"] == 0
    state = db.dataset_states()["asset_universe"]
    assert state["status"] == "success" and state["asset_snapshot_qualified"]
    assert state["last_success_at"] == NOW.isoformat()
    with db.connect() as c:
        assert c.execute("SELECT tradable FROM assets WHERE symbol='CCC'").fetchone()[0] == 0
    assert result["assets_parsed"] == len(response.rows)


def test_valid_retry_clears_failure_and_does_not_reuse_old_diagnostics(setup):
    db, client, response, sync = setup
    response.rows = [raw("AAA"), raw("BBB"), {"symbol": "CCC"}]
    with pytest.raises(ValueError, match="ASSET_SNAPSHOT_INCOMPLETE"):
        sync.sync_assets()
    response.rows = [
        raw("AAA", name="Changed"),
        raw("BBB"),
        raw("CCC"),
        raw("DDD", exchange="TXSE"),
    ]
    result = sync.sync_assets()
    state = db.dataset_states()["asset_universe"]
    assert result["assets_upserted"] == 4 and result["assets_deactivated"] == 0
    assert db.list_tradable_asset_symbols() == ["AAA", "BBB", "CCC", "DDD"]
    assert state["status"] == "success" and state["errors"] == 0 and "error" not in state
    assert state["assets_skipped_invalid"] == 0 and state["unknown_exchange_values"] == ["TXSE"]
    assert state["asset_snapshot_qualified"] is True
    assert state["last_completed_at"] == state["last_success_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "response_rows,code",
    [
        ([raw("AAA"), {}, {}, {}], "PARSE_RETENTION_FAILURE"),
        ([], "PARSE_RETENTION_FAILURE"),
        ({"assets": [raw("AAA")]}, "INVALID_ASSET_SNAPSHOT"),
        ([raw("AAA")], "suspicious Alpaca asset snapshot"),
        ([raw(s, tradable=False) for s in ("AAA", "BBB", "CCC")], "empty Alpaca asset snapshot"),
    ],
)
def test_retention_shape_empty_and_universe_retention_guards_keep_old_data(
    setup, response_rows, code
):
    db, client, response, sync = setup
    before = asset_rows(db)
    response.rows = response_rows
    with pytest.raises(ValueError, match=code):
        sync.sync_assets()
    assert asset_rows(db) == before
    state = db.dataset_states()["asset_universe"]
    assert state["status"] == "failed" and state["errors"] == 1
    assert state["last_success_at"] == OLD.isoformat()


def test_response_mapping_collision_preserves_assets_and_failure_state(setup):
    db, _, response, _ = setup
    client = AlpacaDataClient(
        "fixture",
        "fixture",
        historical_client=SimpleNamespace(),
        trading_client=response,
        symbol_mapping={"AAA": "ALIAS"},
    )
    response.rows = [raw("AAA", name="Changed"), raw("ALIAS"), raw("CCC")]
    before = asset_rows(db)
    with pytest.raises(ValueError, match="SYMBOL_MAPPING_COLLISION"):
        DataSynchronizer(db, client, None, clock=lambda: NOW).sync_assets()
    assert asset_rows(db) == before
    state = db.dataset_states()["asset_universe"]
    assert state["errors"] == 1 and not state["asset_snapshot_qualified"]
    assert state["assets_parsed"] == state["raw_assets_received"] == 3


def test_explicit_mapping_and_canonical_symbols_still_promote(setup):
    db, _, response, _ = setup
    client = AlpacaDataClient(
        "fixture",
        "fixture",
        historical_client=SimpleNamespace(),
        trading_client=response,
        symbol_mapping={"AAA": "ALIAS"},
    )
    response.rows = [raw("ALIAS"), raw("BBB"), raw("CCC")]
    result = DataSynchronizer(db, client, None, clock=lambda: NOW).sync_assets()
    assert db.list_tradable_asset_symbols() == ["AAA", "BBB", "CCC"]
    assert result["errors"] == result["assets_deactivated"] == 0


def test_provider_exception_after_parse_failure_has_current_not_stale_diagnostics(setup):
    db, client, response, sync = setup
    before = asset_rows(db)
    response.rows = [raw("AAA"), raw("BBB"), {"symbol": "CCC"}]
    with pytest.raises(ValueError):
        sync.sync_assets()
    response.error = RuntimeError("synthetic provider failure")
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        sync.sync_assets()
    assert asset_rows(db) == before
    state = db.dataset_states()["asset_universe"]
    assert state["status"] == "failed" and state["errors"] == 1
    assert state["asset_snapshot_qualified"] is False
    assert "assets_parsed" not in state and "assets_skipped_invalid" not in state
    assert state["last_success_at"] == OLD.isoformat()
    response.error = None
    response.rows = [raw(s) for s in ("AAA", "BBB", "CCC")]
    sync.sync_assets()
    assert db.dataset_states()["asset_universe"]["errors"] == 0


def test_reconcile_trigger_failure_rolls_back_upserts_and_deactivations(setup):
    db, client, response, sync = setup
    before = asset_rows(db)
    response.rows = [raw("AAA", name="Changed"), raw("BBB"), raw("DDD")]
    with db.connect() as c:
        c.executescript("""CREATE TRIGGER fail_ccc BEFORE UPDATE OF tradable ON assets
            WHEN OLD.symbol='CCC' AND NEW.tradable=0
            BEGIN SELECT RAISE(ABORT,'synthetic reconciliation failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic reconciliation failure"):
        sync.sync_assets()
    assert asset_rows(db) == before  # Includes names and updated_at; no DDD inserted.
    state = db.dataset_states()["asset_universe"]
    assert state["status"] == "failed" and state["errors"] == 1
    assert state["asset_snapshot_qualified"] is True  # Qualified input, failed promotion.
    assert state["last_success_at"] == OLD.isoformat()
    assert "assets_deactivated" not in state


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"assets_skipped_invalid": 1},
        {"asset_snapshot_qualified": False},
        {"raw_assets_received": 3, "assets_parsed": 2},
    ],
)
def test_sync_refuses_explicit_incompleteness_from_alternate_adapter(
    setup, diagnostics, monkeypatch
):
    db, _, _, _ = setup
    before = asset_rows(db)
    typed = db.list_tradable_assets()
    adapter = SimpleNamespace(
        list_tradable_us_equities=lambda: typed, last_asset_diagnostics=diagnostics
    )
    monkeypatch.setattr(db, "reconcile_assets", lambda *a, **kw: pytest.fail("promotion reached"))
    with pytest.raises(ValueError, match="ASSET_SNAPSHOT_INCOMPLETE"):
        DataSynchronizer(db, adapter, None, clock=lambda: NOW).sync_assets()
    assert asset_rows(db) == before
    assert db.dataset_states()["asset_universe"]["errors"] == 1


def test_failed_refresh_blocks_shadow_readiness_until_valid_retry(setup):
    from test_champion_consolidation import bar
    from test_shadow_execution import Screens, ready_states

    from trading_system.data.forward_market import persist
    from trading_system.shadow.inputs import LocalInputs
    from trading_system.shadow.readiness import evaluate_shadow_input_readiness

    db, _, response, sync = setup
    previous_asset = db.sync_value("dataset", "asset_universe")
    ready_states(db, NOW)
    db.set_sync_value("dataset", "asset_universe", previous_asset)
    persist(
        db,
        [bar("SPY", NOW.date())],
        provider="alpaca",
        feed="sip",
        adjustment="all",
        sync_reference="synthetic-f02",
    )

    def readiness():
        with LocalInputs(db.path).snapshot() as inputs:
            return evaluate_shadow_input_readiness(
                inputs, load_strategy_config(), now=NOW, session=NOW.date(), screen_source=Screens()
            )

    assert readiness().ready
    response.rows = [raw("AAA"), raw("BBB"), {"symbol": "CCC"}]
    with pytest.raises(ValueError, match="ASSET_SNAPSHOT_INCOMPLETE"):
        sync.sync_assets()
    failed = readiness()
    assert failed.details["blocking_reasons"] == [
        "ASSET_UNIVERSE_ERRORS_PRESENT",
        "ASSET_UNIVERSE_STATE_FAILED",
    ]
    assert not failed.ready and not failed.observation_ready
    response.rows = [raw(s) for s in ("AAA", "BBB", "CCC")]
    sync.sync_assets()
    assert readiness().ready and readiness().observation_ready


def test_failed_parse_diagnostics_are_bounded_and_cleared_on_retry(setup):
    db, _, response, sync = setup
    before = asset_rows(db)
    response.rows = [raw(s) for s in ("AAA", "BBB", "CCC")] + [
        {"symbol": f"BAD{i}"} for i in range(7)
    ]
    with pytest.raises(ValueError, match="PARSE_RETENTION_FAILURE"):
        sync.sync_assets()
    state = db.dataset_states()["asset_universe"]
    assert asset_rows(db) == before
    assert state["assets_skipped_invalid"] == 7 and state["errors"] == 1
    assert len(state["asset_parse_error_samples"]) == 3
    assert [item["symbol"] for item in state["asset_parse_error_samples"]] == [
        "BAD0",
        "BAD1",
        "BAD2",
    ]
    response.rows = [raw(s) for s in ("AAA", "BBB", "CCC")]
    sync.sync_assets()
    assert db.dataset_states()["asset_universe"]["asset_parse_error_samples"] == []
