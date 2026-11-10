"""Separate SQLite append-only journal; one immediate transaction per session."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import zlib
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
from trading_system.shadow.modes import (
    ECONOMIC,
    MODES,
    OBSERVATION_ONLY,
    blocked_payload,
    observation_payload,
    validate_evidence,
    validate_mode,
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
    shadow_mode: str = ECONOMIC
    last_observed_session: date | None = None
    last_economic_session: date | None = None
    ranking_blocked_sessions: int = 0
    economic_sessions: int = 0
    last_observation_ranking_complete: bool = False
    economic_inception_session: str | None = None


def event_hash(event: dict) -> str:
    return digest({key: value for key, value in event.items() if key != "event_hash"})


class Journal:
    @staticmethod
    def load_input_manifest(connection, content_hash):
        row = connection.execute(
            "SELECT content FROM input_manifests WHERE content_hash=?", (content_hash,)
        ).fetchone()
        require(row is not None, "input manifest missing")
        content = json.loads(row[0])
        if content.get("manifest_encoding") == "zlib+base64/v1":
            content = json.loads(zlib.decompress(base64.b64decode(content["data"])))
        require(digest(content) == content_hash, "input manifest corrupted")
        return content

    @staticmethod
    def store_input_manifest(connection, manifest):
        connection.execute("""CREATE TABLE IF NOT EXISTS input_manifests (
            content_hash TEXT PRIMARY KEY, content TEXT NOT NULL)""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS immutable_manifest_update
            BEFORE UPDATE ON input_manifests BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS immutable_manifest_delete
            BEFORE DELETE ON input_manifests BEGIN SELECT RAISE(ABORT,'IMMUTABLE'); END""")
        content_hash = digest(manifest)
        content = canonical_json(manifest)
        # Full-universe manifests can exceed SQLite's single TEXT value limit.
        # Compress evidence once; its identity remains the uncompressed content hash.
        if len(content) > 1_000_000:
            content = canonical_json(
                {
                    "manifest_encoding": "zlib+base64/v1",
                    "data": base64.b64encode(zlib.compress(content.encode("utf-8"))).decode(
                        "ascii"
                    ),
                }
            )
        connection.execute(
            "INSERT OR IGNORE INTO input_manifests VALUES (?,?)",
            (content_hash, content),
        )
        return content_hash

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
        input_manifest=None,
        shadow_mode=ECONOMIC,
    ):
        validate_mode(shadow_mode)
        if shadow_mode == OBSERVATION_ONLY and (
            not input_readiness
            or not input_readiness.get("observation_shadow_ready")
            or input_readiness.get("observation_blocking_reasons")
        ):
            raise ShadowError("SHADOW_INCEPTION_DATA_NOT_READY")
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
                "shadow_mode": shadow_mode,
                "universe_policy": "U0_CURRENT",
                "source_fingerprint": digest(fingerprints["engine_files"]),
                "latest_completed_official_session_at_inception": boundary,
                "forward_sip_transition_boundary": (
                    input_readiness.get("analysis_session") if input_readiness else None
                ),
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
            self.store_input_manifest(connection, input_manifest or {})
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
            self.store_input_manifest(expected, {})
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
        mode = ECONOMIC
        last_observed = last_economic = economic_inception = None
        blocked_sessions = economic_sessions = 0
        observation_complete = False
        inception_only_keys = {
            "forward_sip_transition_boundary",
            "journal_id",
            "created_at_utc",
            "local_git_commit",
            "branch",
            "initial_capital",
            "latest_completed_official_session_at_inception",
            "input_readiness",
            "shadow_mode",
            "universe_policy",
            "source_fingerprint",
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
                mode = inception.get("shadow_mode", ECONOMIC)
                require(mode in MODES, "inception shadow mode")
                if "shadow_mode" in inception:
                    require(
                        inception["universe_policy"] == "U0_CURRENT", "inception universe policy"
                    )
                    require(
                        inception["source_fingerprint"] == digest(inception["engine_files"]),
                        "inception source fingerprint",
                    )
                if mode == OBSERVATION_ONLY:
                    require("input_readiness" in inception, "observation inception evidence")
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
                if inception.get("input_readiness"):
                    readiness = inception["input_readiness"]
                    require(
                        inception["forward_sip_transition_boundary"]
                        == readiness["analysis_session"],
                        "SIP transition boundary",
                    )
                    self.load_input_manifest(connection, readiness["input_provenance_hash"])
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
                    if mode == OBSERVATION_ONLY:
                        require(
                            readiness["observation_shadow_ready"]
                            and not readiness["observation_blocking_reasons"],
                            "inception observation readiness",
                        )
                        require(
                            readiness["observation_blocking_reasons"]
                            == [
                                r
                                for r in readiness["blocking_reasons"]
                                if r != "RANKING_UNRESOLVED"
                            ],
                            "inception observation blockers",
                        )
                    else:
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
                elif kind == "MODE_TRANSITION":
                    require(active is None and mode == OBSERVATION_ONLY, "mode transition origin")
                    require(
                        payload["from"] == OBSERVATION_ONLY and payload["to"] == ECONOMIC,
                        "explicit mode transition",
                    )
                    require(state == initial_state(capital), "activation economic state exists")
                    require(
                        last is not None and last_observed == last and observation_complete,
                        "activation requires complete latest observation",
                    )
                    require(
                        last == latest_completed_trading_session(timestamp),
                        "activation session not current",
                    )
                    require(
                        payload["effective_after_session"] == last.isoformat(),
                        "activation boundary",
                    )
                    economic_inception = payload["economic_inception_session"]
                    require(
                        trading_sessions_between(last, date.fromisoformat(economic_inception))
                        == [last, date.fromisoformat(economic_inception)],
                        "activation next official session",
                    )
                    evidence = self.load_input_manifest(connection, payload["evidence_hash"])
                    validate_evidence(
                        evidence,
                        last.isoformat(),
                        lambda h: self.load_input_manifest(connection, h),
                        economic=True,
                    )
                    require(payload["state_digest"] == digest(state), "activation changed state")
                    require(
                        payload["fingerprints_digest"]
                        == digest(
                            {k: v for k, v in inception.items() if k not in inception_only_keys}
                        ),
                        "activation fingerprint",
                    )
                    mode = ECONOMIC
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
                    if "shadow_mode" in inception:
                        admission_hash = payload["readiness_evidence_hash"]
                        admission = self.load_input_manifest(connection, admission_hash)
                        validate_evidence(
                            admission,
                            session,
                            lambda h: self.load_input_manifest(connection, h),
                            economic=mode == ECONOMIC,
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
                    elif kind == "OBSERVATION_SESSION":
                        require(
                            mode == OBSERVATION_ONLY and preflight and not screened and not decided,
                            "observation event order/mode",
                        )
                        require(state == initial_state(capital), "observation economic state")
                        evidence = self.load_input_manifest(
                            connection, payload["observation_manifest_hash"]
                        )
                        observation_hash = payload["observation_manifest_hash"]
                        require(
                            observation_hash == admission_hash, "observation admission reference"
                        )
                        qa = validate_evidence(
                            evidence, session, lambda h: self.load_input_manifest(connection, h)
                        )
                        require(
                            payload
                            == observation_payload(evidence, payload["observation_manifest_hash"]),
                            "observation payload",
                        )
                        screened = True
                        observation_complete = qa["unresolved_rank_relevant_count"] == 0
                        decided = observation_complete
                        decision = {"session": session, "decision": "OBSERVED_NO_ECONOMIC_DECISION"}
                    elif kind == "RANKING_BLOCKED":
                        require(
                            mode == OBSERVATION_ONLY and screened and not decided,
                            "ranking blocked order/mode",
                        )
                        require(
                            payload["observation_manifest_hash"] == observation_hash,
                            "ranking blocked observation reference",
                        )
                        require(
                            payload
                            == blocked_payload(evidence, payload["observation_manifest_hash"]),
                            "ranking blocked evidence",
                        )
                        require(
                            payload["rank_blocking_unresolved_count"] > 0, "ranking blocked count"
                        )
                        blocked_sessions += 1
                        decided = True
                    elif kind == "SCREEN_EVALUATED":
                        require(mode == ECONOMIC, "economic screen in observation mode")
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
                            payload["input_set_digest"] == payload["input_manifest_hash"],
                            "input provenance digest",
                        )
                        evidence = self.load_input_manifest(
                            connection, payload["input_manifest_hash"]
                        )
                        contract = inception["market_data_contract"]
                        require(
                            all(evidence.get(k) == v for k, v in contract.items()),
                            "manifest market contract",
                        )
                        require(
                            payload["input_provenance_hash"] == payload["input_manifest_hash"],
                            "manifest reference hash",
                        )
                        require(
                            payload["data_complete"] == (not payload["incomplete_inputs"]),
                            "screen completeness",
                        )
                        screen = payload
                        screened = True
                    elif kind in TRANSITIONS:
                        require(mode == ECONOMIC, "economic event in observation mode")
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
                        require(mode == ECONOMIC, "economic decision in observation mode")
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
                        if mode == OBSERVATION_ONLY:
                            require(
                                state == initial_state(capital),
                                "observation committed economic state",
                            )
                            last_observed = last
                        else:
                            last_economic = last
                            economic_sessions += 1
                        committed.add(last)
                        active = None
                    else:
                        require(False, f"unknown event type {kind}")
                    if kind != "SESSION_COMMITTED":
                        session_events.append({"event_type": kind, "payload": payload})
            previous = event["event_hash"]
        require(active is None, "uncommitted session tail")
        return VerifiedJournal(
            inception,
            state,
            last,
            frozenset(committed),
            decision,
            previous,
            len(events),
            mode,
            last_observed,
            last_economic,
            blocked_sessions,
            economic_sessions,
            observation_complete,
            economic_inception,
        )
