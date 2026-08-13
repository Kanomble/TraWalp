"""SQLite persistence with deterministic upserts and point-in-time reads."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import DailyBar, MarketSnapshot, TradableAsset


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
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol TEXT NOT NULL, timestamp TEXT NOT NULL,
                    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
                    close TEXT NOT NULL, volume INTEGER NOT NULL,
                    trade_count INTEGER, vwap TEXT,
                    PRIMARY KEY(symbol, timestamp)
                );
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
        self, symbol: str, as_of: date, metric: str | None = None
    ) -> list[FundamentalFact]:
        """Return facts actually filed by ``as_of``; never filter by period end alone."""

        query = "SELECT * FROM fundamental_facts WHERE symbol=? AND filed<=?"
        parameters: list[Any] = [symbol.upper(), as_of.isoformat()]
        if metric:
            query += " AND metric=?"
            parameters.append(metric)
        query += " ORDER BY filed, period_end, id"
        with self.connect() as connection:
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

    def upsert_bars(self, bars: Iterable[DailyBar]) -> int:
        rows = [
            (
                b.symbol,
                _iso(b.timestamp),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                b.volume,
                b.trade_count,
                str(b.vwap) if b.vwap is not None else None,
            )
            for b in bars
        ]
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,timestamp) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,
                trade_count=excluded.trade_count,vwap=excluded.vwap""",
                rows,
            )
        return len(rows)

    def latest_bar_timestamp(self, symbol: str) -> datetime | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(timestamp) AS timestamp FROM daily_bars WHERE symbol=?",
                (symbol.upper(),),
            ).fetchone()
        return datetime.fromisoformat(row["timestamp"]) if row and row["timestamp"] else None

    def latest_bar_timestamps(self, symbols: Iterable[str]) -> dict[str, datetime]:
        """Return latest timestamps in one indexed query instead of one query per symbol."""

        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT symbol,MAX(timestamp) AS timestamp FROM daily_bars
                WHERE symbol IN ({placeholders}) GROUP BY symbol""",
                normalized,
            ).fetchall()
        return {
            row["symbol"]: datetime.fromisoformat(row["timestamp"])
            for row in rows
            if row["timestamp"]
        }

    def bars_available_as_of(
        self, symbol: str, as_of: date, *, limit: int | None = None
    ) -> list[DailyBar]:
        """Return chronologically ordered bars whose trading date is not after ``as_of``."""

        query = """SELECT * FROM daily_bars
            WHERE symbol=? AND substr(timestamp,1,10)<=?
            ORDER BY timestamp DESC"""
        parameters: list[Any] = [symbol.upper(), as_of.isoformat()]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_bar_from_row(row) for row in reversed(rows)]

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
    bar_count = int(connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
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
        timestamp=datetime.fromisoformat(row["timestamp"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=row["volume"],
        trade_count=row["trade_count"],
        vwap=Decimal(row["vwap"]) if row["vwap"] else None,
    )
