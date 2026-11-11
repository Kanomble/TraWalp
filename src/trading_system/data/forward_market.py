"""Explicit forward-only SIP storage. Legacy research bars are never relabelled."""

from datetime import UTC, datetime, timedelta

from trading_system.data.observations import current_session_observation
from trading_system.models.market_data import validate_market_bar

CONTRACT = {
    "market_data_contract_id": "SIP_DAILY_ALL_FAIL_CLOSED_V1",
    "primary_feed": "sip",
    "feed": "sip",
    "fallback_policy": "NONE",
    "adjustment": "all",
    "timeframe": "1d",
    "provider": "alpaca",
}


def migrate(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS forward_market_observations (
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
        open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
        volume INTEGER NOT NULL, trade_count INTEGER, vwap TEXT,
        provider TEXT NOT NULL, feed TEXT NOT NULL, adjustment TEXT NOT NULL,
        session TEXT NOT NULL, retrieved_at TEXT NOT NULL, sync_reference TEXT NOT NULL,
        PRIMARY KEY(symbol,session,timeframe,provider,feed,adjustment))""")


def persist(database, bars, *, provider, feed, adjustment, sync_reference, retrieved_at=None):
    """Only the explicit provider acquisition path supplies provenance; no legacy copying."""
    if not provider or not feed or not adjustment or not sync_reference:
        raise ValueError("FORWARD_PROVENANCE_REQUIRED")
    retrieved_at = retrieved_at or datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        raise ValueError("FORWARD_RETRIEVAL_TIME_INVALID")
    rows = []
    for bar in bars:
        validate_market_bar(bar)
        if bar.timeframe.value != "1d":
            raise ValueError("FORWARD_DAILY_REQUIRED")
        rows.append(
            (
                bar.symbol.upper(),
                bar.timeframe.value,
                bar.timestamp.isoformat(),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                bar.volume,
                bar.trade_count,
                str(bar.vwap) if bar.vwap is not None else None,
                provider,
                feed,
                adjustment,
                bar.timestamp.date().isoformat(),
                retrieved_at.isoformat(),
                sync_reference,
            )
        )
    with database.connect() as connection:
        migrate(connection)
        connection.executemany(
            """INSERT INTO forward_market_observations
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,session,timeframe,provider,feed,adjustment) DO UPDATE SET
            timestamp=excluded.timestamp,open=excluded.open,high=excluded.high,
            low=excluded.low,close=excluded.close,volume=excluded.volume,
            trade_count=excluded.trade_count,vwap=excluded.vwap,
            retrieved_at=excluded.retrieved_at,sync_reference=excluded.sync_reference""",
            rows,
        )
    return len(rows)


def install_forward_view(connection):
    """Connection-local view routes ALL inherited market queries away from legacy bars.

    TEMP schema is private, in-memory, including on a mode=ro source connection.
    Missing migration is represented by an empty view, never a legacy fallback.
    """

    def provenance_valid(timestamp, session, retrieved_at):
        try:
            source = datetime.fromisoformat(timestamp)
            retrieved = datetime.fromisoformat(retrieved_at)
            return int(
                source.tzinfo is not None
                and retrieved.tzinfo is not None
                and source.date().isoformat() == session
            )
        except (TypeError, ValueError):
            return 0

    connection.create_function("forward_provenance_valid", 3, provenance_valid, deterministic=True)
    exists = connection.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name=?", ("forward_market_observations",)
    ).fetchone()
    columns = "symbol,timeframe,timestamp,open,high,low,close,volume,trade_count,vwap"
    if exists:
        source = f"""SELECT {columns} FROM main.forward_market_observations
            WHERE provider='alpaca' AND feed='sip' AND adjustment='all' AND timeframe='1d'
            AND session=substr(timestamp,1,10) AND retrieved_at<>'' AND sync_reference<>''
            AND forward_provenance_valid(timestamp,session,retrieved_at)=1"""
    else:
        source = f"SELECT {columns} FROM main.bars WHERE 0"
    connection.execute(f"CREATE TEMP VIEW bars AS {source}")


def sync_forward(database, client, start, end, symbols):
    """Use the official historical client, exclusively in an explicit forward context."""
    from trading_system.data.market_sessions import (
        latest_completed_trading_session,
        trading_sessions_between,
    )

    if client.feed.value != "sip" or client.adjustment.value != "all":
        raise ValueError("FORWARD_SIP_ALL_REQUIRED")
    if start > end or end > latest_completed_trading_session(datetime.now(UTC)):
        raise ValueError("FORWARD_COMPLETED_SESSION_RANGE_REQUIRED")
    state = {
        "status": "running",
        "last_started_at": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": "sip",
        "adjustment": "all",
    }
    database.set_sync_value("dataset", "forward_sip", state)
    begin = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    try:
        requested = sorted(set(symbols) | {"SPY"})
        bars = list(client.daily_bars(requested, begin, finish))
        expected = set(trading_sessions_between(start, end))
        usable = {symbol: set() for symbol in requested}
        for bar in bars:
            if bar.symbol not in usable or bar.timestamp.date() not in expected:
                raise ValueError("FORWARD_RESPONSE_OUTSIDE_REQUEST")
            if current_session_observation(bar, bar.timestamp.date()) == "OBSERVATION_PRESENT":
                usable[bar.symbol].add(bar.timestamp.date())
        count = persist(
            database,
            bars,
            provider="alpaca",
            feed="sip",
            adjustment="all",
            sync_reference=f"update-bars/forward-sip/{start}/{end}",
        )
    except Exception:
        database.set_sync_value(
            "dataset",
            "forward_sip",
            {
                **state,
                "status": "failed",
                "last_completed_at": datetime.now(UTC).isoformat(),
                "errors": 1,
            },
        )
        raise
    database.set_sync_value(
        "dataset",
        "forward_sip",
        {
            "status": "success",
            "last_success_at": datetime.now(UTC).isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": "sip",
            "adjustment": "all",
            "records": count,
            # Success attests acquisition/persistence, not listing age or coverage.
            "coverage_status": "complete"
            if all(days == expected for days in usable.values())
            else "incomplete",
            "coverage_expected_sessions": len(expected),
            "coverage_missing_sessions": {
                symbol: len(expected - days) for symbol, days in usable.items() if days != expected
            },
            "listing_history": "unknown",
        },
    )
    return count


def valid_current(bar, session):
    return current_session_observation(bar, session) == "OBSERVATION_PRESENT"
