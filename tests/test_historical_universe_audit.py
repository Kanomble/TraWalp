import copy
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_system.backtest import historical_universe_audit as audit
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.models.fundamentals import FundamentalFact
from trading_system.models.market_data import DailyBar


def champion_config():
    return StrategyConfig.model_validate(audit._read_yaml(audit.ROOT / "config/strategy.yaml"))


def test_current_and_2022_sets_and_strict_initial_warmup():
    bars = {
        "A": {"daily_bars_2022": 251, "bars_before_2022": 300},
        "B": {"daily_bars_2022": 251, "bars_before_2022": 299},
        "C": {"daily_bars_2022": 0, "bars_before_2022": 500},
    }
    facts = {"A": {"pit_facts_2022": 1}, "B": {"pit_facts_2022": 0}}
    sets = audit.universe_sets({"A", "C"}, bars, facts, {"B"})
    assert sets["DAILY_2022_UNIVERSE"] == {"A", "B"}
    assert sets["MISSING_FROM_CURRENT"] == {"B"}
    assert sets["WARMUP_READY_2022"] == {"A"}
    assert sets["PIT_SEC_2022"] == {"A"}
    assert sets["HISTORICAL_IDENTITY_EVIDENCE"] == {"B"}


@pytest.mark.parametrize(
    "current,historical,expected",
    [
        (["1", "0000000001"], ["1"], "CIK_STABLE"),
        (["2"], ["1"], "CIK_CHANGED"),
        (["1", "2"], ["1"], "CIK_CONFLICT"),
        (["1"], ["2", "3"], "CIK_CONFLICT"),
        ([], [], "CIK_MISSING"),
        (["1"], [], "CIK_UNRESOLVED"),
        (["invalid", "0"], [], "CIK_MISSING"),
    ],
)
def test_cik_classification(current, historical, expected):
    assert audit.cik_classification(current, historical) == expected


@pytest.mark.parametrize(
    "current,historical,expected",
    [
        (["A"], ["A"], "FIGI_STABLE"),
        (["A"], ["B"], "FIGI_CHANGED"),
        ([], [], "FIGI_MISSING"),
        (["A"], [], "FIGI_UNRESOLVED"),
        (["A", "B"], ["A"], "FIGI_UNRESOLVED"),
    ],
)
def test_figi_classification(current, historical, expected):
    assert audit.figi_classification(current, historical) == expected


def test_corporate_actions_map_both_symbols_without_invented_effective_date():
    action = {
        "old_symbol": "OLD",
        "new_symbol": "NEW",
        "process_date": "2022-08-01",
        "effective_date": None,
        "provider_action_id": "id",
        "old_cusip": "x",
        "new_cusip": "x",
    }
    result = audit.action_map([action])
    assert result == {"OLD": [action], "NEW": [action]}
    assert result["OLD"][0]["effective_date"] is None


@pytest.mark.parametrize(
    "name,ref,expected,plausible",
    [
        ("Test ETF", {}, "ETF_OR_ETP", False),
        ("Test", {"security_type": "FUND"}, "ETF_OR_ETP", False),
        ("Test Common Stock", {}, "POSSIBLE_HISTORICAL_COMMON_STOCK", True),
        ("Test Warrants", {}, "KNOWN_NON_COMMON_SECURITY", False),
        ("Test Preferred Stock", {}, "KNOWN_NON_COMMON_SECURITY", False),
        ("Test American Depositary Shares", {}, "ADR_OR_OTHER_NON_COMMON", False),
        ("Test", {}, "INSUFFICIENT_IDENTITY_EVIDENCE", False),
    ],
)
def test_explicit_security_type_evidence_not_ticker_suffix(name, ref, expected, plausible):
    scope = audit.security_scope({"name": name}, ref, [])
    assert scope["class"] == expected
    assert scope["common_plausible"] == plausible
    assert (
        audit.security_scope({"name": "TESTW"}, {}, [])["class"] == "INSUFFICIENT_IDENTITY_EVIDENCE"
    )


def fixture_data():
    return {
        "companies": {"OLD": {"cik": "0000000002"}},
        "assets": {"OLD": {"tradable": 1}},
        "reference": {
            "OLD": {"cik": "0000000002", "composite_figi": "NEW", "share_class_figi": "NEWCLASS"}
        },
        "facts": {},
        "cached_sec": {"OLD": "0000000002"},
        "epochs": [],
        "current_conflicts": set(),
        "bars": {"OLD": {"daily_bars_2022": 251}},
    }


def test_evidence_aggregation_cik_and_two_figi_fields_separate():
    data = fixture_data()
    observations = {
        "OLD": [
            {
                "operation": "details",
                "as_of": "2022-06-15",
                "http_status": 200,
                "results": {
                    "cik": "0000000001",
                    "type": "CS",
                    "composite_figi": "HIST",
                    "share_class_figi": "HISTCLASS",
                },
            }
        ]
    }
    free = {
        "OLD": {
            "free_sec_different_issuers_proven": True,
            "sec": [
                {
                    "issuer_binding_confirmed": True,
                    "trading_symbols": ["OLD"],
                    "filing_date": "2022-05-01",
                    "cik_context": "0000000001",
                }
            ],
        }
    }
    result = audit.identity_record("OLD", data, observations, free, {})
    assert result["identity_class"] == "TICKER_REUSE"
    assert result["cik_class"] == "CIK_CHANGED"
    assert result["figi_classes"] == {
        "share_class_figi": "FIGI_CHANGED",
        "composite_figi": "FIGI_CHANGED",
    }
    assert result["evidence_strength"] == "STRONG"
    assert result["identity_review_status"] == "REQUIRES_IDENTITY_REVIEW"
    assert result["verified_existing_epoch"] is None


def test_intermediate_dated_snapshot_is_not_misclassified_as_current_conflict():
    data = fixture_data()
    observations = {
        "OLD": [
            {
                "operation": "details",
                "as_of": day,
                "http_status": 200,
                "results": {"cik": cik, "type": "CS", "composite_figi": figi},
            }
            for day, cik, figi in [
                ("2022-06-15", "0000000001", "OLD"),
                ("2023-03-20", "0000000001", "OLD"),
                ("2026-10-14", "0000000002", "NEW"),
            ]
        ]
    }
    result = audit.identity_record("OLD", data, observations, {}, {})
    assert result["cik_class"] == "CIK_CHANGED"
    assert result["figi_classes"]["composite_figi"] == "FIGI_CHANGED"
    assert result["current_details_as_of"] == "2026-10-14"
    assert result["intermediate_details"][0]["as_of"] == "2023-03-20"


def test_one_snapshot_and_404_never_form_epoch_or_delisting():
    data = fixture_data()
    data["assets"]["OLD"]["tradable"] = 0
    observations = {
        "OLD": [
            {
                "operation": "details",
                "as_of": "2022-06-15",
                "http_status": 200,
                "results": {"cik": "0000000002", "type": "CS"},
            },
            {"operation": "details", "as_of": "2024-01-02", "http_status": 404},
        ]
    }
    result = audit.identity_record("OLD", data, observations, {}, {})
    assert result["verified_existing_epoch"] is None
    assert not any(f["class"] == "DELISTED_SECURITY" for f in result["findings"])
    assert result["evidence_strength"] == "MODERATE"
    assert not audit.proxy_sessions(result, {"warmup": {date(2022, 6, 15)}})


def test_current_inactive_bit_is_not_historical_identity_evidence():
    data = fixture_data()
    data["assets"]["OLD"] = {"tradable": 0, "name": "Test Common Stock"}
    result = audit.identity_record("OLD", data, {}, {}, {})
    assert result["identity_class"] == "CURRENTLY_INACTIVE"
    assert not result["historical_identity_evidence"]
    assert result["evidence_strength"] == "UNRESOLVED"


def test_existing_verified_interval_is_end_exclusive_and_needs_warmup():
    identity = {
        "verified_existing_epoch": {"effective_start": "2022-01-03", "effective_end": "2022-01-05"},
        "identity_conflict": False,
        "scope": {"common_plausible": True},
    }
    gates = {"warmup": {date(2022, 1, 4), date(2022, 1, 5)}}
    assert audit.proxy_sessions(identity, gates) == {date(2022, 1, 4)}
    assert gates["warmup"] == {date(2022, 1, 4), date(2022, 1, 5)}


def test_decision_uses_persistence_and_candidate_scale_not_returns():
    rows = [candidate("AAA")]
    persistent = [
        {"session": f"2022-{m:02}-03", "potentially_F_relevant_missing_symbols": 2}
        for m in range(1, 13)
    ]
    result = audit.decisions(rows, persistent, 1)
    assert result == (
        "HISTORICAL_UNIVERSE_BIAS_MATERIAL",
        "2022_F_RESULT_REQUIRES_UNIVERSE_RECONSTRUCTION",
        "MASSIVE_HISTORICAL_IDENTITY_PILOT_JUSTIFIED",
    )
    assert (
        audit.decisions(rows, persistent[:1], 1)[0]
        == "HISTORICAL_UNIVERSE_BIAS_PRESENT_BUT_LIMITED"
    )
    assert audit.decisions([], persistent, 1)[0] == "HISTORICAL_UNIVERSE_BIAS_LOW"


def gate_fixture():
    return {
        k: {date(2022, 1, 3)}
        for k in (
            "warmup",
            "price",
            "adv",
            "cheap",
            "market_cap_input",
            "market_cap",
            "pit",
            "joint",
        )
    }


def test_relevance_high_still_fails_closed_for_proxy():
    identity = {
        "findings": [],
        "local_cik": "0000000001",
        "identity_conflict": False,
        "late_first_local_filing": False,
        "scope": {"common_plausible": True},
        "verified_existing_epoch": None,
    }
    args = (
        {"bars_before_2022": 300},
        {"pit_facts_2022": 1},
        gate_fixture(),
        champion_config(),
        {"sic": "3571"},
    )
    assert audit.relevance(identity, *args)[0] == audit.HIGH
    assert not audit.proxy_sessions(identity, gate_fixture())
    identity["identity_conflict"] = True
    assert audit.relevance(identity, *args)[0] == audit.UNKNOWN
    identity["findings"] = [{"class": "ETF_OR_ETP"}]
    assert audit.relevance(identity, *args)[0] == audit.UNKNOWN
    identity["identity_conflict"] = False
    assert audit.relevance(identity, *args)[0] == audit.LOW


def test_stored_openfigi_ticker_mapping_is_current_evidence_only():
    data = fixture_data()
    data["reference"] = {}
    free = {
        "OLD": {
            "openfigi": [
                {
                    "query": {"idType": "TICKER", "idValue": "OLD"},
                    "mappings": [
                        {
                            "ticker": "OLD",
                            "compositeFIGI": "CURRENT",
                            "shareClassFIGI": "CURRENTCLASS",
                        }
                    ],
                },
                {
                    "query": {"idType": "ID_CUSIP", "idValue": "old"},
                    "mappings": [{"ticker": "OTHER", "compositeFIGI": "DIFFERENT"}],
                },
            ]
        }
    }
    result = audit.identity_record("OLD", data, {}, free, {})
    assert result["current_figis"]["composite_figi"] == ["CURRENT"]
    assert result["historical_figis"]["composite_figi"] == []
    assert result["figi_classes"]["composite_figi"] == "FIGI_UNRESOLVED"
    assert result["verified_existing_epoch"] is None


def test_relevance_medium_for_later_warmup_low_for_no_ready_session():
    identity = {
        "findings": [],
        "local_cik": "0000000001",
        "identity_conflict": False,
        "late_first_local_filing": False,
        "scope": {"common_plausible": True},
    }
    gates = gate_fixture()
    args = ({"bars_before_2022": 299}, {"pit_facts_2022": 1}, gates, champion_config(), {})
    assert audit.relevance(identity, *args)[0] == audit.MEDIUM
    gates["warmup"] = set()
    assert audit.relevance(identity, *args)[0] == audit.LOW


def test_late_first_filing_is_not_proof_of_identity_conflict_or_backfill_cause():
    identity = {
        "findings": [],
        "local_cik": "0000000001",
        "identity_conflict": False,
        "late_first_local_filing": True,
        "scope": {"common_plausible": True},
        "verified_existing_epoch": None,
    }
    gates = gate_fixture()
    gates["market_cap_input"] = gates["market_cap"] = gates["pit"] = gates["joint"] = set()
    result = audit.relevance(
        identity, {"bars_before_2022": 300}, {"pit_facts_2022": 0}, gates, champion_config(), {}
    )
    assert result[0] == audit.MEDIUM
    assert not audit.proxy_sessions(identity, gates)
    assert not identity["identity_conflict"]


def test_market_gates_existing_ADV_formula_filed_cutoff_and_no_future_bars(tmp_path):
    db = Database(tmp_path / "fixture.sqlite3")
    db.initialize()
    first = datetime(2020, 11, 1, tzinfo=UTC)
    db.upsert_bars(
        [
            DailyBar(
                symbol="AAA",
                timestamp=first + timedelta(days=i),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=2_000_000,
            )
            for i in range(430)
        ]
    )
    db.upsert_facts(
        [
            FundamentalFact(
                cik="0000000001",
                symbol="AAA",
                metric="shares_outstanding",
                tag="Shares",
                value=Decimal("20000000"),
                unit="shares",
                period_end=date(2021, 12, 31),
                filed=date(2023, 1, 3),
                form="10-K",
            )
        ]
    )
    readonly = audit.prior.AuditDatabase(db.path)
    sessions = [date(2022, 1, 3), date(2022, 1, 4)]
    with audit.prior.local_guard():
        gates = audit.market_gates(
            readonly,
            champion_config(),
            ["AAA"],
            sessions,
            {"AAA": {"first_pit_filed": "2021-01-01"}},
        )["AAA"]
    assert gates["warmup"] == gates["price"] == gates["adv"] == set(sessions)
    assert not gates["market_cap_input"]
    assert not gates["joint"]


def candidate(symbol, **updates):
    return {
        "symbol": symbol,
        "f_relevance": audit.HIGH,
        "warmup_ready_before_2022": True,
        "pit_facts_2022": 10,
        "daily_bars_2022": 251,
        "evidence_strength": "UNRESOLVED",
        "requires_external_identity_probe": True,
        **updates,
    }


def test_pilot_order_and_limit_ignores_all_outcomes():
    rows = [candidate(f"S{i:02}") for i in range(30)]
    rows += [
        candidate("FIRST", evidence_strength="MODERATE"),
        candidate("ETF", f_relevance=audit.LOW),
        candidate("PROVEN", evidence_strength="STRONG"),
    ]
    expected = [r["symbol"] for r in audit.pilot_order(rows)]
    assert len(expected) == 20
    assert expected[0] == "FIRST"
    assert "ETF" not in expected and "PROVEN" not in expected
    changed = copy.deepcopy(list(reversed(rows)))
    for i, row in enumerate(changed):
        row.update(
            future_return=i * 999,
            return_2022=-i,
            known_winner=True,
            F_trade_outcomes=[1000],
            C1_performance=i,
        )
    assert [r["symbol"] for r in audit.pilot_order(changed)] == expected


def test_gap_aggregation_primary_classes_are_exclusive():
    rows = [
        {"identity_class": "CURRENTLY_INACTIVE", "daily_bars_2022": 251, "f_relevance": audit.HIGH},
        {"identity_class": "ETF_OR_ETP", "daily_bars_2022": 50, "f_relevance": audit.LOW},
    ]
    result = audit.aggregate_gaps(rows)
    assert sum(r["symbols"] for r in result) == 2
    assert sum(r["symbol_sessions"] for r in result) == 301
    assert result[0]["high_relevance"] == 1


def test_session_aggregation_no_new_universe_membership():
    session = date(2022, 1, 3)
    data = {
        "bars": {"AAA": {"bars_before_2022": 299}, "OLD": {"bars_before_2022": 300}},
        "dates": {"AAA": [session], "OLD": [session]},
    }
    sets = {
        "DAILY_2022_UNIVERSE": {"AAA", "OLD"},
        "CURRENT_SOURCE_UNIVERSE": {"AAA"},
        "MISSING_FROM_CURRENT": {"OLD"},
    }
    identity = {"OLD": {"verified_existing_epoch": None}}
    rows = [{"symbol": "OLD", "f_relevance": audit.HIGH}]
    result = audit.session_bias([session], sets, data, rows, {"OLD": gate_fixture()}, identity)[0]
    assert result["current_source_symbols"] == result["warmup_ready_current"] == 1
    assert result["historical_proxy_symbols"] == 0
    assert result["potentially_F_relevant_missing_symbols"] == 1


def test_offline_guard_database_read_only_no_client_construction_no_network(tmp_path):
    from trading_system.data.alpaca_client import AlpacaDataClient
    from trading_system.data.massive_identity_research import MassiveIdentityResearchClient
    from trading_system.data.sec_client import SecClient

    db = Database(tmp_path / "guard.sqlite3")
    db.initialize()
    before = audit.prior.digest(db.path)
    with audit.prior.local_guard() as safety:
        with audit.prior.AuditDatabase(db.path).connect() as connection:
            assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM assets")
        with pytest.raises(RuntimeError, match="INITIALIZE_FORBIDDEN"):
            audit.prior.AuditDatabase(db.path).initialize()
        for cls in (AlpacaDataClient, SecClient, MassiveIdentityResearchClient):
            with pytest.raises(RuntimeError, match="PROVIDER_CONSTRUCTION_FORBIDDEN"):
                cls("unused")
        import requests

        with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
            requests.get("https://example.invalid")
    assert before == audit.prior.digest(db.path)
    assert safety["database_writes"] == safety["network_calls"] == 0
    assert safety["blocked_provider_constructions"] == 3


def test_production_reader_always_uses_mode_ro(monkeypatch):
    observed = []
    connection = SimpleNamespace(row_factory=None, close=lambda: None)

    def connect(path, **kwargs):
        observed.append((path, kwargs))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with audit.prior.AuditDatabase("data/trading_system.sqlite3").connect():
        pass
    assert "mode=ro" in observed[0][0]
    assert observed[0][1]["uri"] is True
