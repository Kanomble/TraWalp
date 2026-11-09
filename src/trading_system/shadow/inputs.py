"""One read-only SQLite source snapshot, with digests of consumed local inputs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from trading_system.data.database import Database, _fact_from_row
from trading_system.data.forward_market import CONTRACT, install_forward_view
from trading_system.shadow.contracts import digest


class LocalInputs(Database):
    """Existing Database read APIs on a pinned mode=ro/query_only transaction.

    Even inherited mutator methods cannot write through this adapter. No data
    synchronizer or provider is imported or accepted by the Shadow runner.
    """

    forward_sip_required = True

    def __init__(self, path, *, capture_provenance=True):
        super().__init__(path)
        self.capture_provenance = capture_provenance

    @contextmanager
    def snapshot(self):
        with super().read_only() as connection:
            install_forward_view(connection)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            # Establish the read snapshot before any collection of source inputs.
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            self._connection = connection
            self._fact_ciks = None
            self._conflict_symbols = None
            self.provenance = {
                **CONTRACT,
                "market_inputs": {},
                "fundamental_inputs": {},
                "identity_inputs": {},
            }
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
        if self.capture_provenance:
            self.provenance["market_inputs"][key] = self._market_manifest(bars)
        return bars

    def _market_manifest(self, bars):
        if not bars:
            return []
        rows = self._connection.execute(
            """SELECT * FROM main.forward_market_observations WHERE symbol=?
            AND provider='alpaca' AND feed='sip' AND adjustment='all' AND timeframe='1d'""",
            (bars[0].symbol,),
        ).fetchall()
        by_key = {(row["symbol"], row["timestamp"]): dict(row) for row in rows}
        observations = []
        for bar in bars:
            row = by_key[(bar.symbol, bar.timestamp.isoformat())]
            observations.append(
                {
                    **dict(row),
                    "symbol": bar.symbol,
                    "timeframe": "1d",
                    "source_timestamp": bar.timestamp.isoformat(),
                }
            )
        return observations

    def bars_on_session(self, symbols, session):
        symbols = tuple(sorted(set(symbols)))
        bars = super().bars_on_session(symbols, session)
        if self.capture_provenance:
            self.provenance["market_inputs"][f"execution|{session}|{','.join(symbols)}"] = [
                item for bar in bars.values() for item in self._market_manifest([bar])
            ]
        return bars

    def facts_available_as_of(self, symbol, as_of, metric=None, **kwargs):
        # Company Facts are issuer-keyed (including the storage uniqueness key).
        # Ticker labels are acquisition provenance, not an accounting identity.
        # Never join different CIKs merely because a ticker was reused, and never
        # relabel original observations or fall back on quarantined identities.
        symbol = symbol.upper()
        if self._fact_ciks is None:
            self._fact_ciks = self.company_symbol_to_cik()
        cik = self._fact_ciks.get(symbol)
        if symbol in self.unresolved_sec_identity_conflict_symbols():
            cik = None
        self.provenance["identity_inputs"][f"fundamental_cik|{symbol}"] = cik
        facts = []
        if cik is not None:
            query = "SELECT * FROM fundamental_facts WHERE cik=? AND filed<=?"
            parameters = [cik, as_of.isoformat()]
            if metric:
                query += " AND metric=?"
                parameters.append(metric)
            query += " ORDER BY filed,period_end,id"
            with self.connect() as connection:
                facts = [_fact_from_row(row) for row in connection.execute(query, parameters)]
        if self.capture_provenance:
            self.provenance["fundamental_inputs"][f"{symbol}|{as_of.isoformat()}|{metric}"] = (
                digest(facts)
            )
        return facts

    def list_tradable_companies(self):
        companies = super().list_tradable_companies()
        self.provenance["identity_inputs"]["tradable_companies"] = digest(companies)
        return companies

    def unresolved_sec_identity_conflict_symbols(self):
        if self._conflict_symbols is None:
            self._conflict_symbols = super().unresolved_sec_identity_conflict_symbols()
        conflicts = self._conflict_symbols
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
