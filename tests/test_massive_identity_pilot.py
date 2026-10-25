"""Synthetic transport, clocks and temporary DBs only; no real credentials."""

import ast
import inspect
import sqlite3
from datetime import date, timedelta

import pytest
import requests
from test_massive_reference import CATALOG, DUMMY_KEY, response, ticker
from test_massive_reference import clock as clock
from test_massive_reference import offline as offline

from trading_system.backtest import massive_identity_pilot as pilot
from trading_system.data import massive_reference


@pytest.fixture
def plan():
    days = [date(2022, 1, 3) + timedelta(days=n) for n in range(39)]
    return pilot.make_plan({s: days for s in pilot.SYMBOLS})


@pytest.fixture
def baseline():
    return dict(
        symbol="QRVO",
        role="PRIMARY_CASE",
        local_cik="1",
        cached_sec_cik="1",
        current_massive_cik=None,
        pit_facts_2022=12,
        corporate_actions=[],
    )


@pytest.fixture
def transport(monkeypatch, clock):
    monkeypatch.setattr(pilot.research, "_REQUEST_LIMITER", massive_reference._REQUEST_LIMITER)
    clients = []

    def create(responses):
        pending, calls = iter(responses), []

        def request(session, method, url, **kwargs):
            calls.append(url)
            item = next(pending)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(requests.Session, "request", request)
        client = pilot.research.MassiveIdentityResearchClient(DUMMY_KEY)
        clients.append(client)
        return client, calls

    yield create
    for client in clients:
        client.close()


def observations(plan, symbol="QRVO", cik="1"):
    return [
        {
            **r,
            "http_status": 200,
            "requests_attempted": 1,
            "results": ticker(symbol, cik=cik) if r["operation"] == "details" else {"events": []},
        }
        for r in plan
        if r["symbol"] == symbol
    ]


def test_exact_five_manifest_first_middle_last_and_events(plan):
    assert tuple(dict.fromkeys(r["symbol"] for r in plan)) == pilot.SYMBOLS
    assert len(plan) == 16
    q = [r["probe_date"] for r in plan if r["symbol"] == "QRVO" and r["operation"] == "details"]
    assert q == ["2022-01-03", "2022-01-22", "2022-02-10"]
    assert all(
        sum(r["symbol"] == s and r["operation"] == "events" for r in plan) == 1
        for s in pilot.SYMBOLS
    )


def test_sparse_sessions_not_calendar_interpolation():
    days = [date(2022, 1, 3), date(2022, 2, 18), date(2022, 3, 18)]
    assert pilot.midpoint_probes(days[::-1] + days) == days
    with pytest.raises(ValueError):
        pilot.make_plan({"QRVO": days})


@pytest.mark.parametrize("status", [401, 402, 403])
def test_access_denial_stops_all_subsequent_requests(transport, plan, status):
    client, calls = transport([response(status)])
    guard = pilot.TransportBudget(plan)
    rows = pilot.run_probes(client, plan, guard)
    assert len(calls) == len(guard.attempts) == 1
    assert guard.access_blocked and rows[0]["http_status"] == status
    assert all(r.get("not_attempted") for r in rows[1:])


def test_404_is_point_not_found_and_does_not_stop_other_queries(transport, plan, baseline):
    client, calls = transport([response(404), response(results=ticker("QRVO"))])
    rows = pilot.run_probes(client, plan[:2], pilot.TransportBudget(plan[:2]))
    exported = pilot.probe_rows(rows, {"QRVO": baseline}, CATALOG)
    assert len(calls) == 2
    assert exported[0]["identity_classification"] == "MASSIVE_POINT_NOT_FOUND"
    assert "SECURITY_DID_NOT_EXIST" not in str(exported)


def test_budget_counts_retries_through_existing_limiter(transport, plan, clock):
    client, calls = transport([response(500), response(429), response(results=ticker("QRVO"))])
    guard = pilot.TransportBudget(plan, maximum=2)
    rows = pilot.run_probes(client, plan, guard)
    assert len(calls) == len(guard.attempts) == 2
    assert client.diagnostics["research_http_attempts"] == 2
    assert client.diagnostics["research_retries"] == 1
    assert rows[0]["error"] == "LOCAL_SAFETY_OR_ACCESS_BLOCK"
    with pytest.raises(ValueError):
        pilot.TransportBudget(plan, maximum=26)


def test_absolute_25_attempt_budget_including_retries(transport, plan):
    client, calls = transport([response(500) for _ in range(25)])
    guard = pilot.TransportBudget(plan)
    rows = pilot.run_probes(client, plan, guard)
    assert len(calls) == len(guard.attempts) == 25
    assert client.diagnostics["research_http_attempts"] == 25
    assert rows[-1].get("not_attempted")


def test_full_plan_uses_shared_limiter_and_only_two_endpoints(transport, plan, clock):
    client, calls = transport(
        [
            response(results={"events": []})
            if r["operation"] == "events"
            else response(results=ticker(r["symbol"]))
            for r in plan
        ]
    )
    guard = pilot.TransportBudget(plan)
    rows = pilot.run_probes(client, plan, guard)
    assert len(calls) == len(rows) == 16
    assert sum(clock.sleeps) >= 3 * 60.5
    assert all(url.startswith("https://api.massive.com/") for url in calls)
    assert all("?date=" in url or url.endswith("/events?types=ticker_change") for url in calls)
    assert DUMMY_KEY not in str(rows) + str(guard.attempts)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.massive.com/v3/reference/tickers",
        "https://api.massive.com/v3/reference/tickers/OTHER?date=2022-01-03",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK1.json",
        "https://api.alpaca.markets/v2/assets",
    ],
)
def test_unsupported_endpoint_and_other_provider_blocked(transport, plan, url):
    client, calls = transport([])
    guard = pilot.TransportBudget(plan)
    with guard.guard(client), pytest.raises(pilot.PilotStop):
        client._session.get(url, allow_redirects=False)
    assert calls == [] and guard.attempts == []


def test_database_connection_forbidden_during_live_phase(transport, plan, tmp_path):
    client, calls = transport([])
    with pilot.TransportBudget(plan).guard(client), pytest.raises(pilot.PilotStop):
        sqlite3.connect(tmp_path / "forbidden.sqlite")
    assert not (tmp_path / "forbidden.sqlite").exists() and not calls


def test_local_read_only_guard_blocks_mutation_and_initialization(tmp_path):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE example (value TEXT)")
    before = pilot.prior.digest(path)
    with pilot.prior.local_guard():
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            assert con.execute("SELECT COUNT(*) FROM example").fetchone()[0] == 0
            with pytest.raises(sqlite3.DatabaseError):
                con.execute("INSERT INTO example VALUES ('x')")
        with pytest.raises(RuntimeError):
            pilot.prior.AuditDatabase(path).initialize()
    assert before == pilot.prior.digest(path)


@pytest.mark.parametrize(
    "current,old,expected",
    [
        (["1", "0000000001"], ["1"], "CIK_MATCH"),
        (["1"], ["2"], "CIK_CHANGED"),
        (["1", "2"], ["1"], "CIK_CONFLICT"),
        (["1"], ["1", "2"], "CIK_CONFLICT"),
        (["1"], [], "CIK_MISSING"),
        ([], ["1"], "CIK_UNRESOLVED"),
    ],
)
def test_cik_comparison(current, old, expected):
    assert pilot.compare_cik(current, old) == expected


@pytest.mark.parametrize(
    "current,old,expected",
    [
        (["A"], ["A"], "FIGI_MATCH"),
        (["A"], ["B"], "FIGI_CHANGED"),
        ([], ["A"], "FIGI_UNRESOLVED"),
        (["A"], [], "FIGI_MISSING"),
        ([], ["A", "B"], "FIGI_UNRESOLVED"),
    ],
)
def test_figi_comparison(current, old, expected):
    assert pilot.compare_figi(current, old) == expected


@pytest.mark.parametrize(
    "code,expected",
    [
        ("CS", "COMMON_STOCK"),
        ("ADRC", "ADR"),
        ("ETF", "ETF/ETP"),
        ("PFD", "PREFERRED"),
        ("WARRANT", "WARRANT"),
        ("UNIT", "UNIT"),
        ("RIGHT", "RIGHT"),
        ("OTHER", "OTHER"),
        ("FUTURE_CODE", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_existing_catalog_security_mapping_only(code, expected):
    assert pilot.security_type(code, CATALOG) == expected
    assert pilot.security_type("CS", []) == "UNKNOWN"


def test_qrvo_replay_requires_all_three_real_points_and_complementary_evidence(plan, baseline):
    rows = observations(plan)
    result = pilot.analyze_symbol("QRVO", baseline, rows, CATALOG)
    assert result["identity_result"] == "QRVO_IDENTITY_VERIFIED_FOR_F_REPLAY"
    assert result["evidence_strength"] == "STRONG"
    incomplete = pilot.analyze_symbol("QRVO", baseline, rows[:1], CATALOG)
    assert incomplete["identity_result"] == "QRVO_IDENTITY_PLAUSIBLE_BUT_INCOMPLETE"
    assert incomplete["evidence_strength"] == "MODERATE"
    missing = pilot.analyze_symbol("QRVO", baseline, [], CATALOG)
    assert missing["identity_result"] == "QRVO_IDENTITY_UNRESOLVED"


def test_ticker_reuse_mismatch_and_no_false_chain(plan, baseline):
    rows = observations(plan, cik="2")
    result = pilot.analyze_symbol("QRVO", baseline, rows, CATALOG)
    assert result["ticker_reuse_evidence"] and result["identity_result"] == "QRVO_IDENTITY_CONFLICT"
    assert result["evidence_strength"] == "MODERATE"
    rows[-1]["results"] = {
        "events": [
            {"date": "2022-02-02", "type": "ticker_change", "ticker_change": {"ticker": "QRVO"}}
        ]
    }
    result = pilot.analyze_symbol("QRVO", baseline, rows, CATALOG)
    assert not result["ticker_change_evidence"]


def test_historical_figi_switch_is_conflict_without_current_reference(plan, baseline):
    rows = observations(plan)
    rows[1]["results"]["share_class_figi"] = "OTHER_CLASS"
    result = pilot.analyze_symbol("QRVO", baseline, rows, CATALOG)
    assert result["ticker_reuse_evidence"]
    assert result["identity_result"] == "QRVO_IDENTITY_CONFLICT"


def test_events_and_local_corporate_action_require_agreement(plan, baseline):
    rows = observations(plan, symbol="BG")
    baseline = {
        **baseline,
        "role": "CONTROL_OR_IDENTITY_REVIEW_CASE",
        "local_cik": "1",
        "corporate_actions": [
            {"old_symbol": "OLD", "new_symbol": "BG", "process_date": "2022-02-02"}
        ],
    }
    rows[-1]["results"] = {
        "events": [
            {"date": "2022-02-02", "type": "ticker_change", "ticker_change": {"ticker": "BG"}}
        ]
    }
    result = pilot.analyze_symbol("BG", baseline, rows, CATALOG)
    assert result["ticker_change_evidence"]
    assert result["identity_result"] == "IDENTITY_CHANGE_CONFIRMED"


def test_no_score_screen_backtest_sync_or_other_client_execution():
    tree = ast.parse(inspect.getsource(pilot))
    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
    }
    assert not calls & {
        "run_champion_backtest",
        "screen_champion",
        "champion_ranking",
        "Screener",
        "BacktestEngine",
        "sync",
        "sync_sec_full",
        "initialize",
        "SecClient",
        "AlpacaClient",
        "MassiveReferenceClient",
    }
    assert "MassiveIdentityResearchClient" in calls
