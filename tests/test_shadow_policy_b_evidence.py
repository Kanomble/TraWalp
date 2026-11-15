"""Policy B evidence only: in-memory journals, synthetic SIP inputs, no runner switch."""

import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from shadow_fixtures import history_evidence, incomplete_candidate, rejected_candidate
from test_champion_consolidation import bar, record
from test_champion_consolidation import offline as offline
from test_shadow_execution import Screens
from test_shadow_execution import config as config

from trading_system.data.forward_market import CONTRACT
from trading_system.shadow.contracts import (
    FULL,
    MANAGEMENT_WITH_BLOCKED_SIGNALS,
    STRICT_CONTRACT_VERSION,
    ShadowError,
    digest,
    fingerprints,
    normalized,
)
from trading_system.shadow.execution import ChampionSession
from trading_system.shadow.journal import DDL, Journal
from trading_system.shadow.modes import ECONOMIC, evidence_manifest, session_admission_manifest
from trading_system.shadow.readiness import evaluate_shadow_input_readiness
from trading_system.shadow.state import initial_state, portfolio


class Inputs:
    def __init__(self, journal, connection, day):
        self.day = day
        self.now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=22)
        self.provenance = {
            **CONTRACT,
            "market_inputs": {},
            "fundamental_inputs": {},
            "identity_inputs": {},
        }
        self.manifest_sink = lambda m: journal.store_input_manifest(connection, m)

    def sync_values(self, source):
        return {
            k: {"status": "success", "errors": 0, "last_success_at": self.now.isoformat()}
            for k in ("asset_universe", "sec", "forward_sip")
        }

    def history_coverage(self, symbols, session, sessions):
        return history_evidence(symbols, session, count=sessions)

    def bars_available_as_of(self, *args, **kwargs):
        return []

    def bars_on_session(self, symbols, session):
        bars = {s: bar(s, session) for s in symbols}
        self.provenance["market_inputs"][
            "execution|" + session.isoformat() + "|" + ",".join(sorted(symbols))
        ] = [
            normalized(
                {
                    **b.model_dump(),
                    "session": session,
                    "provider": "alpaca",
                    "feed": "sip",
                    "adjustment": "all",
                    "retrieved_at": self.now,
                    "sync_reference": "synthetic",
                }
            )
            for b in bars.values()
        ]
        return bars


@pytest.fixture
def memory(config, request):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    journal = Journal("unused-policy-b-fixture-path")
    journal.store_input_manifest(c, {})
    fp = fingerprints(config)
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    inception = normalized(
        {
            **fp,
            "journal_id": "memory",
            "created_at_utc": now,
            "initial_capital": 100000.0,
            "shadow_mode": ECONOMIC,
            "universe_policy": "U0_CURRENT",
            "source_fingerprint": digest(fp["engine_files"]),
            "latest_completed_official_session_at_inception": date(2023, 12, 29),
        }
    )
    if getattr(request, "param", False):
        for key in ("shadow_mode", "universe_policy", "source_fingerprint"):
            inception.pop(key)
    journal.append(c, "SHADOW_INITIALIZED", inception, now=now)
    yield journal, c, config, fp
    c.close()


def append_session(
    memory,
    day=date(2024, 1, 2),
    *,
    version="new",
    unresolved=True,
    purpose=MANAGEMENT_WITH_BLOCKED_SIGNALS,
    mutate=None,
    signal=False,
):
    journal, c, config, fp = memory
    state = journal.verify(c).state
    inputs = Inputs(journal, c, day)
    symbols = (
        {state["position"]["symbol"]}
        if state["position"]
        else {state["pending"]["order"]["record"]["symbol"]}
        if state["pending"]
        else set()
    )
    bars = inputs.bars_on_session(symbols, day)
    rows = (
        (record("AAA"), incomplete_candidate())
        if unresolved
        else ((record("AAA"),) if signal else (rejected_candidate(),))
    )
    ready = evaluate_shadow_input_readiness(
        inputs, config, now=inputs.now, session=day, screen_source=Screens({day: rows})
    )
    evidence = evidence_manifest(ready, inputs.manifest_sink(inputs.provenance))
    admission = (
        session_admission_manifest(
            evidence, purpose=purpose, state=state, fingerprints_digest=digest(fp), bars=bars
        )
        if version == "new"
        else evidence
    )
    events = []

    def emit(kind, payload):
        events.append([kind, normalized(payload)])

    emit(
        "DATA_PREFLIGHT_PASSED",
        {
            "mode": "STRICT",
            "contract_version": STRICT_CONTRACT_VERSION,
            "observed_bars": bars,
            "position_management_ohlc_digest": digest(bars),
        },
    )
    after = ChampionSession(inputs, config, qualified_inputs=ready).process(
        state, day, date(2024, 1, 3) if day.day == 2 else date(2024, 1, 4), bars, emit
    )
    if mutate:
        mutate(admission, events, inputs, c)
    if version != "legacy":
        admission_hash = journal.store_input_manifest(c, admission)
    start = {
        "portfolio_before": portfolio(state),
        "fingerprints_digest": digest(fp),
        "fingerprint_keys": sorted(fp),
    }
    if version != "legacy":
        start["readiness_evidence_hash"] = admission_hash
    journal.append(c, "SESSION_STARTED", start, now=inputs.now, session=day)
    for kind, payload in events:
        terminal = journal.append(c, kind, payload, now=inputs.now, session=day)
    journal.append(
        c,
        "SESSION_COMMITTED",
        {
            "portfolio_after": portfolio(after),
            "portfolio_digest": digest(portfolio(after)),
            "candidate_decision_digest": digest(
                [{"event_type": k, "payload": p} for k, p in events]
            ),
            "terminal_event_hash": terminal,
        },
        now=inputs.now,
        session=day,
    )
    return journal.verify(c)


@pytest.mark.parametrize("version", ["new", "v1"])
def test_full_sessions_and_existing_evidence_still_verify(memory, version):
    verified = append_session(memory, version=version, unresolved=False, purpose=FULL)
    assert verified.economic_sessions == 1


@pytest.mark.parametrize("memory", [True], indirect=True)
def test_pre_mode_journal_without_admission_keeps_old_contract(memory):
    verified = append_session(memory, version="legacy", unresolved=False, purpose=FULL)
    assert verified.economic_sessions == 1
    assert memory[0].verify(memory[1]).terminal_hash == verified.terminal_hash


def test_invalid_mode_transition_does_not_use_session_admission(memory):
    append_session(memory)
    journal, c, _, fp = memory
    journal.append(
        c,
        "MODE_TRANSITION",
        {"from": ECONOMIC, "to": "OBSERVATION_ONLY"},
        now=datetime(2024, 1, 2, 22, tzinfo=UTC),
    )
    with pytest.raises(ShadowError) as exc:
        journal.verify(c)
    assert exc.value.details["reason"] == "mode transition origin"


def test_legitimate_limited_evidence_keeps_economic_readiness_false(memory):
    verified = append_session(memory)
    assert verified.last_decision["reason"] == "NO_TRADE_DATA_INCOMPLETE"
    assert verified.state == initial_state(100000.0)
    assert verified.shadow_mode == ECONOMIC


def test_old_evidence_does_not_gain_limited_admission(memory):
    with pytest.raises(ShadowError) as exc:
        append_session(memory, version="v1")
    assert exc.value.details["reason"] == "activation economic readiness"


def test_cannot_make_economic_readiness_true_for_limited_purpose(memory):
    def tamper(a, *rest):
        r = a["evidence"]["readiness"]
        r["ready"] = r["economic_shadow_ready"] = True
        r["input_readiness_digest"] = digest(
            {k: v for k, v in r.items() if k != "input_readiness_digest"}
        )

    with pytest.raises(ShadowError) as exc:
        append_session(memory, mutate=tamper)
    assert exc.value.details["reason"] == "economic readiness separation"


def test_old_pending_execution_remains_permitted(memory):
    first = append_session(memory, unresolved=False, purpose=FULL, signal=True)
    assert first.state["pending"] is not None
    second = append_session(memory, date(2024, 1, 3))
    assert second.state["pending"] is None
    assert second.state["position"] is not None
    assert second.state["position"]["holding_days"] == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda a: a.update(purpose="UNKNOWN"), "session purpose"),
        (lambda a: a.update(purpose=FULL), "activation economic readiness"),
        (lambda a: a.update(fingerprints_digest="0" * 64), "admission fingerprint"),
        (lambda a: a.update(state_digest="0" * 64), "admission state"),
        (lambda a: a.update(strict_contract_version="relaxed"), "admission strict contract"),
        (
            lambda a: a.update(contract="shadow-session-admission/v999"),
            "observation evidence contract",
        ),
    ],
)
def test_rehashed_manipulated_admission_is_rejected(memory, change, reason):
    with pytest.raises(ShadowError) as exc:
        append_session(memory, mutate=lambda a, *rest: change(a))
    assert exc.value.details["reason"] == reason


@pytest.mark.parametrize(
    "field,update",
    [
        ("sec_state", {"last_success_at": "2023-12-01T22:00:00+00:00"}),
        ("sec_state", {"status": "failed", "errors": 1}),
        ("asset_state", {"status": "running"}),
        ("daily_state", {"errors": 1}),
        ("spy_state", {"status": "OBSERVATION_MISSING"}),
    ],
)
def test_unresolved_plus_hidden_upstream_failure_rejected(memory, field, update):
    def tamper(a, *rest):
        ready = a["evidence"]["readiness"]
        ready[field].update(update)
        ready["input_readiness_digest"] = digest(
            {k: v for k, v in ready.items() if k != "input_readiness_digest"}
        )

    with pytest.raises(ShadowError):
        append_session(memory, mutate=tamper)


def test_unresolved_plus_sec_blocker_never_admitted(memory):
    def tamper(a, *rest):
        r = a["evidence"]["readiness"]
        r["blocking_reasons"].append("SEC_SNAPSHOT_STALE")
        r["observation_blocking_reasons"] = ["SEC_SNAPSHOT_STALE"]
        r["observation_shadow_ready"] = False
        r["input_readiness_digest"] = digest(
            {k: v for k, v in r.items() if k != "input_readiness_digest"}
        )

    with pytest.raises(ShadowError):
        append_session(memory, mutate=tamper)


def test_missing_critical_bar_even_with_rebound_digests_rejected(memory):
    append_session(memory, unresolved=False, purpose=FULL, signal=True)

    def tamper(a, events, *rest):
        events[0][1]["observed_bars"] = {}
        events[0][1]["position_management_ohlc_digest"] = digest({})
        a["critical_bars_digest"] = digest({})

    with pytest.raises(ShadowError) as exc:
        append_session(memory, date(2024, 1, 3), mutate=tamper)
    assert "PENDING_ENTRY_BAR_MISSING" in exc.value.details["reason"]


def test_forbidden_new_intent_cannot_hide_behind_complete_screen(memory):
    def tamper(a, events, *rest):
        events.insert(-1, ["ENTRY_INTENT_CREATED", {}])

    with pytest.raises(ShadowError) as exc:
        append_session(memory, mutate=tamper)
    assert exc.value.details["reason"] == "new intent forbidden by session purpose"


def test_invalid_transition_before_preflight_rejected(memory):
    def tamper(a, events, *rest):
        events.insert(0, ["POSITION_MANAGED", {}])

    with pytest.raises(ShadowError) as exc:
        append_session(memory, mutate=tamper)
    assert exc.value.details["reason"] == "economic transition before strict preflight"


def test_source_provenance_rehashed_to_wrong_feed_rejected(memory):
    def tamper(a, events, inputs, c):
        source = deepcopy(inputs.provenance)
        for rows in source["market_inputs"].values():
            for row in rows:
                row["feed"] = "iex"
        h = inputs.manifest_sink(source)
        e = a["evidence"]
        e["input_manifest_hash"] = h
        e["readiness"]["input_provenance_hash"] = h
        r = e["readiness"]
        r["input_readiness_digest"] = digest(
            {k: v for k, v in r.items() if k != "input_readiness_digest"}
        )

    with pytest.raises(ShadowError) as exc:
        append_session(memory, mutate=tamper)
    assert exc.value.details["reason"] == "admission critical SIP contract"
