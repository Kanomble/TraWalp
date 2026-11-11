"""Verified, drift-guarded, forward-only local Shadow session lifecycle."""

from __future__ import annotations

from datetime import UTC, date, datetime

from trading_system.backtest.data_integrity import (
    DataIntegrityError,
    validate_session_position_data,
)
from trading_system.backtest.engine import BacktestEngine
from trading_system.data.forward_market import CONTRACT
from trading_system.data.market_sessions import latest_completed_trading_session
from trading_system.shadow.admission import evaluate_inception_admission
from trading_system.shadow.contracts import (
    ShadowError,
    assert_no_drift,
    digest,
    fingerprints,
    local_git_identity,
)
from trading_system.shadow.execution import ChampionSession
from trading_system.shadow.inputs import LocalInputs, separate_paths
from trading_system.shadow.journal import Journal
from trading_system.shadow.modes import (
    ECONOMIC,
    OBSERVATION_ONLY,
    blocked_payload,
    evidence_manifest,
    observation_payload,
)
from trading_system.shadow.readiness import evaluate_shadow_input_readiness
from trading_system.shadow.state import portfolio


def next_official_session(anchor: date) -> date:
    session = BacktestEngine._next_xnys_session(anchor)
    if session is None:
        raise ShadowError("SHADOW_CALENDAR_UNAVAILABLE")
    return session


class ShadowRunner:
    def __init__(self, journal: Journal, market_path, config, *, clock=None, screen_source=None):
        self.journal, self.market_path, self.config = journal, market_path, config
        separate_paths(journal.path, market_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.screen_source = screen_source

    def initialize(self, *, capital=100.0, shadow_mode=ECONOMIC):
        now = self.clock()
        admission = evaluate_inception_admission(
            self.market_path,
            self.config,
            journal_path=self.journal.path,
            now=now,
            requested_mode=shadow_mode,
            capital=capital,
            screen_source=self.screen_source,
        )
        if not admission.selected_mode_ready:
            raise ShadowError("SHADOW_INCEPTION_DATA_NOT_READY", **admission.as_dict())
        readiness = admission.readiness
        completed_at = self.clock()
        # A large screen can span the official close. Never bind a stale boundary
        # or silently perform a second expensive screen in this initialization.
        if (
            latest_completed_trading_session(completed_at).isoformat()
            != readiness.details["analysis_session"]
        ):
            raise ShadowError("SHADOW_INCEPTION_SESSION_CHANGED_RETRY")
        if completed_at < now:
            raise ShadowError("SHADOW_INCEPTION_CLOCK_REGRESSION")
        return self.journal.initialize(
            now=completed_at,
            fingerprints=admission.fingerprints,
            git_identity=local_git_identity(),
            capital=capital,
            input_readiness=readiness.as_dict(),
            input_manifest=admission.input_manifest,
            shadow_mode=shadow_mode,
        )

    def status(self):
        verified = self.journal.verify()
        inception = verified.inception
        try:
            assert_no_drift(inception, fingerprints(self.config))
            drift = "UNCHANGED"
        except ShadowError as exc:
            drift = exc.as_dict()
        anchor = verified.last_session or date.fromisoformat(
            inception["latest_completed_official_session_at_inception"]
        )
        state = portfolio(verified.state)
        position, pending = state["position"], state["pending"]
        return {
            "journal_mode": verified.shadow_mode,
            "last_observed_session": verified.last_observed_session,
            "last_economic_session": verified.last_economic_session,
            "ranking_blocked_sessions": verified.ranking_blocked_sessions,
            "economic_sessions": verified.economic_sessions,
            "economic_inception_session": verified.economic_inception_session
            or (
                next_official_session(
                    date.fromisoformat(inception["latest_completed_official_session_at_inception"])
                )
                if inception.get("shadow_mode", ECONOMIC) == ECONOMIC
                else None
            ),
            "current_position_state": "OPEN" if position else "PENDING" if pending else "FLAT",
            "journal_id": inception["journal_id"],
            "inception": inception["created_at_utc"],
            "inception_session": inception["latest_completed_official_session_at_inception"],
            "last_committed_session": verified.last_session,
            "next_expected_session": next_official_session(anchor),
            "cash": state["cash"],
            "equity": state["equity"],
            "realized_pnl": state["realized_pnl"],
            "unrealized_pnl": state["unrealized_pnl"],
            "open_position": (
                {
                    key: position[key]
                    for key in (
                        "symbol",
                        "quantity",
                        "entry_date",
                        "entry_price",
                        "stop_price",
                        "target_price",
                        "holding_days",
                    )
                }
                if position
                else None
            ),
            "pending_entry": (
                {
                    "symbol": pending["order"]["record"]["symbol"],
                    "signal_session": pending["order"]["signal_date"],
                    "expected_execution_session": pending["expected_execution_session"],
                }
                if pending
                else None
            ),
            "last_decision": verified.last_decision,
            "journal_verification": "VALID",
            "configuration_drift": drift,
        }

    def activate_economic(self):
        """Explicit append-only admission; first economic decision is a FUTURE session."""
        now = self.clock()
        with self.journal.connection(write=True) as connection:
            verified = self.journal.verify(connection)
            current = fingerprints(self.config)
            assert_no_drift(verified.inception, current)
            if (
                verified.shadow_mode != OBSERVATION_ONLY
                or verified.state["position"]
                or verified.state["pending"]
                or not verified.last_observation_ranking_complete
                or verified.last_observed_session != verified.last_session
                or verified.last_session != latest_completed_trading_session(now)
            ):
                raise ShadowError("SHADOW_ACTIVATION_NOT_READY")
            with LocalInputs(self.market_path).snapshot() as inputs:
                readiness = evaluate_shadow_input_readiness(
                    inputs,
                    self.config,
                    now=now,
                    session=verified.last_session,
                    screen_source=self.screen_source,
                )
                if not readiness.ready:
                    raise ShadowError("SHADOW_ACTIVATION_NOT_READY", **readiness.as_dict())
                if latest_completed_trading_session(self.clock()) != verified.last_session:
                    raise ShadowError("SHADOW_ACTIVATION_SESSION_CHANGED_RETRY")
                input_hash = self.journal.store_input_manifest(connection, inputs.provenance)
                evidence = evidence_manifest(readiness, input_hash)
                evidence_hash = self.journal.store_input_manifest(connection, evidence)
            completed_at = self.clock()
            if (
                completed_at < now
                or latest_completed_trading_session(completed_at) != verified.last_session
            ):
                raise ShadowError("SHADOW_ACTIVATION_SESSION_CHANGED_RETRY")
            self.journal.append(
                connection,
                "MODE_TRANSITION",
                {
                    "from": OBSERVATION_ONLY,
                    "to": ECONOMIC,
                    "effective_after_session": verified.last_session,
                    "economic_inception_session": next_official_session(verified.last_session),
                    "evidence_hash": evidence_hash,
                    "state_digest": digest(verified.state),
                    "fingerprints_digest": digest(current),
                },
                now=completed_at,
            )
            self.journal.verify(connection)
        return {
            "status": "ECONOMIC_MODE_ACTIVATED",
            "economic_inception_session": next_official_session(verified.last_session),
        }

    def run(self, session: date | None = None):
        now = self.clock()
        failure = None
        # Writers serialize before verification or source reads. A second runner
        # sees the committed first runner state and returns an idempotent no-op.
        with self.journal.connection(write=True) as connection:
            verified = self.journal.verify(connection)
            current = fingerprints(self.config)
            assert_no_drift(verified.inception, current)
            boundary = date.fromisoformat(
                verified.inception["latest_completed_official_session_at_inception"]
            )
            expected = next_official_session(verified.last_session or boundary)
            selected = session or expected
            if selected <= boundary:
                raise ShadowError("SHADOW_BEFORE_INCEPTION", session=selected.isoformat())
            if selected in verified.committed_sessions:
                return {"status": "ALREADY_PROCESSED", "session": selected}
            if session is None and selected > latest_completed_trading_session(now):
                return {"status": "NO_COMPLETED_SESSION", "next_expected_session": selected}
            # Calendar/clock validity uses CC-01, including explicit future sessions.
            validate_session_position_data(
                selected,
                bars={},
                held_positions={},
                pending_entries={},
                previous_processed_session=None,
                now=now,
            )
            if selected != expected:
                raise ShadowError(
                    "SHADOW_SESSION_GAP",
                    session=selected.isoformat(),
                    expected=expected.isoformat(),
                )
            state = verified.state
            position, pending = state["position"], state["pending"]
            held = (
                {position["symbol"]: date.fromisoformat(position["entry_date"])} if position else {}
            )
            intents = (
                {
                    pending["order"]["record"]["symbol"]: date.fromisoformat(
                        pending["order"]["signal_date"]
                    )
                }
                if pending
                else {}
            )
            with LocalInputs(self.market_path).snapshot() as inputs:
                inputs.manifest_sink = lambda manifest: self.journal.store_input_manifest(
                    connection, manifest
                )
                bars = inputs.bars_on_session(set(held) | set(intents), selected)
                try:
                    validate_session_position_data(
                        selected,
                        bars=bars,
                        held_positions=held,
                        pending_entries=intents,
                        previous_processed_session=verified.last_session or boundary,
                        now=now,
                    )
                except DataIntegrityError as exc:
                    # Append failure evidence only. No session or state is committed,
                    # so retry remains bound to the SAME pending execution session.
                    self.journal.append(
                        connection,
                        "DATA_PREFLIGHT_FAILED",
                        {
                            **CONTRACT,
                            "session": selected,
                            "input_manifest_hash": inputs.manifest_sink(inputs.provenance),
                            "error": exc.as_dict(),
                            "state_digest": digest(state),
                            "observed_bars": bars,
                            "input_digest": digest(bars),
                            "fingerprints_digest": digest(current),
                        },
                        now=now,
                        session=selected,
                    )
                    failure = exc
                if failure is None:
                    readiness = evaluate_shadow_input_readiness(
                        inputs,
                        self.config,
                        now=now,
                        session=selected,
                        screen_source=self.screen_source,
                    )
                    admitted = (
                        readiness.observation_ready
                        if verified.shadow_mode == OBSERVATION_ONLY
                        else readiness.ready
                    )
                    if not admitted:
                        raise ShadowError("SHADOW_SESSION_DATA_NOT_READY", **readiness.as_dict())
                    input_hash = self.journal.store_input_manifest(connection, inputs.provenance)
                    evidence = evidence_manifest(readiness, input_hash)
                    evidence_hash = self.journal.store_input_manifest(connection, evidence)
                    terminal = self.journal.append(
                        connection,
                        "SESSION_STARTED",
                        {
                            "portfolio_before": portfolio(state),
                            "fingerprints_digest": digest(current),
                            "fingerprint_keys": sorted(current),
                            "readiness_evidence_hash": evidence_hash,
                        },
                        now=now,
                        session=selected,
                    )
                    events = []

                    def emit(kind, payload):
                        nonlocal terminal
                        terminal = self.journal.append(
                            connection, kind, payload, now=now, session=selected
                        )
                        events.append({"event_type": kind, "payload": payload})

                    emit(
                        "DATA_PREFLIGHT_PASSED",
                        {
                            "mode": "STRICT",
                            "contract_version": current["data_integrity_contract_version"],
                            "observed_bars": bars,
                            "position_management_ohlc_digest": digest(bars),
                        },
                    )
                    if verified.shadow_mode == OBSERVATION_ONLY:
                        if position or pending:
                            raise ShadowError("SHADOW_OBSERVATION_ECONOMIC_STATE")
                        emit("OBSERVATION_SESSION", observation_payload(evidence, evidence_hash))
                        if not readiness.details["ranking_complete"]:
                            emit("RANKING_BLOCKED", blocked_payload(evidence, evidence_hash))
                        after = state
                    else:
                        after = ChampionSession(
                            inputs, self.config, qualified_inputs=readiness
                        ).process(state, selected, next_official_session(selected), bars, emit)
                    terminal = self.journal.append(
                        connection,
                        "SESSION_COMMITTED",
                        {
                            "portfolio_after": portfolio(after),
                            "portfolio_digest": digest(portfolio(after)),
                            "candidate_decision_digest": digest(events),
                            "terminal_event_hash": terminal,
                        },
                        now=now,
                        session=selected,
                    )
                # Verify the transaction's entire chain + reconstructed ledger
                # before SQLite commits it, including failures with unchanged state.
                self.journal.verify(connection)
        if failure:
            raise failure
        return {
            "status": (
                "OBSERVED_NO_ECONOMIC_DECISION"
                if verified.shadow_mode == OBSERVATION_ONLY
                else "SESSION_COMMITTED"
            ),
            "ranking_complete": readiness.details["ranking_complete"],
            "session": selected,
            "portfolio": portfolio(after),
            "terminal_event_hash": terminal,
        }
