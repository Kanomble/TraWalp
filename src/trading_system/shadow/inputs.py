"""One read-only SQLite source snapshot, with digests of consumed local inputs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from trading_system.data.database import Database
from trading_system.shadow.contracts import digest


class LocalInputs(Database):
    """Existing Database read APIs on a pinned mode=ro/query_only transaction.

    Even inherited mutator methods cannot write through this adapter. No data
    synchronizer or provider is imported or accepted by the Shadow runner.
    """

    @contextmanager
    def snapshot(self):
        with super().read_only() as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            # Establish the read snapshot before any collection of source inputs.
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            self._connection = connection
            self.provenance = {"market_inputs": {}, "fundamental_inputs": {}, "identity_inputs": {}}
            try:
                yield self
            finally:
                self._connection = None
                connection.rollback()

    @contextmanager
    def connect(self):
        if getattr(self, "_connection", None) is None:
            raise RuntimeError("Shadow inputs require a read-only snapshot")
        yield self._connection

    read_only = connect

    def initialize(self):
        raise RuntimeError("Shadow cannot initialize a market database")

    def bars_available_as_of(self, symbol, as_of, **kwargs):
        bars = super().bars_available_as_of(symbol, as_of, **kwargs)
        key = f"{symbol}|{as_of.isoformat()}|{kwargs.get('limit')}|{kwargs.get('timeframe', '1d')}"
        self.provenance["market_inputs"][key] = digest(bars)
        return bars

    def facts_available_as_of(self, symbol, as_of, metric=None, **kwargs):
        facts = super().facts_available_as_of(symbol, as_of, metric, **kwargs)
        self.provenance["fundamental_inputs"][f"{symbol}|{as_of.isoformat()}|{metric}"] = digest(
            facts
        )
        return facts

    def list_tradable_companies(self):
        companies = super().list_tradable_companies()
        self.provenance["identity_inputs"]["tradable_companies"] = digest(companies)
        return companies

    def unresolved_sec_identity_conflict_symbols(self):
        conflicts = super().unresolved_sec_identity_conflict_symbols()
        self.provenance["identity_inputs"]["identity_conflicts"] = digest(conflicts)
        return conflicts


def separate_paths(journal_path, market_path):
    journal, market = journal_path.resolve(), market_path.resolve()
    if journal == market or (journal.exists() and market.exists() and journal.samefile(market)):
        from trading_system.shadow.contracts import ShadowError

        raise ShadowError("SHADOW_DATABASE_MUST_BE_SEPARATE")


def source_error(exc: Exception):
    """Normalize local schema/read failures without attempting data repair."""
    from trading_system.shadow.contracts import ShadowError

    if isinstance(exc, (sqlite3.Error, FileNotFoundError)):
        return ShadowError("SHADOW_LOCAL_DATA_UNAVAILABLE", reason=str(exc))
    return exc
