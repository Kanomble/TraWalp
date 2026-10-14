"""Small synthetic-only tests for raw evidence, strict promotion and existing epoch protection."""

import json
import socket
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from trading_system import cli
from trading_system.data.alpaca_corporate_actions import ENDPOINT, AlpacaCorporateActionClient
from trading_system.data.database import Database
from trading_system.data.security_identity import LIFECYCLE_EVIDENCE, SymbolIdentityEpoch
from trading_system.data.security_lifecycle import (
    CONFLICT,
    normalize_name_changes,
    resolve_verified_symbol_identity_epochs,
    sync_security_lifecycle,
)

START, END = date(2025, 1, 1), date(2025, 8, 1)
ACTION_ID = "12345678-1234-5678-1234-567812345678"
CIK = "0000000001"
OBSERVED = "2026-10-14T00:00:00+00:00"


def forbidden(*args, **kwargs):
    raise AssertionError("This test must not contact a provider or start a strategy")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(cli, "_synchronizer", forbidden)
    monkeypatch.setattr(cli, "AlpacaDataClient", forbidden)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "lifecycle.sqlite3")
    database.initialize()
    return database


def event(**changes):
    return {
        "id": ACTION_ID,
        "old_symbol": "OLD",
        "new_symbol": "NEW",
        "old_cusip": "123456789",
        "new_cusip": "987654321",
        "process_date": "2025-06-20",
        **changes,
    }


def action(raw=None):
    return normalize_name_changes(
        {"name_changes": [event() if raw is None else raw]},
        ["NEW"],
        START,
        END,
        observed_at=OBSERVED,
    )[0]


def epoch(
    symbol="NEW",
    *,
    cik=CIK,
    stable="SHARE_CLASS_FIGI:CLASS1",
    start=date(2025, 6, 18),
    end=None,
    source="REVIEWED_EXCHANGE_SECURITY_MASTER",
):
    return SymbolIdentityEpoch(
        symbol=symbol,
        cik=cik,
        stable_security_identity=stable,
        effective_start=start,
        effective_end=end,
        source=source,
        evidence_type=LIFECYCLE_EVIDENCE,
        verified=True,
        observed_at=OBSERVED,
        evidence_reference=f"reviewed:{symbol}:{start}",
    )


def supplement():
    return {
        "provider": "ALPACA",
        "provider_action_id": ACTION_ID,
        "old_cusip": "123456789",
        "new_cusip": "987654321",
        "epochs": [
            epoch("OLD", start=date(2020, 1, 1), end=date(2025, 6, 18)).record(),
            epoch().record(),
        ],
    }


def identity():
    return {
        "symbol": "NEW",
        "cik": CIK,
        "share_class_figi": "CLASS1",
        "qualification_status": "ELIGIBLE_COMMON_STOCK",
        "observed_at": OBSERVED,
    }


def client(payload):
    return SimpleNamespace(name_changes=lambda *args, **kwargs: payload)


def test_raw_action_is_persisted_once_and_process_date_is_not_effective_date(db):
    first = action()
    assert db.ingest_security_corporate_actions([first]) == (1, [])
    second = {**first, "observed_at": "2026-10-15T00:00:00+00:00"}
    assert db.ingest_security_corporate_actions([second]) == (0, [])
    rows = db.security_corporate_actions(["OLD", "NEW"])
    assert len(rows) == 1 and rows[0]["provider_action_id"] == ACTION_ID
    assert rows[0]["process_date"] == "2025-06-20" and rows[0]["effective_date"] is None
    assert rows[0]["observed_at"] == OBSERVED
    assert db.symbol_identity_epochs(["NEW"])["NEW"] == []


def test_fully_reviewed_symbol_change_creates_verified_intervals_idempotently(db, monkeypatch):
    from trading_system.data import security_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "local_lifecycle_identities", lambda *args: {"NEW": identity()})
    first = sync_security_lifecycle(
        db, client({"name_changes": [event()]}), ["NEW"], START, END, verifications=[supplement()]
    )
    assert first["verified_epochs_created"] == 2
    assert first["symbols_resolved"] == ["NEW"] and first["identity_conflicts"] == 0
    stored = db.symbol_identity_epochs(["OLD", "NEW"])
    assert stored["NEW"][0]["effective_start"] == "2025-06-18"  # NOT process_date.
    assert stored["OLD"][0]["effective_end"] == "2025-06-18"
    assert all(r["verified"] for rows in stored.values() for r in rows)
    repeated = sync_security_lifecycle(
        db, client({"name_changes": [event()]}), ["NEW"], START, END, verifications=[supplement()]
    )
    assert repeated["raw_evidence_inserted"] == repeated["verified_epochs_created"] == 0
    assert db.symbol_identity_epochs(["OLD", "NEW"]) == stored


def test_alpaca_name_change_even_equal_cusips_never_auto_verifies(db):
    outcome = resolve_verified_symbol_identity_epochs(
        action(event(new_cusip="123456789")), identity()
    )
    assert not outcome["epochs"] and outcome["status"] == "UNVERIFIED_EVIDENCE"
    proof = supplement()
    proof["epochs"][0]["stable_security_identity"] = None
    outcome = resolve_verified_symbol_identity_epochs(action(), identity(), proof)
    assert outcome["status"] == "UNVERIFIED_EVIDENCE" and not outcome["epochs"]


@pytest.mark.parametrize(
    "field,value", [("cik", "0000000002"), ("stable_security_identity", "SHARE_CLASS_FIGI:OTHER")]
)
def test_conflicting_issuer_or_security_identity_fails_closed(field, value):
    proof = supplement()
    proof["epochs"][0][field] = value
    outcome = resolve_verified_symbol_identity_epochs(action(), identity(), proof)
    assert outcome["status"] == CONFLICT and outcome["epochs"] == []


def test_ticker_reuse_is_not_security_continuity(db):
    raw = action(event(old_symbol="XYZ", new_symbol="XYZ"))
    proof = supplement()
    proof["epochs"] = [
        epoch(
            "XYZ",
            cik="0000000002",
            stable="SHARE_CLASS_FIGI:OLD",
            start=date(2020, 1, 1),
            end=date(2021, 1, 1),
        ).record(),
        epoch("XYZ", start=date(2025, 6, 18)).record(),
    ]
    outcome = resolve_verified_symbol_identity_epochs(raw, {**identity(), "symbol": "XYZ"}, proof)
    assert outcome["epochs"] == [] and outcome["status"] == CONFLICT
    # Separate explicitly verified intervals can coexist; they are never joined.
    db.upsert_symbol_identity_epochs(
        [SymbolIdentityEpoch.from_record(r) for r in proof["epochs"]], protect_existing=True
    )
    assert len(db.symbol_identity_epochs(["XYZ"])["XYZ"]) == 2


def test_existing_verified_epoch_survives_weak_evidence_and_empty_responses(db):
    original = epoch(source="MANUAL_REVIEWED_SOURCE")
    assert db.upsert_symbol_identity_epochs([original]) == 1
    assert db.upsert_symbol_identity_epochs([]) == 0
    saved = db.symbol_identity_epochs(["NEW"])
    report = sync_security_lifecycle(db, client({"name_changes": [event()]}), ["NEW"], START, END)
    assert report["unverified_evidence"] == 1 and report["verified_epochs_created"] == 0
    empty = sync_security_lifecycle(db, client({}), ["NEW"], START, END)
    assert empty["symbols_unresolved"] == ["NEW"]
    assert empty["unresolved_sample"][0]["reason"] == "NO_RETURNED_EVENT_IS_NOT_IDENTITY_EVIDENCE"
    assert db.symbol_identity_epochs(["NEW"]) == saved


def test_epoch_conflict_rolls_back_whole_promotion_and_preserves_manual_record(db):
    original = epoch(cik="0000000002", stable="SHARE_CLASS_FIGI:OTHER", source="MANUAL_SOURCE")
    db.upsert_symbol_identity_epochs([original])
    saved = db.symbol_identity_epochs(["NEW", "OLD"])
    with pytest.raises(ValueError, match=CONFLICT):
        db.upsert_symbol_identity_epochs(
            [epoch("OLD", start=date(2020, 1, 1), end=date(2025, 6, 18)), epoch()],
        )
    assert db.symbol_identity_epochs(["NEW", "OLD"]) == saved


def test_changed_provider_event_conflicts_without_replacing_raw_evidence(db):
    original = action()
    db.ingest_security_corporate_actions([original])
    assert db.ingest_security_corporate_actions([action(event(new_cusip="111111111"))]) == (
        0,
        [original["event_key"]],
    )
    assert json.loads(db.security_corporate_actions(["NEW"])[0]["raw_payload"]) == event()


def test_current_identifier_unavailable_does_not_become_an_identity_conflict():
    outcome = resolve_verified_symbol_identity_epochs(
        action(), {**identity(), "share_class_figi": None}, supplement()
    )
    assert outcome["status"] == "UNVERIFIED_EVIDENCE" and outcome["epochs"] == []


def test_multi_hop_does_not_borrow_current_binding_from_final_symbol(db, monkeypatch):
    from trading_system.data import security_lifecycle as lifecycle

    first = event(old_symbol="AAA", new_symbol="BBB", process_date="2025-03-03")
    second = event(
        id="12345678-1234-5678-1234-567812345679",
        old_symbol="BBB",
        new_symbol="CCC",
    )
    proof = {
        **supplement(),
        "epochs": [
            epoch("AAA", start=date(2020, 1, 1), end=date(2025, 3, 1)).record(),
            epoch("BBB", start=date(2025, 3, 1), end=date(2025, 6, 18)).record(),
        ],
    }
    # Even reviewed first-hop intervals cannot borrow the current CCC binding.
    monkeypatch.setattr(
        lifecycle,
        "local_lifecycle_identities",
        lambda *args: {"CCC": {**identity(), "symbol": "CCC"}},
    )
    report = sync_security_lifecycle(
        db,
        client({"name_changes": [first, second]}),
        ["AAA", "BBB", "CCC"],
        START,
        END,
        verifications=[proof],
    )
    assert report["corporate_actions_received"] == report["raw_evidence_inserted"] == 2
    assert report["verified_epochs_created"] == report["identity_conflicts"] == 0
    assert report["unverified_evidence"] == 2
    assert report["symbols_resolved"] == []
    assert report["symbols_unresolved"] == ["AAA", "BBB", "CCC"]
    assert report["unresolved_sample"][0] == {
        "symbol": "AAA",
        "reason": "CURRENT_COMMON_STOCK_BINDING_UNAVAILABLE",
    }
    assert db.symbol_identity_epochs(["AAA", "BBB", "CCC"]) == {
        "AAA": [],
        "BBB": [],
        "CCC": [],
    }


def test_installed_sdk_request_and_synthetic_envelope_parsing_are_bounded(monkeypatch):
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {"corporate_actions": {"name_changes": [event()]}, "next_page_token": None}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    provider = AlpacaCorporateActionClient("test-key", "test-secret")
    try:
        result = provider.name_changes(["NEW", "OLD"], START, END, limit=10)
        assert result == {"name_changes": [event()]}
        method, url, kwargs = calls[0]
        assert method == "GET" and url == ENDPOINT
        assert kwargs["params"]["symbols"] == "NEW,OLD"
        assert kwargs["params"]["types"] == "name_change"
        assert kwargs["params"]["start"] == "2025-01-01"
        assert kwargs["params"]["end"] == "2025-08-01"
        assert kwargs["params"]["limit"] == 10
        assert kwargs["timeout"] == (5, 15) and not kwargs["allow_redirects"]
        assert len(calls) == 1
        with pytest.raises(ValueError, match="ONE_TO_SEVEN"):
            provider.name_changes([f"S{i}" for i in range(8)], START, END)
    finally:
        provider.close()


def test_limit_reached_stores_raw_but_cannot_promote(db, monkeypatch):
    from trading_system.data import security_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "local_lifecycle_identities", lambda *args: {"NEW": identity()})
    report = sync_security_lifecycle(
        db,
        client({"name_changes": [event()]}),
        ["NEW"],
        START,
        END,
        limit=1,
        verifications=[supplement()],
    )
    assert report["provider_result_limited"] and report["errors"] == 1
    assert report["raw_evidence_inserted"] == 1 and report["verified_epochs_created"] == 0
    assert report["symbols_unresolved"] == ["NEW"]


def test_failed_provider_request_does_not_create_database_and_hides_details(tmp_path):
    def failed(*args, **kwargs):
        raise ValueError("secret-must-not-be-printed")

    database = Database(tmp_path / "not-created.sqlite3")
    report = sync_security_lifecycle(
        database, SimpleNamespace(name_changes=failed), ["NEW"], START, END
    )
    assert not database.path.exists()
    assert report["errors"] == 1 and report["provider_query_status"] == "NOT_COMPLETED"
    assert "secret" not in json.dumps(report)


def test_cli_dispatch_is_explicit_and_never_starts_bar_or_pair_work(db, monkeypatch, capsys):
    from trading_system.data import alpaca_corporate_actions as adapter

    settings = SimpleNamespace(
        strategy=SimpleNamespace(storage=SimpleNamespace(database_path=db.path)),
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
    )
    fake = client({"name_changes": [event()]})
    fake.close = lambda: None
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    monkeypatch.setattr(adapter, "AlpacaCorporateActionClient", lambda *args: fake)
    from trading_system.backtest import pairs_stat_arb_v1_research

    monkeypatch.setattr(pairs_stat_arb_v1_research, "run_pairs_stat_arb_v1", forbidden)
    assert (
        cli.main(
            [
                "sync-security-lifecycle",
                "--symbols",
                "NEW",
                "--start",
                str(START),
                "--end",
                str(END),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["raw_evidence_inserted"] == report["unverified_evidence"] == 1
    assert report["verified_epochs_created"] == 0 and report["symbols_unresolved"] == ["NEW"]


def seed_current_reference(db):
    from test_massive_reference import CATALOG, ticker

    from trading_system.data.security_reference import map_record, normalize_catalog
    from trading_system.models.fundamentals import CompanyIdentity

    db.upsert_company(CompanyIdentity(symbol="NEW", name="Test issuer", cik=CIK, sic="3571"))
    raw = {**ticker("NEW", cik="1"), "share_class_figi": "CLASS1"}
    record = map_record(raw, normalize_catalog(CATALOG), OBSERVED)
    db.promote_security_reference([record], {"snapshot_id": "fixture", "sync_success": True})
    db.set_sync_value("sec_reference", "ticker_to_cik", {"NEW": "1"})


def test_cli_reviewed_file_uses_real_local_reference_binding(db, tmp_path, monkeypatch, capsys):
    from trading_system.data import alpaca_corporate_actions as adapter

    seed_current_reference(db)
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps([supplement()]), encoding="utf-8")
    settings = SimpleNamespace(
        strategy=SimpleNamespace(storage=SimpleNamespace(database_path=db.path)),
        alpaca_api_key="test-key", alpaca_secret_key="test-secret",
    )
    fake = client({"name_changes": [event()]})
    fake.close = lambda: None
    monkeypatch.setattr(cli, "load_settings", lambda *args: settings)
    monkeypatch.setattr(adapter, "AlpacaCorporateActionClient", lambda *args: fake)
    assert cli.main([
        "sync-security-lifecycle", "--symbols", "NEW", "--start", str(START), "--end", str(END),
        "--verified-evidence", str(path),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified_epochs_created"] == 2 and report["symbols_resolved"] == ["NEW"]


def test_current_sec_conflict_blocks_reviewed_promotion(db):
    seed_current_reference(db)
    db.set_sync_value("sec_reference", "ticker_to_cik", {"NEW": "2"})
    report = sync_security_lifecycle(
        db, client({"name_changes": [event()]}), ["NEW"], START, END,
        verifications=[supplement()],
    )
    assert report["identity_conflicts"] == 1 and report["verified_epochs_created"] == 0
    assert report["raw_evidence_inserted"] == 1 and report["symbols_unresolved"] == ["NEW"]
    assert db.symbol_identity_epochs(["NEW", "OLD"]) == {"NEW": [], "OLD": []}
