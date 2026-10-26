"""Verified, drift-guarded, forward-only local Shadow session lifecycle."""

from __future__ import annotations

from datetime import UTC, date, datetime

from trading_system.backtest.data_integrity import (
    DataIntegrityError,
    validate_session_position_data,
)
from trading_system.backtest.engine import BacktestEngine
from trading_system.data.market_sessions import latest_completed_trading_session
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

    def initialize(self, *, capital=100.0):
        # No market access, screening, simulation, --as-of, or historical start option.
        return self.journal.initialize(
            now=self.clock(),
            fingerprints=fingerprints(self.config),
            git_identity=local_git_identity(),
            capital=capital,
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
                    terminal = self.journal.append(
                        connection,
                        "SESSION_STARTED",
                        {
                            "portfolio_before": portfolio(state),
                            "fingerprints_digest": digest(current),
                            "fingerprint_keys": sorted(current),
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
                    after = ChampionSession(
                        inputs, self.config, screen_source=self.screen_source
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
            "status": "SESSION_COMMITTED",
            "session": selected,
            "portfolio": portfolio(after),
            "terminal_event_hash": terminal,
        }
