"""Dataset binding and durable pending requests for SEC sync stages."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from trading_system.data.canonical_facts import Admission, require_contract
from trading_system.data.sec_persistence import (
    CanonicalSecPersistence,
    LegacySecPersistence,
    SecRequestArchive,
    SecRequestEnvelope,
    digest,
    json_bytes,
)

PENDING = "sec_pending_request_v1"


@dataclass(frozen=True)
class IsolatedCanonicalSec:
    """Explicit API-only scope, with an evidence-based admission planner."""

    directory: Path
    admissions: Callable[[SecRequestEnvelope], Mapping[tuple[str, str], Admission]]
    policy_version: str


@contextmanager
def stage_lock(path):
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield  # Closing releases the lock, also on process exit.


class SecSyncPersistence:
    def __init__(self, database, candidate: IsolatedCanonicalSec | None, *, force: bool):
        self.database = database
        self.candidate = candidate
        self.force = force
        path = Path(database.path).absolute()
        self.path = path
        if path.resolve() != path:
            raise ValueError("SEC_PERSISTENCE_LINKED_TARGET")
        if candidate is not None and (
            force
            or not candidate.directory.name.startswith("f01-sec-isolated-")
            or path != candidate.directory.absolute() / "f01-sec-candidate.sqlite3"
        ):
            raise ValueError("SEC_CANONICAL_ISOLATED_TARGET_REQUIRED")
        stat = path.stat()
        if stat.st_nlink != 1:
            raise ValueError("SEC_PERSISTENCE_LINKED_TARGET")
        self.file_identity = (stat.st_dev, stat.st_ino)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
            marker = c.execute("SELECT 1 FROM sqlite_master WHERE name='fact_contract'").fetchone()
            if bool(marker) != (candidate is not None):
                raise ValueError("SEC_SYNC_SCHEMA_SCOPE_MISMATCH")
            if candidate is not None:
                require_contract(c, writer=False)
                schema = c.execute("SELECT version,schema_hash FROM fact_contract").fetchone()
                origin = c.execute(
                    "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1"
                ).fetchone()
                if origin is None:
                    raise ValueError("SEC_CANONICAL_DATASET_ORIGIN_REQUIRED")
            else:
                if (
                    c.execute(
                        "SELECT type FROM sqlite_master WHERE name='fundamental_facts'"
                    ).fetchone()
                    != ("table",)
                    or c.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='canonical_migration'"
                    ).fetchone()
                ):
                    raise ValueError("SEC_LEGACY_SCHEMA_REQUIRED")
                schema, origin = ("legacy",), None
        self.binding = digest(json_bytes([str(path.resolve()), self.file_identity, schema, origin]))
        self.directory = path.with_name(path.name + ".sec-requests")
        self.directory.mkdir(exist_ok=True)
        archive = self.directory / self.binding
        archive.mkdir(exist_ok=True)
        if archive.resolve() != archive:
            raise ValueError("SEC_REQUEST_ARCHIVE_LINKED")
        self.archive = SecRequestArchive(archive)
        if candidate is not None:
            self.backend = CanonicalSecPersistence(path, self.archive)
        else:
            self.backend = LegacySecPersistence(
                path,
                self.archive,
                connection_guard=getattr(database, "authorize_sec_connection", None),
            )

    def lock(self):
        return stage_lock(self.directory / "stage.lock")

    def require_target(self):
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.file_identity:
            raise ValueError("SEC_SYNC_TARGET_REPLACED")

    def version(self):
        if self.candidate is None:
            return None
        with self.database.read_only() as c:
            return c.execute("SELECT current_version FROM fact_contract").fetchone()[0]

    def submit(self, envelope):
        self.require_target()
        marker = {
            "binding": self.binding,
            "request_id": envelope.request_id,
            "input_hash": envelope.input_hash,
            "expected_version": self.version(),
            "force": self.force,
        }
        self.archive.store(envelope)
        # A crash before this marker cannot have started the fact transaction.
        self.database.set_sync_value(PENDING, envelope.company.cik, marker)
        return self.backend.persist(envelope, expected_version=marker["expected_version"])

    def resume(self, marker, symbol, cik, *, evidence_callback=None):
        self.require_target()
        if marker["binding"] != self.binding or marker["force"] is not self.force:
            raise ValueError("SEC_PENDING_TARGET_OR_SCOPE_MISMATCH")
        envelope = self.archive.load(marker["request_id"])
        if (
            envelope.company.symbol != symbol
            or envelope.company.cik != cik
            or envelope.input_hash != marker["input_hash"]
            or envelope.submissions_origin != ("verified_local" if self.force else "provider")
            or self.candidate is not None
            and envelope.policy_version != self.candidate.policy_version
        ):
            raise ValueError("SEC_PENDING_REQUEST_BINDING_MISMATCH")
        if evidence_callback is not None:
            evidence_callback(cik, json.loads(envelope.payload_json)["companyfacts"])
        return self.backend.persist(envelope, expected_version=marker["expected_version"])
