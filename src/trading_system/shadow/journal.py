"""Separate SQLite append-only journal; one immediate transaction per session."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from trading_system.data.market_sessions import (
    latest_completed_trading_session,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.shadow.contracts import (
    GENESIS_HASH,
    IDENTITY,
    LABEL,
    SCHEMA_VERSION,
    STRICT_CONTRACT_VERSION,
    ShadowError,
    canonical_json,
    digest,
    normalized,
)
from trading_system.shadow.state import (
    TRANSITIONS,
    initial_state,
    portfolio,
    reduce_transition,
    require,
    validate_state,
)

DDL = """
CREATE TABLE events (
 sequence_number INTEGER PRIMARY KEY CHECK(sequence_number > 0),
 journal_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version = 1),
 created_at_utc TEXT NOT NULL,
 session TEXT,
 event_type TEXT NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload)),
 previous_event_hash TEXT NOT NULL CHECK(length(previous_event_hash) = 64),
 event_hash TEXT NOT NULL UNIQUE CHECK(length(event_hash) = 64)
);
CREATE UNIQUE INDEX one_genesis ON events(event_type) WHERE event_type='SHADOW_INITIALIZED';
CREATE UNIQUE INDEX one_session_commit ON events(session) WHERE event_type='SESSION_COMMITTED';
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'SHADOW_IMMUTABLE_UPDATE'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'SHADOW_IMMUTABLE_DELETE'); END;
CREATE TRIGGER events_append BEFORE INSERT ON events
BEGIN
 SELECT CASE WHEN NEW.sequence_number != COALESCE((SELECT MAX(sequence_number)+1 FROM events),1)
   THEN RAISE(ABORT, 'SHADOW_SEQUENCE') END;
 SELECT CASE WHEN NEW.previous_event_hash != COALESCE(
   (SELECT event_hash FROM events ORDER BY sequence_number DESC LIMIT 1),
   '0000000000000000000000000000000000000000000000000000000000000000')
   THEN RAISE(ABORT, 'SHADOW_HASH_LINK') END;
 SELECT CASE WHEN NEW.sequence_number=1 AND NEW.event_type!='SHADOW_INITIALIZED'
   THEN RAISE(ABORT, 'SHADOW_GENESIS_REQUIRED') END;
 SELECT CASE WHEN NEW.sequence_number>1 AND NEW.journal_id !=
   (SELECT journal_id FROM events WHERE sequence_number=1)
   THEN RAISE(ABORT, 'SHADOW_IDENTITY') END;
END;
"""


@dataclass(frozen=True)
class VerifiedJournal:
    inception: dict
    state: dict
    last_session: date | None
    committed_sessions: frozenset[date]
    last_decision: dict | None
    terminal_hash: str
    event_count: int


def event_hash(event: dict) -> str:
    return digest({key: value for key, value in event.items() if key != "event_hash"})


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self, *, write=False):
        if not self.path.is_file():
            raise ShadowError("SHADOW_NOT_INITIALIZED", path=str(self.path))
        uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            code = (
                "SHADOW_JOURNAL_BUSY"
                if "locked" in str(exc) or "busy" in str(exc)
                else "SHADOW_JOURNAL_INTEGRITY_FAILURE"
            )
            raise ShadowError(code, reason=str(exc)) from exc
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(
        self,
        *,
        now: datetime,
        fingerprints: dict,
        git_identity: dict,
        capital=100.0,
        input_readiness=None,
    ):
        if not math.isfinite(capital) or capital <= 0:
            raise ShadowError("SHADOW_INVALID_INITIAL_CAPITAL")
        boundary = latest_completed_trading_session(now)
        inception = normalized(
            {
                "journal_id": str(uuid4()),
                "created_at_utc": now,
                **git_identity,
                **fingerprints,
                "initial_capital": float(capital),
                "latest_completed_official_session_at_inception": boundary,
                **({"input_readiness": input_readiness} if input_readiness is not None else {}),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive creation: a second init never opens or replaces the existing file.
            with self.path.open("xb"):
                pass
        except FileExistsError as exc:
            raise ShadowError("SHADOW_ALREADY_INITIALIZED", path=str(self.path)) from exc
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("BEGIN IMMEDIATE;\n" + DDL)
            self.append(connection, "SHADOW_INITIALIZED", inception, now=now)
            connection.commit()
        except BaseException:
            connection.rollback()
            # Keep a failed initialization file as evidence. Never auto-overwrite it.
            raise
        finally:
            connection.close()
        return inception

    def append(self, connection, kind: str, payload: dict, *, now: datetime, session=None) -> str:
        last = connection.execute(
            "SELECT * FROM events ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        event = normalized(
            {
                "sequence_number": last["sequence_number"] + 1 if last else 1,
                "journal_id": last["journal_id"] if last else payload["journal_id"],
                "schema_version": SCHEMA_VERSION,
                "created_at_utc": now,
                "session": session,
                "event_type": kind,
                "payload": payload,
                "previous_event_hash": last["event_hash"] if last else GENESIS_HASH,
            }
        )
        hashed = event_hash(event)
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event["sequence_number"],
                event["journal_id"],
                event["schema_version"],
                event["created_at_utc"],
                event["session"],
                kind,
                canonical_json(payload),
                event["previous_event_hash"],
                hashed,
            ),
        )
        return hashed

    def verify(self, connection=None) -> VerifiedJournal:
        try:
            if connection is None:
                with self.connection() as opened:
                    return self._verify(opened)
            return self._verify(connection)
        except ShadowError:
            raise
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
        ) as exc:
            raise ShadowError("SHADOW_JOURNAL_INTEGRITY_FAILURE", reason=str(exc)) from exc

    def _verify(self, connection) -> VerifiedJournal:
        # Missing or altered guards are corruption, never silently repaired by a run.
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(DDL)
            schema_query = (
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            schema = [tuple(row) for row in connection.execute(schema_query)]
            require(
                schema == expected.execute(schema_query).fetchall(), "journal SQL schema/guards"
            )
        finally:
            expected.close()
        events = connection.execute("SELECT * FROM events ORDER BY sequence_number").fetchall()
        require(bool(events), "missing genesis")
        previous = GENESIS_HASH
        inception = state = last = decision = active = None
        inception_only_keys = {
            "journal_id",
            "created_at_utc",
            "local_git_commit",
            "branch",
            "initial_capital",
            "latest_completed_official_session_at_inception",
            "input_readiness",
        }
        committed, session_events = set(), []
        preflight = screened = decided = False
        for number, row in enumerate(events, 1):
            event = dict(row)
            payload = json.loads(event["payload"])
            require(canonical_json(payload) == event["payload"], "noncanonical payload")
            event["payload"] = payload
            require(event["sequence_number"] == number, "sequence continuity")
            require(event["schema_version"] == SCHEMA_VERSION, "event schema version")
            require(event["previous_event_hash"] == previous, "previous hash linkage")
            require(event["event_hash"] == event_hash(event), "event hash mismatch")
            timestamp = datetime.fromisoformat(event["created_at_utc"])
            require(timestamp.utcoffset() is not None, "event clock")
            kind, session = event["event_type"], event["session"]
            if number == 1:
                require(kind == "SHADOW_INITIALIZED" and session is None, "genesis")
                inception = payload
                require(inception["journal_id"] == event["journal_id"], "inception journal id")
                require(
                    inception["created_at_utc"] == event["created_at_utc"], "inception timestamp"
                )
                require(
                    inception["strategy_identity"] == IDENTITY
                    and inception["production_label"] == LABEL,
                    "inception identity",
                )
                require(inception["journal_schema_version"] == SCHEMA_VERSION, "inception schema")
                require(
                    inception["data_integrity_contract_version"] == STRICT_CONTRACT_VERSION,
                    "inception contract",
                )
                require(
                    inception["latest_completed_official_session_at_inception"]
                    == latest_completed_trading_session(timestamp).isoformat(),
                    "inception boundary",
                )
                for key in (
                    "strategy_fingerprint",
                    "engine_fingerprint",
                    "economic_config_fingerprint",
                    "strict_contract_fingerprint",
                ):
                    require(len(inception[key]) == 64, f"inception {key}")
                require(
                    inception["strategy_fingerprint"] == digest(inception["champion_definition"]),
                    "inception strategy digest",
                )
                require(
                    inception["economic_config_fingerprint"]
                    == digest(inception["economic_config"]),
                    "inception config digest",
                )
                require(
                    inception["engine_fingerprint"]
                    == digest(
                        {"files": inception["engine_files"], "runtime": inception["runtime"]}
                    ),
                    "inception engine digest",
                )
                require(
                    inception["strict_contract_fingerprint"]
                    == inception["engine_files"]["backtest/data_integrity.py"],
                    "inception strict digest",
                )
                if "data_pipeline_fingerprint" in inception:
                    require(
                        inception["data_pipeline_fingerprint"]
                        == digest(
                            {
                                "files": inception["data_pipeline_files"],
                                "runtime": inception["runtime"],
                            }
                        ),
                        "inception data pipeline digest",
                    )
                if "input_readiness" in inception:
                    readiness = inception["input_readiness"]
                    require(
                        readiness["ready"] and not readiness["blocking_reasons"],
                        "inception readiness",
                    )
                    require(
                        readiness["unresolved_rank_relevant_count"] == 0,
                        "inception unresolved inputs",
                    )
                    require(
                        readiness["analysis_session"]
                        == inception["latest_completed_official_session_at_inception"],
                        "inception analysis session",
                    )
                    require(
                        readiness["input_readiness_digest"]
                        == digest(
                            {
                                key: value
                                for key, value in readiness.items()
                                if key != "input_readiness_digest"
                            }
                        ),
                        "inception readiness digest",
                    )
                capital = inception["initial_capital"]
                require(math.isfinite(capital) and capital > 0, "inception capital")
                state = initial_state(capital)
            else:
                require(event["journal_id"] == inception["journal_id"], "journal identity")
                require(
                    timestamp >= datetime.fromisoformat(inception["created_at_utc"]),
                    "pre-inception event",
                )
                if kind == "DATA_PREFLIGHT_FAILED":
                    require(
                        active is None and payload["state_digest"] == digest(state),
                        "failure changed state",
                    )
                    require(payload["error"]["session"] == session, "failure session")
                    require(
                        payload["input_digest"] == digest(payload["observed_bars"]),
                        "failure input digest",
                    )
                    current = date.fromisoformat(session)
                    anchor = last or date.fromisoformat(
                        inception["latest_completed_official_session_at_inception"]
                    )
                    require(
                        current > anchor
                        and trading_sessions_between(anchor, current) == [anchor, current],
                        "failed session sequence",
                    )
                    identity = {
                        key: value
                        for key, value in inception.items()
                        if key not in inception_only_keys
                    }
                    require(
                        payload["fingerprints_digest"] == digest(identity), "failure fingerprint"
                    )
                elif kind == "SESSION_STARTED":
                    require(active is None, "nested session")
                    current = date.fromisoformat(session)
                    anchor = last or date.fromisoformat(
                        inception["latest_completed_official_session_at_inception"]
                    )
                    require(
                        current > anchor
                        and trading_sessions_between(anchor, current) == [anchor, current],
                        "session sequence",
                    )
                    require(current not in committed, "duplicate session commit")
                    require(
                        timestamp >= regular_session_bounds(current)[1],
                        "incomplete journal session",
                    )
                    require(payload["portfolio_before"] == portfolio(state), "session before state")
                    require(
                        payload["fingerprint_keys"] == sorted(set(inception) - inception_only_keys),
                        "session fingerprint scope",
                    )
                    require(
                        payload["fingerprints_digest"]
                        == digest({key: inception[key] for key in payload["fingerprint_keys"]}),
                        "session fingerprint",
                    )
                    active = session
                    session_events = []
                    preflight = screened = decided = False
                else:
                    require(active is not None and session == active, "event outside session")
                    if kind == "DATA_PREFLIGHT_PASSED":
                        require(not preflight and not session_events, "preflight order")
                        require(payload["mode"] == "STRICT", "preflight mode")
                        require(
                            payload["contract_version"]
                            == inception["data_integrity_contract_version"],
                            "preflight contract",
                        )
                        require(
                            payload["position_management_ohlc_digest"]
                            == digest(payload["observed_bars"]),
                            "preflight input digest",
                        )
                        preflight = True
                    elif kind == "SCREEN_EVALUATED":
                        require(preflight and not screened, "screen order")
                        rows = payload["candidates"]
                        require(payload["eligible_candidate_count"] == len(rows), "candidate count")
                        require(
                            [r["rank"] for r in rows] == list(range(1, len(rows) + 1)),
                            "candidate ranks",
                        )
                        require(
                            rows == sorted(rows, key=lambda r: (-r["f_score"], r["symbol"])),
                            "candidate ordering",
                        )
                        require(payload["ranking_digest"] == digest(rows), "ranking digest")
                        require(
                            payload["input_set_digest"] == digest(payload["input_provenance"]),
                            "input provenance digest",
                        )
                        require(
                            payload["data_complete"] == (not payload["incomplete_inputs"]),
                            "screen completeness",
                        )
                        screen = payload
                        screened = True
                    elif kind in TRANSITIONS:
                        require(preflight, "economic transition before strict preflight")
                        if kind == "ENTRY_INTENT_CREATED":
                            require(screened and not decided, "intent order")
                            require(
                                screen["data_complete"] and bool(screen["candidates"]),
                                "intent on incomplete screen",
                            )
                            require(
                                payload["symbol"] == screen["candidates"][0]["symbol"],
                                "intent selected rank one",
                            )
                            require(
                                payload["f_score"] == screen["candidates"][0]["f_score"],
                                "intent selected score",
                            )
                            require(payload["signal_session"] == session, "intent signal session")
                            require(
                                payload["after"]["pending"]["order"]["signal_date"] == session,
                                "pending signal session",
                            )
                            order = payload["after"]["pending"]["order"]
                            require(
                                order["record"]["symbol"] == payload["symbol"]
                                and order["variant_score"] == payload["f_score"],
                                "intent ranking snapshot",
                            )
                            decided = True
                            decision = {
                                "session": session,
                                "decision": kind,
                                "symbol": payload["symbol"],
                            }
                        elif kind in {"ENTRY_EXECUTED", "ENTRY_REJECTED"}:
                            require(not screened and not decided, "entry execution order")
                            require(
                                state["pending"]["expected_execution_session"] == session,
                                "pending execution session",
                            )
                        else:
                            require(not decided, "management after signal decision")
                        state = reduce_transition(state, kind, payload, capital)
                    elif kind == "DECISION_NO_TRADE":
                        require(preflight and screened and not decided, "no trade order")
                        require(payload["state_digest"] == digest(state), "no trade changed state")
                        if not screen["data_complete"]:
                            require(
                                payload["reason"] == "NO_TRADE_DATA_INCOMPLETE",
                                "incomplete data decision",
                            )
                        elif state["position"] or state["pending"]:
                            require(payload["reason"] == "CAPACITY_FULL", "capacity decision")
                        elif not screen["candidates"]:
                            require(
                                payload["reason"] == "NO_ELIGIBLE_CANDIDATES",
                                "empty ranking decision",
                            )
                        else:
                            require(
                                payload["reason"] == "ENTRY_REJECTED_BY_CHAMPION",
                                "unknown no trade reason",
                            )
                        decided = True
                        decision = {"session": session, **payload}
                    elif kind == "SESSION_COMMITTED":
                        require(preflight and screened and decided, "incomplete session")
                        require(payload["portfolio_after"] == portfolio(state), "commit state")
                        require(
                            payload["portfolio_digest"] == digest(portfolio(state)),
                            "portfolio digest",
                        )
                        require(
                            payload["candidate_decision_digest"] == digest(session_events),
                            "decision digest",
                        )
                        require(payload["terminal_event_hash"] == previous, "terminal hash")
                        validate_state(state, capital)
                        if state["position"]:
                            position = state["position"]
                            require(
                                position["holding_days"]
                                == len(
                                    trading_sessions_between(
                                        date.fromisoformat(position["entry_date"]),
                                        date.fromisoformat(session),
                                    )
                                ),
                                "official holding sessions",
                            )
                        if state["pending"]:
                            require(
                                state["pending"]["order"]["signal_date"] == session,
                                "pending intent was deferred",
                            )
                        last = date.fromisoformat(session)
                        committed.add(last)
                        active = None
                    else:
                        require(False, f"unknown event type {kind}")
                    if kind != "SESSION_COMMITTED":
                        session_events.append({"event_type": kind, "payload": payload})
            previous = event["event_hash"]
        require(active is None, "uncommitted session tail")
        return VerifiedJournal(
            inception, state, last, frozenset(committed), decision, previous, len(events)
        )
