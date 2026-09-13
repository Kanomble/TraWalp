"""Content fingerprints scoped to mutable inputs of one historical F research window."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic

from trading_system.data.database import Database
from trading_system.data.market_sessions import daily_warmup_start, required_daily_warmup_sessions
from trading_system.data.universe import is_financial_or_reit, is_reit


def data_fingerprint(database, config, *, start, end, diagnostics=None):
    """Hash a conservative superset of all inputs that can change this PIT discovery.

    Current asset membership, all issuer identities/SIC and SEC quarantine/reference
    state determine which histories may participate. Only safe, non-statically-excluded
    tradable companies need history. All their facts filed <= end are included (including
    old shares and old periods retained by the accounting fallback); future filings are
    excluded. Daily OHLCV spans the exact feature warmup through end, plus SPY for Daily
    qualification/benchmark. The all-symbol session *keys* preserve calendar availability.
    Universe gates are recomputed on every hash: newly tradable/unquarantined companies
    cannot slip past the scope. No price/cap/liquidity gate narrows the hashed universe.

    SQLite quote(CAST(... AS BLOB)) encodes each schema-typed column without losing
    embedded NULs or confusing NULL/empty values. Length-framed
    rows and section names make the encoding unambiguous. No per-row JSON/domain models,
    no SELECT *, no history-sized Python allocation, and no intraday data or sync cursors.
    Operational updated_at timestamps do not affect research and are intentionally absent.
    """
    started = monotonic()
    digest = hashlib.sha256()
    queries = rows_hashed = sql_queries = 0
    warmup = daily_warmup_start(
        start,
        max(
            required_daily_warmup_sessions(config),
            config.universe.market_data_days,
        ),
    )
    cutoff = (end + timedelta(days=1)).isoformat()
    with database.read_only() as connection:

        def trace(statement):
            nonlocal sql_queries
            if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                sql_queries += 1

        if diagnostics is not None:
            connection.set_trace_callback(trace)
        connection.execute("BEGIN")

        def stream(section, columns, source, parameters=(), order=""):
            nonlocal queries, rows_hashed
            digest.update(section.encode() + b"\0")
            encoded = "||'|'||".join(f"quote(CAST({column} AS BLOB))" for column in columns)
            cursor = connection.execute(
                f"SELECT {encoded} FROM {source}" + (f" ORDER BY {order}" if order else ""),
                parameters,
            )
            queries += 1
            while rows := cursor.fetchmany(4096):
                rows_hashed += len(rows)
                for row in rows:
                    raw = row[0].encode("utf-8")
                    digest.update(len(raw).to_bytes(8, "big"))
                    digest.update(raw)

        stream("assets", ("symbol", "tradable"), "assets", order="symbol")
        stream(
            "companies",
            ("cik", "symbol", "name", "sic", "sic_description"),
            "companies",
            order="symbol",
        )
        stream(
            "identity",
            ("source", "key", "value"),
            "sync_state WHERE source IN ('sec_identity_conflicts','sec_reference')",
            order="source,key",
        )

        # Resolve through the canonical resolver using this same SQLite read snapshot.
        class SnapshotDatabase(Database):
            @contextmanager
            def connect(self):
                yield connection

        snapshot = SnapshotDatabase(database.path)
        conflicts = snapshot.unresolved_sec_identity_conflict_symbols()
        companies = snapshot.list_tradable_companies()
        symbols = sorted(
            {
                company.symbol
                for company in companies
                if company.symbol not in conflicts
                and not (config.universe.exclude_reits and is_reit(company.sic))
                and not (config.universe.exclude_financials and is_financial_or_reit(company.sic))
            }
        )
        for offset in range(0, len(symbols), 200):
            batch = symbols[offset : offset + 200]
            placeholders = ",".join("?" for _ in batch)
            stream(
                f"facts:{offset}",
                (
                    "symbol",
                    "cik",
                    "metric",
                    "taxonomy",
                    "tag",
                    "value",
                    "unit",
                    "period_start",
                    "period_end",
                    "filed",
                    "fiscal_year",
                    "fiscal_period",
                    "form",
                    "accession_number",
                    "frame",
                    "id",
                ),
                f"fundamental_facts WHERE symbol IN ({placeholders}) AND filed<=?",
                [*batch, end.isoformat()],
                "symbol,metric,filed,period_end,id",
            )
        daily_symbols = sorted({*symbols, "SPY"})
        for offset in range(0, len(daily_symbols), 200):
            batch = daily_symbols[offset : offset + 200]
            placeholders = ",".join("?" for _ in batch)
            stream(
                f"daily:{offset}",
                ("symbol", "timestamp", "open", "high", "low", "close", "volume"),
                f"bars WHERE symbol IN ({placeholders}) AND timeframe='1d' "
                "AND timestamp>=? AND timestamp<?",
                [*batch, warmup.isoformat(), cutoff],
                "symbol,timestamp",
            )
        stream(
            "sessions",
            ("session",),
            "(SELECT DISTINCT substr(timestamp,1,10) AS session FROM bars "
            "WHERE timeframe='1d' AND timestamp>=? AND timestamp<?)",
            (start.isoformat(), cutoff),
            "session",
        )
    if diagnostics is not None:
        diagnostics.update(
            fingerprint_seconds=monotonic() - started,
            fingerprint_stream_queries=queries,
            fingerprint_rows=rows_hashed,
            fingerprint_sql_queries=sql_queries,
            fingerprint_symbols=len(symbols),
            fingerprint_warmup_start=warmup.isoformat(),
        )
    return digest.hexdigest()
