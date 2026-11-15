"""Opt-in G5 adapter for isolated A-V1 work datasets. No provider or sync entry point."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from trading_system.data.canonical_facts import install_writer_guard, write_company


class IsolatedCanonicalSecWriter:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            install_writer_guard(c)
            yield c
        finally:
            # External transactions must have an explicit owner; never implicitly commit.
            c.close()

    def update_company(self, company, facts, accessions, *, connection=None, **contract):
        if connection is not None:
            # Validate contract + install the same allowlist on supplied connections.
            install_writer_guard(connection)
            return write_company(connection, company, facts, accessions, **contract)
        with self.connect() as c:
            return write_company(c, company, facts, accessions, **contract)
