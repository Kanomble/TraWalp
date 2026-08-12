"""SQLite persistence with deterministic upserts and point-in-time reads."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import DailyBar, TradableAsset


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
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
        rows = [
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
        with self.connect() as connection:
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
        return len(rows)

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

    def cache_sec_payload(self, cik: str, endpoint: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO raw_sec_cache VALUES (?, ?, ?, ?)
                ON CONFLICT(cik,endpoint) DO UPDATE SET fetched_at=excluded.fetched_at,
                payload=excluded.payload""",
                (cik, endpoint, _now(), json.dumps(payload, separators=(",", ":"))),
            )

    def cached_sec_payload(
        self, cik: str, endpoint: str, *, max_age: timedelta = timedelta(hours=12)
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT fetched_at,payload FROM raw_sec_cache WHERE cik=? AND endpoint=?",
                (cik, endpoint),
            ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(UTC) - fetched_at.astimezone(UTC) > max_age:
            return None
        payload = json.loads(row["payload"])
        return payload if isinstance(payload, dict) else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
