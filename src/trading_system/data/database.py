"""SQLite persistence with deterministic upserts and point-in-time reads."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_system.data.sec_identity import resolve_sec_identities
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import (
    BarTimeframe,
    DailyBar,
    MarketDataBar,
    MarketSnapshot,
    TradableAsset,
    validate_market_bar,
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    symbol TEXT PRIMARY KEY, name TEXT NOT NULL, exchange TEXT,
                    tradable INTEGER NOT NULL, fractionable INTEGER NOT NULL,
                    shortable INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS companies (
                    cik TEXT PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    sic TEXT, sic_description TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fundamental_facts (
                    id INTEGER PRIMARY KEY,
                    cik TEXT NOT NULL, symbol TEXT NOT NULL, metric TEXT NOT NULL,
                    taxonomy TEXT NOT NULL, tag TEXT NOT NULL, value TEXT NOT NULL,
                    unit TEXT NOT NULL, period_start TEXT, period_end TEXT NOT NULL,
                    filed TEXT NOT NULL, fiscal_year INTEGER, fiscal_period TEXT,
                    form TEXT NOT NULL, accession_number TEXT NOT NULL DEFAULT '',
                    frame TEXT NOT NULL DEFAULT '',
                    UNIQUE(cik, metric, tag, unit, period_start, period_end, filed,
                           accession_number, frame)
                );
                CREATE INDEX IF NOT EXISTS ix_facts_pit
                    ON fundamental_facts(symbol, metric, filed, period_end);
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
                    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
                    close TEXT NOT NULL, volume INTEGER NOT NULL,
                    trade_count INTEGER, vwap TEXT,
                    PRIMARY KEY(symbol, timeframe, timestamp)
                );
                CREATE INDEX IF NOT EXISTS ix_bars_timeframe_timestamp
                    ON bars(timeframe, timestamp);
                CREATE INDEX IF NOT EXISTS ix_bars_symbol_timeframe_timestamp
                    ON bars(symbol, timeframe, timestamp);
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    symbol TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    latest_trade_price TEXT,
                    latest_trade_timestamp TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_market_snapshots_observed_at
                    ON market_snapshots(observed_at);
                CREATE TABLE IF NOT EXISTS raw_sec_cache (
                    cik TEXT NOT NULL, endpoint TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(cik, endpoint)
                );
                CREATE TABLE IF NOT EXISTS sync_state (
                    source TEXT NOT NULL, key TEXT NOT NULL, updated_at TEXT NOT NULL,
                    value TEXT, PRIMARY KEY(source, key)
                );
                """
            )
            _migrate_daily_bars(connection)

    def upsert_assets(self, assets: Iterable[TradableAsset]) -> int:
        rows = [
            (a.symbol, a.name, a.exchange, a.tradable, a.fractionable, a.shortable, _now())
            for a in assets
        ]
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, exchange=excluded.exchange,
                tradable=excluded.tradable, fractionable=excluded.fractionable,
                shortable=excluded.shortable, updated_at=excluded.updated_at""",
                rows,
            )
        return len(rows)

    def reconcile_assets(
        self,
        assets: Iterable[TradableAsset],
        *,
        minimum_retained_ratio: float = 0.5,
    ) -> dict[str, int]:
        """Atomically reconcile an authoritative current tradable-asset snapshot."""

        if not 0 < minimum_retained_ratio <= 1:
            raise ValueError("minimum_retained_ratio must be within (0, 1]")
        current = list(assets)
        if not current:
            raise ValueError("Refusing to reconcile an empty Alpaca asset snapshot")
        symbols = [asset.symbol for asset in current]
        if len(set(symbols)) != len(symbols):
            raise ValueError("Alpaca asset snapshot contains duplicate symbols")
        if any(not asset.tradable for asset in current):
            raise ValueError("Asset reconciliation requires a fully tradable snapshot")

        now = _now()
        rows = [
            (
                asset.symbol,
                asset.name,
                asset.exchange,
                asset.tradable,
                asset.fractionable,
                asset.shortable,
                now,
            )
            for asset in current
        ]
        with self.connect() as connection:
            tradable_before = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM assets WHERE tradable=1"
                ).fetchone()["count"]
            )
            minimum_expected = math.ceil(tradable_before * minimum_retained_ratio)
            if tradable_before and len(current) < minimum_expected:
                raise ValueError(
                    "Refusing suspicious Alpaca asset snapshot: "
                    f"received={len(current)} previous_tradable={tradable_before} "
                    f"minimum_expected={minimum_expected}"
                )
            connection.execute(
                "CREATE TEMP TABLE current_asset_symbols(symbol TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO current_asset_symbols(symbol) VALUES (?)",
                ((symbol,) for symbol in symbols),
            )
            connection.executemany(
                """INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, exchange=excluded.exchange,
                tradable=excluded.tradable, fractionable=excluded.fractionable,
                shortable=excluded.shortable, updated_at=excluded.updated_at""",
                rows,
            )
            deactivated = connection.execute(
                """UPDATE assets SET tradable=0, updated_at=?
                WHERE tradable=1 AND NOT EXISTS (
                    SELECT 1 FROM current_asset_symbols current
                    WHERE current.symbol=assets.symbol
                )""",
                (now,),
            ).rowcount
            tradable_after = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM assets WHERE tradable=1"
                ).fetchone()["count"]
            )
            if tradable_after != len(current):
                raise RuntimeError(
                    "Asset reconciliation invariant failed: "
                    f"snapshot={len(current)} tradable_after={tradable_after}"
                )
        return {
            "records_updated": len(current) + deactivated,
            "assets_received": len(current),
            "assets_upserted": len(current),
            "assets_deactivated": deactivated,
            "tradable_assets_after": tradable_after,
        }

    def list_tradable_asset_symbols(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT symbol FROM assets WHERE tradable=1 ORDER BY symbol"
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def list_tradable_assets(self) -> list[TradableAsset]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT symbol,name,exchange,tradable,fractionable,shortable
                FROM assets WHERE tradable=1 ORDER BY symbol"""
            ).fetchall()
        return [
            TradableAsset(
                symbol=row["symbol"],
                name=row["name"],
                exchange=row["exchange"],
                tradable=bool(row["tradable"]),
                fractionable=bool(row["fractionable"]),
                shortable=bool(row["shortable"]),
            )
            for row in rows
        ]

    def upsert_company(self, company: CompanyIdentity) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cik) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
                sic=excluded.sic, sic_description=excluded.sic_description,
                updated_at=excluded.updated_at""",
                (
                    company.cik,
                    company.symbol,
                    company.name,
                    company.sic,
                    company.sic_description,
                    _now(),
                ),
            )

    def upsert_facts(self, facts: Iterable[FundamentalFact]) -> int:
        rows = _fact_rows(facts)
        with self.connect() as connection:
            _execute_fact_upsert(connection, rows)
        return len(rows)

    def upsert_sec_company_update(
        self,
        company: CompanyIdentity,
        facts: Iterable[FundamentalFact],
        accessions: Iterable[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Persist one changed SEC company atomically with a single commit."""

        rows = _fact_rows(facts)
        now = _now()
        if connection is None:
            with self.connect() as owned_connection:
                _execute_sec_company_update(
                    owned_connection,
                    company,
                    rows,
                    accessions,
                    now,
                )
        else:
            _execute_sec_company_update(connection, company, rows, accessions, now)
        return len(rows)

    def set_sync_value(
        self,
        source: str,
        key: str,
        value: Any,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        parameters = (source, key, _now(), json.dumps(value, separators=(",", ":")))
        query = """INSERT INTO sync_state(source,key,updated_at,value) VALUES (?,?,?,?)
            ON CONFLICT(source,key) DO UPDATE SET
            updated_at=excluded.updated_at,value=excluded.value"""
        if connection is None:
            with self.connect() as owned_connection:
                owned_connection.execute(query, parameters)
        else:
            connection.execute(query, parameters)

    def sync_value(self, source: str, key: str) -> Any | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM sync_state WHERE source=? AND key=?", (source, key)
            ).fetchone()
        if row is None or row["value"] is None:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def sync_values(self, source: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT key,value FROM sync_state WHERE source=?", (source,)
            ).fetchall()
        output: dict[str, Any] = {}
        for row in rows:
            try:
                output[str(row["key"])] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                output[str(row["key"])] = row["value"]
        return output

    def unresolved_sec_identity_conflicts(self) -> dict[str, dict[str, Any]]:
        """Return locally persisted issuer/ticker conflicts used by operational guards."""

        conflicts = {
            symbol: value
            for symbol, value in self.sync_values("sec_identity_conflicts").items()
            if isinstance(value, dict) and value.get("status") == "unresolved"
        }
        cached_sec = self.sync_value("sec_reference", "ticker_to_cik")
        if not isinstance(cached_sec, dict):
            return conflicts
        current_sec = {
            str(symbol).upper(): str(cik).zfill(10) for symbol, cik in cached_sec.items()
        }
        resolution = resolve_sec_identities(
            set(self.list_tradable_asset_symbols()),
            self.company_symbol_to_cik(),
            current_sec,
        )
        for conflict in resolution.conflicts:
            conflicts.setdefault(
                conflict.symbol,
                {
                    "symbol": conflict.symbol,
                    "existing_cik": conflict.existing_cik,
                    "existing_symbol": conflict.existing_symbol,
                    "proposed_cik": conflict.proposed_cik,
                    "source": conflict.source,
                    "status": "unresolved",
                    "detected_at": None,
                    "last_seen_at": None,
                    "state_source": "cached_sec_reference",
                },
            )
        return conflicts

    def unresolved_sec_identity_conflict_symbols(self) -> set[str]:
        return set(self.unresolved_sec_identity_conflicts())

    def delete_sync_value(
        self,
        source: str,
        key: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        query = "DELETE FROM sync_state WHERE source=? AND key=?"
        if connection is None:
            with self.connect() as owned_connection:
                owned_connection.execute(query, (source, key))
        else:
            connection.execute(query, (source, key))

    def dataset_states(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT key,value FROM sync_state WHERE source='dataset' ORDER BY key"
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(row["value"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                output[str(row["key"])] = value
        return output

    def facts_available_as_of(
        self,
        symbol: str,
        as_of: date,
        metric: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[FundamentalFact]:
        """Return facts actually filed by ``as_of``; never filter by period end alone."""

        query = "SELECT * FROM fundamental_facts WHERE symbol=? AND filed<=?"
        parameters: list[Any] = [symbol.upper(), as_of.isoformat()]
        if metric:
            query += " AND metric=?"
            parameters.append(metric)
        query += " ORDER BY filed, period_end, id"
        if connection is None:
            with self.connect() as owned_connection:
                rows = owned_connection.execute(query, parameters).fetchall()
        else:
            rows = connection.execute(query, parameters).fetchall()
        return [_fact_from_row(row) for row in rows]

    def list_tradable_companies(self) -> list[CompanyIdentity]:
        """Return SEC-identified companies still marked tradable by Alpaca."""

        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.cik,c.symbol,c.name,c.sic,c.sic_description
                FROM companies c JOIN assets a ON a.symbol=c.symbol
                WHERE a.tradable=1 ORDER BY c.symbol"""
            ).fetchall()
        return [
            CompanyIdentity(
                cik=row["cik"],
                symbol=row["symbol"],
                name=row["name"],
                sic=row["sic"],
                sic_description=row["sic_description"],
            )
            for row in rows
        ]

    def company_symbol_to_cik(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT symbol,cik FROM companies").fetchall()
        return {str(row["symbol"]): str(row["cik"]) for row in rows}

    def upsert_bars(self, bars: Iterable[MarketDataBar]) -> int:
        return self.upsert_bars_with_stats(bars)["records_updated"]

    def upsert_bars_with_stats(self, bars: Iterable[MarketDataBar]) -> dict[str, int]:
        received = list(bars)
        valid: list[MarketDataBar] = []
        invalid_bars = 0
        for bar in received:
            try:
                validate_market_bar(bar)
            except ValueError:
                invalid_bars += 1
            else:
                valid.append(bar)
        unique = {
            (bar.symbol.upper(), bar.timeframe.value, _iso(bar.timestamp)): bar
            for bar in valid
        }
        rows = [
            (
                symbol,
                timeframe,
                timestamp,
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                b.volume,
                b.trade_count,
                str(b.vwap) if b.vwap is not None else None,
            )
            for (symbol, timeframe, timestamp), b in unique.items()
        ]
        if not rows:
            return {
                "bars_received": len(received),
                "bars_inserted": 0,
                "bars_updated": 0,
                "bars_unchanged": 0,
                "duplicate_bars": 0,
                "invalid_bars": invalid_bars,
                "records_updated": 0,
            }
        with self.connect() as connection:
            connection.execute(
                """CREATE TEMP TABLE incoming_bar_keys (
                symbol TEXT NOT NULL,timeframe TEXT NOT NULL,timestamp TEXT NOT NULL,
                PRIMARY KEY(symbol,timeframe,timestamp)) WITHOUT ROWID"""
            )
            connection.executemany(
                "INSERT INTO incoming_bar_keys VALUES (?,?,?)",
                ((row[0], row[1], row[2]) for row in rows),
            )
            existing_rows = connection.execute(
                """SELECT stored.* FROM bars AS stored JOIN incoming_bar_keys AS incoming
                USING(symbol,timeframe,timestamp)"""
            ).fetchall()
            existing_values = {
                (row["symbol"], row["timeframe"], row["timestamp"]): tuple(row)[3:]
                for row in existing_rows
            }
            incoming_values = {(row[0], row[1], row[2]): tuple(row[3:]) for row in rows}
            updated = sum(
                existing_values[key] != incoming_values[key] for key in existing_values
            )
            existing = len(existing_rows)
            connection.executemany(
                """INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,timeframe,timestamp) DO UPDATE SET
                open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,
                trade_count=excluded.trade_count,vwap=excluded.vwap""",
                rows,
            )
        inserted = len(rows) - existing
        return {
            "bars_received": len(received),
            "bars_inserted": inserted,
            "bars_updated": updated,
            "bars_unchanged": existing - updated,
            "duplicate_bars": len(valid) - len(rows),
            "invalid_bars": invalid_bars,
            "records_updated": len(rows),
        }

    def latest_bar_timestamp(
        self, symbol: str, timeframe: BarTimeframe | str = BarTimeframe.DAY_1
    ) -> datetime | None:
        normalized_timeframe = BarTimeframe(timeframe)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(timestamp) AS timestamp FROM bars WHERE symbol=? AND timeframe=?",
                (symbol.upper(), normalized_timeframe.value),
            ).fetchone()
        return datetime.fromisoformat(row["timestamp"]) if row and row["timestamp"] else None

    def latest_bar_timestamps(
        self,
        symbols: Iterable[str],
        timeframe: BarTimeframe | str = BarTimeframe.DAY_1,
    ) -> dict[str, datetime]:
        """Return latest timestamps in one indexed query instead of one query per symbol."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return {}
        normalized_timeframe = BarTimeframe(timeframe)
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT symbol,MAX(timestamp) AS timestamp FROM bars
                WHERE symbol IN ({placeholders}) AND timeframe=? GROUP BY symbol""",
                [*normalized, normalized_timeframe.value],
            ).fetchall()
        return {
            row["symbol"]: datetime.fromisoformat(row["timestamp"])
            for row in rows
            if row["timestamp"]
        }

    def bars_available_as_of(
        self,
        symbol: str,
        as_of: date | datetime,
        *,
        timeframe: BarTimeframe | str = BarTimeframe.DAY_1,
        limit: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[DailyBar]:
        """Return chronologically ordered bars whose trading date is not after ``as_of``."""

        normalized_timeframe = BarTimeframe(timeframe)
        query = """SELECT * FROM bars
            WHERE symbol=? AND timeframe=? AND timestamp<?
            ORDER BY timestamp DESC"""
        cutoff = (
            datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            if isinstance(as_of, date) and not isinstance(as_of, datetime)
            else as_of
        )
        parameters: list[Any] = [symbol.upper(), normalized_timeframe.value, _iso(cutoff)]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        if connection is None:
            with self.connect() as owned_connection:
                rows = owned_connection.execute(query, parameters).fetchall()
        else:
            rows = connection.execute(query, parameters).fetchall()
        return [_bar_from_row(row) for row in reversed(rows)]

    def bar_date_bounds(
        self,
        symbol: str | None = None,
        timeframe: BarTimeframe | str = BarTimeframe.DAY_1,
    ) -> tuple[date | None, date | None]:
        """Return local adjusted-bar coverage without modifying dataset state."""

        normalized_timeframe = BarTimeframe(timeframe)
        query = (
            "SELECT MIN(substr(timestamp,1,10)),MAX(substr(timestamp,1,10)) "
            "FROM bars WHERE timeframe=?"
        )
        parameters: tuple[str, ...] = (normalized_timeframe.value,)
        if symbol is not None:
            query += " AND symbol=?"
            parameters = (normalized_timeframe.value, symbol.upper())
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return (
            date.fromisoformat(row[0]) if row and row[0] else None,
            date.fromisoformat(row[1]) if row and row[1] else None,
        )

    def bar_sessions(self, start: date, end: date) -> list[date]:
        """Return dates with at least one locally stored daily bar in a requested range."""

        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT substr(timestamp,1,10) AS session FROM bars
                WHERE timeframe=? AND substr(timestamp,1,10) BETWEEN ? AND ? ORDER BY session""",
                (BarTimeframe.DAY_1.value, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [date.fromisoformat(row["session"]) for row in rows]

    def bars_on_session(self, symbols: Iterable[str], session: date) -> dict[str, DailyBar]:
        """Load one session for a bounded portfolio/order symbol set in one indexed query."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        start_inclusive = session.isoformat()
        end_exclusive = (session + timedelta(days=1)).isoformat()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM bars WHERE symbol IN ({placeholders})
                AND timeframe=? AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                [*normalized, BarTimeframe.DAY_1.value, start_inclusive, end_exclusive],
            ).fetchall()
        return {str(row["symbol"]): _bar_from_row(row) for row in rows}

    def bars_between(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        timeframe: BarTimeframe | str,
    ) -> list[MarketDataBar]:
        """Load a bounded multi-symbol timeframe range in one indexed query."""

        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("bar query boundaries must be timezone-aware")
        if start >= end:
            return []
        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return []
        normalized_timeframe = BarTimeframe(timeframe)
        placeholders = ",".join("?" for _ in normalized)
        with self.read_only() as connection:
            rows = connection.execute(
                f"""SELECT * FROM bars WHERE symbol IN ({placeholders})
                AND timeframe=? AND timestamp>=? AND timestamp<?
                ORDER BY timestamp,symbol""",
                [
                    *normalized,
                    normalized_timeframe.value,
                    _iso(start),
                    _iso(end),
                ],
            ).fetchall()
        return [_bar_from_row(row) for row in rows]

    def bar_bounds(
        self,
        symbol: str,
        timeframe: BarTimeframe | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        normalized_timeframe = BarTimeframe(timeframe)
        clauses = ["symbol=?", "timeframe=?"]
        parameters: list[Any] = [symbol.upper(), normalized_timeframe.value]
        if start is not None:
            clauses.append("timestamp>=?")
            parameters.append(_iso(start))
        if end is not None:
            clauses.append("timestamp<?")
            parameters.append(_iso(end))
        with self.read_only() as connection:
            row = connection.execute(
                f"SELECT MIN(timestamp),MAX(timestamp) FROM bars WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()
        return (
            datetime.fromisoformat(row[0]) if row and row[0] else None,
            datetime.fromisoformat(row[1]) if row and row[1] else None,
        )

    def bar_inventory(self) -> list[dict[str, Any]]:
        with self.read_only() as connection:
            rows = connection.execute(
                """SELECT timeframe,COUNT(DISTINCT symbol) AS symbols,COUNT(*) AS bars,
                MIN(timestamp) AS first_timestamp,MAX(timestamp) AS last_timestamp
                FROM bars GROUP BY timeframe ORDER BY
                CASE timeframe WHEN '1d' THEN 1 WHEN '1h' THEN 2
                WHEN '15m' THEN 3 WHEN '5m' THEN 4 ELSE 5 END"""
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_bar_batches(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        batch_size: int = 400,
    ) -> Iterator[list[DailyBar]]:
        """Yield bounded symbol batches using the `(symbol,timestamp)` index range."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        end_exclusive = (end + timedelta(days=1)).isoformat()
        start_inclusive = start.isoformat()
        with self.read_only() as connection:
            for offset in range(0, len(normalized), batch_size):
                batch = normalized[offset : offset + batch_size]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT * FROM bars WHERE symbol IN ({placeholders})
                    AND timeframe=? AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                    [*batch, BarTimeframe.DAY_1.value, start_inclusive, end_exclusive],
                ).fetchall()
                yield [_bar_from_row(row) for row in rows]

    def iter_bar_value_batches(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        batch_size: int = 400,
    ) -> Iterator[list[tuple[Any, ...]]]:
        """Yield compact raw bar values for a rebuildable historical feature run."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        end_exclusive = (end + timedelta(days=1)).isoformat()
        start_inclusive = start.isoformat()
        with self.read_only() as connection:
            for offset in range(0, len(normalized), batch_size):
                batch = normalized[offset : offset + batch_size]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT symbol,timestamp,high,low,close,volume
                    FROM bars WHERE symbol IN ({placeholders}) AND timeframe=?
                    AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                    [*batch, BarTimeframe.DAY_1.value, start_inclusive, end_exclusive],
                ).fetchall()
                yield [tuple(row) for row in rows]

    def iter_fact_batches(
        self,
        symbols: Iterable[str],
        end: date,
        *,
        batch_size: int = 100,
        metrics: Iterable[str] | None = None,
        period_end_on_or_after: date | None = None,
        retain_latest_periods: int | None = None,
    ) -> Iterator[list[FundamentalFact]]:
        """Yield PIT fact streams for selected symbols without loading the full fact table."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        selected_metrics = sorted(set(metrics or ()))
        with self.read_only() as connection:
            for offset in range(0, len(normalized), batch_size):
                batch = normalized[offset : offset + batch_size]
                placeholders = ",".join("?" for _ in batch)
                metric_clause = ""
                parameters: list[Any] = [*batch, end.isoformat()]
                if selected_metrics:
                    metric_placeholders = ",".join("?" for _ in selected_metrics)
                    metric_clause = f" AND metric IN ({metric_placeholders})"
                    parameters.extend(selected_metrics)
                period_clause = ""
                if period_end_on_or_after is not None:
                    period_clause = " AND period_end>=?"
                    parameters.append(period_end_on_or_after.isoformat())
                if period_end_on_or_after is not None and retain_latest_periods is not None:
                    # Preserve stale-but-still-selected inputs without transferring an
                    # issuer's entire history. Dense ranking retains every amendment
                    # and unit for each selected reporting period.
                    ranked_parameters = parameters[:-1]
                    ranked_parameters.extend(
                        [period_end_on_or_after.isoformat(), retain_latest_periods]
                    )
                    rows = connection.execute(
                        f"""WITH ranked AS (
                            SELECT *,DENSE_RANK() OVER (
                                PARTITION BY symbol,metric ORDER BY period_end DESC
                            ) AS period_rank
                            FROM fundamental_facts
                            WHERE symbol IN ({placeholders}) AND filed<=?{metric_clause}
                        )
                        SELECT * FROM ranked
                        WHERE period_end>=? OR period_rank<=?
                        ORDER BY symbol,filed,period_end,id""",
                        ranked_parameters,
                    ).fetchall()
                else:
                    rows = connection.execute(
                        f"""SELECT * FROM fundamental_facts WHERE symbol IN ({placeholders})
                        AND filed<=?{metric_clause}{period_clause}
                        ORDER BY symbol,filed,period_end,id""",
                        parameters,
                    ).fetchall()
                yield [_fact_from_row(row) for row in rows]

    def upsert_market_snapshots(self, snapshots: Iterable[MarketSnapshot]) -> int:
        rows = [
            (
                snapshot.symbol,
                _iso(snapshot.observed_at),
                (
                    str(snapshot.latest_trade_price)
                    if snapshot.latest_trade_price is not None
                    else None
                ),
                _iso(snapshot.latest_trade_timestamp),
            )
            for snapshot in snapshots
        ]
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO market_snapshots
                (symbol,observed_at,latest_trade_price,latest_trade_timestamp)
                VALUES (?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                observed_at=excluded.observed_at,
                latest_trade_price=excluded.latest_trade_price,
                latest_trade_timestamp=excluded.latest_trade_timestamp""",
                rows,
            )
        return len(rows)

    def latest_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_snapshots WHERE symbol=?", (symbol.upper(),)
            ).fetchone()
        if row is None:
            return None
        return MarketSnapshot(
            symbol=row["symbol"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            latest_trade_price=(
                Decimal(row["latest_trade_price"])
                if row["latest_trade_price"] is not None
                else None
            ),
            latest_trade_timestamp=(
                datetime.fromisoformat(row["latest_trade_timestamp"])
                if row["latest_trade_timestamp"]
                else None
            ),
        )

    def cache_sec_payload(self, cik: str, endpoint: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO raw_sec_cache VALUES (?, ?, ?, ?)
                ON CONFLICT(cik,endpoint) DO UPDATE SET fetched_at=excluded.fetched_at,
                payload=excluded.payload""",
                (cik, endpoint, _now(), json.dumps(payload, separators=(",", ":"))),
            )

    def cached_sec_payload(
        self,
        cik: str,
        endpoint: str,
        *,
        max_age: timedelta | None = timedelta(hours=12),
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT fetched_at,payload FROM raw_sec_cache WHERE cik=? AND endpoint=?",
                (cik, endpoint),
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if max_age is not None and datetime.now(UTC) - fetched_at.astimezone(UTC) > max_age:
            return None
        payload = json.loads(row["payload"])
        return payload if isinstance(payload, dict) else None

    def has_cached_sec_payload(self, cik: str, endpoint: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM raw_sec_cache WHERE cik=? AND endpoint=? LIMIT 1",
                (cik, endpoint),
            ).fetchone()
        return row is not None

    def known_accession_numbers(self, cik: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT accession_number FROM fundamental_facts
                WHERE cik=? AND accession_number<>''""",
                (cik,),
            ).fetchall()
        return {str(row["accession_number"]) for row in rows}

    def has_fundamental_facts(self, cik: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM fundamental_facts WHERE cik=? LIMIT 1", (cik,)
            ).fetchone()
        return row is not None

    def storage_report(self) -> dict[str, Any]:
        """Inspect allocated SQLite storage without modifying the database."""

        with self.read_only() as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            table_names = [
                str(row[0])
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
                )
            ]
            row_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in table_names
            }
            raw_endpoints = []
            if "raw_sec_cache" in table_names:
                raw_endpoints = [
                    {
                        "endpoint": str(row[0]),
                        "rows": int(row[1]),
                        "payload_bytes": int(row[2] or 0),
                        "average_payload_bytes": int(row[3] or 0),
                        "largest_payload_bytes": int(row[4] or 0),
                    }
                    for row in connection.execute(
                        """WITH payloads AS MATERIALIZED (
                            SELECT endpoint,length(CAST(payload AS BLOB)) AS payload_bytes
                            FROM raw_sec_cache
                        )
                        SELECT endpoint,COUNT(*),SUM(payload_bytes),
                        AVG(payload_bytes),MAX(payload_bytes)
                        FROM payloads GROUP BY endpoint ORDER BY endpoint"""
                    )
                ]
            object_sizes: list[dict[str, Any]] | None
            dbstat_error: str | None = None
            try:
                object_sizes = [
                    {"name": str(row[0]), "bytes": int(row[1] or 0)}
                    for row in connection.execute(
                        """SELECT name,SUM(pgsize) FROM dbstat
                        GROUP BY name ORDER BY SUM(pgsize) DESC"""
                    )
                ]
            except sqlite3.DatabaseError as exc:
                object_sizes = None
                dbstat_error = str(exc)
        return {
            "database_path": str(self.path),
            "file_bytes": self.path.stat().st_size,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist_count,
            "estimated_reclaimable_bytes": freelist_count * page_size,
            "row_counts": row_counts,
            "bar_timeframes": self.bar_inventory(),
            "raw_sec_cache": raw_endpoints,
            "object_sizes": object_sizes,
            "dbstat_error": dbstat_error,
        }

    def raw_sec_cleanup_plan(self, endpoint: str = "companyfacts") -> dict[str, Any]:
        """Report legacy cache rows safe to remove under the per-CIK fact guard."""

        with self.read_only() as connection:
            return _raw_sec_cleanup_plan(connection, endpoint)

    def cleanup_raw_sec_cache(
        self, endpoint: str = "companyfacts", *, dry_run: bool = True
    ) -> dict[str, Any]:
        """Remove guarded legacy raw payloads; never touches normalized facts or bars."""

        if not self.path.is_file():
            raise FileNotFoundError(f"Database does not exist: {self.path}")
        manager = self.read_only() if dry_run else self.connect()
        with manager as connection:
            plan = _raw_sec_cleanup_plan(connection, endpoint)
            deleted_rows = 0
            if not dry_run and plan["safe_rows"]:
                cursor = connection.execute(
                    """DELETE FROM raw_sec_cache AS cache
                    WHERE endpoint=? AND EXISTS (
                        SELECT 1 FROM fundamental_facts AS facts
                        WHERE facts.cik=cache.cik
                    )""",
                    (endpoint,),
                )
                deleted_rows = cursor.rowcount
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            freelist_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        return {
            **plan,
            "dry_run": dry_run,
            "deleted_rows": deleted_rows,
            "freelist_pages_after": freelist_pages,
            "reclaimable_bytes_after": freelist_pages * page_size,
        }

    def vacuum_requirements(self) -> dict[str, int]:
        """Return a conservative free-space check for an explicit VACUUM."""

        file_bytes = self.path.stat().st_size
        free_bytes = shutil.disk_usage(self.path.parent).free
        return {
            "database_bytes": file_bytes,
            "available_bytes": free_bytes,
            "required_temporary_bytes": file_bytes,
        }

    def vacuum(self) -> None:
        """Compact the database after an explicit caller request."""

        requirements = self.vacuum_requirements()
        if requirements["available_bytes"] < requirements["required_temporary_bytes"]:
            raise RuntimeError(
                "Insufficient free disk space for VACUUM: "
                f"need at least {requirements['required_temporary_bytes']} bytes, "
                f"have {requirements['available_bytes']} bytes"
            )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        """Open an existing database without creating files or persistent journal state."""

        if not self.path.is_file():
            raise FileNotFoundError(f"Database does not exist: {self.path}")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fact_rows(facts: Iterable[FundamentalFact]) -> list[tuple[Any, ...]]:
    return [
        (
            fact.cik,
            fact.symbol,
            fact.metric,
            fact.taxonomy,
            fact.tag,
            str(fact.value),
            fact.unit,
            _iso(fact.period_start),
            _iso(fact.period_end),
            _iso(fact.filed),
            fact.fiscal_year,
            fact.fiscal_period,
            fact.form,
            fact.accession_number or "",
            fact.frame or "",
        )
        for fact in facts
    ]


def _execute_fact_upsert(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    connection.executemany(
        """INSERT INTO fundamental_facts
        (cik,symbol,metric,taxonomy,tag,value,unit,period_start,period_end,filed,
         fiscal_year,fiscal_period,form,accession_number,frame)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT DO UPDATE SET value=excluded.value,
        fiscal_year=excluded.fiscal_year,fiscal_period=excluded.fiscal_period,
        form=excluded.form""",
        rows,
    )


def _execute_sec_company_update(
    connection: sqlite3.Connection,
    company: CompanyIdentity,
    rows: list[tuple[Any, ...]],
    accessions: Iterable[str],
    now: str,
) -> None:
    connection.execute(
        """INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
        sic=excluded.sic, sic_description=excluded.sic_description,
        updated_at=excluded.updated_at""",
        (
            company.cik,
            company.symbol,
            company.name,
            company.sic,
            company.sic_description,
            now,
        ),
    )
    _execute_fact_upsert(connection, rows)
    connection.execute(
        """INSERT INTO sync_state(source,key,updated_at,value) VALUES (?,?,?,?)
        ON CONFLICT(source,key) DO UPDATE SET
        updated_at=excluded.updated_at,value=excluded.value""",
        (
            "sec_accessions",
            company.cik,
            now,
            json.dumps(sorted(set(accessions)), separators=(",", ":")),
        ),
    )
    connection.execute(
        "DELETE FROM sync_state WHERE source='sec_companyfacts_status' AND key=?",
        (company.cik,),
    )


def _raw_sec_cleanup_plan(connection: sqlite3.Connection, endpoint: str) -> dict[str, Any]:
    row = connection.execute(
        """WITH candidates AS MATERIALIZED (
            SELECT length(CAST(cache.payload AS BLOB)) AS payload_bytes,
            EXISTS (
                SELECT 1 FROM fundamental_facts AS facts WHERE facts.cik=cache.cik
            ) AS safe
            FROM raw_sec_cache AS cache WHERE endpoint=?
        )
        SELECT COUNT(*) AS total_rows,
        COALESCE(SUM(payload_bytes),0) AS total_bytes,
        COALESCE(SUM(safe),0) AS safe_rows,
        COALESCE(SUM(CASE WHEN safe THEN payload_bytes ELSE 0 END),0) AS safe_bytes
        FROM candidates""",
        (endpoint,),
    ).fetchone()
    total_rows = int(row["total_rows"])
    total_bytes = int(row["total_bytes"])
    safe_rows = int(row["safe_rows"])
    safe_bytes = int(row["safe_bytes"])
    fact_count = int(connection.execute("SELECT COUNT(*) FROM fundamental_facts").fetchone()[0])
    bar_count = int(
        connection.execute("SELECT COUNT(*) FROM bars WHERE timeframe='1d'").fetchone()[0]
    )
    return {
        "endpoint": endpoint,
        "total_rows": total_rows,
        "total_payload_bytes": total_bytes,
        "safe_rows": safe_rows,
        "safe_payload_bytes": safe_bytes,
        "blocked_rows": total_rows - safe_rows,
        "blocked_payload_bytes": total_bytes - safe_bytes,
        "fundamental_fact_rows": fact_count,
        "daily_bar_rows": bar_count,
    }


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fact_from_row(row: sqlite3.Row) -> FundamentalFact:
    return FundamentalFact(
        cik=row["cik"],
        symbol=row["symbol"],
        metric=row["metric"],
        taxonomy=row["taxonomy"],
        tag=row["tag"],
        value=Decimal(row["value"]),
        unit=row["unit"],
        period_start=date.fromisoformat(row["period_start"]) if row["period_start"] else None,
        period_end=date.fromisoformat(row["period_end"]),
        filed=date.fromisoformat(row["filed"]),
        fiscal_year=row["fiscal_year"],
        fiscal_period=row["fiscal_period"],
        form=row["form"],
        accession_number=row["accession_number"] or None,
        frame=row["frame"] or None,
    )


def _bar_from_row(row: sqlite3.Row) -> DailyBar:
    return DailyBar(
        symbol=row["symbol"],
        timeframe=BarTimeframe(row["timeframe"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=row["volume"],
        trade_count=row["trade_count"],
        vwap=Decimal(row["vwap"]) if row["vwap"] else None,
    )


def _migrate_daily_bars(connection: sqlite3.Connection) -> None:
    """Migrate the legacy daily-only table once and retain a read compatibility view."""

    legacy = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='daily_bars'"
    ).fetchone()
    if legacy is not None and legacy["type"] == "table":
        legacy_count = int(connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
        connection.execute(
            """INSERT INTO bars(
            symbol,timeframe,timestamp,open,high,low,close,volume,trade_count,vwap)
            SELECT symbol,'1d',timestamp,open,high,low,close,volume,trade_count,vwap
            FROM daily_bars WHERE true
            ON CONFLICT(symbol,timeframe,timestamp) DO UPDATE SET
            open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
            volume=excluded.volume,trade_count=excluded.trade_count,vwap=excluded.vwap"""
        )
        migrated_count = int(
            connection.execute("SELECT COUNT(*) FROM bars WHERE timeframe='1d'").fetchone()[0]
        )
        if migrated_count < legacy_count:
            raise RuntimeError(
                "Daily-bar migration validation failed: "
                f"legacy={legacy_count} migrated={migrated_count}"
            )
        connection.execute("DROP TABLE daily_bars")
    elif legacy is not None and legacy["type"] == "view":
        connection.execute("DROP VIEW daily_bars")
    connection.execute(
        """CREATE VIEW daily_bars AS
        SELECT symbol,timestamp,open,high,low,close,volume,trade_count,vwap
        FROM bars WHERE timeframe='1d'"""
    )
    connection.execute("PRAGMA user_version = 1")
