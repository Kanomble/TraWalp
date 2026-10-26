"""Event reducer: reconstruct the ledger without rereading or reevaluating source data."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import date

from pydantic import TypeAdapter

from trading_system.backtest.engine import _PendingEntry
from trading_system.backtest.position_manager import PositionState
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.shadow.contracts import ShadowError, digest, normalized

POSITION = TypeAdapter(PositionState)
PENDING = TypeAdapter(_PendingEntry)
TRANSITIONS = {
    "ENTRY_INTENT_CREATED",
    "ENTRY_EXECUTED",
    "ENTRY_REJECTED",
    "POSITION_MANAGED",
    "EXIT_EXECUTED",
}


def initial_state(capital: float) -> dict:
    return {
        "cash": capital,
        "position": None,
        "pending": None,
        "realized_pnl": 0.0,
        "position_sequence": 0,
        "last_exits": {},
    }


def portfolio(state: dict) -> dict:
    position = state["position"]
    value = position["quantity"] * position["last_price"] if position else 0.0
    unrealized = (
        (position["last_price"] - position["entry_price"]) * position["quantity"]
        - position["entry_commission"]
        if position
        else 0.0
    )
    return {**deepcopy(state), "equity": state["cash"] + value, "unrealized_pnl": unrealized}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ShadowError("SHADOW_JOURNAL_INTEGRITY_FAILURE", reason=reason)


def validate_state(state: dict, capital: float) -> None:
    require(set(state) == set(initial_state(capital)), "state schema")
    require(math.isfinite(state["cash"]) and state["cash"] >= -1e-8, "cash")
    require(math.isfinite(state["realized_pnl"]), "realized pnl")
    require(
        isinstance(state["position_sequence"], int) and state["position_sequence"] >= 0, "sequence"
    )
    require(not (state["position"] and state["pending"]), "C1 capacity")
    position = state["position"]
    if position:
        typed = POSITION.validate_python(position)
        require(normalized(typed) == position, "position serialization")
        require(typed.quantity > 0 and typed.holding_days >= 1, "position quantities")
        require(typed.variant is FROZEN_CHAMPION_F.variant, "position champion identity")
        require(
            math.isclose(typed.position_value, typed.quantity * typed.entry_price, abs_tol=1e-10),
            "position cost basis",
        )
    if state["pending"]:
        typed = PENDING.validate_python(state["pending"]["order"])
        encoded = normalized(typed)
        if "current_market_observation" not in state["pending"]["order"]["record"]:
            # Optional forward provenance was absent in the original V1 record.
            # Verify its old bytes without rewriting events or granting readiness.
            encoded["record"].pop("current_market_observation")
        require(encoded == state["pending"]["order"], "pending serialization")
        require(typed.daily_candidate_rank == 1, "pending rank")
        require(typed.variant is FROZEN_CHAMPION_F.variant, "pending champion identity")
        execution = date.fromisoformat(state["pending"]["expected_execution_session"])
        require(
            execution > typed.signal_date
            and trading_sessions_between(typed.signal_date, execution)
            == [typed.signal_date, execution],
            "pending execution boundary",
        )
    # Ledger conservation holds independently of marks and slippage: cost basis
    # plus remaining cash equals inception capital plus realized net profit.
    cost = position["position_value"] + position["entry_commission"] if position else 0.0
    require(
        math.isclose(state["cash"] + cost, capital + state["realized_pnl"], abs_tol=1e-8),
        "ledger conservation",
    )


def reduce_transition(state: dict, kind: str, payload: dict, capital: float) -> dict:
    require(kind in TRANSITIONS, "unknown economic transition")
    require(payload["before_state_digest"] == digest(state), "transition predecessor state")
    after = payload["after"]
    validate_state(after, capital)
    before_position, next_position = state["position"], after["position"]
    if kind == "ENTRY_INTENT_CREATED":
        require(not before_position and not state["pending"], "intent capacity")
        require(after["pending"] is not None, "intent missing")
        expected = {**state, "pending": after["pending"]}
    elif kind in {"ENTRY_EXECUTED", "ENTRY_REJECTED"}:
        require(state["pending"] is not None and before_position is None, "entry without intent")
        if kind == "ENTRY_EXECUTED":
            require(next_position is not None, "entry position missing")
            require(
                next_position["symbol"] == state["pending"]["order"]["record"]["symbol"], "symbol"
            )
            require(
                next_position["entry_date"] == state["pending"]["expected_execution_session"],
                "entry timing",
            )
            require(next_position["holding_days"] == 1, "entry holding count")
            expected = {
                **state,
                "pending": None,
                "position": next_position,
                "cash": state["cash"]
                - next_position["position_value"]
                - next_position["entry_commission"],
                "position_sequence": state["position_sequence"] + 1,
            }
        else:
            expected = {**state, "pending": None}
    elif kind == "EXIT_EXECUTED":
        require(before_position is not None and next_position is None, "exit position")
        trade = payload["trade"]
        require(trade["symbol"] == before_position["symbol"], "exit symbol")
        require(trade["quantity"] == before_position["quantity"], "exit quantity")
        expected = {
            **state,
            "position": None,
            "cash": state["cash"] + trade["exit_price"] * trade["quantity"] - trade["fees"],
            "realized_pnl": state["realized_pnl"] + trade["pnl"],
            "last_exits": {**state["last_exits"], trade["symbol"]: payload["completed_position"]},
        }
    else:
        require(before_position is not None and next_position is not None, "management position")
        for name in (
            "position_id",
            "symbol",
            "quantity",
            "entry_price",
            "stop_price",
            "target_price",
        ):
            require(before_position[name] == next_position[name], f"management changed {name}")
        expected_holding = before_position["holding_days"] + (
            1 if payload["phase"] == "SESSION_OPEN" else 0
        )
        require(next_position["holding_days"] == expected_holding, "holding counter transition")
        expected = {**state, "position": next_position}
    # Do not compare floats via rounded JSON. Engine subtraction order is preserved;
    # a tolerance is used only for independently reconstructed cash arithmetic.
    require(math.isclose(expected["cash"], after["cash"], abs_tol=1e-10), "transition cash")
    expected["cash"] = after["cash"]
    require(expected == after, "transition state mismatch")
    return deepcopy(after)
