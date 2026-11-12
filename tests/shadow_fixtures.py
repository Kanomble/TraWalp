"""Synthetic Shadow inputs. No providers, external database or guard overrides."""

from test_champion_consolidation import bar, record

from trading_system.data.forward_market import persist
from trading_system.data.market_sessions import (
    full_history_request_window,
    trading_sessions_between,
)
from trading_system.data.observations import current_session_observation
from trading_system.shadow.inputs import LocalInputs


def official_history(session, count=300):
    start, _ = full_history_request_window(session, count)
    days = trading_sessions_between(start.date(), session)
    assert len(days) == count
    return days


def current_sip_observation(symbol, session):
    value = bar(symbol, session)
    assert current_session_observation(value, session) == "OBSERVATION_PRESENT"
    return value


def seed_history(db, symbols, session, *, count=300, missing=()):
    """Persist real synthetic SIP observations; gaps are explicit (symbol, day) pairs."""
    omitted = set(missing)
    values = [
        current_sip_observation(symbol, day)
        for symbol in symbols
        for day in official_history(session, count)
        if (symbol, day) not in omitted
    ]
    db.upsert_bars(values)
    persist(
        db,
        values,
        provider="alpaca",
        feed="sip",
        adjustment="all",
        sync_reference="synthetic-shadow-history",
    )


def history_evidence(symbols, session, *, count=300, missing=()):
    """Consistent evidence for pure qualification tests; never replaces DB coverage."""
    days, omitted = official_history(session, count), set(missing)
    result = {}
    for symbol in symbols:
        gaps = [day for day in days if (symbol, day) in omitted]
        result[symbol] = dict(
            status="incomplete" if gaps else "complete",
            expected_sessions=count,
            usable_sessions=count - len(gaps),
            missing_sessions=gaps,
            listing_history="unknown",
        )
    return result


def assert_history(db, symbols, session, *, count=300, missing=()):
    with LocalInputs(db.path).snapshot() as inputs:
        actual = inputs.history_coverage(symbols, session, count)
    assert actual == history_evidence(symbols, session, count=count, missing=missing)


def incomplete_candidate(symbol="BBB"):
    return record(symbol, quality=None, valuation=None)


def rejected_candidate(symbol="AAA"):
    # Current quality 50 is below frozen F's 65; downstream gaps are irrelevant.
    return record(symbol, quality=50)


def assert_census(payload, *, source, rejected, evaluated, unresolved):
    assert source == rejected + evaluated + unresolved
    summary = payload["qualification_summary"]
    assert summary["source_universe_count"] == source
    assert summary["deterministically_rejected_count"] == rejected
    assert summary["fully_evaluated_count"] == evaluated
    assert summary["unresolved_rank_relevant_count"] == unresolved
    assert payload["unresolved_count"] == unresolved
